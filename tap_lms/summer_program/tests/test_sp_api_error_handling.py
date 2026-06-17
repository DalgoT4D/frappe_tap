"""
BR-003 — Glific 4s-budget hardening: error-response + insert-retry helpers.

Production incident (2026-06-04, trainer cohort BPR 215a88b7f7): get_next_content
→ _get_or_create_sp_progress → StudentStageProgress insert → tabSeries.current
FOR UPDATE → SerializationFailure → the except handler called frappe.log_error
on the ALREADY-POISONED txn → InFailedSqlTransaction (L-030) → Glific got a 400
with a corrupted traceback.

These pure unit tests (frappe fully mocked, no bench DB) cover the utils helpers:
  - safe_sp_api_error_response: rollback FIRST, survive log_error double-fault,
    return a flat api-standard error.
  - _insert_with_serialization_retry: retry on transient Sf, respect time budget,
    log+raise on exhaustion.
  - sp_safe_endpoint: decorator turns an unhandled exception into a flat error.
"""
import unittest
from unittest.mock import patch, MagicMock

import psycopg2.errors as pg_errors

import tap_lms.summer_program.utils as utils
import tap_lms.summer_program.program_enrollment_api as pe_api
import tap_lms.summer_program.reactivation as reactivation
import tap_lms.summer_program.custom_messages as custom_messages


def _sf(msg="could not serialize access due to concurrent update"):
    return pg_errors.SerializationFailure(msg)


class TestSafeSpApiErrorResponse(unittest.TestCase):

    def test_clears_poisoned_txn_before_log_error(self):
        """rollback() MUST run before log_error() — that's the whole L-030 fix
        (log_error's INSERT would re-raise on a poisoned txn otherwise)."""
        manager = MagicMock()
        with patch.object(utils.frappe.db, "rollback") as rb, \
             patch.object(utils.frappe, "log_error") as le, \
             patch.object(utils.frappe, "local") as local:
            local.response = {}
            manager.attach_mock(rb, "rollback")
            manager.attach_mock(le, "log_error")

            ret = utils.safe_sp_api_error_response(_sf(), "get_next_content",
                                                   student_id="ST1")

        # Returns the flat error dict (also written to frappe.local.response).
        self.assertEqual(ret["success"], False)
        self.assertEqual(ret["status"], "error")
        names = [c[0] for c in manager.mock_calls]
        self.assertIn("rollback", names)
        self.assertIn("log_error", names)
        self.assertLess(names.index("rollback"), names.index("log_error"),
                        "rollback must precede log_error (L-030)")

    def test_survives_log_error_double_fault(self):
        """If log_error itself raises (double-fault), the handler still returns a
        flat response (nested try → logger().error fallback)."""
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error", side_effect=Exception("log boom")), \
             patch.object(utils.frappe, "logger") as logger, \
             patch.object(utils.frappe, "local") as local:
            local.response = {}

            ret = utils.safe_sp_api_error_response(ValueError("x"), "complete_content")

        self.assertEqual(ret["success"], False)
        logger.return_value.error.assert_called()  # fallback path used
        self.assertEqual(local.response.get("success"), False)
        self.assertEqual(local.response.get("status"), "error")

    def test_returns_glific_compliant_flat_shape(self):
        """Response is flat, scalar-only, snake_case, with success+status
        (docs/api-standard-glific.md). No raw exception text leaks."""
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "local") as local:
            local.response = {}

            utils.safe_sp_api_error_response(
                ValueError("secret traceback detail"), "start_quiz", student_id="ST9")

        resp = local.response
        self.assertEqual(resp["success"], False)
        self.assertEqual(resp["status"], "error")
        self.assertIn("user_message", resp)
        # All scalar (no nested dicts/lists), and no leaked exception text.
        for v in resp.values():
            self.assertIsInstance(v, (str, int, float, bool, type(None)))
        self.assertNotIn("secret traceback detail", str(resp))


class TestInsertWithSerializationRetry(unittest.TestCase):

    def test_succeeds_on_transient_serialization_failure(self):
        """insert raises Sf once, then succeeds → doc inserted, no exception."""
        doc = MagicMock()
        doc.doctype = "StudentStageProgress"
        doc.insert.side_effect = [_sf(), None]
        with patch.object(utils.frappe.db, "rollback") as rb, \
             patch.object(utils.time, "sleep") as sleep:
            utils._insert_with_serialization_retry(doc)

        self.assertEqual(doc.insert.call_count, 2)
        self.assertEqual(rb.call_count, 1)
        self.assertEqual(sleep.call_count, 1)

    def test_respects_time_budget(self):
        """budget_ms exceeded → TimeoutError, not infinite retry."""
        doc = MagicMock()
        doc.doctype = "StudentStageProgress"
        doc.insert.side_effect = _sf()  # always fails
        # time.time(): start=0.0, iter0 check=0.0, iter1 check=0.2s (>100ms budget)
        with patch.object(utils.time, "time", side_effect=[0.0, 0.0, 0.2]), \
             patch.object(utils.time, "sleep"), \
             patch.object(utils.frappe.db, "rollback"):
            with self.assertRaises(TimeoutError):
                utils._insert_with_serialization_retry(doc, budget_ms=100)

        self.assertEqual(doc.insert.call_count, 1, "stopped at budget, not retried to exhaustion")

    def test_exhaustion_logs_and_raises(self):
        """Always Sf → 4 attempts (initial + 3), log_error, then re-raise Sf."""
        doc = MagicMock()
        doc.doctype = "StudentStageProgress"
        doc.insert.side_effect = _sf()
        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error") as le, \
             patch.object(utils.time, "sleep"):
            with self.assertRaises(pg_errors.SerializationFailure):
                utils._insert_with_serialization_retry(doc, max_retries=3)

        self.assertEqual(doc.insert.call_count, 4)
        le.assert_called()

    def test_non_transient_propagates_without_retry(self):
        """A non-serialization error propagates immediately (no retry)."""
        doc = MagicMock()
        doc.doctype = "Submission"
        doc.insert.side_effect = ValueError("bad field")
        with patch.object(utils.frappe.db, "rollback") as rb, \
             patch.object(utils.time, "sleep") as sleep:
            with self.assertRaises(ValueError):
                utils._insert_with_serialization_retry(doc)

        self.assertEqual(doc.insert.call_count, 1)
        sleep.assert_not_called()


