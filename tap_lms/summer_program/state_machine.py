"""
State Machine — Resolved Flow State Transitions
tap_lms/summer_program/state_machine.py

Implements all 25 transitions (T0–T25) on ProgramEnrollment.resolved_flow_state.
Each transition:
  1. Validates the current state is allowed
  2. Updates PE fields (resolved_flow_state, journey_label, next_action_at/type, etc.)
  3. Updates Glific contact fields
  4. Logs to ProgramEventLog

Called by: update_flow_status, save_submission, reactivate_student, scheduler actions.
"""
import frappe
from frappe.utils import now_datetime, add_to_date, getdate

from tap_lms.glific_integration import update_contact_fields
from tap_lms.summer_program.constants import (
    STATE_NORMAL_CONTENT, STATE_NORMAL_ESCALATION,
    STATE_REMEDIAL_CONTENT, STATE_REMEDIAL_ESCALATION,
    STATE_GRACE_WAITING, STATE_PAUSED_BINGE,
    STATE_SUBMITTED_AWAITING, STATE_FEEDBACK_READY,
    STATE_WEEK_COMPLETED, STATE_PROGRAM_COMPLETED, STATE_PROGRAM_DROPPED,
    STATE_PROGRAM_PAUSED, PAUSED_STATES, TERMINAL_STATES,
    LABEL_CONTENT_DELIVERED, LABEL_REMEDIAL_STARTED,
    LABEL_SUBMITTED, LABEL_FEEDBACK_DELIVERED,
    LABEL_GRACE_WINDOW, LABEL_PAUSED, LABEL_RESUMED,
    LABEL_COMPLETED, LABEL_DROPPED, LABEL_WEEK_ADVANCED,
    PROGRAM_ACTIVE, PROGRAM_PAUSED, PROGRAM_COMPLETED, PROGRAM_DROPPED,
    PATH_CORE, PATH_REMEDIAL,
    ACTION_ESCALATION, ACTION_CONTENT_DELIVERY,
    ACTION_FEEDBACK_TIMEOUT, ACTION_WEEK_ADVANCEMENT,
    ACTION_GRACE_CHECK,
    ACTION_PAUSE_CHECK,
    PAUSE_BINGE_LIMIT,
    DEFAULT_GRACE_WINDOW_DAYS,
    FEEDBACK_TIMEOUT_HOURS,
    CF_RESOLVED_FLOW_STATE, CF_CURRENT_WEEK, CF_CURRENT_PATH,
    CF_CURRENT_TIER, CF_PROGRAM_STATUS, CF_TOTAL_POINTS,
    CF_CURRENT_STREAK, CF_GRACE_WINDOW_END, CF_EXPECTED_SUBMISSION,
    CF_LAST_ESCALATION_STEP, CF_SUBMISSION_COUNT,
    # CR-002 v2 — 8 new gamification contact fields
    CF_TOTAL_ACTIVITY_POINTS, CF_WEEKLY_ACTIVITY_POINTS,
    CF_TOTAL_QUIZ_POINTS, CF_WEEKLY_QUIZ_POINTS,
    CF_TOTAL_SUBMISSION_POINTS, CF_WEEKLY_SUBMISSION_POINTS,
    CF_SPECIAL_GEMS, CF_WEEKLY_SUBMISSION_DONE, CF_BONUS_QUIZ_POINTS,
    CF_WEEKLY_ENGAGEMENT_POINTS,
    # CR-003 — 2 new escalation channel routing fields
    CF_ESCALATION_ORDER, CF_ESCALATION_TYPE,
    GLIFIC_SYNC_MAX_RETRIES, GLIFIC_SYNC_RETRY_LOG_TITLE, GLIFIC_SYNC_DLQ_LOG_TITLE,
    TIER_BY_WEEK, DEFAULT_TIER,
)
from tap_lms.summer_program.event_log import log_event, log_state_transition
from tap_lms.summer_program.weekly_rollup import calculate_week_advance_rollup
# CR-005 (2026-05-15): module-level import so tests can patch
# `tap_lms.summer_program.state_machine.maintain_collections` cleanly.
# (A function-local import would force tests to patch the source module,
# which is fragile against future refactors.)
from tap_lms.summer_program.collection_membership import maintain_collections


# ════════════════════════════════════════════════════════════
# CORE TRANSITION ENGINE
# ════════════════════════════════════════════════════════════

def transition(pe, new_state, trigger_source="scheduler", extra_updates=None, skip_glific=False):
    """
    Execute a state transition on a ProgramEnrollment.

    L-011 race-safety (task #27, 2026-05-22):
      Previously this function did `pe.save(ignore_permissions=True)` which
      writes EVERY column on the row, not just the dirty ones. Concurrent
      atomic bumps to columns NOT in `extra_updates` (total_*/weekly_*/
      grace_*/submission_count/weekly_video_done) — performed by
      activity_points, quiz_points, save_submission's primary claim, and
      _award_submission_points_atomic — were silently overwritten by the
      stale in-memory values pe was loaded with.

      Now uses `frappe.db.set_value` with the targeted updates dict —
      touches ONLY the fields actually being changed, so concurrent
      atomic bumps survive. Followed by `pe.reload()` so the in-memory
      doc reflects (a) our updates AND (b) any concurrent column bumps;
      downstream code (log_state_transition, _enqueue_contact_field_sync,
      maintain_collections) sees fresh state, and the Glific contact
      field push carries the post-bump values in the same payload as
      the state change.

      Safe because ProgramEnrollment has no controller validate/
      before_save/after_save hooks and no doc_events registered in
      hooks.py — the previous pe.save() was just performing the row
      write, no business-logic hooks to preserve.

    Args:
        pe: ProgramEnrollment doc (must be loaded, not just name)
        new_state: target resolved_flow_state
        trigger_source: who triggered this
        extra_updates: dict of additional PE field updates
        skip_glific: if True, skip Glific contact field update (for batch operations)

    Returns:
        True if transition was applied
    """
    old_state = pe.resolved_flow_state

    # Build the targeted updates dict — only fields actually changing.
    # Concurrent atomic bumps to other columns (handled by activity_points,
    # quiz_points, feedback hook) will survive because they're not in the
    # UPDATE SET clause.
    updates = {
        "resolved_flow_state": new_state,
        "last_label_change_at": now_datetime(),
    }
    if extra_updates:
        updates.update(extra_updates)

    # Targeted SQL UPDATE via Frappe ORM. set_value updates `modified` and
    # `modified_by` by default — same audit-trail behavior as pe.save().
    frappe.db.set_value("ProgramEnrollment", pe.name, updates)

    # Reload the in-memory doc so downstream code (logging, Glific push,
    # collection maintenance) sees fresh DB state — including any
    # concurrent column bumps that happened between pe load and now.
    pe.reload()

    # Log the transition
    log_state_transition(pe, old_state, new_state, trigger_source)

    # Update Glific contact fields (async — runs in background worker).
    # The payload built here now reflects ALL post-update column values,
    # including concurrent atomic bumps that pe.reload() picked up.
    if not skip_glific and pe.glific_id:
        _enqueue_contact_field_sync(pe)

        # CR-005 (2026-05-15): maintain Glific group membership (audit + main)
        # via state-driven writes (Approach B). The weekly Tuesday cron just
        # fires the flow against the main collection — no membership recompute.
        # See collection_membership.py for the delta-based decision logic.
        maintain_collections(pe, from_state=old_state, to_state=new_state)

    return True


