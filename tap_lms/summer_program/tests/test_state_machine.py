"""
Tests for CR-002 v2 state-machine extensions.

Covers:
  - T3/T7/T9/T17 submission transitions extended with streak/gem/submission
    counter bumps and `weekly_submission_done` sticky flag.
  - T19 (`t14_week_advance`) restructured to compute streak/gem updates from
    sticky flags before resetting weeklies.
  - `_enqueue_contact_field_sync` extended to push 8 new gamification fields
    (cache size 26 total, 18 existing + 8 new). `weekly_video_done` is NOT
    pushed. CR-003 adds 1 more (escalation_order) to the per-transition push
    for a total of 20 fields per transition; the 28th cache field
    (escalation_type) is pushed by the dispatcher per-step.

Tests in this file:
  1. test_t7_extends_streak_gems_submission_done_flag
  2. test_t9_extends_same_atomicity
  3. test_t17_t17b_same  (covers T17 + T3 — the four "submission transitions")
  4. test_t19_streak_reset_when_assigned_no_submit
  5. test_t19_streak_unchanged_when_not_assigned
  6. test_t19_gem_floored_at_zero
  7. test_sync_contact_fields_pushes_expected_fields (CR-003: 11 + 8 + 1)
"""
import frappe

from tap_lms.summer_program.tests.factories import make_batch
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from tap_lms.summer_program.constants import (
    LABEL_CONTENT_DELIVERED,
    LABEL_GRACE_WINDOW,
    LABEL_SUBMITTED,
    LABEL_WEEK_ADVANCED,
    PATH_CORE,
    PROGRAM_ACTIVE,
    STATE_GRACE_WAITING,
    STATE_NORMAL_CONTENT,
    STATE_NORMAL_ESCALATION,
    STATE_REMEDIAL_CONTENT,
    STATE_SUBMITTED_AWAITING,
    STATE_WEEK_COMPLETED,
)
from tap_lms.summer_program.state_machine import (
    t3_escalation_submission,
    t7_core_submission,
    t9_remedial_submission,
    t14_week_advance,
    t17_grace_submission,
    _enqueue_contact_field_sync,
)


# ════════════════════════════════════════════════════════════
# Test fixtures
# ════════════════════════════════════════════════════════════

def _ensure_batch():
    # Delegates to the shared factory (L-037) so this fixture inherits future
    # mandatory-field additions instead of breaking with MandatoryError.
    return make_batch(label="SMachineTestBatch", batch_id="SMT01")


def _ensure_student(suffix):
    name = frappe.get_value("Student", {"phone": f"+9999300{suffix}"}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"SMachineTestStudent{suffix}"
    s.phone = f"+9999300{suffix}"
    s.glific_id = f"glific-sm-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(
    batch_name,
    student_name,
    suffix,
    *,
    resolved_flow_state=STATE_NORMAL_CONTENT,
    journey_label=LABEL_CONTENT_DELIVERED,
    current_week=1,
    total_points=0,
    current_streak=0,
    special_gems=0,
    weekly_video_done=0,
    weekly_submission_done=0,
    total_submission_points=0,
    weekly_submission_points=0,
    weekly_activity_points=0,
    weekly_quiz_points=0,
    bonus_quiz_points=0,
    total_activity_points=0,
    total_quiz_points=0,
):
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-SM-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-sm-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = resolved_flow_state
    pe.journey_label = journey_label
    pe.current_path = PATH_CORE
    pe.current_week = current_week
    pe.total_points = total_points
    pe.current_streak = current_streak
    pe.special_gems = special_gems
    pe.weekly_video_done = weekly_video_done
    pe.weekly_submission_done = weekly_submission_done
    pe.total_submission_points = total_submission_points
    pe.weekly_submission_points = weekly_submission_points
    pe.weekly_activity_points = weekly_activity_points
    pe.weekly_quiz_points = weekly_quiz_points
    pe.bonus_quiz_points = bonus_quiz_points
    pe.total_activity_points = total_activity_points
    pe.total_quiz_points = total_quiz_points
    pe.insert(ignore_permissions=True)
    return pe


# ════════════════════════════════════════════════════════════
# T7 / T9 / T17 / T3 — submission transitions
# ════════════════════════════════════════════════════════════

