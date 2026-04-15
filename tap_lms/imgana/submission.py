import frappe
import json
import pika
import requests
from urllib.parse import urlparse
from google.cloud import storage
import os
from frappe.utils.file_manager import get_file_path
import base64


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
    try:
        # Get GCS client
        result = get_gcs_client()
        
        if result is None:
            frappe.throw("GCS Storage is not enabled. Enable it in GCS Settings.")
        
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
        
        # Upload with explicit content_type - THIS IS THE FIX
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
        
    except requests.exceptions.RequestException as e:
        frappe.logger("submission").error(f"Failed to download image from {img_url}: {str(e)}")
        raise frappe.ValidationError(f"Failed to download image: {str(e)}")
    except Exception as e:
        frappe.logger("submission").error(f"Failed to upload to GCS: {str(e)}")
        raise frappe.ValidationError(f"Failed to upload to GCS: {str(e)}")

def upload_audio_to_gcs(local_audio_path: str, submission_id: str, original_filename: str) -> str:
    """
    Upload audio file from local path to GCS.
    Returns the public URL.
    
    Args:
        local_audio_path: Path to the local audio file
        submission_id: Submission ID for naming
        original_filename: Original filename for the audio file
    
    Returns:
        Public URL of the uploaded audio file
    """
    try:
        # Get GCS client
        result = get_gcs_client()
        
        if result is None:
            frappe.throw("GCS Storage is not enabled. Enable it in GCS Settings.")
        
        client, bucket_name = result
        
        # Ensure the local file exists
        if not os.path.exists(local_audio_path):
            raise FileNotFoundError(f"Audio file not found at {local_audio_path}")
        
        # Get file extension and determine content type
        ext = os.path.splitext(original_filename)[1].lower()
        content_type_map = {
            '.mp3': 'audio/mpeg',
            '.wav': 'audio/wav',
            '.ogg': 'audio/ogg',
            '.m4a': 'audio/mp4',
            '.aac': 'audio/aac',
        }
        content_type = content_type_map.get(ext, 'audio/mpeg')
        
        # Create GCS path: feedback/{submission_id}_{original_filename}
        gcs_filename = f"audio_feedback/{submission_id}_{original_filename}"
        
        # Upload to GCS
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(gcs_filename)
        
        # Upload file with explicit content type
        with open(local_audio_path, 'rb') as f:
            blob.upload_from_file(f, content_type=content_type)
        
        # Generate public URL
        public_url = f"https://storage.googleapis.com/{bucket_name}/{gcs_filename}"
        
        frappe.logger("submission").info(
            f"Audio uploaded to GCS: {local_audio_path} -> {public_url} (content_type: {content_type})"
        )
        
        return public_url
        
    except FileNotFoundError as e:
        frappe.logger("submission").error(f"Audio file not found: {str(e)}")
        raise frappe.ValidationError(f"Audio file not found: {str(e)}")
    except Exception as e:
        frappe.logger("submission").error(f"Failed to upload audio to GCS: {str(e)}")
        raise frappe.ValidationError(f"Failed to upload audio to GCS: {str(e)}")

def upload_video_to_gcs(video_url: str, submission_id: str) -> str:
    """
    Download video from external URL and upload to GCS.
    Returns the public URL.
    """
    try:
        # Get GCS client
        result = get_gcs_client()
        if result is None:
            frappe.throw("GCS Storage is not enabled. Enable it in GCS Settings.")
        client, bucket_name = result

        # Download the video from the external URL
        response = requests.get(video_url, timeout=60, stream=True)
        response.raise_for_status()

        # Get filename from URL
        parsed_url = urlparse(video_url)
        original_filename = os.path.basename(parsed_url.path)

        # Determine content type and extension
        content_type_map = {
            'video/mp4': '.mp4',
            'video/quicktime': '.mov',
            'video/x-msvideo': '.avi',
            'video/x-matroska': '.mkv',
            'video/webm': '.webm',
            'video/x-flv': '.flv',
            'video/x-ms-wmv': '.wmv',
        }
        ext_to_content_type = {v: k for k, v in content_type_map.items()}

        # Try to get content type from response headers
        content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
        ext = content_type_map.get(content_type)

        # If not found, try from filename
        if not ext and original_filename:
            ext = os.path.splitext(original_filename)[1].lower()
            content_type = ext_to_content_type.get(ext, 'video/mp4')
        if not ext:
            ext = '.mp4'
            content_type = 'video/mp4'

        # Create filename if empty or no extension
        if not original_filename or '.' not in original_filename:
            original_filename = f"video{ext}"

        # Create unique filename with folder structure
        gcs_filename = f"submissions/{submission_id}_{original_filename}"

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
            f"Video uploaded to GCS: {video_url} -> {public_url} (content_type: {content_type})"
        )

        return public_url

    except requests.exceptions.RequestException as e:
        frappe.logger("submission").error(f"Failed to download video from {video_url}: {str(e)}")
        raise frappe.ValidationError(f"Failed to download video: {str(e)}")
    except Exception as e:
        frappe.logger("submission").error(f"Failed to upload video to GCS: {str(e)}")
        raise frappe.ValidationError(f"Failed to upload video to GCS: {str(e)}")


