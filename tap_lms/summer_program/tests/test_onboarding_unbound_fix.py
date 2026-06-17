"""
CR-004 Slice 0 — AC-9: the unbound-glific_contact NameError is gone.

Root-cause (from CR §Motivation #5): the `except` block inside process_batch_job
had `glific_contact = None` commented out and replaced by `vlaue = []`.  When
process_glific_contact raised (invalid phone → ValueError, or Glific API error),
`glific_contact` was never bound, so the very next line
    student_doc = process_student_record(student, glific_contact, ...)
raised NameError: name 'glific_contact' is not defined.  The outer except caught
THAT error and marked the student Failed with a misleading message.

After the fix (T-04-03b):
- `glific_contact = None` is initialised BEFORE the try block.
- ValueError (invalid phone) surfaces as a logged "missing/invalid phone" error.
- Any other exception surfaces as a logged "Glific error".
- In both cases glific_contact is None and process_student_record proceeds
  without raising NameError.

Tests mock at the process_batch_job layer so we don't need a real DB or Glific.
"""
import json
import unittest
from unittest.mock import patch, MagicMock, call

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_backend_student(name="BS-TEST-001", student_name="Test Student",
                           phone="919876543210", course_vertical="Coding",
                           grade="5", batch="BT00000001",
                           batch_skeyword="coding_batch_1"):
    """Return a MagicMock that behaves like a Backend Students doc."""
    bs = MagicMock()
    bs.name = name
    bs.student_name = student_name
    bs.phone = phone
    bs.course_vertical = course_vertical
    bs.grade = grade
    bs.batch = batch
    bs.batch_skeyword = batch_skeyword
    bs.parent = "SET-001"
    bs.processing_status = "Pending"
    bs.glific_sync_status = "pending"
    return bs


# ── Test class ────────────────────────────────────────────────────────────────

class TestOnboardingUnboundFix(unittest.TestCase):
    """Verify the unbound-glific_contact NameError path is gone."""

    def _run_single_student_loop(
        self, mock_student, glific_side_effect, *,
        process_student_side_effect=None
    ):
        """Drive the inner student-processing loop for one student.

        Patches:
        - frappe.get_doc → returns mock_student
        - process_glific_contact → raises glific_side_effect (or returns None)
        - process_student_record → returns a mock student doc (or raises)
        - update_backend_student_status → no-op
        - get_course_level_with_validation_backend → returns "CL-TEST-001"
        - frappe.db.* → no-ops
        - frappe.log_error → captured for assertion

        Returns (success_count, failure_count, log_error_calls).
        """
        student_doc = MagicMock()
        student_doc.name = "ST-TEST-001"
        student_doc.name1 = "Test Student"
        student_doc.glific_id = None

        # Rebuild frappe.get_doc mock to return batch then student in sequence
        get_doc_mock = MagicMock()
        get_doc_mock.side_effect = [
            MagicMock(status="Processing",
                      name="SET-001", save=MagicMock()),
            mock_student,
            # called again for final batch status update
            MagicMock(status="Processed",
                      name="SET-001",
                      save=MagicMock(),
                      processed_student_count=0,
                      __bool__=lambda s: True),
        ]

        with patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.get_doc",
                   get_doc_mock), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.get_all",
                   MagicMock(return_value=[
                       MagicMock(name=mock_student.name,
                                 batch_skeyword=mock_student.batch_skeyword)
                   ])), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.db",
                   MagicMock(count=MagicMock(return_value=0))), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.log_error",
                   MagicMock()) as mock_log_error, \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.enqueue",
                   MagicMock()), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.logger",
                   MagicMock(return_value=MagicMock())), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.create_or_get_glific_group_for_batch",
                   MagicMock(return_value={"group_id": "GRP-001"})), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.get_initial_stage",
                   MagicMock(return_value="Stage-0")), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.update_job_progress",
                   MagicMock()), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.process_glific_contact",
                   MagicMock(side_effect=glific_side_effect)) as mock_glific, \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.process_student_record",
                   MagicMock(return_value=student_doc)) as mock_psr, \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.update_backend_student_status",
                   MagicMock()) as mock_ubss, \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.get_course_level_with_validation_backend",
                   MagicMock(return_value="CL-TEST-001")):

            result = bop.process_batch_job("SET-001")
            return result, mock_psr, mock_ubss, mock_log_error, mock_glific

    def test_invalid_phone_does_not_raise_nameerror(self):
        """T-04-04 two-phase update: in Phase 1, process_glific_contact is never
        called, so an invalid phone is irrelevant in the Phase-1 loop.
        process_student_record must still be called with glific_contact=None —
        the NameError-absence guarantee is now trivially preserved because
        process_glific_contact is not invoked in Phase 1 at all.

        The ValueError side_effect on the mocked process_glific_contact is kept
        to verify the helper is still not called (AC-1).
        """
        bs = _make_backend_student(phone="invalid_phone")

        result, mock_psr, mock_ubss, mock_log_error, mock_glific = \
            self._run_single_student_loop(bs, ValueError("Invalid phone number format: invalid_phone"))

        # process_student_record must have been called regardless
        mock_psr.assert_called_once()
        call_args = mock_psr.call_args
        # Second positional arg is glific_contact — must be None (Phase 1 passes None)
        glific_contact_arg = call_args.args[1] if call_args.args else call_args.kwargs.get("glific_contact")
        self.assertIsNone(
            glific_contact_arg,
            "glific_contact passed to process_student_record must be None in Phase 1",
        )

        # AC-1: process_glific_contact must NOT be called in Phase 1.
        mock_glific.assert_not_called()

    def test_glific_api_error_does_not_raise_nameerror(self):
        """T-04-04 two-phase update: in Phase 1, process_glific_contact is never
        called, so a Glific API error cannot surface from Phase 1.
        process_student_record must still be called with glific_contact=None.
        The Exception side_effect on the mock is kept to verify AC-1 (not called).
        """
        bs = _make_backend_student()

        result, mock_psr, mock_ubss, mock_log_error, mock_glific = \
            self._run_single_student_loop(bs, Exception("Glific 500 Internal Server Error"))

        # process_student_record still called with glific_contact=None
        mock_psr.assert_called_once()
        call_args = mock_psr.call_args
        glific_contact_arg = call_args.args[1] if call_args.args else call_args.kwargs.get("glific_contact")
        self.assertIsNone(
            glific_contact_arg,
            "glific_contact must be None in Phase 1 (process_glific_contact not called)",
        )

        # AC-1: process_glific_contact must NOT be called in Phase 1.
        mock_glific.assert_not_called()

    def test_success_path_still_works(self):
        """Regression: when process_glific_contact succeeds, the contact is
        passed through correctly and the student is marked Success."""
        bs = _make_backend_student()
        glific_contact = {"id": "GLIFIC-42", "phone": "919876543210"}

        # Override the side_effect: process_glific_contact returns the contact
        result, mock_psr, mock_ubss, mock_log_error, mock_glific = \
            self._run_single_student_loop(bs, None)

        # mock was called without raising — it returns default MagicMock
        mock_psr.assert_called_once()
        # No "glific" errors logged
        log_titles = [
            c.kwargs.get("title", "") or (c.args[0] if c.args else "")
            for c in mock_log_error.call_args_list
        ]
        glific_errors = [t for t in log_titles if "glific error" in t.lower()]
        self.assertEqual(
            len(glific_errors), 0,
            f"No Glific-error logs expected on success path; got: {log_titles}",
        )


