"""
Tests for CR-005 collection-mode weekly content delivery.

Covers:
  - `maintain_collections` state-delta logic (Approach B):
      * main-eligible → audit (remove from main, add to audit)
      * audit → main-eligible (remove from audit, add to main)
      * main → main (no group writes)
      * audit_A → audit_B (remove from A, add to B)
      * unknown → terminal (correct audit add, no main change when source was
        not main-eligible)
      * PE with no glific_id (no-op)
  - `weekly_content_delivery_trigger` only fires SP_Content_Delivery against
    the `main` Glific group, exactly once per active BPR with members.
  - `t0_enrollment` and `t14_week_advance` do NOT arm
    `next_action_type = ACTION_CONTENT_DELIVERY`.
  - PGCollection `validate` enforces (parent BPR, kind) uniqueness on
    kind-keyed rows.

Mocking strategy: patch `_get_pg_collection_by_kind` so tests don't touch
the DB collection lookup; patch `frappe.enqueue` so we capture group-write
intent without actually queuing Glific calls. The sweep test patches
`start_group_flow` directly. No `frappe.db.commit()` per L-017.
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch, MagicMock, call

from tap_lms.summer_program import collection_membership
from tap_lms.summer_program.collection_membership import (
    MAIN_ELIGIBLE_STATES,
    STATE_TO_AUDIT_KIND,
    COLLECTION_KINDS,
    maintain_collections,
)


# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════

def _fake_pe(name="PE-test", glific_id="glific-123", batch="BATCH-x"):
    """Build a doc-like shim the helper can consume (only the 3 attrs it reads)."""
    return frappe._dict({
        "name": name,
        "glific_id": glific_id,
        "batch": batch,
    })


def _stub_collection(kind, group_id="grp-1"):
    """Mock return from _get_pg_collection_by_kind."""
    return {
        "name": f"PGC-{kind}",
        "glific_group_id": group_id,
        "collection_label": f"SP_BATCH-x_{kind}",
        "member_count": 5,
    }


# ════════════════════════════════════════════════════════════
# maintain_collections — state-delta tests
# ════════════════════════════════════════════════════════════

class TestMaintainCollections(FrappeTestCase):
    """The single helper that turns a state delta into Glific group writes."""

    @patch("tap_lms.summer_program.collection_membership._get_pg_collection_by_kind")
    @patch("tap_lms.summer_program.collection_membership.frappe.enqueue")
    def test_maintain_collections_main_eligible_to_audit(self, mock_enqueue, mock_lookup):
        """T2: normal_content_delivery (main) → normal_escalation (audit=escalation).
        Expect: remove from main + add to escalation, in that order."""
        mock_lookup.side_effect = lambda batch, kind: _stub_collection(kind, f"grp-{kind}")
        pe = _fake_pe()

        maintain_collections(pe, from_state="normal_content_delivery",
                             to_state="normal_escalation")

        # Two enqueued jobs: main remove, escalation add
        self.assertEqual(mock_enqueue.call_count, 2)
        actions = [c.kwargs.get("action") for c in mock_enqueue.call_args_list]
        groups = [c.kwargs.get("glific_group_id") for c in mock_enqueue.call_args_list]
        self.assertIn("remove", actions)
        self.assertIn("add", actions)
        # main removed, escalation added
        remove_idx = actions.index("remove")
        add_idx = actions.index("add")
        self.assertEqual(groups[remove_idx], "grp-main")
        self.assertEqual(groups[add_idx], "grp-escalation")

    @patch("tap_lms.summer_program.collection_membership._get_pg_collection_by_kind")
    @patch("tap_lms.summer_program.collection_membership.frappe.enqueue")
    def test_maintain_collections_audit_to_main(self, mock_enqueue, mock_lookup):
        """T3: normal_escalation (audit) → submitted_awaiting_feedback (main).
        Expect: remove from escalation + add to main."""
        mock_lookup.side_effect = lambda batch, kind: _stub_collection(kind, f"grp-{kind}")
        pe = _fake_pe()

        maintain_collections(pe, from_state="normal_escalation",
                             to_state="submitted_awaiting_feedback")

        self.assertEqual(mock_enqueue.call_count, 2)
        actions = [c.kwargs.get("action") for c in mock_enqueue.call_args_list]
        groups = [c.kwargs.get("glific_group_id") for c in mock_enqueue.call_args_list]
        # main add, escalation remove
        add_idx = actions.index("add")
        remove_idx = actions.index("remove")
        self.assertEqual(groups[add_idx], "grp-main")
        self.assertEqual(groups[remove_idx], "grp-escalation")

    @patch("tap_lms.summer_program.collection_membership._get_pg_collection_by_kind")
    @patch("tap_lms.summer_program.collection_membership.frappe.enqueue")
    def test_maintain_collections_no_change_main_to_main(self, mock_enqueue, mock_lookup):
        """T7: normal_content_delivery → submitted_awaiting_feedback (both main).
        Expect: NO group writes."""
        mock_lookup.side_effect = lambda batch, kind: _stub_collection(kind)
        pe = _fake_pe()

        maintain_collections(pe, from_state="normal_content_delivery",
                             to_state="submitted_awaiting_feedback")

        mock_enqueue.assert_not_called()

    @patch("tap_lms.summer_program.collection_membership._get_pg_collection_by_kind")
    @patch("tap_lms.summer_program.collection_membership.frappe.enqueue")
    def test_maintain_collections_audit_to_audit_change(self, mock_enqueue, mock_lookup):
        """Cross-audit transition (synthetic — direct normal_escalation → paused_binge).
        Real transitions don't do this in one hop, but the helper must handle
        an audit_A → audit_B delta correctly: remove from A, add to B, no main
        write (neither state is main-eligible)."""
        mock_lookup.side_effect = lambda batch, kind: _stub_collection(kind, f"grp-{kind}")
        pe = _fake_pe()

        maintain_collections(pe, from_state="normal_escalation",
                             to_state="paused_binge")

        self.assertEqual(mock_enqueue.call_count, 2)
        actions = [c.kwargs.get("action") for c in mock_enqueue.call_args_list]
        groups = [c.kwargs.get("glific_group_id") for c in mock_enqueue.call_args_list]
        # escalation remove, binge_paused add
        remove_idx = actions.index("remove")
        add_idx = actions.index("add")
        self.assertEqual(groups[remove_idx], "grp-escalation")
        self.assertEqual(groups[add_idx], "grp-binge_paused")

    @patch("tap_lms.summer_program.collection_membership._get_pg_collection_by_kind")
    @patch("tap_lms.summer_program.collection_membership.frappe.enqueue")
    def test_maintain_collections_to_terminal(self, mock_enqueue, mock_lookup):
        """T17_exp: grace_waiting (no audit, not main-eligible) → program_dropped.
        Expect: add to program_dropped, NO main write (source wasn't main)."""
        mock_lookup.side_effect = lambda batch, kind: _stub_collection(kind, f"grp-{kind}")
        pe = _fake_pe()

        maintain_collections(pe, from_state="grace_waiting",
                             to_state="program_dropped")

        # Exactly one enqueue: add to program_dropped
        self.assertEqual(mock_enqueue.call_count, 1)
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["action"], "add")
        self.assertEqual(kwargs["glific_group_id"], "grp-program_dropped")

    @patch("tap_lms.summer_program.collection_membership._get_pg_collection_by_kind")
    @patch("tap_lms.summer_program.collection_membership.frappe.enqueue")
    def test_maintain_collections_no_glific_id_no_op(self, mock_enqueue, mock_lookup):
        """PE without a Glific contact — no group writes attempted."""
        pe = _fake_pe(glific_id=None)

        maintain_collections(pe, from_state="normal_content_delivery",
                             to_state="normal_escalation")

        mock_enqueue.assert_not_called()
        mock_lookup.assert_not_called()


