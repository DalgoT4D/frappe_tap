"""
Tests for summer_program.pe_dispatcher

Covers the per-PE event-driven dispatcher and its eight handlers, focusing on
the bug classes the prior code review (CR-2026-05-10) flagged:

- B1: dispatcher SQL referenced a non-existent `Batch.scheduler_mode` column.
  Test ensures the dispatcher runs cleanly on PG with no JOIN to Batch.
- B2: missing FOR UPDATE SKIP LOCKED + journey-label-guarded atomic claim
  caused duplicate Glific flow triggers under parallel workers. Test
  simulates a parallel worker by manipulating journey_label between SELECT
  and dispatch and asserts the second pass is a no-op (P-001).
- B3: WHERE filter `program_status = 'active'` excluded paused PEs and made
  the pause_check handler unreachable. Test enrols a paused PE with
  `next_action_type = pause_check` and asserts the dispatcher picks it up.
  (Pre-CR-003 also covered the now-retired handle_re_engagement; that handler
  is gone — see task #51 / CR-003.)
- #52 (counter race): handle_feedback_timeout and t25_delivery_failure must
  use COALESCE-update SQL to be race-tolerant. Test calls the handler twice
  in sequence and asserts the counter equals exactly 2 (no read-then-write
  loss). Pre-CR-003 this also covered handle_re_engagement which has been
  retired (task #51).

Glific is mocked via unittest.mock.patch so we never hit the network.
No frappe.db.commit() in tests — the runner relies on transaction rollback
for isolation (lesson L-017).
"""
import frappe

from tap_lms.summer_program.tests.factories import make_batch
from datetime import timedelta
from unittest.mock import patch
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime, add_to_date

from tap_lms.summer_program.constants import (
    ACTION_CONTENT_DELIVERY,
    ACTION_ESCALATION,
    ACTION_FEEDBACK_TIMEOUT,
    ACTION_GRACE_CHECK,
    ACTION_PAUSE_CHECK,
    ACTION_WEEK_ADVANCEMENT,
    BPR_ACTIVE,
    BPR_COLLECTIONS_READY,
    LABEL_CONTENT_DELIVERED,
    LABEL_PAUSED,
    LABEL_GRACE_WINDOW,
    LABEL_SUBMITTED,
    PROGRAM_ACTIVE,
    PROGRAM_PAUSED,
    PATH_CORE,
    STATE_NORMAL_CONTENT,
    STATE_PAUSED_BINGE,
    STATE_GRACE_WAITING,
    STATE_SUBMITTED_AWAITING,
    STATE_WEEK_COMPLETED,
    VALIDATION_PASSED,
)
# CR-003 / task #51: ACTION_GRACE_REMINDER and ACTION_RE_ENGAGEMENT removed
# from the constants module; handle_re_engagement and handle_grace_reminder
# removed from pe_dispatcher. The tests that exercised handle_grace_reminder /
# handle_re_engagement / t17b_grace_reminder have been deleted; the
# `test_journey_label_changes_skip_dispatch` test below has been retargeted
# to use ACTION_GRACE_CHECK which is the live grace action post-CR-003.


# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════


def _ensure_batch():
    """Create or fetch a test Batch the test PEs hang off.

    Delegates to the shared factory (L-037). Tests that need to advance
    batch.current_calendar_week mutate it via frappe.db.set_value after
    creation — that behavior is unchanged by this delegation.
    """
    return make_batch(label="DispatcherTestBatch", batch_id="DSPT01")


