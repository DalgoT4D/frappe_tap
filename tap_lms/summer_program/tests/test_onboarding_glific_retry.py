"""
CR-004 Slice 0 — AC-2: sync_student_to_glific retry + DLQ.

Tests verify:
1. Transient Glific error (Timeout) re-enqueues with _attempt+1.
2. After 3 retries (budget exhausted), DLQ is written to Error Log
   and glific_sync_status is set to 'failed'.
3. Non-transient error (HTTP 400) goes straight to DLQ on first call.
4. Success path: synced + glific_id written back.
5. Idempotency: already-synced row is skipped without re-calling Glific.
6. The student's processing_status is NOT changed to 'Failed' by the DLQ path
   (Phase-1 records persist).

Pattern P-007 / L-015 / L-030 / L-056.
"""
import json
import unittest
from unittest.mock import patch, MagicMock, call

import requests

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_bs(name="BS-RETRY-001", student_id="ST-TEST-001", phone="919876543210",
             parent="SET-001", course_vertical="Coding", grade="5",
             batch_skeyword="coding_1", glific_sync_status="pending"):
    bs = MagicMock()
    bs.name = name
    bs.student_id = student_id
    bs.phone = phone
    bs.parent = parent
    bs.course_vertical = course_vertical
    bs.grade = grade
    bs.batch_skeyword = batch_skeyword
    bs.glific_sync_status = glific_sync_status
    bs.student_name = "Test Student"
    bs.glific_id = None
    bs.save = MagicMock()
    bs.reload = MagicMock()
    return bs


# ── Test: success path ────────────────────────────────────────────────────────

class TestSyncStudentToGlificSuccess(unittest.TestCase):

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.logger")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.enqueue")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_all")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.process_glific_contact")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.create_or_get_glific_group_for_batch")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.get_course_level_with_validation_backend")
    def test_success_sets_synced_and_writes_glific_id(
        self, mock_cl, mock_group, mock_glific_contact, mock_get_all,
        mock_get_doc, mock_log_error, mock_enqueue, mock_logger, mock_db
    ):
        """Happy path: Glific contact returned, glific_id written back,
        glific_sync_status set to 'synced'."""
        bs = _make_bs()
        mock_get_doc.return_value = bs
        mock_get_all.return_value = [MagicMock(kit_less=False)]
        mock_group.return_value = {"group_id": "GRP-001"}
        mock_cl.return_value = "CL-CODING-001"
        mock_glific_contact.return_value = {"id": "GLIFIC-99", "phone": "919876543210"}

        bop.sync_student_to_glific("BS-RETRY-001")

        # glific_sync_status set to synced
        self.assertEqual(bs.glific_sync_status, "synced")
        # glific_id written back to Backend Students
        self.assertEqual(bs.glific_id, "GLIFIC-99")
        # glific_id written back to Student via db.set_value
        mock_db.set_value.assert_called_with(
            "Student", "ST-TEST-001", "glific_id", "GLIFIC-99",
            update_modified=False
        )
        # No DLQ logged
        mock_log_error.assert_not_called()
        # No re-enqueue
        mock_enqueue.assert_not_called()


# ── Test: idempotency ─────────────────────────────────────────────────────────

class TestSyncStudentToGlificIdempotency(unittest.TestCase):

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.logger")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.enqueue")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.process_glific_contact")
    def test_already_synced_row_is_skipped(
        self, mock_glific_contact, mock_get_doc, mock_log_error,
        mock_enqueue, mock_logger, mock_db
    ):
        """If glific_sync_status is already 'synced', the function returns
        immediately without calling Glific again."""
        bs = _make_bs(glific_sync_status="synced")
        mock_get_doc.return_value = bs

        bop.sync_student_to_glific("BS-RETRY-001")

        # Glific was never called
        mock_glific_contact.assert_not_called()
        # No DLQ, no re-enqueue
        mock_log_error.assert_not_called()
        mock_enqueue.assert_not_called()


# ── Test: transient retry ─────────────────────────────────────────────────────