# ════════════════════════════════════════════════════════════
# weekly_content_delivery_trigger — fires on main, no recompute
# ════════════════════════════════════════════════════════════

class TestWeeklyContentDeliveryTrigger(FrappeTestCase):
    """The Tuesday cron: one start_group_flow per active BPR, on main only."""

    @patch("tap_lms.summer_program.scheduler.start_group_flow")
    @patch("tap_lms.summer_program.scheduler.process_pending_feedback_ready_before_weekly_content")
    @patch("tap_lms.summer_program.scheduler.frappe.db.sql")
    def test_weekly_trigger_fires_on_main_only(self, mock_sql, mock_preflight, mock_start_flow):
        """One active BPR with a populated main collection → exactly one
        start_group_flow call, against the main group, with the BPR's
        content_delivery_flow."""
        from tap_lms.summer_program.scheduler import weekly_content_delivery_trigger

        # The function makes two SQL calls per BPR: one for active BPRs, one
        # for the main collection. Sequence them.
        active_bprs = [
            {"name": "BPR-test-1", "batch": "BATCH-1",
             "content_delivery_flow": "flow-99"},
        ]
        main_col = [
            {"name": "PGC-main", "glific_group_id": "grp-main-99",
             "collection_label": "SP_BATCH-1_main", "member_count": 5},
        ]
        mock_sql.side_effect = [active_bprs, main_col]
        mock_start_flow.return_value = True

        weekly_content_delivery_trigger()

        mock_preflight.assert_called_once()
        mock_start_flow.assert_called_once_with(
            flow_id="flow-99",
            group_id="grp-main-99",
        )

    @patch("tap_lms.summer_program.scheduler.start_group_flow")
    @patch("tap_lms.summer_program.scheduler.process_pending_feedback_ready_before_weekly_content")
    @patch("tap_lms.summer_program.scheduler.frappe.db.sql")
    def test_weekly_trigger_skips_empty_main(self, mock_sql, mock_preflight, mock_start_flow):
        """BPR whose main collection has 0 members → no flow start."""
        from tap_lms.summer_program.scheduler import weekly_content_delivery_trigger

        active_bprs = [
            {"name": "BPR-test-2", "batch": "BATCH-2",
             "content_delivery_flow": "flow-x"},
        ]
        main_col = [
            {"name": "PGC-main", "glific_group_id": "grp-empty",
             "collection_label": "SP_BATCH-2_main", "member_count": 0},
        ]
        mock_sql.side_effect = [active_bprs, main_col]

        weekly_content_delivery_trigger()

        mock_preflight.assert_called_once()
        mock_start_flow.assert_not_called()