class TestSubmissionTransitionsExtended(FrappeTestCase):
    """T7/T9/T17/T3 must bump the new submission counters, streak, gems, and
    set the weekly_submission_done sticky flag in one atomic save."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def _assert_submission_extras(self, pe, *, prior_streak, prior_gems, points):
        """Common assertions for any of T3/T7/T9/T17.

        Post-CR-007 + task #67 (2026-05-22): submission transitions NO
        LONGER bump the point-stream columns (total_points,
        total_submission_points, weekly_submission_points). Those are
        owned exclusively by feedback_consumer_hook._award_submission_points_atomic
        which runs after AI validation. The transition's job is the
        streak/gems/sticky-flag triplet and the state transition itself.

        The `points` parameter is preserved on the test signatures for
        backward compatibility with the existing call sites but is ignored
        in the assertion set — the columns are expected to stay at their
        pre-transition values (0 for fresh _make_pe-built fixtures).
        """
        pe.reload()
        self.assertEqual(pe.weekly_submission_done, 1,
                         "Sticky flag must be set on submission")
        self.assertEqual(pe.current_streak, prior_streak + 1,
                         "current_streak += 1 on submission")
        self.assertEqual(pe.special_gems, prior_gems + 1,
                         "special_gems += 1 on submission")
        # Point-stream columns are NOT bumped by the transition anymore
        # (task #67). They start at 0 from the fixture and stay at 0 here
        # because the feedback hook (which owns the atomic bump) is not
        # part of this test path. `points` is accepted for signature
        # compatibility but unused.
        del points
        self.assertEqual(pe.total_submission_points, 0,
                         "total_submission_points NOT bumped by transition "
                         "(feedback hook owns this)")
        self.assertEqual(pe.weekly_submission_points, 0,
                         "weekly_submission_points NOT bumped by transition")
        self.assertEqual(pe.total_points, 0,
                         "total_points NOT bumped by transition")

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t7_extends_streak_gems_submission_done_flag(self, mock_sync):
        """T7 (Core path submission) bumps the full set of CR-002 v2 fields."""
        student = _ensure_student("T7")
        pe = _make_pe(
            self.batch_name, student, "T7",
            current_streak=2, special_gems=3,
        )

        t7_core_submission(pe, points=25, trigger_source="flow_callback")

        self._assert_submission_extras(pe, prior_streak=2, prior_gems=3, points=25)
        pe.reload()
        self.assertEqual(pe.resolved_flow_state, STATE_SUBMITTED_AWAITING)
        self.assertEqual(pe.journey_label, LABEL_SUBMITTED)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t9_extends_same_atomicity(self, mock_sync):
        """T9 (Remedial submission) bumps the same fields as T7 — single
        atomic save, no second UPDATE."""
        student = _ensure_student("T9")
        pe = _make_pe(
            self.batch_name, student, "T9",
            resolved_flow_state=STATE_REMEDIAL_CONTENT,
            current_streak=5, special_gems=1,
        )

        t9_remedial_submission(pe, points=10, trigger_source="flow_callback")

        self._assert_submission_extras(pe, prior_streak=5, prior_gems=1, points=10)
        pe.reload()
        self.assertEqual(pe.resolved_flow_state, STATE_SUBMITTED_AWAITING)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t17_t17b_same(self, mock_sync):
        """T17 (grace submission) AND T3 (escalation submission) both bump
        the full set. The CR labelled these 'T7/T9/T17/T17b'; in code T3 is
        the fourth submission transition (apply_submission_transition map).
        """
        student_a = _ensure_student("T17")
        pe_a = _make_pe(
            self.batch_name, student_a, "T17",
            resolved_flow_state=STATE_GRACE_WAITING,
            journey_label=LABEL_GRACE_WINDOW,
            current_streak=0, special_gems=0,
        )
        # Grace transition uses these — set them so the reset clauses fire.
        pe_a.in_grace_window = 1
        pe_a.save(ignore_permissions=True)

        t17_grace_submission(pe_a, points=15, trigger_source="flow_callback")
        self._assert_submission_extras(pe_a, prior_streak=0, prior_gems=0, points=15)
        pe_a.reload()
        self.assertEqual(pe_a.in_grace_window, 0)
        self.assertEqual(pe_a.resolved_flow_state, STATE_SUBMITTED_AWAITING)

        # T3 (escalation submission)
        student_b = _ensure_student("T3")
        pe_b = _make_pe(
            self.batch_name, student_b, "T3",
            resolved_flow_state=STATE_NORMAL_ESCALATION,
            current_streak=4, special_gems=2,
        )
        t3_escalation_submission(pe_b, points=20, trigger_source="flow_callback")
        self._assert_submission_extras(pe_b, prior_streak=4, prior_gems=2, points=20)


# ════════════════════════════════════════════════════════════
# T19 (`t14_week_advance`) — streak/gem reset logic
# ════════════════════════════════════════════════════════════

class TestT19WeekAdvanceExtended(FrappeTestCase):
    """CR-002 v2 §"T19 week-advance — revised" + CR-008 lazy-reset (2026-05-23).

    Penalty branch fires IFF weekly_video_done=1 AND weekly_submission_done=0.
    Cumulative counters are rolled up via calculate_week_advance_rollup at T14.

    Post-CR-008: T14 NO LONGER zeros weekly_* gamification fields, submission_done
    flag, quiz_completed, bonus_quiz_points, or submission_count. Those reset
    LAZILY on the first VideoClass of the new week (see test_lazy_reset_on_video
    below). T14 still resets weekly_video_done = 0 (the lazy-reset trigger
    signal) and current_escalation_step.

    These tests verify:
      - The rollup math is correct (totals = previous + weekly_*).
      - weekly_*_points are PRESERVED (not zeroed) after T14.
      - submission_count is PRESERVED (lifetime accumulator).
      - weekly_video_done = 0 after T14 (the signal that gates next-video
        lazy reset in activity_points.award_activity_points).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t19_streak_reset_when_assigned_no_submit(self, mock_sync):
        """Penalty branch: assigned video this week (weekly_video_done=1)
        but no submission (weekly_submission_done=0) → streak → 0,
        gems → max(0, gems-1)."""
        student = _ensure_student("T19a")
        pe = _make_pe(
            self.batch_name, student, "T19a",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=1,
            current_streak=4,
            special_gems=3,
            weekly_video_done=1,
            weekly_submission_done=0,
            # Pre-existing weekly + cumulative values to verify reset behavior
            weekly_activity_points=10, weekly_quiz_points=5, weekly_submission_points=0,
            bonus_quiz_points=2,
            total_activity_points=30, total_quiz_points=15,
            total_submission_points=50, total_points=95,
        )

        t14_week_advance(pe, new_week=2)

        pe.reload()
        # Penalty applied
        self.assertEqual(pe.current_streak, 0, "Streak resets to 0 on penalty")
        self.assertEqual(pe.special_gems, 2, "Gems decrement by 1 (3 → 2)")
        # CR-008 lazy reset: weekly_* PRESERVED at pre-T14 values (next
        # VideoClass wipes them via activity_points handler). Sticky flags
        # likewise preserved except for weekly_video_done which is the
        # lazy-reset trigger and must be 0 here.
        self.assertEqual(pe.weekly_activity_points, 10,
                         "lazy reset: weekly_* preserved through inter-week gap")
        self.assertEqual(pe.weekly_quiz_points, 5)
        self.assertEqual(pe.bonus_quiz_points, 2)
        self.assertEqual(pe.weekly_submission_points, 0)
        self.assertEqual(pe.weekly_video_done, 0,
                         "trigger signal: must flip to 0 for next-video lazy reset")
        self.assertEqual(pe.weekly_submission_done, 0,
                         "preserved (was 0 entering T14)")
        # Weekly buckets STILL roll into cumulative totals at T14; the lazy
        # reset is orthogonal to the rollup. Product rule: submission is part
        # of activity, so total_activity includes weekly submission points.
        # Pre-rollup setup: total_activity=30, total_quiz=15, total_submission=50.
        # weekly_activity=10, weekly_quiz=5, weekly_submission=0, bonus=2.
        self.assertEqual(pe.total_activity_points, 40)   # 30 + 10 + 0
        self.assertEqual(pe.total_quiz_points, 20)       # 15 + 5
        self.assertEqual(pe.total_submission_points, 50)  # 50 + 0
        # total_points += weekly_act + weekly_quiz + weekly_sub + weekly_bonus
        # = 95 + 10 + 5 + 0 + 2 = 112
        self.assertEqual(pe.total_points, 112)
        # Week advanced + journey label updated
        self.assertEqual(pe.current_week, 2)
        self.assertEqual(pe.journey_label, LABEL_WEEK_ADVANCED)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t19_rolls_weekly_points_into_totals(self, mock_sync):
        """Week advance rolls current-week buckets into cumulative totals."""
        student = _ensure_student("T19roll")
        pe = _make_pe(
            self.batch_name, student, "T19roll",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=1,
            current_streak=1,
            special_gems=1,
            weekly_video_done=1,
            weekly_submission_done=1,
            weekly_activity_points=10,
            weekly_submission_points=25,
            weekly_quiz_points=7,
            bonus_quiz_points=3,
            total_activity_points=0,
            total_submission_points=0,
            total_quiz_points=0,
            total_points=0,
        )

        t14_week_advance(pe, new_week=2)

        pe.reload()
        self.assertEqual(pe.total_activity_points, 35)
        self.assertEqual(pe.total_submission_points, 25)
        self.assertEqual(pe.total_quiz_points, 7)
        # total_points = 0 + 10 + 25 + 7 + 3 = 45
        self.assertEqual(pe.total_points, 45)
        # CR-008: weekly_* PRESERVED (next VideoClass wipes them)
        self.assertEqual(pe.weekly_activity_points, 10)
        self.assertEqual(pe.weekly_submission_points, 25)
        self.assertEqual(pe.weekly_quiz_points, 7)
        self.assertEqual(pe.bonus_quiz_points, 3)
        self.assertEqual(pe.current_streak, 1)
        self.assertEqual(pe.special_gems, 1)
        # weekly_video_done must flip to 0 (lazy-reset trigger signal)
        self.assertEqual(pe.weekly_video_done, 0)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t19_activity_only_week_adds_activity_to_existing_total(self, mock_sync):
        """Existing total 35 + weekly video activity 10 → total activity 45."""
        student = _ensure_student("T19actonly")
        pe = _make_pe(
            self.batch_name, student, "T19actonly",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=2,
            current_streak=2,
            special_gems=2,
            weekly_video_done=1,
            weekly_submission_done=0,
            total_activity_points=35,
            total_submission_points=25,
            total_quiz_points=0,
            total_points=35,
            weekly_activity_points=10,
            weekly_submission_points=0,
            weekly_quiz_points=0,
            bonus_quiz_points=0,
        )

        t14_week_advance(pe, new_week=3)

        pe.reload()
        # Rollup math. Pre-rollup: total_activity=35,
        # total_sub=25, total_points=35. This week: weekly_act=10, weekly_sub=0.
        self.assertEqual(pe.total_activity_points, 45)   # 35 + 10
        self.assertEqual(pe.total_submission_points, 25)  # unchanged (no sub this week)
        self.assertEqual(pe.total_points, 45)             # 35 + 10
        # CR-008: weekly_* PRESERVED (next VideoClass wipes them)
        self.assertEqual(pe.weekly_activity_points, 10)
        self.assertEqual(pe.weekly_submission_points, 0)
        self.assertEqual(pe.current_streak, 0)
        self.assertEqual(pe.special_gems, 1)
        self.assertEqual(pe.weekly_video_done, 0)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t19_activity_and_submission_week_adds_both_to_existing_total(self, mock_sync):
        """Existing total 35 + weekly activity 10 + submission 25 → 70."""
        student = _ensure_student("T19actsub")
        pe = _make_pe(
            self.batch_name, student, "T19actsub",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=2,
            current_streak=2,
            special_gems=2,
            weekly_video_done=1,
            weekly_submission_done=1,
            total_activity_points=35,
            total_submission_points=25,
            total_quiz_points=0,
            total_points=35,
            weekly_activity_points=10,
            weekly_submission_points=25,
            weekly_quiz_points=0,
            bonus_quiz_points=0,
        )

        t14_week_advance(pe, new_week=3)

        pe.reload()
        # Rollup math. Pre-rollup: total_act=35, total_sub=25,
        # total_points=35. This week: weekly_act=10, weekly_sub=25.
        self.assertEqual(pe.total_activity_points, 70)   # 35 + 10 + 25
        self.assertEqual(pe.total_submission_points, 50)  # 25 + 25
        self.assertEqual(pe.total_points, 70)             # 35 + 10 + 25 + 0
        # CR-008: weekly_* PRESERVED (next VideoClass wipes them)
        self.assertEqual(pe.weekly_activity_points, 10)
        self.assertEqual(pe.weekly_submission_points, 25)
        self.assertEqual(pe.current_streak, 2)
        self.assertEqual(pe.special_gems, 2)
        self.assertEqual(pe.weekly_video_done, 0)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t19_streak_unchanged_when_not_assigned(self, mock_sync):
        """No-penalty branch: nothing assigned this week
        (weekly_video_done=0) → streak/gems unchanged regardless of
        weekly_submission_done value."""
        student = _ensure_student("T19b")
        pe = _make_pe(
            self.batch_name, student, "T19b",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=1,
            current_streak=7,
            special_gems=4,
            weekly_video_done=0,
            weekly_submission_done=0,
        )

        t14_week_advance(pe, new_week=2)

        pe.reload()
        self.assertEqual(pe.current_streak, 7,
                         "Streak unchanged when no video assigned")
        self.assertEqual(pe.special_gems, 4,
                         "Gems unchanged when no video assigned")

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t19_no_penalty_when_submission_done(self, mock_sync):
        """No-penalty branch: video assigned AND submitted → streak/gems
        unchanged (they were already bumped at submission time)."""
        student = _ensure_student("T19c")
        pe = _make_pe(
            self.batch_name, student, "T19c",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=1,
            current_streak=3,
            special_gems=5,
            weekly_video_done=1,
            weekly_submission_done=1,
        )

        t14_week_advance(pe, new_week=2)

        pe.reload()
        self.assertEqual(pe.current_streak, 3,
                         "Streak unchanged when both flags = 1")
        self.assertEqual(pe.special_gems, 5,
                         "Gems unchanged when both flags = 1")

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t19_gem_floored_at_zero(self, mock_sync):
        """E9: penalty branch with special_gems already at 0 → gems stays
        0, NOT -1. Floor enforced in Python via max(0, gems-1)."""
        student = _ensure_student("T19d")
        pe = _make_pe(
            self.batch_name, student, "T19d",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=1,
            current_streak=2,
            special_gems=0,
            weekly_video_done=1,
            weekly_submission_done=0,
        )

        t14_week_advance(pe, new_week=2)

        pe.reload()
        self.assertEqual(pe.current_streak, 0)
        self.assertEqual(pe.special_gems, 0,
                         "Gems must floor at 0 — never negative")


