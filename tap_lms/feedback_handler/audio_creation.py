"""
Audio creation module for generating feedback audio files.
Handles text-to-speech conversion and GCS upload for feedback audio.
"""
import os
import frappe
from tap_lms.audio.audio_helpers import generate_speech_file
from tap_lms.imgana.gcs_client import upload_audio_feedback_to_gcs
from tap_lms.monitoring import emit


def get_language_code(language_name: str) -> str:
    """
    Convert language name (Hindi, Punjabi, Marathi, etc.) to language code (hi, pa, mr, etc.)
    Returns the language code or defaults to 'mr' if not found.
    """
    language_mapping = {
        "English": "en",
        "Hindi": "hi",
        "Punjabi": "pa",
        "Marathi": "mr",
        "Kannada": "ka",
        "hi": "hi",
        "pa": "pa",
        "mr": "mr",
    }
    
    # Normalize the input (case-insensitive)
    normalized_lang = language_name.strip() if language_name else ""
    
    # Try exact match first
    if normalized_lang in language_mapping:
        return language_mapping[normalized_lang]
    
    # Try case-insensitive match
    for key, value in language_mapping.items():
        if key.lower() == normalized_lang.lower():
            return value
    
    # Default to Marathi if not found
    frappe.logger().warning(
        f"Language '{language_name}' not found in mapping, defaulting to 'mr'"
    )
    emit(
        severity="WARNING",
        message="feedback_audio_language_defaulted",
        requested_language=language_name,
        defaulted_to="mr"
    )
    return "mr"


def generate_feedback_audio(
    text: str,
    language_name: str,
    submission_id: str,
    tone: str = None
) -> str:
    """
    Generate audio feedback from translated text and upload to GCS.
    
    Args:
        text: Translated feedback text to convert to speech
        language_name: Language name (Hindi, Punjabi, Marathi, etc.) or language code
        submission_id: Submission ID for file naming
        tone: Optional tone parameter (currently not used)
    
    Returns:
        Public URL of the uploaded audio file on GCS
    
    Raises:
        ValueError: If text is empty or invalid
        frappe.ValidationError: If audio generation or upload fails
    """
    emit(
        severity="INFO",
        message="feedback_audio_generation_start",
        submission_id=submission_id or "unknown",
        language=language_name,
        text_length=len(text) if text else 0
    )

    if not text or not text.strip():
        emit(
            severity="ERROR",
            message="feedback_audio_generation_failed",
            submission_id=submission_id or "unknown",
            error="Text cannot be empty"
        )
        raise ValueError("Text cannot be empty")
    
    if not submission_id:
        emit(
            severity="ERROR",
            message="feedback_audio_generation_failed",
            submission_id="unknown",
            error="Submission ID is required"
        )
        raise ValueError("Submission ID is required")
    
    # Convert language name to language code
    langcode = get_language_code(language_name)
    
    # Create temporary filename
    original_filename = f"feedback_{submission_id}.mp3"
    
    # Create temporary directory for audio file
    try:
        temp_dir = frappe.get_site_path("private", "tmp", "feedback_audio")
    except (AttributeError, RuntimeError):
        # Fallback for when running outside Frappe context
        temp_dir = os.path.join(os.path.expanduser("~"), ".frappe_tmp", "feedback_audio")
    os.makedirs(temp_dir, exist_ok=True)
    
    local_audio_path = os.path.join(temp_dir, original_filename)
    
    try:
        # Generate speech file
        frappe.logger().info(
            f"Generating audio for submission {submission_id} in language {langcode}"
        )
        emit(
            severity="INFO",
            message="feedback_audio_speech_generation_start",
            submission_id=submission_id,
            language=langcode
        )
        generate_speech_file(text, langcode, local_audio_path, tone)
        
        emit(
            severity="INFO",
            message="feedback_audio_speech_generated",
            submission_id=submission_id
        )
        
        # Upload to GCS
        frappe.logger().info(
            f"Uploading audio to GCS for submission {submission_id}"
        )
        emit(
            severity="INFO",
            message="feedback_audio_gcs_upload_start",
            submission_id=submission_id,
            file_path=local_audio_path
        )
        public_url = upload_audio_feedback_to_gcs(
            local_audio_path,
            submission_id,
            original_filename,
        )
        
        emit(
            severity="INFO",
            message="feedback_audio_generation_success",
            submission_id=submission_id,
            public_url=public_url
        )
        return public_url
        
    except Exception as e:
        frappe.logger().error(
            f"Error generating feedback audio for submission {submission_id}: {str(e)}"
        )
        emit(
            severity="ERROR",
            message="feedback_audio_generation_failed",
            submission_id=submission_id,
            error=str(e)
        )
        raise frappe.ValidationError(f"Failed to generate feedback audio: {str(e)}")
    
    finally:
        # Clean up local file after upload
        if os.path.exists(local_audio_path):
            try:
                os.remove(local_audio_path)
                frappe.logger().info(
                    f"Cleaned up temporary audio file: {local_audio_path}"
                )
            except Exception as e:
                frappe.logger().warning(
                    f"Failed to delete temporary audio file {local_audio_path}: {str(e)}"
                )


def get_pre_generated_feedback_audio(feedback_type: str, language_name: str) -> str:
    if feedback_type == 'plagarised':
        pass

    elif feedback_type == 'ai-generated':
        pass

    elif feedback_type == 'system-error':
        pass

