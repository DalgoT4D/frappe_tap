"""
Tests for CR-002 v2 quiz-points handler.

Per-question independent award model:
  correct → QuizQuestion.points
  wrong   → QuizQuestion.failed_points

Weekly quiz points always add each completed attempt's full earned score
(effort, not latest). Cumulative quiz totals roll up on week advance.

Idempotency: attempt.points_earned > 0 is the write-once anchor (P-005).

Tests in this file:
  1. test_quiz_attempt_awards_per_question_correct
  2. test_quiz_attempt_awards_per_question_wrong_failed_points
  3. test_quiz_idempotent_via_points_earned
  4. test_quiz_retake_higher_score_delta_to_total
  5. test_quiz_retake_lower_score_negative_delta_to_total
  6. test_quiz_zero_earned_no_pe_update
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime
from unittest.mock import patch

from tap_lms.summer_program.tests.factories import make_batch
from tap_lms.summer_program.constants import (
    LABEL_CONTENT_DELIVERED,
    PATH_CORE,
    PROGRAM_ACTIVE,
    STATE_NORMAL_CONTENT,
)
from tap_lms.summer_program.quiz_points import (
    compute_quiz_points,
    handle_attempt_update,
)


# ════════════════════════════════════════════════════════════
# Test fixtures
# ════════════════════════════════════════════════════════════

def _ensure_batch():
    # Delegates to the shared factory (L-037) so this fixture inherits future
    # mandatory-field additions instead of breaking with MandatoryError.
    return make_batch(label="QuizPointsTestBatch", batch_id="QPT01")


def _ensure_student(suffix):
    name = frappe.get_value("Student", {"phone": f"+9999200{suffix}"}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"QuizPointsTestStudent{suffix}"
    s.phone = f"+9999200{suffix}"
    s.glific_id = f"glific-qzpts-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(batch_name, student_name, suffix):
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-QZPTS-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-qzpts-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.journey_label = LABEL_CONTENT_DELIVERED
    pe.current_path = PATH_CORE
    pe.current_week = 1
    pe.total_points = 0
    pe.total_quiz_points = 0
    pe.weekly_quiz_points = 0
    pe.insert(ignore_permissions=True)
    return pe.name


def _make_quiz_question(suffix, points, failed_points):
    """Insert a QuizQuestion row."""
    q = frappe.new_doc("QuizQuestion")
    q.question_text = f"Quiz question {suffix}?"
    q.points = points
    # `failed_points` is added by T-CR002v2-01; default 0 if absent.
    setattr(q, "failed_points", failed_points)
    q.insert(ignore_permissions=True)
    return q.name


def _make_quiz():
    quiz = frappe.new_doc("Quiz")
    quiz.quiz_name = f"QuizPointsTestQuiz-{frappe.utils.random_string(6)}"
    quiz.insert(ignore_permissions=True)
    return quiz.name


def _make_attempt(student, quiz, answers, completed=True):
    """Insert a StudentQuizAttempt with the provided answers.

    answers: list of dicts with keys (question, is_correct).
    """
    attempt = frappe.new_doc("StudentQuizAttempt")
    attempt.student = student
    attempt.quiz = quiz
    attempt.status = "Completed" if completed else "InProgress"
    attempt.total_questions = len(answers)
    attempt.attempt_number = 1
    attempt.started_at = now_datetime()
    if completed:
        attempt.completed_at = now_datetime()
    for i, a in enumerate(answers):
        attempt.append("answers", {
            "question_index": i,
            "question": a["question"],
            "is_correct": 1 if a["is_correct"] else 0,
        })
    attempt.insert(ignore_permissions=True)
    return attempt


# ════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════

class TestQuizPoints(FrappeTestCase):
    """CR-002 v2 §Test Plan — quiz-points handler regression coverage."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.quiz_points._enqueue_contact_field_sync")
    def test_quiz_attempt_awards_per_question_correct(self, mock_sync):
        """Two correct answers (points=8, points=10) → earned = 18."""
        student = _ensure_student("01")
        pe_name = _make_pe(self.batch_name, student, "01")
        quiz = _make_quiz()
        q1 = _make_quiz_question("01a", 8, 2)
        q2 = _make_quiz_question("01b", 10, 3)
        attempt = _make_attempt(student, quiz, [
            {"question": q1, "is_correct": True},
            {"question": q2, "is_correct": True},
        ])

        handle_attempt_update(attempt)

        attempt.reload()
        self.assertEqual(attempt.points_earned, 18)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_quiz_points, 0)
        self.assertEqual(pe.weekly_quiz_points, 18)
        self.assertEqual(pe.total_points, 0)

    @patch("tap_lms.summer_program.quiz_points._enqueue_contact_field_sync")
    def test_quiz_attempt_awards_per_question_wrong_failed_points(self, mock_sync):
        """Two wrong answers (failed_points=2, failed_points=3) → earned = 5.
        Verifies per-question independence of attempt-level pass/fail."""
        student = _ensure_student("02")
        pe_name = _make_pe(self.batch_name, student, "02")
        quiz = _make_quiz()
        q1 = _make_quiz_question("02a", 8, 2)
        q2 = _make_quiz_question("02b", 10, 3)
        attempt = _make_attempt(student, quiz, [
            {"question": q1, "is_correct": False},
            {"question": q2, "is_correct": False},
        ])
        # Even though attempt-level "passed" is false, partial credit applies.
        attempt.passed = 0
        attempt.save(ignore_permissions=True)

        handle_attempt_update(attempt)

        attempt.reload()
        self.assertEqual(attempt.points_earned, 5,
                         "Wrong answers award failed_points (2+3=5)")

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_quiz_points, 0)
        self.assertEqual(pe.weekly_quiz_points, 5)
        self.assertEqual(pe.total_points, 0)

    @patch("tap_lms.summer_program.quiz_points._enqueue_contact_field_sync")
    def test_quiz_idempotent_via_points_earned(self, mock_sync):
        """Re-running the handler on a completed attempt is a no-op:
        sees points_earned > 0 and returns. PE counters do not double-bump."""
        student = _ensure_student("03")
        pe_name = _make_pe(self.batch_name, student, "03")
        quiz = _make_quiz()
        q1 = _make_quiz_question("03", 10, 0)
        attempt = _make_attempt(student, quiz, [
            {"question": q1, "is_correct": True},
        ])

        handle_attempt_update(attempt)
        attempt.reload()
        self.assertEqual(attempt.points_earned, 10)

        # Second call should be a no-op.
        handle_attempt_update(attempt)
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_quiz_points, 0,
                         "Re-running handler must not double-bump")
        self.assertEqual(pe.weekly_quiz_points, 10)
        self.assertEqual(pe.total_points, 0)

    @patch("tap_lms.summer_program.quiz_points._enqueue_contact_field_sync")
    def test_quiz_retake_higher_score_delta_to_total(self, mock_sync):
        """Attempt 1 earns 5, attempt 2 earns 8 in same week:
        weekly += full new earned = +8 (so total weekly is 5 + 8 = 13).
        Cumulative totals roll up at week advance."""
        student = _ensure_student("04")
        pe_name = _make_pe(self.batch_name, student, "04")
        quiz = _make_quiz()
        q1 = _make_quiz_question("04a", 5, 0)
        q2 = _make_quiz_question("04b", 8, 0)

        # Attempt 1: one correct → 5 earned.
        attempt1 = _make_attempt(student, quiz, [
            {"question": q1, "is_correct": True},
        ])
        handle_attempt_update(attempt1)
        # Allow some sequencing on completed_at — set attempt2's later.

        # Attempt 2: one correct different question → 8 earned.
        attempt2 = _make_attempt(student, quiz, [
            {"question": q2, "is_correct": True},
        ])
        # Force chronological order on completed_at so delta query picks attempt1
        # as the prior-latest.
        frappe.db.set_value(
            "StudentQuizAttempt", attempt1.name,
            "completed_at", "2026-05-12 09:00:00",
            update_modified=False,
        )
        frappe.db.set_value(
            "StudentQuizAttempt", attempt2.name,
            "completed_at", "2026-05-12 10:00:00",
            update_modified=False,
        )
        # Re-fetch attempt2 to get fresh state for the handler call.
        attempt2.reload()
        attempt2.points_earned = 0  # ensure the idempotency anchor is not yet set
        attempt2.save(ignore_permissions=True)

        handle_attempt_update(attempt2)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_quiz_points, 0,
                         "Cumulative total rolls up at week advance")
        self.assertEqual(pe.total_points, 0)
        # Weekly: 5 + 8 = 13 (effort, not latest)
        self.assertEqual(pe.weekly_quiz_points, 13,
                         "Weekly always adds full new earned (5 + 8 = 13)")

    @patch("tap_lms.summer_program.quiz_points._enqueue_contact_field_sync")
    def test_quiz_retake_lower_score_negative_delta_to_total(self, mock_sync):
        """Attempt 1 earns 8, attempt 2 earns 5 in same week:
        weekly += 5 (effort). Cumulative totals roll up at week advance."""
        student = _ensure_student("05")
        pe_name = _make_pe(self.batch_name, student, "05")
        quiz = _make_quiz()
        q1 = _make_quiz_question("05a", 8, 0)
        q2 = _make_quiz_question("05b", 5, 0)

        attempt1 = _make_attempt(student, quiz, [
            {"question": q1, "is_correct": True},
        ])
        handle_attempt_update(attempt1)

        attempt2 = _make_attempt(student, quiz, [
            {"question": q2, "is_correct": True},
        ])
        frappe.db.set_value(
            "StudentQuizAttempt", attempt1.name,
            "completed_at", "2026-05-12 09:00:00",
            update_modified=False,
        )
        frappe.db.set_value(
            "StudentQuizAttempt", attempt2.name,
            "completed_at", "2026-05-12 10:00:00",
            update_modified=False,
        )
        attempt2.reload()
        attempt2.points_earned = 0
        attempt2.save(ignore_permissions=True)

        handle_attempt_update(attempt2)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_quiz_points, 0,
                         "Cumulative total rolls up at week advance")
        self.assertEqual(pe.total_points, 0)
        # Weekly: 8 + 5 = 13
        self.assertEqual(pe.weekly_quiz_points, 13,
                         "Weekly always adds full new earned (8 + 5 = 13)")

    @patch("tap_lms.summer_program.quiz_points._enqueue_contact_field_sync")
    def test_quiz_zero_earned_no_pe_update(self, mock_sync):
        """E10: question correct but points=0; all answers wrong but
        failed_points=0. Earned = 0. points_earned written, no PE update."""
        student = _ensure_student("06")
        pe_name = _make_pe(self.batch_name, student, "06")
        quiz = _make_quiz()
        q1 = _make_quiz_question("06", 0, 0)
        attempt = _make_attempt(student, quiz, [
            {"question": q1, "is_correct": True},
        ])

        handle_attempt_update(attempt)

        attempt.reload()
        self.assertEqual(attempt.points_earned, 0,
                         "Zero-earned still writes points_earned so re-runs skip")
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_quiz_points, 0)
        self.assertEqual(pe.weekly_quiz_points, 0)
        self.assertEqual(pe.total_points, 0)

    def test_compute_quiz_points_correct_only(self):
        """Pure unit test on the compute helper — no DB write needed."""
        # Use a fake attempt-like object with answers child rows.
        class FakeAns:
            def __init__(self, question, is_correct):
                self.question = question
                self.is_correct = is_correct

        class FakeAttempt:
            def __init__(self, answers):
                self.answers = answers

        quiz = _make_quiz()
        q1 = _make_quiz_question("compute01", 10, 2)
        q2 = _make_quiz_question("compute02", 5, 1)

        attempt = FakeAttempt([
            FakeAns(q1, 1),  # correct → 10
            FakeAns(q2, 0),  # wrong → 1
        ])
        self.assertEqual(compute_quiz_points(attempt), 11)


