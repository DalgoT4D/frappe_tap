"""Plain-Python tests for weekly rollup math (post-CR-011).

Run without bench or a Frappe server:

    python3 -m unittest tap_lms.summer_program.tests.test_weekly_rollup_local -v

CR-011 (2026-05-25) — Eager total_* updates.

  Pre-CR-011, per-event handlers only bumped weekly_* columns and T14 rolled
  weekly→total at week advance. Mid-week state was incoherent (e.g., a
  student saw weekly_quiz_points=3 but total_quiz_points=0 and
  total_points=0 on Glific).

  Post-CR-011 the per-event handlers (quiz_points.py / activity_points.py /
  feedback_consumer_hook.py) update total_* atomically with weekly_*, so
  T14 no longer needs to roll the totals — it just resets weekly_*.

Invariant — `stream_sum == total_points` now holds at ALL TIMES (not just
post-T14), because totals are eager. `calculate_week_advance_rollup` returns
the input total_* values unchanged (pass-through); only streak/gems may
change here (via the no-submission penalty branch).
"""
import unittest

from tap_lms.summer_program.weekly_rollup import calculate_week_advance_rollup


class TestWeeklyRollupLocal(unittest.TestCase):
    """CR-011 invariant: streams are summed eagerly per-event into total_*,
    so calculate_week_advance_rollup returns total_* untouched (pass-through).
    The invariant `total_activity_points + total_quiz_points + total_submission_points
    == total_points` (modulo bonus_quiz_points which is independent of the
    stream sum per quiz_points.award_bonus_quiz_points) is enforced AT ALL
    TIMES by the per-event handlers, not just at week advance.
    """

    def test_activity_and_submission_week_each_count_once(self):
        """Single week: weekly_activity=10, weekly_submission=25.
        Post-CR-011: totals are pass-through; T14 doesn't mutate them.
        The per-event handlers already updated total_* eagerly when those
        weekly_* values were earned.
        """
        result = calculate_week_advance_rollup({
            "total_activity_points": 0,
            "total_submission_points": 0,
            "total_quiz_points": 0,
            "total_points": 0,
            "weekly_activity_points": 10,
            "weekly_submission_points": 25,
            "weekly_quiz_points": 0,
            "bonus_quiz_points": 0,
            "current_streak": 1,
            "special_gems": 1,
            "weekly_video_done": 1,
            "weekly_submission_done": 1,
        })

        # CR-011: pass-through. The per-event handlers updated total_* when
        # the weekly_* points were earned; T14 leaves them alone.
        self.assertEqual(result["total_activity_points"], 0)
        self.assertEqual(result["total_submission_points"], 0)
        self.assertEqual(result["total_quiz_points"], 0)
        self.assertEqual(result["total_points"], 0)
        self.assertEqual(result["current_streak"], 1)
        self.assertEqual(result["special_gems"], 1)

    def test_activity_only_week_adds_to_existing_activity_total(self):
        """Second week with activity only (no submission this week).
        Post-CR-011: totals come through unchanged from input.
        """
        result = calculate_week_advance_rollup({
            "total_activity_points": 10,
            "total_submission_points": 25,
            "total_quiz_points": 0,
            "total_points": 35,
            "weekly_activity_points": 10,
            "weekly_submission_points": 0,
            "weekly_quiz_points": 0,
            "bonus_quiz_points": 0,
            "current_streak": 2,
            "special_gems": 2,
            "weekly_video_done": 1,
            "weekly_submission_done": 0,
        })

        # CR-011: pass-through. Inputs match what the per-event handlers
        # already wrote when the points were earned.
        self.assertEqual(result["total_activity_points"], 10)
        self.assertEqual(result["total_submission_points"], 25)
        self.assertEqual(result["total_points"], 35)
        # Penalty branch: weekly_video_done=1, weekly_submission_done=0
        self.assertEqual(result["current_streak"], 0)
        self.assertEqual(result["special_gems"], 1)

    def test_activity_and_submission_week_adds_both_to_existing_totals(self):
        """Second week with both activity AND submission.
        Post-CR-011: totals are pass-through.
        """
        result = calculate_week_advance_rollup({
            "total_activity_points": 10,
            "total_submission_points": 25,
            "total_quiz_points": 0,
            "total_points": 35,
            "weekly_activity_points": 10,
            "weekly_submission_points": 25,
            "weekly_quiz_points": 0,
            "bonus_quiz_points": 0,
            "current_streak": 2,
            "special_gems": 2,
            "weekly_video_done": 1,
            "weekly_submission_done": 1,
        })

        # CR-011: pass-through.
        self.assertEqual(result["total_activity_points"], 10)
        self.assertEqual(result["total_submission_points"], 25)
        self.assertEqual(result["total_points"], 35)
        self.assertEqual(result["current_streak"], 2)
        self.assertEqual(result["special_gems"], 2)

    def test_quiz_and_bonus_roll_into_total_points(self):
        """Quiz week + bonus points. Post-CR-011 totals are passthrough.
        The pre-CR-011 claim that "total_points exceeds stream sum by bonus"
        no longer applies — the per-event quiz handler updated total_quiz_points
        and total_points already; bonus stays independent of the stream sum.
        """
        result = calculate_week_advance_rollup({
            "total_activity_points": 20,
            "total_submission_points": 50,
            "total_quiz_points": 2,
            "total_points": 72,
            "weekly_activity_points": 0,
            "weekly_submission_points": 0,
            "weekly_quiz_points": 8,
            "bonus_quiz_points": 3,
            "current_streak": 2,
            "special_gems": 2,
            "weekly_video_done": 0,
            "weekly_submission_done": 0,
        })

        # CR-011: pass-through.
        self.assertEqual(result["total_activity_points"], 20)
        self.assertEqual(result["total_submission_points"], 50)
        self.assertEqual(result["total_quiz_points"], 2)
        self.assertEqual(result["total_points"], 72)
        # weekly_video_done=0 → no penalty branch
        self.assertEqual(result["current_streak"], 2)
        self.assertEqual(result["special_gems"], 2)

    def test_zero_week_no_op(self):
        """Empty week (no activity, no submission, no quiz, no bonus).
        Streak/gem penalty branch doesn't fire because weekly_video_done=0.
        Post-CR-011 totals pass-through (unchanged here too — this test
        already had matching expectations).
        """
        result = calculate_week_advance_rollup({
            "total_activity_points": 100,
            "total_submission_points": 50,
            "total_quiz_points": 25,
            "total_points": 175,
            "weekly_activity_points": 0,
            "weekly_submission_points": 0,
            "weekly_quiz_points": 0,
            "bonus_quiz_points": 0,
            "current_streak": 3,
            "special_gems": 4,
            "weekly_video_done": 0,
            "weekly_submission_done": 0,
        })
        self.assertEqual(result["total_activity_points"], 100)
        self.assertEqual(result["total_submission_points"], 50)
        self.assertEqual(result["total_quiz_points"], 25)
        self.assertEqual(result["total_points"], 175)
        self.assertEqual(result["current_streak"], 3)
        self.assertEqual(result["special_gems"], 4)

    def test_penalty_branch_floors_gems_at_zero(self):
        """Gems can't go negative even if penalty fires when gems=0.
        Post-CR-011: totals stay 0 (pass-through from input).
        """
        result = calculate_week_advance_rollup({
            "total_activity_points": 10,
            "total_submission_points": 0,
            "total_quiz_points": 0,
            "total_points": 10,
            "weekly_activity_points": 0,
            "weekly_submission_points": 0,
            "weekly_quiz_points": 0,
            "bonus_quiz_points": 0,
            "current_streak": 0,
            "special_gems": 0,
            "weekly_video_done": 1,
            "weekly_submission_done": 0,
        })
        self.assertEqual(result["current_streak"], 0)
        self.assertEqual(result["special_gems"], 0,
                         "gems must floor at 0, never go negative")
        # CR-011 pass-through sanity:
        self.assertEqual(result["total_activity_points"], 10)
        self.assertEqual(result["total_submission_points"], 0)
        self.assertEqual(result["total_quiz_points"], 0)
        self.assertEqual(result["total_points"], 10)


if __name__ == "__main__":
    unittest.main()