def upload_to_gcs(submission_url, submission_name):
    """
    Detect media type (image or video) from the URL extension and upload to GCS.
    Returns the public URL.
    """
    # Supported image and video extensions
    image_exts = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg'}
    video_exts = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'flv', 'wmv'}

    url_without_query = submission_url.split("?", 1)[0].lower()
    if "." in url_without_query:
        ext = url_without_query.rsplit(".", 1)[-1]
        if ext in image_exts:
            return upload_image_to_gcs(submission_url, submission_name)
        if ext in video_exts:
            return upload_video_to_gcs(submission_url, submission_name)


    # If no extension is detected, default to image upload
    return upload_image_to_gcs(submission_url, submission_name)


@frappe.whitelist(allow_guest=True)
def submit_artwork_internal(api_key, assign_id, name1, glific_id, img_url):
    """
    API endpoint to submit artwork.
    Downloads image, uploads to GCS, creates submission, and enqueues to RabbitMQ.
    """
    # Authenticate the API request using the provided api_key
    api_key_doc = frappe.db.get_value("API Key", {"key": api_key, "enabled": 1}, ["user"], as_dict=True)
    if not api_key_doc:
        frappe.throw("Invalid API key")

    # Switch to the user associated with the API key
    frappe.set_user(api_key_doc.user)
    
    student_id = "ST00000206"

    try:
        # Create a new submission first (to get the submission name)
        submission = frappe.new_doc("ImgSubmission")
        submission.assign_id = assign_id
        submission.student_id = student_id
        submission.img_url = img_url  # Store original URL initially
        submission.status = "Pending"
        submission.insert()
        
        # Upload to GCS and get public URL
        public_url = upload_to_gcs(img_url, submission.name)
        
        # Update the submission with the GCS URL
        submission.img_url = public_url
        submission.save()
        
        frappe.db.commit()

        # Log for debugging
        frappe.logger("submission").debug(
            f"Inserted submission: assign_id={submission.assign_id}, "
            f"student_id={submission.student_id}, "
            f"original_url={img_url}, "
            f"gcs_url={public_url}"
        )

        # Send the submission details to RabbitMQ with the GCS public URL
        enqueue_submission(submission.name)

        return {
            "message": "Submission received",
            "submission_id": submission.name,
            "student_id": student_id,
            "image_url": public_url
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.logger("submission").error(f"Error in submit_artwork: {str(e)}")
        frappe.throw(f"Failed to process submission: {str(e)}")

    finally:
        # Switch back to the original user
        frappe.set_user("Administrator")


@frappe.whitelist(allow_guest=True)
def submit_artwork(api_key, assign_id, name1, glific_id, img_url):
    """
    API endpoint to submit artwork.
    Downloads image, uploads to GCS, creates submission, and enqueues to RabbitMQ.
    """
    # Authenticate the API request using the provided api_key
    api_key_doc = frappe.db.get_value("API Key", {"key": api_key, "enabled": 1}, ["user"], as_dict=True)
    if not api_key_doc:
        frappe.throw("Invalid API key")

    # Switch to the user associated with the API key
    frappe.set_user(api_key_doc.user)
    
    # Get student document
    student = frappe.get_doc(
                    "Student",
                    {
                        "name1": name1,
                        "glific_id": glific_id
                    },
                    limit=1
                )
    if not student:
        frappe.throw("Student not found with provided name and glific_id")
    student_id = student.name

    try:
        # Create a new submission first (to get the submission name)
        submission = frappe.new_doc("ImgSubmission")
        submission.assign_id = assign_id
        submission.student_id = student_id
        submission.img_url = img_url  # Store original URL initially
        submission.status = "Pending"
        submission.insert()
        
        # Upload to GCS and get public URL
        public_url = upload_to_gcs(img_url, submission.name)
        
        # Update the submission with the GCS URL
        submission.img_url = public_url
        submission.save()
        
        frappe.db.commit()

        # Log for debugging
        frappe.logger("submission").debug(
            f"Inserted submission: assign_id={submission.assign_id}, "
            f"student_id={submission.student_id}, "
            f"original_url={img_url}, "
            f"gcs_url={public_url}"
        )

        # Send the submission details to RabbitMQ with the GCS public URL
        enqueue_submission(submission.name)

        return {
            "message": "Submission received",
            "submission_id": submission.name,
            "student_id": student_id,
            "image_url": public_url
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.logger("submission").error(f"Error in submit_artwork: {str(e)}")
        frappe.throw(f"Failed to process submission: {str(e)}")

    finally:
        # Switch back to the original user
        frappe.set_user("Administrator")


def enqueue_submission(submission_id):
    """
    Send submission details to RabbitMQ queue.
    The img_url now contains the GCS public URL.
    """
    try:
        submission = frappe.get_doc("ImgSubmission", submission_id)
        
        # Payload with GCS public URL
        payload = {
            "submission_id": submission.name,
            "assign_id": submission.assign_id,
            "student_id": submission.student_id,
            "img_url": submission.img_url,  # This is now the GCS public URL
            # Optional: Add metadata for better detection
            "created_at": str(submission.created_at)
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
        try:
            # First try passive declaration to check if queue exists
            channel.queue_declare(queue=rabbitmq_config['queue'],durable=True,passive=True)
        except Exception:
            # If it doesn't exist, declare it
            channel.queue_declare(queue=rabbitmq_config['queue'], durable=True)


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
    except Exception as e:
        frappe.logger("submission").error(f"Failed to enqueue submission {submission_id}: {str(e)}")
        raise frappe.ValidationError(f"Failed to enqueue submission: {str(e)}")


@frappe.whitelist(allow_guest=True)
def img_feedback(api_key, submission_id):
    """
    API endpoint to get feedback for a submission.
    """
    # Authenticate the API request using the provided api_key
    api_key_doc = frappe.db.get_value("API Key", {"key": api_key, "enabled": 1}, ["user"], as_dict=True)
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
                "overall_feedback": submission.overall_feedback,
                "overall_feedback_translated" : submission.overall_feedback_translated,
                "audio_feedback_url": submission.audio_feedback_url,
            }
        else:
            response = {
                "status": submission.status
            }
        
        return response

    except frappe.DoesNotExistError:
        return {"error": "Submission not found"}
    
    except Exception as e:
        frappe.log_error(f"Error checking submission status: {str(e)}", "Submission Status Error")
        return {"error": "An error occurred while checking submission status"}

    finally:
        # Switch back to the original user
        frappe.set_user("Administrator")


@frappe.whitelist()
def get_assignment_context(assignment_id, student_id=None):
    """Get complete assignment context for RAG service"""
    try:
        assignment = frappe.get_doc("Assignment", assignment_id)
        images = []
        for row in assignment.reference_images:
            file_url = row.image
            file_doc = frappe.get_doc("File", {"file_url": file_url})

            file_path = file_doc.get_full_path()
            with open(file_path, 'rb') as f:
                content = base64.b64encode(f.read()).decode('utf-8')
            images.append({
                'name': file_doc.file_name,
                'content_type': 'image/jpeg',
                'content': content  # base64 encoded
            })

        rubrics = {}
        rubric_grades = assignment.get('rubric_grades', [])

        # Process each rubric grade entry
        for grade in rubric_grades:
            skill_name = grade.get('skill_name')
            if skill_name not in rubrics:
                rubrics[skill_name] = []
            # Create the grade entry with only grade_value and grade_description
            grade_entry = {
                'grade_value': grade.get('grade_value'),
                'grade_description': grade.get('grade_description')
            }
            rubrics[skill_name].append(grade_entry)


        context = {
            "assignment": {
                "name": assignment.assignment_name,
                "description": assignment.description,
                "type": assignment.assignment_type, 
                "subject": assignment.subject,
                "submission_guidelines": assignment.submission_guidelines,
                "reference_images": images,
                "max_score": assignment.max_score,
                "rubrics": rubrics
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


@frappe.whitelist()
def get_student_details(student_id):
    """Get student grade level and language details"""
    try:
        student = frappe.get_doc("Student", student_id)
                    
        print(student)
        
        if not student:
            frappe.log_error(
                f"Student {student_id} not found",
                "Student Details Error"
            )
            return None

            
        
        return {
            "student_id": student.name,
            "grade": student.grade,
            "level": student.level,
            "language": student.language
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error getting student details: {str(e)}",
            "Student Details Error"
        )
        print(e)
        return None


