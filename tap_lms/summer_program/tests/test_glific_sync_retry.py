"""
Tests for Glific contact-field sync retry + DLQ.

Background: v3.0 Glific Integration Guide §1.2 promises Glific that contact
fields catch up 200-500ms after the API responds, via a background job.
A bare try/except that swallows the exception (the previous behavior) means
a single Glific outage drops the update on the floor permanently.

Pattern P-007 / lesson L-015: external calls (Glific, RabbitMQ, anything
off-box) must retry on failure and write a structured DLQ entry once the
retry budget is exhausted, so operators can replay manually.

These tests mock update_contact_fields to simulate three scenarios:
  1. Success on first attempt — single call, no retry, no DLQ.
  2. Two transient failures then success — re-enqueue twice, eventual success.
  3. All retries exhausted — DLQ log entry written with structured payload.
"""
import json
import unittest
from unittest.mock import patch, MagicMock, ANY

from tap_lms.summer_program.constants import (
    GLIFIC_SYNC_MAX_RETRIES,
    GLIFIC_SYNC_RETRY_LOG_TITLE,
    GLIFIC_SYNC_DLQ_LOG_TITLE,
)
from tap_lms.summer_program.state_machine import _sync_contact_fields_job


GLIFIC_ID = "98765"
PE_NAME = "PE-TEST-G3-001"
STUDENT_ID = "STU-TEST-001"
FIELDS = {
    "resolved_flow_state": "normal_content_delivery",
    "current_week": "1",
    "program_status": "active",
}