def _enqueue_contact_field_sync(pe):
    """
    Enqueue Glific contact field sync as a background job.

    Serializes the PE fields into a plain dict so the background worker
    doesn't need to reload the doc. This keeps the API response fast
    (~50ms) while Glific sync happens in the background (~200-500ms).

    ── Field provenance — the 30 SP contact fields ────────────────────
    This function builds the recurring 23-field STATE payload pushed on
    every state-machine transition. The 7 IDENTITY fields below (immutable
    after enrollment) are pushed ONCE by `_process_pe_chunk` at PE creation
    and never re-synced from here. Total cache size on Glific: 30 fields
    (was 28 pre-task-#98; bonus_quiz_points added 2026-05-25;
    weekly_engagement_points added 2026-05-26 task #7 — COMPUTED, not
    stored on PE).

    23 STATE fields (this function — re-synced on every transition):
        resolved_flow_state         pe.resolved_flow_state
        current_week                pe.current_week
        current_path                pe.current_path
        current_tier                pe.current_tier
        program_status              pe.program_status
        total_points                pe.total_points (stored, not summed)
        current_streak              pe.current_streak
        grace_window_end_at         pe.grace_window_end_at
        current_expected_submission_type
                                    pe.current_expected_submission_type
                                    (denormalized — written at enrollment +
                                     T14 week advance, read directly here)
        last_escalation_step        pe.current_escalation_step
        submission_count            pe.submission_count
        # CR-002 v2 gamification (9 — bonus_quiz_points added 2026-05-25 task #98):
        total_activity_points       pe.total_activity_points
        weekly_activity_points      pe.weekly_activity_points
        total_quiz_points           pe.total_quiz_points
        weekly_quiz_points          pe.weekly_quiz_points
        total_submission_points     pe.total_submission_points
        weekly_submission_points    pe.weekly_submission_points
        special_gems                pe.special_gems
        weekly_submission_done      pe.weekly_submission_done
        bonus_quiz_points           pe.bonus_quiz_points
                                    (added 2026-05-25 task #98 — the Glific
                                     gamification card references
                                     @contact.fields.bonus_quiz_points and
                                     was rendering literal template text
                                     when the field was missing, e.g. for
                                     ST00051295)
        weekly_engagement_points    COMPUTED — weekly_submission_points
                                    + weekly_activity_points (added
                                    2026-05-26 task #7). NOT a PE column;
                                    not bumped by any handler; recomputed
                                    here every push so it always equals
                                    the live sum of its two addends. The
                                    Glific gamification card can render a
                                    single "engagement" stat without doing
                                    the sum in the flow editor.
        # CR-003 escalation routing (2):
        escalation_order            pe.current_escalation_step (re-aliased)
        escalation_type             pe.current_escalation_type (denormalized,
                                    written by dispatcher T2/T4/T8/T10
                                    escalation handlers)

    7 IDENTITY fields (pushed once by _process_pe_chunk, immutable):
        student_id, student_name, batch_id, archetype, language,
        experiment_arm, course_level

    INTENTIONALLY EXCLUDED:
        weekly_video_done — internal state-machine flag for T19 streak/gem
                            penalty branch; never pushed to Glific.
        streak / gems     — these are LEGACY Glific keys OWNED BY OTHER
                            and other programs sharing the same Glific
                            organisation (TLM, scert, pocflow, …). The
                            shared org has ~470 contact field definitions
                            across all programs; the SP backend MUST NOT
                            write to any field outside its registered
                            namespace (the 29 SP-owned fields above) —
                            doing so would silently clobber data that
                            other programs rely on. SP-owned canonical
                            keys are `current_streak` and `special_gems`.
                            See L-008 (rename = breakage) and constants.py
                            multi-tenant boundary note.

    If you add a new contact field to the SP, update both this function
    AND _process_pe_chunk's enrollment-time push so the cache is consistent
    from day-one rather than only after the first transition.
    """
    fields = {
        CF_RESOLVED_FLOW_STATE: pe.resolved_flow_state or "",
        CF_CURRENT_WEEK: str(pe.current_week or 0),
        CF_CURRENT_PATH: pe.current_path or "",
        CF_CURRENT_TIER: pe.current_tier or "",
        CF_PROGRAM_STATUS: pe.program_status or "",
        CF_TOTAL_POINTS: str(pe.total_points or 0),
        CF_CURRENT_STREAK: str(pe.current_streak or 0),
        CF_GRACE_WINDOW_END: str(pe.grace_window_end_at) if pe.grace_window_end_at else "",
        CF_EXPECTED_SUBMISSION: pe.current_expected_submission_type or "",
        CF_LAST_ESCALATION_STEP: str(pe.current_escalation_step or 0),
        CF_SUBMISSION_COUNT: str(pe.submission_count or 0),
        # ── CR-002 v2: 8 new gamification fields ──
        # Pushed alongside the existing fields. Cache evolution:
        #   - 26 after CR-002 v2 (+ 8 gamification fields)
        #   - 28 after CR-003 (+ escalation_order, escalation_type)
        #   - 29 after task #98 (+ bonus_quiz_points)
        # `weekly_video_done` is intentionally NOT included — internal-only.
        CF_TOTAL_ACTIVITY_POINTS: str(pe.total_activity_points or 0),
        CF_WEEKLY_ACTIVITY_POINTS: str(pe.weekly_activity_points or 0),
        CF_TOTAL_QUIZ_POINTS: str(pe.total_quiz_points or 0),
        CF_WEEKLY_QUIZ_POINTS: str(pe.weekly_quiz_points or 0),
        CF_TOTAL_SUBMISSION_POINTS: str(pe.total_submission_points or 0),
        CF_WEEKLY_SUBMISSION_POINTS: str(pe.weekly_submission_points or 0),
        CF_SPECIAL_GEMS: str(pe.special_gems or 0),
        CF_WEEKLY_SUBMISSION_DONE: str(int(pe.weekly_submission_done or 0)),
        # 2026-05-25 (task #98): added to sync payload because the
        # Glific gamification card references @contact.fields.bonus_quiz_points
        # and was rendering literal text when the field was missing. The earlier
        # "intentionally excluded" stance has been retired — the card needs it.
        CF_BONUS_QUIZ_POINTS: str(pe.bonus_quiz_points or 0),
        # 2026-05-26 (task #7): weekly_engagement_points is COMPUTED, not
        # stored. Pushed alongside the canonical weekly_* fields so the
        # Glific flow can render a single "engagement" stat (submission +
        # activity) without computing the sum in the flow editor. Resets
        # to 0 naturally with the lazy reset because both addends reset.
        CF_WEEKLY_ENGAGEMENT_POINTS: str(
            (pe.weekly_submission_points or 0)
            + (pe.weekly_activity_points or 0)
        ),
        # ── CR-003: escalation_order + escalation_type are re-synced here so
        # the Glific contact cache reflects the current escalation step on
        # every transition. The dispatcher's T2/T4/T8/T10 calls now resolve
        # the step's escalation_type and write it to PE.current_escalation_type
        # as part of the same transition, so the standard sync below is the
        # only push needed — the previous eager `_push_escalation_contact_fields`
        # in pe_dispatcher.handle_escalation has been removed.
        CF_ESCALATION_ORDER: str(pe.current_escalation_step or 0),
        CF_ESCALATION_TYPE: str(pe.current_escalation_type or ""),
    }
    frappe.enqueue(
        "tap_lms.summer_program.state_machine._sync_contact_fields_job",
        queue="short",
        timeout=30,
        enqueue_after_commit=True,
        glific_id=str(pe.glific_id),
        fields=fields,
        pe_name=pe.name,
        retry_count=0,
        student_id=pe.student,
    )


