import frappe
import json
from frappe import _
import traceback
from datetime import datetime

# Task #1 (2026-05-28): journey APIs deprecated pre-launch. Preferred-day /
# preferred-time was a legacy feature; the SP flow uses a fixed Tuesday
# cadence (via summer_program.scheduler.weekly_content_delivery_trigger),
# so the per-student preferences are no longer used. Old bodies preserved
# as _DEPRECATED_* for one release cycle.
from tap_lms.journey._deprecation import journey_deprecated_response


@frappe.whitelist(allow_guest=False)
def update_student_preferences(**_kw):
    """[DEPRECATED 2026-05-28] Was: update student's preferred day/time.
    No SP-module replacement — the SP flow uses a fixed Tuesday cadence."""
    return journey_deprecated_response("update_student_preferences", _kw)


# _DEPRECATED_update_student_preferences_original ===================
def _DEPRECATED_update_student_preferences_original(
    student_id=None, glific_id=None, phone=None, name=None,
    preferred_day=None, preferred_time=None,
):
    """
    Update student's preferred day and time for receiving messages

    Args:
        student_id (str, optional): Student ID (name field)
        glific_id (str, optional): Glific ID of student  
        phone (str, optional): Phone number of student
        name (str, optional): Student name to help identify unique student (STRICT matching)
        preferred_day (str, optional): Preferred day (Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday, Daily)
        preferred_time (str, optional): Preferred time in 12-hour format (e.g., "10:00 AM", "6:45 PM") or 24-hour format (e.g., "14:30")
        
    Returns:
        dict: Success status with updated preferences information
    """
    try:
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)
        
        # Validate that at least one identifier is provided
        if not student_id and not glific_id and not phone:
            frappe.local.response.http_status_code = 400
            return {
                "success": False, 
                "error": "At least one of student_id, glific_id, or phone must be provided"
            }
        
        # Validate that at least one preference field is provided
        if preferred_day is None and preferred_time is None:
            frappe.local.response.http_status_code = 400
            return {
                "success": False,
                "error": "At least one of preferred_day or preferred_time must be provided"
            }
        
        # Validate preferred_day options
        valid_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Daily"]
        if preferred_day is not None and preferred_day not in valid_days:
            frappe.local.response.http_status_code = 400
            return {
                "success": False,
                "error": f"Invalid preferred_day. Allowed values: {', '.join(valid_days)}"
            }
        
        # Validate preferred_time format (12-hour with AM/PM or 24-hour HH:MM)
        if preferred_time is not None:
            converted_time = validate_and_convert_time(preferred_time)
            if converted_time is None:
                frappe.local.response.http_status_code = 400
                return {
                    "success": False,
                    "error": "Invalid preferred_time format. Use 12-hour format like '10:00 AM' or '6:45 PM', or 24-hour format like '14:30'"
                }
        
        # Find the student using STRICT name matching
        student_records = find_student_records(student_id, glific_id, phone, name)
        
        if not student_records:
            frappe.local.response.http_status_code = 404
            
            # Enhanced error message for strict name matching
            if name and glific_id:
                # Check if students exist with this glific_id but different names
                all_students_with_glific = frappe.get_all(
                    "Student",
                    filters={"glific_id": glific_id},
                    fields=["name", "name1"],
                    order_by="creation desc"
                )
                
                if all_students_with_glific:
                    available_names = [s.name1 for s in all_students_with_glific if s.name1]
                    return {
                        "success": False,
                        "error": f"No student found with name '{name}' for glific_id '{glific_id}'",
                        "available_students": available_names,
                        "suggestion": f"Use one of these names: {', '.join(available_names)}" if available_names else "No student names available"
                    }
            
            return {
                "success": False,
                "error": "Student not found"
            }
        
        # Get the student document
        student = frappe.get_doc("Student", student_records[0].name)
        
        # Track what was updated
        updated_fields = {}
        
        # Update preferred_day if provided
        if preferred_day is not None:
            student.preferred_day = preferred_day
            updated_fields["preferred_day"] = preferred_day
        
        # Update preferred_time if provided  
        if preferred_time is not None:
            # Convert to 24-hour format for storage
            converted_time = validate_and_convert_time(preferred_time)
            student.preferred_time = converted_time
            updated_fields["preferred_time"] = preferred_time  # Show original format in response
        
        # Save the student document
        student.save()
        
        # Build response with current values
        response = {
            "success": True,
            "message": "Student preferences updated successfully",
            "student_id": student.name,
            "student_name": student.name1,
            "updated_fields": updated_fields,
            "current_preferences": {
                "preferred_day": student.preferred_day,
                "preferred_time": convert_time_to_12_hour(student.preferred_time) if student.preferred_time else None
            }
        }
        
        # Add warning if multiple students were found (should be rare with strict matching)
        if len(student_records) > 1:
            response["_warning"] = f"Multiple students found with exact name match. Updated the most recently created one. Count: {len(student_records)}"
        
        # Add search parameters used for debugging
        response["_search_params"] = {
            "student_id": student_id,
            "glific_id": glific_id, 
            "phone": phone,
            "name": name
        }
        
        return response
        
    except frappe.ValidationError as e:
        frappe.local.response.http_status_code = 400
        frappe.log_error(
            f"Validation error in update_student_preferences: {str(e)}",
            "Student Preferences Update API Validation Error"
        )
        return {
            "success": False,
            "error": str(e)
        }
    
    except frappe.AuthenticationError as e:
        frappe.local.response.http_status_code = 401
        frappe.log_error(
            f"Authentication error in update_student_preferences: {str(e)}",
            "Student Preferences Update API Error"
        )
        return {
            "success": False,
            "error": str(e)
        }
    
    except Exception as e:
        frappe.local.response.http_status_code = 500
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error updating student preferences: {str(e)}\n{error_traceback}",
            "Student Preferences Update API Error"
        )
        return {
            "success": False,
            "error": str(e)
        }


