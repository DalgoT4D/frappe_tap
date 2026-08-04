import json
from datetime import datetime
from urllib.parse import urlparse

import frappe


GLIFIC_FEEDBACK_FLOW_ID = "34108"
FEEDBACK_PIPELINE_MAX_RETRIES = 5
FEEDBACK_PIPELINE_RETRY_LOG_TITLE = "Demo Feedback Pipeline Retry"
FEEDBACK_PIPELINE_DLQ_LOG_TITLE = "Demo Feedback Pipeline DLQ - manual replay required"


def _normalize_unicode_surrogates(value):
    if not isinstance(value, str):
        return value

    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value

    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _normalize_required_text(value, label):
    if value is None:
        frappe.throw(f"{label} is required")

    value = str(value).strip()
    if not value:
        frappe.throw(f"{label} is required")
    return value


def _normalize_submission_payload(submission):
    return {
        "raw_submission": _normalize_required_text(submission, "Submission"),
        "submission_type": None,
        "submission_text": None,
        "submission_url": None,
    }


def _looks_like_url(submission):
    parsed = urlparse(submission.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _contains_only_emoji(submission):
    text = submission.strip()
    if not text:
        return False

    return not any(char.isalnum() for char in text)


def _set_if_field(doc, fieldname, value):
    if doc.meta.has_field(fieldname):
        setattr(doc, fieldname, value)


def _create_submission(assignment_id, glific_id, payload):
    submission_doc = frappe.new_doc("Submission")
    submission_doc.assign_id = assignment_id
    submission_doc.student_id = glific_id
    submission_doc.submission_type = payload["submission_type"]
    submission_doc.submission_text = payload["submission_text"]
    submission_doc.submission_url = payload["submission_url"]
    submission_doc.status = "Pending"

    now = datetime.now()
    _set_if_field(submission_doc, "send_feedback", "yes")
    _set_if_field(submission_doc, "feedback_requested_at", now)
    _set_if_field(submission_doc, "created_at", now)
    _set_if_field(submission_doc, "is_primary", 1)

    submission_doc.insert(ignore_permissions=True)
    submission_doc._raw_submission = payload["raw_submission"]
    return submission_doc


def _queue_submission_processing(submission_doc):
    frappe.enqueue(
        "tap_lms.imgana.demo_submission.process_submission_async",
        queue="long",
        timeout=600,
        enqueue_after_commit=True,
        submission_id=submission_doc.name,
        raw_submission=getattr(submission_doc, "_raw_submission", None),
    )


def _build_submission_response(submission_doc):
    return {
        "success": True,
        "status": "accepted",
        "message": "Submission received",
        "submission_id": submission_doc.name,
        "assignment_id": submission_doc.assign_id,
        "glific_id": submission_doc.student_id,
    }


@frappe.whitelist(allow_guest=True)
def submit_artwork(assignment_id, glific_id, submission):
    """
    Create a demo Submission using the Glific contact ID and publish it to the
    RabbitMQ feedback pipeline. This endpoint intentionally does not resolve a
    Student record or inspect enrollment state.
    """
    try:
        assignment_id = _normalize_unicode_surrogates(
            _normalize_required_text(assignment_id, "Assignment ID")
        )
        glific_id = _normalize_required_text(glific_id, "Glific ID")

        if not frappe.db.exists("Assignment", assignment_id):
            frappe.local.response.update(
                {
                    "success": False,
                    "status": "not_found",
                    "error_detail": f"Assignment {assignment_id} not found",
                }
            )
            return

        payload = _normalize_submission_payload(submission)
        submission_doc = _create_submission(assignment_id, glific_id, payload)
        _queue_submission_processing(submission_doc)

        return _build_submission_response(submission_doc)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        frappe.local.response.update(
            {
                "success": False,
                "status": "validation_error",
                "error_detail": str(exc),
            }
        )
        return
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(
            f"submit_artwork failed for assignment_id={assignment_id}, "
            f"glific_id={glific_id}: {type(exc).__name__}: {exc}",
            "Demo Submit Artwork",
        )
        frappe.local.response.update(
            {
                "success": False,
                "status": "internal_error",
                "error_detail": f"{type(exc).__name__}: {exc}",
            }
        )
        return


def process_submission_async(submission_id, raw_submission=None, submission_url=None):
    """
    Normalize the raw demo submission, upload URL media to GCS, then publish the
    normalized Submission payload to RabbitMQ for feedback generation.
    """
    try:
        submission_doc = frappe.get_doc("Submission", submission_id)
        raw_submission = (raw_submission or submission_url or "").strip()

        if raw_submission and _looks_like_url(raw_submission):
            from tap_lms.imgana.gcs_client import upload_to_gcs
            from tap_lms.imgana.media_detection import detect_url_media_type

            media_type = detect_url_media_type(raw_submission, default="image")
            uploaded_url = upload_to_gcs(
                raw_submission,
                submission_doc.name,
                media_type=media_type,
            )
            submission_doc.submission_type = media_type
            submission_doc.submission_url = uploaded_url
            submission_doc.submission_text = None
        elif raw_submission:
            submission_doc.submission_type = (
                "emoji" if _contains_only_emoji(raw_submission) else "text"
            )
            submission_doc.submission_text = raw_submission
            submission_doc.submission_url = None
        elif not (submission_doc.submission_text or submission_doc.submission_url):
            frappe.throw("Submission content is missing")

        submission_doc.status = "Processing"
        _set_if_field(submission_doc, "upload_error_log", None)
        submission_doc.save(ignore_permissions=True)
        frappe.db.commit()

        enqueue_submission(submission_doc.name)
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(
            f"Error in demo background processing for submission "
            f"{submission_id}: {str(exc)}",
            "Demo process_submission_async",
        )

        try:
            submission_doc = frappe.get_doc("Submission", submission_id)
            submission_doc.status = "Failed"
            _set_if_field(submission_doc, "upload_error_log", frappe.get_traceback()[:5000])
            submission_doc.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception as log_error:
            frappe.log_error(
                f"Failed to update demo submission {submission_id} after "
                f"background error: {str(log_error)}",
                "Demo process_submission_async",
            )

        raise


def enqueue_submission(submission_id, retry_count=0):
    try:
        import pika
        from tap_lms.imgana.submission import get_rabbitmq_settings

        submission_doc = frappe.get_doc("Submission", submission_id)
        payload = {
            "submission_id": submission_doc.name,
            "assign_id": submission_doc.assign_id,
            "student_id": submission_doc.student_id,
            "submission_type": submission_doc.submission_type,
            "submission_text": submission_doc.submission_text,
            "submission_url": submission_doc.submission_url,
            "is_primary": getattr(submission_doc, "is_primary", 1),
            "created_at": str(getattr(submission_doc, "created_at", submission_doc.creation)),
            "glific_contact_id": submission_doc.student_id,
            "glific_feedback_flow_id": GLIFIC_FEEDBACK_FLOW_ID,
            "source": "demo_submission",
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

        connection = None
        publish_succeeded = False
        try:
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            channel.confirm_delivery()
            channel.queue_declare(queue=rabbitmq_config["queue"], durable=True)
            channel.basic_publish(
                exchange="",
                routing_key=rabbitmq_config["queue"],
                body=json.dumps(payload, default=str),
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                ),
                mandatory=True,
            )
            publish_succeeded = True
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as close_err:
                    frappe.logger("submission").warning(
                        f"RabbitMQ close failed for demo submission {submission_id} "
                        f"(publish_succeeded={publish_succeeded}): {close_err}"
                    )

        if publish_succeeded:
            frappe.logger("submission").info(
                f"Enqueued demo submission {submission_id} with type "
                f"{submission_doc.submission_type}"
            )
    except Exception as exc:
        frappe.logger("submission").error(
            f"Failed to enqueue demo submission {submission_id}: {str(exc)}"
        )
        retry_count = (retry_count or 0) + 1
        glific_id = ""

        try:
            glific_id = frappe.db.get_value("Submission", submission_id, "student_id") or ""
        except Exception:
            glific_id = ""

        if retry_count <= FEEDBACK_PIPELINE_MAX_RETRIES:
            frappe.log_error(
                title=FEEDBACK_PIPELINE_RETRY_LOG_TITLE,
                message=(
                    f"Demo feedback pipeline transient failure "
                    f"(attempt {retry_count}/{FEEDBACK_PIPELINE_MAX_RETRIES + 1}) "
                    f"for submission {submission_id} "
                    f"(glific_id={glific_id or 'unknown'}): {exc}"
                ),
            )
            try:
                frappe.enqueue(
                    "tap_lms.imgana.demo_submission.enqueue_submission",
                    queue="default",
                    timeout=120,
                    submission_id=submission_id,
                    retry_count=retry_count,
                )
            except Exception as enqueue_err:
                frappe.log_error(
                    title=FEEDBACK_PIPELINE_DLQ_LOG_TITLE,
                    message=json.dumps(
                        {
                            "reason": "double_fault_enqueue_failed",
                            "submission_id": submission_id,
                            "glific_id": glific_id,
                            "final_error": str(exc),
                            "enqueue_error": str(enqueue_err),
                            "retries_attempted": retry_count,
                        },
                        indent=2,
                        default=str,
                    ),
                )
        else:
            frappe.log_error(
                title=FEEDBACK_PIPELINE_DLQ_LOG_TITLE,
                message=json.dumps(
                    {
                        "submission_id": submission_id,
                        "glific_id": glific_id,
                        "final_error": str(exc),
                        "retries_attempted": retry_count,
                    },
                    indent=2,
                    default=str,
                ),
            )
