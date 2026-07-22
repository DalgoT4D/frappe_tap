"""
Per-PE Scheduler Dispatcher
tap_lms/summer_program/pe_dispatcher.py

The per-PE dispatcher is the "brain" of the time-based automation layer.
It runs every 1-2 minutes, queries all PEs with overdue next_action_at,
and routes each one to the appropriate handler based on next_action_type.

Unlike the collection-based daily scheduler (scheduler.py) which triggers
flows on entire groups, this dispatcher handles individual student timelines.

Register in hooks.py:
    scheduler_events = {
        "cron": {
            "*/1 * * * *": [
                "tap_lms.summer_program.pe_dispatcher.process_program_actions",
            ]
        }
    }

Scheduler partition: this dispatcher processes PEs whose next_action_type is
an individual-timer action (feedback_timeout, grace_reminder, pause_check,
etc.). Synchronous events shared across many students (content_delivery,
week_advancement at batch start) are handled by collection-mode batchers
when those exist. Partition is by next_action_type, not by Batch.
"""
import frappe
from frappe.utils import now_datetime, get_datetime, add_to_date

from tap_lms.summer_program.constants import (
    BPR_ACTIVE,
    PROGRAM_ACTIVE,
    PROGRAM_PAUSED,
    TERMINAL_STATES,
    ACTION_CONTENT_DELIVERY,
    ACTION_ESCALATION,
    ACTION_FEEDBACK_TIMEOUT,
    ACTION_WEEK_ADVANCEMENT,
    ACTION_GRACE_CHECK,
    ACTION_PAUSE_CHECK,
    ACTION_FLOW_FIELD_MAP,
    STATE_NORMAL_CONTENT, STATE_NORMAL_ESCALATION,
    STATE_REMEDIAL_CONTENT, STATE_REMEDIAL_ESCALATION,
    STATE_SUBMITTED_AWAITING,
    STATE_GRACE_WAITING, STATE_WEEK_COMPLETED,
    STATE_PAUSED_BINGE,
    PATH_CORE,
)
from tap_lms.summer_program.event_log import log_event


# ════════════════════════════════════════════════════════════
# DISPATCHER (entry point)
# ════════════════════════════════════════════════════════════

# Max PEs to process per dispatch cycle. Bumped 500 → 1000 (task #15)
# for the 100K-student MVP target: a week-boundary T19 burst can ship up
# to 100K PEs into the "due" set within a minute, and 4 parallel workers
# at LIMIT 1000 × 1-min cron = 240K/hour drains that burst in ~25 min.
# See architecture.md §8.8 sizing math and ADR-003 audit log (2026-05-13).
DISPATCH_BATCH_SIZE = 1000


