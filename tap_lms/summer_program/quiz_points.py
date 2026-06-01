"""
Quiz-points handler (CR-002 v2).
tap_lms/summer_program/quiz_points.py

Awards per-question quiz points to a ProgramEnrollment on
`StudentQuizAttempt.on_update` once `completed_at` is set. Wired via
`doc_events["StudentQuizAttempt"]["on_update"]` in hooks.py.

Per-question award rule (CR-002 v2 §"Per-question quiz scoring"):

  - Correct answer  → `QuizQuestion.points`
  - Wrong answer    → `QuizQuestion.failed_points`

The per-question award is **independent of attempt-level pass/fail**.

Cumulative vs weekly split:

  - `weekly_quiz_points`: adds the new attempt's full earned score using the
    existing effort semantics. Two attempts in the same week with earned 5
    then 8 add weekly +5 +8 = +13.
  - `total_quiz_points`, `total_points`: roll up once during week advance.

Idempotency: `attempt.points_earned > 0` is the write-once anchor (P-005).
The audit field is written FIRST, before the PE bump, so a crash between
the two writes leaves a clean state on retry.

Race-tolerance: the PE UPDATE uses COALESCE (P-002 / L-011) so it is safe
against T19's weekly reset.
"""
import frappe

from tap_lms.summer_program.state_machine import (
    _enqueue_contact_field_sync,
    get_active_pe,
)
from tap_lms.summer_program.event_log import log_event
from tap_lms.summer_program.utils import (
    glific_response,
    normalize_unicode_surrogates,
    resolve_student,
)


# ════════════════════════════════════════════════════════════
# DocType-event entry point
# ════════════════════════════════════════════════════════════

def handle_attempt_update(doc, method=None):
    """`on_update` hook for StudentQuizAttempt.

    Returns early if the attempt isn't completed yet, or if it has already
    been awarded (idempotency anchor: `doc.points_earned > 0`).
    """
    if not getattr(doc, "completed_at", None):
        return
    if (doc.points_earned or 0) > 0:
        return

    award_quiz_points(doc)


# ════════════════════════════════════════════════════════════
# Core award logic
# ════════════════════════════════════════════════════════════