class TestSpSafeEndpointDecorator(unittest.TestCase):

    def test_unhandled_exception_becomes_flat_error(self):
        """A decorated endpoint that raises returns a flat error (not a 500)."""
        @utils.sp_safe_endpoint("award_bonus_quiz_points")
        def boom(student_id, points):
            raise RuntimeError("kaboom")

        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error") as le, \
             patch.object(utils.frappe, "local") as local:
            local.response = {}
            ret = boom("ST1", 10)

        # Decorator sits OUTSIDE @glific_response, so it must return None (not the
        # dict) — otherwise Frappe re-wraps under response.message (L-028). The
        # flat keys live in frappe.local.response; there is NO nested message.
        self.assertIsNone(ret)
        self.assertEqual(local.response.get("success"), False)
        self.assertEqual(local.response.get("status"), "error")
        self.assertNotIn("message", local.response,
                         "decorator path must not leave a nested message wrapper")
        le.assert_called()

    def test_happy_path_passes_through(self):
        """No exception → the endpoint's own return value is returned unchanged."""
        @utils.sp_safe_endpoint("ok_endpoint")
        def fine(student_id):
            return {"success": True, "status": "ok"}

        self.assertEqual(fine("ST1"), {"success": True, "status": "ok"})


class TestGlificEndpointCallSiteWiring(unittest.TestCase):
    """Call-site wiring tests (L-025): prove the BR-003 hardening is actually
    applied to the THREE real Glific-facing endpoints — not just to the generic
    helper. Each test forces the endpoint body to raise and asserts the response
    is a flat api-standard error (success=False/status=error, no nested
    `message` wrapper, no raw exception text), instead of a 500 to Glific.

    Pure-mock: the endpoint's first internal call is patched to raise, and
    utils.frappe (used by safe_sp_api_error_response / sp_safe_endpoint /
    glific_response) is mocked — no bench DB.
    """

    def test_get_student_state_error_path_is_flat(self):
        """program_enrollment_api.get_student_state — wrapped by
        @sp_safe_endpoint. A raise in the body → flat error via
        frappe.local.response, returns None (no nested message → L-028)."""
        with patch.object(pe_api, "_resolve_student", side_effect=RuntimeError("boom")), \
             patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "local") as local:
            local.response = {}
            ret = pe_api.get_student_state("ST1")

        # If the decorator were NOT applied, RuntimeError would propagate and
        # this test would error rather than see a flat response.
        self.assertIsNone(ret)
        self.assertEqual(local.response.get("success"), False)
        self.assertEqual(local.response.get("status"), "error")
        self.assertNotIn("message", local.response)
        self.assertNotIn("boom", str(local.response))

    def test_reactivate_student_error_path_is_flat(self):
        """reactivation.reactivate_student — wrapped by @sp_safe_endpoint.
        Same contract; also exercises the wrapper's student_id-from-args[0]
        threading (signature is reactivate_student(student_id, **_glific_kwargs))."""
        with patch.object(reactivation, "_resolve_student", side_effect=RuntimeError("boom")), \
             patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "local") as local:
            local.response = {}
            ret = reactivation.reactivate_student("ST1")

        self.assertIsNone(ret)
        self.assertEqual(local.response.get("success"), False)
        self.assertEqual(local.response.get("status"), "error")
        self.assertNotIn("message", local.response)
        self.assertNotIn("boom", str(local.response))

    def test_get_submission_message_except_path_is_flat(self):
        """custom_messages.get_submission_message — has its OWN try/except now
        routed through safe_sp_api_error_response, and is decorated with
        @glific_response (so the returned dict is flattened and the wrapper
        returns None). Also proves `student_id` is bound in the except even
        though `resolve_student` raised before the reassignment (no NameError)."""
        with patch.object(custom_messages, "resolve_student", side_effect=RuntimeError("boom")), \
             patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "local") as local:
            local.response = {}
            ret = custom_messages.get_submission_message("ST1", "main")

        # @glific_response flattens + returns None; flat error in local.response.
        self.assertIsNone(ret)
        self.assertEqual(local.response.get("success"), False)
        self.assertEqual(local.response.get("status"), "error")
        self.assertNotIn("boom", str(local.response))


if __name__ == "__main__":
    unittest.main()