# ════════════════════════════════════════════════════════════
# Contact-field sync — 18 + 8 + 2 = 28 fields after CR-003
# ════════════════════════════════════════════════════════════

class TestSyncContactFieldsExtended(FrappeTestCase):
    """`_enqueue_contact_field_sync` must include the 8 CR-002 v2 gamification
    fields alongside the existing 18 plus the 2 new CR-003 escalation-routing
    fields (escalation_order AND escalation_type — post-2026-05-13 follow-up
    both flow via this standard sync; the eager dispatcher push was removed).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    def test_sync_contact_fields_pushes_expected_fields(self, mock_enqueue):
        """The fields dict serialized into the background job includes the
        11 state-mutating fields plus 8 new gamification fields plus 2 new
        CR-003 fields (escalation_order + escalation_type) = 21 per-transition
        push fields. Plus the 7 immutable enrollment-time fields = 28 total
        in the Glific cache. Post 2026-05-13 follow-up both CR-003 fields
        flow via this standard sync; the eager dispatcher push was deleted."""
        student = _ensure_student("CFSYNC")
        pe = _make_pe(
            self.batch_name, student, "CFSYNC",
            current_streak=2, special_gems=3,
            weekly_submission_done=1, weekly_video_done=1,
            total_activity_points=42, weekly_activity_points=10,
            total_quiz_points=33, weekly_quiz_points=8,
            total_submission_points=51, weekly_submission_points=25,
        )

        _enqueue_contact_field_sync(pe)

        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        fields = kwargs.get("fields") or {}

        # All 8 new gamification fields must be present
        self.assertIn("total_activity_points", fields)
        self.assertIn("weekly_activity_points", fields)
        self.assertIn("total_quiz_points", fields)
        self.assertIn("weekly_quiz_points", fields)
        self.assertIn("total_submission_points", fields)
        self.assertIn("weekly_submission_points", fields)
        self.assertIn("special_gems", fields)
        self.assertIn("weekly_submission_done", fields)

        # Values are string-cast like the existing pattern
        self.assertEqual(fields["total_activity_points"], "42")
        self.assertEqual(fields["weekly_activity_points"], "10")
        self.assertEqual(fields["total_quiz_points"], "33")
        self.assertEqual(fields["weekly_quiz_points"], "8")
        self.assertEqual(fields["total_submission_points"], "51")
        self.assertEqual(fields["weekly_submission_points"], "25")
        self.assertEqual(fields["special_gems"], "3")
        self.assertEqual(fields["weekly_submission_done"], "1")

        # weekly_video_done is INTERNAL-ONLY — must NOT be in the Glific push
        self.assertNotIn("weekly_video_done", fields,
                         "weekly_video_done is internal-only; never push to Glific")

        # Sanity: existing 11 state-mutating fields are still present
        for k in (
            "resolved_flow_state", "current_week", "current_path",
            "current_tier", "program_status", "total_points", "current_streak",
            "grace_window_end_at", "current_expected_submission_type",
            "last_escalation_step", "submission_count",
            # last_escalation_step is the Glific CONTACT FIELD name (public
            # contract per L-008). The PE column was renamed to
            # current_escalation_step but the contact field name stays.
        ):
            self.assertIn(k, fields, f"existing field {k} missing from sync")

        # CR-003 (post-impl): escalation_order AND escalation_type are both
        # included in the per-transition sync. The eager
        # _push_escalation_contact_fields helper was removed — the dispatcher
        # writes current_escalation_step + current_escalation_type to PE
        # inside T2/T4/T8/T10, and this sync map pushes both to Glific.
        self.assertIn("escalation_order", fields)
        self.assertIn("escalation_type", fields)

        # Task #98 (2026-05-25): bonus_quiz_points added to per-transition
        # push so the Glific gamification card's
        # @contact.fields.bonus_quiz_points always resolves.
        self.assertIn("bonus_quiz_points", fields)

        # Task #7 (2026-05-26): weekly_engagement_points pushed too. Computed
        # (NOT stored on PE) — sum of weekly_submission_points +
        # weekly_activity_points.
        self.assertIn("weekly_engagement_points", fields)

        # Total count = 11 state-mutating + 8 CR-002 v2 gamification +
        # 1 (task #98) bonus_quiz_points + 1 (task #7) weekly_engagement_points +
        # 2 CR-003 (escalation_order + escalation_type) = 23 per-transition
        # push fields. (The other 7 immutables live elsewhere.)
        self.assertEqual(
            len(fields), 23,
            "Per-transition sync pushes 11 existing + 8 gamification + "
            "1 bonus_quiz_points + 1 weekly_engagement_points + "
            "2 CR-003 (escalation_order + escalation_type) = 23 fields",
        )


# ════════════════════════════════════════════════════════════
# CR-006 — T6 deprecation + T6b regression guard
# ════════════════════════════════════════════════════════════

class TestCR006T6Deprecation(FrappeTestCase):
    """CR-006 (2026-05-15): T6 (escalation_to_remedial) is removed.

    Verifies:
      - Calling t6_escalation_to_remedial directly raises with CR-006 in msg.
      - T6b (failed-feedback → remedial) is unchanged — the SOLE path to
        remedial post-CR-006.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def test_t6_deprecation_stub_raises(self):
        """CR-006: calling t6_escalation_to_remedial directly must raise.
        The function is kept as a deprecation stub for one release cycle so
        any hidden caller surfaces loudly.
        """
        from tap_lms.summer_program.state_machine import t6_escalation_to_remedial

        student = _ensure_student("CR006T6")
        pe = _make_pe(
            self.batch_name, student, "CR006T6",
            resolved_flow_state=STATE_NORMAL_ESCALATION,
        )
        with self.assertRaises(frappe.ValidationError) as ctx:
            t6_escalation_to_remedial(pe)
        self.assertIn("CR-006", str(ctx.exception))

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t6b_unchanged_by_cr_006(self, mock_sync):
        """CR-006 regression guard — T6b (CR-004) is the SOLE path to remedial.

        Setup a PE in submitted_awaiting_feedback (the source state of T6b),
        fire T6b, assert PE ends up in remedial_content_delivery.
        """
        from tap_lms.summer_program.state_machine import t6b_failed_feedback_to_remedial

        student = _ensure_student("CR006T6b")
        pe = _make_pe(
            self.batch_name, student, "CR006T6b",
            resolved_flow_state=STATE_SUBMITTED_AWAITING,
            journey_label=LABEL_SUBMITTED,
        )
        t6b_failed_feedback_to_remedial(pe, trigger_source="microservice")
        pe.reload()
        self.assertEqual(pe.resolved_flow_state, STATE_REMEDIAL_CONTENT)


