"""
Legacy tests for the removed save_submission._enqueue_to_feedback_pipeline retry + DLQ.

Background: when a student submits a photo/video/voice/emoji, save_submission
records the Submission row and enqueues a background job that uploads to
GCS and publishes the enriched payload to RabbitMQ for AI feedback generation.

Before G4, the pipeline had a bare `try/except` that swallowed exceptions and
logged. A broker hiccup meant the submission was silently lost — the student
remained in `submitted_awaiting_feedback` until the feedback_timeout watchdog
fired ~24h later. Bad UX, manual recovery.

These tests mock pika + get_rabbitmq_settings + upload_to_gcs to simulate:
  1. Success on first attempt — single publish, no retry, no DLQ.
  2. Transient failure — re-enqueue with retry_count+1.
  3. Budget exhausted — DLQ entry with structured payload (student_id, etc.).
  4. Boundary case — MAX-1 retries reenqueues, MAX retries DLQs.
  5. None retry_count — treated as zero (backward-compat for in-flight jobs).
  6. Idempotent GCS upload — if Submission.submission_url already a GCS URL, skip.
  7. Double-fault — pika fails AND frappe.enqueue fails → immediate DLQ.
"""
import json
import unittest
from unittest.mock import patch, MagicMock

FEEDBACK_PIPELINE_DLQ_LOG_TITLE = "obsolete"
FEEDBACK_PIPELINE_MAX_RETRIES = 0
FEEDBACK_PIPELINE_RETRY_LOG_TITLE = "obsolete"
_enqueue_to_feedback_pipeline = None


SUB_ID = "SUB-TEST-0001"
STUDENT_ID = "STU-TEST-G4-001"
MEDIA_URL = "https://glific-cdn.example/abc.jpg"
GCS_URL = "https://storage.googleapis.com/tap-feedback/SUB-TEST-0001.jpg"
PE_CONTEXT = {
    "student": STUDENT_ID,
    "archetype": "submitter",
    "experiment_arm": "arm_a",
    "current_expected_submission_type": "photo",
    "language": "en",
    "batch": "BATCH-SP-2026-A",
    "current_week": 1,
    "current_path": "Core",
    "current_tier": "Basic",
    "course_level": "beginner",
    "current_escalation_step": 0,
}


def _patches():
    """Returns the standard set of mocks for the pipeline.

    Returns a context manager you `with` to apply all patches.
    """
    return [
        patch("tap_lms.imgana.submission.get_rabbitmq_settings"),
        patch("tap_lms.imgana.gcs_client.upload_to_gcs"),
        patch("tap_lms.summer_program.save_submission.pika"),
        patch("tap_lms.summer_program.save_submission.frappe.db"),
        patch("tap_lms.summer_program.save_submission.frappe.enqueue"),
        patch("tap_lms.summer_program.save_submission.frappe.log_error"),
        patch("tap_lms.summer_program.save_submission.frappe.logger"),
    ]