def _sync_contact_fields_job(glific_id, fields, pe_name, retry_count=0, student_id=None):
    """
    Background job: push PE state to Glific contact fields.

    Called via frappe.enqueue from _enqueue_contact_field_sync and the three
    enrollment paths. Uses the single-call update_contact_fields mutation.

    Args:
        glific_id: Glific contact ID — required to address the contact.
        fields: dict of contact field name → string value (already serialized).
        pe_name: ProgramEnrollment.name when available, OR a synthetic id like
                 "pre-pe:<student_id>" during the pre-PE enrollment chunk path.
                 Used for log correlation; do NOT assume it's resolvable via
                 frappe.get_doc("ProgramEnrollment", pe_name).
        retry_count: Current retry attempt (0 on first call). Re-enqueues
                     increment this; jobs exhausting GLIFIC_SYNC_MAX_RETRIES
                     attempts land in the DLQ.
        student_id: Optional Student document name. Always populated for
                    operator replay so the DLQ entry is actionable even when
                    pe_name is a synthetic "pre-pe:..." string. None for legacy
                    in-flight messages (backward-compat).

    Retry policy (pattern P-007 / lesson L-015):
      - On exception, re-enqueue self with retry_count+1 until GLIFIC_SYNC_MAX_RETRIES.
      - When the retry budget is exhausted, log a structured DLQ entry so operators
        can replay manually. The DLQ payload includes student_id, pe_name, glific_id,
        the fields dict, the final error, and the retry count.

    Known limitation (filed as follow-up): retries are IMMEDIATE (no backoff).
    For sustained Glific outages > ~30 seconds, all 5 retries fire within
    milliseconds and the job DLQs. A proper exponential-backoff scheduler is
    deferred to a follow-up task. Bumping MAX_RETRIES to 5 (from the originally
    proposed 3) covers short Glific 502/503 blips and Redis hiccups while keeping
    this revision minimal.
    """
    try:
        # Fix 2026-05-22 (task #5): update_contact_fields returns True/False
        # and writes diagnostics to frappe.logger().error on every failure
        # mode (Glific GraphQL fetch errors, contact-not-found, mutation
        # errors, network errors, catch-all) — it NEVER raises. The except
        # branch below only fires on raised exceptions, so prior to this
        # fix every False return silently exited and the retry/DLQ machinery
        # never ran. Raise an explicit marker exception on False so failures
        # actually flow through retries + the DLQ log entry. Without this
        # guard the file-log line was the ONLY signal of a dropped write.
        ok = update_contact_fields(glific_id, fields)
        if not ok:
            raise RuntimeError(
                f"update_contact_fields returned False for contact "
                f"{glific_id} (PE {pe_name}); see prior logger().error "
                f"for the specific failure mode."
            )
    except Exception as e:
        import json as _json
        from datetime import timedelta as _timedelta
        retry_count = (retry_count or 0) + 1
        if retry_count <= GLIFIC_SYNC_MAX_RETRIES:
            # CR-025 Layer 2 — exponential backoff delay (min(60, 2**retry) seconds).
            # We attempt to schedule via rq Queue.enqueue_in (non-blocking, no worker
            # sleep). If enqueue_in is unavailable (rq-scheduler not running), we fall
            # back to an immediate frappe.enqueue so the retry still fires.
            # do NOT use time.sleep() here — this blocks a shared worker for up to 60s.
            delay_seconds = min(60, 2 ** retry_count)
            frappe.log_error(
                title=GLIFIC_SYNC_RETRY_LOG_TITLE,
                message=(
                    f"Glific sync transient failure "
                    f"(attempt {retry_count}/{GLIFIC_SYNC_MAX_RETRIES + 1}) "
                    f"for PE {pe_name} (student={student_id or 'unknown'}), "
                    f"backoff={delay_seconds}s: {e}"
                ),
            )
            try:
                from frappe.utils.background_jobs import get_queue as _get_queue
                q = _get_queue("short")
                q.enqueue_in(
                    _timedelta(seconds=delay_seconds),
                    "tap_lms.summer_program.state_machine._sync_contact_fields_job",
                    kwargs=dict(
                        glific_id=glific_id,
                        fields=fields,
                        pe_name=pe_name,
                        retry_count=retry_count,
                        student_id=student_id,
                    ),
                    job_timeout=30,
                )
            except Exception as enqueue_err:
                # Double-fault: enqueue_in failed (rq-scheduler unavailable or
                # Redis unhealthy). Fall back to immediate frappe.enqueue so we
                # don't silently drop the retry.
                try:
                    frappe.enqueue(
                        "tap_lms.summer_program.state_machine._sync_contact_fields_job",
                        queue="short",
                        timeout=30,
                        glific_id=glific_id,
                        fields=fields,
                        pe_name=pe_name,
                        retry_count=retry_count,
                        student_id=student_id,
                    )
                except Exception as fallback_err:
                    # Both delayed and immediate enqueue failed — queue is down.
                    # Surface to DLQ so the update isn't silently lost.
                    frappe.log_error(
                        title=GLIFIC_SYNC_DLQ_LOG_TITLE,
                        message=_json.dumps(
                            {
                                "reason": "double_fault_enqueue_failed",
                                "student_id": student_id,
                                "pe_name": pe_name,
                                "glific_id": glific_id,
                                "fields": fields,
                                "final_error": str(e),
                                "enqueue_error": str(enqueue_err),
                                "fallback_error": str(fallback_err),
                                "retries_attempted": retry_count,
                            },
                            indent=2,
                            default=str,
                        ),
                    )
        else:
            frappe.log_error(
                title=GLIFIC_SYNC_DLQ_LOG_TITLE,
                message=_json.dumps(
                    {
                        "student_id": student_id,
                        "pe_name": pe_name,
                        "glific_id": glific_id,
                        "fields": fields,
                        "final_error": str(e),
                        "retries_attempted": retry_count,
                    },
                    indent=2,
                    default=str,
                ),
            )


# ════════════════════════════════════════════════════════════
# CR-003 follow-up (2026-05-13) — Grace clock helper
# ════════════════════════════════════════════════════════════
#
# The original CR-003 helper `_grace_clock_updates` (armed grace at week
# starts T0 / T19) was retired in the 2026-05-13 follow-up. Grace is now
# armed by `activity_points.handle_content_log` on the week's FIRST
# VideoClass completion (atomic Postgres `CASE WHEN`) and cleared by the
# four primary-submission transitions (T7/T9/T17/T3). The only remaining
# grace-arming code path is the defensive backfill in T5/T11 for legacy
# PEs that pre-date CR-003 and somehow land in escalation-exhaust without
# a clock set — that's what `_batch_grace_window_days` still serves.

def _batch_grace_window_days(pe):
    """Return the cohort's grace window in days.

    CR-003: grace duration is per-batch via Batch.grace_window_days. Falls
    back to DEFAULT_GRACE_WINDOW_DAYS (14) only if the field is unset, which
    should not happen on properly-configured batches. We log a warning rather
    than throwing so a misconfigured batch doesn't block dispatch.
    """
    if not pe.batch:
        return DEFAULT_GRACE_WINDOW_DAYS
    try:
        days = frappe.db.get_value("Batch", pe.batch, "grace_window_days")
    except Exception:
        days = None
    if not days:
        frappe.log_error(
            f"Batch {pe.batch} has no grace_window_days; "
            f"falling back to default ({DEFAULT_GRACE_WINDOW_DAYS}d) for PE {pe.name}",
            "SP Grace Clock Config",
        )
        return DEFAULT_GRACE_WINDOW_DAYS
    return int(days)


