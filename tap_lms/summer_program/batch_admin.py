"""
Batch Admin APIs
tap_lms/summer_program/batch_admin.py

API A7: update_batch_week — advances batch calendar, triggers binge-paused eligibility
Extra: admin_drop_student — drops a student from the program
"""
import frappe
from frappe import _
from frappe.utils import now_datetime, cint

from tap_lms.summer_program.constants import (
    STATE_PAUSED_BINGE, STATE_PROGRAM_DROPPED,
    PROGRAM_PAUSED, PAUSE_BINGE_LIMIT,
    ACTION_PAUSE_CHECK,
    TERMINAL_STATES,
)
from tap_lms.summer_program.state_machine import (
    get_active_pe,
    t24_admin_drop,
)
from tap_lms.summer_program.event_log import log_event


@frappe.whitelist(allow_guest=False)
def update_batch_week(batch_id, new_calendar_week=None):
    """
    API A7: update_batch_week

    Advances batch.current_calendar_week by 1 (or to a specific value).
    Then queries all binge-paused students and schedules pause_check
    for those who are now eligible to resume.

    Called by:
      - Weekly cron (Monday 00:00)
      - Admin action

    Args:
        batch_id: Batch document name
        new_calendar_week: Optional explicit week number.
                          If omitted, increments by 1.

    Returns:
        dict with updated week and count of eligible students
    """
    if not frappe.db.exists("Batch", batch_id):
        return {"success": False, "error": "Batch not found"}

    batch = frappe.get_doc("Batch", batch_id)
    old_week = batch.current_calendar_week or 0

    if new_calendar_week:
        new_week = cint(new_calendar_week)
    else:
        new_week = old_week + 1

    # Validate: only allow increment by 1 per real week
    if new_week > old_week + 1:
        return {
            "success": False,
            "error": f"Cannot skip weeks. Current: {old_week}, requested: {new_week}",
        }

    if new_week <= old_week:
        return {
            "success": False,
            "error": f"New week ({new_week}) must be > current ({old_week})",
        }

    # Update batch
    batch.current_calendar_week = new_week
    batch.save(ignore_permissions=True)

    # ── Find binge-paused students eligible to resume ──────
    max_allowed_week = new_week + 1

    eligible_pes = frappe.get_all(
        "ProgramEnrollment",
        filters={
            "batch": batch_id,
            "program_status": PROGRAM_PAUSED,
            "pause_reason": PAUSE_BINGE_LIMIT,
            "resolved_flow_state": STATE_PAUSED_BINGE,
        },
        fields=["name", "student", "current_week"],
    )

    # Filter: only those whose next week <= max_allowed_week
    eligible = [
        pe for pe in eligible_pes
        if (pe.current_week or 0) + 1 <= max_allowed_week
    ]

    # Schedule pause_check for eligible students
    scheduled_count = 0
    for pe_row in eligible:
        try:
            frappe.db.set_value("ProgramEnrollment", pe_row.name, {
                "next_action_at": now_datetime(),
                "next_action_type": ACTION_PAUSE_CHECK,
                "max_allowed_week": max_allowed_week,
            })
            scheduled_count += 1
        except Exception as e:
            frappe.log_error(
                f"Pause check scheduling error for PE {pe_row.name}: {str(e)}",
                "SP Batch Admin",
            )

    # Also update max_allowed_week for ALL active PEs in this batch
    frappe.db.sql("""
        UPDATE `tabProgramEnrollment`
        SET max_allowed_week = %s
        WHERE batch = %s
          AND program_status NOT IN ('completed', 'dropped')
    """, (max_allowed_week, batch_id))

    # Removed mid-handler commit per L-017 — Frappe commits at request-end.

    return {
        "success": True,
        "batch": batch_id,
        "old_calendar_week": old_week,
        "new_calendar_week": new_week,
        "max_allowed_week": max_allowed_week,
        "binge_paused_total": len(eligible_pes),
        "students_eligible_for_resume": scheduled_count,
    }


@frappe.whitelist(allow_guest=False)
def admin_drop_student(student_id, batch_id=None, reason=None):
    """
    Drop a student from the Summer Program. (UC28)

    Transitions to program_dropped (T24), cancels all scheduling,
    updates Glific contact fields, removes from collections.

    Args:
        student_id: Student document name
        batch_id: Optional batch to drop from (uses active PE if omitted)
        reason: Optional reason for dropping

    Returns:
        dict with drop result
    """
    if not frappe.db.exists("Student", student_id):
        return {"success": False, "error": "Student not found"}

    pe = get_active_pe(student_id, batch_id)
    if not pe:
        return {"success": False, "error": "No active ProgramEnrollment"}

    if pe.resolved_flow_state in TERMINAL_STATES:
        return {
            "success": False,
            "error": "Student already in terminal state",
            "resolved_flow_state": pe.resolved_flow_state,
        }

    old_state = pe.resolved_flow_state

    # T24: ANY → program_dropped
    t24_admin_drop(pe, "admin")

    log_event(pe, "label_changed", old_value=old_state,
              new_value=STATE_PROGRAM_DROPPED, trigger_source="admin",
              details={"reason": reason or "admin_action"})

    # Removed mid-handler commit per L-017 — Frappe commits at request-end.

    return {
        "success": True,
        "student_id": student_id,
        "enrollment": pe.name,
        "old_state": old_state,
        "new_state": pe.resolved_flow_state,
        "program_status": pe.program_status,
    }


def auto_advance_batch_week():
    """
    Scheduled job: runs every Monday at 00:00 (via hooks cron).

    Finds all active Summer Program batches and advances their
    calendar week by 1. Delegates to update_batch_week for each batch.
    """
    from tap_lms.summer_program.constants import BPR_ACTIVE

    # Find batches with active BatchProgramRun records
    active_batches = frappe.db.sql("""
        SELECT DISTINCT bpr.batch
        FROM `tabBatchProgramRun` bpr
        WHERE bpr.status = %s
    """, (BPR_ACTIVE,), as_dict=True)

    results = []
    for row in active_batches:
        try:
            result = update_batch_week(row.batch)
            results.append(result)
        except Exception as e:
            frappe.log_error(
                f"Auto-advance failed for batch {row.batch}: {str(e)}",
                "SP Auto Advance Batch Week",
            )
            results.append({"success": False, "batch": row.batch, "error": str(e)})

    frappe.logger().info(f"auto_advance_batch_week: processed {len(results)} batches")
    return results
