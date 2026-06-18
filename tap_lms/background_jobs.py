import time

import frappe
import requests
from frappe.utils.background_jobs import enqueue

from .glific_integration import (
    add_contact_to_group,
    create_or_get_teacher_group_for_batch,
    optin_contact,
    start_contact_flow,
)
from .monitoring import record_job

# Remove the import: from .api import get_active_batch_for_school


def process_glific_actions(
    teacher_id,
    phone,
    first_name,
    school,
    school_name,
    language,
    model_name,
    batch_name,
    batch_id,
):
    _t0 = time.monotonic()
    _status = "success"
    _error = None
    try:
        # Optin the contact
        try:
            optin_success = optin_contact(phone, first_name)
        except requests.exceptions.RequestException as e:
            frappe.log_error(
                title="Glific timeout (degraded)",
                message=f"process_glific_actions: optin_contact timed out for {phone}: {e}",
            )
            optin_success = False
        if not optin_success:
            emit(
                severity="ERROR",
                message="teacher_optin_failed",
                teacher_id=teacher_id,
                phone=phone,
            )
            _status = "error"
            _error = "optin_failed"
            return

        # Get the Glific ID
        glific_id = frappe.db.get_value("Teacher", teacher_id, "glific_id")
        if not glific_id:
            emit(
                severity="ERROR",
                message="teacher_glific_id_missing_in_background_job",
                teacher_id=teacher_id,
            )
            _status = "error"
            _error = "glific_id_not_found"
            return

        # Create or get the teacher group for this batch
        # Now we use the passed batch_name and batch_id directly
        if batch_id and batch_id != "no_active_batch_id" and batch_name:
            try:
                teacher_group = create_or_get_teacher_group_for_batch(
                    batch_name, batch_id
                )

                if teacher_group and teacher_group.get("group_id"):
                    # Add the teacher to the group
                    try:
                        group_added = add_contact_to_group(
                            glific_id, teacher_group["group_id"]
                        )
                    except requests.exceptions.RequestException as e:
                        frappe.log_error(
                            title="Glific timeout (degraded)",
                            message=f"process_glific_actions: add_contact_to_group timed out for teacher {teacher_id}: {e}",
                        )
                        group_added = False
                    if group_added:
                        emit(
                            severity="INFO",
                            message="teacher_added_to_group_background",
                            teacher_id=teacher_id,
                            glific_id=glific_id,
                            group_label=teacher_group["label"],
                        )
                    else:
                        emit(
                            severity="WARNING",
                            message="teacher_group_addition_failed_background",
                            teacher_id=teacher_id,
                            glific_id=glific_id,
                            group_label=teacher_group["label"],
                        )
                else:
                    emit(
                        severity="WARNING",
                        message="teacher_group_creation_failed_background",
                        teacher_id=teacher_id,
                        batch_id=batch_id,
                    )

            except Exception as e:
                # Log error but don't stop the flow
                emit(
                    severity="ERROR",
                    message="teacher_group_management_error",
                    teacher_id=teacher_id,
                    error=str(e),
                )
        else:
            emit(
                severity="INFO",
                message="teacher_group_skipped_no_batch",
                teacher_id=teacher_id,
            )

        # Start the "Teacher Web Onboarding Flow" in Glific
        flow = frappe.db.get_value(
            "Glific Flow", {"label": "Teacher Web Onboarding Flow"}, "flow_id"
        )
        if flow:
            default_results = {
                "teacher_id": teacher_id,
                "school_id": school,
                "school_name": school_name,
                "language": language,
                "model": model_name,
            }
            flow_started = start_contact_flow(flow, glific_id, default_results)
            if flow_started:
                emit(
                    severity="INFO",
                    message="teacher_onboarding_flow_started_background",
                    teacher_id=teacher_id,
                    glific_id=glific_id,
                    flow_id=flow,
                )
            else:
                emit(
                    severity="ERROR",
                    message="teacher_onboarding_flow_failed_background",
                    teacher_id=teacher_id,
                    glific_id=glific_id,
                    flow_id=flow,
                )
                _status = "warning"
                _error = "flow_start_failed"
        else:
            emit(
                severity="ERROR",
                message="teacher_onboarding_flow_not_found",
                teacher_id=teacher_id,
            )
            _status = "error"
            _error = "flow_not_found"

    except Exception as e:
        _status = "error"
        _error = str(e)
        import traceback

        emit(
            severity="ERROR",
            message="process_glific_actions_exception",
            teacher_id=teacher_id,
            error=str(e),
            traceback=traceback.format_exc(),
        )
    finally:
        try:
            record_job(
                job_name="process_glific_actions",
                status=_status,
                duration_ms=(time.monotonic() - _t0) * 1000,
                error=_error,
                teacher_id=teacher_id,
            )
        except Exception:
            pass


def enqueue_glific_actions(
    teacher_id,
    phone,
    first_name,
    school,
    school_name,
    language,
    model_name,
    batch_name,
    batch_id,
):
    enqueue(
        process_glific_actions,
        queue="short",
        timeout=300,
        teacher_id=teacher_id,
        phone=phone,
        first_name=first_name,
        school=school,
        school_name=school_name,
        language=language,
        model_name=model_name,
        batch_name=batch_name,
        batch_id=batch_id,
    )