class TestGlificSyncRetry(unittest.TestCase):
    """G3 regression coverage."""

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_success_on_first_attempt_no_retry_no_dlq(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """Happy path: one successful call, no re-enqueue, no DLQ log."""
        mock_update.return_value = {"ok": True}

        _sync_contact_fields_job(GLIFIC_ID, FIELDS, PE_NAME, retry_count=0)

        mock_update.assert_called_once_with(GLIFIC_ID, FIELDS)
        mock_enqueue.assert_not_called()
        mock_log_error.assert_not_called()

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_transient_failure_reenqueues_with_incremented_counter(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """When Glific returns an error, the job re-enqueues itself with
        retry_count+1 and writes a 'retry' log entry (not a DLQ entry)."""
        mock_update.side_effect = Exception("Glific 503 service unavailable")

        _sync_contact_fields_job(GLIFIC_ID, FIELDS, PE_NAME, retry_count=0)

        # Update was attempted once
        mock_update.assert_called_once_with(GLIFIC_ID, FIELDS)
        # Job re-enqueued itself
        mock_enqueue.assert_called_once()
        enqueue_kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(
            enqueue_kwargs["retry_count"],
            1,
            "retry_count must increment on re-enqueue",
        )
        self.assertEqual(enqueue_kwargs["glific_id"], GLIFIC_ID)
        self.assertEqual(enqueue_kwargs["pe_name"], PE_NAME)
        self.assertEqual(enqueue_kwargs["fields"], FIELDS)
        # Logged as retry, not DLQ
        log_calls = mock_log_error.call_args_list
        self.assertEqual(len(log_calls), 1)
        self.assertEqual(log_calls[0].kwargs.get("title"), GLIFIC_SYNC_RETRY_LOG_TITLE)

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_dlq_log_written_when_retry_budget_exhausted(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """After GLIFIC_SYNC_MAX_RETRIES failed attempts, the job stops
        re-enqueueing and writes a DLQ log entry with structured JSON payload.

        Trigger: simulate the final attempt by passing retry_count=MAX_RETRIES.
        That call increments to MAX+1, exceeds budget, lands in DLQ."""
        mock_update.side_effect = Exception("Glific permanent failure")

        _sync_contact_fields_job(
            GLIFIC_ID,
            FIELDS,
            PE_NAME,
            retry_count=GLIFIC_SYNC_MAX_RETRIES,
            student_id=STUDENT_ID,
        )

        # Update attempted
        mock_update.assert_called_once_with(GLIFIC_ID, FIELDS)
        # NO re-enqueue (budget exhausted)
        mock_enqueue.assert_not_called()
        # DLQ log entry written
        log_calls = mock_log_error.call_args_list
        self.assertEqual(len(log_calls), 1)
        dlq_call = log_calls[0]
        self.assertEqual(dlq_call.kwargs.get("title"), GLIFIC_SYNC_DLQ_LOG_TITLE)

        # The DLQ payload must be structured JSON for operator replay
        dlq_message = dlq_call.kwargs.get("message", "")
        payload = json.loads(dlq_message)
        self.assertEqual(payload["pe_name"], PE_NAME)
        self.assertEqual(payload["student_id"], STUDENT_ID)
        self.assertEqual(payload["glific_id"], GLIFIC_ID)
        self.assertEqual(payload["fields"], FIELDS)
        self.assertIn("Glific permanent failure", payload["final_error"])
        self.assertEqual(payload["retries_attempted"], GLIFIC_SYNC_MAX_RETRIES + 1)

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_dlq_payload_includes_student_id_for_pre_pe_enrollment(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """Pre-PE enrollment uses synthetic pe_name='pre-pe:STU-xxx'. The DLQ
        payload MUST also include student_id separately so operators can replay
        without parsing the synthetic id. Otherwise DLQ entries from the bulk
        enrollment chunk path are unactionable."""
        mock_update.side_effect = Exception("Glific outage")
        synthetic_pe_name = f"pre-pe:{STUDENT_ID}"

        _sync_contact_fields_job(
            GLIFIC_ID,
            FIELDS,
            synthetic_pe_name,
            retry_count=GLIFIC_SYNC_MAX_RETRIES,
            student_id=STUDENT_ID,
        )

        mock_enqueue.assert_not_called()
        dlq_call = mock_log_error.call_args_list[0]
        payload = json.loads(dlq_call.kwargs.get("message"))
        self.assertEqual(payload["student_id"], STUDENT_ID)
        self.assertEqual(payload["pe_name"], synthetic_pe_name)

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_double_fault_when_enqueue_itself_fails(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """Double-fault path: Glific call fails AND the re-enqueue itself fails
        (e.g., Redis is down too). The update must not silently vanish — it must
        land in the DLQ immediately with reason=double_fault_enqueue_failed."""
        mock_update.side_effect = Exception("Glific 502")
        mock_enqueue.side_effect = Exception("Redis connection refused")

        _sync_contact_fields_job(
            GLIFIC_ID, FIELDS, PE_NAME, retry_count=0, student_id=STUDENT_ID
        )

        mock_update.assert_called_once_with(GLIFIC_ID, FIELDS)
        mock_enqueue.assert_called_once()  # attempted the re-enqueue
        # Expect: one retry log + one DLQ log (double-fault)
        titles = [c.kwargs.get("title") for c in mock_log_error.call_args_list]
        self.assertIn(GLIFIC_SYNC_RETRY_LOG_TITLE, titles)
        self.assertIn(GLIFIC_SYNC_DLQ_LOG_TITLE, titles)

        # Find the DLQ entry and verify it tags itself as a double-fault
        dlq_msg = next(
            c.kwargs["message"]
            for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == GLIFIC_SYNC_DLQ_LOG_TITLE
        )
        payload = json.loads(dlq_msg)
        self.assertEqual(payload["reason"], "double_fault_enqueue_failed")
        self.assertEqual(payload["student_id"], STUDENT_ID)
        self.assertIn("Glific 502", payload["final_error"])
        self.assertIn("Redis connection refused", payload["enqueue_error"])

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_retry_budget_is_respected_exactly(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """The boundary case: retry_count = MAX_RETRIES - 1 should still
        re-enqueue (one more chance); retry_count = MAX_RETRIES should DLQ.
        Tests both."""
        mock_update.side_effect = Exception("Glific timeout")

        # At MAX - 1: should re-enqueue (becomes MAX, still within budget)
        _sync_contact_fields_job(
            GLIFIC_ID, FIELDS, PE_NAME, retry_count=GLIFIC_SYNC_MAX_RETRIES - 1
        )
        self.assertEqual(mock_enqueue.call_count, 1)
        retry_log_count = sum(
            1
            for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == GLIFIC_SYNC_RETRY_LOG_TITLE
        )
        self.assertEqual(retry_log_count, 1)

        # Reset mocks
        mock_enqueue.reset_mock()
        mock_log_error.reset_mock()
        mock_update.reset_mock()
        mock_update.side_effect = Exception("Glific timeout")

        # At MAX exactly: should NOT re-enqueue, should DLQ
        _sync_contact_fields_job(
            GLIFIC_ID, FIELDS, PE_NAME, retry_count=GLIFIC_SYNC_MAX_RETRIES
        )
        mock_enqueue.assert_not_called()
        dlq_log_count = sum(
            1
            for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == GLIFIC_SYNC_DLQ_LOG_TITLE
        )
        self.assertEqual(dlq_log_count, 1)

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_none_retry_count_treated_as_zero(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """Defensive: if a caller passes retry_count=None (e.g., legacy queue
        message), treat it as 0 and re-enqueue with 1."""
        mock_update.side_effect = Exception("Glific outage")

        _sync_contact_fields_job(GLIFIC_ID, FIELDS, PE_NAME, retry_count=None)

        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["retry_count"], 1)


# ════════════════════════════════════════════════════════════
# Task #5 regression — False-return silent drop
# ════════════════════════════════════════════════════════════

class TestGlificSyncFalseReturnTriggersRetry(unittest.TestCase):
    """Pin task #5: update_contact_fields never raises, it returns False on
    every failure mode (Glific GraphQL errors, contact-not-found, mutation
    errors, network errors, catch-all). The pre-2026-05-22 wrapper ignored
    the return value and silently exited cleanly on False — the retry+DLQ
    machinery NEVER fired. After the fix, the wrapper raises a marker
    RuntimeError on False so the existing exception-driven retry path picks
    it up.

    Tests:
      1. False return triggers retry (was silently dropped before).
      2. False return through retry budget exhausts → DLQ entry written.
      3. True return is unchanged happy path (no regression).
    """

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_false_return_triggers_retry_not_silent_drop(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """The core regression. Before the fix, this scenario:
          - update_contact_fields returns False (e.g., Glific 503 swallowed)
          - wrapper exits cleanly, no retry, no DLQ
        After the fix:
          - wrapper detects False, raises RuntimeError, retry machinery fires
        """
        mock_update.return_value = False

        _sync_contact_fields_job(GLIFIC_ID, FIELDS, PE_NAME, retry_count=0,
                                  student_id=STUDENT_ID)

        # Update was attempted once
        mock_update.assert_called_once_with(GLIFIC_ID, FIELDS)
        # The bug was: this assertion would FAIL pre-fix (enqueue not called
        # because wrapper exited cleanly). Post-fix it passes.
        mock_enqueue.assert_called_once()
        self.assertEqual(
            mock_enqueue.call_args.kwargs["retry_count"],
            1,
            "False return must trigger retry with incremented counter",
        )
        # And a retry-tier log entry was written
        log_titles = [c.kwargs.get("title") for c in mock_log_error.call_args_list]
        self.assertIn(GLIFIC_SYNC_RETRY_LOG_TITLE, log_titles)

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_false_return_through_budget_writes_dlq(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """Last-attempt False return must land in the DLQ Error Log entry
        with structured payload, not silently disappear."""
        mock_update.return_value = False

        _sync_contact_fields_job(
            GLIFIC_ID, FIELDS, PE_NAME,
            retry_count=GLIFIC_SYNC_MAX_RETRIES,
            student_id=STUDENT_ID,
        )

        # NO re-enqueue (budget exhausted)
        mock_enqueue.assert_not_called()
        # DLQ entry written with the marker exception text
        dlq_calls = [
            c for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == GLIFIC_SYNC_DLQ_LOG_TITLE
        ]
        self.assertEqual(len(dlq_calls), 1,
                         "Exactly one DLQ entry must be written when False "
                         "return exhausts the retry budget")
        payload = json.loads(dlq_calls[0].kwargs["message"])
        self.assertEqual(payload["pe_name"], PE_NAME)
        self.assertEqual(payload["student_id"], STUDENT_ID)
        self.assertEqual(payload["glific_id"], GLIFIC_ID)
        self.assertEqual(payload["fields"], FIELDS)
        # The marker exception text must include the contact_id and PE_NAME
        # so operators can grep for the specific failure.
        self.assertIn("returned False", payload["final_error"])
        self.assertIn(GLIFIC_ID, payload["final_error"])

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_true_return_remains_silent_happy_path(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """Regression guard: the True return path must remain unchanged.
        No retry, no log, no DLQ — just a clean success exit."""
        mock_update.return_value = True

        _sync_contact_fields_job(GLIFIC_ID, FIELDS, PE_NAME, retry_count=0)

        mock_update.assert_called_once_with(GLIFIC_ID, FIELDS)
        mock_enqueue.assert_not_called()
        mock_log_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