# ════════════════════════════════════════════════════════════
# Task #92 — award_bonus_quiz_points must update total_points
# ════════════════════════════════════════════════════════════

class TestAwardBonusQuizPoints(FrappeTestCase):
    """Task #92 (2026-05-25): bonus_quiz_points are awarded by Glific via
    the `award_bonus_quiz_points` whitelisted endpoint when a student
    completes an independent bonus activity (separate from regular quiz
    attempts). They must update BOTH `bonus_quiz_points` (the dedicated
    stream column) AND `total_points` (the cumulative scoreboard), and
    must NOT leak into `weekly_quiz_points` or `total_quiz_points`
    (those are reserved for regular quiz attempts).

    Pre-task #92 the SQL only bumped `bonus_quiz_points`, breaking the
    CR-011 invariant `total_activity + total_quiz + total_submission
    + bonus_quiz_points == total_points`.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def _setup_pe(self, suffix, total_points=0, bonus=0,
                  total_quiz=0, weekly_quiz=0):
        student = _ensure_student(suffix)
        pe_name = _make_pe(self.batch_name, student, suffix)
        # Seed starting state via direct DB write so we exercise the
        # COALESCE branch of the SQL on a non-zero baseline.
        frappe.db.set_value("ProgramEnrollment", pe_name, {
            "total_points": total_points,
            "bonus_quiz_points": bonus,
            "total_quiz_points": total_quiz,
            "weekly_quiz_points": weekly_quiz,
        }, update_modified=False)
        return student, pe_name

    def test_bonus_award_updates_total_points_and_bonus(self):
        """The headline regression: awarding 10 bonus points bumps BOTH
        bonus_quiz_points (+10) AND total_points (+10)."""
        from tap_lms.summer_program.quiz_points import award_bonus_quiz_points

        student, pe_name = self._setup_pe(
            "bonus01", total_points=50, bonus=0,
            total_quiz=8, weekly_quiz=4,
        )

        result = award_bonus_quiz_points(student, 10)
        self.assertTrue(result.get("success"),
                        f"award should succeed, got {result}")

        pe = frappe.db.get_value("ProgramEnrollment", pe_name,
            ["total_points", "bonus_quiz_points",
             "total_quiz_points", "weekly_quiz_points"], as_dict=True)
        self.assertEqual(pe.total_points, 60,
                         "total_points must reflect the +10 bonus")
        self.assertEqual(pe.bonus_quiz_points, 10,
                         "bonus_quiz_points must reflect the +10 bonus")
        # Bonus must NOT leak into regular-quiz columns
        self.assertEqual(pe.total_quiz_points, 8,
                         "total_quiz_points must be untouched — bonus is "
                         "independent of regular quiz attempts")
        self.assertEqual(pe.weekly_quiz_points, 4,
                         "weekly_quiz_points must be untouched — bonus is "
                         "independent of regular quiz attempts")

    def test_bonus_award_preserves_invariant(self):
        """After awarding bonus points, the CR-011 invariant must hold:
        total_activity + total_quiz + total_submission + bonus_quiz_points
        == total_points."""
        from tap_lms.summer_program.quiz_points import award_bonus_quiz_points

        student, pe_name = self._setup_pe(
            "bonus02", total_points=83, bonus=0,
        )
        # Seed per-stream so invariant starts true: 25 + 8 + 50 + 0 == 83
        frappe.db.set_value("ProgramEnrollment", pe_name, {
            "total_activity_points": 25,
            "total_quiz_points": 8,
            "total_submission_points": 50,
        }, update_modified=False)

        award_bonus_quiz_points(student, 15)

        pe = frappe.db.get_value("ProgramEnrollment", pe_name,
            ["total_points", "total_activity_points", "total_quiz_points",
             "total_submission_points", "bonus_quiz_points"], as_dict=True)
        stream_sum = (pe.total_activity_points + pe.total_quiz_points
                      + pe.total_submission_points + pe.bonus_quiz_points)
        self.assertEqual(stream_sum, pe.total_points,
                         "CR-011 invariant must hold post-bonus-award")
        self.assertEqual(pe.total_points, 98,
                         "83 + 15 bonus = 98")
        self.assertEqual(pe.bonus_quiz_points, 15)

    def test_multiple_bonus_awards_accumulate(self):
        """Three sequential bonus awards (5, 7, 3) must accumulate to
        +15 on both columns. Verifies COALESCE-add semantics rather
        than overwrite."""
        from tap_lms.summer_program.quiz_points import award_bonus_quiz_points

        student, pe_name = self._setup_pe("bonus03", total_points=0, bonus=0)

        award_bonus_quiz_points(student, 5)
        award_bonus_quiz_points(student, 7)
        award_bonus_quiz_points(student, 3)

        pe = frappe.db.get_value("ProgramEnrollment", pe_name,
            ["total_points", "bonus_quiz_points"], as_dict=True)
        self.assertEqual(pe.bonus_quiz_points, 15)
        self.assertEqual(pe.total_points, 15)

    def test_invalid_inputs_return_failure_without_mutating(self):
        """Negative, non-numeric, and empty inputs must return
        {'success': False} and leave the PE state untouched."""
        from tap_lms.summer_program.quiz_points import award_bonus_quiz_points

        student, pe_name = self._setup_pe(
            "bonus04", total_points=42, bonus=7,
        )

        for bad in ("-5", "abc", "", None, "1.5"):
            result = award_bonus_quiz_points(student, bad)
            self.assertFalse(result.get("success"),
                             f"input {bad!r} should fail validation")

        pe = frappe.db.get_value("ProgramEnrollment", pe_name,
            ["total_points", "bonus_quiz_points"], as_dict=True)
        self.assertEqual(pe.total_points, 42,
                         "total_points must be untouched on validation failure")
        self.assertEqual(pe.bonus_quiz_points, 7,
                         "bonus_quiz_points must be untouched on validation failure")

    def test_event_log_records_bonus_award(self):
        """Each successful award must write a `bonus_quiz_points_awarded`
        ProgramEventLog row so the audit trail (and the
        recompute-from-audit script) can reconstruct the total."""
        from tap_lms.summer_program.quiz_points import award_bonus_quiz_points

        student, pe_name = self._setup_pe("bonus05", total_points=0, bonus=0)
        award_bonus_quiz_points(student, 25)

        events = frappe.db.sql("""
            SELECT event_type, new_value, old_value, trigger_source,
                   LEFT(details::text, 200) AS details
            FROM "tabProgramEventLog"
            WHERE enrollment = %s
              AND event_type = 'bonus_quiz_points_awarded'
            ORDER BY created_at DESC LIMIT 1
        """, (pe_name,), as_dict=True)
        self.assertEqual(len(events), 1, "exactly one bonus event expected")
        ev = events[0]
        self.assertEqual(ev.new_value, "25")
        self.assertEqual(ev.old_value, "0")
        self.assertEqual(ev.trigger_source, "microservice")
        self.assertIn("25", ev.details)