def _defensive_grace_clock_updates(pe):
    """Return a grace-clock-arming dict for the LEGACY-PE defensive path in
    T5/T11 only. Used when a PE somehow reaches escalation exhaustion without
    a clock set (e.g., a PE that pre-dates the 2026-05-13 follow-up that moved
    arming into the activity-points handler).

    DO NOT use this for new week-start arming — that's now handled by
    `activity_points.handle_content_log` on the week's first VideoClass
    completion via atomic Postgres CASE WHEN.
    """
    now = now_datetime()
    grace_end = add_to_date(now, days=_batch_grace_window_days(pe))
    return {
        "in_grace_window": 1,
        "grace_window_start": now,
        "grace_window_end_at": grace_end,
    }


# ════════════════════════════════════════════════════════════
# NAMED TRANSITIONS (T0–T25)
# ════════════════════════════════════════════════════════════

# ── T0: Enrollment → normal_content_delivery ───────────────
def t0_enrollment(pe, trigger_source="scheduler"):
    """T0: Initial enrollment. Sets resolved_flow_state = normal_content_delivery.

    CR-003 follow-up (2026-05-13): T0 NO LONGER arms the grace clock. The
    clock is now armed by `activity_points.handle_content_log` on the
    week's first VideoClass completion (atomic Postgres CASE WHEN trick on
    the existing UPDATE). T0 only sets resolved_flow_state, journey_label,
    program_status, path, and current_week. The grace clock arms naturally
    once the student watches their first VideoClass; it stays unset until
    then.
    """
    updates = {
        "journey_label": LABEL_CONTENT_DELIVERED,
        "program_status": PROGRAM_ACTIVE,
        "current_path": PATH_CORE,
        "current_week": 1,
    }
    return transition(pe, STATE_NORMAL_CONTENT, trigger_source, updates)


# ── T1: Content delivered, no response → stays, schedule escalation ──
def t1_content_no_response(pe, escalation_step, trigger_source="flow_callback"):
    """
    T1: Content delivered but student didn't respond within wait window.
    State stays normal_content_delivery but schedule escalation.

    CR-003 follow-up: the dispatcher resolves the step's `escalation_type`
    when it actually fires the escalation (T2/T4/T8/T10). T1 only schedules
    the next tick — we don't write `current_escalation_type` here because
    the step hasn't fired yet and the contact field would announce a channel
    we haven't taken. The dispatcher's T2 call sets it when the step fires.
    """
    hours = escalation_step.get("hours_after_previous", 24) if escalation_step else 24
    return transition(pe, STATE_NORMAL_CONTENT, trigger_source, {
        "next_action_at": add_to_date(now_datetime(), hours=hours),
        "next_action_type": ACTION_ESCALATION,
    })


# ── T2: Start escalation (Core) ───────────────────────────
def t2_start_escalation(pe, step_number=1, escalation_type="",
                         next_hours=None, trigger_source="scheduler"):
    """T2: normal_content_delivery → normal_escalation.

    CR-003 follow-up: also writes `current_escalation_type` so the standard
    contact-field sync pushes it to Glific without the dispatcher needing a
    separate eager push. Defaults to "" for backward compatibility with any
    caller that still passes only `step_number`.

    CR-009 follow-up (2026-05-23): re-arm next_action_at + next_action_type
    so the dispatcher fires the NEXT escalation step. Pre-fix, the
    dispatcher's atomic claim cleared next_action_at on row pickup, and T2
    failed to re-arm — leaving the chain stuck at step 1 forever. Mirrors
    T4's existing re-arm pattern. `next_hours` is the wait before the next
    step fires (the current step's hours_after_previous, matching T4's
    semantic). If None, defaults to 24h so legacy callers (tests / one-off
    admin invocations) still produce a sensible schedule.
    """
    return transition(pe, STATE_NORMAL_ESCALATION, trigger_source, {
        "current_escalation_step": step_number,
        "current_escalation_type": escalation_type,
        "journey_label": LABEL_CONTENT_DELIVERED,
        "next_action_at": add_to_date(now_datetime(), hours=float(next_hours)),
        "next_action_type": ACTION_ESCALATION,
    })


# ── T3: Submission during escalation ──────────────────────
def t3_escalation_submission(pe, points=0, trigger_source="flow_callback"):
    """T3: normal_escalation → submitted_awaiting_feedback.

    CR-002 v2: sets the sticky `weekly_submission_done` flag and increments
    `current_streak` + `special_gems`. Streak/gems increment AT SUBMISSION
    TIME, not deferred to T19.

    CR-003 follow-up (2026-05-13): also clears the grace clock fields
    (`in_grace_window`, `grace_window_end_at`, `grace_window_start`). A
    primary submission ends the week's grace window even if the student
    submitted before the dispatcher escalated all the way to grace_waiting.

    CR-007 follow-up (2026-05-22): `points` is ignored — submission point
    awards run later in feedback_consumer_hook via atomic SQL. See
    t7_core_submission's docstring for the L-011 rationale. DO NOT re-add
    `+ points` reads on the point-stream columns.

    NOTE: submission_count is owned by save_submission._try_claim_primary
    (atomic claim); state-machine transitions no longer bump it — see
    task #80 / audit 2026-05-15. Removing the bump here prevents the
    double-increment that occurred when _try_claim_primary's UPDATE +
    this transition's update both incremented the column.
    """
    # CR-007 follow-up (2026-05-22): points parameter intentionally unused.
    del points  # silence linter; preserve signature
    return transition(pe, STATE_SUBMITTED_AWAITING, trigger_source, {
        "journey_label": LABEL_SUBMITTED,
        "last_submission_at": now_datetime(),
        # CR-002 v2: streak/gems/sticky flag set explicitly. Point-stream
        # columns owned by the feedback hook's atomic SQL bump.
        "weekly_submission_done": 1,
        "current_streak": (pe.current_streak or 0) + 1,
        "special_gems": (pe.special_gems or 0) + 1,
        # CR-003 follow-up: clear grace state on any primary submission.
        "in_grace_window": 0,
        "grace_window_end_at": None,
        "grace_window_start": None,
        "next_action_at": None,
        "next_action_type": "",
    })


# ── T4: Next escalation step ─────────────────────────────
def t4_next_escalation_step(pe, step_number, next_hours=24, escalation_type="", trigger_source="scheduler"):
    """T4: normal_escalation → normal_escalation (next step).

    CR-003 follow-up: writes `current_escalation_type` so the standard
    contact-field sync pushes it to Glific. Defaults to "" for backward
    compatibility.
    """
    return transition(pe, STATE_NORMAL_ESCALATION, trigger_source, {
        "current_escalation_step": step_number,
        "current_escalation_type": escalation_type or "",
        "next_action_at": add_to_date(now_datetime(), hours=next_hours),
        "next_action_type": ACTION_ESCALATION,
    })


