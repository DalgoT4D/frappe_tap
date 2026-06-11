"""
Summer Program Scheduler
tap_lms/summer_program/scheduler.py

PAL Scheduler — runs as a Frappe scheduled task.
Handles collection-based and per-student flow triggers.

Register in hooks.py:
    scheduler_events = {
        "daily": [
            "tap_lms.summer_program.scheduler.run_daily_actions",
        ]
    }
"""
import frappe
from frappe.utils import now_datetime, getdate, today, date_diff

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
    PROGRAM_PAUSED,
)
# CR-003: ACTION_REENGAGEMENT and ACTION_GRACE_REMINDER removed from constants.
# _run_reengagement and _run_grace_notifications were the daily-scheduler
# entry points that consumed those action types; both functions are deleted
# from this file (re-engagement is now inbound-only; grace reminders are
# replaced by per-week escalation steps).
from tap_lms.summer_program.glific_extensions import start_group_flow


def run_daily_actions():
    """
    Main scheduler entry point. Called daily by Frappe scheduler.
    Finds all active BatchProgramRuns and processes scheduled actions.
    """
    import time as _time
    from tap_lms.monitoring import record_job
    _t0 = _time.monotonic()
    _status = "success"
    _error = None
    _bpr_count = 0

    try:
        active_bprs = frappe.get_all(
            "BatchProgramRun",
            filters={"status": BPR_ACTIVE},
            fields=["name"],
        )
        _bpr_count = len(active_bprs)

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
    except Exception as e:
        _status = "error"
        _error = str(e)
        raise
    finally:
        try:
            record_job(
                job_name="run_daily_actions",
                status=_status,
                duration_ms=(_time.monotonic() - _t0) * 1000,
                error=_error,
                bpr_count=_bpr_count,
            )
        except Exception:
            pass


def _process_bpr_actions(bpr, batch):
    """
    Determine which actions to run today for a given BPR.
    """
    current_week = _get_current_week(batch)
    total_weeks = batch.total_weeks or 0
    grace_days = batch.grace_window_days or 0

    # ── Collection-based actions ─────────────────────────
    # Content delivery: runs daily for active batches
    _run_collection_action(bpr, ACTION_CONTENT_DELIVERY)

    # Escalation: for Dormant and Fence Sitter collections
    _run_escalation(bpr)

    # CR-003: proactive re-engagement removed. Dropped students are
    # re-engaged via SP_Incoming_Router when they send an inbound
    # message; the backend does not push re-engagement nudges.

    # ── Per-student actions ──────────────────────────────
    # CR-003: proactive grace notifications removed. The per-week
    # escalation steps within the active week ARE the reminders;
    # grace expiry is policed by handle_grace_check in pe_dispatcher.

    # Program complete: when batch reaches total_weeks
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
