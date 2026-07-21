import frappe
import json
import pika
import requests
from urllib.parse import urlparse
from google.cloud import storage
import os


def get_rabbitmq_settings():
    """
    Fetch RabbitMQ configuration from the RabbitMQ Settings DocType.
    Returns a dict with connection parameters.
    """
    settings = frappe.get_single("RabbitMQ Settings")
    return {
        'host': settings.host,
        'port': int(settings.port),
        'virtual_host': settings.virtual_host,
        'username': settings.username,
        'password': settings.get_password('password'),
        'queue': settings.submission_queue
    }


def get_gcs_client():
    """
    Get GCS client using credentials from GCS Settings DocType.
    Returns tuple of (client, bucket_name) or None if disabled.
    """
    settings = frappe.get_single("GCS Settings")

    if not settings.enabled:
        return None

    # Parse credentials JSON
    credentials_dict = json.loads(settings.credentials_json)

    # Create client from credentials
    client = storage.Client.from_service_account_info(credentials_dict)

    return client, settings.bucket_name


def get_content_type_from_response(response, filename):
    """
    Determine the correct content type from response headers or filename.
    Returns tuple of (content_type, file_extension)
    """
    # First try to get from response headers
    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()

    # Map of content types to extensions
    content_type_map = {
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'image/bmp': '.bmp',
        'image/svg+xml': '.svg'
    }

    # Reverse map for extension to content type
    ext_to_content_type = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
        '.svg': 'image/svg+xml'
    }

    # If we have a valid content type from headers
    if content_type in content_type_map:
        return content_type, content_type_map[content_type]

    # Try to get from filename extension
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in ext_to_content_type:
            return ext_to_content_type[ext], ext

    # Default to jpeg
    return 'image/jpeg', '.jpg'


def upload_image_to_gcs(img_url, submission_name):
    """
    Download image from external URL and upload to GCS.
    Returns the public URL.
    """
    # Get GCS client
    result = get_gcs_client()

    if result is None:
        raise Exception("GCS Storage is not enabled. Enable it in GCS Settings.")

    client, bucket_name = result

    # Download the image
    response = requests.get(img_url, timeout=30)
    response.raise_for_status()

    # Get filename from URL
    parsed_url = urlparse(img_url)
    original_filename = os.path.basename(parsed_url.path)

    # Determine content type and extension
    content_type, ext = get_content_type_from_response(response, original_filename)

    # Create filename if empty or no extension
    if not original_filename or '.' not in original_filename:
        original_filename = f"image{ext}"

    # Create unique filename with folder structure
    gcs_filename = f"submissions/{submission_name}_{original_filename}"

    # Upload to GCS
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(gcs_filename)

    # Upload with explicit content_type
    blob.upload_from_string(
        response.content,
        content_type=content_type
    )

    # Generate public URL
    public_url = f"https://storage.googleapis.com/{bucket_name}/{gcs_filename}"

    frappe.logger("submission").info(
        f"Image uploaded to GCS: {img_url} -> {public_url} (content_type: {content_type})"
    )

    return public_url


