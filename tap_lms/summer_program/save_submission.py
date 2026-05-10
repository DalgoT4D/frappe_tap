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


@frappe.whitelist(allow_guest=False)
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
    student_id = _resolve_student(student_id)
    if not student_id:
        return {"success": False, "error": "Student not found"}

    pe = get_active_pe(student_id)
    if not pe:
        return {"success": False, "error": "No active ProgramEnrollment"}

    current_week = cint(week) or pe.current_week or 1

    if pe.resolved_flow_state in TERMINAL_STATES:
        return {
            "success": False,
            "error": "Student in terminal state",
            "resolved_flow_state": pe.resolved_flow_state,
        }

    payload = _normalize_submission_payload(submission)
    is_primary = _try_claim_primary(pe, current_week)
    points = _calculate_points(pe) if is_primary else 0

    submission_doc = _create_submission(
        pe=pe,
        student_id=student_id,
        week=current_week,
        assignment_id=assignment_id,
        payload=payload,
        is_primary=is_primary,
    )

    if is_primary:
        transition_id, success = apply_submission_transition(
            pe, points=points, trigger_source="flow_callback"
        )
    else:
        from tap_lms.summer_program.state_machine import t22_duplicate_submission

        t22_duplicate_submission(pe, "flow_callback")
        transition_id = "T22"

    _update_engagement(student_id)

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

    if is_primary and submission_doc:
        _queue_submission_processing(
            submission_doc,
            pe_context=_build_pe_context(pe),
        )

    frappe.db.commit()

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
    if not isinstance(submission, str) or not submission.strip():
        frappe.throw("Submission is required")

    submission = submission.strip()

    if _looks_like_url(submission):
        return {
            "submission_type": _infer_url_submission_type(submission),
            "submission_text": None,
            "submission_url": submission,
        }

    submission_type = "emoji" if _contains_only_emoji(submission) else "text"
    return {
        "submission_type": submission_type,
        "submission_text": submission,
        "submission_url": None,
    }


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
    except Exception:
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

    pe.reload()
    return rows_affected > 0


# =============================================================================
# POINTS CALCULATION
# =============================================================================


def _calculate_points(pe):
    from tap_lms.summer_program.student_progression_sp import _get_escalation_steps

    student = frappe.get_doc("Student", pe.student)
    batch = frappe.get_doc("Batch", pe.batch)
    steps = _get_escalation_steps(student, batch)

    if not steps:
        return 0

    sent_count = pe.last_escalation_step or 0

    if sent_count == 0:
        return steps[0].get("points_awarded", 0)

    if sent_count < len(steps):
        return steps[sent_count].get("points_awarded", 0)

    return steps[-1].get("points_awarded", 0)


# =============================================================================
# SUBMISSION RECORD
# =============================================================================


def _create_submission(pe, student_id, week, assignment_id, payload, is_primary):
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
    return {
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


def _queue_submission_processing(submission_doc, pe_context):
    frappe.enqueue(
        "tap_lms.summer_program.save_submission.process_submission_async",
        queue="long"
        if submission_doc.submission_type in URL_SUBMISSION_TYPES
        else "default",
        timeout=600,
        enqueue_after_commit=True,
        submission_id=submission_doc.name,
        submission_url=submission_doc.submission_url,
        pe_context=pe_context,
    )


def process_submission_async(submission_id, submission_url=None, pe_context=None):
    """
    Upload URL submissions to GCS, mark the record Processing, and enqueue
    feedback processing. Text and emoji submissions skip GCS upload.
    """
    pe_context = pe_context or {}
    try:
        submission = frappe.get_doc("Submission", submission_id)

        if submission.submission_type in URL_SUBMISSION_TYPES:
            from tap_lms.imgana.gcs_client import upload_to_gcs

            uploaded_url = upload_to_gcs(submission_url, submission.name)
            submission.submission_url = uploaded_url

        submission.status = "Processing"
        submission.upload_error_log = None
        submission.save(ignore_permissions=True)
        frappe.db.commit()

        enqueue_submission(submission.name, pe_context=pe_context)

    except Exception as e:
        frappe.db.rollback()
        frappe.logger("submission").error(
            f"Error in background processing for submission {submission_id}: {str(e)}"
        )

        try:
            submission = frappe.get_doc("Submission", submission_id)
            submission.status = "Failed"
            submission.upload_error_log = frappe.get_traceback()[:5000]
            submission.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception as log_error:
            frappe.logger("submission").error(
                f"Failed to update submission {submission_id} after background error: {str(log_error)}"
            )


def enqueue_submission(submission_id, pe_context=None):
    try:
        import pika
        from tap_lms.imgana.submission import get_rabbitmq_settings

        pe_context = pe_context or {}
        submission = frappe.get_doc("Submission", submission_id)

        payload = {
            "submission_id": submission.name,
            "assign_id": submission.assign_id,
            "student_id": submission.student_id,
            "submission_type": submission.submission_type,
            "submission_text": submission.submission_text,
            "submission_url": submission.submission_url,
            "program_enrollment": submission.program_enrollment,
            "week": submission.week,
            "is_primary": submission.is_primary,
            "escalation_step_at_submit": submission.escalation_step_at_submit,
            "archetype": pe_context.get("archetype", ""),
            "experiment_arm": pe_context.get("experiment_arm", ""),
            "expected_submission_type": pe_context.get("expected_submission_type", ""),
            "language": pe_context.get("language", ""),
            "batch": pe_context.get("batch", ""),
            "current_week": pe_context.get("current_week", 1),
            "current_path": pe_context.get("current_path", ""),
            "current_tier": pe_context.get("current_tier", ""),
            "course_level": pe_context.get("course_level", ""),
            "created_at": str(submission.created_at),
        }

        rabbitmq_config = get_rabbitmq_settings()
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
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        try:
            channel.queue_declare(
                queue=rabbitmq_config["queue"],
                durable=True,
                passive=True,
            )
        except Exception:
            channel.queue_declare(queue=rabbitmq_config["queue"], durable=True)

        channel.basic_publish(
            exchange="",
            routing_key=rabbitmq_config["queue"],
            body=json.dumps(payload),
        )
        connection.close()

        frappe.logger("submission").info(
            f"Enqueued submission {submission_id} with type {submission.submission_type}"
        )
    except Exception as e:
        frappe.logger("submission").error(
            f"Failed to enqueue submission {submission_id}: {str(e)}"
        )
        raise frappe.ValidationError(f"Failed to enqueue submission: {str(e)}")


# =============================================================================
# ENGAGEMENT STATE
# =============================================================================


def _update_engagement(student_id):
    try:
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
        else:
            new_es = frappe.new_doc("EngagementState")
            new_es.student = student_id
            new_es.last_activity_date = today_date
            new_es.current_streak = 1
            new_es.last_updated = now_datetime()
            new_es.insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(f"EngagementState error: {str(e)}", "SP Engagement")


# =============================================================================
# HELPERS
# =============================================================================


def _resolve_student(identifier):
    from tap_lms.summer_program.utils import resolve_student

    return resolve_student(identifier)