# ════════════════════════════════════════════════════════════
# Task #27 — transition() L-011 race-safety
# ════════════════════════════════════════════════════════════

class TestTransitionRaceSafety(FrappeTestCase):
    """Pin the L-011 race-safety guarantee from task #27 (2026-05-22).

    Pre-fix, `transition()` called `pe.save(ignore_permissions=True)` which
    writes EVERY column on the row from the in-memory pe doc. If a
    concurrent handler (activity_points, quiz_points, feedback hook's
    atomic SQL bump) updated a column between the pe load and the save,
    the in-memory stale value silently overwrote that bump.

    Post-fix, `transition()` uses `frappe.db.set_value` with a targeted
    updates dict — touches ONLY the dirty fields. Concurrent bumps to
    other columns survive because they're not in the UPDATE SET clause.

    These tests simulate the race by:
      1. Loading a pe doc (in-memory stale snapshot).
      2. Atomically bumping a column via raw SQL (simulates the concurrent
         activity_points / quiz_points / feedback hook write).
      3. Calling `transition()` on the in-memory stale pe.
      4. Asserting the DB value matches the BUMPED value, not the stale one.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_transition_preserves_concurrent_total_points_bump(self, mock_sync):
        """Race: activity_points bumps total_points while a transition is
        loading pe. Post-fix the bump must survive."""
        student = _ensure_student("RACE_TP")
        pe = _make_pe(
            self.batch_name, student, "RACE_TP",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            total_points=10,
        )
        # Simulate the race: another handler atomically bumps total_points
        # to 13 AFTER pe was loaded (pe.total_points still reads 10 in memory).
        frappe.db.sql(
            'UPDATE "tabProgramEnrollment" SET total_points = 13 WHERE name = %s',
            (pe.name,),
        )
        self.assertEqual(pe.total_points, 10, "in-memory pe is stale by design")

        # Fire a transition that does NOT mention total_points in extra_updates.
        # Pre-fix this would clobber total_points back to 10. Post-fix it
        # leaves the column alone.
        from tap_lms.summer_program.state_machine import transition
        transition(
            pe, STATE_GRACE_WAITING,
            trigger_source="test",
            extra_updates={"journey_label": LABEL_GRACE_WINDOW},
            skip_glific=True,
        )

        # The concurrent bump must have survived.
        fresh_total = frappe.db.get_value("ProgramEnrollment", pe.name, "total_points")
        self.assertEqual(int(fresh_total), 13,
                         "Concurrent total_points bump must survive transition "
                         "(L-011 race-safety)")
        # And the transition's own updates were applied.
        fresh_state, fresh_label = frappe.db.get_value(
            "ProgramEnrollment", pe.name,
            ["resolved_flow_state", "journey_label"],
        )
        self.assertEqual(fresh_state, STATE_GRACE_WAITING)
        self.assertEqual(fresh_label, LABEL_GRACE_WINDOW)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_transition_preserves_concurrent_total_activity_points_bump(self, mock_sync):
        """Same race, total_activity_points column (owned by activity_points
        handler). Pre-fix would clobber; post-fix preserves."""
        student = _ensure_student("RACE_TAP")
        pe = _make_pe(
            self.batch_name, student, "RACE_TAP",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            total_activity_points=20,
        )
        frappe.db.sql(
            'UPDATE "tabProgramEnrollment" SET total_activity_points = 35 '
            'WHERE name = %s',
            (pe.name,),
        )
        from tap_lms.summer_program.state_machine import transition
        transition(
            pe, STATE_SUBMITTED_AWAITING,
            trigger_source="test",
            extra_updates={"journey_label": LABEL_SUBMITTED},
            skip_glific=True,
        )
        fresh = frappe.db.get_value(
            "ProgramEnrollment", pe.name, "total_activity_points",
        )
        self.assertEqual(int(fresh), 35,
                         "total_activity_points bump must survive transition")

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_transition_preserves_concurrent_weekly_video_done_flip(self, mock_sync):
        """weekly_video_done is flipped 0→1 by activity_points handler.
        If a transition races, the flip must survive (it's the gating
        signal for T19's streak/gem penalty branch — clobbering it would
        silently lose the gating signal)."""
        student = _ensure_student("RACE_WVD")
        pe = _make_pe(
            self.batch_name, student, "RACE_WVD",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            weekly_video_done=0,
        )
        frappe.db.sql(
            'UPDATE "tabProgramEnrollment" SET weekly_video_done = 1 '
            'WHERE name = %s',
            (pe.name,),
        )
        from tap_lms.summer_program.state_machine import transition
        transition(
            pe, STATE_NORMAL_ESCALATION,
            trigger_source="test",
            extra_updates={
                "current_escalation_step": 1,
                "current_escalation_type": "help_note_a",
            },
            skip_glific=True,
        )
        fresh = frappe.db.get_value(
            "ProgramEnrollment", pe.name, "weekly_video_done",
        )
        self.assertEqual(int(fresh), 1,
                         "weekly_video_done flip must survive concurrent "
                         "transition — it gates the T19 penalty branch")

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_transition_preserves_concurrent_submission_count_bump(self, mock_sync):
        """submission_count is owned by save_submission._try_claim_primary
        (atomic claim). A transition that doesn't mention it must not
        clobber the count."""
        student = _ensure_student("RACE_SC")
        pe = _make_pe(
            self.batch_name, student, "RACE_SC",
            resolved_flow_state=STATE_NORMAL_CONTENT,
        )
        pe.submission_count = 2
        pe.save(ignore_permissions=True)
        # Concurrent bump
        frappe.db.sql(
            'UPDATE "tabProgramEnrollment" SET submission_count = 3 '
            'WHERE name = %s',
            (pe.name,),
        )
        from tap_lms.summer_program.state_machine import transition
        transition(
            pe, STATE_GRACE_WAITING,
            trigger_source="test",
            extra_updates={"journey_label": LABEL_GRACE_WINDOW},
            skip_glific=True,
        )
        fresh = frappe.db.get_value(
            "ProgramEnrollment", pe.name, "submission_count",
        )
        self.assertEqual(int(fresh), 3,
                         "submission_count bump from _try_claim_primary "
                         "must survive transition")

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_transition_applies_all_extra_updates(self, mock_sync):
        """Regression guard: the targeted UPDATE must actually apply EVERY
        field in extra_updates (otherwise we lose the state transition).
        This is the boring happy-path test."""
        student = _ensure_student("APPLY_ALL")
        pe = _make_pe(
            self.batch_name, student, "APPLY_ALL",
            resolved_flow_state=STATE_NORMAL_CONTENT,
        )
        from tap_lms.summer_program.state_machine import transition
        transition(
            pe, STATE_SUBMITTED_AWAITING,
            trigger_source="test",
            extra_updates={
                "journey_label": LABEL_SUBMITTED,
                "current_streak": 7,
                "special_gems": 5,
                "weekly_submission_done": 1,
            },
            skip_glific=True,
        )
        fresh = frappe.db.get_value(
            "ProgramEnrollment", pe.name,
            ["resolved_flow_state", "journey_label", "current_streak",
             "special_gems", "weekly_submission_done"],
            as_dict=True,
        )
        self.assertEqual(fresh["resolved_flow_state"], STATE_SUBMITTED_AWAITING)
        self.assertEqual(fresh["journey_label"], LABEL_SUBMITTED)
        self.assertEqual(int(fresh["current_streak"]), 7)
        self.assertEqual(int(fresh["special_gems"]), 5)
        self.assertEqual(int(fresh["weekly_submission_done"]), 1)


# ════════════════════════════════════════════════════════════
# CR-008 (2026-05-23) — Lazy reset on first VideoClass of new week
# ════════════════════════════════════════════════════════════

def _make_video_class(suffix, points):
    """Create a VideoClass with given points value (for activity_points tests)."""
    video = frappe.new_doc("VideoClass")
    video.video_name = f"LazyResetTestVideo-{suffix}-{frappe.utils.random_string(4)}"
    video.duration = "5:00"
    video.points = points
    video.insert(ignore_permissions=True)
    return video.name


def _make_scl(student, video_id, stage_no=1, action="completed"):
    """Create a StudentContentLog pointing at the given VideoClass.
    Inserting an SCL with action='completed' fires the activity_points
    after_insert hook (per hooks.py) which calls handle_content_log →
    award_activity_points → atomic UPDATE on PE.
    """
    log = frappe.new_doc("StudentContentLog")
    log.student = student
    log.stage_no = stage_no
    log.content_type = "VideoClass"
    log.content_id = video_id
    log.action = action
    log.insert(ignore_permissions=True)
    return log.name


class TestLazyResetOnVideo(FrappeTestCase):
    """CR-008 (2026-05-23) — first-VideoClass-of-new-week lazy reset.

    T14 no longer zeros the gamification fields; the first VideoClass of
    the new week atomically resets weekly_*_points + sticky flags + quiz
    counters and bumps weekly_activity_points by the video's points — all
    in a single CASE-WHEN'd UPDATE gated on `weekly_video_done = 0`.

    Test matrix:
      - test_first_video_after_t14_resets_all_lazy_fields
      - test_second_video_same_week_does_not_reset
      - test_first_video_fresh_pe_is_a_noop_reset_plus_bump
      - test_submission_count_preserved_across_t14
      - test_quiz_completed_resets_on_first_video_of_new_week
      - test_bonus_quiz_points_reset_on_first_video_of_new_week
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_first_video_after_t14_resets_all_lazy_fields(self, _le, _es):
        """The core CR-008 scenario. PE in W2 with weekly_video_done=0
        (T14 just fired) carries W1 values in weekly_*_points. Watching
        W2's first VideoClass must atomically wipe all six lazy fields
        and bump weekly_activity_points by the video's points value."""
        student = _ensure_student("LZR1")
        pe = _make_pe(
            self.batch_name, student, "LZR1",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=2,
            weekly_video_done=0,           # post-T14 signal
            weekly_submission_done=1,       # W1's value, lazy
            # W1's preserved gamification values (CR-008 keeps them visible
            # through the inter-week gap)
            weekly_activity_points=15,
            weekly_quiz_points=8,
            weekly_submission_points=20,
        )
        # bonus_quiz_points and quiz_completed need direct set (not in _make_pe sig)
        frappe.db.set_value("ProgramEnrollment", pe.name, {
            "bonus_quiz_points": 5,
            "quiz_completed": 1,
        })

        # W2 video worth 12 points
        video_id = _make_video_class("W2V1", 12)
        _make_scl(student, video_id, stage_no=2)

        # Reload — the lazy-reset CASE WHEN should have wiped W1 values
        # and bumped weekly_activity_points = 12.
        pe.reload()
        self.assertEqual(pe.weekly_activity_points, 12,
                         "wiped from 15 then bumped to V2=12")
        self.assertEqual(pe.weekly_quiz_points, 0, "lazy reset")
        self.assertEqual(pe.weekly_submission_points, 0, "lazy reset")
        self.assertEqual(pe.bonus_quiz_points, 0, "lazy reset")
        self.assertEqual(pe.weekly_submission_done, 0, "lazy reset")
        self.assertEqual(pe.quiz_completed, 0, "lazy reset")
        self.assertEqual(pe.weekly_video_done, 1,
                         "trigger signal flipped to 1; same-week subsequent "
                         "videos bypass the reset gate")

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_second_video_same_week_does_not_reset(self, _le, _es):
        """Within the same week (weekly_video_done already = 1), a second
        VideoClass just bumps weekly_activity_points. Other weekly fields
        and sticky flags MUST NOT be reset (regression guard against the
        CASE WHEN firing twice in the same week)."""
        student = _ensure_student("LZR2")
        pe = _make_pe(
            self.batch_name, student, "LZR2",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=1,
            weekly_video_done=1,           # already watched W1's first video
            weekly_submission_done=1,
            weekly_activity_points=15,     # first video already bumped this
            weekly_quiz_points=8,
            weekly_submission_points=20,
        )
        frappe.db.set_value("ProgramEnrollment", pe.name, {
            "bonus_quiz_points": 5,
            "quiz_completed": 1,
        })

        video_id = _make_video_class("W1V2", 7)
        _make_scl(student, video_id, stage_no=1)

        pe.reload()
        # weekly_activity_points bumped by 7 (15 + 7 = 22)
        self.assertEqual(pe.weekly_activity_points, 22,
                         "same-week bump: 15 + 7")
        # Other lazy fields preserved
        self.assertEqual(pe.weekly_quiz_points, 8, "preserved (same week)")
        self.assertEqual(pe.weekly_submission_points, 20, "preserved")
        self.assertEqual(pe.bonus_quiz_points, 5, "preserved")
        self.assertEqual(pe.weekly_submission_done, 1, "preserved")
        self.assertEqual(pe.quiz_completed, 1, "preserved")
        self.assertEqual(pe.weekly_video_done, 1, "stays 1")

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_first_video_fresh_pe_is_a_noop_reset_plus_bump(self, _le, _es):
        """Fresh PE just enrolled: weekly_video_done=0, all weekly_*=0.
        First video fires the lazy reset gate, but the resets are no-ops
        (already 0) and weekly_activity_points gets bumped from 0 to V1."""
        student = _ensure_student("LZR3")
        pe = _make_pe(
            self.batch_name, student, "LZR3",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=1,
            weekly_video_done=0,
            # All other weekly fields default to 0 in _make_pe
        )

        video_id = _make_video_class("W1V1", 10)
        _make_scl(student, video_id, stage_no=1)

        pe.reload()
        self.assertEqual(pe.weekly_activity_points, 10)
        self.assertEqual(pe.weekly_quiz_points, 0)
        self.assertEqual(pe.weekly_submission_points, 0)
        self.assertEqual(pe.weekly_video_done, 1)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_submission_count_preserved_across_t14(self, _sync):
        """CR-008: submission_count is a lifetime counter — T14 does NOT
        reset it. A student who submits across multiple weeks ends each
        week with a higher submission_count."""
        student = _ensure_student("LZR4")
        pe = _make_pe(
            self.batch_name, student, "LZR4",
            resolved_flow_state=STATE_WEEK_COMPLETED,
            current_week=1,
            weekly_video_done=1,
            weekly_submission_done=1,
        )
        # Set submission_count to 2 (simulating 2 prior submissions)
        frappe.db.set_value("ProgramEnrollment", pe.name, "submission_count", 2)
        pe.reload()
        self.assertEqual(pe.submission_count, 2)

        t14_week_advance(pe, new_week=2)

        pe.reload()
        self.assertEqual(pe.submission_count, 2,
                         "lifetime counter: NOT reset at T14 (CR-008)")
        self.assertEqual(pe.current_week, 2)

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_quiz_completed_flips_to_zero_on_first_video_of_new_week(self, _le, _es):
        """quiz_completed lazy-resets to 0 on the first video of a new week
        (was 1 from the prior week's quiz finish)."""
        student = _ensure_student("LZR5")
        pe = _make_pe(
            self.batch_name, student, "LZR5",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=2,
            weekly_video_done=0,
        )
        frappe.db.set_value("ProgramEnrollment", pe.name, "quiz_completed", 1)

        video_id = _make_video_class("LZR5V", 10)
        _make_scl(student, video_id, stage_no=2)

        pe.reload()
        self.assertEqual(pe.quiz_completed, 0)

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_bonus_quiz_points_reset_on_first_video_of_new_week(self, _le, _es):
        """bonus_quiz_points lazy-resets to 0 on first video of new week
        (was N from prior week's bonus awards)."""
        student = _ensure_student("LZR6")
        pe = _make_pe(
            self.batch_name, student, "LZR6",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=2,
            weekly_video_done=0,
        )
        frappe.db.set_value("ProgramEnrollment", pe.name, "bonus_quiz_points", 7)

        video_id = _make_video_class("LZR6V", 10)
        _make_scl(student, video_id, stage_no=2)

        pe.reload()
        self.assertEqual(pe.bonus_quiz_points, 0)


