# tap_lms/feedback_consumer/feedback_consumer.py

import frappe
import json
import pika
import time
from datetime import datetime
from typing import Dict, Optional
from ..glific_integration import get_glific_settings, start_contact_flow

class FeedbackConsumer:
    def __init__(self):
        self.connection = None
        self.channel = None
        self.settings = None

    def setup_rabbitmq(self):
        """Setup RabbitMQ connection and channel with proper error handling"""
        try:
            self.settings = frappe.get_single("RabbitMQ Settings")
            credentials = pika.PlainCredentials(
                self.settings.username, 
                self.settings.get_password('password')
            )
            
            parameters = pika.ConnectionParameters(
                host=self.settings.host,
                port=int(self.settings.port),
                virtual_host=self.settings.virtual_host,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300
            )
            
            self.connection = pika.BlockingConnection(parameters)
            self.channel = self.connection.channel()
            
            # Get queue names
            main_queue = self.settings.feedback_results_queue
            dlx_exchange = f"{main_queue}_dlx"
            dl_queue = f"{main_queue}_dead_letter"
            
            # Handle dead letter exchange (use existing settings)
            try:
                # Try to declare with existing settings first
                self.channel.exchange_declare(
                    exchange=dlx_exchange,
                    exchange_type='direct',
                    passive=True  # Check if exists
                )
                frappe.logger().info(f"Using existing dead letter exchange: {dlx_exchange}")
            except pika.exceptions.ChannelClosedByBroker:
                # Exchange doesn't exist or needs to be created
                self._reconnect()
                try:
                    # Try with durable=False (common default)
                    self.channel.exchange_declare(
                        exchange=dlx_exchange,
                        exchange_type='direct',
                        durable=False
                    )
                    frappe.logger().info(f"Created dead letter exchange: {dlx_exchange}")
                except pika.exceptions.ChannelClosedByBroker:
                    # Try with durable=True
                    self._reconnect()
                    self.channel.exchange_declare(
                        exchange=dlx_exchange,
                        exchange_type='direct',
                        durable=True
                    )
                    frappe.logger().info(f"Created durable dead letter exchange: {dlx_exchange}")
            
            # Handle dead letter queue
            try:
                self.channel.queue_declare(
                    queue=dl_queue,
                    durable=True
                )
                frappe.logger().info(f"Using/created dead letter queue: {dl_queue}")
            except pika.exceptions.ChannelClosedByBroker:
                self._reconnect()
                self.channel.queue_declare(
                    queue=dl_queue,
                    durable=True
                )
            
            # Bind dead letter queue to exchange (ignore if already bound)
            try:
                self.channel.queue_bind(
                    exchange=dlx_exchange,
                    queue=dl_queue,
                    routing_key=main_queue
                )
            except:
                pass  # Binding might already exist
            
            # Handle main queue (use existing configuration)
            try:
                self.channel.queue_declare(
                    queue=main_queue,
                    durable=True,
                    passive=True  # Use existing queue
                )
                frappe.logger().info(f"Using existing main queue: {main_queue}")
            except pika.exceptions.ChannelClosedByBroker:
                # Queue doesn't exist, create simple version
                self._reconnect()
                self.channel.queue_declare(
                    queue=main_queue,
                    durable=True
                )
                frappe.logger().info(f"Created main queue: {main_queue}")
            
            frappe.logger().info("RabbitMQ connection established successfully")
            
        except Exception as e:
            frappe.logger().error(f"Failed to setup RabbitMQ connection: {str(e)}")
            raise

    def _reconnect(self):
        """Reconnect to RabbitMQ after channel error"""
        try:
            if self.connection and not self.connection.is_closed:
                self.connection.close()
        except:
            pass
        
        credentials = pika.PlainCredentials(
            self.settings.username, 
            self.settings.get_password('password')
        )
        
        parameters = pika.ConnectionParameters(
            host=self.settings.host,
            port=int(self.settings.port),
            virtual_host=self.settings.virtual_host,
            credentials=credentials,
            heartbeat=600,
            blocked_connection_timeout=300
        )
        
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()

    def start_consuming(self):
        """Start consuming messages from the queue"""
        try:
            if not self.channel:
                self.setup_rabbitmq()
            
            frappe.logger().info(f"Starting to consume from queue: {self.settings.feedback_results_queue}")
            
            self.channel.basic_qos(prefetch_count=1)
            self.channel.basic_consume(
                queue=self.settings.feedback_results_queue,
                on_message_callback=self.process_message,
                auto_ack=False
            )
            
            self.channel.start_consuming()
            
        except KeyboardInterrupt:
            frappe.logger().info("Consumer stopped by user")
            self.stop_consuming()
            self.cleanup()
        except Exception as e:
            frappe.logger().error(f"Error in consumer: {str(e)}")
            self.cleanup()
            raise

    def process_message(self, ch, method, properties, body):
        """Process incoming feedback message with improved error handling"""
        message_data = None
        submission_id = None
        
        try:
            # Start new database transaction
            frappe.db.begin()
            
            # Parse and validate message
            try:
                message_data = json.loads(body)
                submission_id = message_data.get("submission_id")
                print(f"Processing feedback for : {submission_id}")

                if not submission_id:
                    raise ValueError("Missing submission_id in message")
                    
                if not message_data.get("feedback"):
                    raise ValueError("Missing feedback data in message")
                    
            except (json.JSONDecodeError, ValueError) as e:
                frappe.logger().error(f"Invalid message format: {str(e)}. Body: {body}")
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return
            
            frappe.logger().info(f"Processing feedback for submission: {submission_id}")
            frappe.db.commit()
            
            # Check if submission exists
            if not frappe.db.exists("ImgSubmission", submission_id):
                frappe.logger().error(f"ImgSubmission {submission_id} not found")
                # get a list of existing submission ids for logging
                # existing_ids = frappe.db.get_all("ImgSubmission", fields=["name"], limit=5)
                # existing_ids_list = [doc.name for doc in existing_ids]
                # print(f"Existing ImgSubmission IDs (sample): {existing_ids_list}")
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return
            
            # Process the message
            self.update_submission(message_data)
            
            # Send Glific notification (non-critical - don't fail message if this fails)
            try:
                self.send_glific_notification(message_data)
            except Exception as glific_error:
                frappe.logger().warning(f"Glific notification failed for {submission_id}: {str(glific_error)}")
                # Continue processing - notification failure shouldn't fail the entire message
            
            # Commit transaction
            frappe.db.commit()
            
            # Acknowledge message only after successful processing
            ch.basic_ack(delivery_tag=method.delivery_tag)
            
            frappe.logger().info(f"Successfully processed feedback for submission: {submission_id}")
            
        except Exception as e:
            # Rollback database transaction
            frappe.db.rollback()
            
            error_msg = str(e)
            frappe.logger().error(f"Error processing submission {submission_id}: {error_msg}")
            
            # Determine if error is retryable
            if self.is_retryable_error(e):
                frappe.logger().warning(f"Retryable error for submission {submission_id}, will retry")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            else:
                frappe.logger().error(f"Non-retryable error for submission {submission_id}, rejecting message")
                # Mark submission as failed and reject message
                try:
                    if submission_id:
                        self.mark_submission_failed(submission_id, error_msg)
                        frappe.db.commit()
                except:
                    frappe.db.rollback()
                
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)

    def is_retryable_error(self, error):
        """Determine if an error should be retried"""
        error_str = str(error).lower()
        
        # Non-retryable errors - these should not be retried
        non_retryable_patterns = [
            'does not exist',
            'not found',
            'invalid',
            'permission denied',
            'duplicate',
            'constraint violation',
            'missing submission_id',
            'missing feedback data',
            'validation error'
        ]
        
        for pattern in non_retryable_patterns:
            if pattern in error_str:
                return False
        
        # All other errors are considered retryable (database locks, network issues, etc.)
        return True

    def update_submission_old(self, message_data: Dict):
        """Update ImgSubmission with feedback data - FIXED to handle correct grade path"""
        try:
            submission_id = message_data["submission_id"]
            feedback_data = message_data["feedback"]
            
            # Get submission document
            submission = frappe.get_doc("ImgSubmission", submission_id)
            
            # Extract grade from correct path: message_data["feedback"]["grade_recommendation"]
            grade_recommendation = feedback_data.get("grade_recommendation", "0")
            
            # Handle grade conversion safely
            try:
                if isinstance(grade_recommendation, str):
                    # Remove any non-numeric characters except decimal point
                    grade_clean = ''.join(c for c in grade_recommendation if c.isdigit() or c == '.')
                    grade = float(grade_clean) if grade_clean else 0.0
                else:
                    grade = float(grade_recommendation)
            except (ValueError, TypeError):
                grade = 0.0
                frappe.logger().warning(f"Could not parse grade '{grade_recommendation}' for submission {submission_id}, using 0.0")
            
            # Handle plagiarism score
            plagiarism_score = message_data.get("plagiarism_score", 0)
            try:
                plagiarism_score = float(plagiarism_score)
            except (ValueError, TypeError):
                plagiarism_score = 0.0
            
            # Prepare update data with safe defaults
            update_data = {
                "status": "Completed",
                "grade": grade,
                "plagiarism_result": plagiarism_score,
                "feedback_summary": message_data.get("summary", ""),
                "overall_feedback": feedback_data.get("overall_feedback", ""),
                "completed_at": datetime.now()
            }
            
            # Handle JSON fields safely
            similar_sources = message_data.get("similar_sources", [])
            if isinstance(similar_sources, list):
                update_data["similar_sources"] = json.dumps(similar_sources)
            else:
                update_data["similar_sources"] = json.dumps([])
            
            # Store complete feedback as JSON
            if isinstance(feedback_data, dict):
                update_data["generated_feedback"] = json.dumps(feedback_data)
            else:
                update_data["generated_feedback"] = json.dumps({})
            
            # Update the document
            submission.update(update_data)
            submission.save(ignore_permissions=True)
            
            frappe.logger().info(f"Updated ImgSubmission {submission_id}: grade={grade}, status=Completed")
            
        except Exception as e:
            frappe.logger().error(f"Error updating ImgSubmission {submission_id}: {str(e)}")
            raise


    def update_submission(self, message_data: Dict):
        """Update ImgSubmission with comprehensive plagiarism data"""
        try:
            
            submission_id = message_data["submission_id"]
            feedback_data = message_data.get("feedback", {})

            # Get submission document
            submission = frappe.get_doc("ImgSubmission", submission_id)
            print(f"Updating submission : {submission_id}")

            # Extract plagiarism data
            is_plagiarized = message_data.get("is_plagiarized", False)
            is_ai_generated = message_data.get("is_ai_generated", False)
            match_type = message_data.get("match_type", "original")
            plagiarism_source = message_data.get("plagiarism_source", "none")
            similarity_score = message_data.get("similarity_score", 0.0)
            ai_detection_source = message_data.get("ai_detection_source")
            ai_confidence = message_data.get("ai_confidence", 0.0)
            similar_sources = message_data.get("similar_sources", [])

            # Determine plagiarism_status
            plagiarism_status = self._determine_plagiarism_status(
                is_plagiarized, is_ai_generated, match_type, plagiarism_source
            )

            # Determine result_status
            result_status = self._determine_result_status(is_plagiarized, is_ai_generated)

            # Extract grade
            grade = self._extract_grade(feedback_data, submission_id)

            # Prepare update data
            update_data = {
                "status": "Completed",
                "result_status": result_status,
                "completed_at": datetime.now(),

                # Plagiarism fields
                "plagiarism_status": plagiarism_status,
                "is_plagiarized": is_plagiarized,
                "match_type": match_type,
                "plagiarism_source": plagiarism_source,
                "similarity_score": similarity_score * 100,
                "similar_sources": json.dumps(similar_sources),

                # AI detection fields
                "is_ai_generated": is_ai_generated,
                "ai_detection_source": ai_detection_source or "",
                "ai_confidence": ai_confidence * 100,

                # Feedback fields
                "grade": grade,
                "overall_feedback": feedback_data.get("overall_feedback", ""),
                "generated_feedback": json.dumps(feedback_data),
                "feedback_summary": message_data.get("summary", ""),
                "plagiarism_result": message_data.get("plagiarism_score", 0),

            }

            submission.update(update_data)
            submission.save(ignore_permissions=True)
            frappe.db.commit()

        except Exception as e:
            # Update result_status to Failed on error
            self._mark_submission_failed(submission_id, str(e))
            frappe.logger().error(f"Error updating ImgSubmission: {str(e)}")
            raise

    def _determine_result_status(self, is_plagiarized: bool, is_ai_generated: bool) -> str:
        """Determine overall result status"""
        if is_plagiarized or is_ai_generated:
            return "Success - Flagged"
        return "Success - Original"

    def _mark_submission_failed(self, submission_id: str, error_message: str):
        """Mark submission as failed"""
        try:
            submission = frappe.get_doc("ImgSubmission", submission_id)
            submission.status = "Failed"
            
            # Add error message if field exists
            if hasattr(submission, 'error_message'):
                submission.error_message = error_message[:500]  # Limit length to prevent field overflow
            
            submission.save(ignore_permissions=True)
            
            frappe.logger().error(f"Marked submission {submission_id} as failed: {error_message}")
            
        except Exception as e:
            frappe.logger().error(f"Error marking submission {submission_id} as failed: {str(e)}")

    def _determine_plagiarism_status(
        self, is_plagiarized, is_ai_generated, match_type, plagiarism_source
    ) -> str:
        """Determine human-readable plagiarism status"""

        if is_ai_generated:
            return "Flagged - AI Generated"

        if not is_plagiarized:
            if match_type == "resubmission_allowed":
                return "Resubmission Allowed"
            return "Original"

        status_map = {
            "exact_duplicate": "Flagged - Exact Match",
            "near_duplicate": "Flagged - Near Duplicate",
            "semantic_match": "Flagged - Semantic Match",
        }

        if match_type in status_map:
            return status_map[match_type]

        if plagiarism_source in ["peer", "peer_collusion"]:
            return "Flagged - Peer Plagiarism"
        elif plagiarism_source in ["self_cross_assignment", "self_late_resubmission"]:
            return "Flagged - Self Plagiarism"

        return "Flagged - Exact Match"

    def _extract_grade(self, feedback_data, submission_id):
        grade_recommendation = feedback_data.get("grade_recommendation", "0")
        
        try:
            if isinstance(grade_recommendation, str):
                # Remove any non-numeric characters except decimal point
                grade_clean = ''.join(c for c in grade_recommendation if c.isdigit() or c == '.')
                grade = float(grade_clean) if grade_clean else 0.0
            else:
                grade = float(grade_recommendation)
        except (ValueError, TypeError):
            grade = 0.0
            frappe.logger().warning(f"Could not parse grade '{grade_recommendation}' for submission {submission_id}, using 0.0")
        
        return grade



    def send_glific_notification(self, message_data: Dict):
        """Send feedback notification via Glific with proper error handling"""
        try:
            submission_id = message_data["submission_id"]
            student_id = message_data.get("student_id")
            
            if not student_id:
                frappe.logger().warning(f"No student_id for submission {submission_id}, skipping Glific notification")
                return
            
            feedback_data = message_data.get("feedback", {})
            overall_feedback = feedback_data.get("overall_feedback", "")
            
            if not overall_feedback:
                frappe.logger().warning(f"No overall_feedback for submission {submission_id}, skipping Glific notification")
                return
            
            # Get Glific flow ID
            flow_id = frappe.get_value("Glific Flow", {"label": "feedback"}, "flow_id")
            if not flow_id:
                frappe.logger().warning("Feedback flow not configured in Glific Flow, skipping notification")
                return
            
            # Prepare flow variables
            default_results = {
                "submission_id": submission_id,
                "feedback": overall_feedback
            }
            
            # Start Glific flow
            success = start_contact_flow(
                flow_id=flow_id,
                contact_id=student_id,
                default_results=default_results
            )
            
            if success:
                frappe.logger().info(f"Sent Glific notification for submission: {submission_id}")
            else:
                frappe.logger().warning(f"Failed to send Glific notification for submission: {submission_id}")
            
        except Exception as e:
            frappe.logger().error(f"Error sending Glific notification for {submission_id}: {str(e)}")
            # Re-raise so it can be caught in process_message and handled as non-critical
            raise

    def mark_submission_failed(self, submission_id: str, error_message: str):
        """Mark submission as failed with error details"""
        try:
            submission = frappe.get_doc("ImgSubmission", submission_id)
            submission.status = "Failed"
            
            # Add error message if field exists
            if hasattr(submission, 'error_message'):
                submission.error_message = error_message[:500]  # Limit length to prevent field overflow
            
            submission.save(ignore_permissions=True)
            
            frappe.logger().error(f"Marked submission {submission_id} as failed: {error_message}")
            
        except Exception as e:
            frappe.logger().error(f"Error marking submission {submission_id} as failed: {str(e)}")

    def stop_consuming(self):
        """Stop consuming messages gracefully"""
        try:
            if self.channel and not self.channel.is_closed:
                self.channel.stop_consuming()
                frappe.logger().info("Stopped consuming messages")
        except Exception as e:
            frappe.logger().error(f"Error stopping consumer: {str(e)}")

    def cleanup(self):
        """Clean up connections and resources"""
        try:
            if self.channel and not self.channel.is_closed:
                self.channel.close()
            if self.connection and not self.connection.is_closed:
                self.connection.close()
                frappe.logger().info("RabbitMQ connection closed")
        except Exception as e:
            frappe.logger().error(f"Error cleaning up connections: {str(e)}")

    def move_to_dead_letter(self, message_data: Dict):
        """Move failed message to dead letter queue (if needed for manual processing)"""
        try:
            dead_letter_queue = f"{self.settings.feedback_results_queue}_dead_letter"
            
            self.channel.basic_publish(
                exchange='',
                routing_key=dead_letter_queue,
                body=json.dumps(message_data),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # make message persistent
                )
            )
            
            frappe.logger().warning(
                f"Moved message for submission {message_data.get('submission_id')} "
                f"to dead letter queue"
            )
        except Exception as e:
            frappe.logger().error(f"Error moving message to dead letter queue: {str(e)}")

    def get_queue_stats(self):
        """Get statistics about the queues"""
        try:
            if not self.channel:
                self.setup_rabbitmq()
            
            # Main queue stats
            main_queue_state = self.channel.queue_declare(
                queue=self.settings.feedback_results_queue,
                passive=True
            )
            main_count = main_queue_state.method.message_count
            
            # Dead letter queue stats
            try:
                dl_queue_state = self.channel.queue_declare(
                    queue=f"{self.settings.feedback_results_queue}_dead_letter",
                    passive=True
                )
                dl_count = dl_queue_state.method.message_count
            except:
                dl_count = 0
            
            return {
                "main_queue": main_count,
                "dead_letter_queue": dl_count
            }
            
        except Exception as e:
            frappe.logger().error(f"Error getting queue stats: {str(e)}")
            return {"main_queue": 0, "dead_letter_queue": 0}