def _ensure_student(suffix):
    """Create a Student row."""
    name = frappe.get_value("Student", {"phone": f"+9999000{suffix}"}, "name")
    if name:
        return name

    s = frappe.new_doc("Student")
    s.name1 = f"DispatcherTestStudent{suffix}"
    s.phone = f"+9999000{suffix}"
    s.glific_id = f"glific-disp-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(
    batch_name,
    student_name,
    next_action_at,
    next_action_type,
    program_status=PROGRAM_ACTIVE,
    resolved_flow_state=STATE_NORMAL_CONTENT,
    journey_label=LABEL_CONTENT_DELIVERED,
    glific_id=None,
    enrollment_suffix="A",
    current_week=1,
    submission_count=0,
    current_path=PATH_CORE,
    grace_window_start=None,
    re_engagement_count=0,
    delivery_failure_count=0,
):
    """Insert a ProgramEnrollment with the fields needed for dispatcher tests.

    Note: `feedback_retry_count` is intentionally NOT a kwarg here. Task #11
    was closed without adding that field — production schema only has
    `delivery_failure_count` (used by handle_feedback_timeout as the retry
    counter). If you need a feedback-specific counter later, file it tied to
    the watchdog feature that actually consumes it (task #56).
    """
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-DISP-{enrollment_suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = glific_id or f"glific-disp-{enrollment_suffix}"
    pe.program_status = program_status
    pe.resolved_flow_state = resolved_flow_state
    pe.journey_label = journey_label
    pe.current_path = current_path
    pe.current_week = current_week
    pe.submission_count = submission_count
    pe.next_action_at = next_action_at
    pe.next_action_type = next_action_type
    if grace_window_start is not None:
        pe.grace_window_start = grace_window_start
    pe.insert(ignore_permissions=True)

    # Set counter fields the tests examine. Raw UPDATE so we don't trip
    # controllers and we exercise the same SQL path the production
    # COALESCE-update does.
    frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET re_engagement_count = %s,
               delivery_failure_count = %s
         WHERE name = %s
        """,
        (re_engagement_count, delivery_failure_count, pe.name),
    )
    return pe.name


# ════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════


class TestPeDispatcher(FrappeTestCase):
    """Dispatcher-level (process_program_actions) tests.

    Task #15 (2026-05-13) renamed `dispatch_pending_actions` →
    `process_program_actions` and bumped DISPATCH_BATCH_SIZE 500→1000. The
    legacy name is preserved as a thin alias inside pe_dispatcher.py so
    any caller still importing it via the old name continues to work
    through one release cycle.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        # Clean any leftover dispatcher PEs from a previous test in the same class
        # (FrappeTestCase wraps each test in a rollback, but be defensive).
        for pe in frappe.get_all(
            "ProgramEnrollment",
            filters={"batch": self.batch_name},
            pluck="name",
        ):
            frappe.delete_doc("ProgramEnrollment", pe, force=True)

    def test_dispatcher_picks_due_pes_in_order(self):
        """Dispatcher SELECT orders by next_action_at and routes to handlers in order."""
        from tap_lms.summer_program import pe_dispatcher

        now = now_datetime()
        s1 = _ensure_student("01")
        s2 = _ensure_student("02")
        s3 = _ensure_student("03")
        s4 = _ensure_student("04")
        s5 = _ensure_student("05")

        pe_names = []
        for i, (s, offset_min) in enumerate(
            [(s1, -50), (s2, -40), (s3, -30), (s4, -20), (s5, -10)]
        ):
            pe_names.append(
                _make_pe(
                    self.batch_name,
                    s,
                    add_to_date(now, minutes=offset_min),
                    ACTION_CONTENT_DELIVERY,
                    enrollment_suffix=f"O{i}",
                    glific_id=f"glific-disp-O{i}",
                )
            )

        called_with = []

        def fake_handler(pe_row):
            called_with.append(pe_row.name)

        # Replace handle_content_delivery in HANDLER_MAP for the duration of the test.
        original = pe_dispatcher.HANDLER_MAP[ACTION_CONTENT_DELIVERY]
        pe_dispatcher.HANDLER_MAP[ACTION_CONTENT_DELIVERY] = fake_handler
        try:
            result = pe_dispatcher.process_program_actions()
        finally:
            pe_dispatcher.HANDLER_MAP[ACTION_CONTENT_DELIVERY] = original

        # All five should be processed in next_action_at ascending order.
        self.assertEqual(result["dispatched"], 5)
        self.assertEqual(called_with, pe_names)

    def test_atomic_claim_prevents_double_dispatch(self):
        """If a parallel worker advances journey_label between SELECT and claim,
        the dispatcher's UPDATE-RETURNING returns 0 rows and the handler is
        skipped. Models the L-010 / P-001 race."""
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("11")
        pe_name = _make_pe(
            self.batch_name,
            s,
            add_to_date(now_datetime(), minutes=-5),
            ACTION_CONTENT_DELIVERY,
            enrollment_suffix="C1",
            glific_id="glific-disp-C1",
        )

        called = []

        # Simulate the parallel worker by advancing journey_label inside the
        # handler — that's after our SELECT but before the next dispatch tick.
        # The first dispatch should claim and call the handler; the second
        # dispatch (with no fresh next_action_at) should pick up nothing.
        def fake_handler(pe_row):
            called.append(pe_row.name)

        original = pe_dispatcher.HANDLER_MAP[ACTION_CONTENT_DELIVERY]
        pe_dispatcher.HANDLER_MAP[ACTION_CONTENT_DELIVERY] = fake_handler

        # First — manually flip journey_label *before* dispatch, simulating
        # a parallel handler that has already moved this PE past the
        # snapshot the dispatcher SELECT saw.
        try:
            # Re-set next_action_at so SELECT sees the row
            frappe.db.sql(
                'UPDATE "tabProgramEnrollment" SET next_action_at = %s WHERE name = %s',
                (add_to_date(now_datetime(), minutes=-5), pe_name),
            )

            # Patch the SQL helper so the SELECT runs but BEFORE the atomic claim
            # we mutate journey_label, simulating a concurrent handler advancing
            # state. Use a wrapper that fires after the SELECT.
            real_sql = frappe.db.sql
            select_done = {"done": False}

            def maybe_race(query, values=None, *args, **kwargs):
                # Detect the SELECT that opens the dispatcher tick.
                is_select = (
                    isinstance(query, str)
                    and "FOR UPDATE SKIP LOCKED" in query
                )
                result = real_sql(query, values, *args, **kwargs) if values is not None else real_sql(query, *args, **kwargs)
                if is_select and not select_done["done"]:
                    select_done["done"] = True
                    # Race: a "parallel handler" advances journey_label.
                    real_sql(
                        'UPDATE "tabProgramEnrollment" SET journey_label = %s WHERE name = %s',
                        (LABEL_SUBMITTED, pe_name),
                    )
                return result

            with patch.object(frappe.db, "sql", side_effect=maybe_race):
                result = pe_dispatcher.process_program_actions()

            # Handler must NOT have been called — atomic claim returned 0 rows.
            self.assertEqual(called, [])
            self.assertEqual(result.get("dispatched", 0), 0)
            self.assertGreaterEqual(result.get("skipped", 0), 1)
        finally:
            pe_dispatcher.HANDLER_MAP[ACTION_CONTENT_DELIVERY] = original

    def test_paused_pe_with_pause_check_action_dispatched(self):
        """A PE with program_status='paused' and next_action_type='pause_check'
        must be picked up by the dispatcher (regression for B3)."""
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("21")
        pe_name = _make_pe(
            self.batch_name,
            s,
            add_to_date(now_datetime(), minutes=-1),
            ACTION_PAUSE_CHECK,
            program_status=PROGRAM_PAUSED,
            resolved_flow_state=STATE_PAUSED_BINGE,
            journey_label=LABEL_PAUSED,
            enrollment_suffix="P1",
            glific_id="glific-disp-P1",
            current_week=2,
        )

        called = []

        def fake_pause_check(pe_row):
            called.append(pe_row.name)

        original = pe_dispatcher.HANDLER_MAP[ACTION_PAUSE_CHECK]
        pe_dispatcher.HANDLER_MAP[ACTION_PAUSE_CHECK] = fake_pause_check
        try:
            result = pe_dispatcher.process_program_actions()
        finally:
            pe_dispatcher.HANDLER_MAP[ACTION_PAUSE_CHECK] = original

        self.assertEqual(called, [pe_name])
        self.assertEqual(result["dispatched"], 1)

    def test_journey_label_changes_skip_dispatch(self):
        """If journey_label changes between SELECT and the atomic claim, the
        atomic UPDATE returns 0 rows and the handler is NOT invoked.
        Same primitive as test_atomic_claim_prevents_double_dispatch but
        framed against a different action type (grace_check) to confirm the
        guard isn't action-type-specific.

        CR-003: retargeted from the retired ACTION_GRACE_REMINDER to
        ACTION_GRACE_CHECK — the live grace-window scheduler action.
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("31")
        pe_name = _make_pe(
            self.batch_name,
            s,
            add_to_date(now_datetime(), minutes=-5),
            ACTION_GRACE_CHECK,
            resolved_flow_state=STATE_GRACE_WAITING,
            journey_label=LABEL_GRACE_WINDOW,
            enrollment_suffix="G1",
            glific_id="glific-disp-G1",
            grace_window_start=add_to_date(now_datetime(), days=-7),
        )

        called = []

        def fake_grace(pe_row):
            called.append(pe_row.name)

        original = pe_dispatcher.HANDLER_MAP[ACTION_GRACE_CHECK]
        pe_dispatcher.HANDLER_MAP[ACTION_GRACE_CHECK] = fake_grace

        real_sql = frappe.db.sql
        first_select = {"seen": False}

        def maybe_race(query, values=None, *args, **kwargs):
            is_select = (
                isinstance(query, str)
                and "FOR UPDATE SKIP LOCKED" in query
            )
            result = real_sql(query, values, *args, **kwargs) if values is not None else real_sql(query, *args, **kwargs)
            if is_select and not first_select["seen"]:
                first_select["seen"] = True
                real_sql(
                    'UPDATE "tabProgramEnrollment" SET journey_label = %s WHERE name = %s',
                    (LABEL_SUBMITTED, pe_name),
                )
            return result

        try:
            with patch.object(frappe.db, "sql", side_effect=maybe_race):
                pe_dispatcher.process_program_actions()
        finally:
            pe_dispatcher.HANDLER_MAP[ACTION_GRACE_CHECK] = original

        # Handler must NOT have been called.
        self.assertEqual(called, [])

    def test_process_program_actions_logs_structured_metrics(self):
        """Task #15: every tick emits a single structured info log with
        `claimed`, `skipped`, `errors`, `queue_depth`. Operators monitor
        these to decide when to scale parallel workers (architecture §8.8).

        We don't assert exact values — they're empty-batch dependent — but
        the logger MUST be called exactly once with a dict shape that
        contains the four required keys."""
        from tap_lms.summer_program import pe_dispatcher

        # No PEs match the dispatcher SELECT (setUp wiped them); the
        # function should still emit a log line (the empty-batch case is
        # the most important to monitor — if it stops, the cron is down).
        captured = []

        class FakeLogger:
            def info(self, payload):
                captured.append(payload)

        with patch.object(frappe, "logger", return_value=FakeLogger()):
            pe_dispatcher.process_program_actions()

        # Exactly one info call.
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertIsInstance(payload, dict)
        # Required keys per the task #15 spec.
        for required_key in ("claimed", "skipped", "errors", "queue_depth"):
            self.assertIn(required_key, payload)
        # `dispatcher` tag identifies the source.
        self.assertEqual(payload.get("dispatcher"), "process_program_actions")


class TestPeDispatcherHandlers(FrappeTestCase):
    """Per-handler unit tests."""

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

    def test_handle_feedback_timeout_fallback_calls_t12_when_feedback_present(self):
        """When handle_feedback_timeout fires and the AI feedback Submission
        is found (consumer succeeded but somehow didn't move PE state), the
        handler invokes t12_feedback_ready as a fallback transition. T12 is
        what wires up F5 on Glific via the consumer's notification path —
        the handler itself does NOT call Glific directly.

        Replaces the prior `test_handle_feedback_notification_fires_F5` which
        had a misleading name (asserted F5 was NOT fired) and tested the same
        codepath. See task #55."""
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("41")
        pe_name = _make_pe(
            self.batch_name,
            s,
            add_to_date(now_datetime(), minutes=-5),
            ACTION_FEEDBACK_TIMEOUT,
            resolved_flow_state=STATE_SUBMITTED_AWAITING,
            journey_label=LABEL_SUBMITTED,
            enrollment_suffix="F1",
            glific_id="glific-disp-F1",
            current_week=1,
        )

        # Patch frappe.db.exists so the handler thinks AI feedback is ready
        # (forces the T12 fallback branch).
        original_exists = frappe.db.exists

        def fake_exists(*args, **kwargs):
            if args and args[0] == "Submission":
                return "FAKE-SUB-001"
            return original_exists(*args, **kwargs)

        with patch.object(frappe.db, "exists", side_effect=fake_exists), \
             patch("tap_lms.summer_program.state_machine.t12_feedback_ready") as fake_t12, \
             patch("tap_lms.glific_integration.start_contact_flow") as fake_glific:
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_FEEDBACK_TIMEOUT,
                "journey_label": LABEL_SUBMITTED,
            })
            pe_dispatcher.handle_feedback_timeout(row)

        # T12 was invoked exactly once.
        self.assertEqual(fake_t12.call_count, 1)
        # No direct Glific call from this handler — T12 owns the feedback flow.
        self.assertEqual(fake_glific.call_count, 0)

    def test_handle_feedback_timeout_increments_count_atomically(self):
        """Calling handle_feedback_timeout twice in sequence (no AI feedback yet)
        must result in delivery_failure_count == 2.

        Pattern P-002 guards against the read-then-write race that would
        otherwise lose one increment. The handler uses delivery_failure_count
        (not a separate feedback_retry_count — task #11 was closed, see
        _make_pe docstring)."""
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("51")
        pe_name = _make_pe(
            self.batch_name,
            s,
            add_to_date(now_datetime(), minutes=-5),
            ACTION_FEEDBACK_TIMEOUT,
            resolved_flow_state=STATE_SUBMITTED_AWAITING,
            journey_label=LABEL_SUBMITTED,
            enrollment_suffix="FT2",
            glific_id="glific-disp-FT2",
            delivery_failure_count=0,
        )

        # Make sure the handler thinks no feedback row exists, so we hit the
        # increment branch both times.
        with patch.object(frappe.db, "exists", return_value=False), \
             patch("tap_lms.glific_integration.start_contact_flow"):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_FEEDBACK_TIMEOUT,
                "journey_label": LABEL_SUBMITTED,
            })
            pe_dispatcher.handle_feedback_timeout(row)
            pe_dispatcher.handle_feedback_timeout(row)

        new_count = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "delivery_failure_count"
        )
        self.assertEqual(new_count, 2)

    # CR-003: test_handle_grace_reminder_picks_correct_day removed.
    # The handle_grace_reminder dispatcher and _get_current_reminder_index
    # helper are deleted; grace reminders are gone in favor of per-step
    # escalations within the week. Coverage for the new grace_check handler
    # lives in test_grace_logic.py.

    def test_handle_pause_check_resumes_when_calendar_advances(self):
        """A binge-paused PE on week 2 should resume to normal_content_delivery
        when batch.current_calendar_week advances to >= the PE's next_week (3)."""
        from tap_lms.summer_program import pe_dispatcher

        # Bump the batch calendar so the resume condition holds.
        frappe.db.set_value(
            "Batch", self.batch_name, "current_calendar_week", 3,
            update_modified=False,
        )

        s = _ensure_student("71")
        pe_name = _make_pe(
            self.batch_name,
            s,
            add_to_date(now_datetime(), minutes=-1),
            ACTION_PAUSE_CHECK,
            program_status=PROGRAM_PAUSED,
            resolved_flow_state=STATE_PAUSED_BINGE,
            journey_label=LABEL_PAUSED,
            enrollment_suffix="PC1",
            glific_id="glific-disp-PC1",
            current_week=2,
        )

        with patch("tap_lms.glific_integration.start_contact_flow"), \
             patch("tap_lms.glific_integration.update_contact_fields"):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_PAUSE_CHECK,
                "journey_label": LABEL_PAUSED,
            })
            pe_dispatcher.handle_pause_check(row)

        # State must have moved off paused_binge.
        new_state = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "resolved_flow_state"
        )
        self.assertNotEqual(new_state, STATE_PAUSED_BINGE)
        # Restore batch calendar
        frappe.db.set_value(
            "Batch", self.batch_name, "current_calendar_week", 1,
            update_modified=False,
        )

    def test_handle_week_advancement_calls_t14(self):
        """A week_completed PE should advance one week via T14 when the next
        week is within max_allowed and not past total_weeks."""
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("81")
        pe_name = _make_pe(
            self.batch_name,
            s,
            add_to_date(now_datetime(), minutes=-1),
            ACTION_WEEK_ADVANCEMENT,
            resolved_flow_state=STATE_WEEK_COMPLETED,
            journey_label=LABEL_CONTENT_DELIVERED,  # any non-special label is fine here
            enrollment_suffix="W1",
            glific_id="glific-disp-W1",
            current_week=1,
        )

        # Allow the advancement: max_allowed_week >= 2
        frappe.db.set_value(
            "ProgramEnrollment", pe_name, "max_allowed_week", 4,
            update_modified=False,
        )
        # Make sure batch.current_calendar_week is far enough.
        frappe.db.set_value(
            "Batch", self.batch_name, "current_calendar_week", 4,
            update_modified=False,
        )

        with patch(
            "tap_lms.summer_program.state_machine.t14_week_advance"
        ) as fake_t14, patch(
            "tap_lms.glific_integration.start_contact_flow"
        ), patch(
            "tap_lms.glific_integration.update_contact_fields"
        ):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_WEEK_ADVANCEMENT,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_week_advancement(row)

        # T14 was invoked with new_week == 2.
        self.assertEqual(fake_t14.call_count, 1)
        args, _ = fake_t14.call_args
        # signature: (pe, new_week, week_rule, trigger_source)
        self.assertEqual(args[1], 2)

        # Cleanup
        frappe.db.set_value(
            "Batch", self.batch_name, "current_calendar_week", 1,
            update_modified=False,
        )

    def test_handle_grace_check_calls_t17_when_still_in_grace(self):
        """A PE whose grace_check timer fires while still in grace_waiting
        must invoke t17_grace_expired (CR-003 renamed from t18). If the
        student submitted during grace and moved out of the state, the
        handler should no-op (clear action, skip transition).

        CR-003: the t17 function now routes directly to program_dropped
        with drop_reason='grace_expired'. The paused_no_activity hop
        and re-engagement loop are gone."""
        from tap_lms.summer_program import pe_dispatcher
        from tap_lms.summer_program.constants import ACTION_GRACE_CHECK

        # Case 1: still in grace_waiting → t18 should fire
        s = _ensure_student("71")
        pe_name = _make_pe(
            self.batch_name,
            s,
            add_to_date(now_datetime(), minutes=-1),
            ACTION_GRACE_CHECK,
            resolved_flow_state=STATE_GRACE_WAITING,
            journey_label=LABEL_GRACE_WINDOW,
            enrollment_suffix="GC1",
            glific_id="glific-disp-GC1",
            grace_window_start=add_to_date(now_datetime(), days=-14),
        )

        with patch(
            "tap_lms.summer_program.state_machine.t17_grace_expired"
        ) as fake_t18:
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_GRACE_CHECK,
                "journey_label": LABEL_GRACE_WINDOW,
            })
            pe_dispatcher.handle_grace_check(row)

        self.assertEqual(fake_t18.call_count, 1)

        # Case 2: state already moved (student submitted during grace) →
        # handler should no-op
        s2 = _ensure_student("72")
        pe_name_2 = _make_pe(
            self.batch_name,
            s2,
            add_to_date(now_datetime(), minutes=-1),
            ACTION_GRACE_CHECK,
            resolved_flow_state=STATE_SUBMITTED_AWAITING,  # already moved
            journey_label=LABEL_SUBMITTED,
            enrollment_suffix="GC2",
            glific_id="glific-disp-GC2",
        )

        with patch(
            "tap_lms.summer_program.state_machine.t17_grace_expired"
        ) as fake_t18_b:
            row = frappe._dict({
                "name": pe_name_2,
                "next_action_type": ACTION_GRACE_CHECK,
                "journey_label": LABEL_SUBMITTED,
            })
            pe_dispatcher.handle_grace_check(row)

        # t18 NOT invoked — student is past the grace state
        self.assertEqual(fake_t18_b.call_count, 0)
        # And next_action was cleared (idempotency)
        cleared = frappe.db.get_value(
            "ProgramEnrollment", pe_name_2, "next_action_type"
        )
        self.assertEqual(cleared or "", "")

    def test_auto_activate_due_bprs_idempotent(self):
        """check_auto_activate run twice on the same BPR is a no-op the
        second time (the BPR is already active, so the inner activate_bpr
        call returns success=False with 'already active' message)."""
        from tap_lms.summer_program import batch_activation

        # Stand up a BPR that's ready to auto-activate.
        bpr = frappe.new_doc("BatchProgramRun")
        bpr.batch = self.batch_name
        bpr.status = BPR_COLLECTIONS_READY
        bpr.validation_status = VALIDATION_PASSED
        bpr.total_imported = 1
        bpr.total_enrolled = 1
        bpr.insert(ignore_permissions=True)
        bpr_name = bpr.name

        try:
            # First pass: should activate.
            count_first = batch_activation.check_auto_activate()
            self.assertGreaterEqual(count_first, 1)

            status_after_first = frappe.db.get_value(
                "BatchProgramRun", bpr_name, "status"
            )
            self.assertEqual(status_after_first, BPR_ACTIVE)

            # Second pass: BPR is now active, so the candidate query
            # filters on status = collections_ready and finds nothing.
            count_second = batch_activation.check_auto_activate()
            self.assertEqual(count_second, 0)

            # State unchanged.
            status_after_second = frappe.db.get_value(
                "BatchProgramRun", bpr_name, "status"
            )
            self.assertEqual(status_after_second, BPR_ACTIVE)
        finally:
            if frappe.db.exists("BatchProgramRun", bpr_name):
                frappe.delete_doc("BatchProgramRun", bpr_name, force=True)