# ════════════════════════════════════════════════════════════
# CR-009 (2026-05-23) — backend-driven escalation arming on first video
# ════════════════════════════════════════════════════════════

class TestEscalationArmingOnFirstVideo(FrappeTestCase):
    """CR-009: when a student watches the week's first VideoClass,
    activity_points.award_activity_points must arm
    next_action_at = NOW + first_step.hours_after_previous,
    next_action_type = 'escalation' — independent of any Glific callback.

    Closes the 'watched-but-no-submission' gap where 4 PEs in
    palv2-test-BT52231 had weekly_video_done=1, weekly_submission_done=0,
    next_action_at=None, current_escalation_step=0.

    Idempotency gates:
      (a) is_first_video_of_week — subsequent videos same week don't re-arm
      (b) current_escalation_step == 0 — don't reset in-flight chain
      (c) next_action_type != 'escalation' — defensive
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    @patch("tap_lms.summer_program.activity_points._get_escalation_steps", create=True,
           new=None)
    def test_first_video_arms_escalation(self, _le, _es):
        """First VideoClass of the week → next_action_at set to
        NOW + first_step.hours_after_previous, next_action_type='escalation'."""
        student = _ensure_student("ESC1")
        pe = _make_pe(
            self.batch_name, student, "ESC1",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=1,
            weekly_video_done=0,
        )

        # Stub _get_escalation_steps via patch.object — it lives in
        # student_progression_sp; activity_points imports it lazily.
        fake_steps = [
            {"escalation_order": 1, "escalation_type": "help_note_a",
             "hours_after_previous": 24, "points_awarded": 10},
            {"escalation_order": 2, "escalation_type": "help_note_b",
             "hours_after_previous": 24, "points_awarded": 7},
        ]
        with patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps",
                   return_value=fake_steps):
            video_id = _make_video_class("ESC1V", 10)
            _make_scl(student, video_id, stage_no=1)

        pe.reload()
        self.assertEqual(pe.next_action_type, "escalation",
                         "first video must arm escalation")
        self.assertIsNotNone(pe.next_action_at,
                             "first video must set next_action_at")
        # next_action_at should be ~24 hours in the future
        from frappe.utils import now_datetime, get_datetime
        delta_hours = (get_datetime(pe.next_action_at) - now_datetime()).total_seconds() / 3600
        self.assertAlmostEqual(delta_hours, 24, delta=0.5,
                               msg=f"next_action_at should be ~24h ahead, got {delta_hours:.2f}h")

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_second_video_same_week_does_not_re_arm(self, _le, _es):
        """A second VideoClass in the same week (weekly_video_done=1
        already) must NOT touch next_action_at. The first video already
        armed it."""
        from frappe.utils import now_datetime, add_to_date, get_datetime
        student = _ensure_student("ESC2")
        pe = _make_pe(
            self.batch_name, student, "ESC2",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=1,
            weekly_video_done=1,    # already watched first video
        )
        # Pre-armed: next_action_at set 12 hours from now (the post-first-video state)
        original_fire = add_to_date(now_datetime(), hours=12)
        frappe.db.set_value("ProgramEnrollment", pe.name, {
            "next_action_at": original_fire,
            "next_action_type": "escalation",
        })

        # Now watch the second video — should NOT re-arm
        video_id = _make_video_class("ESC2V", 10)
        _make_scl(student, video_id, stage_no=1)

        pe.reload()
        self.assertEqual(pe.next_action_type, "escalation")
        # next_action_at should still be the original ~12h ahead, not reset to ~24h
        delta_hours = (get_datetime(pe.next_action_at) - now_datetime()).total_seconds() / 3600
        self.assertLess(delta_hours, 13,
                        f"second video must NOT push next_action_at out — "
                        f"original was 12h, got {delta_hours:.2f}h")

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_first_video_does_not_re_arm_when_escalation_in_flight(self, _le, _es):
        """If current_escalation_step > 0 (escalation chain already running),
        a video watch must NOT reset the schedule."""
        from frappe.utils import now_datetime, add_to_date, get_datetime
        student = _ensure_student("ESC3")
        pe = _make_pe(
            self.batch_name, student, "ESC3",
            resolved_flow_state=STATE_NORMAL_ESCALATION,
            current_week=1,
            weekly_video_done=0,    # somehow back to 0 (e.g., new week not yet engaged)
        )
        # Escalation in flight — step 1 already fired
        original_fire = add_to_date(now_datetime(), hours=6)
        frappe.db.set_value("ProgramEnrollment", pe.name, {
            "current_escalation_step": 1,
            "current_escalation_type": "help_note_a",
            "next_action_at": original_fire,
            "next_action_type": "escalation",
        })

        video_id = _make_video_class("ESC3V", 10)
        _make_scl(student, video_id, stage_no=1)

        pe.reload()
        # next_action_at should still be the original ~6h ahead, not 24h
        delta_hours = (get_datetime(pe.next_action_at) - now_datetime()).total_seconds() / 3600
        self.assertLess(delta_hours, 7,
                        f"escalation-in-flight: video must NOT reset next_action_at — "
                        f"original was 6h, got {delta_hours:.2f}h")
        # current_escalation_step preserved
        self.assertEqual(pe.current_escalation_step, 1)

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_no_escalation_steps_configured_no_arm(self, _le, _es):
        """If the student's archetype/arm/path has no escalation_steps
        (returns empty list), don't arm next_action_at. Logger.info note
        but no error."""
        student = _ensure_student("ESC4")
        pe = _make_pe(
            self.batch_name, student, "ESC4",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=1,
            weekly_video_done=0,
        )

        # Force _get_escalation_steps to return empty
        with patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps",
                   return_value=[]):
            video_id = _make_video_class("ESC4V", 10)
            _make_scl(student, video_id, stage_no=1)

        pe.reload()
        # No escalation steps → no arming
        self.assertIsNone(pe.next_action_at)
        self.assertIn(pe.next_action_type, ("", None))


# ════════════════════════════════════════════════════════════
# CR-009 follow-up (task #76) — T2/T8 must re-arm next_action_at
# Code review gap F1/F2 (task #78) — regression test for chain stuck at step 1
# ════════════════════════════════════════════════════════════

class TestEscalationChainProgression(FrappeTestCase):
    """Pin the bug discovered via production diagnostic on 2026-05-23:
    4 PEs in palv2-test-BT52231 (h2i84o5mki, kgn2nc9gt5, kgn4pn2ddn,
    kuut1ssmi7) all reached current_escalation_step=1 but next_action_at
    was NULL → dispatcher never picked them up for step 2.

    Root cause: dispatcher's atomic claim SQL sets next_action_at=NULL on
    row pickup. T4/T10 (subsequent escalation steps) re-arm next_action_at;
    T2/T8 (first escalation step) did NOT — the chain stuck at step 1
    forever.

    Fix: T2 and T8 now take a next_hours parameter and set next_action_at
    in their updates dict, mirroring T4/T10's pattern.

    These tests pin (a) T2 re-arms after firing step 1, (b) T8 same for
    Remedial, (c) end-to-end the chain progresses to step 2 when the
    dispatcher runs again.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t2_re_arms_next_action_at(self, _sync):
        """T2 must set next_action_at = NOW + next_hours so the dispatcher
        picks up step 2. Pre-fix (task #76), the chain stuck at step 1
        forever — production proof: 4 PEs in palv2-test-BT52231 stuck."""
        from tap_lms.summer_program.state_machine import t2_start_escalation
        from frappe.utils import now_datetime, get_datetime

        student = _ensure_student("T2ARM")
        pe = _make_pe(
            self.batch_name, student, "T2ARM",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=1,
        )

        t2_start_escalation(
            pe, step_number=1, escalation_type="help_note_a",
            next_hours=24, trigger_source="test",
        )

        pe.reload()
        self.assertEqual(pe.resolved_flow_state, STATE_NORMAL_ESCALATION)
        self.assertEqual(pe.current_escalation_step, 1)
        self.assertEqual(pe.current_escalation_type, "help_note_a")
        self.assertEqual(pe.next_action_type, "escalation",
                         "task #76: T2 MUST re-arm next_action_type")
        self.assertIsNotNone(pe.next_action_at,
                             "task #76: T2 MUST re-arm next_action_at — "
                             "without this the dispatcher never picks up step 2")
        delta_h = (get_datetime(pe.next_action_at) - now_datetime()).total_seconds() / 3600
        self.assertAlmostEqual(
            delta_h, 24, delta=0.5,
            msg=f"T2 should schedule step 2 ~24h ahead, got {delta_h:.2f}h",
        )

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    def test_t8_re_arms_next_action_at(self, _sync):
        """T8 (Remedial counterpart of T2) — same regression guard."""
        from tap_lms.summer_program.state_machine import t8_start_remedial_escalation
        from frappe.utils import now_datetime, get_datetime
        from tap_lms.summer_program.constants import STATE_REMEDIAL_CONTENT, PATH_REMEDIAL

        student = _ensure_student("T8ARM")
        pe = _make_pe(
            self.batch_name, student, "T8ARM",
            resolved_flow_state=STATE_REMEDIAL_CONTENT,
            current_week=2,
        )
        # _make_pe sets current_path=Core; override for Remedial test
        frappe.db.set_value("ProgramEnrollment", pe.name,
                            "current_path", PATH_REMEDIAL)
        pe.reload()

        t8_start_remedial_escalation(
            pe, step_number=1, escalation_type="help_note_a",
            next_hours=12, trigger_source="test",
        )

        pe.reload()
        self.assertEqual(pe.current_escalation_step, 1)
        self.assertEqual(pe.next_action_type, "escalation",
                         "task #76: T8 MUST re-arm next_action_type")
        self.assertIsNotNone(pe.next_action_at)
        delta_h = (get_datetime(pe.next_action_at) - now_datetime()).total_seconds() / 3600
        self.assertAlmostEqual(delta_h, 12, delta=0.5)

    @patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.pe_dispatcher._trigger_flow")
    @patch("tap_lms.summer_program.pe_dispatcher._get_flow_id",
           return_value="fake-flow-id")
    def test_full_escalation_chain_progresses_through_all_steps(
        self, _flow_id, _trigger, _sync
    ):
        """End-to-end regression: drive a PE through every escalation step
        and verify it progresses. This test would have caught task #76's
        'chain stuck at step 1' bug — pre-fix, the dispatcher's second
        run would have done nothing because T2 didn't re-arm next_action_at.

        Simulates the dispatcher tick-by-tick by calling handle_escalation
        directly with a synthetic pe_row dict (matching the SQL SELECT
        shape) for each step.
        """
        from tap_lms.summer_program.pe_dispatcher import handle_escalation
        from tap_lms.summer_program.constants import STATE_GRACE_WAITING

        student = _ensure_student("CHAIN")
        pe = _make_pe(
            self.batch_name, student, "CHAIN",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=1,
            current_streak=0,
            special_gems=0,
        )
        # Stub the escalation step config — 3 steps with predictable hours.
        # The dispatcher resolves these via _get_escalation_steps_for_pe.
        fake_steps = [
            {"escalation_order": 1, "escalation_type": "help_note_a",
             "hours_after_previous": 24, "points_awarded": 10, "is_active": 1},
            {"escalation_order": 2, "escalation_type": "help_note_b",
             "hours_after_previous": 12, "points_awarded": 7, "is_active": 1},
            {"escalation_order": 3, "escalation_type": "voice_note",
             "hours_after_previous": 6, "points_awarded": 4, "is_active": 1},
        ]

        with patch("tap_lms.summer_program.pe_dispatcher._get_escalation_steps_for_pe",
                   return_value=fake_steps):
            # Round 1: state=normal_content_delivery → T2 fires step 1
            pe_row = frappe._dict({
                "name": pe.name,
                "batch": pe.batch,
                "next_action_type": "escalation",
                "journey_label": pe.journey_label,
            })
            handle_escalation(pe_row)
            pe.reload()
            self.assertEqual(pe.current_escalation_step, 1)
            self.assertEqual(pe.resolved_flow_state, STATE_NORMAL_ESCALATION)
            self.assertEqual(pe.next_action_type, "escalation",
                             "step 1 → step 2 chain: T2 must re-arm")
            self.assertIsNotNone(pe.next_action_at)

            # Round 2: state=normal_escalation → T4 fires step 2
            pe_row.journey_label = pe.journey_label
            handle_escalation(pe_row)
            pe.reload()
            self.assertEqual(pe.current_escalation_step, 2,
                             "T4 must advance step 1 → step 2")
            self.assertEqual(pe.current_escalation_type, "help_note_b")
            self.assertEqual(pe.next_action_type, "escalation",
                             "step 2 → step 3 chain: T4 must re-arm")

            # Round 3: state=normal_escalation → T4 fires step 3
            handle_escalation(pe_row)
            pe.reload()
            self.assertEqual(pe.current_escalation_step, 3)
            self.assertEqual(pe.current_escalation_type, "voice_note")

            # Round 4: chain exhausted (next_step=4 > len(steps)=3) → T5 grace
            handle_escalation(pe_row)
            pe.reload()
            self.assertEqual(pe.resolved_flow_state, STATE_GRACE_WAITING,
                             "exhausted chain must route to grace_waiting")

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    @patch("tap_lms.summer_program.activity_points.log_event")
    def test_watched_but_no_submission_now_gets_escalation(self, _le, _es):
        """The production scenario that motivated CR-009 + task #76: a
        student watches the first video of a week but doesn't submit.
        Pre-fix they were stuck — no escalation, no nudges, just grace
        clock ticking down. Post-fix they get escalation step 1 armed.

        This test reproduces the EXACT state of the 4 stuck PEs and
        verifies the new code arms next_action_at correctly.
        """
        student = _ensure_student("GAP")
        pe = _make_pe(
            self.batch_name, student, "GAP",
            resolved_flow_state=STATE_NORMAL_CONTENT,
            current_week=1,
            weekly_video_done=0,    # haven't watched yet
            weekly_submission_done=0,
        )

        fake_steps = [
            {"escalation_order": 1, "escalation_type": "help_note_a",
             "hours_after_previous": 24, "points_awarded": 10},
        ]

        with patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps",
                   return_value=fake_steps):
            video_id = _make_video_class("GAPV", 10)
            _make_scl(student, video_id, stage_no=1)

        pe.reload()
        # Production stuck state was: weekly_video_done=1, weekly_submission_done=0,
        # next_action_at=None. Post-fix it must be:
        self.assertEqual(pe.weekly_video_done, 1,
                         "video flip happens via activity_points atomic SQL")
        self.assertEqual(pe.weekly_submission_done, 0)
        self.assertIsNotNone(pe.next_action_at,
                             "CR-009 / task #76: video watch arms next_action_at")
        self.assertEqual(pe.next_action_type, "escalation")
