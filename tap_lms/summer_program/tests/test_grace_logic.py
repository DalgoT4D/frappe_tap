"""
Tests for CR-003 follow-up (2026-05-13) — grace clock refactor.

The grace clock is now:
  - ARMED by `activity_points.handle_content_log` on the week's FIRST
    VideoClass completion (atomic Postgres CASE WHEN on the same UPDATE
    that flips weekly_video_done 0→1).
  - CLEARED by primary submission transitions T7/T9/T17/T3.
  - RE-ARMED automatically each week — T19 resets weekly_video_done to 0,
    and the next VideoClass completion re-trips the CASE WHEN with a fresh
    grace_window_end_at.
  - DROPS the student via the existing handle_grace_check dispatcher path
    (scheduled by T5/T11).

T0 and T19 NO LONGER arm the clock. Tests below cover all five paths plus
the multi-week-grace scenario where the clock spans week boundaries.
"""
from datetime import timedelta

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime, add_to_date, get_datetime
from unittest.mock import patch

from tap_lms.summer_program.tests.factories import make_batch

from tap_lms.summer_program.constants import (
    LABEL_CONTENT_DELIVERED,
    LABEL_GRACE_WINDOW,
    PATH_CORE,
    PROGRAM_ACTIVE,
    STATE_GRACE_WAITING,
    STATE_NORMAL_CONTENT,
    STATE_NORMAL_ESCALATION,
    STATE_PROGRAM_DROPPED,
    STATE_WEEK_COMPLETED,
)
from tap_lms.summer_program.state_machine import (
    t0_enrollment,
    t5_escalation_to_grace,
    t7_core_submission,
    t14_week_advance,
)


# ════════════════════════════════════════════════════════════
# Test fixtures
# ════════════════════════════════════════════════════════════

def _ensure_batch(grace_days=14):
    # Idempotent-update branch preserved: re-asserts grace_window_days when the
    # batch already exists (tests call it with varying grace values). Creation
    # delegates to the shared factory (L-037) so the fixture inherits future
    # mandatory-field additions.
    name = frappe.get_value("Batch", {"name1": f"GraceTestBatch{grace_days}"}, "name")
    if name:
        # Make sure grace_window_days is what we expect.
        frappe.db.set_value("Batch", name, "grace_window_days", grace_days,
                            update_modified=False)
        return name
    return make_batch(
        label=f"GraceTestBatch{grace_days}",
        batch_id=f"GT{grace_days:02d}",
        grace_window_days=grace_days,
    )


