# tap_lms/feedback_handler/feedback_consumer.py

import frappe
import json
import pika
from typing import Dict

from ..glific_integration import start_contact_flow
from .feedback_processor import FeedbackProcessor

class FeedbackConsumer:
    def __init__(self):
        self.connection = None
        self.channel = None
        self.settings = None
        self.processor = FeedbackProcessor()

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
                message_data, submission_id = self.processor.parse_and_validate(body)
            except ValueError as e:
                frappe.logger().error(f"Invalid message format: {str(e)}. Body: {body}")
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return
            
            frappe.logger().info(f"Processing feedback for submission: {submission_id}")
            
            # Check if submission exists
            try:
                self.processor.ensure_submission_exists(submission_id)
            except ValueError as e:
                frappe.logger().error(f"{str(e)}. Rejecting message.")
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return
            
            # Process the message
            self.processor.update_submission(message_data)
            
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
            print(f"Successfully processed feedback for submission: {submission_id}")
            
        except Exception as e:
            # Rollback database transaction
            frappe.db.rollback()
            
            error_msg = str(e)
            frappe.logger().error(f"Error processing submission {submission_id}: {error_msg}")
            
            # Determine if error is retryable
            if self.processor.is_retryable_error(e):
                frappe.logger().warning(f"Retryable error for submission {submission_id}, will retry")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            else:
                frappe.logger().error(f"Non-retryable error for submission {submission_id}, rejecting message")
                # Mark submission as failed and reject message
                try:
                    if submission_id:
                        self.processor.mark_submission_failed(submission_id, error_msg)
                        frappe.db.commit()
                except:
                    frappe.db.rollback()
                
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)


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
        # Backwards-compatible wrapper (prefer using self.processor directly)
        self.processor.mark_submission_failed(submission_id, error_message)

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
