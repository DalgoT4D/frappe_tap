import unittest
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("pika", MagicMock())

from tap_lms.feedback_handler.feedback_consumer import FeedbackConsumer


class TestFeedbackConsumerOrdering(unittest.TestCase):
    def test_submission_and_sp_state_commit_before_glific_flow_starts(self):
        """Glific can call the feedback API immediately after flow start.

        The completed Submission row and SP state transition must therefore
        commit before send_glific_notification runs; otherwise callbacks can
        observe stale Submission/ProgramEnrollment state.
        """
        events = []
        message_data = {
            "submission_id": "SUB-ORDER-001",
            "student_id": "STU-ORDER-001",
            "feedback": {"overall_feedback": "Done"},
        }

        consumer = FeedbackConsumer.__new__(FeedbackConsumer)
        consumer.processor = MagicMock()
        consumer.processor.parse_and_validate.side_effect = (
            lambda body: (events.append("parse"), (message_data, "SUB-ORDER-001"))[1]
        )
        consumer.processor.ensure_submission_exists.side_effect = (
            lambda submission_id: events.append("ensure_exists")
        )
        consumer.processor.update_submission.side_effect = (
            lambda payload: events.append("update_submission")
        )
        consumer.processor.is_retryable_error.return_value = False
        consumer._is_feedback_requested = MagicMock(return_value=True)
        consumer._claim_feedback_flow = MagicMock(
            side_effect=lambda submission_id: (events.append("claim_feedback"), True)[1]
        )
        consumer.send_glific_notification = MagicMock(
            side_effect=lambda payload: events.append("send_glific")
        )
        consumer._update_sp_state = MagicMock(
            side_effect=lambda submission_id, payload: events.append("update_sp_state")
        )

        channel = MagicMock()
        channel.basic_ack.side_effect = lambda delivery_tag: events.append("ack")
        method = SimpleNamespace(delivery_tag="delivery-1")

        with patch("tap_lms.feedback_handler.feedback_consumer.frappe") as mock_frappe:
            mock_frappe.db.begin.side_effect = lambda: events.append("begin")
            mock_frappe.db.commit.side_effect = lambda: events.append("commit")
            mock_frappe.db.rollback.side_effect = lambda: events.append("rollback")
            mock_frappe.logger.return_value = MagicMock()

            consumer.process_message(channel, method, None, b"{}")

        self.assertLess(
            events.index("update_submission"),
            events.index("commit"),
            "Submission update must be committed after it is written.",
        )
        self.assertLess(
            events.index("commit"),
            events.index("update_sp_state"),
            "SP state must update only after the completed Submission is committed.",
        )
        self.assertLess(
            events.index("begin"),
            events.index("send_glific"),
            "SP transaction must start before Glific flow.",
        )
        self.assertLess(
            events.index("update_sp_state"),
            events.index("send_glific"),
            "Glific flow must start only after SP state has been updated.",
        )
        self.assertLess(
            events.index("commit", events.index("update_sp_state")),
            events.index("send_glific"),
            "Glific flow must start only after SP state update is committed.",
        )
        self.assertEqual(events[-1], "ack")

    def test_ack_without_trigger_when_feedback_not_requested_after_wait(self):
        events = []
        message_data = {
            "submission_id": "SUB-ORDER-002",
            "student_id": "STU-ORDER-002",
            "feedback": {"overall_feedback": "Done"},
        }

        consumer = FeedbackConsumer.__new__(FeedbackConsumer)
        consumer.processor = MagicMock()
        consumer.processor.parse_and_validate.return_value = (
            message_data,
            "SUB-ORDER-002",
        )
        consumer.processor.ensure_submission_exists.return_value = None
        consumer.processor.update_submission.side_effect = (
            lambda payload: events.append("update_submission")
        )
        consumer.processor.is_retryable_error.return_value = False
        consumer._is_feedback_requested = MagicMock(return_value=False)
        consumer._claim_feedback_flow = MagicMock(return_value=False)
        consumer.trigger_feedback_flow = MagicMock()

        channel = MagicMock()
        channel.basic_ack.side_effect = lambda delivery_tag: events.append("ack")
        method = SimpleNamespace(delivery_tag="delivery-2")

        with patch("tap_lms.feedback_handler.feedback_consumer.time.sleep") as mock_sleep, \
                patch("tap_lms.feedback_handler.feedback_consumer.frappe") as mock_frappe:
            mock_frappe.db.begin.side_effect = lambda: events.append("begin")
            mock_frappe.db.commit.side_effect = lambda: events.append("commit")
            mock_frappe.db.rollback.side_effect = lambda: events.append("rollback")
            mock_frappe.logger.return_value = MagicMock()

            consumer.process_message(channel, method, None, b"{}")

        mock_sleep.assert_called_once_with(5)
        consumer._claim_feedback_flow.assert_called_once_with("SUB-ORDER-002")
        consumer.trigger_feedback_flow.assert_not_called()
        self.assertEqual(events[-1], "ack")


if __name__ == "__main__":
    unittest.main()