def process_program_actions():
    """
    Main dispatcher entry point. Called every 1 minute by Frappe scheduler
    (cron `*/1 * * * *`). v4.1 §7.2 spec; architecture.md §8.1 / §8.8.

    Finds all PEs where:
      - next_action_at <= now
      - program_status is active OR paused (paused PEs need pause_check
        to be reachable for binge-resume; B3 fix)
      - next_action_type is a per-PE individual-timer action

    Routes each PE to the appropriate handler based on next_action_type.

    Renamed from `dispatch_pending_actions` (task #15, 2026-05-13). The
    legacy name is preserved below as a thin alias for one release cycle
    so any cron entry that wasn't yet updated keeps working through the
    cutover.

    Note: there is no batch-level partition. Collection-mode batchers (when
    built) filter on different next_action_type values; this dispatcher and
    those batchers are partitioned by action type, not by Batch. See
    architecture §8.
    """
    now = now_datetime()

    # SELECT candidate PEs with FOR UPDATE SKIP LOCKED so multiple parallel
    # workers can run this loop without contending for the same rows
    # (architecture §8.1, pattern P-001). The SKIP LOCKED clause makes each
    # worker take a different slice. We also capture journey_label here so
    # the atomic claim below can guard against state moving under us.
    #
    # program_status filter includes both ACTIVE and PAUSED so paused-state
    # handlers (handle_pause_check for binge-resume) are reachable. Earlier
    # ACTIVE-only filter excluded them entirely (B3). Post-CR-003 the
    # re-engagement handler is gone; only binge-pause keeps PAUSED status
    # alive on the read.
    # Per L-005: avoid `= ANY(%s)` with a list-in-tuple — Frappe's
    # modify_values mangles a 2-element list into a Postgres record
    # `('active','paused')` instead of a `text[]` array, producing the
    # "op ANY/ALL (array) requires array on right side" error. Use flat
    # `IN (%s, %s)` with scalar params instead — same semantics, deterministic.
    candidates = frappe.db.sql(
        """
        SELECT pe.name, pe.next_action_type, pe.next_action_at,
               pe.batch, pe.student, pe.glific_id,
               pe.resolved_flow_state, pe.current_week,
               pe.current_escalation_step, pe.current_path,
               pe.journey_label
        FROM `tabProgramEnrollment` pe
        WHERE pe.next_action_at IS NOT NULL
          AND pe.next_action_at <= %s
          AND pe.program_status IN (%s, %s)
          AND pe.next_action_type != ''
        ORDER BY pe.next_action_at ASC
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (now, PROGRAM_ACTIVE, PROGRAM_PAUSED, DISPATCH_BATCH_SIZE),
        as_dict=True,
    )

    processed = 0
    skipped = 0
    errors = 0

    for pe_row in (candidates or []):
        # Atomic claim per P-001: clear next_action_at conditionally on the
        # journey_label still matching what we read. If 0 rows are updated,
        # another worker (or a flow callback) moved the state under us and
        # we must NOT dispatch — skip and continue. Postgres-specific
        # RETURNING NAME is the idempotency primitive; see L-001/L-018.
        #
        # Note: architecture §8.1 also calls for `last_dispatched_at = NOW()`
        # as an audit trail. That column doesn't exist on PE yet — file a
        # follow-up to add it via DocType UI if dispatcher debugging needs it.
        # The atomic-claim primitive works without it.
        claimed = frappe.db.sql(
            """
            UPDATE `tabProgramEnrollment`
            SET next_action_at = NULL
            WHERE name = %s
              AND journey_label = %s
              AND next_action_at IS NOT NULL
            RETURNING name
            """,
            (pe_row.name, pe_row.journey_label),
        )
        if not claimed:
            skipped += 1
            continue

        try:
            _dispatch_single(pe_row)
            processed += 1
        except Exception as e:
            errors += 1
            # Postgres aborts the entire transaction on the first failed
            # query. Without a rollback here, frappe.log_error itself fails
            # with InFailedSqlTransaction (it does its own SELECT to fetch
            # the Error Log doctype meta), which means the dispatcher
            # silently swallows BOTH the original error AND the log call.
            # Rolling back first ensures the log entry actually lands.
            # See 2026-05-19 incident: _get_week_rule schema-mismatch
            # crashed every cron tick for an hour with zero visible errors.
            try:
                frappe.db.rollback()
            except Exception:
                pass
            frappe.log_error(
                f"Dispatcher error for PE {pe_row.name} "
                f"(action={pe_row.next_action_type}): {str(e)}",
                "SP PE Dispatcher",
            )
            # Action already cleared by the atomic claim above; no retry loop.

    if processed or errors or skipped:
        frappe.db.commit()

    # Observability for 100K-scale operations. Operators monitor `claimed`
    # (throughput) and `queue_depth` (lag indicator) to decide when to scale
    # parallel workers. See architecture §8.8.
    # NOTE: uses `IN (%s, %s)` rather than `ANY(%s)` with a list parameter.
    # Frappe's `modify_values` wrapper converts a list-in-tuple to a Postgres
    # record `('active','paused')` instead of a text[] array when the params
    # tuple has only 2 entries (vs. the 3-entry candidates query above which
    # happens to bind correctly). Lesson learned via the `validate_archetype_config`
    # 500 (2026-05-14). The candidates SELECT above is safe because of its
    # parameter-count quirk; this 2-param query is not.
    try:
        queue_depth = frappe.db.sql("""
            SELECT COUNT(*)
              FROM "tabProgramEnrollment"
             WHERE next_action_at IS NOT NULL
               AND next_action_at <= %s
               AND program_status IN (%s, %s)
               AND next_action_type != ''
        """, (now, PROGRAM_ACTIVE, PROGRAM_PAUSED))[0][0]
    except Exception:
        queue_depth = -1  # log "unknown" rather than crash the tick

    frappe.logger("sp_dispatcher").info({
        "dispatcher": "process_program_actions",
        "claimed": processed,
        "skipped": skipped,
        "errors": errors,
        "queue_depth": queue_depth,
    })

    return {"dispatched": processed, "skipped": skipped, "errors": errors}


# Backward-compat alias (task #15, 2026-05-13). Resolves any code path or
# cron entry that still references the pre-rename name through one release
# cycle. Delete once `bench show-scheduler-events` confirms no remaining
# call site uses the old name.


def _dispatch_single(pe_row):
    """Route a single PE to its handler based on next_action_type."""
    action_type = pe_row.next_action_type
    handler = HANDLER_MAP.get(action_type)

    if not handler:
        frappe.log_error(
            f"Unknown action_type '{action_type}' for PE {pe_row.name}",
            "SP PE Dispatcher",
        )
        _clear_action(pe_row.name)
        return

    handler(pe_row)


def _clear_action(pe_name):
    """Clear next_action fields to prevent re-processing."""
    frappe.db.set_value(
        "ProgramEnrollment", pe_name,
        {"next_action_at": None, "next_action_type": ""},
        update_modified=False,
    )


# ════════════════════════════════════════════════════════════
# HANDLERS
# ════════════════════════════════════════════════════════════


# CR-005 (2026-05-15): preserved for future use; NOT reached in the normal flow.
# Weekly content delivery now fires via `weekly_content_delivery_trigger` on the
# BPR's `main` Glific collection — `t0_enrollment` and `t14_week_advance` no
# longer arm `ACTION_CONTENT_DELIVERY` on individual PEs. This handler stays in
# place as: (1) an operator escape hatch for per-PE re-delivery, (2) rollback
# safety, and (3) future per-student catch-up flows. See CR-005 §4.
def handle_content_delivery(pe_row):
    """
    Handler: content_delivery

    Triggers SP_Content_Delivery flow for this student.
    The flow handles the content display; on completion it calls
    update_flow_status which sets the next action (or escalation on timeout).

    After triggering, clears next_action since the flow callback
    will set the next one.

    CR-005: under normal operation no PE has `next_action_type =
    content_delivery` armed (the weekly cron drives delivery via the main
    collection). An unexpected fire here surfaces in logs.
    """
    frappe.logger().info(
        f"handle_content_delivery fired for PE {pe_row.name} "
        f"(CR-005: this handler is preserved but not part of the normal flow; "
        f"check whether someone manually armed content_delivery on this PE)"
    )

    flow_id = _get_flow_id(pe_row.batch, ACTION_CONTENT_DELIVERY)
    if not flow_id:
        _clear_action(pe_row.name)
        return

    if pe_row.glific_id:
        _trigger_flow(flow_id, pe_row.glific_id, pe_row.name, "content_delivery")

    # Clear — flow callback will set next action
    _clear_action(pe_row.name)


def handle_escalation(pe_row):
    """
    Handler: escalation (CR-003 channel-aware)

    Determines which escalation step to fire, pushes the per-step contact
    fields to Glific (escalation_order + escalation_type), then branches on
    the step's `escalation_type`:

    - help_note_a / help_note_b / voice_note → fire SP_Escalation flow.
      Glific reads the two contact fields and routes per-channel content.
    - parent_call → enqueue Vocallabs job, SKIP SP_Escalation entirely.
      The Glific flow is not involved in this branch; the call is placed
      via the Vocallabs 3-step API in summer_program/vocallabs.py.

    Step exhaustion routes per the existing T5 / T6 / T11 split (preserved
    from pre-CR-003). The grace clock is NOT armed here — it was already
    armed at week start (T0 / T19); these transitions just flip the state
    label and let the existing `grace_check` scheduler tick handle expiry.
    """
    from tap_lms.summer_program.state_machine import (
        t2_start_escalation, t4_next_escalation_step,
        t5_escalation_to_grace,
        t8_start_remedial_escalation, t10_next_remedial_escalation,
        t11_remedial_to_grace,
    )

    pe = frappe.get_doc("ProgramEnrollment", pe_row.name)
    state = pe.resolved_flow_state
    current_step = pe.current_escalation_step or 0

    # Get escalation steps config for this student
    steps = _get_escalation_steps_for_pe(pe)
    if not steps:
        # No escalation config — go to grace
        if state == STATE_NORMAL_ESCALATION:
            t5_escalation_to_grace(pe, "dispatcher")
        elif state == STATE_REMEDIAL_ESCALATION:
            t11_remedial_to_grace(pe, "dispatcher")
        else:
            _clear_action(pe_row.name)
        return

    next_step = current_step + 1

    if next_step > len(steps):
        # All steps exhausted — route to grace regardless of submission history.
        # CR-006 (2026-05-15): T6 (escalation_to_remedial) is removed.
        # Remedial is now reserved for failed-feedback students (T6b, CR-004).
        # Students who never submitted go to grace, then drop per CR-001.
        # CR-003: the grace clock is already armed at the week start; T5/T11
        # preserve it.
        if state in (STATE_NORMAL_CONTENT, STATE_NORMAL_ESCALATION):
            t5_escalation_to_grace(pe, "dispatcher")
        elif state in (STATE_REMEDIAL_CONTENT, STATE_REMEDIAL_ESCALATION):
            t11_remedial_to_grace(pe, "dispatcher")
        else:
            _clear_action(pe_row.name)
        return

    # ── Fire escalation step (CR-003 branch on escalation_type) ─────
    step_config = None
    for step in steps:
        try:
            if int(step.get("escalation_order")) == next_step:
                step_config = step
                break
        except (TypeError, ValueError):
            step_config = steps[next_step - 1]
            break

    # BR-007: if no step is tagged escalation_order == next_step (non-contiguous
    # escalation_order values in ArchetypeConfig), step_config stays None and
    # `step_config.get(...)` below crashes with `'NoneType' object has no
    # attribute 'get'`. That crash rolls back the atomic claim, so the PE is
    # re-tried every dispatcher tick forever (poison row) and starves the queue.
    # Fall back to the positionally-Nth step (next_step is guaranteed
    # 1..len(steps) by the `next_step > len(steps)` guard above). Mirrors the
    # except branch + sweep_migration._pick_first_escalation_step's fallback.
    if not step_config:
        step_config = steps[next_step - 1]

    next_hours = step_config.get("hours_after_previous", 24)
    escalation_type = step_config.get("escalation_type") or "help_note_a"

    # CR-003 §"Escalation flow — new semantics" step 3 (refined post-impl):
    # escalation_order + escalation_type are now written to PE inside the
    # T2/T4/T8/T10 transitions below, and the standard per-transition
    # _enqueue_contact_field_sync pushes BOTH to Glific before the flow
    # trigger fires (the contact-field job is enqueued via
    # `enqueue_after_commit=True` inside transition(), which runs before this
    # function returns to the dispatcher's row commit). The previous eager
    # `_push_escalation_contact_fields` helper has been removed — one sync
    # path, less duplication.
    #
    # Transition to escalation state + schedule next step. t2/t4/t8/t10
    # bump current_escalation_step AND write current_escalation_type from
    # the resolved step config. The flow trigger (or Vocallabs enqueue)
    # happens below, after the state transition.
    if state == STATE_NORMAL_CONTENT:
        # CR-009 follow-up (2026-05-23): pass next_hours so T2 re-arms
        # next_action_at for step 2 — otherwise the chain stuck at step 1.
        t2_start_escalation(pe, next_step, escalation_type,
                            next_hours=next_hours, trigger_source="dispatcher")
    elif state == STATE_NORMAL_ESCALATION:
        t4_next_escalation_step(pe, next_step, next_hours, escalation_type, "dispatcher")
    elif state == STATE_REMEDIAL_CONTENT:
        # Same CR-009 follow-up — T8 mirror of T2.
        t8_start_remedial_escalation(pe, next_step, escalation_type,
                                       next_hours=next_hours, trigger_source="dispatcher")
    elif state == STATE_REMEDIAL_ESCALATION:
        t10_next_remedial_escalation(pe, next_step, next_hours, escalation_type, "dispatcher")
    else:
        _clear_action(pe_row.name)
        return

    # ── Channel branch ─────────────────────────────────────────────
    if escalation_type == "parent_call":
        # Resolve ParentCallConfig and enqueue Vocallabs call. Skip
        # SP_Escalation entirely — Glific is not involved for parent calls.
        # The Vocallabs module handles its own retry/DLQ; the dispatcher
        # tick continues without waiting on the actual call.

        frappe.enqueue(
            "tap_lms.summer_program.vocallabs.initiate_parent_call",
            queue="long",
            timeout=300,
            enqueue_after_commit=True,
            pe_name=pe.name,
            escalation_step=step_config,
        )

        log_event(pe, "escalation_sent", trigger_source="dispatcher",
                  details={"step": next_step, "escalation_type": "parent_call"})
        return

    # Didi continuation call: any non-parent_call step with enable_voice_call = 1,
    # when VoiceAgentSettings.enable_continuation_calls is on (master switch).
    # This covers Track B — students who have submitted before and are slipping.
    # The nudge_config_override on the step (if set) forces a specific nudge type;
    # otherwise situation is auto-computed by _compute_situation() at call time.
    if step_config.get("enable_voice_call"):
        try:
            _settings = frappe.get_single("VoiceAgentSettings")
            if getattr(_settings, "enable_continuation_calls", 0):
                _step_for_call = dict(step_config)
                # Pass nudge_config_override so vocallabs.py can skip situation
                # auto-computation when the admin has pinned a specific nudge.
                frappe.enqueue(
                    "tap_lms.summer_program.vocallabs.initiate_parent_call",
                    queue="long",
                    timeout=300,
                    enqueue_after_commit=True,
                    pe_name=pe.name,
                    escalation_step=_step_for_call,
                )
                log_event(pe, "voice_call_queued", trigger_source="dispatcher",
                          details={
                              "step": next_step,
                              "escalation_type": escalation_type,
                              "nudge_override": step_config.get("nudge_config_override"),
                          })
        except Exception as _exc:
            frappe.log_error(
                message=f"Continuation call enqueue failed for PE {pe.name}: {_exc}",
                title="SP Vocallabs Continuation",
            )
        # Fall through to also fire the WhatsApp flow (belt-and-suspenders).

    # Text or voice-note channels → fire SP_Escalation flow.

    flow_id = _get_flow_id(pe_row.batch, ACTION_ESCALATION)
    if flow_id and pe.glific_id:
        _trigger_flow(flow_id, pe.glific_id, pe.name, "escalation")

    return


# CR-003 follow-up: `_push_escalation_contact_fields` removed. The two
# CR-003 fields (escalation_order, escalation_type) now flow via PE columns
# (current_escalation_step, current_escalation_type) written by the
# T2/T4/T8/T10 transitions, then pushed to Glific by the standard
# _enqueue_contact_field_sync. One push path, no duplication.


def handle_feedback_timeout(pe_row):
    """
    Handler: feedback_timeout

    Safety-net check: if FeedbackConsumer hasn't processed the AI feedback
    within the expected window, verify once whether it arrived (DB check).
    If yes, trigger T12 as a fallback. If no after 3 retries, alert admin.

    NOTE: Normal path is handled by FeedbackConsumer directly — it calls
    t12_feedback_ready after updating Submission and sending the Glific
    notification. This handler only fires as a timeout fallback.
    """
    from tap_lms.summer_program.state_machine import t12_feedback_ready

    pe = frappe.get_doc("ProgramEnrollment", pe_row.name)

    if pe.resolved_flow_state != STATE_SUBMITTED_AWAITING:
        # FeedbackConsumer already handled it — state moved
        _clear_action(pe_row.name)
        return

    # Check if feedback arrived but FeedbackConsumer missed the SP hook
    has_feedback = frappe.db.exists(
        "Submission",
        {
            "student_id": pe.student,
            "program_enrollment": pe.name,
            "week": pe.current_week,
            "is_primary": 1,
            "status": "Completed",
        },
    )

    if has_feedback:
        # Feedback arrived but state wasn't updated — trigger T12 as fallback
        t12_feedback_ready(pe, "scheduler")
    else:
        # Retry: schedule another check in 1 hour (max 3 retries)
        # Task #19 (2026-05-28, L-011): replaced `pe.save(ignore_permissions=True)`
        # with targeted `frappe.db.set_value` + atomic COALESCE increment for
        # delivery_failure_count. The previous pe.save would write every PE
        # column from this possibly-stale doc, clobbering concurrent point-
        # handler bumps to total_*/weekly_* that ran between the dispatch
        # claim and this branch. The atomic increment (P-002) is race-safe.
        retry_count = pe.delivery_failure_count or 0
        if retry_count < 3:
            frappe.db.sql(
                """
                UPDATE "tabProgramEnrollment"
                   SET delivery_failure_count = COALESCE(delivery_failure_count, 0) + 1
                 WHERE name = %s
                """,
                (pe.name,),
            )
            frappe.db.set_value(
                "ProgramEnrollment", pe.name,
                {
                    "next_action_at": add_to_date(now_datetime(), hours=1),
                    "next_action_type": ACTION_FEEDBACK_TIMEOUT,
                },
                update_modified=False,
            )
        else:
            # Give up — alert admin, clear action
            frappe.log_error(
                f"Feedback timeout: AI feedback not received for PE {pe.name} "
                f"(student={pe.student}, week={pe.current_week}). "
                f"Check RabbitMQ/GCS pipeline.",
                "SP Feedback Timeout Alert",
            )
            _clear_action(pe_row.name)


def handle_week_advancement(pe_row):
    """
    Handler: week_advancement

    Advances the student to the next week. Checks:
      - If next week > total_weeks → T16 (program completed)
      - If next week > max_allowed_week → T15 (binge pause)
      - Otherwise → T14 (normal week advance)
    """
    from tap_lms.summer_program.state_machine import (
        t14_week_advance, t15_binge_pause, t16_program_completed,
    )

    pe = frappe.get_doc("ProgramEnrollment", pe_row.name)

    if pe.resolved_flow_state != STATE_WEEK_COMPLETED:
        _clear_action(pe_row.name)
        return

    batch = frappe.get_doc("Batch", pe.batch)
    next_week = (pe.current_week or 1) + 1
    total_weeks = batch.total_weeks or 8
    max_allowed = pe.max_allowed_week or (batch.current_calendar_week or 1) + 1

    if next_week > total_weeks:
        # Program completed
        t16_program_completed(pe, "dispatcher")
        # Trigger program_complete flow
        flow_id = _get_flow_id(pe.batch, "program_complete")
        if flow_id and pe.glific_id:
            _trigger_flow(flow_id, pe.glific_id, pe.name, "program_complete")

    elif next_week > max_allowed:
        # Binge limit — can't go faster than batch calendar
        # Calculate when next week opens (next Monday or batch schedule)
        next_open = _get_next_week_open_date(batch, next_week)
        t15_binge_pause(pe, next_open, "dispatcher")
        # Trigger binge info flow
        flow_id = _get_flow_id(pe.batch, ACTION_PAUSE_CHECK)
        if flow_id and pe.glific_id:
            _trigger_flow(flow_id, pe.glific_id, pe.name, "binge_info")

    else:
        # Normal advancement
        week_rule = _get_week_rule(pe, batch, next_week)
        t14_week_advance(pe, next_week, week_rule, "dispatcher")


def handle_grace_check(pe_row):
    """
    Handler: grace_check (CR-003 — direct drop)

    Grace window has expired. CR-003 §"Grace window — new semantics":
    if the student hasn't submitted (weekly_submission_done = 0) the PE
    transitions directly to STATE_PROGRAM_DROPPED via t17_grace_expired.
    No paused_no_activity hop, no re-engagement loop — that's all deleted
    in this CR.

    If the student DID submit during the window (weekly_submission_done = 1)
    and we somehow land here (a race or a stale scheduler row), the handler
    is a no-op — clear the action and trust the existing state.

    Defensive: if `resolved_flow_state` is not grace_waiting (student moved
    via T17 grace submission already), no-op as well.
    """
    from tap_lms.summer_program.state_machine import t17_grace_expired

    pe = frappe.get_doc("ProgramEnrollment", pe_row.name)

    # Student already moved out of grace_waiting (submission landed → T17
    # transitioned them, or they were dropped by an admin). No-op.
    if pe.resolved_flow_state != STATE_GRACE_WAITING:
        _clear_action(pe_row.name)
        return

    # CR-003 + CR-002 v2: if weekly_submission_done is set, the student
    # submitted within the window. Don't drop — clear the action and let
    # the next T19 (week advance) re-arm the clock.
    if pe.weekly_submission_done:
        _clear_action(pe_row.name)
        return

    # Defensive: if the clock hasn't actually expired yet, re-schedule the
    # tick for the proper expiry time rather than dropping early. Should
    # match exactly under normal scheduling but absorbs minor clock skew.
    # Task #19 (2026-05-28, L-011): targeted set_value instead of pe.save —
    # avoids clobbering concurrent point-handler bumps that ran between the
    # dispatcher's atomic claim and this branch.
    now = now_datetime()
    if pe.grace_window_end_at and get_datetime(pe.grace_window_end_at) > now:
        frappe.db.set_value(
            "ProgramEnrollment", pe.name,
            {
                "next_action_at": pe.grace_window_end_at,
                "next_action_type": ACTION_GRACE_CHECK,
            },
            update_modified=False,
        )
        return

    # Clock expired AND no submission this week → drop.
    t17_grace_expired(pe, "dispatcher")


# CR-003: handle_re_engagement and handle_grace_reminder removed.
# Re-engagement is now inbound-only (SP_Incoming_Router routes a rejoin path
# when program_status = 'dropped'); the backend no longer reaches out to
# paused students. Grace reminders are also gone — the escalation steps
# within the week ARE the reminders.


def handle_pause_check(pe_row):
    """
    Handler: pause_check

    For binge-paused students: check if the calendar has advanced
    enough to allow them to resume. If yes, trigger T21 (binge resume).
    """
    from tap_lms.summer_program.state_machine import t21_binge_resume

    pe = frappe.get_doc("ProgramEnrollment", pe_row.name)

    if pe.resolved_flow_state != STATE_PAUSED_BINGE:
        _clear_action(pe_row.name)
        return

    batch = frappe.get_doc("Batch", pe.batch)
    next_week = (pe.current_week or 1) + 1
    # Binge ceiling is calendar_week + 1 (a student may be up to 1 week ahead).
    # The PAUSE decision (handle_week_advancement) and the inbound REACTIVATION
    # path both use calendar+1; this resume check previously used bare
    # `calendar_week`, holding paused students one week longer than the pause
    # policy allows and disagreeing with reactivation (same student resumed via
    # an inbound message but not via the scheduler). Aligned to calendar+1,
    # mirroring reactivation.py exactly. Compute fresh from the batch rather
    # than reading pe.max_allowed_week: the live calendar value is authoritative
    # and can't be stale if the calendar was changed via a path that didn't run
    # update_batch_week (e.g. a manual db.set_value on the Batch).
    max_allowed = (batch.current_calendar_week or 1) + 1

    if next_week <= max_allowed:
        # Calendar caught up — resume
        t21_binge_resume(pe, "dispatcher")
    else:
        # Still ahead of calendar — check again next Monday.
        # Task #19 (2026-05-28, L-011): targeted set_value instead of
        # pe.save — avoids clobbering any column written by a concurrent
        # path (the binge-paused state doesn't normally race with point
        # bumps because award handlers gate on get_active_pe → would skip
        # a paused student, but defensive consistency with the rest of the
        # dispatcher matters more than the 1-line shortcut).
        frappe.db.set_value(
            "ProgramEnrollment", pe.name,
            {
                "next_action_at": add_to_date(now_datetime(), days=7),
                "next_action_type": ACTION_PAUSE_CHECK,
            },
            update_modified=False,
        )


# ════════════════════════════════════════════════════════════
# HANDLER MAP
# ════════════════════════════════════════════════════════════

HANDLER_MAP = {
    ACTION_CONTENT_DELIVERY: handle_content_delivery,
    ACTION_ESCALATION: handle_escalation,
    ACTION_FEEDBACK_TIMEOUT: handle_feedback_timeout,
    ACTION_WEEK_ADVANCEMENT: handle_week_advancement,
    ACTION_GRACE_CHECK: handle_grace_check,
    ACTION_PAUSE_CHECK: handle_pause_check,
    # CR-003: handle_re_engagement and handle_grace_reminder removed.
    # Legacy PEs with next_action_type IN ('re_engagement', 'grace_reminder')
    # are nulled by the migration patch (cr_003.grace_and_reengagement); any
    # post-migration row that somehow holds these values falls through to
    # the "Unknown action_type" branch in _dispatch_single which logs and
    # clears the action defensively.
}


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════


def _get_flow_id(batch_name, action_type):
    """Get the Glific flow UUID for an action type from the BPR."""
    field = ACTION_FLOW_FIELD_MAP.get(action_type)
    if not field:
        return None

    bpr_name = frappe.db.get_value(
        "BatchProgramRun",
        {"batch": batch_name, "status": BPR_ACTIVE},
        "name",
    )
    if not bpr_name:
        return None

    return frappe.db.get_value("BatchProgramRun", bpr_name, field)


def _trigger_flow(flow_id, glific_id, pe_name, action_label):
    """Enqueue a Glific flow trigger for a single contact (BR-007).

    Previously this called `start_contact_flow` INLINE. In the per-PE dispatcher
    that blocks every tick on a Glific HTTP round-trip per PE; at scale (~25K due
    escalations) the tick blows its 300s job timeout and the queue starves —
    binge-resume (`pause_check`) and `week_advancement` actions never get
    reached because they sit behind the escalation backlog in `next_action_at`
    order. The flow trigger is fire-and-forget (no PE state depends on its
    result), so enqueue it to the `short` queue and let the dispatcher tick
    return immediately. `enqueue_after_commit=True` so the PE's just-written
    state is durable before the flow fires (and so a rolled-back tick doesn't
    fire a flow for a transition that didn't persist). Mirrors the parent_call
    branch in handle_escalation.

    All callers (content_delivery, escalation, program_complete, binge_info)
    are dispatcher-driven and fire-and-forget, so async is correct for every
    call site.
    """
    frappe.enqueue(
        "tap_lms.summer_program.pe_dispatcher._trigger_flow_job",
        queue="short",
        timeout=120,
        enqueue_after_commit=True,
        flow_id=flow_id,
        glific_id=glific_id,
        pe_name=pe_name,
        action_label=action_label,
    )


def _trigger_flow_job(flow_id, glific_id, pe_name, action_label):
    """Background worker (RQ): actually fire the Glific flow. Enqueued by
    `_trigger_flow` (BR-007). Decoupled from the dispatcher tick — a failure
    here never touches the dispatcher."""
    from tap_lms.glific_integration import start_contact_flow

    try:
        default_results = {
            "pe_name": pe_name,
            "action": action_label,
        }
        start_contact_flow(str(flow_id), str(glific_id), default_results)
        # L-035: log the success path too, so ops can confirm a flow fired
        # without spelunking the Error Log.
        frappe.logger().info(
            f"_trigger_flow_job: fired flow {flow_id} for PE={pe_name} "
            f"action={action_label} glific_id={glific_id}"
        )
    except Exception as e:
        # L-056: an RQ job must fail LOUDLY — rollback (L-030/L-077), log
        # durably in a nested try (double-fault defense), then RE-RAISE so the
        # failure lands in RQ's FailedJobRegistry (the rq_queue_depth_watcher
        # alerts on it) instead of a silent clean exit. No frappe-level retry
        # is configured, so this won't re-send; it's visible for manual replay.
        try:
            frappe.db.rollback()
        except Exception:
            pass
        try:
            frappe.log_error(
                f"Flow trigger error: PE={pe_name}, action={action_label}, "
                f"flow={flow_id}, glific_id={glific_id}: {str(e)}",
                "SP Flow Trigger",
            )
        except Exception:
            frappe.logger().error(f"_trigger_flow_job double-fault: {e}")
        raise


def _get_escalation_steps_for_pe(pe):
    """Get escalation step configs for a PE's archetype × current_path.

    Path-aware lookup (task #68 / 2026-05-22):
      - Pass `pe.current_path` (Core | Remedial) to `_get_escalation_steps`.
      - If the PE is on Remedial AND the Remedial ArchetypeConfig has no
        active escalation steps configured, fall back to Core's chain so
        the dispatcher still has work to do. This prevents a misconfigured
        Remedial config from silently breaking the escalation handler — a
        fallback is a better failure mode than "student receives no
        escalation messages at all and silently drops at grace."
      - Defaults to PATH_CORE if pe.current_path is empty (defensive — the
        field is set at enrollment and on T14/T6b, but legacy PEs may
        have it null).
    """
    from tap_lms.summer_program.student_progression_sp import _get_escalation_steps
    from tap_lms.summer_program.constants import PATH_CORE, PATH_REMEDIAL

    try:
        student = frappe.get_doc("Student", pe.student)
        batch = frappe.get_doc("Batch", pe.batch)
        path = pe.current_path or PATH_CORE

        steps = _get_escalation_steps(student, batch, path=path)

        # Fallback: Remedial config is empty → use Core's chain so the
        # student still gets nudged. Log so operators notice missing config.
        if not steps and path == PATH_REMEDIAL:
            frappe.logger().warning(
                f"_get_escalation_steps_for_pe: PE {pe.name} is on Remedial "
                f"but archetype={student.archetype}, arm={student.experiment_arm} "
                f"has no Remedial escalation_steps configured — falling back "
                f"to Core's chain. Populate the Remedial ArchetypeConfig to "
                f"silence this warning."
            )
            steps = _get_escalation_steps(student, batch, path=PATH_CORE)

        return steps
    except Exception:
        return []


def _get_week_rule(pe, batch, week):
    """Get the WeekRule for a specific (PE, week).

    WeekRule is a CHILD table on ArchetypeConfig (istable=1). The canonical
    lookup, mirroring _get_week1_submission_type in program_enrollment_api.py:

      1. Find parent ArchetypeConfig matching the PE's
         (batch, archetype, experiment_arm, path, is_active=1) tuple.
      2. Fall back to the "default" experiment_arm if the PE's arm has no
         active config — same fallback pattern as enrollment-time.
      3. Read the WeekRule child row for the requested week.

    Returns a dict with `expected_submission_type` (and any other WeekRule
    fields the callers want) — or None if no config or no matching week.

    Historical bug (fixed 2026-05-19): this helper used to call
    frappe.db.get_value("ArchetypeConfig", ..., ["expected_submission_type",
    "core_learning_unit", "remedial_learning_unit"]) directly on the parent
    table. None of those columns live on ArchetypeConfig itself —
    expected_submission_type is on the WeekRule child table, and
    core_learning_unit / remedial_learning_unit are phantom field names that
    don't exist on any doctype. Every dispatcher tick that reached this
    function crashed, silently failing week_advancement for the whole cohort
    (the silence was compounded by the except branch's frappe.log_error
    running inside the aborted Postgres transaction).
    """
    try:
        config_path = pe.current_path or PATH_CORE
        config_name = frappe.db.get_value(
            "ArchetypeConfig",
            {
                "batch": batch.name,
                "archetype": pe.archetype,
                "experiment_arm": pe.experiment_arm or "default",
                "path": config_path,
                "is_active": 1,
            },
            "name",
        )
        if not config_name:
            # Fallback: same arm not found → try the "default" arm.
            config_name = frappe.db.get_value(
                "ArchetypeConfig",
                {
                    "batch": batch.name,
                    "archetype": pe.archetype,
                    "experiment_arm": "default",
                    "path": config_path,
                    "is_active": 1,
                },
                "name",
            )
        if not config_name:
            return None

        rule = frappe.db.get_value(
            "WeekRule",
            {
                "parent": config_name,
                "parenttype": "ArchetypeConfig",
                "week": week,
            },
            ["expected_submission_type", "submission_validation_enabled"],
            as_dict=True,
        )
        return rule
    except Exception as e:
        frappe.logger().warning(
            f"_get_week_rule failed for pe={pe.name} batch={batch.name} "
            f"week={week}: {e}"
        )
        return None


def _get_next_week_open_date(batch, target_week):
    """Calculate when a specific week becomes available based on batch calendar."""
    if not batch.start_date:
        return add_to_date(now_datetime(), days=7)

    # Each week opens 7 days after the previous
    days_offset = (target_week - 1) * 7
    return add_to_date(get_datetime(batch.start_date), days=days_offset)


def _record_delivery_failure(pe_name):
    """Bump `delivery_failure_count`; chain to T23 when the threshold is hit.

    Intended to be called from `_trigger_flow` (or any other per-PE delivery
    helper) when a Glific API call fails after retries. Currently `_trigger_flow`
    catches exceptions and only calls `frappe.log_error` — the per-PE failure
    counter is NOT incremented from there yet because `start_contact_flow` is a
    fire-and-forget request without delivery confirmation. The proper wire-up
    point is the Glific webhook for delivery-status events (Phase 1), at which
    point this helper becomes the canonical T25→T23 chain. The helper is shipped
    now so the state-machine plumbing is in place when the webhook lands.

    Idempotent on already-terminal PEs (completed / dropped) — the helper
    early-returns without touching the counter so a late delivery-failure event
    can't resurrect a dropped PE.

    Atomic increment uses the COALESCE-update pattern (P-002) on the column to
    survive races against quiz/submission counter writes that may share the
    same row. We load the PE doc only to read the post-update value for the
    threshold check; the increment itself is the atomic SQL.
    """
    from tap_lms.summer_program.state_machine import t23_auto_drop
    from tap_lms.summer_program.constants import (
        MAX_DELIVERY_FAILURES,
        PROGRAM_ACTIVE,
        PROGRAM_PAUSED,
    )

    # Atomic increment with the status filter folded INTO the UPDATE WHERE,
    # so a PE that transitions to terminal between this caller's intent and
    # the UPDATE doesn't get its counter bumped. The 0-row return means the
    # PE was already terminal — short-circuit. Eliminates the race between
    # a preliminary status read and the increment (M1 fix, 2026-05-13;
    # L-018 concurrency rule).
    # Use `IN (%s, %s)` rather than `ANY(%s)` with a list parameter — Frappe's
    # parameter wrapper turns the 2-tuple-with-list shape into a Postgres
    # record, breaking ANY. See validators.py:189 comment for the same fix.
    rows = frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET delivery_failure_count = COALESCE(delivery_failure_count, 0) + 1
         WHERE name = %s
           AND program_status IN (%s, %s)
        RETURNING delivery_failure_count
        """,
        (pe_name, PROGRAM_ACTIVE, PROGRAM_PAUSED),
    )
    if not rows:
        return  # PE was already terminal, or didn't exist

    new_count = rows[0][0]
    if new_count >= MAX_DELIVERY_FAILURES:
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        t23_auto_drop(pe, reason="delivery_failure", trigger_source="dispatcher")