@frappe.whitelist(allow_guest=True)
def submit_artwork(api_key, assign_id, student_id, img_url):
    """
    API endpoint to submit artwork.

    Creates the ImgSubmission record immediately, returns the submission_id,
    and offloads the heavy work (download + GCS upload + RabbitMQ publish) to
    a background job so the HTTP request returns fast.
    """
    # Authenticate the API request using the provided api_key
    api_key_doc = frappe.db.get_value(
        "API Key", {"key": api_key, "enabled": 1}, ["user"], as_dict=True
    )
    if not api_key_doc:
        frappe.throw("Invalid API key")

    # Switch to the user associated with the API key
    frappe.set_user(api_key_doc.user)

    try:
        # Create a new submission record. The original (external) URL is stored
        # for now; the worker will replace it with the GCS URL after upload.
        submission = frappe.new_doc("ImgSubmission")
        submission.assign_id = assign_id
        submission.student_id = student_id
        submission.img_url = img_url
        submission.status = "Pending"
        submission.insert()

        # Commit so the row is visible to the worker immediately.
        frappe.db.commit()

        # Enqueue the slow path (download -> GCS -> RabbitMQ) as a background job.
        # Using the "long" queue because outbound HTTP + GCS upload can take a while.
        frappe.enqueue(
            "tap_lms.imgana.submission.process_submission_upload",
            queue="long",
            timeout=600,
            submission_name=submission.name,
            source_url=img_url,
            enqueue_after_commit=False,
        )

        frappe.logger("submission").info(
            f"Queued submission {submission.name} for background upload "
            f"(assign_id={assign_id}, student_id={student_id}, src={img_url})"
        )

        # Return immediately with the submission id. The client should poll
        # img_feedback / a status endpoint to learn when processing is done.
        return {
            "message": "Submission received",
            "submission_id": submission.name,
            "status": "Pending"
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.logger("submission").error(f"Error in submit_artwork: {str(e)}")
        frappe.throw(f"Failed to create submission: {str(e)}")

    finally:
        # Switch back to the original user
        frappe.set_user("Administrator")


def process_submission_upload(submission_name, source_url=None):
    """
    Background job: download the artwork from the original URL, upload it to
    GCS, update the ImgSubmission with the public GCS URL, and enqueue the
    downstream RabbitMQ message.

    On failure, the submission is marked Failed and the error is logged so a
    human (or a retry job) can pick it up.
    """
    try:
        submission = frappe.get_doc("ImgSubmission", submission_name)

        # Fall back to the URL on the doc if the caller didn't pass one.
        original_url = source_url or submission.img_url

        # Mark as Processing so clients polling status see progress.
        submission.status = "Processing"
        submission.save(ignore_permissions=True)
        frappe.db.commit()

        # Heavy step 1: download + push to GCS
        public_url = upload_image_to_gcs(original_url, submission.name)

        # Persist the GCS URL on the submission
        submission.reload()
        submission.img_url = public_url
        submission.save(ignore_permissions=True)
        frappe.db.commit()

        # Heavy step 2: hand off to RabbitMQ for downstream RAG processing
        enqueue_submission(submission.name)

        frappe.logger("submission").info(
            f"Background upload finished for {submission_name}: {public_url}"
        )

    except Exception as e:
        frappe.db.rollback()
        # Best-effort: mark Failed so callers see the terminal state.
        try:
            submission = frappe.get_doc("ImgSubmission", submission_name)
            submission.status = "Failed"
            # Stash the error text in generated_feedback (no dedicated error
            # field on the doctype) so it shows up in the UI.
            submission.generated_feedback = (
                f"Upload/queue failed: {str(e)}"
            )[:140000]
            submission.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception as inner:
            frappe.logger("submission").error(
                f"Could not mark {submission_name} as Failed: {inner}"
            )

        frappe.log_error(
            message=frappe.get_traceback(),
            title=f"process_submission_upload failed: {submission_name}",
        )
        # Re-raise so the RQ worker records the failure and respects retries.
        raise


def enqueue_submission(submission_id):
    """
    Send submission details to RabbitMQ queue.
    The img_url now contains the GCS public URL.
    """
    submission = frappe.get_doc("ImgSubmission", submission_id)

    # Payload with GCS public URL
    payload = {
        "submission_id": submission.name,
        "assign_id": submission.assign_id,
        "student_id": submission.student_id,
        "img_url": submission.img_url  # This is now the GCS public URL
    }

    # Get RabbitMQ settings from DocType
    rabbitmq_config = get_rabbitmq_settings()

    # Establish a connection to RabbitMQ
    credentials = pika.PlainCredentials(
        rabbitmq_config['username'],
        rabbitmq_config['password']
    )
    parameters = pika.ConnectionParameters(
        rabbitmq_config['host'],
        rabbitmq_config['port'],
        rabbitmq_config['virtual_host'],
        credentials
    )
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    # Declare the queue
    channel.queue_declare(queue=rabbitmq_config['queue'])

    # Publish the message to the queue
    channel.basic_publish(
        exchange='',
        routing_key=rabbitmq_config['queue'],
        body=json.dumps(payload)
    )

    # Close the connection
    connection.close()

    frappe.logger("submission").info(
        f"Enqueued submission {submission_id} with GCS URL: {submission.img_url}"
    )


@frappe.whitelist(allow_guest=True)
def img_feedback(api_key, submission_id):
    """
    API endpoint to get feedback for a submission.
    """
    # Authenticate the API request using the provided api_key
    api_key_doc = frappe.db.get_value(
        "API Key", {"key": api_key, "enabled": 1}, ["user"], as_dict=True
    )
    if not api_key_doc:
        frappe.throw("Invalid API key")

    # Switch to the user associated with the API key
    frappe.set_user(api_key_doc.user)

    try:
        # Get the submission document
        submission = frappe.get_doc("ImgSubmission", submission_id)

        # Prepare the response based on status
        if submission.status == "Completed":
            response = {
                "status": submission.status,
                "overall_feedback": submission.overall_feedback
            }
        elif submission.status == "Failed":
            response = {
                "status": submission.status,
                "error": submission.generated_feedback or "Submission failed"
            }
        else:
            response = {
                "status": submission.status
            }

        return response

    except frappe.DoesNotExistError:
        return {"error": "Submission not found"}

    except Exception as e:
        frappe.log_error(
            f"Error checking submission status: {str(e)}",
            "Submission Status Error"
        )
        return {"error": "An error occurred while checking submission status"}

    finally:
        # Switch back to the original user
        frappe.set_user("Administrator")


@frappe.whitelist()
def get_assignment_context(assignment_id, student_id=None):
    """Get complete assignment context for RAG service"""
    try:
        assignment = frappe.get_doc("Assignment", assignment_id)

        context = {
            "assignment": {
                "name": assignment.assignment_name,
                "description": assignment.description,
                "type": assignment.assignment_type,
                "subject": assignment.subject,
                "submission_guidelines": assignment.submission_guidelines,
                "reference_image": assignment.reference_image,
                "max_score": assignment.max_score
            },
            "learning_objectives": [
                {
                    "objective": obj.learning_objective,
                    "description": frappe.db.get_value(
                        "Learning Objective",
                        obj.learning_objective,
                        "description"
                    )
                }
                for obj in assignment.learning_objectives
            ]
        }

        # Add student context if provided
        if student_id:
            student = frappe.get_doc("Student", student_id)
            context["student"] = {
                "grade": student.grade,
                "level": student.level,
                "language": student.language
            }

        # Add custom feedback prompt if enabled
        if assignment.enable_auto_feedback and assignment.feedback_prompt:
            context["feedback_prompt"] = assignment.feedback_prompt

        return context

    except Exception as e:
        frappe.log_error(
            f"Error getting assignment context: {str(e)}",
            "RAG Context Error"
        )
        return None