@frappe.whitelist(allow_guest=False)
def get_student_preferences(**_kw):
    """[DEPRECATED 2026-05-28] Was: get student's current preferences.
    No SP-module replacement — the feature is gone."""
    return journey_deprecated_response("get_student_preferences", _kw)


# _DEPRECATED_get_student_preferences_original =======================
def _DEPRECATED_get_student_preferences_original(
    student_id=None, glific_id=None, phone=None, name=None,
):
    """
    Get student's current preferences for day and time

    Args:
        student_id (str, optional): Student ID (name field)
        glific_id (str, optional): Glific ID of student
        phone (str, optional): Phone number of student
        name (str, optional): Student name to help identify unique student (STRICT matching)

    Returns:
        dict: Student's current preferences
    """
    try:
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)
        
        # Validate that at least one identifier is provided
        if not student_id and not glific_id and not phone:
            frappe.local.response.http_status_code = 400
            return {
                "success": False, 
                "error": "At least one of student_id, glific_id, or phone must be provided"
            }
        
        # Find the student using STRICT name matching
        student_records = find_student_records(student_id, glific_id, phone, name)
        
        if not student_records:
            frappe.local.response.http_status_code = 404
            
            # Enhanced error message for strict name matching
            if name and glific_id:
                # Check if students exist with this glific_id but different names
                all_students_with_glific = frappe.get_all(
                    "Student",
                    filters={"glific_id": glific_id},
                    fields=["name", "name1"],
                    order_by="creation desc"
                )
                
                if all_students_with_glific:
                    available_names = [s.name1 for s in all_students_with_glific if s.name1]
                    return {
                        "success": False,
                        "error": f"No student found with name '{name}' for glific_id '{glific_id}'",
                        "available_students": available_names,
                        "suggestion": f"Use one of these names: {', '.join(available_names)}" if available_names else "No student names available"
                    }
            
            return {
                "success": False,
                "error": "Student not found"
            }
        
        # Get the student document
        student = frappe.get_doc("Student", student_records[0].name)
        
        # Build response
        response = {
            "success": True,
            "student_id": student.name,
            "student_name": student.name1,
            "preferences": {
                "preferred_day": student.preferred_day,
                "preferred_time": convert_time_to_12_hour(student.preferred_time) if student.preferred_time else None
            }
        }
        
        # Add warning if multiple students were found (should be rare with strict matching)
        if len(student_records) > 1:
            response["_warning"] = f"Multiple students found with exact name match. Showing preferences for the most recently created one. Count: {len(student_records)}"
        
        return response
        
    except frappe.AuthenticationError as e:
        frappe.local.response.http_status_code = 401
        return {"success": False, "error": str(e)}
    
    except Exception as e:
        frappe.local.response.http_status_code = 500
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error getting student preferences: {str(e)}\n{error_traceback}",
            "Student Preferences Get API Error"
        )
        return {"success": False, "error": str(e)}


