"""
Regression tests for dev_tools.reconcile_pe_to_glific + reconcile_batch_to_glific.

Pins the contract added 2026-05-21 (task #51): Frappe PE is canonical; the
reconciler builds the 28-field bundle from PE state and pushes only the
fields that differ from Glific. Decision authority is one-way Frappe → Glific.

These tests mock the Glific HTTP layer so we never hit the network.
"""
import frappe
import json
from unittest.mock import patch, MagicMock
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program.tests.factories import (
    make_batch,
    make_student,
    make_active_pe,
)


_PHONE_PREFIX = "+9998200"   # unique prefix per file convention


def _ensure_pe_ready(suffix, **pe_overrides):
    """Create a Student + PE wired for reconciliation tests."""
    batch_name = make_batch("ReconcileTestBatch", batch_id="RCT01")
    sid = make_student(suffix=suffix, phone_prefix=_PHONE_PREFIX,
                       glific_id=f"gl-rec-{suffix}")
    pe_name = make_active_pe(sid, batch_name, glific_id=f"gl-rec-{suffix}")
    if pe_overrides:
        frappe.db.set_value("ProgramEnrollment", pe_name, pe_overrides)
    return pe_name, sid, batch_name


def _make_mock_glific_response(fields_dict):
    """Build the response shape Glific returns for the contact query.

    Real shape: {"data": {"contact": {"contact": {"fields": "<json-string>"}}}}
    """
    # `fields` is a JSON-encoded string with values shaped as {value, type, ...}
    encoded = {k: {"value": str(v)} for k, v in fields_dict.items()}
    return {
        "data": {
            "contact": {
                "contact": {
                    "id": "fake-contact",
                    "fields": json.dumps(encoded),
                }
            }
        }
    }


