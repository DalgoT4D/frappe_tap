"""
Tests for T23 system-initiated auto-drop (task #12, 2026-05-13).

T23 is the canonical system-initiated drop transition. CR-003 retired the
`reengagement_exhausted` trigger; today only delivery failures chain into
T23. The `_record_delivery_failure` helper in `pe_dispatcher.py` is the
counter+threshold logic that calls into T23.

Coverage:
  - `t23_auto_drop` correctly drops an active PE with the supplied reason.
  - The transition is idempotent on already-terminal PEs.
  - `_record_delivery_failure` increments the counter atomically (P-002).
  - At threshold, the helper fires T23 → PE moves to program_dropped.
  - The helper short-circuits on terminal PEs so a late delivery-failure
    event can't resurrect a dropped student.

All tests are FrappeTestCase so transaction rollback handles isolation
(L-017). No `frappe.db.commit()` calls. Glific contact-field sync is
mocked at the enqueue boundary so we don't hit the network.
"""
import frappe
from unittest.mock import patch
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime, add_to_date

from tap_lms.summer_program.tests.factories import make_batch
from tap_lms.summer_program.constants import (
    LABEL_CONTENT_DELIVERED,
    LABEL_DROPPED,
    MAX_DELIVERY_FAILURES,
    PATH_CORE,
    PROGRAM_ACTIVE,
    PROGRAM_COMPLETED,
    PROGRAM_DROPPED,
    STATE_NORMAL_CONTENT,
    STATE_PROGRAM_COMPLETED,
    STATE_PROGRAM_DROPPED,
)


# ════════════════════════════════════════════════════════════
# Helpers (copied lightly from test_pe_dispatcher.py so this file
# can stand alone; the shared helper isn't a public API to reuse).
# ════════════════════════════════════════════════════════════


def _ensure_batch():
    # Delegates to the shared factory (L-037) so this fixture inherits future
    # mandatory-field additions instead of breaking with MandatoryError.
    return make_batch(label="T23TestBatch", batch_id="T23B01")


