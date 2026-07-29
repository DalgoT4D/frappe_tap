# consumer code for testing in bench console
import os
import frappe
from tap_lms.feedback_handler.feedback_consumer import FeedbackConsumer

site_name = os.getenv("SITE_NAME", "tap_lms.localhost")

frappe.init(site_name)
frappe.connect()
frappe.set_user("Administrator")

print("\n=== Starting Feedback Consumer ===\n")
consumer = FeedbackConsumer()
consumer.setup_rabbitmq()
# Check queue state (just for info)
queue_state = consumer.channel.queue_declare(
    queue=consumer.settings.feedback_results_queue, passive=True
)
print(
    f"Found {queue_state.method.message_count} messages in queue '{consumer.settings.feedback_results_queue}'\n"
)

# Always start consuming - it will wait for new messages
print("Starting consumer... (waiting for messages, press CTRL+C to exit)")
consumer.start_consuming()