# ── T5: Escalation exhausted + some activity → grace ─────
def t5_escalation_to_grace(pe, trigger_source="scheduler"):
    """T5: normal_escalation → grace_waiting (escalation steps exhausted).

    CR-003 follow-up (2026-05-13): the grace clock is now armed by the
    activity-points handler on the week's first VideoClass completion
    (atomic CASE WHEN). T5 PRESERVES whatever clock is currently set —
    it does not reset it. The PE just enters the dead-air tail state;
    the `grace_check` scheduler action set here fires at grace_window_end_at
    and routes via `handle_grace_check`. If pe.grace_window_end_at is somehow
    None (legacy PE that never watched a VideoClass this week, or a PE that
    pre-dates the 2026-05-13 follow-up), we defensively arm the clock here
    using the batch grace duration so the dispatcher schedule is valid.
    """
    # Defensive: PE without an armed clock — happens if the student never
    # watched a VideoClass this week and somehow reached escalation exhaust
    # via a non-content path, or for legacy pre-follow-up PEs.
    if not pe.grace_window_end_at:
        grace_updates = _defensive_grace_clock_updates(pe)
    else:
        grace_updates = {"in_grace_window": 1}

    grace_updates.update({
        "journey_label": LABEL_GRACE_WINDOW,
        # The grace_check action is scheduled for the existing grace_window_end_at.
        "next_action_at": pe.grace_window_end_at or grace_updates.get("grace_window_end_at"),
        "next_action_type": ACTION_GRACE_CHECK,
    })
    return transition(pe, STATE_GRACE_WAITING, trigger_source, grace_updates)


# ── T6: REMOVED per CR-006 (2026-05-15) — kept as deprecation stub ──
def t6_escalation_to_remedial(pe, week_rule=None, trigger_source="scheduler"):
    """T6: DEPRECATED per CR-006 (2026-05-15).

    Previously transitioned `normal_escalation → remedial_content_delivery`
    when the escalation chain exhausted with zero submissions. Removed
    because remedial is now reserved for failed-feedback students (T6b,
    CR-004). Zero-submission exhausters go to grace via T5, then drop at
    grace end per CR-001.

    This stub raises so any hidden caller surfaces loudly. The function
    will be removed entirely in a follow-up cleanup CR.
    """
    frappe.throw(
        "t6_escalation_to_remedial is removed per CR-006. "
        "Use t5_escalation_to_grace for escalation exhaustion, "
        "or t6b_failed_feedback_to_remedial for failed AI feedback."
    )


# ── T6b: Failed AI feedback → remedial (CR-004) ──
def t6b_failed_feedback_to_remedial(pe, week_rule=None, trigger_source="microservice"):
    """T6b: submitted_awaiting_feedback → week_completed.

    CR-004. Fires when an AI-graded submission comes back with
    `Submission.submission_validity == "Invalid"` AND
    `WeekRule.submission_validation_enabled == 1` (current contract per
    `feedback_consumer_hook.on_feedback_ready` and architecture.md §9.3).
    Routes the student into the remedial content track for the SAME week.
    Does NOT clawback points awarded by T7/T9 — the submission counted;
    only the LEARNING outcome failed.

    Note: CR-004 originally specified branching on
    `Submission.result_status == 'failed'`; the routing input was later
    switched to `submission_validity` to separate the AI-scoring signal
    (`result_status` → points) from the validation-gate signal
    (`submission_validity` → routing). `result_status` still governs the
    POINT award via `_compute_submission_points` (Failed/Flagged → 0).

    Differences vs T6:
    - Does NOT reset current_escalation_step / current_escalation_type
      (the student never escalated; those counters are already at defaults).
    - Source state is submitted_awaiting_feedback, not normal_escalation.
    - trigger_source defaults to "microservice" (matches the FeedbackConsumer
      caller; T6 defaults to "scheduler").
    """
    updates = {
        "journey_label": LABEL_FEEDBACK_DELIVERED,
        "current_path": PATH_REMEDIAL,
        "next_action_at": now_datetime(),
        "next_action_type": ACTION_WEEK_ADVANCEMENT,
    }
    if week_rule:
        updates["current_expected_submission_type"] = week_rule.get("expected_submission_type", "")

    log_event(pe, "path_changed", PATH_CORE, PATH_REMEDIAL, trigger_source)

    return transition(pe, STATE_WEEK_COMPLETED, trigger_source, updates)


# ── T7: First submission (Core, from content delivery) ────
def t7_core_submission(pe, points=0, trigger_source="flow_callback"):
    """T7: normal_content_delivery → submitted_awaiting_feedback.

    CR-002 v2: sets the sticky `weekly_submission_done` flag and increments
    `current_streak` + `special_gems`. Streak/gems increment AT SUBMISSION
    TIME, not deferred to T19.

    CR-003 follow-up (2026-05-13): also clears the grace clock fields
    (`in_grace_window`, `grace_window_end_at`, `grace_window_start`). A
    primary submission ends the week's grace window (which the activity-points
    handler armed when the student watched their first VideoClass).

    CR-007 follow-up (2026-05-22): the `points` parameter is retained for
    signature compatibility but IGNORED. Submission point awards are now
    owned by `feedback_consumer_hook._award_submission_points_atomic`
    (atomic COALESCE SQL), which runs after AI validation. The previous
    `(pe.X or 0) + points` reads on total_points / total_submission_points
    / weekly_submission_points opened a read-modify-write window that
    clobbered concurrent atomic bumps from activity_points / quiz_points
    / the feedback hook (L-011 violation). DO NOT re-add those lines.

    NOTE: submission_count is owned by save_submission._try_claim_primary
    (atomic claim); state-machine transitions no longer bump it — see
    task #80 / audit 2026-05-15.
    """
    # CR-007 follow-up (2026-05-22): points parameter intentionally unused.
    # The atomic award fires later in feedback_consumer_hook.
    del points  # silence linter; preserve signature
    return transition(pe, STATE_SUBMITTED_AWAITING, trigger_source, {
        "journey_label": LABEL_SUBMITTED,
        "last_submission_at": now_datetime(),
        # CR-002 v2: streak/gems/sticky flag set explicitly (not additive on
        # a stale read). Point-stream columns are owned by the feedback
        # hook's atomic SQL bump; do NOT include them here.
        "weekly_submission_done": 1,
        "current_streak": (pe.current_streak or 0) + 1,
        "special_gems": (pe.special_gems or 0) + 1,
        # CR-003 follow-up: clear grace state on any primary submission.
        "in_grace_window": 0,
        "grace_window_end_at": None,
        "grace_window_start": None,
        "next_action_at": None,
        "next_action_type": "",
    })


# ── T8: Start Remedial escalation ────────────────────────
def t8_start_remedial_escalation(pe, step_number=1, escalation_type="",
                                  next_hours=None, trigger_source="scheduler"):
    """T8: remedial_content_delivery → remedial_escalation.

    CR-003 follow-up: writes `current_escalation_type` so the standard
    contact-field sync pushes it to Glific. Defaults to "" for backward
    compatibility.

    CR-009 follow-up (2026-05-23): re-arm next_action_at + next_action_type
    so the dispatcher fires the NEXT remedial escalation step. Mirror of
    the T2 fix — pre-CR-009 the Remedial chain was also stuck at step 1.
    """
    return transition(pe, STATE_REMEDIAL_ESCALATION, trigger_source, {
        "current_escalation_step": step_number,
        "current_escalation_type": escalation_type or "",
        "next_action_at": add_to_date(now_datetime(), hours=float(next_hours)),
        "next_action_type": ACTION_ESCALATION,
    })