class TestSyncStudentToGlificRetry(unittest.TestCase):

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.logger")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.enqueue")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_all")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.process_glific_contact")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.create_or_get_glific_group_for_batch")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.get_course_level_with_validation_backend")
    def test_timeout_on_attempt_0_reenqueues_with_attempt_1(
        self, mock_cl, mock_group, mock_glific_contact, mock_get_all,
        mock_get_doc, mock_log_error, mock_enqueue, mock_logger, mock_db
    ):
        """First Timeout → re-enqueue with _attempt=1, no DLQ."""
        bs = _make_bs()
        mock_get_doc.return_value = bs
        mock_get_all.return_value = [MagicMock(kit_less=False)]
        mock_group.return_value = None
        mock_cl.return_value = None
        mock_glific_contact.side_effect = requests.Timeout("Glific hung")

        bop.sync_student_to_glific("BS-RETRY-001", _attempt=0)

        mock_enqueue.assert_called_once()
        enqueue_kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(enqueue_kwargs["_attempt"], 1)
        self.assertEqual(enqueue_kwargs["backend_student_name"], "BS-RETRY-001")
        self.assertEqual(enqueue_kwargs["queue"], "long")
        # No DLQ logged on first transient failure
        mock_log_error.assert_not_called()

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.logger")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.enqueue")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_all")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.process_glific_contact")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.create_or_get_glific_group_for_batch")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.get_course_level_with_validation_backend")
    def test_retry_3_times_before_dlq(
        self, mock_cl, mock_group, mock_glific_contact, mock_get_all,
        mock_get_doc, mock_log_error, mock_enqueue, mock_logger, mock_db
    ):
        """On _attempt=2 (last retry before budget exhausted), Timeout → re-enqueue.
        On _attempt=3 (budget exhausted), Timeout → DLQ."""
        bs = _make_bs()
        mock_get_doc.return_value = bs
        mock_get_all.return_value = [MagicMock(kit_less=False)]
        mock_group.return_value = None
        mock_cl.return_value = None
        mock_glific_contact.side_effect = requests.Timeout("persistent hang")

        # _attempt=2: still within budget, should re-enqueue
        bop.sync_student_to_glific("BS-RETRY-001", _attempt=2)
        mock_enqueue.assert_called_once()
        enqueue_kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(enqueue_kwargs["_attempt"], 3)
        mock_log_error.assert_not_called()

        # reset
        mock_enqueue.reset_mock()
        mock_log_error.reset_mock()
        mock_get_doc.reset_mock()
        mock_get_doc.return_value = bs
        bs.reload = MagicMock()

        # _attempt=3: budget exhausted → DLQ then re-raise (L-056 / FIX 1).
        # The call MUST raise so RQ surfaces a failed job, not a finished one.
        with self.assertRaises(Exception):
            bop.sync_student_to_glific("BS-RETRY-001", _attempt=3)
        mock_enqueue.assert_not_called()
        mock_log_error.assert_called()
        # DLQ title must mention the backend_student_name
        dlq_title = mock_log_error.call_args.kwargs.get(
            "title", mock_log_error.call_args.args[0] if mock_log_error.call_args.args else ""
        )
        self.assertIn("BS-RETRY-001", dlq_title)

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.logger")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.enqueue")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_all")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.process_glific_contact")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.create_or_get_glific_group_for_batch")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.get_course_level_with_validation_backend")
    def test_connection_error_is_also_transient(
        self, mock_cl, mock_group, mock_glific_contact, mock_get_all,
        mock_get_doc, mock_log_error, mock_enqueue, mock_logger, mock_db
    ):
        """requests.ConnectionError is also transient — must re-enqueue."""
        bs = _make_bs()
        mock_get_doc.return_value = bs
        mock_get_all.return_value = [MagicMock(kit_less=False)]
        mock_group.return_value = None
        mock_cl.return_value = None
        mock_glific_contact.side_effect = requests.ConnectionError("DNS resolution failed")

        bop.sync_student_to_glific("BS-RETRY-001", _attempt=1)

        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["_attempt"], 2)
        mock_log_error.assert_not_called()


# ── Test: non-transient → immediate DLQ ──────────────────────────────────────

