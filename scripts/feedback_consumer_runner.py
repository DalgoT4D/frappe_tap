"""Entrypoint for running the RabbitMQ feedback consumer as a service."""

from tap_lms.feedback_handler.feedback_consumer import FeedbackConsumer


def run():
    consumer = FeedbackConsumer()
    consumer.setup_rabbitmq()
    consumer.start_consuming()