# ── T9: Remedial submission ──────────────────────────────
def t9_remedial_submission(pe, points=0, trigger_source="flow_callback"):
    """T9: remedial_content_delivery → submitted_awaiting_feedback.

    CR-002 v2: sets the sticky `weekly_submission_done` flag and increments
    `current_streak` + `special_gems`. Streak/gems increment AT SUBMISSION
    TIME, not deferred to T19.

    CR-003 follow-up (2026-05-13): also clears the grace clock fields
    (`in_grace_window`, `grace_window_end_at`, `grace_window_start`). A
    primary submission ends the week's grace window.

    CR-007 follow-up (2026-05-22): `points` is ignored — submission point
    awards run later in feedback_consumer_hook via atomic SQL. See
    t7_core_submission's docstring for the L-011 rationale. DO NOT re-add
    `+ points` reads on the point-stream columns.

    NOTE: submission_count is owned by save_submission._try_claim_primary
    (atomic claim); state-machine transitions no longer bump it — see
    task #80 / audit 2026-05-15.
    """
    # CR-007 follow-up (2026-05-22): points parameter intentionally unused.
    del points  # silence linter; preserve signature
    return transition(pe, STATE_SUBMITTED_AWAITING, trigger_source, {
        "journey_label": LABEL_SUBMITTED,
        "last_submission_at": now_datetime(),
        # CR-002 v2: streak/gems/sticky flag set explicitly. Point-stream
        # columns owned by the feedback hook's atomic SQL bump.
        "weekly_submission_done": 1,
        "current_streak": (pe.current_streak or 0) + 1,
        "special_gems": (pe.special_gems or 0) + 1,
        # CR-003 follow-up: clear grace state on any primary submission.
        "in_grace_window": 0,
        "grace_window_end_at": None,
        "grace_window_start": None,
        "next_action_at": None,
        "next_action_type": "",
    })


# ── T10: Next Remedial escalation step ───────────────────
def t10_next_remedial_escalation(pe, step_number, next_hours=24, escalation_type="", trigger_source="scheduler"):
    """T10: remedial_escalation → remedial_escalation (next step).

    CR-003 follow-up: writes `current_escalation_type` so the standard
    contact-field sync pushes it to Glific. Defaults to "" for backward
    compatibility.
    """
    return transition(pe, STATE_REMEDIAL_ESCALATION, trigger_source, {
        "current_escalation_step": step_number,
        "current_escalation_type": escalation_type or "",
        "next_action_at": add_to_date(now_datetime(), hours=next_hours),
        "next_action_type": ACTION_ESCALATION,
    })


# ── T11: All Remedial escalation exhausted → grace ──────
def t11_remedial_to_grace(pe, trigger_source="scheduler"):
    """T11: remedial_escalation → grace_waiting.

    CR-003 follow-up (2026-05-13): mirror of T5 for the remedial path. The
    grace clock is armed by activity-points on the week's first VideoClass
    completion; T11 preserves it. Defensive backfill for legacy PEs as in T5.
    """
    if not pe.grace_window_end_at:
        grace_updates = _defensive_grace_clock_updates(pe)
    else:
        grace_updates = {"in_grace_window": 1}

    grace_updates.update({
        "journey_label": LABEL_GRACE_WINDOW,
        "next_action_at": pe.grace_window_end_at or grace_updates.get("grace_window_end_at"),
        "next_action_type": ACTION_GRACE_CHECK,
    })
    return transition(pe, STATE_GRACE_WAITING, trigger_source, grace_updates)


# ── T12: AI feedback generated ──────────────────────────
def t12_feedback_ready(pe, trigger_source="microservice"):
    """T12: submitted_awaiting_feedback → feedback_ready.

    FeedbackConsumer handles the Glific notification directly (label="feedback").
    When that Glific flow completes, the flow callback triggers T13
    (feedback_ready → week_completed → week_advancement).

    No dispatcher action is scheduled here — the Glific callback drives the
    next transition. If the callback never fires, a manual check or admin
    intervention is needed.
    """
    return transition(pe, STATE_FEEDBACK_READY, trigger_source, {
        "next_action_at": None,
        "next_action_type": "",
    })


# ── T13: Feedback delivered → week_completed ─────────────
def t13_feedback_delivered(pe, trigger_source="flow_callback"):
    """T13: feedback_ready → week_completed."""
    return transition(pe, STATE_WEEK_COMPLETED, trigger_source, {
        "journey_label": LABEL_FEEDBACK_DELIVERED,
        "next_action_at": now_datetime(),
        "next_action_type": ACTION_WEEK_ADVANCEMENT,
    })


# ── T14 / T19: Week advance (normal) ────────────────────
#
# Naming note: this function is named `t14_week_advance` for historical
# reasons; the architecture-doc vocabulary calls it T19. CR-002 v2 keeps
# the function name (per CR §"T19 week-advance — extended" — do NOT rename).
#
# CR-003 follow-up (2026-05-13): T19 NO LONGER arms the grace clock.
# The clock is armed naturally each week by `activity_points.handle_content_log`
# when the student completes the week's FIRST VideoClass — the atomic
# Postgres CASE WHEN trick in that handler reads `weekly_video_done = 0`
# (which T19 resets below) and re-arms the clock on the 0→1 flip. T19's
# job here is just the weekly reset; the next VideoClass watch drives the
# re-arm.
def t14_week_advance(pe, new_week, week_rule=None, trigger_source="scheduler"):
    """T19: week_completed → normal_content_delivery (next week).

    CR-002 v2 extends T19 in two phases:

    Phase 1 — compute streak/gem update from the two sticky weekly flags:
        if `weekly_video_done = 1 AND weekly_submission_done = 0`:
            current_streak → 0
            special_gems   → max(0, special_gems - 1)
        else:
            both unchanged.

    Phase 2 — roll up weekly points into cumulative totals via
    `calculate_week_advance_rollup`. Advance the week. Reset `weekly_video_done`
    to 0 (the lazy-reset trigger for next week's first video). Apply streak /
    gem updates.

    CR-008 lazy reset (2026-05-23): T14 NO LONGER zeros these fields —
        weekly_activity_points, weekly_quiz_points, weekly_submission_points,
        bonus_quiz_points, weekly_submission_done, quiz_completed,
        submission_count
    The first VideoClass of the new week (gated on weekly_video_done = 0)
    atomically resets the gamification + sticky fields. submission_count is
    intentionally NOT reset — it's a lifetime program counter. This keeps
    Glific showing the student's W1 reward through the inter-week gap until
    they actively start W2 content. See activity_points.award_activity_points.

    Gem floor is enforced in Python (`max(0, ...)`) because the value is
    computed before the UPDATE. SQL `GREATEST(0, ...)` is not needed — the
    value is plain-int by the time we write it.
    """
    tier = TIER_BY_WEEK.get(new_week, DEFAULT_TIER)

    # ── Phase 2: roll up weekly points + build the reset update dict ─────
    # The rollup reads CURRENT weekly_* values (still showing this just-completed
    # week's earnings) and adds them to the corresponding total_* columns.
    # After this T14, the weekly_* values remain visible on Glific (lazy reset);
    # the next VideoClass of the new week wipes them via
    # activity_points.award_activity_points' CASE WHEN gates.
    rollup_updates = calculate_week_advance_rollup(pe)

    updates = {
        "journey_label": LABEL_WEEK_ADVANCED,
        "current_week": new_week,
        "current_path": PATH_CORE,
        "current_tier": tier,
        # CR-008 (2026-05-23): submission_count / quiz_completed are NOT
        # reset here — submission_count accumulates as a lifetime counter,
        # quiz_completed flips back via the lazy-reset in activity_points
        # on the next week's first VideoClass.
        "current_escalation_step": 0,
        "current_escalation_type": "",
        # CR-005 (2026-05-15): content_delivery is now batch-triggered via the
        # weekly Tuesday cron (`weekly_content_delivery_trigger`) firing
        # SP_Content_Delivery against the BPR's `main` Glific collection. T14
        # no longer arms `next_action_type = ACTION_CONTENT_DELIVERY` — that
        # would re-introduce the per-PE dispatcher path we're moving off of.
        # `handle_content_delivery` in pe_dispatcher.py is preserved as an
        # operator escape hatch (see CR-005 §4) but is no longer reached in
        # the normal weekly cadence.
        "next_action_at": None,
        "next_action_type": "",
        # CR-002 v2: streak/gem update from rollup + cumulative totals.
        **rollup_updates,
        # CR-008 lazy reset (2026-05-23): the following fields are NOT zeroed
        # here — the first VideoClass of the new week wipes them atomically:
        #   weekly_activity_points, weekly_quiz_points, weekly_submission_points,
        #   bonus_quiz_points, weekly_submission_done, quiz_completed
        # weekly_video_done MUST stay at 0 here — it's the signal that gates
        # the lazy-reset SQL in activity_points.award_activity_points.
        "weekly_video_done": 0,
    }

    # ── CR-003 follow-up (2026-05-13): grace re-arm removed ──
    # T19 no longer arms the grace clock. The activity-points handler will
    # re-arm it on the new week's first VideoClass completion via atomic
    # CASE WHEN (now that `weekly_video_done` is reset to 0 above, the next
    # video watch flips it 0→1 and the same UPDATE seeds the new clock).
    # Grace state from the prior week is intentionally NOT cleared here —
    # the only ways a PE leaves T19 with a non-null grace_window_end_at are:
    #   (a) the student submitted (T7/T9/T17/T3 already cleared the fields), or
    #   (b) the student went silent through escalation and the dispatcher
    #       fired t17_grace_expired (PE is now in program_dropped, not here).
    # Either way the fields are already in the correct state by the time T19
    # runs, so no explicit clear is needed.

    if week_rule:
        updates["current_expected_submission_type"] = week_rule.get("expected_submission_type", "")

    log_event(pe, "week_advanced", str(pe.current_week), str(new_week), trigger_source)

    return transition(pe, STATE_NORMAL_CONTENT, trigger_source, updates)