class TestSyncStudentToGlificNonTransientDLQ(unittest.TestCase):

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.logger")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.enqueue")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_all")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.process_glific_contact")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.create_or_get_glific_group_for_batch")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.get_course_level_with_validation_backend")
    def test_non_transient_error_goes_directly_to_dlq(
        self, mock_cl, mock_group, mock_glific_contact, mock_get_all,
        mock_get_doc, mock_log_error, mock_enqueue, mock_logger, mock_db
    ):
        """A generic ValueError (non-transient) skips retry and goes straight
        to DLQ. No re-enqueue."""
        bs = _make_bs()
        mock_get_doc.return_value = bs
        mock_get_all.return_value = [MagicMock(kit_less=False)]
        mock_group.return_value = None
        mock_cl.return_value = None
        mock_glific_contact.side_effect = ValueError("Data validation error")

        # Non-transient → DLQ then re-raise (L-056 / FIX 1).
        with self.assertRaises(Exception):
            bop.sync_student_to_glific("BS-RETRY-001", _attempt=0)

        # Straight to DLQ, no re-enqueue
        mock_enqueue.assert_not_called()
        mock_log_error.assert_called()

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.logger")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.enqueue")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_all")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.process_glific_contact")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.create_or_get_glific_group_for_batch")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.get_course_level_with_validation_backend")
    def test_dlq_sets_glific_sync_status_failed(
        self, mock_cl, mock_group, mock_glific_contact, mock_get_all,
        mock_get_doc, mock_log_error, mock_enqueue, mock_logger, mock_db
    ):
        """On DLQ, glific_sync_status must be set to 'failed' and
        processing_status must NOT be changed (Phase-1 records persist)."""
        bs = _make_bs()
        bs.processing_status = "Success"  # Phase 1 already succeeded
        mock_get_doc.return_value = bs
        mock_get_all.return_value = [MagicMock(kit_less=False)]
        mock_group.return_value = None
        mock_cl.return_value = None
        mock_glific_contact.side_effect = requests.Timeout("hang")

        # Exhaust retries → DLQ then re-raise (L-056 / FIX 1).
        with self.assertRaises(Exception):
            bop.sync_student_to_glific("BS-RETRY-001", _attempt=3)

        # glific_sync_status is 'failed'
        self.assertEqual(bs.glific_sync_status, "failed")
        # processing_status is NOT touched
        self.assertEqual(bs.processing_status, "Success",
                         "processing_status must not be changed by DLQ path — "
                         "Phase-1 records persist")

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.logger")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.enqueue")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_all")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.process_glific_contact")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.create_or_get_glific_group_for_batch")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.get_course_level_with_validation_backend")
    def test_dlq_error_log_payload_is_structured_json(
        self, mock_cl, mock_group, mock_glific_contact, mock_get_all,
        mock_get_doc, mock_log_error, mock_enqueue, mock_logger, mock_db
    ):
        """DLQ log entry's message must be valid JSON with required fields so
        operators can programmatically replay the sync."""
        bs = _make_bs()
        mock_get_doc.return_value = bs
        mock_get_all.return_value = [MagicMock(kit_less=False)]
        mock_group.return_value = None
        mock_cl.return_value = None
        mock_glific_contact.side_effect = requests.Timeout("hang")

        # Exhaust retries → DLQ then re-raise (L-056 / FIX 1).
        with self.assertRaises(Exception):
            bop.sync_student_to_glific("BS-RETRY-001", _attempt=3)

        mock_log_error.assert_called()
        # Find the DLQ log_error call
        message = None
        for c in mock_log_error.call_args_list:
            msg = c.kwargs.get("message") or (c.args[0] if c.args else None)
            if msg:
                message = msg
                break

        self.assertIsNotNone(message, "log_error must be called with a message")
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError) as e:
            self.fail(f"DLQ message is not valid JSON: {e}\nMessage was: {message!r}")

        self.assertIn("backend_student", payload,
                      "DLQ payload must include 'backend_student'")
        self.assertIn("phone", payload,
                      "DLQ payload must include 'phone'")
        self.assertIn("set", payload,
                      "DLQ payload must include 'set' (the parent onboarding set)")
        self.assertIn("error", payload,
                      "DLQ payload must include 'error'")


# ── Test: rollback-before-log (L-030) ─────────────────────────────────────────

class TestDlqRollbackBeforeLog(unittest.TestCase):
    """L-030: _dlq_glific must call frappe.db.rollback() before frappe.log_error."""

    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.db")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.log_error")
    @patch("tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process.frappe.get_doc")
    def test_rollback_called_before_log_error(
        self, mock_get_doc, mock_log_error, mock_db
    ):
        """Verify rollback() is called before log_error() in _dlq_glific.
        L-030: clear poisoned txn before writing to the Error Log.

        Ordering is asserted via a manager mock's ``mock_calls`` ledger
        (attach_mock) rather than ``side_effect`` appends — when ``frappe.db``
        is a werkzeug LocalProxy, a child-mock ``side_effect`` on
        ``mock_db.rollback`` does not reliably fire, but the manager's
        ``mock_calls`` still records the call in invocation order.
        """
        bs = _make_bs()
        mock_get_doc.return_value = bs

        manager = MagicMock()
        manager.attach_mock(mock_db.rollback, "rollback")
        manager.attach_mock(mock_log_error, "log_error")

        bop._dlq_glific("BS-RETRY-001", Exception("test error"))

        # Both must have been called.
        mock_db.rollback.assert_called()
        mock_log_error.assert_called()

        names = [c[0] for c in manager.mock_calls]
        self.assertIn("rollback", names, "rollback must be called")
        self.assertIn("log_error", names, "log_error must be called")
        self.assertLess(
            names.index("rollback"), names.index("log_error"),
            "rollback() must be called BEFORE log_error() (L-030)"
        )


if __name__ == "__main__":
    unittest.main()
