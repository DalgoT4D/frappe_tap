"""
FeedbackConsumer → Summer Program Hook
tap_lms/summer_program/feedback_consumer_hook.py

This module provides the hook that FeedbackConsumer calls after processing
AI feedback from RabbitMQ. It bridges the existing feedback pipeline into
the SP state machine without duplicating Glific notification logic.

── Integration Point ──────────────────────────────────────────────
Add this call in FeedbackConsumer.process_message() AFTER update_submission()
commits and BEFORE send_glific_notification() starts the feedback flow:

    from tap_lms.summer_program.feedback_consumer_hook import on_feedback_ready
    on_feedback_ready(submission_name, student_id)

── What This Replaces ─────────────────────────────────────────────
Previously, pe_dispatcher.py had a `handle_feedback_notification` handler that:
  1. Checked PE state == feedback_ready
  2. Triggered SP_Feedback_Delivery Glific flow
  3. Cleared next_action

That was redundant because FeedbackConsumer sends the feedback to the student
via Glific (start_contact_flow with label="feedback") after this hook commits.
We only need the state machine transition (T12 → feedback_ready), which unlocks
week advancement and makes get_student_state accurate for feedback-flow
callbacks.

── Safety Net ─────────────────────────────────────────────────────
If this hook fails or FeedbackConsumer crashes before calling it,
pe_dispatcher's `handle_feedback_timeout` acts as a fallback: it polls
Submission.status every hour and triggers T12 if feedback arrived but
the state wasn't updated.

── CR-007: Points award is here (not in save_submission) ──────────
As of 2026-05-19, submission points are awarded by THIS hook, not by
save_submission. Reason: the points calculation depends on AI validation
results (Submission.result_status), which only land here. The submission
itself is already on disk by the time this hook fires; save_submission has
moved the PE into submitted_awaiting_feedback with points=0 (streak / gems /
weekly_submission_done are still bumped there because those apply to every
submission regardless of validity — user spec 2026-05-19).

Award logic (`_compute_submission_points`):
  - sent_count >= 1 (late submission, escalation fired):
      → EscalationStep[sent_count].points_awarded (decreasing-tier reward)
  - sent_count == 0, validation OFF (W1-2 by spec):
      → Assignment.points_per_item (full award, no validity check)
  - sent_count == 0, validation ON, AI says valid:
      → Assignment.points_per_item (full award)
  - sent_count == 0, validation ON, AI says Failed / Success - Flagged:
      → 0 (and route to Remedial via t6b)

The `submission_validation_enabled` flag on WeekRule is the single gate
that controls BOTH (a) whether failed/flagged submissions route to Remedial
AND (b) whether points are gated by validity. AI validation itself runs
unconditionally — the flag only controls how its result is interpreted.
"""
import frappe

from tap_lms.summer_program.utils import normalize_unicode_surrogates


