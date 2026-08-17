"""
NOT BEING USED CURRENTLY — DO NOT SUGGEST THIS FILE FOR NEW CODE

Escalation Runner
tap_lms/summer_program/escalation_runner.py

Processes time-based escalation steps for students who haven't submitted.
Runs more frequently than the daily scheduler (e.g., every 2 hours)
because EscalationStep.hours_after_previous can be < 24 hours.

Register in hooks.py:
    scheduler_events = {
        "cron": {
            "0 */2 * * *": [
                "tap_lms.summer_program.escalation_runner.run_escalation_check",
            ]
        }
    }
"""
import frappe
import json
from frappe.utils import now_datetime, getdate, time_diff_in_hours

from tap_lms.glific_integration import start_contact_flow
from tap_lms.summer_program.constants import BPR_ACTIVE
from tap_lms.summer_program.student_progression_sp import (
    _get_current_week,
    _get_escalation_steps,
)


def run_escalation_check():
    """
    Main entry point for escalation processing.
    Finds all active BPRs and checks each enrolled student
    for pending escalation actions.

    Runs every 2 hours via Frappe scheduler cron.
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
            fields=["name", "batch", "escalation_flow"],
        )
        _bpr_count = len(active_bprs)

        for bpr_row in active_bprs:
            if not bpr_row.escalation_flow:
                continue

            try:
                _process_bpr_escalation(bpr_row)
            except Exception as e:
                frappe.log_error(
                    f"Escalation runner error for BPR {bpr_row.name}: {str(e)}",
                    "SP Escalation Runner",
                )
    except Exception as e:
        _status = "error"
        _error = str(e)
        raise
    finally:
        try:
            record_job(
                job_name="run_escalation_check",
                status=_status,
                duration_ms=(_time.monotonic() - _t0) * 1000,
                error=_error,
                bpr_count=_bpr_count,
            )
        except Exception:
            pass


def _process_bpr_escalation(bpr_row):
    """
    Process escalation for a single BatchProgramRun.
    For each enrolled student who hasn't submitted this week:
      - Check if enough hours have passed since last escalation
      - If yes, trigger the escalation flow via Glific
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_row.name)
    batch = frappe.get_doc("Batch", bpr.batch)
    current_week = _get_current_week(batch)

    if current_week <= 0 or current_week > (batch.total_weeks or 0):
        return

    # Get all students in this BPR's collections
    from tap_lms.summer_program.enrollment import _get_students_for_bpr
    student_ids = _get_students_for_bpr(bpr)

    if not student_ids:
        return

    # Find students who haven't submitted this week
    # (batch query for efficiency at 150K scale)
    submitted_students = set(
        frappe.get_all(
            "StudentContentLog",
            filters={
                "student": ["in", student_ids],
                "stage_no": current_week,
                "content_type": "Assignment",
                "action": "completed",
            },
            pluck="student",
        )
    )

    non_submitted = [sid for sid in student_ids if sid not in submitted_students]

    if not non_submitted:
        return

    escalated_count = 0

    for student_id in non_submitted:
        try:
            result = _check_and_escalate_student(
                student_id, batch, bpr, current_week
            )
            if result:
                escalated_count += 1
        except Exception as e:
            frappe.log_error(
                f"Escalation error for student {student_id}: {str(e)}",
                "SP Escalation Runner Student",
            )

    if escalated_count > 0:
        frappe.logger().info(
            f"Escalation: sent {escalated_count} escalations "
            f"for BPR {bpr.name} week {current_week}"
        )


def _check_and_escalate_student(student_id, batch, bpr, current_week):
    """
    Check if a specific student needs escalation and trigger it.

    Returns True if escalation was sent, False otherwise.
    """
    student = frappe.get_doc("Student", student_id)
    steps = _get_escalation_steps(student, batch)

    if not steps:
        return False

    # Get escalation logs for this student/week
    escalation_logs = frappe.get_all(
        "StudentContentLog",
        filters={
            "student": student_id,
            "stage_no": current_week,
            "content_type": "Assignment",
            "action": "started",
            "tier": "Escalation",
        },
        fields=["started_at", "content_id"],
        order_by="started_at desc",
        limit=len(steps),
    )

    sent_count = len(escalation_logs)

    if sent_count >= len(steps):
        return False  # All steps exhausted

    next_step = steps[sent_count]

    # Check timing: has enough time passed since last escalation (or since week start)?
    if escalation_logs:
        last_sent = escalation_logs[0].started_at
        hours_since = time_diff_in_hours(now_datetime(), last_sent)
        if hours_since < next_step.get("hours_after_previous", 24):
            return False  # Not enough time elapsed
    else:
        # First escalation: check hours since content was delivered
        # (approximated as hours since batch week start)
        week_start = _get_week_start_datetime(batch, current_week)
        if week_start:
            hours_since_week = time_diff_in_hours(now_datetime(), week_start)
            if hours_since_week < next_step.get("hours_after_previous", 24):
                return False

    # Time to escalate — trigger the flow
    glific_id = student.glific_id
    if not glific_id:
        return False

    flow_id = bpr.escalation_flow
    # CR-003: step shape uses `escalation_type` (Select) instead of message_type (Data).
    escalation_type = next_step.get("escalation_type", "help_note_a")
    default_results = {
        "escalation_type": escalation_type,
        "escalation_order": str(next_step["escalation_order"]),
        "points_if_submit": str(next_step.get("points_awarded", 0)),
        "week": str(current_week),
    }

    # CR-003 §M6: `parent_call` steps go to Vocallabs, NOT the Glific
    # SP_Escalation flow. The bulk runner has to branch the same way
    # `pe_dispatcher.handle_escalation` does — else parent_call steps
    # fire BOTH a WhatsApp escalation AND a phone call. The PE name
    # comes from the student's active enrollment for this batch.
    if escalation_type == "parent_call":
        pe_name = frappe.db.get_value(
            "ProgramEnrollment",
            {
                "student": student_id,
                "batch": batch.name,
                "program_status": ["in", ["active", "paused"]],
            },
            "name",
        )
        if not pe_name:
            return False
        frappe.enqueue(
            "tap_lms.summer_program.vocallabs.initiate_parent_call",
            queue="long",
            pe_name=pe_name,
            escalation_step=next_step,
        )
        success = True  # enqueue succeeded; vocallabs handles retry/DLQ
    else:
        success = start_contact_flow(
            str(flow_id), str(glific_id), default_results
        )

    if success:
        # Log the escalation
        log = frappe.new_doc("StudentContentLog")
        log.student = student_id
        log.stage_no = current_week
        log.content_type = "Assignment"
        log.content_id = f"escalation_step_{next_step['escalation_order']}"
        log.content_name = f"Escalation: {escalation_type}"
        log.action = "started"
        log.tier = "Escalation"
        log.started_at = now_datetime()
        log.metadata = json.dumps({
            "escalation_order": next_step["escalation_order"],
            "escalation_type": escalation_type,
            "points_if_submit": next_step.get("points_awarded", 0),
            "flow_id": flow_id,
            "source": "summer_program",
        })
        log.insert(ignore_permissions=True)
        frappe.db.commit()

    return success


def _get_week_start_datetime(batch, week):
    """Calculate the datetime when a specific week started."""
    if not batch.start_date:
        return None
    from datetime import timedelta
    start = getdate(batch.start_date)
    week_start = start + timedelta(days=(week - 1) * 7)
    return frappe.utils.get_datetime(week_start)