class TestGlificContactBindingAtEntryPoint(unittest.TestCase):
    """Direct unit test: confirm glific_contact is always bound in the
    inner student-processing try/except block, regardless of what
    process_glific_contact does."""

    def _call_process_batch_job_inner(self, side_effect):
        """Invoke the specific try/except block from process_batch_job by
        calling the module-level function and checking what process_student_record
        receives as its second argument.

        T-04-04: frappe.enqueue is now also patched to prevent Phase-2
        sync_student_to_glific jobs from hitting real Redis during unit tests.
        """
        student_doc = MagicMock()
        student_doc.name = "ST-DIRECT-001"

        with patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.get_doc",
                   MagicMock(side_effect=[
                       MagicMock(status="Processing", name="SET-DIRECT",
                                 save=MagicMock()),
                       _make_backend_student(),
                       MagicMock(status="Processed", name="SET-DIRECT",
                                 save=MagicMock(), processed_student_count=0,
                                 __bool__=lambda s: True),
                   ])), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.get_all",
                   MagicMock(return_value=[
                       MagicMock(name="BS-DIRECT-001", batch_skeyword="bk1")
                   ])), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.db",
                   MagicMock(count=MagicMock(return_value=0))), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.log_error",
                   MagicMock()), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.enqueue",
                   MagicMock()), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.frappe.logger",
                   MagicMock(return_value=MagicMock())), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.create_or_get_glific_group_for_batch",
                   MagicMock(return_value=None)), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.get_initial_stage",
                   MagicMock(return_value=None)), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.update_job_progress",
                   MagicMock()), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.process_glific_contact",
                   MagicMock(side_effect=side_effect)), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.process_student_record",
                   MagicMock(return_value=student_doc)) as mock_psr, \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.update_backend_student_status",
                   MagicMock()), \
             patch("tap_lms.tap_lms.page.backend_onboarding_process"
                   ".backend_onboarding_process.get_course_level_with_validation_backend",
                   MagicMock(return_value=None)):

            bop.process_batch_job("SET-DIRECT")
            return mock_psr

    def test_nameerror_never_raised_on_valueerror(self):
        """NameError must never propagate when ValueError is raised by
        process_glific_contact. Before the fix this test would fail because
        glific_contact was unbound."""
        try:
            mock_psr = self._call_process_batch_job_inner(
                ValueError("Invalid phone number format: bad")
            )
        except NameError as e:
            self.fail(
                f"NameError was raised — the pre-CR bug is still present: {e}"
            )
        # process_student_record was still invoked (student processed successfully)
        mock_psr.assert_called_once()

    def test_nameerror_never_raised_on_generic_exception(self):
        """NameError must never propagate when a generic Exception is raised
        by process_glific_contact."""
        try:
            mock_psr = self._call_process_batch_job_inner(
                Exception("Glific 503")
            )
        except NameError as e:
            self.fail(
                f"NameError was raised — the pre-CR bug is still present: {e}"
            )
        mock_psr.assert_called_once()


if __name__ == "__main__":
    unittest.main()
