"""
CR-024 Layer 3 — Glific unresolved-placeholder defense.

Production incident 2026-06-04→05: Glific timed out on SP webhook calls,
failed to substitute real values, and passed literal "@results.X.Y"
placeholder strings into downstream SP API calls — causing 81 "Error
getting assignment context" cascade errors (2,029 broken messages, 466 students).

Layer 3 stops the cascade at endpoint entry: detect the placeholder,
return a clean status="upstream_resolution_failed" so the Glific flow can
retry instead of crashing a lookup.

These are pure-unit tests (frappe fully mocked, no bench DB required).
They verify:
  1. is_unresolved_glific_placeholder — all boundary conditions.
  2. check_glific_placeholders — the convenience batch-checker.
  3. One rejection test per guarded endpoint (status=upstream_resolution_failed).
  4. One happy-path-still-works test (normal ids pass through).
"""
import unittest
from unittest.mock import patch, MagicMock

import tap_lms.summer_program.utils as utils
import tap_lms.summer_program.student_progression_sp as sp
import tap_lms.summer_program.flow_callback as flow_cb
import tap_lms.summer_program.save_submission as save_sub


# ────────────────────────────────────────────────────────────────────────
# 1. Unit tests for is_unresolved_glific_placeholder
# ────────────────────────────────────────────────────────────────────────

class TestIsUnresolvedGlificPlaceholder(unittest.TestCase):
    """Boundary-condition tests for the detection function."""

    def test_true_for_two_segment_placeholder(self):
        """'@results.content_details.youtube_url' — canonical incident pattern."""
        self.assertTrue(
            utils.is_unresolved_glific_placeholder(
                "@results.content_details.youtube_url"
            )
        )

    def test_true_for_quiz_response_placeholder(self):
        """'@results.quiz_response.option_a' — another incident pattern."""
        self.assertTrue(
            utils.is_unresolved_glific_placeholder(
                "@results.quiz_response.option_a"
            )
        )

    def test_false_for_single_segment_after_prefix(self):
        """'@results.foo' has no second dot — treated as safe (ambiguous shape)."""
        self.assertFalse(utils.is_unresolved_glific_placeholder("@results.foo"))

    def test_false_for_normal_course_level_id(self):
        """Normal course_level value 'ReadingFluency-Literacy-C0001' must not be flagged."""
        self.assertFalse(
            utils.is_unresolved_glific_placeholder("ReadingFluency-Literacy-C0001")
        )

    def test_false_for_none(self):
        """None is not a placeholder — must not raise, must return False."""
        self.assertFalse(utils.is_unresolved_glific_placeholder(None))

    def test_false_for_non_string(self):
        """Integer, float, list — all False (no isinstance(str) match)."""
        self.assertFalse(utils.is_unresolved_glific_placeholder(42))
        self.assertFalse(utils.is_unresolved_glific_placeholder(3.14))
        self.assertFalse(utils.is_unresolved_glific_placeholder([]))

    def test_false_for_empty_string(self):
        """Empty string — False."""
        self.assertFalse(utils.is_unresolved_glific_placeholder(""))

    def test_false_for_normal_student_id(self):
        """Normal Student doc name 'ST00051383' must not be flagged."""
        self.assertFalse(utils.is_unresolved_glific_placeholder("ST00051383"))

    def test_false_for_normal_quiz_attempt_id(self):
        """Hash-autoname quiz attempt id — False."""
        self.assertFalse(utils.is_unresolved_glific_placeholder("2rscijc6nd"))

    def test_true_for_deep_nesting(self):
        """'@results.a.b.c.d' — still True (multiple dots after prefix)."""
        self.assertTrue(
            utils.is_unresolved_glific_placeholder("@results.a.b.c.d")
        )


# ────────────────────────────────────────────────────────────────────────
# 2. Unit tests for check_glific_placeholders
# ────────────────────────────────────────────────────────────────────────

