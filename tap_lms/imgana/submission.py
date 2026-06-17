import base64
import json
import os
from urllib.parse import urlparse

import frappe
import pika
import requests
from frappe.utils.file_manager import get_file_path
from google.cloud import storage
import mimetypes
from tap_lms.imgana.gcs_client import upload_to_gcs

URL_SUBMISSION_TYPES = {"audio", "image", "video"}


def _normalize_unicode_surrogates(value):
    """Convert escaped UTF-16 surrogate pairs into valid Unicode."""
    if not isinstance(value, str):
        return value

    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value

    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def get_rabbitmq_settings():
    """
    Fetch RabbitMQ configuration from the RabbitMQ Settings DocType.
    Returns a dict with connection parameters.
    """
    settings = frappe.get_single("RabbitMQ Settings")
    return {
        "host": settings.host,
        "port": int(settings.port),
        "virtual_host": settings.virtual_host,
        "username": settings.username,
        "password": settings.get_password("password"),
        "queue": settings.submission_queue,
    }

def process_submission_async(submission_id, raw_submission=None, submission_url=None):
    """
    Background job that classifies the raw submission, uploads URL media to GCS,
    and enqueues it for processing.
    """
    try:
        submission = frappe.get_doc("Submission", submission_id)

        raw_submission = (raw_submission or submission_url or "").strip()
        url = None

        if raw_submission and _looks_like_url(raw_submission):
            from tap_lms.imgana.media_detection import detect_url_media_type

            media_type = detect_url_media_type(raw_submission, default="image")
            url = upload_to_gcs(raw_submission, submission.name, media_type=media_type)

            submission.submission_type = media_type
            submission.submission_text = None
            submission.submission_url = url
        elif raw_submission:
            submission.submission_type = (
                "emoji" if _contains_only_emoji(raw_submission) else "text"
            )
            submission.submission_text = raw_submission
            submission.submission_url = None
        submission.status = "Processing"
        submission.upload_error_log = None
        submission.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger("submission").debug(
            f"Submission prepared for processing: assign_id={submission.assign_id}, "
            f"student_id={submission.student_id}, "
            f"submission_type={submission.submission_type}, "
            f"raw_submission={raw_submission}, "
            f"gcs_url={url}"
        )

        enqueue_submission(submission.name)

    except Exception as e:
        frappe.db.rollback()
        error_message = str(e)
        frappe.logger("submission").error(
            f"Error in background processing for submission {submission_id}: {error_message}"
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


def _authenticate_api_key(api_key):
    api_key_doc = frappe.db.get_value(
        "API Key",
        {"key": api_key, "enabled": 1},
        ["user"],
        as_dict=True,
    )
    if not api_key_doc:
        frappe.throw("Invalid API key")
    return api_key_doc.user


def _looks_like_url(submission):
    parsed = urlparse(submission.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _contains_only_emoji(submission):
    text = submission.strip()
    if not text:
        return False

    return not any(char.isalnum() for char in text)


def _normalize_submission_payload(submission):
    if not isinstance(submission, str) or not submission.strip():
        frappe.throw("Submission is required")

    return {
        "raw_submission": submission.strip(),
        "submission_type": None,
        "submission_text": None,
        "submission_url": None,
    }


def _create_submission(assign_id, student_id, payload):
    submission = frappe.new_doc("Submission")
    submission.assign_id = assign_id
    submission.student_id = student_id
    submission.submission_type = payload["submission_type"]
    submission.submission_text = payload["submission_text"]
    submission.submission_url = payload["submission_url"]
    submission.status = "Pending"
    submission.insert()
    frappe.db.commit()
    submission._raw_submission = payload.get("raw_submission")
    return submission


def _queue_submission_processing(submission, payload):
    frappe.enqueue(
        process_submission_async,
        queue="long",
        timeout=600,
        submission_id=submission.name,
        raw_submission=payload["raw_submission"],
    )


def _build_submission_response(submission):
    return {
        "message": "Submission received",
        "submission_id": submission.name,
        "student_id": submission.student_id,
        "submission_type": submission.submission_type,
    }


@frappe.whitelist(allow_guest=True)
def assignment_submission_internal(
    api_key,
    assign_id,
    name1,
    glific_id,
    submission,
):
    """
    Create an assignment submission for the internal fixed student.
    """
    user = _authenticate_api_key(api_key)
    frappe.set_user(user)

    payload = _normalize_submission_payload(submission)
    student_id = "ST00000206"

    try:
        submission = _create_submission(assign_id, student_id, payload)
        _queue_submission_processing(submission, payload)
        return _build_submission_response(submission)
    except Exception as e:
        frappe.db.rollback()
        frappe.logger("submission").error(f"Error in assignment_submission_internal: {str(e)}")
        frappe.throw(f"Failed to process submission: {str(e)}")
    finally:
        frappe.set_user("Administrator")


@frappe.whitelist(allow_guest=True)
def assignment_submission(
    api_key,
    assign_id,
    name1,
    glific_id,
    submission,
):
    """
    Create an assignment submission and enqueue it for feedback processing.
    """
    user = _authenticate_api_key(api_key)
    frappe.set_user(user)

    student = frappe.get_doc(
        "Student",
        {
            "name1": name1,
            "glific_id": glific_id,
        },
        limit=1,
    )
    if not student:
        frappe.throw("Student not found with provided name and glific_id")

    payload = _normalize_submission_payload(submission)

    try:
        submission = _create_submission(assign_id, student.name, payload)
        _queue_submission_processing(submission, payload)
        return _build_submission_response(submission)
    except Exception as e:
        frappe.db.rollback()
        frappe.logger("submission").error(f"Error in assignment_submission: {str(e)}")
        frappe.throw(f"Failed to process submission: {str(e)}")
    finally:
        frappe.set_user("Administrator")


def enqueue_submission(submission_id):
    """
    Send submission details to RabbitMQ queue.
    """
    try:
        submission = frappe.get_doc("Submission", submission_id)

        payload = {
            "submission_id": submission.name,
            "assign_id": submission.assign_id,
            "student_id": submission.student_id,
            "submission_type": submission.submission_type,
            "submission_text": submission.submission_text,
            "submission_url": submission.submission_url,
            "created_at": str(submission.created_at),
        }

        # Get RabbitMQ settings from DocType
        rabbitmq_config = get_rabbitmq_settings()

        # Establish a connection to RabbitMQ
        credentials = pika.PlainCredentials(
            rabbitmq_config["username"], rabbitmq_config["password"]
        )
        parameters = pika.ConnectionParameters(
            rabbitmq_config["host"],
            rabbitmq_config["port"],
            rabbitmq_config["virtual_host"],
            credentials,
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        # Declare the queue
        try:
            # First try passive declaration to check if queue exists
            channel.queue_declare(
                queue=rabbitmq_config["queue"], durable=True, passive=True
            )
        except Exception:
            # If it doesn't exist, declare it
            channel.queue_declare(queue=rabbitmq_config["queue"], durable=True)

        # Publish the message to the queue
        channel.basic_publish(
            exchange="", routing_key=rabbitmq_config["queue"], body=json.dumps(payload)
        )
        print("Submission payload:")
        print(json.dumps(payload))
        frappe.logger("submission").error(f"Enqueued submission {submission_id} with payload: {json.dumps(payload)}")

        # Close the connection
        connection.close()

        # SRE: pipeline trace — step 1. This structured log is the anchor for
        # the submission_id trace in Cloud Logging and BigQuery.
        from tap_lms.monitoring import record_submission_published

        try:
            record_submission_published(
                submission_id=submission_id,
                student_id=payload.get("student_id", ""),
                assign_id=payload.get("assign_id", ""),
                submission_type=payload.get("submission_type", ""),
                queue_name=rabbitmq_config["queue"],
            )
        except Exception as e:
            print(f"[monitoring] record_submission_published failed: {e}", flush=True)
            # Just print and do nothing else - monitoring issues should never fail the process

        frappe.logger("submission").info(
            f"Enqueued submission {submission_id} with type {submission.submission_type}"
        )
    except Exception as e:
        frappe.logger("submission").error(
            f"Failed to enqueue submission {submission_id}: {str(e)}"
        )
        raise frappe.ValidationError(f"Failed to enqueue submission: {str(e)}")


@frappe.whitelist(allow_guest=True)
def assignment_feedback(api_key, submission_id):
    """
    API endpoint to get feedback for a submission.
    """
    user = _authenticate_api_key(api_key)
    frappe.set_user(user)

    try:
        submission = frappe.get_doc("Submission", submission_id)
        
        if submission.status == "Completed":
            response = {
                "status": submission.status,
                "submission_type": submission.submission_type,
                "overall_feedback": submission.overall_feedback,
                "overall_feedback_translated": submission.overall_feedback_translated,
                "audio_feedback_url": submission.audio_feedback_url,
            }
        else:
            response = {
                "status": submission.status,
                "submission_type": submission.submission_type,
            }
        
        return response

    except frappe.DoesNotExistError:
        return {"error": "Submission not found"}

    except Exception as e:
        frappe.log_error(
            f"Error checking submission status: {str(e)}", "Submission Status Error"
        )
        return {"error": "An error occurred while checking submission status"}

    finally:
        frappe.set_user("Administrator")


@frappe.whitelist()
def get_assignment_context(assignment_id, student_id=None):
    """Get complete assignment context for RAG service"""
    try:
        assignment_id = _normalize_unicode_surrogates(assignment_id)
        assignment = frappe.get_doc("Assignment", assignment_id)
        images = []
        for row in assignment.get("reference_images") or []:
            file_url = row.get("image")
            if not file_url:
                continue

            try:
                file_doc = frappe.get_doc("File", {"file_url": file_url})
                file_path = file_doc.get_full_path()
                with open(file_path, "rb") as image_file:
                    content = base64.b64encode(image_file.read()).decode("utf-8")

                content_type = mimetypes.guess_type(file_doc.file_name or file_url)[0] or "image/jpeg"
                images.append({
                    "name": row.get("image_name") or file_doc.file_name,
                    "content_type": content_type,
                    "content": content,
                })
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Assignment Context Image Error - {assignment_id}",
                )

        rubrics = {}
        for grade in assignment.get("rubric_grades") or []:
            rubric_key = grade.get("rubric_name") or grade.get("skill_name") or "General"
            rubrics.setdefault(rubric_key, []).append({
                "grade_value": grade.get("grade_value"),
                "grade_name": grade.get("grade_name"),
                "grade_description": grade.get("grade_description"),
                "skill_name": grade.get("skill_name"),
            })

        learning_objectives = []
        for objective_row in assignment.get("learning_objectives") or []:
            objective_name = objective_row.get("learning_objective")
            if not objective_name:
                continue

            learning_objectives.append({
                "objective": objective_name,
                "description": frappe.db.get_value(
                    "Learning Objective",
                    objective_name,
                    "description",
                ),
            })

        submission_rules = []
        for rule in assignment.get("submission_rules") or []:
            submission_rules.append({
                "submission_title": rule.get("submission_title"),
                "allowed_submission_types": [
                    item.strip()
                    for item in (rule.get("allowed_submission_types") or "").split(",")
                    if item.strip()
                ],
                "guided_text": rule.get("guided_text"),
                "unguided_text": rule.get("unguided_text"),
                "valid_criteria": rule.get("valid_criteria"),
                "invalid_criteria": rule.get("invalid_criteria"),
            })

        context = {
            "assignment": {
                "name": assignment.get("assignment_name"),
                "program_name": assignment.get("program_name"),
                "description": assignment.get("description"),
                "assignment_type": assignment.get("assignment_type"),
                "activity_type": assignment.get("activity_type"),
                "course_vertical": assignment.get("subject"),
                "difficulty_tier": assignment.get("difficulty_tier"),
                "submission_guidelines": assignment.get("submission_guidelines"),
                "submission_rules": submission_rules,
                "reference_images": images,
                "max_score": assignment.get("max_score"),
                "rubrics": rubrics,
            },
            "learning_objectives": learning_objectives,
        }

        # Add custom feedback prompt if enabled
        if assignment.enable_auto_feedback and assignment.feedback_prompt:
            context["feedback_prompt"] = assignment.feedback_prompt

        return context

    except Exception as e:
        frappe.log_error(
            f"Error getting assignment context: {str(e)}", "RAG Context Error"
        )
        return None


@frappe.whitelist()
def get_student_details(student_id):
    """Get student grade level and language details"""
    try:
        student = frappe.get_doc("Student", student_id)

        print(student)

        if not student:
            frappe.log_error(f"Student {student_id} not found", "Student Details Error")
            return None

        return {
            "student_id": student.name,
            "grade": student.grade,
            "level": student.level,
            "language": student.language,
        }

    except Exception as e:
        frappe.log_error(
            f"Error getting student details: {str(e)}", "Student Details Error"
        )
        return None