class TestReconcilePeToGlific(FrappeTestCase):
    """reconcile_pe_to_glific is the per-PE workhorse. It must:
    1. Build the expected 28-field bundle from PE state.
    2. Diff against current Glific contact fields.
    3. Push ONLY the differing fields (minimizes payload churn).
    4. Honor dry_run — no push when dry_run=True.
    5. Return a structured diff so the operator can audit.
    """

    @patch("tap_lms.summer_program.dev_tools.requests.post")
    @patch("tap_lms.summer_program.dev_tools.update_contact_fields")
    def test_no_mismatch_no_push(self, mock_update, mock_get):
        """If Glific already matches Frappe, no fields are pushed."""
        from tap_lms.summer_program.dev_tools import reconcile_pe_to_glific
        pe_name, sid, batch = _ensure_pe_ready(suffix="MATCH")
        pe = frappe.get_doc("ProgramEnrollment", pe_name)

        # Glific returns the SAME values the reconciler would compute.
        # Use just a few key fields — the helper checks all 28 but
        # missing-in-mock will be treated as None == "" == 0 mismatches,
        # so we need to mock everything reasonably.
        mock_get.return_value = MagicMock(json=lambda: _make_mock_glific_response({
            "student_id": sid,
            "current_week": "1",
            "current_path": "Core",
            "current_tier": "Basic",
            "program_status": "active",
            "resolved_flow_state": "normal_content_delivery",
            "total_points": "0",
            "current_streak": "0",
            "weekly_submission_done": "0",
            "submission_count": "0",
            "escalation_order": "0",
            "escalation_type": "",
            "total_activity_points": "0",
            "weekly_activity_points": "0",
            "total_quiz_points": "0",
            "weekly_quiz_points": "0",
            "total_submission_points": "0",
            "weekly_submission_points": "0",
            "special_gems": "0",
            "expected_submission_type": "",
            "grace_window_end": "",
            "batch_id": "RCT01",
            "archetype": "fence_sitter",
            "language_id": "",
            "experiment_arm": "arm_a",
            "course_level": "",
            "student_name": frappe.get_value("Student", sid, "name1"),
        }))

        result = reconcile_pe_to_glific(pe_name, dry_run=False, verbose=False)

        mock_update.assert_not_called()
        self.assertEqual(result["pushed"], False)
        self.assertEqual(result["diff"], [])

    @patch("tap_lms.summer_program.dev_tools.requests.post")
    @patch("tap_lms.summer_program.dev_tools.update_contact_fields")
    def test_mismatch_pushes_only_diff(self, mock_update, mock_get):
        """When Glific has a stale value, ONLY that field is in the push payload."""
        from tap_lms.summer_program.dev_tools import reconcile_pe_to_glific

        # Set Frappe PE to week 2; mock Glific to still show week 1.
        pe_name, sid, batch = _ensure_pe_ready(
            suffix="STALE",
            current_week=2,
            current_tier="Intermediate",
        )
        mock_get.return_value = MagicMock(json=lambda: _make_mock_glific_response({
            "current_week": "1",         # ← stale
            "current_tier": "Basic",      # ← stale
        }))
        mock_update.return_value = True

        result = reconcile_pe_to_glific(pe_name, dry_run=False, verbose=False)

        self.assertTrue(result["pushed"])
        # The diff should include at least current_week and current_tier.
        diff_fields = {d["field"] for d in result["diff"]}
        self.assertIn("current_week", diff_fields)
        self.assertIn("current_tier", diff_fields)

        # The pushed payload must only contain fields that actually differ.
        call_args = mock_update.call_args
        pushed_fields = call_args.kwargs["fields_to_update"]
        self.assertEqual(pushed_fields.get("current_week"), "2")
        self.assertEqual(pushed_fields.get("current_tier"), "Intermediate")
        # Sanity: nothing un-mismatched should leak into the push.
        # (Only fields in diff appear in pushed_fields.)
        for k in pushed_fields:
            self.assertIn(k, diff_fields,
                          f"Pushed field {k!r} should also be in diff")

    @patch("tap_lms.summer_program.dev_tools.requests.post")
    @patch("tap_lms.summer_program.dev_tools.update_contact_fields")
    def test_dry_run_no_push(self, mock_update, mock_get):
        """dry_run=True reports the diff but never calls update_contact_fields."""
        from tap_lms.summer_program.dev_tools import reconcile_pe_to_glific
        pe_name, sid, batch = _ensure_pe_ready(suffix="DRY",
                                                current_week=2)

        mock_get.return_value = MagicMock(json=lambda: _make_mock_glific_response({
            "current_week": "1",
        }))

        result = reconcile_pe_to_glific(pe_name, dry_run=True, verbose=False)

        mock_update.assert_not_called()
        self.assertFalse(result["pushed"])
        # But the diff is still computed.
        self.assertTrue(any(d["field"] == "current_week" for d in result["diff"]))

    @patch("tap_lms.summer_program.dev_tools.requests.post")
    @patch("tap_lms.summer_program.dev_tools.update_contact_fields")
    def test_pe_without_glific_id_skipped(self, mock_update, mock_get):
        """No glific_id on the PE → no diff, no push, no error."""
        from tap_lms.summer_program.dev_tools import reconcile_pe_to_glific
        pe_name, sid, batch = _ensure_pe_ready(suffix="NOID")
        # Wipe the glific_id
        frappe.db.set_value("ProgramEnrollment", pe_name, "glific_id", "")

        result = reconcile_pe_to_glific(pe_name, dry_run=False, verbose=False)

        mock_get.assert_not_called()
        mock_update.assert_not_called()
        self.assertEqual(result["diff"], [])
        self.assertFalse(result["pushed"])
        self.assertIsNone(result["glific_id"])


class TestReconcileBatchToGlific(FrappeTestCase):
    """The batch variant loops PEs and aggregates results. The per-PE helper
    is already tested above; this test just pins the orchestration shape."""

    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    def test_iterates_active_pes_in_batch(self, mock_per_pe):
        """Exercises the loop + result aggregation. Per-PE result content
        is mocked so we only test the orchestration."""
        from tap_lms.summer_program.dev_tools import reconcile_batch_to_glific

        # Create 2 active PEs in the test batch
        batch = make_batch("ReconcileBatchTest", batch_id="RCB01")
        for i, suffix in enumerate(["P1", "P2"]):
            sid = make_student(suffix=suffix, phone_prefix=_PHONE_PREFIX,
                               glific_id=f"gl-rcb-{suffix}")
            make_active_pe(sid, batch, glific_id=f"gl-rcb-{suffix}")

        # Mock per-PE response — both have 1 mismatch and got pushed
        mock_per_pe.return_value = {
            "pe": "<pe>", "glific_id": "gl", "diff": [{"field": "x"}],
            "pushed": True,
        }

        result = reconcile_batch_to_glific(batch, dry_run=False, verbose=False)

        # Both PEs iterated
        self.assertEqual(mock_per_pe.call_count, 2)
        self.assertEqual(len(result), 2)