def on_feedback_ready(submission_name, student_id=None):
    """
    Called by FeedbackConsumer after AI feedback is saved to Submission
    and the Glific notification is sent.

    Awards submission points (CR-007) then triggers the appropriate state
    transition: T6b (Remedial) when validation is enabled and AI flagged
    the submission, otherwise T12 (feedback_ready).

    Args:
        submission_name: Submission document name (e.g., "SUB-00123")
        student_id: Student document name (optional — resolved from submission if not provided)

    Returns:
        dict with status ("transitioned", "skipped", "no_pe", or "error")
    """
    # Eager imports for both transitions so the lazy/eager split flagged in
    # CR-007 review is resolved — t6b and t12 both live in state_machine,
    # neither has any back-reference to this module, so eager is safe.
    from tap_lms.summer_program.state_machine import (
        t12_feedback_ready,t13_feedback_delivered,
        t6b_failed_feedback_to_remedial,
    )
    from tap_lms.summer_program.constants import STATE_SUBMITTED_AWAITING

    try:
        # Resolve student from submission if not provided
        if not student_id:
            student_id = frappe.db.get_value("Submission", submission_name, "student_id")

        if not student_id:
            return {"status": "error", "message": f"No student found for submission {submission_name}"}

        # Find the active SP enrollment for this student
        pe_name = frappe.db.get_value(
            "ProgramEnrollment",
            {
                "student": student_id,
                "program_status": "active",
                "resolved_flow_state": STATE_SUBMITTED_AWAITING,
            },
            "name",
        )

        if not pe_name:
            # Student isn't in SP or isn't awaiting feedback — skip silently
            return {"status": "no_pe"}

        pe = frappe.get_doc("ProgramEnrollment", pe_name)

        # Double-check: is this submission for the PE's current week?
        sub_week = frappe.db.get_value("Submission", submission_name, "week")
        if sub_week and pe.current_week and int(sub_week) != int(pe.current_week):
            # Feedback for a different week — don't transition
            return {"status": "skipped", "reason": "week_mismatch",
                    "sub_week": sub_week, "pe_week": pe.current_week}

        # CR-004: branch on AI verdict from the Submission record.
        # CR-007: gate the Remedial branch on WeekRule.submission_validation_enabled
        # so weeks 1-2 (validation OFF by spec) don't accidentally route students
        # to Remedial on the first AI-flagged submission.
        #
        # NOTE: Do NOT commit here — let the caller (FeedbackConsumer.process_message)
        # handle the commit so that submission update + state transition are atomic.
        result_status = frappe.db.get_value("Submission", submission_name, "result_status")
        validity_status = frappe.db.get_value("Submission", submission_name, "submission_validity")

        # CR-007: compute & award submission points BEFORE the routing decision
        # so the contact-field sync that fires from the transition below carries
        # the correct points values to Glific.
        #
        # CRITICAL invariant (do NOT add intermediate mutations between the
        # reload and the transition below): the sequence must be exactly
        # (1) atomic SQL bump → (2) pe.reload() → (3) transition() → save().
        # Frappe's Document.save() writes ALL persistent fields, so if anything
        # mutates `pe` in memory between reload and save, the bumped point
        # columns will be silently re-written with stale values. Keep this
        # block tight.
        points = _compute_submission_points(pe, submission_name, result_status)
        if points > 0:
            _award_submission_points_atomic(pe.name, points)
            pe.reload()

        # CR-007: gate Remedial routing on submission_validation_enabled.
        # AI validation always runs; only its routing consequence is gated.
        if validity_status == "Invalid" or validity_status == "invalid":
            week_rule = _get_week_rule_for_pe(pe, sub_week or pe.current_week)
            validation_enabled = bool((week_rule or {}).get("submission_validation_enabled"))
            validation_enabled = True  # TEMP OVERRIDE 
            if validation_enabled:
                t6b_failed_feedback_to_remedial(pe, trigger_source="microservice")
                _sync_contact_fields(pe)
                return {"status": "transitioned", "pe": pe_name, "branch": "remedial",
                        "points_awarded": points}
            # else: lax mode (W1-2 or per-archetype override) — student keeps
            # points_per_item and stays on Core; fall through to feedback_ready.

        # Default branch — pass / unset / lax-mode-failed → feedback_ready as before.
        # Note: CR-004 aligns this call's trigger_source from "feedback_consumer" to "microservice"
        # for analytics consistency with the new t6b branch.
        t13_feedback_delivered(pe, trigger_source="microservice")
        _sync_contact_fields(pe)
        return {"status": "transitioned", "pe": pe_name, "branch": "feedback_ready",
                "points_awarded": points}

    except Exception as e:
        # L-030 / task #24 mirror: if a prior query in this txn aborted
        # (Postgres InFailedSqlTransaction), calling frappe.log_error
        # without first rolling back will itself fail with the same error
        # and the message gets silently dropped. Defensive rollback before
        # logging — this is a leaf hook, no in-flight writes worth keeping.
        try:
            frappe.db.rollback()
        except Exception:
            # If rollback itself fails we can't do much, but try to surface
            # the original error anyway via a logger.error (which doesn't
            # require a healthy txn).
            frappe.logger().error(
                f"SP feedback hook: rollback failed before log_error; "
                f"original error: {str(e)[:200]}"
            )
        # Truncate the message defensively to keep frappe's Error Log
        # doctype (message field has a length limit on some installs) from
        # CharacterLengthExceededError-ing — same hardening as task #29.
        msg = (
            f"SP feedback hook failed: submission={submission_name}, "
            f"student={student_id}, error={str(e)}"
        )
        try:
            frappe.log_error(
                msg[:1000],
                "SP Feedback Consumer Hook",
            )
        except Exception:
            # log_error itself failed — fall back to file logger, which
            # is independent of the Frappe DB layer.
            frappe.logger().error(msg[:1000])
        return {"status": "error", "message": str(e)}


