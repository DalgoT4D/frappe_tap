"""
Save Submission API
tap_lms/summer_program/save_submission.py

API A3: save_submission -- the summer program submission handler.

Creates assessment-style Submission records while preserving the summer-program
state machine behavior:
  - resolve Student and active ProgramEnrollment
  - normalize raw submission text or URL
  - atomically claim the primary submission
  - calculate points
  - create Submission with summer-program context
  - apply ProgramEnrollment state transition
  - enqueue feedback processing asynchronously
"""
import json
from urllib.parse import urlparse

import frappe
from frappe.utils import cint, getdate, now_datetime, today

from tap_lms.summer_program.constants import TERMINAL_STATES
from tap_lms.summer_program.event_log import log_event
from tap_lms.summer_program.state_machine import (
    apply_submission_transition,
    get_active_pe,
)


URL_SUBMISSION_TYPES = {"audio", "image", "video"}


# TEMP_SUBMISSION_QUEUE_DEBUG_START
def _debug_log(message, **context):
    try:
        frappe.logger("submission").info(
            f"[TEMP_SUBMISSION_QUEUE_DEBUG] {message} | {json.dumps(context, default=str)}"
        )
    except Exception:
        frappe.logger("submission").info(f"[TEMP_SUBMISSION_QUEUE_DEBUG] {message}")
# TEMP_SUBMISSION_QUEUE_DEBUG_END