@unittest.skip("Obsolete after Submission async processing replaced _enqueue_to_feedback_pipeline")
class TestFeedbackPipelineRetry(unittest.TestCase):
    """G4 regression coverage."""

    def _rabbitmq_config(self):
        return {
            "host": "localhost",
            "port": "5672",
            "username": "guest",
            "password": "guest",
            "virtual_host": "/",
            "queue": "submissions",
        }

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_success_on_first_attempt_emoji_no_retry_no_dlq(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """Emoji submission (no media_url) — pika publish succeeds → no retry."""
        mock_db.get_value.return_value = "some-assign-id"

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_pika.BlockingConnection.return_value = mock_conn

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "🎯", "emoji", PE_CONTEXT, retry_count=0,
            )

        mock_channel.basic_publish.assert_called_once()
        mock_enqueue.assert_not_called()
        # No DLQ-titled log entries
        dlq_calls = [
            c for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == FEEDBACK_PIPELINE_DLQ_LOG_TITLE
        ]
        self.assertEqual(len(dlq_calls), 0)

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_transient_broker_failure_reenqueues_with_incremented_counter(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """RabbitMQ refuses connection → job re-enqueues with retry_count=1."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_pika.BlockingConnection.side_effect = Exception(
                "ConnectionRefusedError: RabbitMQ down"
            )

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "test", "text_word", PE_CONTEXT, retry_count=0,
            )

        # Re-enqueue happened with retry_count=1
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["retry_count"], 1)
        self.assertEqual(kwargs["img_sub_name"], SUB_ID)
        self.assertEqual(kwargs["pe_context"], PE_CONTEXT)
        # Retry log (not DLQ)
        retry_logs = [
            c for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == FEEDBACK_PIPELINE_RETRY_LOG_TITLE
        ]
        self.assertEqual(len(retry_logs), 1)

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_dlq_log_written_when_retry_budget_exhausted(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """After MAX_RETRIES failed attempts, DLQ with structured JSON payload
        including student_id so operators can replay the submission manually."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_pika.BlockingConnection.side_effect = Exception(
                "RabbitMQ permanent failure"
            )

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "test", "text_word", PE_CONTEXT,
                retry_count=FEEDBACK_PIPELINE_MAX_RETRIES,
            )

        # NO re-enqueue (budget exhausted)
        mock_enqueue.assert_not_called()
        # DLQ log written
        dlq_calls = [
            c for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == FEEDBACK_PIPELINE_DLQ_LOG_TITLE
        ]
        self.assertEqual(len(dlq_calls), 1)
        payload = json.loads(dlq_calls[0].kwargs["message"])
        self.assertEqual(payload["submission_id"], SUB_ID)
        self.assertEqual(payload["student_id"], STUDENT_ID)
        self.assertEqual(payload["submission_type"], "text_word")
        self.assertEqual(payload["response_text"], "test")
        self.assertIn("RabbitMQ permanent failure", payload["final_error"])
        self.assertEqual(
            payload["retries_attempted"], FEEDBACK_PIPELINE_MAX_RETRIES + 1
        )

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_retry_budget_boundary_is_respected_exactly(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """MAX-1 → still re-enqueue; MAX → DLQ."""
        mock_db.get_value.return_value = ""

        # Phase 1: MAX-1 should re-enqueue
        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_pika.BlockingConnection.side_effect = Exception("transient")

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT,
                retry_count=FEEDBACK_PIPELINE_MAX_RETRIES - 1,
            )

        self.assertEqual(mock_enqueue.call_count, 1)
        dlq_count = sum(
            1 for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == FEEDBACK_PIPELINE_DLQ_LOG_TITLE
        )
        self.assertEqual(dlq_count, 0)

        # Phase 2: at MAX exactly, should DLQ
        mock_enqueue.reset_mock()
        mock_log_error.reset_mock()
        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_pika.BlockingConnection.side_effect = Exception("transient")

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT,
                retry_count=FEEDBACK_PIPELINE_MAX_RETRIES,
            )

        mock_enqueue.assert_not_called()
        dlq_count = sum(
            1 for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == FEEDBACK_PIPELINE_DLQ_LOG_TITLE
        )
        self.assertEqual(dlq_count, 1)

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_none_retry_count_treated_as_zero(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """Legacy in-flight messages that lack retry_count default to 0 and
        re-enqueue with 1 (backward-compat)."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_pika.BlockingConnection.side_effect = Exception("transient")

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT, retry_count=None,
            )

        self.assertEqual(mock_enqueue.call_args.kwargs["retry_count"], 1)

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_gcs_upload_is_idempotent_on_retry(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """If Submission.submission_url is already a GCS URL (a previous attempt
        succeeded at GCS but failed at the broker), the retry MUST NOT re-upload
        — that wastes GCS storage AND the Glific URL may have expired by then."""
        # Simulate Submission.submission_url already pointing at the GCS path.
        mock_db.get_value.return_value = GCS_URL

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()), \
             patch("tap_lms.imgana.gcs_client.upload_to_gcs") as mock_upload:
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_pika.BlockingConnection.return_value = mock_conn

            _enqueue_to_feedback_pipeline(
                SUB_ID, MEDIA_URL, None, "photo", PE_CONTEXT, retry_count=1,
            )

            # GCS upload was NOT called
            mock_upload.assert_not_called()

        # The publish payload should carry the existing GCS URL
        publish_call = mock_channel.basic_publish.call_args
        body = json.loads(publish_call.kwargs["body"])
        self.assertEqual(body["img_url"], GCS_URL)

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_double_fault_when_enqueue_itself_fails(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """Broker fails AND the re-enqueue itself fails (Redis down). The
        submission MUST land in DLQ immediately with reason=double_fault_enqueue_failed."""
        mock_db.get_value.return_value = ""
        mock_enqueue.side_effect = Exception("Redis connection refused")

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_pika.BlockingConnection.side_effect = Exception("RabbitMQ 503")

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT, retry_count=0,
            )

        # Both retry log AND DLQ log are present
        titles = [c.kwargs.get("title") for c in mock_log_error.call_args_list]
        self.assertIn(FEEDBACK_PIPELINE_RETRY_LOG_TITLE, titles)
        self.assertIn(FEEDBACK_PIPELINE_DLQ_LOG_TITLE, titles)

        dlq_msg = next(
            c.kwargs["message"] for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == FEEDBACK_PIPELINE_DLQ_LOG_TITLE
        )
        payload = json.loads(dlq_msg)
        self.assertEqual(payload["reason"], "double_fault_enqueue_failed")
        self.assertEqual(payload["student_id"], STUDENT_ID)
        self.assertEqual(payload["submission_id"], SUB_ID)
        self.assertIn("RabbitMQ 503", payload["final_error"])
        self.assertIn("Redis connection refused", payload["enqueue_error"])


@unittest.skip("Obsolete after Submission async processing replaced _enqueue_to_feedback_pipeline")
class TestPublishDurability(unittest.TestCase):
    """G5 regression coverage — publisher confirms + delivery_mode=2 + mandatory.

    These tests guard that every publish goes out with the three durability
    flags. If a future refactor accidentally drops one of them, broker restarts
    will start eating submissions silently and the only signal will be students
    going quiet — which is exactly the failure mode we're trying to prevent.
    """

    def _rabbitmq_config(self):
        return {
            "host": "localhost",
            "port": "5672",
            "username": "guest",
            "password": "guest",
            "virtual_host": "/",
            "queue": "submissions",
        }

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_confirm_delivery_is_enabled_before_publish(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """channel.confirm_delivery() must be called before basic_publish so
        the broker is in confirm mode. Without it, basic_publish returns
        immediately on TCP-write success and the publisher never learns the
        message was dropped."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_pika.BlockingConnection.return_value = mock_conn

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT, retry_count=0,
            )

            mock_channel.confirm_delivery.assert_called_once()

            # Order check: confirm_delivery must happen before basic_publish
            call_names = [c[0] for c in mock_channel.method_calls]
            self.assertIn("confirm_delivery", call_names)
            self.assertIn("basic_publish", call_names)
            self.assertLess(
                call_names.index("confirm_delivery"),
                call_names.index("basic_publish"),
                "confirm_delivery must precede basic_publish",
            )

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_publish_uses_persistent_delivery_mode(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """delivery_mode=2 ensures the broker writes to disk before ack. Without
        this, durable queue retains its DEFINITION across restart but NOT its
        contents — messages held in RAM only are dropped on broker restart."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_pika.BlockingConnection.return_value = mock_conn
            # Real pika.BasicProperties so we can introspect what was built
            mock_pika.BasicProperties.side_effect = (
                lambda **kwargs: MagicMock(**kwargs)
            )

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT, retry_count=0,
            )

            mock_channel.basic_publish.assert_called_once()
            props_call = mock_pika.BasicProperties.call_args
            self.assertEqual(
                props_call.kwargs.get("delivery_mode"),
                2,
                "delivery_mode must be 2 (PERSISTENT)",
            )
            self.assertEqual(
                props_call.kwargs.get("content_type"),
                "application/json",
            )

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_publish_marked_mandatory(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """mandatory=True makes the broker surface unroutable messages back to
        the publisher (Basic.Return → UnroutableError) instead of silently
        dropping them. Pairs with confirm_delivery to catch the missing-queue
        race during broker maintenance."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_pika.BlockingConnection.return_value = mock_conn

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT, retry_count=0,
            )

            publish_kwargs = mock_channel.basic_publish.call_args.kwargs
            self.assertTrue(
                publish_kwargs.get("mandatory"),
                "basic_publish must be mandatory=True",
            )

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_close_failure_after_successful_publish_does_not_retry(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """B2 regression: if basic_publish succeeds (broker confirmed) and
        THEN connection.close() raises (network blip on teardown), the message
        is already durably accepted. Retrying would publish a duplicate. The
        try/finally + publish_succeeded sentinel must guarantee no retry."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            # basic_publish succeeds, then close raises
            mock_conn.close.side_effect = Exception("Connection reset during close")
            mock_pika.BlockingConnection.return_value = mock_conn

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT, retry_count=0,
            )

            # Publish happened exactly once
            mock_channel.basic_publish.assert_called_once()
            # NO re-enqueue (publish was confirmed; close error is non-fatal)
            mock_enqueue.assert_not_called()
            # NO DLQ entry either
            dlq_calls = [
                c for c in mock_log_error.call_args_list
                if c.kwargs.get("title") == FEEDBACK_PIPELINE_DLQ_LOG_TITLE
            ]
            self.assertEqual(len(dlq_calls), 0)

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_connection_closed_even_when_publish_fails(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """B1 regression: under sustained broker outage, every retry creates a
        new connection. Without try/finally, basic_publish raising would leak
        the TCP socket. This test asserts close() is called even on failure
        so FDs / broker connection slots don't exhaust."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_channel.basic_publish.side_effect = Exception("Broker 503")
            mock_pika.BlockingConnection.return_value = mock_conn

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT, retry_count=0,
            )

            # close() WAS called despite publish failure (no leak)
            mock_conn.close.assert_called_once()
            # And the publish failure still triggered G4 retry
            mock_enqueue.assert_called_once()

    @patch("tap_lms.summer_program.save_submission.frappe.logger")
    @patch("tap_lms.summer_program.save_submission.frappe.log_error")
    @patch("tap_lms.summer_program.save_submission.frappe.enqueue")
    @patch("tap_lms.summer_program.save_submission.frappe.db")
    def test_unroutable_error_triggers_g4_retry(
        self, mock_db, mock_enqueue, mock_log_error, mock_logger
    ):
        """When confirms are on, pika raises UnroutableError if the broker
        can't deliver. This must be caught by the G4 try/except and re-enqueued
        — proving the durability flags actually flow into the retry path."""
        mock_db.get_value.return_value = ""

        with patch("tap_lms.summer_program.save_submission.pika") as mock_pika, \
             patch("tap_lms.imgana.submission.get_rabbitmq_settings",
                   return_value=self._rabbitmq_config()):

            # Real pika.exceptions hierarchy isn't available without importing
            # pika; use a generic Exception subclass to simulate.
            class FakeUnroutableError(Exception):
                pass

            mock_pika.exceptions = MagicMock()
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_pika.BlockingConnection.return_value = mock_conn
            mock_channel.basic_publish.side_effect = FakeUnroutableError(
                "queue not found, message returned"
            )

            _enqueue_to_feedback_pipeline(
                SUB_ID, None, "x", "text_word", PE_CONTEXT, retry_count=0,
            )

            # G4 retry path fired
            mock_enqueue.assert_called_once()
            self.assertEqual(mock_enqueue.call_args.kwargs["retry_count"], 1)

            # Logged as retry (not DLQ)
            retry_log = next(
                c for c in mock_log_error.call_args_list
                if c.kwargs.get("title") == FEEDBACK_PIPELINE_RETRY_LOG_TITLE
            )
            self.assertIn("queue not found", retry_log.kwargs["message"])


if __name__ == "__main__":
    unittest.main()
