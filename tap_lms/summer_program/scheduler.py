"""
Summer Program Scheduler
tap_lms/summer_program/scheduler.py

PAL Scheduler — runs as Frappe scheduled tasks.
Live triggers: weekly_content_delivery_trigger (Tue content → `main` collection),
weekly_content_sweep (Mon, CR-027), and run_daily_actions (post BR-006: only the
batch-level program-complete flag). Per-PE escalation + per-student actions live
in pe_dispatcher.handle_*.

Register in hooks.py:
    scheduler_events = {
        "daily": [
            "tap_lms.summer_program.scheduler.run_daily_actions",
        ]
    }
"""
import frappe
from frappe.utils import now_datetime, getdate, today, date_diff, nowdate

from tap_lms.glific_integration import start_contact_flow
from tap_lms.summer_program.constants import (
    BPR_ACTIVE,
    ACTION_CONTENT_DELIVERY,
    ACTION_ESCALATION,
    ACTION_FLOW_FIELD_MAP,
    COLLECTION_ACTIONS,
    PER_STUDENT_ACTIONS,
    ARCHETYPE_DORMANT,
    ARCHETYPE_FENCE_SITTER,
    PROGRAM_ACTIVE,
    STATE_NORMAL_CONTENT,
)
# CR-003: ACTION_REENGAGEMENT and ACTION_GRACE_REMINDER removed from constants.
# _run_reengagement and _run_grace_notifications were the daily-scheduler
# entry points that consumed those action types; both functions are deleted
# from this file (re-engagement is now inbound-only; grace reminders are
# replaced by per-week escalation steps).
from tap_lms.summer_program.glific_extensions import (
    start_group_flow,
    add_contacts_to_group_bulk,
    create_or_get_collection,
)
# CR-027: shared SET-BASED demotion routine — the SINGLE demotion mechanism
# used by BOTH the one-time backlog migration and this weekly sweep's Phase 1
# (L-074 anti-drift; L-082 — set-based, can't hang like the old per-PE loop).
from tap_lms.summer_program.migrations.sweep_migration import _bulk_demote_batch


def run_daily_actions():
    """
    Main scheduler entry point. Called daily by Frappe scheduler.
    Finds all active BatchProgramRuns and processes scheduled actions.
    """
    active_bprs = frappe.get_all(
        "BatchProgramRun",
        filters={"status": BPR_ACTIVE},
        fields=["name"],
    )

    for row in active_bprs:
        try:
            bpr = frappe.get_doc("BatchProgramRun", row.name)
            batch = frappe.get_doc("Batch", bpr.batch)
            _process_bpr_actions(bpr, batch)
        except Exception as e:
            frappe.log_error(
                f"Scheduler error for BPR {row.name}: {str(e)}",
                "SP Scheduler",
            )


def _process_bpr_actions(bpr, batch):
    """
    Determine which actions to run today for a given BPR.
    """
    current_week = _get_current_week(batch)
    total_weeks = batch.total_weeks or 0

    # ── Collection-based actions ─────────────────────────
    # BR-006 (2026-06-16): the daily content-delivery + escalation
    # collection-fires are REMOVED. They duplicated the live paths and the
    # collection model drifted underneath them (firing at audit collections
    # like program_dropped / program_completed and at is_active=0 legacy
    # archetype×arm groups, daily instead of weekly). Superseded by:
    #   - content delivery → weekly_content_delivery_trigger (Tue 09:00 IST,
    #     `main` collection only) + weekly_content_sweep (CR-005 / CR-027)
    #   - escalation → pe_dispatcher.handle_escalation (per-PE, config-driven
    #     from ArchetypeConfig, advances current_escalation_step)
    # Same defect class that retired escalation_runner (task #50, 2026-05-21).
    # Preserved (not deleted) per L-050; re-enable ONLY if the per-PE / weekly
    # paths are removed. See docs/bug-reports/BR-006-run-daily-actions-collection-overlap.md.
    # _run_collection_action(bpr, ACTION_CONTENT_DELIVERY)
    # _run_escalation(bpr)

    # CR-003: proactive re-engagement removed. Dropped students are
    # re-engaged via SP_Incoming_Router when they send an inbound
    # message; the backend does not push re-engagement nudges.

    # ── Per-student actions ──────────────────────────────
    # CR-003: proactive grace notifications removed. The per-week
    # escalation steps within the active week ARE the reminders;
    # grace expiry is policed by handle_grace_check in pe_dispatcher.

    # Program complete: retained ONLY for the batch-level BPR.status='completed'
    # flag (the sole setter — it gates the active-BPR schedulers). Per-STUDENT
    # completion is handled individually by pe_dispatcher.handle_week_advancement
    # (t16 + per-PE program_complete flow), so _run_program_complete's own
    # collection group-fire is redundant (BR-006 / architect review 2026-06-16)
    # and is a candidate for a separate cleanup.
    if current_week and total_weeks and current_week > total_weeks:
        _run_program_complete(bpr, batch)


