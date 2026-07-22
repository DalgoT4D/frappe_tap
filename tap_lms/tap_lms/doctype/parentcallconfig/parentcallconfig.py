# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ParentCallConfig(Document):
    pass


@frappe.whitelist()
def preview_prompt(config_name, pe_name):
    """Render the status_template with real student data for admin preview.

    Called from the Preview Prompt dialog in the ParentCallConfig desk view.
    Read-only — no writes, no real calls.

    Returns:
        dict with rendered prompt text, full context dict, and student info.
    """
    try:
        config = frappe.get_doc("ParentCallConfig", config_name)
    except frappe.DoesNotExistError:
        frappe.throw(f"ParentCallConfig '{config_name}' not found.")

    try:
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
    except frappe.DoesNotExistError:
        frappe.throw(
            f"ProgramEnrollment '{pe_name}' not found. "
            f"Enter the exact PE document name (e.g. 7a5r04h8fk)."
        )

    student = frappe.get_doc("Student", pe.student)

    from tap_lms.summer_program.voice_context import build_student_context
    from tap_lms.summer_program.vocallabs import _render_status_template

    ctx = build_student_context(pe, student)

    step = {
        "escalation_order": 1,
        "escalation_type": pe.current_escalation_type or "parent_call",
        "hours_after_previous": 0,
        "points_awarded": 0,
    }

    rendered = _render_status_template(
        config.status_template or "(no template set — add text to status_template field)",
        pe, student, step
    )

    return {
        "ok": True,
        "rendered": rendered,
        "student_name": student.name1 or student.name,
        "pe_name": pe_name,
        "situation": ctx.get("situation", "unknown"),
        "language": pe.language or "(not set)",
        "archetype": pe.archetype or "(not set)",
        "context": {k: v for k, v in ctx.items() if v},
    }