def _ensure_student(suffix):
    name = frappe.get_value("Student", {"phone": f"+9999700{suffix}"}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"GraceTestStudent{suffix}"
    s.phone = f"+9999700{suffix}"
    s.glific_id = f"glific-grace-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _ensure_video(suffix, points=10):
    """Create or reuse a VideoClass with the given points value."""
    name = frappe.get_value("VideoClass", {"video_id": f"GR-VID-{suffix}"}, "name")
    if name:
        frappe.db.set_value("VideoClass", name, "points", points,
                            update_modified=False)
        return name
    v = frappe.new_doc("VideoClass")
    v.video_id = f"GR-VID-{suffix}"
    v.title = f"Grace Test Video {suffix}"
    v.points = points
    # VideoClass requires some additional fields depending on schema; insert
    # may need ignore_mandatory if the doctype has reqd fields. Use
    # ignore_mandatory to be tolerant of schema drift in the test env.
    v.insert(ignore_permissions=True, ignore_mandatory=True)
    return v.name


def _make_pe(batch_name, student_name, suffix, **kwargs):
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-GR-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-grace-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = kwargs.get("resolved_flow_state", STATE_NORMAL_CONTENT)
    pe.journey_label = kwargs.get("journey_label", LABEL_CONTENT_DELIVERED)
    pe.current_path = PATH_CORE
    pe.current_week = kwargs.get("current_week", 1)
    pe.current_tier = "Basic"
    pe.archetype = "submitter"
    pe.weekly_submission_done = kwargs.get("weekly_submission_done", 0)
    pe.weekly_video_done = kwargs.get("weekly_video_done", 0)
    if "grace_window_end_at" in kwargs:
        pe.grace_window_end_at = kwargs["grace_window_end_at"]
        pe.grace_window_start = kwargs.get("grace_window_start",
                                          add_to_date(now_datetime(), days=-1))
        pe.in_grace_window = 1
    pe.insert(ignore_permissions=True)
    return pe.name


def _make_scl(student_name, video_name, suffix):
    """Insert a StudentContentLog row representing a VideoClass completion."""
    scl = frappe.new_doc("StudentContentLog")
    scl.student = student_name
    scl.content_id = video_name
    scl.content_type = "VideoClass"
    scl.action = "completed"
    scl.completed_at = now_datetime()
    # SCL.name auto-generated. Insert ignoring permissions; ignore_mandatory
    # for tolerance of schema drift.
    scl.insert(ignore_permissions=True, ignore_mandatory=True)
    return scl


# ════════════════════════════════════════════════════════════
# CR-003 follow-up — Grace clock ARMED by activity-points handler
# ════════════════════════════════════════════════════════════

class TestActivityPointsArmsGraceClock(FrappeTestCase):
    """The activity-points handler is now the sole arm path for the grace
    clock. These tests exercise the atomic CASE WHEN UPDATE end-to-end.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch(grace_days=14)

    def test_first_videoclass_of_week_arms_grace_clock(self):
        """First VideoClass completion of the week (weekly_video_done = 0)
        arms grace_window_end_at = NOW() + batch.grace_window_days * 24h,
        sets grace_window_start, and flips in_grace_window = 1.
        """
        from tap_lms.summer_program.activity_points import award_activity_points

        student_name = _ensure_student("a1")
        video_name = _ensure_video("a1", points=10)
        pe_name = _make_pe(self.batch_name, student_name, "a1",
                           weekly_video_done=0)

        scl = _make_scl(student_name, video_name, "a1")
        before = now_datetime()
        with patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync"):
            award_activity_points(scl)
        after = now_datetime()

        row = frappe.db.get_value(
            "ProgramEnrollment", pe_name,
            ["in_grace_window", "grace_window_start", "grace_window_end_at",
             "weekly_video_done"],
            as_dict=True,
        )
        self.assertEqual(row.in_grace_window, 1)
        self.assertIsNotNone(row.grace_window_start)
        self.assertIsNotNone(row.grace_window_end_at)
        self.assertEqual(row.weekly_video_done, 1)

        # End - start should be 14 days (give or take a few seconds).
        delta = get_datetime(row.grace_window_end_at) - get_datetime(row.grace_window_start)
        self.assertAlmostEqual(delta.total_seconds(), 14 * 86400, delta=120)

        # Start should be ~ now() at the time of the call.
        start = get_datetime(row.grace_window_start)
        self.assertGreaterEqual(start, before - timedelta(seconds=2))
        self.assertLessEqual(start, after + timedelta(seconds=2))

    def test_second_videoclass_same_week_does_not_re_arm(self):
        """Second VideoClass completion while weekly_video_done = 1 must
        NOT change grace_window_end_at. The CASE WHEN should evaluate
        ELSE grace_window_end_at (preserve) on the second call.
        """
        from tap_lms.summer_program.activity_points import award_activity_points

        student_name = _ensure_student("a2")
        video_name = _ensure_video("a2", points=10)
        # Pre-arm: weekly_video_done = 1 and grace_window_end_at set.
        original_end = add_to_date(now_datetime(), days=10)
        pe_name = _make_pe(self.batch_name, student_name, "a2",
                           weekly_video_done=1,
                           grace_window_end_at=original_end)

        scl = _make_scl(student_name, video_name, "a2")
        with patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync"):
            award_activity_points(scl)

        new_end = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "grace_window_end_at"
        )
        new_end_dt = get_datetime(new_end)
        # Preserved — within 2 seconds of the original.
        self.assertAlmostEqual(
            (new_end_dt - get_datetime(original_end)).total_seconds(),
            0, delta=2,
            msg="Second VideoClass of week must NOT re-arm grace_window_end_at",
        )

    def test_first_videoclass_next_week_re_arms_with_fresh_clock(self):
        """After T19 resets weekly_video_done, the next VideoClass watch
        re-arms grace_window_end_at to NOW() + grace_window_days * 24h.
        The new timestamp must reflect the second arm, not the original.
        """
        from tap_lms.summer_program.activity_points import award_activity_points

        student_name = _ensure_student("a3")
        video_name = _ensure_video("a3", points=10)

        # Simulate end-of-week-1: weekly_video_done = 1 and a stale clock.
        old_end = add_to_date(now_datetime(), days=-3)
        pe_name = _make_pe(self.batch_name, student_name, "a3",
                           current_week=1,
                           weekly_video_done=1,
                           grace_window_end_at=old_end)
        pe = frappe.get_doc("ProgramEnrollment", pe_name)

        # T19 (week advance) — resets weekly_video_done to 0.
        with patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            pe.resolved_flow_state = STATE_WEEK_COMPLETED
            pe.save(ignore_permissions=True)
            t14_week_advance(pe, 2, week_rule=None, trigger_source="test")

        # After T19, weekly_video_done should be 0 and grace fields still
        # whatever they were (T19 doesn't touch them — see follow-up).
        row = frappe.db.get_value(
            "ProgramEnrollment", pe_name,
            ["weekly_video_done", "grace_window_end_at"], as_dict=True,
        )
        self.assertEqual(row.weekly_video_done, 0)

        # Now fire activity-points; this is the new week's first VideoClass.
        scl = _make_scl(student_name, video_name, "a3")
        before = now_datetime()
        with patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync"):
            award_activity_points(scl)

        new_end = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "grace_window_end_at"
        )
        new_end_dt = get_datetime(new_end)
        # New end is in the future (>13 days from now) — definitely a new
        # arm, not the stale 3-days-ago value.
        self.assertGreater(new_end_dt, before)
        delta = new_end_dt - now_datetime()
        self.assertGreater(delta.total_seconds(), 13 * 86400)
        self.assertLess(delta.total_seconds(), 15 * 86400)


# ════════════════════════════════════════════════════════════
# CR-003 follow-up — Grace clock CLEARED by primary submissions
# ════════════════════════════════════════════════════════════

class TestSubmissionClearsGraceClock(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch(grace_days=14)

    def test_submission_clears_grace_state(self):
        """T7 (primary submission from normal_content_delivery) clears
        in_grace_window, grace_window_end_at, and grace_window_start.
        """
        student_name = _ensure_student("b1")
        original_end = add_to_date(now_datetime(), days=7)
        pe_name = _make_pe(self.batch_name, student_name, "b1",
                           resolved_flow_state=STATE_NORMAL_CONTENT,
                           grace_window_end_at=original_end,
                           weekly_video_done=1)
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        # Sanity: clock is armed.
        self.assertEqual(pe.in_grace_window, 1)
        self.assertIsNotNone(pe.grace_window_end_at)

        with patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            t7_core_submission(pe, points=10, trigger_source="test")

        pe.reload()
        self.assertEqual(pe.in_grace_window, 0)
        self.assertIsNone(pe.grace_window_end_at)
        self.assertIsNone(pe.grace_window_start)


# ════════════════════════════════════════════════════════════
# CR-003 follow-up — T19 and T0 NO LONGER arm the grace clock
# ════════════════════════════════════════════════════════════

class TestT0T19DoNotArmGrace(FrappeTestCase):
    """T0 (enrollment) and T19 (week advance) used to arm the grace clock
    under the original CR-003. The 2026-05-13 follow-up moved arming into
    the activity-points handler. These tests pin the new behaviour so a
    regression that re-introduces T0/T19 arming would fail loudly.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch(grace_days=14)

    def test_t0_does_not_arm_grace_clock(self):
        """T0 (enrollment) does NOT arm the grace clock. The PE starts with
        no grace_window_end_at; the clock arms only when the student watches
        their first VideoClass.
        """
        student_name = _ensure_student("c1")
        pe_name = _make_pe(self.batch_name, student_name, "c1")
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        # Sanity: nothing armed pre-T0.
        self.assertFalse(pe.in_grace_window)
        self.assertIsNone(pe.grace_window_end_at)

        with patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            t0_enrollment(pe, "test")

        pe.reload()
        # T0 does NOT arm.
        self.assertFalse(pe.in_grace_window,
                         "T0 must not arm in_grace_window (CR-003 follow-up)")
        self.assertIsNone(pe.grace_window_end_at,
                          "T0 must not set grace_window_end_at (CR-003 follow-up)")
        self.assertIsNone(pe.grace_window_start,
                          "T0 must not set grace_window_start (CR-003 follow-up)")

    def test_t19_does_not_arm_grace_clock(self):
        """T19 (week advance) does NOT touch the grace clock fields. They
        retain whatever the prior week left behind (None if cleared by a
        submission, or an old timestamp if grace_expired already fired —
        though that path lands in program_dropped, not here).
        """
        student_name = _ensure_student("c2")
        # PE entering T19 with no grace state (submission already cleared it).
        pe_name = _make_pe(self.batch_name, student_name, "c2",
                           resolved_flow_state=STATE_WEEK_COMPLETED,
                           current_week=1)
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        # Sanity: PE has no clock at the start.
        self.assertIsNone(pe.grace_window_end_at)

        with patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            t14_week_advance(pe, 2, week_rule=None, trigger_source="test")

        pe.reload()
        # T19 did NOT arm.
        self.assertIsNone(pe.grace_window_end_at,
                          "T19 must not set grace_window_end_at (CR-003 follow-up)")
        # weekly_video_done was reset, which is the gating signal for the
        # activity-points handler's next arm.
        self.assertEqual(pe.weekly_video_done, 0)
        self.assertEqual(pe.current_week, 2)


# ════════════════════════════════════════════════════════════
# CR-003 follow-up — Multi-week grace span
# ════════════════════════════════════════════════════════════

class TestGraceCanSpanMultipleWeeks(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch(grace_days=14)

    def test_grace_can_span_multiple_weeks(self):
        """A student who arms the grace clock on week 1 day 1 and doesn't
        submit can carry their grace window across the T19 boundary into
        week 2 — the clock is real-time, not weekly-bound. T19 preserves
        grace_window_end_at and does not reset it.

        (In production the dispatcher's escalation chain would normally
        drop the student via t17_grace_expired before week 2 starts, but
        the multi-week-grace semantic remains: an unsubmitted clock from
        week 1 keeps ticking into week 2 if T19 happens to fire first.)
        """
        student_name = _ensure_student("d1")
        # Week 1, grace clock armed 10 days from now.
        original_end = add_to_date(now_datetime(), days=10)
        pe_name = _make_pe(self.batch_name, student_name, "d1",
                           current_week=1,
                           resolved_flow_state=STATE_WEEK_COMPLETED,
                           weekly_video_done=1,
                           grace_window_end_at=original_end)
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.in_grace_window, 1)

        with patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            t14_week_advance(pe, 2, week_rule=None, trigger_source="test")

        pe.reload()
        # Grace window survives the week boundary.
        self.assertEqual(pe.current_week, 2)
        self.assertEqual(pe.in_grace_window, 1,
                         "Grace must persist across T19 (CR-003 follow-up multi-week span)")
        # grace_window_end_at unchanged from week-1 arm.
        new_end = get_datetime(pe.grace_window_end_at)
        self.assertAlmostEqual(
            (new_end - get_datetime(original_end)).total_seconds(),
            0, delta=2,
            msg="T19 must preserve grace_window_end_at across the week boundary",
        )


# ════════════════════════════════════════════════════════════
# CR-003 — preserved tests (T5 + handle_grace_check)
# ════════════════════════════════════════════════════════════

class TestGraceCheckHandler(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch(grace_days=14)

    def test_grace_check_drops_at_expiry(self):
        """handle_grace_check: clock expired AND weekly_submission_done = 0
        → t17_grace_expired runs, PE lands in program_dropped.
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("11")
        pe_name = _make_pe(
            self.batch_name, s, "11",
            resolved_flow_state=STATE_GRACE_WAITING,
            journey_label=LABEL_GRACE_WINDOW,
            grace_window_end_at=add_to_date(now_datetime(), minutes=-5),
            weekly_submission_done=0,
        )

        with patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": "grace_check",
                "journey_label": LABEL_GRACE_WINDOW,
            })
            pe_dispatcher.handle_grace_check(row)

        new_state = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "resolved_flow_state"
        )
        self.assertEqual(new_state, STATE_PROGRAM_DROPPED)
        drop_reason = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "drop_reason"
        )
        self.assertEqual(drop_reason, "grace_expired")

    def test_grace_check_no_op_if_submission_done(self):
        """handle_grace_check: weekly_submission_done = 1 → no drop,
        just clear the action.
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("12")
        pe_name = _make_pe(
            self.batch_name, s, "12",
            resolved_flow_state=STATE_GRACE_WAITING,
            journey_label=LABEL_GRACE_WINDOW,
            grace_window_end_at=add_to_date(now_datetime(), minutes=-5),
            weekly_submission_done=1,
        )

        row = frappe._dict({
            "name": pe_name,
            "next_action_type": "grace_check",
            "journey_label": LABEL_GRACE_WINDOW,
        })
        pe_dispatcher.handle_grace_check(row)

        new_state = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "resolved_flow_state"
        )
        # Still in grace_waiting; no drop.
        self.assertEqual(new_state, STATE_GRACE_WAITING)
        next_action_type = frappe.db.get_value(
            "ProgramEnrollment", pe_name, "next_action_type"
        )
        self.assertEqual(next_action_type or "", "")


class TestT5PreservesGraceClock(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch(grace_days=14)

    def test_t5_escalation_to_grace_preserves_existing_clock(self):
        """T5 (escalation exhausted with some activity → grace) does NOT
        reset grace_window_end_at. CR-003 follow-up: the clock was armed
        by the activity-points handler when the student watched their
        week-1 first VideoClass; T5 just transitions the PE into the
        dead-air tail state and schedules grace_check at that timestamp.
        """
        s = _ensure_student("21")
        original_end = add_to_date(now_datetime(), days=10)  # 10 days from now
        pe_name = _make_pe(
            self.batch_name, s, "21",
            resolved_flow_state=STATE_NORMAL_ESCALATION,
            journey_label=LABEL_CONTENT_DELIVERED,
            grace_window_end_at=original_end,
        )
        pe = frappe.get_doc("ProgramEnrollment", pe_name)

        with patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            t5_escalation_to_grace(pe, "test")

        pe.reload()
        # grace_window_end_at unchanged from the value at week start.
        new_end = get_datetime(pe.grace_window_end_at)
        self.assertAlmostEqual(
            (new_end - get_datetime(original_end)).total_seconds(),
            0, delta=2,
            msg="T5 must NOT reset grace_window_end_at",
        )
        # State is grace_waiting; next_action_type = grace_check at grace_end.
        self.assertEqual(pe.resolved_flow_state, STATE_GRACE_WAITING)
        self.assertEqual(pe.next_action_type, "grace_check")
