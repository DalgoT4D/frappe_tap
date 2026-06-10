"""
Student Reactivation API
tap_lms/summer_program/reactivation.py

API A5: reactivate_student — resume a paused student.

Called by SP_Incoming_Router when a paused student sends any message.
The router reads @contact.program_status and routes here for `paused`.

CR-003: the `paused_no_activity` state is retired. Students drop at grace
expiry; re-engagement is inbound-only. The router now routes dropped
students to a separate `rejoin` branch (Glific-side), not through this
API — but if a dropped student ends up here, we surface a "dropped"
status so the router can recover gracefully. The only live re-entry path
in this API is for `paused_binge` students whose calendar caught up.
"""
import frappe
from frappe import _

from tap_lms.summer_program.constants import (
    STATE_PAUSED_BINGE,
    PAUSED_STATES, TERMINAL_STATES,
    PROGRAM_PAUSED, PATH_CORE, PATH_REMEDIAL,STATE_PROGRAM_PAUSED
)
from tap_lms.summer_program.state_machine import (
    get_active_pe,
    t21_binge_resume,t21_a_resume_paused
)
from tap_lms.summer_program.event_log import log_event
from tap_lms.summer_program.utils import sp_safe_endpoint


@frappe.whitelist(allow_guest=False)
@sp_safe_endpoint("reactivate_student")
def reactivate_student(student_id, **_glific_kwargs):
    """
    API A5: reactivate_student

    `**_glific_kwargs` absorbs Glific-injected fields per task #89 — ignored.

    Resumes a paused student. Called by SP_Incoming_Router when a paused
    student sends any WhatsApp message.

    Logic (post-CR-003):
      - paused_binge → T21 → normal_content_delivery (if calendar allows)
      - paused_no_activity is retired (no new transition writes it); legacy
        rows are migrated to program_dropped by the CR-003 patch.

    Args:
        student_id: Student name, glific_id, or phone

    Response (via frappe.local.response per docs/api-standard-glific.md Rule 1):
        Flat dict with `success` + `status` + reactivation details.
    """
    student_id = _resolve_student(student_id)
    if not student_id:
        frappe.local.response.update({
            "success": False, "status": "not_found",
            "error_detail": "Student not found",
        })
        return

    pe = get_active_pe(student_id)
    if not pe:
        frappe.local.response.update({
            "success": False, "status": "no_active_enrollment",
            "error_detail": "No active ProgramEnrollment",
        })
        return

    # Must be in a paused state
    if pe.program_status != PROGRAM_PAUSED:
        frappe.local.response.update({
            "success": False,
            "status": "not_paused",
            "error_detail": "Student is not paused",
            "program_status": pe.program_status,
            "resolved_flow_state": pe.resolved_flow_state,
        })
        return

    if pe.resolved_flow_state in TERMINAL_STATES:
        frappe.local.response.update({
            "success": False,
            "status": "terminal_state",
            "error_detail": "Student in terminal state",
            "resolved_flow_state": pe.resolved_flow_state,
        })
        return

    # ── Handle paused_binge ─────────────────────────────────
    if pe.resolved_flow_state == STATE_PAUSED_BINGE:
        # Check if calendar now allows advancement
        batch = frappe.get_doc("Batch", pe.batch)
        max_allowed = (batch.current_calendar_week or 1) + 1
        next_week = (pe.current_week or 1) + 1

        if next_week <= max_allowed:
            t21_binge_resume(pe, "glific_flow")

            log_event(pe, "resume", trigger_source="glific_flow",
                      details={"reactivation_type": "binge_eligible"})

            # Removed mid-handler commit per L-017 — Frappe commits at request-end.
            frappe.local.response.update({
                "success": True,
                "status": "reactivated",
                "resolved_flow_state": pe.resolved_flow_state,
                "current_week": pe.current_week,
            })
            return
        else:
            # Still binge-limited — can't resume yet
            frappe.local.response.update({
                "success": True,
                "status": "still_paused",
                "reason": "binge_limit",
                "current_week": pe.current_week,
                "max_allowed_week": max_allowed,
                "resolved_flow_state": pe.resolved_flow_state,
            })
            return
    elif pe.resolved_flow_state == STATE_PROGRAM_PAUSED:
        t21_a_resume_paused(pe, "glific_flow")

        log_event(pe, "resume", trigger_source="glific_flow",
                    details={"reactivation_type": "program resumed from reactivation"})

        # Removed mid-handler commit per L-017 — Frappe commits at request-end.
        frappe.local.response.update({
            "success": True,
            "status": "reactivated",
            "resolved_flow_state": pe.resolved_flow_state,
            "current_week": pe.current_week,
        })
        return


    frappe.local.response.update({
        "success": False,
        "status": "unhandled_pause_state",
        "error_detail": "Unhandled pause state",
        "resolved_flow_state": pe.resolved_flow_state,
    })


def _resolve_student(identifier):
    """Delegate to shared utility."""
    from tap_lms.summer_program.utils import resolve_student
    return resolve_student(identifier)
