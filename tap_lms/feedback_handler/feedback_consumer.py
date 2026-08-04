# tap_lms/feedback_handler/feedback_consumer.py

import frappe
import json
import pika
import time
from typing import Dict

from ..glific_integration import start_contact_flow
from .feedback_processor import FeedbackProcessor

GLIFIC_FEEDBACK_FLOW_ID = "34108"


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
                frappe.db.rollback()
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return
            

            print(f"Received feedback for submission: {submission_id}")
            frappe.logger().info(f"Processing feedback for submission: {submission_id}")

            # Check if submission exists
            try:
                self.processor.ensure_submission_exists(submission_id)
            except ValueError as e:
                frappe.logger().error(f"{str(e)}. Rejecting message.")
                frappe.db.rollback()
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return

            # Process the message and commit before the SP transition and
            # external flow. Glific may immediately call back into our public
            # APIs; if this transaction is still open, those APIs only see the
            # previous committed Submission.status (usually "Processing").
            self.processor.update_submission(message_data)
            frappe.db.commit()

            if not self._is_feedback_requested(submission_id):
                time.sleep(5)

            if self._is_feedback_requested(submission_id):
                notification_student_id = (
                    message_data.get("student_id")
                    or frappe.db.get_value("Submission", submission_id, "student_id")
                )
                if self._uses_direct_glific_contact_id(notification_student_id):
                    frappe.logger().info(
                        f"Skipping SP feedback hook for demo submission {submission_id}; "
                        "student_id is a direct Glific contact ID"
                    )
                else:
                    self.process_feedback_ready(submission_id, message_data)

            if self._claim_feedback_flow(submission_id):
                frappe.db.commit()
                self.trigger_feedback_flow(submission_id, message_data)
            else:
                frappe.db.commit()
                frappe.logger().info(
                    f"Feedback flow not requested for submission {submission_id}; "
                    "acknowledging without Glific notification"
                )

            # Acknowledge message only after successful processing
            ch.basic_ack(delivery_tag=method.delivery_tag)

            frappe.logger().info(f"Successfully processed feedback for submission: {submission_id}")
            print(f"Successfully processed feedback for submission: {submission_id}")

        except Exception as e:
            # Rollback database transaction
            frappe.db.rollback()

            error_msg = str(e)
            frappe.logger().error(f"Error processing submission {submission_id}: {error_msg}")

            # CR-026: make the failure DURABLY visible in tabError Log.
            # Downstream hooks (e.g. on_feedback_ready) call frappe.log_error
            # inside this same transaction — the rollback above erases those
            # rows, which is why the 2026-06-09 stale-module-cache incident left
            # ZERO Error Log entries despite 858 failures. Re-log AFTER the
            # rollback and commit it so the record survives.
            try:
                hint = ""
                if isinstance(e, ImportError):
                    # Classic symptom of a long-running consumer whose cached
                    # modules predate a code change. The fix is a process
                    # restart, not a code change — say so loudly.
                    hint = (
                        "\n\nLikely cause: this long-running consumer holds a "
                        "STALE module cache from before a deploy. Restart the "
                        "consumer process (it is not supervisor-managed) so it "
                        "reloads current code."
                    )
                frappe.log_error(
                    message=f"Error processing submission {submission_id}: {error_msg}{hint}",
                    title="Feedback Consumer Failure",
                )
                frappe.db.commit()
            except Exception:
                # Never let logging failure mask the original error handling.
                frappe.db.rollback()

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

    def _is_feedback_requested(self, submission_id):
        return (
            frappe.db.get_value("Submission", submission_id, "send_feedback")
            == "yes"
        )

    def _uses_direct_glific_contact_id(self, student_id):
        student_id = str(student_id or "").strip()
        if not student_id:
            return False

        return not student_id.upper().startswith("ST")

    def _claim_feedback_flow(self, submission_id):
        """
        Atomically claim a pending feedback-flow request.

        Both the public API and RabbitMQ consumer use this gate. Exactly one
        caller can flip send_feedback from yes to no for a terminal submission,
        which prevents duplicate Glific flows.
        """
        result = frappe.db.sql(
            """
            UPDATE `tabSubmission`
            SET send_feedback = 'no',
                feedback_flow_triggered_at = NOW()
            WHERE name = %s
              AND send_feedback = 'yes'
              AND feedback_flow_triggered_at IS NULL
              AND status IN ('Completed', 'Failed')
            RETURNING name
            """,
            (submission_id,),
        )
        return bool(result)

    def _claim_feedback_ready_processing(self, submission_id):
        """
        Atomically claim the internal SP feedback-ready mutation.

        This is intentionally separate from _claim_feedback_flow: weekly
        content delivery may run the PE mutation without sending the
        student-facing Glific feedback notification.
        """
        result = frappe.db.sql(
            """
            UPDATE `tabSubmission`
            SET feedback_ready_processed_at = NOW()
            WHERE name = %s
              AND feedback_ready_processed_at IS NULL
              AND status IN ('Completed', 'Failed')
            RETURNING name
            """,
            (submission_id,),
        )
        return bool(result)

    def process_feedback_ready(self, submission_id, message_data):
        try:
            frappe.db.begin()
            if not self._claim_feedback_ready_processing(submission_id):
                frappe.db.rollback()
                return False

            result = self._update_sp_state(submission_id, message_data) or {}
            if result.get("status") == "error":
                raise Exception(result.get("message") or "on_feedback_ready failed")

            frappe.db.commit()
            return True
        except Exception as sp_error:
            frappe.db.rollback()
            frappe.logger().warning(f"SP state update failed for {submission_id}: {str(sp_error)}")
            raise

    def trigger_feedback_flow(self, submission_id, message_data):
        # Send Glific notification (non-critical - don't fail message if this fails)
        try:
            self.send_glific_notification(message_data)
        except Exception as glific_error:
            frappe.logger().warning(f"Glific notification failed for {submission_id}: {str(glific_error)}")
            # Continue processing - notification failure shouldn't fail the entire message

    def _update_sp_state(self, submission_id, message_data):
        """
        Summer Program hook: advance PE state from submitted_awaiting_feedback
        to feedback_ready (T12) after AI feedback is processed.

        This is non-critical — if it fails, pe_dispatcher's handle_feedback_timeout
        will catch it within 1-4 hours as a safety net.

        The Glific notification is sent after this hook commits, so callbacks
        from the feedback flow can read the updated ProgramEnrollment state.
        """
        try:
            from tap_lms.summer_program.feedback_consumer_hook import on_feedback_ready

            student_id = message_data.get("student_id")
            result = on_feedback_ready(submission_id, student_id)

            if result.get("status") == "transitioned":
                frappe.logger().info(
                    f"[SP] feedback_ready transition done for PE {result.get('pe')}"
                )
            elif result.get("status") == "error":
                frappe.logger().warning(
                    f"[SP] Hook returned error for {submission_id}: {result.get('message')}"
                )
            return result
        except ImportError as e:
            # CR-026: an ImportError here is almost never "SP module not
            # installed" (it is) — it is a STALE module cache in this
            # long-running consumer. Surface it instead of skipping silently so
            # it is not mistaken for a benign no-op. Re-raise so process_message
            # logs it durably and the message is NACKed (not silently acked).
            frappe.logger().error(
                f"[SP] ImportError resolving on_feedback_ready for {submission_id}: "
                f"{str(e)} — consumer likely needs a restart (stale module cache)."
            )
            raise
        except Exception as e:
            frappe.logger().warning(f"[SP] State update failed for {submission_id}: {str(e)}")
            # Re-raise so process_message logs it but continues
            raise

    def send_glific_notification(self, message_data: Dict):
        """Send feedback notification via Glific with proper error handling.

        If student_id starts with ST, resolve Student.glific_id before invoking
        start_contact_flow. Otherwise, treat student_id as the Glific contact
        ID directly. Both branches trigger flow 34108.
        """
        try:
            submission_id = message_data["submission_id"]
            student_id = message_data.get("student_id") or frappe.db.get_value(
                "Submission", submission_id, "student_id"
            )

            if not student_id:
                frappe.logger().warning(f"No student_id for submission {submission_id}, skipping Glific notification")
                return

            if self._uses_direct_glific_contact_id(student_id):
                glific_id = str(student_id).strip()
            else:
                # Resolve the Glific contact ID from the Student record.
                # We accept the message's student_id as the Frappe doc name and
                # look up the contact ID — the source of truth for Glific addressing.
                glific_id = frappe.db.get_value("Student", student_id, "glific_id")
                if not glific_id:
                    frappe.logger().warning(
                        f"Student {student_id} has no glific_id; skipping Glific "
                        f"notification for submission {submission_id}"
                    )
                    return

            feedback_data = message_data.get("feedback", {})
            overall_feedback = feedback_data.get("overall_feedback", "")

            if not overall_feedback:
                frappe.logger().warning(
                    f"No overall_feedback for submission {submission_id}; "
                    "triggering Glific feedback flow with empty feedback"
                )

            # Prepare flow variables
            default_results = {
                "submission_id": submission_id,
                "feedback": overall_feedback,
                "overall_feedback": overall_feedback,
                "overall_feedback_translated": feedback_data.get(
                    "overall_feedback_translated", ""
                ),
            }

            # Start Glific flow with the Glific contact ID, either resolved
            # from Student.glific_id or supplied directly by the demo API.
            success = start_contact_flow(
                flow_id=GLIFIC_FEEDBACK_FLOW_ID,
                contact_id=str(glific_id),
                default_results=default_results,
            )

            if success:
                frappe.logger().info(
                    f"Sent Glific notification for submission {submission_id} "
                    f"to glific_id={glific_id} (student={student_id})"
                )
            else:
                frappe.logger().warning(
                    f"Failed to send Glific notification for submission "
                    f"{submission_id} (student={student_id}, glific_id={glific_id})"
                )

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