@frappe.whitelist(allow_guest=True)
def save_submission(student_id, submission, week=None, assignment_id=None):
    """
    Atomic idempotent submission handler.

    Args:
        student_id: Student name, glific_id, or phone.
        submission: A URL, text, or emoji submission. A submission contains
            either URL content or text content, not both.
        week: Override week number. Defaults to ProgramEnrollment.current_week.
        assignment_id: Assignment ID from get_content_details API.

    Returns:
        Pal-style response with submission_id.
    """
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "save_submission:start",
        raw_student_id=student_id,
        week=week,
        assignment_id=assignment_id,
        has_submission=bool(submission),
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    student_id = _resolve_student(student_id)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("save_submission:student_resolved", student_id=student_id)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    if not student_id:
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("save_submission:exit_student_not_found")
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        return {"success": False, "error": "Student not found"}

    pe = get_active_pe(student_id)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "save_submission:active_pe_loaded",
        student_id=student_id,
        pe_name=getattr(pe, "name", None),
        pe_state=getattr(pe, "resolved_flow_state", None),
        pe_label=getattr(pe, "journey_label", None),
        pe_week=getattr(pe, "current_week", None),
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    if not pe:
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("save_submission:exit_no_active_pe", student_id=student_id)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        return {"success": False, "error": "No active ProgramEnrollment"}

    current_week = cint(week) or pe.current_week or 1
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("save_submission:week_resolved", current_week=current_week)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    if pe.resolved_flow_state in TERMINAL_STATES:
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "save_submission:exit_terminal_state",
            pe_name=pe.name,
            resolved_flow_state=pe.resolved_flow_state,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        return {
            "success": False,
            "error": "Student in terminal state",
            "resolved_flow_state": pe.resolved_flow_state,
        }

    payload = _normalize_submission_payload(submission)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("save_submission:payload_normalized", payload=payload)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    is_primary = _try_claim_primary(pe, current_week)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "save_submission:primary_claim_result",
        pe_name=pe.name,
        is_primary=is_primary,
        journey_label=pe.journey_label,
        submission_count=pe.submission_count,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    points = _calculate_points(pe) if is_primary else 0
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("save_submission:points_calculated", is_primary=is_primary, points=points)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    submission_doc = _create_submission(
        pe=pe,
        student_id=student_id,
        week=current_week,
        assignment_id=assignment_id,
        payload=payload,
        is_primary=is_primary,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "save_submission:submission_created",
        submission_id=getattr(submission_doc, "name", None),
        status=getattr(submission_doc, "status", None),
        submission_type=getattr(submission_doc, "submission_type", None),
        is_primary=is_primary,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    if is_primary:
        transition_id, success = apply_submission_transition(
            pe, points=points, trigger_source="flow_callback"
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "save_submission:primary_transition_applied",
            pe_name=pe.name,
            transition_id=transition_id,
            transition_success=success,
            resolved_flow_state=pe.resolved_flow_state,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
    else:
        from tap_lms.summer_program.state_machine import t22_duplicate_submission

        t22_duplicate_submission(pe, "flow_callback")
        transition_id = "T22"
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("save_submission:duplicate_transition_applied", pe_name=pe.name)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

    _update_engagement(student_id)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("save_submission:engagement_updated", student_id=student_id)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    log_event(
        pe,
        "submission_received",
        trigger_source="flow_callback",
        details={
            "is_primary": is_primary,
            "submission_type": payload["submission_type"],
            "points_awarded": points,
            "week": current_week,
            "escalation_step_at_submit": pe.last_escalation_step or 0,
            "transition": transition_id,
            "submission_id": submission_doc.name if submission_doc else None,
        },
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "save_submission:event_logged",
        pe_name=pe.name,
        submission_id=submission_doc.name if submission_doc else None,
        transition_id=transition_id,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    if is_primary and submission_doc:
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "save_submission:queue_condition_met",
            submission_id=submission_doc.name,
            submission_type=submission_doc.submission_type,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        _queue_submission_processing(
            submission_doc,
            pe_context=_build_pe_context(pe),
        )
    else:
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "save_submission:queue_skipped",
            is_primary=is_primary,
            has_submission_doc=bool(submission_doc),
            submission_id=getattr(submission_doc, "name", None),
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

    frappe.db.commit()
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "save_submission:commit_complete",
        submission_id=submission_doc.name if submission_doc else None,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    return _build_submission_response(
        pe=pe,
        student_id=student_id,
        submission_doc=submission_doc,
        is_primary=is_primary,
        points=points,
        week=current_week,
    )


# =============================================================================
# SUBMISSION NORMALIZATION
# =============================================================================


def _normalize_submission_payload(submission):
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "_normalize_submission_payload:start",
        input_type=type(submission).__name__,
        input_length=len(submission) if isinstance(submission, str) else None,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    if not isinstance(submission, str) or not submission.strip():
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("_normalize_submission_payload:invalid_submission")
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        frappe.throw("Submission is required")

    submission = submission.strip()

    if _looks_like_url(submission):
        payload = {
            "submission_type": _infer_url_submission_type(submission),
            "submission_text": None,
            "submission_url": submission,
        }
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("_normalize_submission_payload:url_payload", payload=payload)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        return payload

    submission_type = "emoji" if _contains_only_emoji(submission) else "text"
    payload = {
        "submission_type": submission_type,
        "submission_text": submission,
        "submission_url": None,
    }
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("_normalize_submission_payload:text_payload", submission_type=submission_type)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    return payload


def _looks_like_url(submission):
    parsed = urlparse(submission.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _infer_url_submission_type(submission):
    path = urlparse(submission.strip()).path.lower()

    audio_extensions = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac")
    image_extensions = (
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
        ".heic",
    )
    video_extensions = (
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".m4v",
        ".3gp",
        ".mpeg",
    )

    if path.endswith(audio_extensions):
        return "audio"
    if path.endswith(video_extensions):
        return "video"
    if path.endswith(image_extensions):
        return "image"

    return "image"


def _contains_only_emoji(submission):
    text = submission.strip()
    if not text:
        return False

    return not any(char.isalnum() for char in text)


def _to_assessment_submission_type(submission_type):
    mapping = {
        "text_word": "text",
        "voice_note": "audio",
        "photo": "image",
        "photo_video_artefact": "image",
        "voice_note_text_summary": "audio",
    }
    return mapping.get(submission_type or "", submission_type or "")


# =============================================================================
# ATOMIC PRIMARY CLAIM
# =============================================================================


def _try_claim_primary(pe, week):
    """
    Atomically claim primary submission for this week.
    Returns True if this is the primary submission, False if duplicate.
    """
    pre_submission_labels = [
        "enrolled",
        "content_delivered",
        "grace_window",
        "resumed",
        "week_advanced",
    ]

    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "_try_claim_primary:start",
        pe_name=pe.name,
        week=week,
        journey_label=pe.journey_label,
        pre_submission_labels=pre_submission_labels,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    try:
        result = frappe.db.sql(
            """
            UPDATE `tabProgramEnrollment`
            SET journey_label = 'submitted',
                last_label_change_at = NOW(),
                submission_count = COALESCE(submission_count, 0) + 1,
                last_submission_at = NOW()
            WHERE name = %s
              AND journey_label IN %s
            RETURNING name
        """,
            (pe.name, pre_submission_labels),
        )
        rows_affected = len(result) if result else 0
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "_try_claim_primary:returning_update_complete",
            pe_name=pe.name,
            rows_affected=rows_affected,
            raw_result=result,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
    except Exception as e:
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "_try_claim_primary:returning_update_failed_fallback",
            pe_name=pe.name,
            error=str(e),
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        frappe.db.sql(
            """
            UPDATE `tabProgramEnrollment`
            SET journey_label = 'submitted',
                last_label_change_at = NOW(),
                submission_count = COALESCE(submission_count, 0) + 1,
                last_submission_at = NOW()
            WHERE name = %s
              AND journey_label IN %s
        """,
            (pe.name, pre_submission_labels),
        )
        rows_affected = frappe.db._cursor.rowcount
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "_try_claim_primary:fallback_update_complete",
            pe_name=pe.name,
            rows_affected=rows_affected,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

    pe.reload()
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "_try_claim_primary:reloaded",
        pe_name=pe.name,
        rows_affected=rows_affected,
        is_primary=rows_affected > 0,
        journey_label=pe.journey_label,
        submission_count=pe.submission_count,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    return rows_affected > 0


# =============================================================================
# POINTS CALCULATION
# =============================================================================


def _calculate_points(pe):
    from tap_lms.summer_program.student_progression_sp import _get_escalation_steps

    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "_calculate_points:start",
        pe_name=pe.name,
        student=pe.student,
        batch=pe.batch,
        last_escalation_step=pe.last_escalation_step,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    student = frappe.get_doc("Student", pe.student)
    batch = frappe.get_doc("Batch", pe.batch)
    steps = _get_escalation_steps(student, batch)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("_calculate_points:steps_loaded", step_count=len(steps) if steps else 0)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END

    if not steps:
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("_calculate_points:no_steps_return_zero")
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        return 0

    sent_count = pe.last_escalation_step or 0

    if sent_count == 0:
        points = steps[0].get("points_awarded", 0)
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("_calculate_points:before_escalation", points=points)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        return points

    if sent_count < len(steps):
        points = steps[sent_count].get("points_awarded", 0)
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("_calculate_points:matched_escalation_step", sent_count=sent_count, points=points)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        return points

    points = steps[-1].get("points_awarded", 0)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("_calculate_points:last_step_fallback", sent_count=sent_count, points=points)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    return points


# =============================================================================
# SUBMISSION RECORD
# =============================================================================


def _create_submission(pe, student_id, week, assignment_id, payload, is_primary):
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "_create_submission:start",
        pe_name=pe.name,
        student_id=student_id,
        week=week,
        assignment_id=assignment_id,
        submission_type=payload.get("submission_type"),
        is_primary=is_primary,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    doc = frappe.new_doc("Submission")
    doc.assign_id = assignment_id
    doc.student_id = student_id
    doc.submission_type = payload["submission_type"]
    doc.submission_text = payload["submission_text"]
    doc.submission_url = payload["submission_url"]
    doc.status = "Pending" if is_primary else "Completed"
    doc.program_enrollment = pe.name
    doc.week = week
    doc.escalation_step_at_submit = pe.last_escalation_step or 0
    doc.is_primary = 1 if is_primary else 0
    doc.created_at = now_datetime()
    doc.insert(ignore_permissions=True)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "_create_submission:inserted",
        submission_id=doc.name,
        status=doc.status,
        is_primary=doc.is_primary,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    return doc


def _build_submission_response(pe, student_id, submission_doc, is_primary, points, week):
    return {
        "success": True,
        "status": "accepted" if is_primary else "duplicate",
        "is_primary": is_primary,
        "points_awarded": points,
        "submission_count": pe.submission_count or 1,
        "week": week,
        "resolved_flow_state": pe.resolved_flow_state,
        "next_action_type": pe.next_action_type or "",
        "next_action_at": str(pe.next_action_at) if pe.next_action_at else "",
        "program_status": pe.program_status or "",
        "current_path": pe.current_path or "",
        "student_id": student_id,
        "submission_id": submission_doc.name if submission_doc else None,
    }


# =============================================================================
# ASYNC FEEDBACK QUEUE
# =============================================================================


def _build_pe_context(pe):
    context = {
        "program_enrollment": pe.name,
        "archetype": pe.archetype,
        "experiment_arm": pe.experiment_arm,
        "expected_submission_type": _to_assessment_submission_type(
            pe.current_expected_submission_type
        ),
        "language": getattr(pe, "language", ""),
        "batch": pe.batch,
        "current_week": pe.current_week,
        "current_path": pe.current_path,
        "current_tier": pe.current_tier,
        "course_level": pe.course_level,
        "last_escalation_step": pe.last_escalation_step,
    }
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("_build_pe_context:built", pe_context=context)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    return context


def _queue_submission_processing(submission_doc, pe_context):
    queue_name = (
        "long"
        if submission_doc.submission_type in URL_SUBMISSION_TYPES
        else "default"
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "_queue_submission_processing:start",
        submission_id=submission_doc.name,
        submission_type=submission_doc.submission_type,
        queue=queue_name,
        enqueue_after_commit=True,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    frappe.enqueue(
        "tap_lms.summer_program.save_submission.process_submission_async",
        queue=queue_name,
        timeout=600,
        enqueue_after_commit=True,
        submission_id=submission_doc.name,
        submission_url=submission_doc.submission_url,
        pe_context=pe_context,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "_queue_submission_processing:frappe_enqueue_called",
        submission_id=submission_doc.name,
        queue=queue_name,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END


def process_submission_async(submission_id, submission_url=None, pe_context=None):
    """
    Upload URL submissions to GCS, mark the record Processing, and enqueue
    feedback processing. Text and emoji submissions skip GCS upload.
    """
    pe_context = pe_context or {}
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log(
        "process_submission_async:start",
        submission_id=submission_id,
        has_submission_url=bool(submission_url),
        pe_context=pe_context,
    )
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    try:
        submission = frappe.get_doc("Submission", submission_id)
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "process_submission_async:submission_loaded",
            submission_id=submission.name,
            status=submission.status,
            submission_type=submission.submission_type,
            submission_url=submission.submission_url,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

        if submission.submission_type in URL_SUBMISSION_TYPES:
            from tap_lms.imgana.gcs_client import upload_to_gcs

            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "process_submission_async:gcs_upload_start",
                submission_id=submission.name,
                source_url=submission_url,
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END
            uploaded_url = upload_to_gcs(submission_url, submission.name)
            submission.submission_url = uploaded_url
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "process_submission_async:gcs_upload_complete",
                submission_id=submission.name,
                uploaded_url=uploaded_url,
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END
        else:
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "process_submission_async:gcs_upload_skipped",
                submission_id=submission.name,
                submission_type=submission.submission_type,
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END

        submission.status = "Processing"
        submission.upload_error_log = None
        submission.save(ignore_permissions=True)
        frappe.db.commit()
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "process_submission_async:submission_marked_processing",
            submission_id=submission.name,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

        enqueue_submission(submission.name, pe_context=pe_context)
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("process_submission_async:enqueue_submission_complete", submission_id=submission.name)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

    except Exception as e:
        frappe.db.rollback()
        frappe.logger("submission").error(
            f"Error in background processing for submission {submission_id}: {str(e)}"
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "process_submission_async:error",
            submission_id=submission_id,
            error=str(e),
            traceback=frappe.get_traceback(),
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

        try:
            submission = frappe.get_doc("Submission", submission_id)
            submission.status = "Failed"
            submission.upload_error_log = frappe.get_traceback()[:5000]
            submission.save(ignore_permissions=True)
            frappe.db.commit()
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "process_submission_async:submission_marked_failed",
                submission_id=submission_id,
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END
        except Exception as log_error:
            frappe.logger("submission").error(
                f"Failed to update submission {submission_id} after background error: {str(log_error)}"
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "process_submission_async:failed_to_mark_failed",
                submission_id=submission_id,
                error=str(log_error),
                traceback=frappe.get_traceback(),
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END


def enqueue_submission(submission_id, pe_context=None):
    try:
        import pika
        from tap_lms.imgana.submission import get_rabbitmq_settings

        pe_context = pe_context or {}
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("enqueue_submission:start", submission_id=submission_id, pe_context=pe_context)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        submission = frappe.get_doc("Submission", submission_id)
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        missing_submission_fields = [
            field
            for field in (
                "program_enrollment",
                "week",
                "is_primary",
                "escalation_step_at_submit",
                "created_at",
            )
            if not hasattr(submission, field)
        ]
        if missing_submission_fields:
            _debug_log(
                "enqueue_submission:missing_submission_fields_using_context_fallbacks",
                submission_id=submission_id,
                missing_fields=missing_submission_fields,
            )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "enqueue_submission:submission_loaded",
            submission_id=submission.name,
            assign_id=submission.assign_id,
            student_id=submission.student_id,
            submission_type=submission.submission_type,
            status=submission.status,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

        payload = {
            "submission_id": submission.name,
            "assign_id": submission.assign_id,
            "student_id": submission.student_id,
            "submission_type": submission.submission_type,
            "submission_text": submission.submission_text,
            "submission_url": submission.submission_url,
            "program_enrollment": getattr(
                submission,
                "program_enrollment",
                pe_context.get("program_enrollment", ""),
            ),
            "week": getattr(
                submission,
                "week",
                pe_context.get("current_week", 1),
            ),
            "is_primary": getattr(submission, "is_primary", 1),
            "escalation_step_at_submit": getattr(
                submission,
                "escalation_step_at_submit",
                pe_context.get("last_escalation_step", 0),
            ),
            "archetype": pe_context.get("archetype", ""),
            "experiment_arm": pe_context.get("experiment_arm", ""),
            "expected_submission_type": pe_context.get("expected_submission_type", ""),
            "language": pe_context.get("language", ""),
            "batch": pe_context.get("batch", ""),
            "current_week": pe_context.get("current_week", 1),
            "current_path": pe_context.get("current_path", ""),
            "current_tier": pe_context.get("current_tier", ""),
            "course_level": pe_context.get("course_level", ""),
            "created_at": str(getattr(submission, "created_at", submission.creation)),
        }
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "enqueue_submission:payload_built",
            submission_id=submission_id,
            payload_keys=list(payload.keys()),
            routing_submission_type=payload.get("submission_type"),
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

        rabbitmq_config = get_rabbitmq_settings()
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "enqueue_submission:rabbitmq_config_loaded",
            host=rabbitmq_config.get("host"),
            port=rabbitmq_config.get("port"),
            virtual_host=rabbitmq_config.get("virtual_host"),
            queue=rabbitmq_config.get("queue"),
            has_username=bool(rabbitmq_config.get("username")),
            has_password=bool(rabbitmq_config.get("password")),
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        credentials = pika.PlainCredentials(
            rabbitmq_config["username"],
            rabbitmq_config["password"],
        )
        parameters = pika.ConnectionParameters(
            rabbitmq_config["host"],
            int(rabbitmq_config["port"]),
            rabbitmq_config["virtual_host"],
            credentials,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("enqueue_submission:rabbitmq_connect_start", submission_id=submission_id)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("enqueue_submission:rabbitmq_connected", submission_id=submission_id)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

        try:
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "enqueue_submission:queue_declare_passive_start",
                queue=rabbitmq_config["queue"],
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END
            channel.queue_declare(
                queue=rabbitmq_config["queue"],
                durable=True,
                passive=True,
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "enqueue_submission:queue_declare_passive_success",
                queue=rabbitmq_config["queue"],
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END
        except Exception as e:
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "enqueue_submission:queue_declare_passive_failed_retry_active",
                queue=rabbitmq_config["queue"],
                error=str(e),
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END
            channel.queue_declare(queue=rabbitmq_config["queue"], durable=True)
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log(
                "enqueue_submission:queue_declare_active_success",
                queue=rabbitmq_config["queue"],
            )
            # TEMP_SUBMISSION_QUEUE_DEBUG_END

        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "enqueue_submission:basic_publish_start",
            queue=rabbitmq_config["queue"],
            submission_id=submission_id,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        channel.basic_publish(
            exchange="",
            routing_key=rabbitmq_config["queue"],
            body=json.dumps(payload),
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "enqueue_submission:basic_publish_complete",
            queue=rabbitmq_config["queue"],
            submission_id=submission_id,
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        connection.close()
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("enqueue_submission:rabbitmq_connection_closed", submission_id=submission_id)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END

        frappe.logger("submission").info(
            f"Enqueued submission {submission_id} with type {submission.submission_type}"
        )
    except Exception as e:
        frappe.logger("submission").error(
            f"Failed to enqueue submission {submission_id}: {str(e)}"
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log(
            "enqueue_submission:error",
            submission_id=submission_id,
            error=str(e),
            traceback=frappe.get_traceback(),
        )
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        raise frappe.ValidationError(f"Failed to enqueue submission: {str(e)}")


# =============================================================================
# ENGAGEMENT STATE
# =============================================================================


def _update_engagement(student_id):
    try:
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("_update_engagement:start", student_id=student_id)
        # TEMP_SUBMISSION_QUEUE_DEBUG_END
        es = frappe.db.get_value(
            "EngagementState",
            {"student": student_id},
            ["name", "last_activity_date", "current_streak"],
            as_dict=True,
        )
        today_date = getdate(today())

        if es:
            updates = {"last_activity_date": today_date, "last_updated": now_datetime()}
            last = es.last_activity_date
            if last:
                if isinstance(last, str):
                    last = getdate(last)
                days_diff = (today_date - last).days
                if days_diff == 1:
                    updates["current_streak"] = (es.current_streak or 0) + 1
                elif days_diff > 1:
                    updates["current_streak"] = 1
            else:
                updates["current_streak"] = 1
            frappe.db.set_value("EngagementState", es.name, updates)
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log("_update_engagement:updated_existing", student_id=student_id, engagement_state=es.name)
            # TEMP_SUBMISSION_QUEUE_DEBUG_END
        else:
            new_es = frappe.new_doc("EngagementState")
            new_es.student = student_id
            new_es.last_activity_date = today_date
            new_es.current_streak = 1
            new_es.last_updated = now_datetime()
            new_es.insert(ignore_permissions=True)
            # TEMP_SUBMISSION_QUEUE_DEBUG_START
            _debug_log("_update_engagement:created_new", student_id=student_id, engagement_state=new_es.name)
            # TEMP_SUBMISSION_QUEUE_DEBUG_END
    except Exception as e:
        frappe.log_error(f"EngagementState error: {str(e)}", "SP Engagement")
        # TEMP_SUBMISSION_QUEUE_DEBUG_START
        _debug_log("_update_engagement:error", student_id=student_id, error=str(e), traceback=frappe.get_traceback())
        # TEMP_SUBMISSION_QUEUE_DEBUG_END


# =============================================================================
# HELPERS
# =============================================================================


def _resolve_student(identifier):
    from tap_lms.summer_program.utils import resolve_student

    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("_resolve_student:start", identifier=identifier)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    student = resolve_student(identifier)
    # TEMP_SUBMISSION_QUEUE_DEBUG_START
    _debug_log("_resolve_student:complete", identifier=identifier, student=student)
    # TEMP_SUBMISSION_QUEUE_DEBUG_END
    return student