def find_student_records(student_id=None, glific_id=None, phone=None, name=None):
    """
    Helper function to find student records using STRICT name matching
    If name is provided and doesn't match, returns empty list to prevent wrong updates
    
    Returns:
        list: List of student records found, or empty list if name doesn't match
    """
    student_records = []
    
    # If student_id is provided, use it directly (no name validation for direct student_id)
    if student_id:
        student_records = frappe.get_all(
            "Student",
            filters={"name": student_id},
            fields=["name", "name1", "phone", "glific_id", "creation"],
            limit=1
        )
    else:
        # Build filters for other identifiers
        filters = {}
        
        if glific_id:
            filters["glific_id"] = glific_id
        
        if phone:
            filters["phone"] = str(phone).strip()
        
        # Find students with current filters
        student_records = frappe.get_all(
            "Student",
            filters=filters,
            fields=["name", "name1", "phone", "glific_id", "creation"],
            order_by="creation desc"
        )
        
        # Phone fallback logic (if original search failed) - SAME AS get_student_minimal_details
        if not student_records and phone and str(phone).strip().startswith('91') and len(str(phone).strip()) == 12:
            phone_without_prefix = str(phone).strip()[2:]
            filters["phone"] = phone_without_prefix
            
            student_records = frappe.get_all(
                "Student",
                filters=filters,
                fields=["name", "name1", "phone", "glific_id", "creation"],
                order_by="creation desc"
            )
        
        # STRICT Name filtering logic - Only proceed if name matches or no name provided
        if name and len(student_records) > 1:
            normalized_name = str(name).strip().lower()
            
            # Try exact match first
            exact_matches = [s for s in student_records if s.name1 and s.name1.strip().lower() == normalized_name]
            
            if exact_matches:
                student_records = exact_matches
            else:
                # Try partial match
                partial_matches = [s for s in student_records if s.name1 and normalized_name in s.name1.strip().lower()]
                if partial_matches:
                    student_records = partial_matches
                else:
                    # STRICT MATCHING: Return empty if no name matches found
                    # This prevents updating wrong students
                    student_records = []
        
        # Additional safety check: if name provided but only one student found, verify name match
        elif name and len(student_records) == 1:
            normalized_name = str(name).strip().lower()
            student_name = student_records[0].name1
            
            if student_name:
                student_name_normalized = student_name.strip().lower()
                # Check if name matches (exact or partial)
                if normalized_name != student_name_normalized and normalized_name not in student_name_normalized:
                    # Name doesn't match - return empty to prevent wrong update
                    student_records = []
    
    return student_records


def validate_and_convert_time(time_str):
    """
    Validate and convert time string to 24-hour format
    Supports both 12-hour (10:00 AM, 6:45 PM) and 24-hour (14:30) formats
    
    Args:
        time_str (str): Time string in various formats
        
    Returns:
        str: Time in HH:MM 24-hour format, or None if invalid
    """
    if not time_str:
        return None
    
    time_str = str(time_str).strip()
    
    # Try different time formats
    formats_to_try = [
        "%I:%M %p",    # 10:00 AM
        "%I:%M%p",     # 10:00AM (no space)
        "%H:%M"        # 14:30 (24-hour)
    ]
    
    for fmt in formats_to_try:
        try:
            time_obj = datetime.strptime(time_str.upper(), fmt)
            return time_obj.strftime("%H:%M")
        except ValueError:
            continue
    
    return None


def convert_time_to_12_hour(time_obj):
    """
    Convert time object or string to 12-hour format with AM/PM
    
    Args:
        time_obj: Time object or string in HH:MM format
        
    Returns:
        str: Time in 12-hour format (e.g., "10:00 AM")
    """
    if not time_obj:
        return None
    
    try:
        if isinstance(time_obj, str):
            time_obj = datetime.strptime(time_obj, "%H:%M").time()
        
        # Convert to 12-hour format and remove leading zero from hour
        return time_obj.strftime("%I:%M %p").lstrip('0')
    except Exception:
        # Fallback: return as string if conversion fails
        return str(time_obj)