# ── T15: Binge limit hit ────────────────────────────────
def t15_binge_pause(pe, next_open_date=None, trigger_source="scheduler"):
    """T15: week_completed → paused_binge.

    Task #14 (2026-05-28, Esc R5): also set `weekly_video_done = 0` so
    the next first-VideoClass of a new week re-arms the CR-008 lazy reset.
    Without this, a binge pause that spans a week boundary leaves the
    student in next-week's calendar week with weekly_* values still
    carrying the prior week's accumulated totals, until the next
    activity_points handler can flip the gate (which it won't because
    `weekly_video_done` is already 1).
    """
    return transition(pe, STATE_PAUSED_BINGE, trigger_source, {
        "journey_label": LABEL_PAUSED,
        "program_status": PROGRAM_PAUSED,
        "pause_reason": PAUSE_BINGE_LIMIT,
        "next_action_at": next_open_date,
        "next_action_type": ACTION_PAUSE_CHECK,
        # Arm CR-008 lazy reset on next week's first VideoClass watch.
        "weekly_video_done": 0,
    })


# ── T16: Program completed ──────────────────────────────
def t16_program_completed(pe, trigger_source="scheduler"):
    """T16: week_completed → program_completed."""
    return transition(pe, STATE_PROGRAM_COMPLETED, trigger_source, {
        "journey_label": LABEL_COMPLETED,
        "program_status": PROGRAM_COMPLETED,
        "next_action_at": None,
        "next_action_type": "",
    })


# ── T17: Grace submission ───────────────────────────────
def t17_grace_submission(pe, points=0, trigger_source="flow_callback"):
    """T17: grace_waiting → submitted_awaiting_feedback.

    CR-002 v2: sets the sticky `weekly_submission_done` flag and increments
    `current_streak` + `special_gems`. Streak/gems increment AT SUBMISSION
    TIME, not deferred to T19.

    CR-007 follow-up (2026-05-22): `points` is ignored — submission point
    awards run later in feedback_consumer_hook via atomic SQL. See
    t7_core_submission's docstring for the L-011 rationale. DO NOT re-add
    `+ points` reads on the point-stream columns.

    NOTE: submission_count is owned by save_submission._try_claim_primary
    (atomic claim); state-machine transitions no longer bump it — see
    task #80 / audit 2026-05-15.
    """
    # CR-007 follow-up (2026-05-22): points parameter intentionally unused.
    del points  # silence linter; preserve signature
    return transition(pe, STATE_SUBMITTED_AWAITING, trigger_source, {
        "journey_label": LABEL_SUBMITTED,
        "in_grace_window": 0,
        "grace_window_start": None,
        "grace_window_end_at": None,
        "last_submission_at": now_datetime(),
        # CR-002 v2: streak/gems/sticky flag set explicitly. Point-stream
        # columns owned by the feedback hook's atomic SQL bump.
        "weekly_submission_done": 1,
        "current_streak": (pe.current_streak or 0) + 1,
        "special_gems": (pe.special_gems or 0) + 1,
        "next_action_at": None,
        "next_action_type": "",
    })


# ── T17 (grace expired) — DELETED in CR-001 / RENAMED in CR-003 ─
#
# Pre-CR-003: T17b fired SP_Grace_Reminder on days 7/11/13 of the window,
# then T18 transitioned the PE to STATE_PAUSED_NO_ACTIVITY and scheduled
# the re-engagement loop. CR-003 retires both: grace reminders are gone
# (escalation steps within the week ARE the reminders) and the
# paused_no_activity state is retired (PEs drop directly at grace expiry).
#
# T17b removed; the dispatcher's `handle_grace_reminder` is also removed.

# ── T17: Grace expired → program_dropped (CR-003) ───────
def t17_grace_expired(pe, trigger_source="scheduler"):
    """T17: grace_waiting → program_dropped.

    CR-003 §"Grace window — new semantics": on grace expiry, the PE
    transitions directly to STATE_PROGRAM_DROPPED. Terminal. No re-engagement
    loop, no paused_no_activity hop. drop_reason = 'grace_expired'. The
    `program_dropped` event is logged for the funnel.

    Pre-CR-003 this slot was T18, which moved to STATE_PAUSED_NO_ACTIVITY and
    scheduled re_engagement. T18 is retained as an alias below for any callers
    that import by the old name; both point to the same function so existing
    grep'ed call sites still work during the cutover window.
    """
    log_event(pe, "program_paused", trigger_source=trigger_source,
              details={"reason": "grace_expired"})
    return transition(pe, STATE_PROGRAM_PAUSED, trigger_source, {
        "journey_label": LABEL_PAUSED,
        "program_status": PROGRAM_PAUSED,
        "drop_reason": "grace_expired",
        "in_grace_window": 0,
        "next_action_at": None,
        "next_action_type": "",
    })


# Backward-compat alias: callers that imported the old T18 name still work.
# CR-003 retires the function semantics but keeps the name resolvable during
# the cutover window. Delete after one release cycle once no caller imports
# `t18_grace_expired` directly. Tests in test_pe_dispatcher.py still patch
# `state_machine.t18_grace_expired` — they continue working via this alias.
t18_grace_expired = t17_grace_expired