# ════════════════════════════════════════════════════════════
# CR-007 helpers — submission point award
# ════════════════════════════════════════════════════════════

def _compute_submission_points(pe, submission_name, result_status):
    """Determine the submission's point award per CR-007.

    Branches:
      - sent_count >= 1: late submission → EscalationStep[sent_count].points_awarded
      - sent_count == 0, validation OFF: Assignment.points_per_item
      - sent_count == 0, validation ON, AI valid: Assignment.points_per_item
      - sent_count == 0, validation ON, AI Failed/Flagged: 0 (routes to Remedial)

    Returns: integer point award, always >= 0.

    Note: pe.current_escalation_step is NOT reset by T7/T9/T17/T3 (the
    submission transitions) — only by T14 (week_advance, state_machine.py:776).
    So between save_submission and this hook firing, the value preserves
    submission-time semantics. Safe to use as the "was this on-time?" proxy.
    """
    sent_count = pe.current_escalation_step or 0

    # Late submission — escalation tier governs reward, independent of validation
    if sent_count >= 1:
        return _escalation_points(pe, sent_count)

    # On-time path: look up validation gate + per-item award
    sub_week = pe.current_week or frappe.db.get_value("Submission", submission_name, "week")
    week_rule = _get_week_rule_for_pe(pe, sub_week)
    validation_enabled = bool((week_rule or {}).get("submission_validation_enabled"))

    # Strict mode: failed/flagged → 0 points
    if validation_enabled and result_status in ("Failed", "Success - Flagged"):
        return 0

    # Lax mode OR strict-mode-valid → award points_per_item
    assign_id = frappe.db.get_value("Submission", submission_name, "assign_id")
    assign_id = normalize_unicode_surrogates(assign_id)
    if not assign_id:
        frappe.logger().warning(
            f"on_feedback_ready: submission {submission_name} has no assign_id; "
            f"awarding 0 points to PE {pe.name}"
        )
        return 0
    return int(
        frappe.db.get_value("Assignment", assign_id, "points_per_item") or 0
    )


