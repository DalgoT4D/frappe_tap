# tap_lms/feedback_handler/feedback_consumer.py

import json
import time
from typing import Dict

import frappe
import pika

from ..glific_integration import start_contact_flow
from .feedback_processor import FeedbackProcessor


def _emit(severity: str, message: str, **kwargs) -> None:
    """
    Safe wrapper around emit_structured_log.

    - Imports monitoring lazily so this file is importable even before
      monitoring.py exists (e.g. during early migration or unit tests).
    - Swallows ALL exceptions — monitoring must never crash the consumer.
    - Falls back to frappe.logger() so nothing is silently lost if the
      import fails.
    - Prints to stdout on any failure so it shows up in docker compose logs.
    """
    try:
        from tap_lms.monitoring import emit_structured_log

        emit_structured_log(severity=severity, message=message, **kwargs)
    except Exception as e:
        try:
            frappe.logger().info(f"[{severity}] {message} {kwargs}")
        except Exception:
            pass
        print(f"[monitoring] _emit failed for '{message}': {e}", flush=True)


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
                self.settings.username, self.settings.get_password("password")
            )

            parameters = pika.ConnectionParameters(
                host=self.settings.host,
                port=int(self.settings.port),
                virtual_host=self.settings.virtual_host,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300,
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
                    exchange_type="direct",
                    passive=True,  # Check if exists
                )
                frappe.logger().info(
                    f"Using existing dead letter exchange: {dlx_exchange}"
                )
            except pika.exceptions.ChannelClosedByBroker:
                # Exchange doesn't exist or needs to be created
                self._reconnect()
                try:
                    # Try with durable=False (common default)
                    self.channel.exchange_declare(
                        exchange=dlx_exchange, exchange_type="direct", durable=False
                    )
                    frappe.logger().info(
                        f"Created dead letter exchange: {dlx_exchange}"
                    )
                except pika.exceptions.ChannelClosedByBroker:
                    # Try with durable=True
                    self._reconnect()
                    self.channel.exchange_declare(
                        exchange=dlx_exchange, exchange_type="direct", durable=True
                    )
                    frappe.logger().info(
                        f"Created durable dead letter exchange: {dlx_exchange}"
                    )

            # Handle dead letter queue
            try:
                self.channel.queue_declare(queue=dl_queue, durable=True)
                frappe.logger().info(f"Using/created dead letter queue: {dl_queue}")
            except pika.exceptions.ChannelClosedByBroker:
                self._reconnect()
                self.channel.queue_declare(queue=dl_queue, durable=True)

            # Bind dead letter queue to exchange (ignore if already bound)
            try:
                self.channel.queue_bind(
                    exchange=dlx_exchange, queue=dl_queue, routing_key=main_queue
                )
            except Exception:
                pass  # Binding might already exist

            # Handle main queue (use existing configuration)
            try:
                self.channel.queue_declare(
                    queue=main_queue,
                    durable=True,
                    passive=True,  # Use existing queue
                )
                frappe.logger().info(f"Using existing main queue: {main_queue}")
            except pika.exceptions.ChannelClosedByBroker:
                self._reconnect()
                self.channel.queue_declare(queue=main_queue, durable=True)
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
        except Exception:
            pass

        credentials = pika.PlainCredentials(
            self.settings.username, self.settings.get_password("password")
        )

        parameters = pika.ConnectionParameters(
            host=self.settings.host,
            port=int(self.settings.port),
            virtual_host=self.settings.virtual_host,
            credentials=credentials,
            heartbeat=600,
            blocked_connection_timeout=300,
        )

        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()

    def start_consuming(self):
        """Start consuming messages from the queue"""
        try:
            if not self.channel:
                self.setup_rabbitmq()

            frappe.logger().info(
                f"Starting to consume from queue: {self.settings.feedback_results_queue}"
            )

            self.channel.basic_qos(prefetch_count=1)
            self.channel.basic_consume(
                queue=self.settings.feedback_results_queue,
                on_message_callback=self.process_message,
                auto_ack=False,
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
        """Process incoming feedback message with structured log emission at every outcome."""
        message_data = None
        submission_id = None
        receive_time = time.monotonic()

        try:
            frappe.db.begin()

            # Parse and validate message
            try:
                message_data, submission_id = self.processor.parse_and_validate(body)
            except ValueError as e:
                frappe.logger().error(f"Invalid message format: {str(e)}. Body: {body}")
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return

            frappe.logger().info(f"Processing feedback for submission: {submission_id}")

            # SRE: pipeline trace — message received from rag_service
            _emit(
                severity="INFO",
                message="feedback_result_received",
                submission_id=submission_id,
                student_id=message_data.get("student_id"),
                queue=self.settings.feedback_results_queue if self.settings else None,
            )

            # Check if submission exists
            try:
                self.processor.ensure_submission_exists(submission_id)
            except ValueError as e:
                frappe.logger().error(f"{str(e)}. Rejecting message.")
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return

            # Process the message
            self.processor.update_submission(message_data)

            # Send Glific notification (non-critical — failure does not fail the message)
            _glific_ok = False
            _glific_err = None
            try:
                self.send_glific_notification(message_data)
                _glific_ok = True
            except Exception as glific_error:
                _glific_err = str(glific_error)
                frappe.logger().warning(
                    f"Glific notification failed for {submission_id}: {_glific_err}"
                )

            # SRE: Glific notification outcome — always emitted regardless of success/failure
            _emit(
                severity="INFO" if _glific_ok else "WARNING",
                message="glific_notification_sent",
                submission_id=submission_id,
                student_id=message_data.get("student_id"),
                success=_glific_ok,
                error=_glific_err,
            )

            # Summer Program: trigger T12 state transition (non-critical)
            try:
                self._update_sp_state(submission_id, message_data)
            except Exception as sp_error:
                frappe.logger().warning(
                    f"SP state update failed for {submission_id}: {str(sp_error)}"
                )
                # pe_dispatcher's feedback_timeout handler is the safety net

            frappe.db.commit()
            ch.basic_ack(delivery_tag=method.delivery_tag)

            duration_ms = int((time.monotonic() - receive_time) * 1000)

            # SRE: pipeline trace — processing complete (emitted after ack so a crash
            # here cannot affect message acknowledgement)
            _emit(
                severity="INFO",
                message="feedback_processing_complete",
                submission_id=submission_id,
                student_id=message_data.get("student_id"),
                duration_ms=duration_ms,
            )

            frappe.logger().info(
                f"Successfully processed feedback for submission: {submission_id}"
            )
            print(f"Successfully processed feedback for submission: {submission_id}")

        except Exception as e:
            frappe.db.rollback()

            error_msg = str(e)
            frappe.logger().error(
                f"Error processing submission {submission_id}: {error_msg}"
            )

            # classify_error returns both the retry decision AND the named reason.
            # Using it here (instead of is_retryable_error) means failure_reason
            # is available for the structured log, and is_retryable_error() is not
            # called twice as it was in the previous version.
            retryable, failure_reason = self.processor.classify_error(e)

            # delivery_count is set by RabbitMQ on redelivered messages
            retry_count = getattr(properties, "delivery_count", None)

            # SRE: pipeline failure — failure_reason tells the operator what action
            # to take without needing to inspect the message body in CloudAMQP.
            _emit(
                severity="ERROR",
                message="feedback_processing_failed",
                submission_id=submission_id or "unknown",
                student_id=message_data.get("student_id") if message_data else None,
                error=error_msg,
                error_type=type(e).__name__,
                retryable=retryable,
                failure_reason=failure_reason,
                retry_count=retry_count,
            )

            if retryable:
                frappe.logger().warning(
                    f"Retryable error ({failure_reason}) for submission "
                    f"{submission_id}, will retry"
                )
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            else:
                frappe.logger().error(
                    f"Non-retryable error ({failure_reason}) for submission "
                    f"{submission_id}, rejecting to DLQ"
                )
                try:
                    if submission_id:
                        self.processor.mark_submission_failed(submission_id, error_msg)
                        frappe.db.commit()
                except Exception:
                    frappe.db.rollback()

                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)

    def _update_sp_state(self, submission_id, message_data):
        """
        Summer Program hook: advance PE state from submitted_awaiting_feedback
        to feedback_ready (T12) after AI feedback is processed.

        This is non-critical — if it fails, pe_dispatcher's handle_feedback_timeout
        will catch it within 1-4 hours as a safety net.

        The Glific notification (send_glific_notification above) already delivers
        the feedback to the student, so this only updates the state machine to
        unlock week advancement.
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
        except ImportError:
            # Summer Program module not installed/available — skip silently
            pass
        except Exception as e:
            frappe.logger().warning(
                f"[SP] State update failed for {submission_id}: {str(e)}"
            )
            # Re-raise so process_message logs it but continues
            raise

    def send_glific_notification(self, message_data: Dict):
        """Send feedback notification via Glific with proper error handling"""
        try:
            submission_id = message_data["submission_id"]
            student_id = message_data.get("student_id")

            if not student_id:
                frappe.logger().warning(
                    f"No student_id for submission {submission_id}, skipping Glific notification"
                )
                return

            feedback_data = message_data.get("feedback", {})
            overall_feedback = feedback_data.get("overall_feedback", "")

            if not overall_feedback:
                frappe.logger().warning(
                    f"No overall_feedback for submission {submission_id}, skipping Glific notification"
                )
                return

            # Get Glific flow ID
            flow_id = frappe.get_value("Glific Flow", {"label": "feedback"}, "flow_id")
            if not flow_id:
                frappe.logger().warning(
                    "Feedback flow not configured in Glific Flow, skipping notification"
                )
                return

            default_results = {
                "submission_id": submission_id,
                "feedback": overall_feedback,
            }

            success = start_contact_flow(
                flow_id=flow_id, contact_id=student_id, default_results=default_results
            )

            if success:
                frappe.logger().info(
                    f"Sent Glific notification for submission: {submission_id}"
                )
            else:
                frappe.logger().warning(
                    f"Failed to send Glific notification for submission: {submission_id}"
                )
                raise RuntimeError(
                    f"start_contact_flow returned False for {submission_id}"
                )

        except Exception as e:
            frappe.logger().error(
                f"Error sending Glific notification for {submission_id}: {str(e)}"
            )
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
                exchange="",
                routing_key=dead_letter_queue,
                body=json.dumps(message_data),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # make message persistent
                ),
            )

            frappe.logger().warning(
                f"Moved message for submission {message_data.get('submission_id')} "
                f"to dead letter queue"
            )
        except Exception as e:
            frappe.logger().error(
                f"Error moving message to dead letter queue: {str(e)}"
            )

    def get_queue_stats(self):
        """Get statistics about the queues"""
        try:
            if not self.channel:
                self.setup_rabbitmq()

            # Main queue stats
            main_queue_state = self.channel.queue_declare(
                queue=self.settings.feedback_results_queue, passive=True
            )
            main_count = main_queue_state.method.message_count

            # Dead letter queue stats
            try:
                dl_queue_state = self.channel.queue_declare(
                    queue=f"{self.settings.feedback_results_queue}_dead_letter",
                    passive=True,
                )
                dl_count = dl_queue_state.method.message_count
            except Exception:
                dl_count = 0

            return {"main_queue": main_count, "dead_letter_queue": dl_count}

        except Exception as e:
            frappe.logger().error(f"Error getting queue stats: {str(e)}")
            return {"main_queue": 0, "dead_letter_queue": 0}