def award_quiz_points(attempt):
    """Award per-question quiz points to the student's active PE.

    Spec: CR-002 v2 §"New module — quiz_points.py".
    """
    earned = compute_quiz_points(attempt)

    # ── Idempotency anchor written FIRST (P-005) ────────────
    # Even when earned == 0 we still write the field so re-saves of the
    # attempt are no-ops via the entry-point guard above.
    frappe.db.set_value(
        "StudentQuizAttempt", attempt.name, "points_earned", earned,
        update_modified=False,
    )

    # If the attempt earned no points, there's nothing more to do.
    if earned == 0:
        return

    # ── Resolve active PE ────────────────────────────────────
    pe = get_active_pe(attempt.student)
    if not pe:
        return

    # ── Atomic UPDATE on PE (P-002 / L-011) ─────────────────
    # Weekly column always adds the full new earned (effort semantics).
    # COALESCE is race-safe vs T19's reset of weekly_quiz_points (E5).
    #
    # CR-011 (2026-05-25): switched to **eager** totals. Pre-CR-011 the
    # per-event handler only bumped weekly_quiz_points and let T14 roll
    # weekly→total at week advance, which left mid-week state incoherent
    # (a student saw weekly_quiz_points=3 but total_quiz_points=0 and
    # total_points=0 on Glific). Now total_quiz_points and total_points
    # are bumped in the SAME atomic UPDATE so the invariant
    # `stream_sum == total_points` holds at ALL TIMES, not just post-T14.
    frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET weekly_quiz_points = COALESCE(weekly_quiz_points, 0) + %s,
               total_quiz_points  = COALESCE(total_quiz_points,  0) + %s,
               total_points       = COALESCE(total_points,       0) + %s
         WHERE name = %s
        """,
        (earned, earned, earned, pe.name),
    )

    # ── Push contact fields ─────────────────────────────────
    pe.reload()
    if pe.glific_id:
        _enqueue_contact_field_sync(pe)

    # ── ProgramEventLog ─────────────────────────────────────
    log_event(
        pe, "quiz_points_awarded",
        new_value=str(earned),
        trigger_source="quiz_attempt",
        details={
            "attempt": attempt.name,
            "quiz": attempt.quiz,
            "earned": earned,
        },
    )


@frappe.whitelist(allow_guest=False)
@glific_response
def award_bonus_quiz_points(student_id, points, **_glific_kwargs):
    """Award bonus points (independent of regular quiz attempts) to the
    student's active PE.

    Use case: Glific flow runs an independent bonus activity. When the
    student completes it, the Glific webhook calls this endpoint with the
    point value. Bonus points are tracked in a dedicated column
    (`bonus_quiz_points`) so the bonus stream is distinguishable from
    regular per-question quiz points, AND they contribute to `total_points`
    so leaderboards / dashboards see the student's true cumulative score.

    Updates (atomically, in one statement):
      - `bonus_quiz_points` += points (the dedicated bonus stream column)
      - `total_points`      += points (CR-011 invariant — task #92, 2026-05-25)

    Does NOT update:
      - `weekly_quiz_points` / `total_quiz_points` — bonus is independent
        of regular quiz attempts (which go through `award_quiz_points`).
      - `weekly_*` — bonus is a lifetime counter, not subject to T14 reset.

    Invariant preserved (task #85, CR-011):
        total_activity + total_quiz + total_submission + bonus_quiz_points
        == total_points

    `**_glific_kwargs` absorbs Glific-injected fields per task #89 — ignored.
    """
    student_id = resolve_student(student_id)
    if not student_id:
        return {"success": False}

    parsed_points = _parse_bonus_points(points)
    if parsed_points is None:
        return {"success": False}

    pe = get_active_pe(student_id)
    if not pe:
        return {"success": False}

    old_bonus_points = int(pe.bonus_quiz_points or 0)
    # Task #92 (2026-05-25): bonus_quiz_points AND total_points both bumped
    # in the same atomic UPDATE. Pre-fix, only bonus_quiz_points was updated,
    # which broke the CR-011 invariant (stream_sum == total_points) by
    # exactly the awarded value — Glific would show inflated bonus but
    # stale total_points until the next regular event happened to land.
    # COALESCE is race-safe vs concurrent quiz / activity / submission
    # updates that also touch total_points (per L-011).
    frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET bonus_quiz_points = COALESCE(bonus_quiz_points, 0) + %s,
               total_points      = COALESCE(total_points,      0) + %s
         WHERE name = %s
        """,
        (parsed_points, parsed_points, pe.name),
    )

    pe.reload()
    if pe.glific_id:
        _enqueue_contact_field_sync(pe)

    log_event(
        pe, "bonus_quiz_points_awarded",
        old_value=str(old_bonus_points),
        new_value=str(pe.bonus_quiz_points or 0),
        trigger_source="microservice",
        details={
            "points_awarded": parsed_points,
        },
    )

    return {"success": True}


# ════════════════════════════════════════════════════════════
# Per-question computation
# ════════════════════════════════════════════════════════════

def compute_quiz_points(attempt):
    """Sum per-question awards across an attempt's answers.

    Correct → QuizQuestion.points; wrong → QuizQuestion.failed_points.
    Independent of attempt-level pass/fail.

    Uses `frappe.get_cached_doc` so repeated questions across cohort
    attempts are served from the request-scoped cache.
    """
    earned = 0
    for ans in (attempt.answers or []):
        question = normalize_unicode_surrogates(ans.question)
        q = frappe.get_cached_doc("QuizQuestion", question)
        if ans.is_correct:
            earned += int(q.points or 0)
        else:
            earned += int(getattr(q, "failed_points", 0) or 0)
    return earned


# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════

def _previous_latest_points(student, quiz, current_attempt_name):
    """Return `points_earned` of the most recent prior attempt for
    (student, quiz), or 0 if none exists.

    Used to compute delta for cumulative quiz-points (latest-score
    semantics per CR-002 v2 §E4). The current attempt is excluded so the
    delta is always (new earned − previous best-known earned).
    """
    row = frappe.db.sql(
        """
        SELECT points_earned
          FROM "tabStudentQuizAttempt"
         WHERE student = %s
           AND quiz    = %s
           AND name   != %s
           AND points_earned IS NOT NULL
         ORDER BY completed_at DESC, modified DESC
         LIMIT 1
        """,
        (student, quiz, current_attempt_name),
        as_dict=False,
    )
    if not row:
        return 0
    return int(row[0][0] or 0)


def _parse_bonus_points(points):
    """Return a non-negative int, or None for invalid API input."""
    try:
        if isinstance(points, bool):
            return None
        text = str(points).strip()
        if not text or not text.isdigit():
            return None
        parsed = int(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