class TestCheckGlificPlaceholders(unittest.TestCase):
    """Tests for the batch-checker convenience helper."""

    def _call_with_mocked_frappe(self, params, api_name="test_api", student_id=None):
        """Helper: run check_glific_placeholders with frappe.db + log mocked."""
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"):
            return utils.check_glific_placeholders(params, api_name, student_id)

    def test_returns_none_when_all_params_clean(self):
        """No placeholder → returns None so caller continues normally."""
        result = self._call_with_mocked_frappe(
            [("student_id", "ST00051383"), ("course_level", "ReadingFluency-C0001")],
            api_name="test_api",
            student_id="ST00051383",
        )
        self.assertIsNone(result)

    def test_returns_error_dict_on_first_hit(self):
        """First placeholder found → returns flat error dict immediately."""
        result = self._call_with_mocked_frappe(
            [
                ("student_id", "@results.student.id"),
                ("course_level", "ReadingFluency-C0001"),
            ],
            api_name="get_weekly_content",
            student_id="@results.student.id",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["success"], False)
        self.assertEqual(result["status"], "upstream_resolution_failed")
        self.assertIn("error_detail", result)

    def test_returns_error_on_second_param_hit(self):
        """Second param is a placeholder — still caught."""
        result = self._call_with_mocked_frappe(
            [
                ("student_id", "ST00051383"),
                ("course_level", "@results.content.course_level"),
            ],
            api_name="get_weekly_content",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "upstream_resolution_failed")

    def test_survives_log_error_double_fault(self):
        """If frappe.log_error raises, the function still returns the error dict."""
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error", side_effect=Exception("boom")), \
             patch.object(utils.frappe, "logger") as mock_logger:
            result = utils.check_glific_placeholders(
                [("content_id", "@results.content.id")],
                api_name="get_content_details",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "upstream_resolution_failed")
        # Logger fallback was used
        mock_logger.return_value.warning.assert_called()

    def test_skips_none_value_without_error(self):
        """None values are not flagged — the check is purely for str detection."""
        result = self._call_with_mocked_frappe(
            [("course_level", None)],
            api_name="get_weekly_content",
        )
        self.assertIsNone(result)

    def test_rollback_called_before_log(self):
        """rollback() must run BEFORE log_error — L-077 / L-030 ordering."""
        manager = MagicMock()
        with patch.object(utils.frappe.db, "rollback") as rb, \
             patch.object(utils.frappe, "log_error") as le, \
             patch.object(utils.frappe, "logger"):
            manager.attach_mock(rb, "rollback")
            manager.attach_mock(le, "log_error")

            utils.check_glific_placeholders(
                [("quiz_id", "@results.quiz.id")],
                api_name="start_quiz",
                student_id="ST1",
            )

        names = [c[0] for c in manager.mock_calls]
        self.assertIn("rollback", names)
        self.assertIn("log_error", names)
        self.assertLess(
            names.index("rollback"),
            names.index("log_error"),
            "rollback must precede log_error (L-077)",
        )


# ────────────────────────────────────────────────────────────────────────
# 3. Per-endpoint rejection tests
# Each test passes a placeholder in one key identifier param and asserts
# status == "upstream_resolution_failed".  The endpoint body is NOT reached
# (no further mocks needed).
# ────────────────────────────────────────────────────────────────────────

def _mock_frappe_io():
    """Context manager: mocks frappe.db.rollback + log_error + local.response."""
    from unittest.mock import patch, MagicMock
    import tap_lms.summer_program.utils as utils
    mock_local = MagicMock()
    mock_local.response = {}
    return (
        patch.object(utils.frappe.db, "rollback"),
        patch.object(utils.frappe, "log_error"),
        patch.object(utils.frappe, "logger"),
        mock_local,
    )