def _ensure_student(suffix):
    name = frappe.get_value("Student", {"phone": f"+9998000{suffix}"}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"T23TestStudent{suffix}"
    s.phone = f"+9998000{suffix}"
    s.glific_id = f"glific-t23-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_active_pe(batch_name, student_name, suffix, delivery_failure_count=0):
    """Insert an active PE for T23 testing."""
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-T23-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-t23-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.journey_label = LABEL_CONTENT_DELIVERED
    pe.current_path = PATH_CORE
    pe.current_week = 1
    pe.insert(ignore_permissions=True)
    frappe.db.sql(
        'UPDATE "tabProgramEnrollment" SET delivery_failure_count = %s WHERE name = %s',
        (delivery_failure_count, pe.name),
    )
    return pe.name


# ════════════════════════════════════════════════════════════
# t23_auto_drop transition tests
# ════════════════════════════════════════════════════════════


class TestT23AutoDrop(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        for pe in frappe.get_all(
            "ProgramEnrollment",
            filters={"batch": self.batch_name},
            pluck="name",
        ):
            frappe.delete_doc("ProgramEnrollment", pe, force=True)

    def test_t23_drops_active_pe(self):
        """T23 on an active PE moves state → program_dropped, status →
        dropped, and stamps drop_reason."""
        from tap_lms.summer_program.state_machine import t23_auto_drop

        s = _ensure_student("D1")
        pe_name = _make_active_pe(self.batch_name, s, "D1")

        with patch(
            "tap_lms.summer_program.state_machine._enqueue_contact_field_sync"
        ):
            pe = frappe.get_doc("ProgramEnrollment", pe_name)
            t23_auto_drop(pe, reason="delivery_failure",
                          trigger_source="dispatcher")

        row = frappe.db.get_value(
            "ProgramEnrollment", pe_name,
            ["resolved_flow_state", "program_status", "drop_reason",
             "journey_label", "next_action_type"],
            as_dict=True,
        )
        self.assertEqual(row.resolved_flow_state, STATE_PROGRAM_DROPPED)
        self.assertEqual(row.program_status, PROGRAM_DROPPED)
        self.assertEqual(row.drop_reason, "delivery_failure")
        self.assertEqual(row.journey_label, LABEL_DROPPED)
        # next_action cleared so the dispatcher never re-tries a terminal PE.
        self.assertEqual(row.next_action_type or "", "")

    def test_t23_idempotent_on_already_dropped(self):
        """Calling T23 twice doesn't crash, doesn't double-fire, doesn't
        flip state away from program_dropped."""
        from tap_lms.summer_program.state_machine import t23_auto_drop

        s = _ensure_student("D2")
        pe_name = _make_active_pe(self.batch_name, s, "D2")

        with patch(
            "tap_lms.summer_program.state_machine._enqueue_contact_field_sync"
        ):
            pe = frappe.get_doc("ProgramEnrollment", pe_name)
            t23_auto_drop(pe, reason="delivery_failure")
            # Reload to pick up the program_status flip from the first call.
            pe = frappe.get_doc("ProgramEnrollment", pe_name)
            # Second call must be a no-op (no exception, state unchanged).
            t23_auto_drop(pe, reason="admin")

        row = frappe.db.get_value(
            "ProgramEnrollment", pe_name,
            ["resolved_flow_state", "drop_reason"],
            as_dict=True,
        )
        # State still program_dropped, reason from the FIRST call preserved.
        self.assertEqual(row.resolved_flow_state, STATE_PROGRAM_DROPPED)
        self.assertEqual(row.drop_reason, "delivery_failure")

    def test_t23_idempotent_on_already_completed(self):
        """Same idempotency rule for the other terminal state."""
        from tap_lms.summer_program.state_machine import t23_auto_drop

        s = _ensure_student("D3")
        pe_name = _make_active_pe(self.batch_name, s, "D3")
        # Force PE into program_completed.
        frappe.db.set_value(
            "ProgramEnrollment", pe_name,
            {
                "resolved_flow_state": STATE_PROGRAM_COMPLETED,
                "program_status": PROGRAM_COMPLETED,
            },
            update_modified=False,
        )

        with patch(
            "tap_lms.summer_program.state_machine._enqueue_contact_field_sync"
        ):
            pe = frappe.get_doc("ProgramEnrollment", pe_name)
            t23_auto_drop(pe, reason="delivery_failure")

        # State must remain program_completed, not flip to dropped.
        state = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "resolved_flow_state"
        )
        self.assertEqual(state, STATE_PROGRAM_COMPLETED)


# ════════════════════════════════════════════════════════════
# _record_delivery_failure helper tests
# ════════════════════════════════════════════════════════════


class TestRecordDeliveryFailure(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        for pe in frappe.get_all(
            "ProgramEnrollment",
            filters={"batch": self.batch_name},
            pluck="name",
        ):
            frappe.delete_doc("ProgramEnrollment", pe, force=True)

    def test_record_delivery_failure_increments_count(self):
        """One call → count = 1, no drop."""
        from tap_lms.summer_program.pe_dispatcher import _record_delivery_failure

        s = _ensure_student("R1")
        pe_name = _make_active_pe(self.batch_name, s, "R1",
                                  delivery_failure_count=0)

        with patch(
            "tap_lms.summer_program.state_machine._enqueue_contact_field_sync"
        ):
            _record_delivery_failure(pe_name)

        row = frappe.db.get_value(
            "ProgramEnrollment", pe_name,
            ["delivery_failure_count", "program_status"],
            as_dict=True,
        )
        self.assertEqual(row.delivery_failure_count, 1)
        # Not dropped — well below threshold.
        self.assertEqual(row.program_status, PROGRAM_ACTIVE)

    def test_record_delivery_failure_fires_t23_at_threshold(self):
        """MAX_DELIVERY_FAILURES (3) calls → counter at threshold, T23
        fires on the third call, state moves to program_dropped with
        drop_reason='delivery_failure'."""
        from tap_lms.summer_program.pe_dispatcher import _record_delivery_failure

        s = _ensure_student("R2")
        pe_name = _make_active_pe(self.batch_name, s, "R2",
                                  delivery_failure_count=0)

        with patch(
            "tap_lms.summer_program.state_machine._enqueue_contact_field_sync"
        ):
            for _ in range(MAX_DELIVERY_FAILURES):
                _record_delivery_failure(pe_name)

        row = frappe.db.get_value(
            "ProgramEnrollment", pe_name,
            ["delivery_failure_count", "program_status",
             "resolved_flow_state", "drop_reason"],
            as_dict=True,
        )
        self.assertEqual(row.delivery_failure_count, MAX_DELIVERY_FAILURES)
        self.assertEqual(row.program_status, PROGRAM_DROPPED)
        self.assertEqual(row.resolved_flow_state, STATE_PROGRAM_DROPPED)
        self.assertEqual(row.drop_reason, "delivery_failure")

    def test_record_delivery_failure_skips_terminal_pe(self):
        """A PE already in a terminal state must NOT have its counter
        incremented — a late delivery-failure event can't resurrect a
        dropped student."""
        from tap_lms.summer_program.pe_dispatcher import _record_delivery_failure

        s = _ensure_student("R3")
        pe_name = _make_active_pe(self.batch_name, s, "R3",
                                  delivery_failure_count=2)
        # Flip PE to terminal (completed) before the call.
        frappe.db.set_value(
            "ProgramEnrollment", pe_name,
            {
                "resolved_flow_state": STATE_PROGRAM_COMPLETED,
                "program_status": PROGRAM_COMPLETED,
            },
            update_modified=False,
        )

        with patch(
            "tap_lms.summer_program.state_machine._enqueue_contact_field_sync"
        ):
            _record_delivery_failure(pe_name)

        row = frappe.db.get_value(
            "ProgramEnrollment", pe_name,
            ["delivery_failure_count", "program_status"],
            as_dict=True,
        )
        # Counter unchanged, status still completed.
        self.assertEqual(row.delivery_failure_count, 2)
        self.assertEqual(row.program_status, PROGRAM_COMPLETED)