# ── Collection-Based Actions ────────────────────────────────


def _run_collection_action(bpr, action_type):
    """
    Trigger a flow on ALL archetype collections for this BPR.
    """
    flow_field = ACTION_FLOW_FIELD_MAP.get(action_type)
    if not flow_field:
        return

    flow_id = getattr(bpr, flow_field, None)
    if not flow_id:
        frappe.logger().info(
            f"No flow configured for {action_type} on BPR {bpr.name}"
        )
        return

    for collection in bpr.pg_collections:
        try:
            result = start_group_flow(flow_id, collection.glific_group_id)
            if result:
                frappe.logger().info(
                    f"{action_type}: triggered flow {flow_id} "
                    f"on collection {collection.collection_label}"
                )
            else:
                frappe.logger().error(
                    f"{action_type}: FAILED flow {flow_id} "
                    f"on collection {collection.collection_label}"
                )
        except Exception as e:
            frappe.log_error(
                f"{action_type} error on {collection.collection_label}: {str(e)}",
                "SP Scheduler Action",
            )


def _run_escalation(bpr):
    """
    Run escalation flow only on Dormant and Fence Sitter collections.
    """
    flow_id = bpr.escalation_flow
    if not flow_id:
        return

    target_archetypes = [ARCHETYPE_DORMANT, ARCHETYPE_FENCE_SITTER]
    for collection in bpr.pg_collections:
        if collection.archetype in target_archetypes:
            try:
                start_group_flow(flow_id, collection.glific_group_id)
                frappe.logger().info(
                    f"Escalation: flow {flow_id} on {collection.collection_label}"
                )
            except Exception as e:
                frappe.log_error(
                    f"Escalation error: {collection.collection_label}: {str(e)}",
                    "SP Scheduler Escalation",
                )


# CR-003: _run_reengagement removed — re-engagement is now inbound-only via
# SP_Incoming_Router when the student sends a message and program_status =
# 'dropped'. The backend never reaches out to dropped students.
#
# CR-003: _run_grace_notifications removed — the per-week escalation steps
# inside the active week ARE the reminders. handle_grace_check in
# pe_dispatcher fires once at grace_window_end_at; on expiry the PE drops
# directly to program_dropped (no proactive grace notification flow).


# ── Per-Student Actions ─────────────────────────────────────


def _run_program_complete(bpr, batch):
    """
    Trigger program-complete flow for all students in the batch.
    Uses collection-based trigger since it applies to everyone.
    """
    flow_id = bpr.program_complete_flow
    if not flow_id:
        return

    # Trigger on ALL collections (every student gets this)
    for collection in bpr.pg_collections:
        try:
            start_group_flow(flow_id, collection.glific_group_id)
        except Exception as e:
            frappe.log_error(
                f"Program complete error: {collection.collection_label}: {str(e)}",
                "SP Scheduler Complete",
            )

    # Also trigger on onboarding set collections
    for pg_set in bpr.pg_onboarding_sets:
        if pg_set.glific_contact_group:
            try:
                group_doc = frappe.get_doc("GlificContactGroup", pg_set.glific_contact_group)
                start_group_flow(flow_id, group_doc.group_id)
            except Exception as e:
                frappe.log_error(
                    f"Program complete error (onboarding set): {str(e)}",
                    "SP Scheduler Complete",
                )

    # Mark BPR as completed
    bpr.status = "completed"
    bpr.save(ignore_permissions=True)
    frappe.db.commit()

    frappe.logger().info(f"Program completed for BPR {bpr.name}")