class TestGetWeeklyContentRejectsPlaceholder(unittest.TestCase):

    def test_rejects_placeholder_student_id(self):
        """get_weekly_content: placeholder in student_id → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.get_weekly_content(
                student_id="@results.contact.id",
                course_level="ReadingFluency-Literacy-C0001",
            )
        # @glific_response returns None and writes to local.response
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")
        self.assertEqual(local.response.get("success"), False)

    def test_rejects_placeholder_course_level(self):
        """get_weekly_content: placeholder in course_level → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.get_weekly_content(
                student_id="ST00051383",
                course_level="@results.enrollment.course_level",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")


class TestGetContentDetailsRejectsPlaceholder(unittest.TestCase):

    def test_rejects_placeholder_content_id(self):
        """get_content_details: placeholder in content_id → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.get_content_details(
                content_type="Assignment",
                content_id="@results.content_details.assignment_id",
                student_id="ST00051383",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")
        self.assertEqual(local.response.get("success"), False)

    def test_rejects_placeholder_content_type(self):
        """get_content_details: placeholder in content_type → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.get_content_details(
                content_type="@results.content.type",
                content_id="some-real-id",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")


class TestCompleteContentRejectsPlaceholder(unittest.TestCase):

    def test_rejects_placeholder_content_id(self):
        """complete_content: placeholder in content_id → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.complete_content(
                student_id="ST00051383",
                course_level="ReadingFluency-Literacy-C0001",
                content_type="VideoClass",
                content_id="@results.content.video_id",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")

    def test_rejects_placeholder_course_level(self):
        """complete_content: placeholder in course_level → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.complete_content(
                student_id="ST00051383",
                course_level="@results.enrollment.course_level",
                content_type="VideoClass",
                content_id="some-video-id",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")


class TestStartQuizRejectsPlaceholder(unittest.TestCase):

    def test_rejects_placeholder_quiz_id(self):
        """start_quiz: placeholder in quiz_id → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.start_quiz(
                student_id="ST00051383",
                course_level="ReadingFluency-Literacy-C0001",
                quiz_id="@results.quiz_response.quiz_id",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")

    def test_rejects_placeholder_course_level(self):
        """start_quiz: placeholder in course_level → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.start_quiz(
                student_id="ST00051383",
                course_level="@results.context.course_level",
                quiz_id="BasicQuiz_Quiz_B",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")


class TestSubmitAnswerRejectsPlaceholder(unittest.TestCase):

    def test_rejects_placeholder_quiz_attempt_id(self):
        """submit_answer: placeholder in quiz_attempt_id → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.submit_answer(
                student_id="ST00051383",
                quiz_attempt_id="@results.quiz.attempt_id",
                question_index=1,
                answer="A",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")

    def test_rejects_placeholder_student_id(self):
        """submit_answer: placeholder in student_id → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.submit_answer(
                student_id="@results.contact.student_id",
                quiz_attempt_id="2rscijc6nd",
                question_index=1,
                answer="B",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")


class TestGetNextContentRejectsPlaceholder(unittest.TestCase):

    def test_rejects_placeholder_student_id(self):
        """get_next_content: placeholder in student_id → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.get_next_content(
                student_id="@results.contact.student_id",
                course_level="ReadingFluency-Literacy-C0001",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")

    def test_rejects_placeholder_course_level(self):
        """get_next_content: placeholder in course_level → upstream_resolution_failed."""
        rb, le, lg, local = _mock_frappe_io()
        with rb, le, lg, \
             patch.object(utils.frappe, "local", local):
            result = sp.get_next_content(
                student_id="ST00051383",
                course_level="@results.enrollment.course_level",
            )
        self.assertIsNone(result)
        self.assertEqual(local.response.get("status"), "upstream_resolution_failed")


class TestUpdateFlowStatusRejectsPlaceholder(unittest.TestCase):
    """update_flow_status uses frappe.local.response.update() directly
    (no @glific_response decorator). The guard writes its result to
    frappe.local.response and returns without calling any DB handlers.

    Patch strategy:
      - utils.frappe.db.rollback + utils.frappe.log_error: for the logging
        inside check_glific_placeholders (which uses utils.frappe).
      - flow_cb.frappe.local: for the frappe.local.response.update() call
        in flow_callback.py's own frappe binding.
    """

    def test_rejects_placeholder_student_id(self):
        """update_flow_status: placeholder in student_id → upstream_resolution_failed."""
        mock_local = MagicMock()
        mock_local.response = {}
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(flow_cb.frappe, "local", mock_local):
            # update_flow_status writes to frappe.local.response directly
            # (it does NOT use @glific_response decorator) and returns None.
            flow_cb.update_flow_status(
                student_id="@results.contact.student_id",
                status="completed",
                flow_name="SP_Content_Delivery",
            )
        self.assertEqual(mock_local.response.get("status"), "upstream_resolution_failed")
        self.assertEqual(mock_local.response.get("success"), False)

    def test_rejects_placeholder_flow_name(self):
        """update_flow_status: placeholder in flow_name → upstream_resolution_failed."""
        mock_local = MagicMock()
        mock_local.response = {}
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(flow_cb.frappe, "local", mock_local):
            flow_cb.update_flow_status(
                student_id="ST00051383",
                status="completed",
                flow_name="@results.flow.name",
            )
        self.assertEqual(mock_local.response.get("status"), "upstream_resolution_failed")

    def test_normal_ids_pass_guard(self):
        """Real student_id + flow_name must NOT trigger the placeholder guard
        (regression guard against a false-positive in the wiring; L-025)."""
        mock_local = MagicMock()
        mock_local.response = {}
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(flow_cb.frappe, "local", mock_local):
            try:
                flow_cb.update_flow_status(
                    student_id="ST00051383",
                    status="completed",
                    flow_name="SP_Content_Delivery",
                )
            except Exception:
                # Body may fail without a DB; we only assert the guard did not fire.
                pass
        self.assertNotEqual(
            mock_local.response.get("status"),
            "upstream_resolution_failed",
            "Real ids were incorrectly rejected by the placeholder guard",
        )


class TestSaveSubmissionRejectsPlaceholder(unittest.TestCase):
    """save_submission uses frappe.local.response.update() directly.

    Patch strategy:
      - utils.frappe.db.rollback + utils.frappe.log_error: for check_glific_placeholders.
      - save_sub.frappe.local: for the frappe.local.response.update() call
        in save_submission.py's own frappe binding.
    """

    def test_rejects_placeholder_assignment_id(self):
        """save_submission: placeholder in assignment_id → upstream_resolution_failed."""
        mock_local = MagicMock()
        mock_local.response = {}
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(save_sub.frappe, "local", mock_local):
            save_sub.save_submission(
                student_id="ST00051383",
                assignment_id="@results.content_details.assignment_id",
                submission="my actual answer text",
            )
        self.assertEqual(mock_local.response.get("status"), "upstream_resolution_failed")
        self.assertEqual(mock_local.response.get("success"), False)

    def test_rejects_placeholder_student_id(self):
        """save_submission: placeholder in student_id → upstream_resolution_failed."""
        mock_local = MagicMock()
        mock_local.response = {}
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(save_sub.frappe, "local", mock_local):
            save_sub.save_submission(
                student_id="@results.contact.student_id",
                assignment_id="B2_FL_L1_RA12-Basic",
                submission="my actual answer text",
            )
        self.assertEqual(mock_local.response.get("status"), "upstream_resolution_failed")

    def test_rejects_placeholder_legacy_content_id(self):
        """save_submission: placeholder in legacy content_id alias → upstream_resolution_failed."""
        mock_local = MagicMock()
        mock_local.response = {}
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(save_sub.frappe, "local", mock_local):
            save_sub.save_submission(
                student_id="ST00051383",
                content_id="@results.content_details.assignment_id",
                submission="my actual answer text",
            )
        self.assertEqual(mock_local.response.get("status"), "upstream_resolution_failed")

    def test_normal_ids_pass_guard(self):
        """Real student_id + assignment_id must NOT trigger the placeholder guard
        (regression guard against a false-positive in the wiring; L-025).
        _try_claim_primary / P-001 atomicity is unaffected by this test."""
        mock_local = MagicMock()
        mock_local.response = {}
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(save_sub.frappe, "local", mock_local):
            try:
                save_sub.save_submission(
                    student_id="ST00051383",
                    assignment_id="B2_FL_L1_RA12-Basic",
                    submission="my actual answer text",
                )
            except Exception:
                # Body may fail without a DB; we only assert the guard did not fire.
                pass
        self.assertNotEqual(
            mock_local.response.get("status"),
            "upstream_resolution_failed",
            "Real ids were incorrectly rejected by the placeholder guard",
        )


# ────────────────────────────────────────────────────────────────────────
# 4. Happy-path-still-works test (L-027 / regression guard)
# A normal valid identifier must NOT be rejected by the guard.
# Uses get_content_details because it has the most branches around the guard.
# ────────────────────────────────────────────────────────────────────────

class TestGetContentDetailsHappyPathNotRejected(unittest.TestCase):

    def test_normal_assignment_id_passes_guard(self):
        """A real assignment id like 'B2_FL_L1_RA12-Basic' must NOT be flagged
        as a placeholder.  The guard returns None and the endpoint continues
        to the content_type validity check (or beyond if db is mocked).

        We assert that local.response does NOT contain status=upstream_resolution_failed,
        i.e. the guard step was a no-op.  The endpoint may fail for other
        reasons (db not mocked) but the guard specifically must not fire.
        """
        mock_local = MagicMock()
        mock_local.response = {}

        # Patch frappe.db.exists to say the content exists, get_doc to return a
        # minimal doc, so the endpoint doesn't error for unrelated reasons.
        mock_doc = MagicMock()
        mock_doc.video_name = "TestVideo"
        mock_doc.video_youtube_url = "https://youtube.com/watch?v=test"
        mock_doc.video_plio_url = None
        mock_doc.video_file = None
        mock_doc.duration = None
        mock_doc.description = None
        mock_doc.video_translations = []
        mock_doc.questions = []

        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(utils.frappe, "local", mock_local), \
             patch.object(sp.frappe, "local", mock_local), \
             patch.object(sp.frappe.db, "exists", return_value=True), \
             patch.object(sp.frappe, "get_doc", return_value=mock_doc), \
             patch.object(sp.frappe, "get_all", return_value=[]):
            result = sp.get_content_details(
                content_type="VideoClass",
                content_id="B2_FL_L1_VC01-Basic",
                student_id="ST00051383",
            )

        # The guard must NOT have fired
        self.assertNotEqual(
            mock_local.response.get("status"),
            "upstream_resolution_failed",
            "Normal assignment id was incorrectly rejected as a Glific placeholder",
        )
        # The guard is a no-op — endpoint proceeded (result is from @glific_response,
        # which returns None after writing to local.response).
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