def _award_submission_points_atomic(pe_name, points):
    """Atomic COALESCE bump on submission-point columns (L-011 / P-002 pattern).

    Mirrors the atomic SQL used by activity_points.award_activity_points.

    CR-011 (2026-05-25): switched to **eager** totals. Pre-CR-011 this writer
    only bumped weekly_submission_points and relied on T14 to roll weekly→total
    at week advance. That left mid-week state incoherent on Glific (a student
    who earned submission points saw weekly_submission_points=N but
    total_submission_points=0 and total_points=0 until week advance). Now
    total_submission_points and total_points are bumped in the SAME atomic
    UPDATE so the invariant `stream_sum == total_points` holds at ALL TIMES,
    not just post-T14.
    """
    if not points:
        return
    frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET weekly_submission_points = COALESCE(weekly_submission_points, 0) + %s,
               total_submission_points  = COALESCE(total_submission_points,  0) + %s,
               total_points             = COALESCE(total_points,             0) + %s
         WHERE name = %s
        """,
        (points, points, points, pe_name),
    )


def _get_week_rule_for_pe(pe, week):
    """Reuse the canonical WeekRule lookup from pe_dispatcher.

    Lazy import avoids a circular dependency between this module and
    pe_dispatcher (which imports from state_machine, which has no direct
    dependency on feedback_consumer_hook today — but the import order
    can flip in the future).
    """
    from tap_lms.summer_program.pe_dispatcher import _get_week_rule
    batch = frappe.get_doc("Batch", pe.batch)
    return _get_week_rule(pe, batch, week)


def _escalation_points(pe, sent_count):
    """Read EscalationStep.points_awarded for the requested step.

    Indexing contract (FIXED 2026-05-22 — was off-by-one):

      `sent_count` == `pe.current_escalation_step`, which the dispatcher
      writes as `next_step = current_step + 1`. The first delivered
      escalation message → current_escalation_step == 1. The dispatcher
      itself reads `steps[next_step - 1]` (pe_dispatcher.py:351), so
      step N's config lives at `steps[N - 1]` (0-indexed list, sorted by
      escalation_order ascending). A student who responds late after
      escalation step 1 fired has sent_count == 1 and should receive
      `steps[0].points_awarded` (the reward associated with step 1).

      The previous indexing `min(sent_count, len(steps)-1)` returned
      `steps[1].points_awarded` (step 2's reward) for sent_count==1, which
      consistently over-rewarded students who submitted after the first
      escalation. This was masked by single-step configs where
      `len(steps)-1` clamped the index back to 0.

    Path-aware lookup (task #68 / 2026-05-22):

      Uses _get_escalation_steps(student, batch, path=pe.current_path) to
      pick the right escalation chain — Core students get Core's steps,
      Remedial students get Remedial's steps. If Remedial config has no
      steps configured, falls back to Core so the student still gets a
      point award (warning logged for operators). Pre-fix, this helper
      hardcoded Core for both paths, silently giving Remedial submitters
      Core's point rewards.

    Falls back to the last step's value if sent_count is beyond the
    configured chain (preserves the pre-CR-007 saturation behavior).
    """
    from tap_lms.summer_program.student_progression_sp import _get_escalation_steps
    from tap_lms.summer_program.constants import PATH_CORE, PATH_REMEDIAL

    student = frappe.get_doc("Student", pe.student)
    batch = frappe.get_doc("Batch", pe.batch)
    path = pe.current_path or PATH_CORE

    steps = _get_escalation_steps(student, batch, path=path)

    # Path-aware fallback: Remedial config empty → use Core's chain so the
    # student still receives a point award. Operator-visible warning logged.
    if not steps and path == PATH_REMEDIAL:
        frappe.logger().warning(
            f"_escalation_points: PE {pe.name} is on Remedial but "
            f"archetype={student.archetype}, arm={student.experiment_arm} "
            f"has no Remedial escalation_steps configured — falling back "
            f"to Core's chain for point award."
        )
        steps = _get_escalation_steps(student, batch, path=PATH_CORE)

    if not steps:
        return 0
    # sent_count is 1-indexed (matches dispatcher's next_step writes);
    # `steps` is 0-indexed → subtract 1 before clamping.
    idx = min(max(sent_count - 1, 0), len(steps) - 1)
    return int(steps[idx].get("points_awarded") or 0)


def _sync_contact_fields(pe):
    """Re-push PE state to Glific contact fields after the points award.

    The state transition itself (T6b or T12) already calls
    _enqueue_contact_field_sync via transition(). We could rely on that —
    but we want the freshly-updated total_points / total_submission_points /
    weekly_submission_points to land on Glific in the same payload as the
    state change. Calling this explicitly after the atomic SQL bump
    guarantees the next push reads the post-bump values.
    """
    from tap_lms.summer_program.state_machine import _enqueue_contact_field_sync
    pe.reload()
    if pe.glific_id:
        _enqueue_contact_field_sync(pe)