# ── Helpers ──────────────────────────────────────────────────


def _get_current_week(batch):
    """Calculate current week number from batch start_date."""
    if not batch.start_date:
        return None
    days = date_diff(today(), batch.start_date)
    if days < 0:
        return 0
    return (days // 7) + 1


def _get_students_for_bpr(bpr):
    """Get all student names linked to a BPR."""
    from tap_lms.summer_program.enrollment import _get_students_for_bpr as get_students
    return get_students(bpr)


# ── CR-005 (2026-05-15): Weekly content delivery via main collection ──

def process_pending_feedback_ready_before_weekly_content():
    """
    Run the internal feedback-ready PE mutation for completed submissions
    that never requested the student-facing feedback flow.

    This intentionally does NOT send the Glific feedback notification. It only
    ensures ProgramEnrollment points/state/remedial routing are up to date
    before the next weekly content flow starts.
    """
    pending_submissions = frappe.db.sql(
        """
        SELECT s.name AS submission_id,
               s.student_id AS student_id
          FROM "tabSubmission" s
          JOIN "tabProgramEnrollment" pe
            ON pe.name = s.program_enrollment
          JOIN "tabBatchProgramRun" bpr
            ON bpr.batch = pe.batch
           AND bpr.status = 'active'
           AND bpr.content_delivery_flow IS NOT NULL
         WHERE s.is_primary = 1
           AND s.status IN ('Completed', 'Failed')
           AND s.feedback_ready_processed_at IS NULL
           AND pe.program_status = 'active'
           AND pe.resolved_flow_state = 'submitted_awaiting_feedback'
        """,
        as_dict=True,
    )

    if not pending_submissions:
        return {"status": "success", "processed": 0, "failed": 0}

    from tap_lms.feedback_handler.feedback_consumer import FeedbackConsumer

    consumer = FeedbackConsumer.__new__(FeedbackConsumer)
    processed = 0
    failed = 0

    for row in pending_submissions:
        submission_id = row["submission_id"]
        try:
            if consumer.process_feedback_ready(
                submission_id,
                {
                    "submission_id": submission_id,
                    "student_id": row.get("student_id"),
                    "feedback": {},
                },
            ):
                processed += 1
        except Exception as e:
            failed += 1
            frappe.log_error(
                f"weekly_content_delivery_trigger feedback-ready preflight "
                f"failed for submission {submission_id}: {e}",
                "SP Weekly Feedback Ready Preflight",
            )

    return {"status": "success", "processed": processed, "failed": failed}


def weekly_content_delivery_trigger():
    """CR-005: Tuesday 09:00 IST (03:30 UTC). For each active BPR, fire
    SP_Content_Delivery against the `main` Glific collection. Membership is
    already current because state transitions wrote it continuously throughout
    the week (Approach B). No recompute, no reconcile.

    Idempotency: re-running produces another start_group_flow call for each
    active BPR. Glific deduplicates identical group-flow starts within a short
    window; operator discipline (don't manually invoke during the cron window)
    is the documented mitigation. No code-level mutex per locked decision
    2026-05-15.
    """
    process_pending_feedback_ready_before_weekly_content()

    active_bprs = frappe.db.sql(
        """
        SELECT name, batch, content_delivery_flow
          FROM "tabBatchProgramRun"
         WHERE status = 'active'
           AND content_delivery_flow IS NOT NULL
        """,
        as_dict=True,
    )

    for bpr in active_bprs:
        main_col = frappe.db.sql(
            """
            SELECT name, glific_group_id, collection_label, member_count
              FROM "tabPGCollection"
             WHERE parent = %s
               AND kind = %s
               AND COALESCE(is_active, 0) = 1
             LIMIT 1
            """,
            (bpr["name"], "main"),
            as_dict=True,
        )
        if not main_col:
            continue
        main_col = main_col[0]

        if not main_col.get("glific_group_id"):
            continue

        # Skip BPRs whose main collection has zero members. Reading
        # member_count avoids a needless Glific call (which would either
        # error or no-op). The count is maintained by the state-driven
        # collection_membership writes (Approach B); if a deployment doesn't
        # yet maintain member_count, treat NULL as zero to be safe.
        if (main_col.get("member_count") or 0) <= 0:
            frappe.logger().info(
                f"weekly_content_delivery_trigger: skipping BPR "
                f"{bpr['name']} — main collection empty"
            )
            continue

        try:
            start_group_flow(
                flow_id=str(bpr["content_delivery_flow"]),
                group_id=str(main_col["glific_group_id"]),
            )
            frappe.logger().info(
                f"weekly_content_delivery_trigger: fired flow "
                f"{bpr['content_delivery_flow']} for BPR {bpr['name']} "
                f"(main members: {main_col.get('member_count', 0)})"
            )
        except Exception as e:
            frappe.log_error(
                f"weekly_content_delivery_trigger failed for BPR "
                f"{bpr['name']}: {e}",
                "SP Weekly Content Delivery",
            )


# ════════════════════════════════════════════════════════════
# CR-027 (2026-06-09): weekly content delivery sweep
# Monday 06:00 UTC — registered in hooks.scheduler_events.cron.
# ════════════════════════════════════════════════════════════


def _sweep_phase1_demote_behind(active_bprs, summary):
    """Phase 1 — demote behind students (per active BPR) to normal_escalation.

    "Behind" = normal_content_delivery, current_week < calendar_week,
    weekly_video_done = 0. Delegates to the SAME SET-BASED helper as the
    one-time backlog migration (`_bulk_demote_batch`): ~8 combo-targeted SQL
    UPDATEs + one bulk Glific move per BPR. ONE demotion mechanism (L-074),
    and it cannot hang the way the old per-PE loop did at scale (L-082).

    Per-BPR try/except so one batch's failure doesn't abort the rest; on
    failure rollback the batch's txn so a poisoned txn (L-030) doesn't cascade.
    """
    for bpr in active_bprs:
        batch_name = bpr["batch"]
        try:
            batch = frappe.get_doc("Batch", batch_name)
            calendar_week = batch.current_calendar_week
        except Exception as e:
            summary["errors"].append(f"BPR {bpr['name']}: phase1 batch load failed: {e}")
            continue

        if not calendar_week:
            summary["errors"].append(
                f"BPR {bpr['name']}: batch {batch_name} has no current_calendar_week"
            )
            continue

        try:
            res = _bulk_demote_batch(
                batch_name, calendar_week, bpr["name"], dry_run=False
            )
        except Exception as e:
            summary["phase1_failed"] += 1
            frappe.db.rollback()
            if len(summary["errors"]) < 50:
                summary["errors"].append(f"phase1 batch {batch_name}: {e}")
            try:
                frappe.log_error(
                    f"weekly_content_sweep phase1: batch {batch_name} failed: {e}",
                    "CR-027 Weekly Sweep",
                )
            except Exception:
                frappe.logger().error(
                    f"weekly_content_sweep phase1 double-fault on {batch_name}: {e}"
                )
            continue

        summary["phase1_demoted"] += res["demoted"]
        summary["phase1_no_config"] += res["no_config_pes"]
        if res["bulk_move"]:
            summary["phase1_removed_from_main"] += res["bulk_move"]["removed_from_main"]
            summary["phase1_added_to_escalation"] += res["bulk_move"]["added_to_escalation"]
        for err in res["errors"][:10]:
            if len(summary["errors"]) < 50:
                summary["errors"].append(err)


def _sweep_phase2_deliver_current_week(active_bprs, summary):
    """Phase 2 — deliver this week's content to current-week candidates.

    For each active BPR:
      - find normal_content_delivery PEs at current_week == calendar_week with
        weekly_video_done = 0 and a glific_id set,
      - build/reuse a temp Glific sweep collection containing only those
        candidates,
      - trigger the BPR's content_delivery_flow against that collection.

    Bulk via temp collection (one Glific group-flow start) — same pattern as
    the staggered launch sub-collections, NOT per-student calls (L-014).

    Idempotent within a week: candidates are filtered by weekly_video_done = 0,
    so a student who already received the video (video_done = 1) is excluded on
    a re-run.
    """
    for bpr in active_bprs:
        batch_name = bpr["batch"]
        try:
            batch = frappe.get_doc("Batch", batch_name)
            calendar_week = batch.current_calendar_week
        except Exception as e:
            summary["errors"].append(f"BPR {bpr['name']}: phase2 batch load failed: {e}")
            continue

        if not calendar_week:
            summary["errors"].append(
                f"BPR {bpr['name']}: batch {batch_name} has no current_calendar_week"
            )
            continue

        content_flow = bpr.get("content_delivery_flow")
        if not content_flow:
            summary["bprs_without_content_flow"] += 1
            summary["errors"].append(
                f"BPR {bpr['name']}: no content_delivery_flow configured"
            )
            continue

        candidates = frappe.db.sql(
            """
            SELECT pe.glific_id
              FROM "tabProgramEnrollment" pe
             WHERE pe.batch = %(batch)s
               AND pe.program_status = %(active)s
               AND pe.resolved_flow_state = %(state)s
               AND pe.current_week = %(calendar_week)s
               AND COALESCE(pe.weekly_video_done, 0) = 0
               AND COALESCE(pe.glific_id, '') != ''
            """,
            {
                "batch": batch_name,
                "active": PROGRAM_ACTIVE,
                "state": STATE_NORMAL_CONTENT,
                "calendar_week": calendar_week,
            },
            as_dict=True,
        )

        if not candidates:
            summary["bprs_with_no_phase2_candidates"] += 1
            continue

        contact_ids = [str(c["glific_id"]) for c in candidates]

        # Build/reuse the sweep collection (one per batch per week per day).
        date_token = nowdate().replace("-", "")
        sweep_label = f"SP_{batch_name}_sweep_wk{calendar_week}_{date_token}"
        sweep_group = create_or_get_collection(
            label=sweep_label,
            description=(
                f"Weekly content sweep candidates for {batch_name} "
                f"week {calendar_week} on {nowdate()}"
            ),
        )
        # create_or_get_collection returns a dict {"id": ..., "label": ...}
        # (or None) — NOT a bare id. Extract the id defensively.
        sweep_group_id = sweep_group.get("id") if isinstance(sweep_group, dict) else None
        if not sweep_group_id:
            summary["errors"].append(
                f"BPR {bpr['name']}: failed to create/get sweep group {sweep_label}"
            )
            continue

        add_ok = add_contacts_to_group_bulk(contact_ids, sweep_group_id)
        if not add_ok:
            summary["errors"].append(
                f"BPR {bpr['name']}: bulk add to sweep group {sweep_group_id} failed"
            )
            continue

        trigger_ok = start_group_flow(
            flow_id=str(content_flow), group_id=str(sweep_group_id)
        )
        if trigger_ok:
            summary["phase2_triggered"] += len(contact_ids)
            summary["phase2_groups_used"].append({
                "bpr": bpr["name"],
                "group_id": sweep_group_id,
                "label": sweep_label,
                "students": len(contact_ids),
            })
            frappe.logger().info(
                f"weekly_content_sweep: triggered flow {content_flow} against "
                f"sweep group {sweep_group_id} ({sweep_label}) for "
                f"{len(contact_ids)} candidates in batch {batch_name}"
            )
        else:
            summary["errors"].append(
                f"BPR {bpr['name']}: start_group_flow failed for sweep group "
                f"{sweep_group_id}"
            )


def _active_bprs_for_sweep():
    """Active BatchProgramRuns the sweep operates over (draft/completed
    excluded). Extracted as a seam so tests can scope the global cron to a
    single fixture batch."""
    return frappe.db.sql(
        """
        SELECT name, batch, content_delivery_flow
          FROM "tabBatchProgramRun"
         WHERE status = %s
        """,
        (BPR_ACTIVE,),
        as_dict=True,
    )


def weekly_content_sweep():
    """Weekly cron — runs Monday 06:00 UTC (after auto_advance_batch_week at
    Monday 00:00 bumps Batch.current_calendar_week).

    Phase 1: demote behind students (normal_content_delivery, wk < calendar_week,
             video_done = 0) to normal_escalation via the SHARED SET-BASED
             `_bulk_demote_batch` (the same helper the one-time migration uses,
             L-074): ~8 combo-targeted SQL UPDATEs + one bulk Glific collection
             move per BPR. escalation_type resolved per-combo from ArchetypeConfig.
             Set-based so it can't hang at scale the way the old per-PE loop did
             (L-082).

    Phase 2: for each active BPR, deliver this week's content to current-week
             candidates (wk == calendar_week, video_done = 0) via a temp Glific
             sweep collection + content_delivery_flow.

    Idempotent: Phase 2 candidates are filtered by weekly_video_done = 0, so
    re-running within the same week skips students who already got the video.

    Returns a summary dict (also logged to the scheduler log + Error Log).
    """
    active_bprs = _active_bprs_for_sweep()

    summary = {
        "active_bprs": len(active_bprs),
        "phase1_demoted": 0,
        "phase1_failed": 0,
        "phase1_no_config": 0,
        "phase1_removed_from_main": 0,
        "phase1_added_to_escalation": 0,
        "phase2_triggered": 0,
        "phase2_groups_used": [],
        "bprs_without_content_flow": 0,
        "bprs_with_no_phase2_candidates": 0,
        "errors": [],
    }

    if not active_bprs:
        frappe.logger().info("weekly_content_sweep: no active BPRs — nothing to do.")
        return summary

    _sweep_phase1_demote_behind(active_bprs, summary)
    _sweep_phase2_deliver_current_week(active_bprs, summary)

    # Final summary — logged to the scheduler log AND the Error Log so
    # operators can monitor the run from the Frappe Desk Error Log list view.
    msg = (
        f"weekly_content_sweep DONE: active_bprs={summary['active_bprs']} "
        f"phase1_demoted={summary['phase1_demoted']} "
        f"phase1_failed={summary['phase1_failed']} "
        f"phase1_no_config={summary['phase1_no_config']} "
        f"phase1_removed_from_main={summary['phase1_removed_from_main']} "
        f"phase1_added_to_escalation={summary['phase1_added_to_escalation']} "
        f"phase2_triggered={summary['phase2_triggered']} "
        f"bprs_without_content_flow={summary['bprs_without_content_flow']} "
        f"bprs_with_no_phase2_candidates={summary['bprs_with_no_phase2_candidates']} "
        f"errors={len(summary['errors'])}"
    )
    frappe.logger().info(msg)
    try:
        frappe.log_error(msg, "CR-027 Weekly Sweep Summary")
        frappe.db.commit()
    except Exception:
        frappe.logger().error(f"weekly_content_sweep: failed to log summary: {msg}")

    return summary


# ════════════════════════════════════════════════════════════
# Periodic Glific reconciliation safety net (added 2026-05-25)
# ════════════════════════════════════════════════════════════

def periodic_glific_reconcile():
    """Manual safety-net — push PE truth to Glific for every active batch.

    Status (2026-05-26): registered as a callable, but NOT wired into
    hooks.scheduler_events.cron. The */10-min cron entry was removed once
    the real root cause of the Himani / ST00051295 rendering bug was
    diagnosed as missing createContactsField definitions (fixed via
    `dev_tools.bootstrap_sp_contact_fields`), not value drift. Per L-027
    MVP discipline: production normal operation doesn't create the drift
    condition this guards against, so a continuously running cron isn't
    justified.

    When to use this function:
      - After a console session that ran multiple `frappe.db.set_value`
        calls against Glific-mirrored PE columns.
      - As a periodic operator-driven sanity check (e.g. once a week).
      - As a one-shot cleanup if the team reports gamification-card
        rendering issues that smell like drift.

    Alternatives for single-PE work:
      - `dev_tools.reconcile_pe_to_glific(pe_name)` — single PE.
      - `dev_tools.reconcile_batch_to_glific(batch_name, dry_run=True)` —
        cohort-wide read-only sweep with a per-field drift roll-up. Pass
        `dry_run=False` to push.

    Cost per invocation on a 45-PE cohort: ~45 Glific GraphQL reads +
    writes only for fields that drifted. Typical run completes in ~15
    seconds. Idempotent — re-running on a clean cohort is essentially
    free.

    Why this exists (CLAUDE.md "set_value bypasses save hooks" gotcha
    applied to Glific-mirrored PE columns):

      `frappe.db.set_value` and Frappe Desk UI edits on ProgramEnrollment
      do NOT trigger `_enqueue_contact_field_sync`. Operational backfills,
      QA manual edits, and any path that updates a Glific-mirrored column
      without going through `_apply_transition_to_pe` /
      `dev_tools.update_student_state` therefore leave Glific stale until
      the next state-machine transition happens to re-push.

      Reconcile sweep on 2026-05-25 (palv2-test-BT52231) found 16/45 PEs
      drifting on `total_points`, 2 on `archetype` + `experiment_arm`,
      and a few isolated stream-column drifts — all from set_value
      bypasses (no DLQ entries, no reset events, sync machinery healthy
      when invoked).

    What it does:
      For every active Batch (matched via active BPRs), call
      `dev_tools.reconcile_batch_to_glific(..., dry_run=False)` which
      diffs each PE's Glific-mirrored fields against the PE record and
      pushes only the fields that differ. The per-field roll-up is
      printed to the scheduler log so operators can spot systemic drift.

    Idempotency:
      `reconcile_pe_to_glific` is idempotent — re-running on a clean PE
      produces an empty diff and pushes nothing. Running often is
      essentially free for PEs that aren't drifting.

    Failure isolation:
      Per-batch try/except so one batch's failure doesn't stop the rest.
      Per-PE try/except is inside reconcile_batch_to_glific (already
      logs the FAILED status line).
    """
    from tap_lms.summer_program import dev_tools

    active_batches = frappe.db.sql(
        """
        SELECT DISTINCT batch
          FROM "tabBatchProgramRun"
         WHERE status = %s
        """,
        (BPR_ACTIVE,),
        as_dict=True,
    )

    if not active_batches:
        frappe.logger().info("periodic_glific_reconcile: no active BPRs — nothing to reconcile.")
        return

    total_pes_checked = 0
    total_pushed = 0
    total_mismatches = 0
    for row in active_batches:
        batch_name = row.batch
        if not batch_name:
            continue
        try:
            results = dev_tools.reconcile_batch_to_glific(
                batch_name, dry_run=False, verbose=False,
            )
        except Exception as e:
            frappe.log_error(
                f"periodic_glific_reconcile: batch {batch_name} failed: {e}",
                "SP Periodic Glific Reconcile",
            )
            continue

        batch_pushed = 0
        batch_mismatches = 0
        for pe_name, result in results.items():
            if "error" in result:
                continue
            total_pes_checked += 1
            diff_len = len(result.get("diff") or [])
            batch_mismatches += diff_len
            if result.get("pushed"):
                batch_pushed += 1
        total_pushed += batch_pushed
        total_mismatches += batch_mismatches

        # Only log per-batch when there's actual work — at 10-min cadence
        # the no-drift case dominates and noisy logs hide real signal.
        if batch_mismatches or batch_pushed:
            frappe.logger().info(
                f"periodic_glific_reconcile: batch={batch_name} "
                f"pes={len(results)} mismatches={batch_mismatches} "
                f"pushed={batch_pushed}"
            )

    # Roll-up logs only when work happened, same reason.
    if total_mismatches or total_pushed:
        frappe.logger().info(
            f"periodic_glific_reconcile DONE: pes={total_pes_checked} "
            f"mismatches={total_mismatches} pushed={total_pushed}"
        )


# ════════════════════════════════════════════════════════════
# Async-failure visibility watchers
# Added 2026-05-28 (task #17 / shared Esc R1 + Content R1+R4)
# ════════════════════════════════════════════════════════════
#
# Both the Glific contact-field sync pipeline and the RQ enqueue path used by
# `complete_content`, `_enqueue_contact_field_sync`, and collection-membership
# writes are ASYNC with retry+DLQ but NO ALERTING. A queue that's stalled or
# a DLQ that's filling up is invisible to operators unless they go looking.
# These two cron-driven watchers turn silent failure into an Error Log entry
# that surfaces in the Frappe Desk Error Log list view.
#
# Wired in hooks.scheduler_events.cron at `0 * * * *` (hourly). Each watcher
# is read-only — never re-enqueues, never auto-recovers — by design. Operator
# replay remains explicit.

# Threshold for the RQ queue-depth alert. Anything above this gets an Error
# Log entry. 100 is conservative: a healthy backend processes the SP cohort
# (~45 PEs, infrequent events) with steady-state queue depth ≈ 0; sustained
# 100+ means a worker is wedged or upstream is firehosing.
RQ_QUEUE_DEPTH_ALERT_THRESHOLD = 100


def glific_sync_dlq_watcher():
    """Hourly watcher — alerts on new entries in the SP Glific Sync DLQ.

    Compares the count of Error Log rows with method='SP Glific Sync DLQ —
    manual replay required' added in the last hour against zero. Any new
    entries → write a single summary Error Log so the operator-on-call
    notices.

    Read-only — does NOT replay. Manual replay via
    `dev_tools.reconcile_pe_to_glific(pe_name)` remains the recovery path.
    """
    from tap_lms.summer_program.constants import GLIFIC_SYNC_DLQ_LOG_TITLE

    new_dlq = frappe.db.sql(
        """
        SELECT COUNT(*) AS n
          FROM "tabError Log"
         WHERE method = %s
           AND creation > NOW() AT TIME ZONE 'UTC' - INTERVAL '1 hour'
        """,
        (GLIFIC_SYNC_DLQ_LOG_TITLE,),
        as_dict=True,
    )
    count = new_dlq[0].n if new_dlq else 0

    if count > 0:
        frappe.log_error(
            f"SP Glific Sync DLQ has {count} new entries in the last hour. "
            f"Operator action required — replay manually via "
            f"`dev_tools.reconcile_pe_to_glific(pe_name)` for each stuck PE. "
            f"List the DLQ entries: `SELECT creation, LEFT(error::text, 400) "
            f"FROM \"tabError Log\" WHERE method = "
            f"'{GLIFIC_SYNC_DLQ_LOG_TITLE}' "
            f"ORDER BY creation DESC LIMIT {count};`",
            "SP DLQ Watcher Alert",
        )
    # No new DLQ entries → silent (don't fill Error Log with no-op pings).


def rq_queue_depth_watcher():
    """Hourly watcher — alerts on RQ queues that have backed up past threshold.

    Polls Frappe's wrapper around RQ (`frappe.utils.background_jobs.get_queue`)
    for each queue (default, short, long) and writes an Error Log entry if
    any queue exceeds `RQ_QUEUE_DEPTH_ALERT_THRESHOLD`.

    Why this matters (Content R1+R4): `complete_content` enqueues the SCL
    insert + activity_points + Glific sync into the RQ queue. If a worker is
    wedged or the queue is paused, the webhook returns 200 OK to Glific but
    the work never happens — students appear stuck mid-progress with no
    error surface anywhere.

    Read-only — does NOT restart workers or flush queues. Operator action
    is to inspect the worker log and restart the supervisor process.
    """
    try:
        from rq import Queue
        from frappe.utils.background_jobs import get_redis_conn
    except Exception as e:
        # If RQ isn't importable in this environment, skip silently — this
        # watcher is best-effort, not load-bearing.
        frappe.logger().warning(f"rq_queue_depth_watcher: rq import failed: {e}")
        return

    try:
        conn = get_redis_conn()
    except Exception as e:
        frappe.log_error(
            f"rq_queue_depth_watcher: cannot connect to Redis: {e}",
            "SP Queue Watcher Error",
        )
        return

    # Frappe's standard RQ queues. If the deployment uses custom queue names,
    # add them here. Threshold applies per-queue independently.
    queue_names = ["default", "short", "long"]
    alerts = []
    for name in queue_names:
        try:
            q = Queue(name, connection=conn)
            depth = len(q)
            failed_depth = q.failed_job_registry.count
            if depth > RQ_QUEUE_DEPTH_ALERT_THRESHOLD:
                alerts.append(
                    f"queue={name} depth={depth} (threshold "
                    f"{RQ_QUEUE_DEPTH_ALERT_THRESHOLD})"
                )
            if failed_depth > 0:
                alerts.append(
                    f"queue={name} failed_jobs={failed_depth} "
                    f"(non-zero — operator should review)"
                )
        except Exception as e:
            alerts.append(f"queue={name}: probe failed: {e}")

    if alerts:
        frappe.log_error(
            "RQ queue-depth alert (operator action required):\n" +
            "\n".join(alerts) +
            f"\n\nDiagnostic: in bench console run `from rq import Queue; "
            f"from frappe.utils.background_jobs import get_redis_conn; "
            f"q = Queue('default', connection=get_redis_conn()); "
            f"print(len(q), q.jobs[:5])` to see queued jobs.",
            "SP Queue Watcher Alert",
        )
    # All queues healthy → silent.