# ════════════════════════════════════════════════════════════
# T0 / T14 — no longer arm content_delivery
# ════════════════════════════════════════════════════════════

class TestT0T14NoLongerArmContentDelivery(FrappeTestCase):
    """CR-005: T0 and T14 do NOT set next_action_type = 'content_delivery'."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from tap_lms.summer_program.tests.test_state_machine import (
            _ensure_batch, _ensure_student,
        )
        cls._ensure_batch = staticmethod(_ensure_batch)
        cls._ensure_student = staticmethod(_ensure_student)
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.state_machine.maintain_collections")
    def test_t0_does_not_arm_content_delivery(self, mock_mc, mock_sync):
        """After t0_enrollment, PE.next_action_type is not 'content_delivery'."""
        from tap_lms.summer_program.state_machine import t0_enrollment
        from tap_lms.summer_program.tests.test_state_machine import _make_pe

        student = _ensure_student = self._ensure_student
        sid = student("CR5-T0")
        pe = _make_pe(self.batch_name, sid, "CR5-T0")

        t0_enrollment(pe, trigger_source="scheduler")
        pe.reload()

        self.assertNotEqual(pe.next_action_type, "content_delivery",
                            "T0 must not arm content_delivery under CR-005")

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.state_machine.maintain_collections")
    def test_t14_does_not_arm_content_delivery(self, mock_mc, mock_sync):
        """After t14_week_advance, PE.next_action_type is not 'content_delivery'."""
        from tap_lms.summer_program.state_machine import t14_week_advance
        from tap_lms.summer_program.constants import STATE_WEEK_COMPLETED
        from tap_lms.summer_program.tests.test_state_machine import _make_pe

        sid = self._ensure_student("CR5-T14")
        pe = _make_pe(
            self.batch_name, sid, "CR5-T14",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=1,
        )

        t14_week_advance(pe, new_week=2, week_rule=None, trigger_source="scheduler")
        pe.reload()

        self.assertNotEqual(pe.next_action_type, "content_delivery",
                            "T14 must not arm content_delivery under CR-005")


# ════════════════════════════════════════════════════════════
# PGCollection (parent BPR, kind) uniqueness
# ════════════════════════════════════════════════════════════

class TestPGCollectionKindUniqueness(FrappeTestCase):
    """The validate hook on PGCollection blocks duplicate (parent, kind) rows."""

    def test_pgcollection_kind_uniqueness(self):
        """Two PGCollection rows with same (parent, kind) → second insert raises."""
        # Use a minimal-but-valid Batch + BPR fixture so we have a parent ref.
        # The validate hook runs on insert; it queries by parent + kind.
        bpr_name = self._ensure_bpr()

        first = frappe.new_doc("PGCollection")
        first.parent = bpr_name
        first.parenttype = "BatchProgramRun"
        first.parentfield = "pg_collections"
        first.kind = "main"
        first.collection_label = "SP_test_main_1"
        first.glific_group_id = "g1"
        first.is_active = 1
        first.insert(ignore_permissions=True)

        # Second row with the same (parent, kind) — should throw.
        second = frappe.new_doc("PGCollection")
        second.parent = bpr_name
        second.parenttype = "BatchProgramRun"
        second.parentfield = "pg_collections"
        second.kind = "main"
        second.collection_label = "SP_test_main_2"
        second.glific_group_id = "g2"
        second.is_active = 1

        with self.assertRaises(frappe.ValidationError):
            second.insert(ignore_permissions=True)

    def _ensure_bpr(self):
        """Build a BPR fixture (only what the FK needs)."""
        from tap_lms.summer_program.tests.test_state_machine import _ensure_batch
        batch_name = _ensure_batch()

        existing = frappe.db.get_value(
            "BatchProgramRun", {"batch": batch_name}, "name"
        )
        if existing:
            return existing

        bpr = frappe.new_doc("BatchProgramRun")
        bpr.batch = batch_name
        bpr.status = "created"
        bpr.insert(ignore_permissions=True)
        return bpr.name