# ── T19 / T20 (reactivate from pause) — DELETED in CR-003 ─
#
# Pre-CR-003: a PE in paused_no_activity could receive an inbound message
# and transition back to normal_content_delivery (T19) or
# remedial_content_delivery (T20). CR-003 removes the paused_no_activity
# state entirely — students drop at grace expiry and re-engagement is
# inbound-only via SP_Incoming_Router (Glific reads program_status='dropped'
# and routes a rejoin path that goes through `reactivate_student`).
#
# `t19_reactivate_core` / `t20_reactivate_remedial` removed. Note the T19
# label is now reused in the architecture-doc vocabulary for the week-advance
# function (`t14_week_advance` keeps its historical function name per CR-002 v2;
# see the comment block at line ~458).


# ── T21: Binge-paused resumes ───────────────────────────
def t21_binge_resume(pe, trigger_source="scheduler"):
    """T21: paused_binge → normal_content_delivery (calendar advanced)."""
    return transition(pe, STATE_NORMAL_CONTENT, trigger_source, {
        "journey_label": LABEL_RESUMED,
        "program_status": PROGRAM_ACTIVE,
        "pause_reason": "",
        "next_action_at": now_datetime(),
        "next_action_type": ACTION_WEEK_ADVANCEMENT,
    })

def t21_a_resume_paused(pe, trigger_source="scheduler"):
    """T21: paused → normal_content_delivery (calendar advanced)."""
    return transition(pe, STATE_NORMAL_CONTENT, trigger_source, {
        "journey_label": LABEL_RESUMED,
        "program_status": PROGRAM_ACTIVE,
        "pause_reason": "",
        "drop_reason": "",
        "in_grace_window": 0,
        "grace_window_start": None,
        "grace_window_end_at": None,
        "next_action_at": now_datetime(),
        "next_action_type": ACTION_CONTENT_DELIVERY,
    })


# ── T22: Duplicate submission (no state change) ─────────
def t22_duplicate_submission(pe, trigger_source="flow_callback"):
    """T22: submitted_awaiting_feedback stays same. Log only."""
    log_event(pe, "submission_received", trigger_source=trigger_source,
              details={"is_primary": False, "duplicate": True})
    return True


# ── T23: System-initiated auto-drop ─────────────────────
def t23_auto_drop(pe, reason, trigger_source="dispatcher"):
    """T23: ANY → program_dropped, system-initiated.

    `reason` must be one of: 'delivery_failure', 'admin', 'manual'.
    NOT for grace expiry — that's t17_grace_expired with
    `drop_reason='grace_expired'`.

    CR-003: the `reengagement_exhausted` trigger was retired. Only
    `delivery_failure_count >= MAX_DELIVERY_FAILURES` chains to T23 today.
    `_record_delivery_failure` in pe_dispatcher.py is the helper that
    counts failures and fires this transition at the threshold.

    Idempotent on already-terminal PEs (completed / dropped) — the early
    return prevents a double `program_dropped` log entry and a redundant
    Glific contact-field sync.
    """
    if pe.program_status in (PROGRAM_COMPLETED, PROGRAM_DROPPED):
        return  # already terminal, no-op

    log_event(pe, "program_dropped", trigger_source=trigger_source,
              details={"reason": reason})

    return transition(pe, STATE_PROGRAM_DROPPED, trigger_source, {
        "program_status": PROGRAM_DROPPED,
        "journey_label": LABEL_DROPPED,
        "drop_reason": reason,
        "in_grace_window": 0,
        "grace_window_end_at": None,
        "grace_window_start": None,
        "next_action_at": None,
        "next_action_type": "",
    })


# ── T24: Admin drops student ────────────────────────────
def t24_admin_drop(pe, trigger_source="admin"):
    """T24: ANY → program_dropped."""
    return transition(pe, STATE_PROGRAM_DROPPED, trigger_source, {
        "journey_label": LABEL_DROPPED,
        "program_status": PROGRAM_DROPPED,
        "next_action_at": None,
        "next_action_type": "",
    })


# ── T25: Delivery failure (no state change) ─────────────
def t25_delivery_failure(pe, flow_name, trigger_source="scheduler"):
    """T25: ANY → same state. Increment failure count.

    Task #19 (2026-05-28, L-011): atomic COALESCE increment (P-002)
    replaces the prior `pe.delivery_failure_count = ... + 1; pe.save()`
    sequence. The Python-read-then-save would write every PE column from
    a possibly-stale doc, clobbering concurrent atomic SQL bumps from
    the activity_points / quiz_points / feedback_consumer_hook award
    handlers. The atomic SQL UPDATE here is race-safe — concurrent
    bumps on OTHER columns proceed independently.

    Mirrors the pattern already used in `pe_dispatcher._record_delivery_failure`.
    """
    frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET delivery_failure_count = COALESCE(delivery_failure_count, 0) + 1
         WHERE name = %s
        """,
        (pe.name,),
    )
    pe.reload()

    log_event(pe, "delivery_failed", trigger_source=trigger_source,
              details={"flow_name": flow_name,
                       "failure_count": pe.delivery_failure_count})
    return True


# ════════════════════════════════════════════════════════════
# SUBMISSION DISPATCH
# ════════════════════════════════════════════════════════════

def apply_submission_transition(pe, points=0, trigger_source="flow_callback"):
    """
    Apply the correct submission transition based on current state.
    Returns (transition_id, success).
    """
    state = pe.resolved_flow_state

    if state == STATE_NORMAL_CONTENT:
        t7_core_submission(pe, points, trigger_source)
        return "T7", True

    if state == STATE_NORMAL_ESCALATION:
        t3_escalation_submission(pe, points, trigger_source)
        return "T3", True

    if state == STATE_REMEDIAL_CONTENT:
        t9_remedial_submission(pe, points, trigger_source)
        return "T9", True

    if state == STATE_REMEDIAL_ESCALATION:
        t9_remedial_submission(pe, points, trigger_source)
        return "T9", True

    if state == STATE_GRACE_WAITING:
        t17_grace_submission(pe, points, trigger_source)
        return "T17", True

    if state == STATE_SUBMITTED_AWAITING:
        t22_duplicate_submission(pe, trigger_source)
        return "T22", True

    # Terminal or paused — should not receive submissions
    return None, False


# ════════════════════════════════════════════════════════════
# HELPER: Get PE for student
# ════════════════════════════════════════════════════════════

def get_active_pe(student_id, batch_name=None):
    """
    Get the active ProgramEnrollment for a student.
    Returns PE doc or None.

    A student CAN have multiple ProgramEnrollment rows for the same batch
    over time (e.g. dropped then re-enrolled, or program_completed then
    re-enrolled for a follow-up cohort). When that happens, return the
    one with the most recent `modified` timestamp — that's the row whose
    state was last advanced by the dispatcher / state machine, i.e. the
    "live" enrollment the student is currently progressing through.

    Dropped enrollments are excluded by the program_status filter
    (only ACTIVE + PAUSED are considered). Among those, `modified desc`
    is the correct tie-breaker because `creation` doesn't change after
    insert, so a stale older PE could outrank a recently-active newer
    PE under `creation desc` ordering.
    """
    filters = {
        "student": student_id,
        "program_status": ["in", [PROGRAM_ACTIVE, PROGRAM_PAUSED]],
    }
    if batch_name:
        filters["batch"] = batch_name

    pe_name = frappe.db.get_value("ProgramEnrollment", filters, "name",
                                   order_by="modified desc")
    if pe_name:
        return frappe.get_doc("ProgramEnrollment", pe_name)
    return None
