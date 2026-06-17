import frappe
import json
from frappe import _
import traceback

# Task #1 (2026-05-28): journey APIs deprecated pre-launch. All 7
# whitelisted endpoints in this file return a deprecation envelope.
# Replacements vary per endpoint — see the _REPLACEMENT_HINT map in
# tap_lms/journey/_deprecation.py. Original bodies preserved as
# `_DEPRECATED_*_original` helpers for one release cycle.
from tap_lms.journey._deprecation import journey_deprecated_response


@frappe.whitelist(allow_guest=False)
def get_profile(**_kw):
    """[DEPRECATED 2026-05-28] Use Frappe REST `GET /api/resource/Student/<id>` or summer_program/program_enrollment_api.get_student_state."""
    return journey_deprecated_response("get_profile", _kw)


def _DEPRECATED_get_profile_original(student_id=None, phone=None, glific_id=None):
    """
    Get student profile details
    
    Args:
        student_id (str, optional): Student ID (name field)
        phone (str, optional): Phone number of student
        glific_id (str, optional): Glific ID of student
        
    Returns:
        dict: Student profile information with enrollment details
    """
    try:
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)
            
        # Validate that at least one parameter is provided
        if not student_id and not phone and not glific_id:
            return {"success": False, "message": "At least one of student_id, phone, or glific_id must be provided"}
        
        # Build filters based on provided parameters
        filters = {}
        if student_id:
            filters["name"] = student_id
        if phone:
            # Format phone number if needed
            formatted_phone = format_phone_number(phone)
            filters["phone"] = formatted_phone
        if glific_id:
            filters["glific_id"] = glific_id
        
        # Find the student
        student_records = frappe.get_all(
            "Student", 
            filters=filters,
            fields=["name"]
        )
        
        if not student_records:
            return {"success": False, "message": "Student not found"}
        
        # Get the complete student document
        student = frappe.get_doc("Student", student_records[0].name)
        
        # Build the response with required fields
        response = {
            "success": True,
            "data": {
                "student_id": student.name,
                "name": student.name1 or None,
                "phone": student.phone or None,
                "gender": student.gender or None,
                "glific_id": student.glific_id or None,
                "language": get_language_details(student.language) if student.language else None,
                "school": get_school_details(student.school_id) if student.school_id else None,
                "enrollments": get_enrollment_details(student) if hasattr(student, 'enrollment') and student.enrollment else None
            }
        }
        
        return response
    
    except frappe.AuthenticationError as e:
        frappe.log_error(
            f"Authentication error: {str(e)}", 
            "Student Profile API Error"
        )
        return {"success": False, "message": str(e)}
    
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error getting student profile: {str(e)}\n{error_traceback}", 
            "Student Profile API Error"
        )
        return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False)
def search(**_kw):
    """[DEPRECATED 2026-05-28] Use Frappe REST API: `GET /api/method/frappe.client.get_list?doctype=Student&filters=...`."""
    return journey_deprecated_response("search", _kw)


def _DEPRECATED_search_original(query=None, offset=0, limit=20):
    """
    Search for students by name, phone, or glific ID
    
    Args:
        query (str): Search query
        offset (int): Offset for pagination
        limit (int): Limit for pagination
        
    Returns:
        dict: List of matching students with basic details
    """
    try:
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)
            
        # Validate query
        if not query:
            return {"success": False, "message": "Search query is required"}
        
        # Convert to integers
        try:
            offset = int(offset)
            limit = int(limit)
        except (ValueError, TypeError):
            offset = 0
            limit = 20
        
        # Build search conditions
        conditions = """
            (name1 LIKE %(query)s OR
            phone LIKE %(query)s OR
            glific_id LIKE %(query)s)
        """
        
        # Count total matches
        count_query = f"""
            SELECT COUNT(*) as total
            FROM `tabStudent`
            WHERE {conditions}
        """
        
        total_count = frappe.db.sql(count_query, {"query": f"%{query}%"}, as_dict=True)
        total = total_count[0].total if total_count else 0
        
        # If no results, return early
        if total == 0:
            return {
                "success": True,
                "data": {
                    "students": [],
                    "total": 0,
                    "offset": offset,
                    "limit": limit,
                    "has_more": False
                }
            }
        
        # Get matching students with pagination
        students_query = f"""
            SELECT 
                name, name1, phone, gender, school_id, glific_id
            FROM `tabStudent`
            WHERE {conditions}
            ORDER BY name1
            LIMIT {limit} OFFSET {offset}
        """
        
        students = frappe.db.sql(students_query, {"query": f"%{query}%"}, as_dict=True)
        
        # Format response
        student_list = []
        for student in students:
            # Get school name if available
            school_name = None
            if student.school_id:
                school = frappe.db.get_value("School", student.school_id, "name1")
                school_name = school
            
            student_list.append({
                "id": student.name,
                "name": student.name1 or None,
                "phone": student.phone or None,
                "gender": student.gender or None,
                "glific_id": student.glific_id or None,
                "school": {
                    "id": student.school_id,
                    "name": school_name
                } if student.school_id else None
            })
        
        return {
            "success": True,
            "data": {
                "students": student_list,
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": (offset + len(student_list)) < total
            }
        }
    
    except frappe.AuthenticationError as e:
        frappe.log_error(
            f"Authentication error: {str(e)}", 
            "Student Search API Error"
        )
        return {"success": False, "message": str(e)}
    
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error searching students: {str(e)}\n{error_traceback}", 
            "Student Search API Error"
        )
        return {"success": False, "message": str(e)}


def format_phone_number(phone):
    """Format phone number for consistency (91 + 10 digits for India)"""
    if not phone:
        return None
    
    phone = str(phone).strip().replace(' ', '')
    if len(phone) == 10:
        return f"91{phone}"
    elif len(phone) == 12 and phone.startswith('91'):
        return phone
    else:
        return phone  # Return as-is if format doesn't match expected patterns


def get_school_details(school_id):
    """
    Get school name from School doctype
    
    Args:
        school_id (str): School ID
        
    Returns:
        dict: School details with name
    """
    if not school_id:
        return None
    
    try:
        school = frappe.get_doc("School", school_id)
        return {
            "id": school.name,
            "name": school.name1 if hasattr(school, 'name1') else None
        }
    except Exception as e:
        frappe.log_error(f"Error fetching school details: {str(e)}", "Student Profile API Error")
        return {
            "id": school_id,
            "name": None
        }


def get_language_details(language_id):
    """
    Get language details from TAP Language doctype
    
    Args:
        language_id (str): Language ID
        
    Returns:
        dict: Language details
    """
    if not language_id:
        return None
    
    try:
        language = frappe.get_doc("TAP Language", language_id)
        return {
            "id": language.name,
            "name": language.language_name if hasattr(language, 'language_name') else None,
            "code": language.language_code if hasattr(language, 'language_code') else None
        }
    except Exception as e:
        frappe.log_error(f"Error fetching language details: {str(e)}", "Student Profile API Error")
        return {
            "id": language_id,
            "name": None,
            "code": None
        }


def get_enrollment_details(student):
    """
    Get enrollment details from student's enrollment child table
    
    Args:
        student (object): Student document
        
    Returns:
        list: List of enrollment details
    """
    if not hasattr(student, 'enrollment') or not student.enrollment:
        return None
    
    enrollments = []
    
    for enrollment in student.enrollment:
        enrollment_data = {
            "batch": get_batch_details(enrollment.batch) if enrollment.batch else None,
            "course": get_course_details(enrollment.course) if enrollment.course else None,
            "grade": enrollment.grade or None,
            "date_joining": str(enrollment.date_joining) if enrollment.date_joining else None,
            "school": get_school_details(enrollment.school) if enrollment.school else None
        }
        enrollments.append(enrollment_data)
    
    return enrollments


def get_batch_details(batch_id):
    """
    Get batch details from Batch doctype
    
    Args:
        batch_id (str): Batch ID
        
    Returns:
        dict: Batch details
    """
    if not batch_id:
        return None
    
    try:
        batch = frappe.get_doc("Batch", batch_id)
        return {
            "id": batch.name,
            "name": batch.name1 if hasattr(batch, 'name1') else None,
            "title": batch.title if hasattr(batch, 'title') else None,
            "start_date": str(batch.start_date) if hasattr(batch, 'start_date') and batch.start_date else None,
            "end_date": str(batch.end_date) if hasattr(batch, 'end_date') and batch.end_date else None
        }
    except Exception as e:
        frappe.log_error(f"Error fetching batch details: {str(e)}", "Student Profile API Error")
        return {
            "id": batch_id,
            "name": None
        }


def get_course_details(course_id):
    """
    Get course details from Course Level doctype
    
    Args:
        course_id (str): Course ID
        
    Returns:
        dict: Course details with vertical info
    """
    if not course_id:
        return None
    
    try:
        course = frappe.get_doc("Course Level", course_id)
        
        # Get vertical info if available
        vertical_info = None
        if hasattr(course, 'vertical') and course.vertical:
            try:
                vertical = frappe.get_doc("Course Verticals", course.vertical)
                vertical_info = {
                    "id": vertical.name,
                    "name": vertical.name1 if hasattr(vertical, 'name1') else None,
                    "short_name": vertical.name2 if hasattr(vertical, 'name2') else None
                }
            except Exception as e:
                frappe.log_error(f"Error fetching vertical details: {str(e)}", "Student Profile API Error")
        
        return {
            "id": course.name,
            "name": course.name1 if hasattr(course, 'name1') else None,
            "vertical": vertical_info,
            "stage": get_stage_details(course.stage) if hasattr(course, 'stage') and course.stage else None
        }
    except Exception as e:
        frappe.log_error(f"Error fetching course details: {str(e)}", "Student Profile API Error")
        return {
            "id": course_id,
            "name": None
        }


def get_stage_details(stage_id):
    """
    Get stage details from Stage Grades doctype
    
    Args:
        stage_id (str): Stage ID
        
    Returns:
        dict: Stage details
    """
    if not stage_id:
        return None
    
    try:
        stage = frappe.get_doc("Stage Grades", stage_id)
        return {
            "id": stage.name,
            "name": stage.stage_name if hasattr(stage, 'stage_name') else None,
            "from_grade": stage.from_grade if hasattr(stage, 'from_grade') else None,
            "to_grade": stage.to_grade if hasattr(stage, 'to_grade') else None
        }
    except Exception as e:
        frappe.log_error(f"Error fetching stage details: {str(e)}", "Student Profile API Error")
        return {
            "id": stage_id,
            "name": None
        }


@frappe.whitelist(allow_guest=False)
def get_student_glific_groups(**_kw):
    """[DEPRECATED 2026-05-28] No SP equivalent — group membership is maintained by summer_program/collection_membership and the 5 PGCollection rows per BPR are the source of truth."""
    return journey_deprecated_response("get_student_glific_groups", _kw)


def _DEPRECATED_get_student_glific_groups_original(student_id=None, phone=None, glific_id=None):
    """
    Get Glific contact groups associated with a student
    
    Args:
        student_id (str, optional): Student ID (name field)
        phone (str, optional): Phone number of student
        glific_id (str, optional): Glific ID of student
        
    Returns:
        dict: Student information with associated Glific contact groups
    """
    try:
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)
            
        # Validate that at least one parameter is provided
        if not student_id and not phone and not glific_id:
            return {"success": False, "message": "At least one of student_id, phone, or glific_id must be provided"}
        
        # Find the student
        student = None
        
        # Build filters based on provided parameters
        filters = {}
        if student_id:
            filters["name"] = student_id
        if phone:
            # Format phone number if needed
            formatted_phone = format_phone_number(phone)
            filters["phone"] = formatted_phone
        if glific_id:
            filters["glific_id"] = glific_id
        
        student_records = frappe.get_all(
            "Student", 
            filters=filters,
            fields=["name"]
        )
        
        if not student_records:
            return {"success": False, "message": "Student not found"}
        
        student = frappe.get_doc("Student", student_records[0].name)
        
        # Find all Backend Student Onboarding sets that this student is part of
        if not student.glific_id:
            return {
                "success": True, 
                "data": {
                    "student_id": student.name,
                    "name": student.name1 or None,
                    "phone": student.phone or None,
                    "glific_id": None,
                    "message": "Student does not have a Glific ID",
                    "contact_groups": []
                }
            }
        
        # Find backend_onboarding sets where this student is included
        backend_students = frappe.get_all(
            "Backend Students",
            filters={"student_id": student.name, "processing_status": "Success"},
            fields=["parent"]
        )
        
        if not backend_students:
            return {
                "success": True, 
                "data": {
                    "student_id": student.name,
                    "name": student.name1 or None,
                    "phone": student.phone or None,
                    "glific_id": student.glific_id,
                    "message": "Student not found in any backend onboarding sets",
                    "contact_groups": []
                }
            }
        
        # Get the unique backend onboarding set IDs
        backend_set_ids = list(set([bs.parent for bs in backend_students]))
        
        # Find all Glific contact groups associated with these backend sets
        contact_groups = frappe.get_all(
            "GlificContactGroup",
            filters={"backend_onboarding_set": ["in", backend_set_ids]},
            fields=["name", "group_id", "label", "description", "backend_onboarding_set"]
        )
        
        # Check if student's phone is directly associated with a Glific group
        # (This is an additional check in case the backend_onboarding_set relationship is missing)
        if student.phone:
            # Try to find direct connections via Student's phone in Backend Students table
            phone_matches = frappe.get_all(
                "Backend Students",
                filters={"phone": student.phone, "processing_status": "Success"},
                fields=["parent"]
            )
            
            if phone_matches:
                additional_set_ids = list(set([pm.parent for pm in phone_matches if pm.parent not in backend_set_ids]))
                
                if additional_set_ids:
                    additional_groups = frappe.get_all(
                        "GlificContactGroup",
                        filters={"backend_onboarding_set": ["in", additional_set_ids]},
                        fields=["name", "group_id", "label", "description", "backend_onboarding_set"]
                    )
                    
                    # Add to the existing groups without duplicates
                    existing_ids = [g.name for g in contact_groups]
                    for group in additional_groups:
                        if group.name not in existing_ids:
                            contact_groups.append(group)
        
        # If we have the Glific ID, we can also check for direct associations
        if student.glific_id:
            # First, find other students with the same Glific ID (should be rare but possible)
            glific_matches = frappe.get_all(
                "Student",
                filters={"glific_id": student.glific_id, "name": ["!=", student.name]},
                fields=["name"]
            )
            
            if glific_matches:
                # Find backend sets for these students
                glific_backend_students = frappe.get_all(
                    "Backend Students",
                    filters={"student_id": ["in", [gm.name for gm in glific_matches]], "processing_status": "Success"},
                    fields=["parent"]
                )
                
                if glific_backend_students:
                    additional_set_ids = list(set([gbs.parent for gbs in glific_backend_students if gbs.parent not in backend_set_ids]))
                    
                    if additional_set_ids:
                        additional_groups = frappe.get_all(
                            "GlificContactGroup",
                            filters={"backend_onboarding_set": ["in", additional_set_ids]},
                            fields=["name", "group_id", "label", "description", "backend_onboarding_set"]
                        )
                        
                        # Add to the existing groups without duplicates
                        existing_ids = [g.name for g in contact_groups]
                        for group in additional_groups:
                            if group.name not in existing_ids:
                                contact_groups.append(group)
        
        # Get batch and enrollment info for each group
        enriched_contact_groups = []
        for group in contact_groups:
            # Get the backend onboarding set for this group
            backend_set = group.backend_onboarding_set
            if not backend_set:
                enriched_contact_groups.append({
                    "id": group.name,
                    "group_id": group.group_id,
                    "label": group.label,
                    "description": group.description,
                    "backend_onboarding_set": None,
                    "batch": None,
                    "course_vertical": None
                })
                continue
            
            # Get a sample Backend Student record to determine batch and course vertical
            sample_students = frappe.get_all(
                "Backend Students",
                filters={"parent": backend_set, "processing_status": "Success"},
                fields=["batch", "course_vertical"],
                limit=1
            )
            
            if not sample_students:
                enriched_contact_groups.append({
                    "id": group.name,
                    "group_id": group.group_id,
                    "label": group.label,
                    "description": group.description,
                    "backend_onboarding_set": backend_set,
                    "batch": None,
                    "course_vertical": None
                })
                continue
            
            # Get batch details
            batch_data = None
            if sample_students[0].batch:
                try:
                    batch = frappe.get_doc("Batch", sample_students[0].batch)
                    batch_data = {
                        "id": batch.name,
                        "name": batch.name1 if hasattr(batch, 'name1') else None,
                        "title": batch.title if hasattr(batch, 'title') else None
                    }
                except Exception as e:
                    frappe.log_error(f"Error fetching batch details for Glific group: {str(e)}", "Student Glific Groups API Error")
            
            # Get course vertical details
            vertical_data = None
            if sample_students[0].course_vertical:
                try:
                    vertical = frappe.get_doc("Course Verticals", sample_students[0].course_vertical)
                    vertical_data = {
                        "id": vertical.name,
                        "name": vertical.name1 if hasattr(vertical, 'name1') else None,
                        "short_name": vertical.name2 if hasattr(vertical, 'name2') else None
                    }
                except Exception as e:
                    frappe.log_error(f"Error fetching vertical details for Glific group: {str(e)}", "Student Glific Groups API Error")
            
            # Enrich the contact group with batch and course vertical info
            enriched_contact_groups.append({
                "id": group.name,
                "group_id": group.group_id,
                "label": group.label,
                "description": group.description,
                "backend_onboarding_set": backend_set,
                "batch": batch_data,
                "course_vertical": vertical_data
            })
        
        # Build the final response
        return {
            "success": True,
            "data": {
                "student_id": student.name,
                "name": student.name1 or None,
                "phone": student.phone or None,
                "glific_id": student.glific_id,
                "contact_groups": enriched_contact_groups
            }
        }
            
    except frappe.AuthenticationError as e:
        frappe.log_error(
            f"Authentication error: {str(e)}", 
            "Student Glific Groups API Error"
        )
        return {"success": False, "message": str(e)}
    
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error getting student Glific groups: {str(e)}\n{error_traceback}", 
            "Student Glific Groups API Error"
        )
        return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False)
def get_student_minimal_details(**_kw):
    """[DEPRECATED 2026-05-28] Use summer_program/program_enrollment_api.get_student_state for flat-map SP-state response."""
    return journey_deprecated_response("get_student_minimal_details", _kw)


def _DEPRECATED_get_student_minimal_details_original(glific_id=None, phone=None, name=None):
    """
    Get student minimal details by Glific ID, with optional phone and name for disambiguation

    Args:
        glific_id (str): Glific ID of student (required)
        phone (str, optional): Phone number to help identify unique student
        name (str, optional): Student name to help identify unique student

    Returns:
        dict: Student minimal information with latest enrollment details and course vertical
              Uses phone+name combination to find unique student when multiple exist
              Includes multi_enrollment indicator
    """
    try:
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)

        # Validate that glific_id is provided
        if not glific_id:
            frappe.local.response.http_status_code = 400
            return {"error": "glific_id is required"}

        # Build filters starting with glific_id
        filters = {"glific_id": glific_id}

        # Track if we used fallback phone search
        phone_search_fallback = False
        original_phone = phone

        # Add phone to filters if provided
        if phone:
            filters["phone"] = str(phone).strip()

        # Find students with current filters
        student_records = frappe.get_all(
            "Student",
            filters=filters,
            fields=["name", "name1", "phone", "creation"],
            order_by="creation desc"
        )

        # If no results and phone was provided with 91 prefix, try without 91
        if not student_records and phone and str(phone).strip().startswith('91') and len(str(phone).strip()) == 12:
            # Remove 91 prefix and search again
            phone_without_prefix = str(phone).strip()[2:]
            filters["phone"] = phone_without_prefix
            phone_search_fallback = True

            # Search again with modified phone
            student_records = frappe.get_all(
                "Student",
                filters=filters,
                fields=["name", "name1", "phone", "creation"],
                order_by="creation desc"
            )

            # Log the fallback search
            if student_records:
                frappe.log_error(
                    f"Phone fallback search used for glific_id: {glific_id}. " +
                    f"Original phone: {original_phone}, Found with: {phone_without_prefix}",
                    "Phone Search Fallback Used"
                )

        # If name is provided and we have multiple records, filter by name
        if name and len(student_records) > 1:
            # Normalize the provided name for comparison
            normalized_name = str(name).strip().lower()

            # Try exact match first
            exact_matches = [s for s in student_records if s.name1 and s.name1.strip().lower() == normalized_name]

            # If no exact match, try partial match
            if not exact_matches:
                partial_matches = [s for s in student_records if s.name1 and normalized_name in s.name1.strip().lower()]
                if partial_matches:
                    student_records = partial_matches
            else:
                student_records = exact_matches

        if not student_records:
            frappe.local.response.http_status_code = 404
            return {"error": "Student not found"}

        # Check if we still have multiple students
        multiple_students_warning = None
        disambiguation_info = None

        if len(student_records) > 1:
            # Create disambiguation info
            disambiguation_info = {
                "total_found": len(student_records),
                "students": []
            }

            for record in student_records[:5]:  # Show max 5 students
                disambiguation_info["students"].append({
                    "student_id": record.name,
                    "name": record.name1,
                    "phone": record.phone
                })

            if len(student_records) > 5:
                disambiguation_info["more_students"] = len(student_records) - 5

            multiple_students_warning = {
                "message": f"Multiple students found with glific_id: {glific_id}",
                "count": len(student_records),
                "selected_student": {
                    "student_id": student_records[0].name,
                    "name": student_records[0].name1,
                    "phone": student_records[0].phone
                },
                "selection_criteria": "most_recently_created",
                "suggestion": "Use phone and/or name parameters to identify unique student",
                "disambiguation": disambiguation_info
            }

            # Log this issue
            frappe.log_error(
                f"Multiple students found with glific_id {glific_id}. Selected: {student_records[0].name}. " +
                f"Filters used - phone: {phone}, name: {name}",
                "Multiple Students with Same Glific ID"
            )

        # Get the first (most recently created) student document
        student = frappe.get_doc("Student", student_records[0].name)

        # Check for multiple enrollments and get the latest enrollment
        multi_enrollment = "No"
        latest_enrollment = None
        
        if hasattr(student, 'enrollment') and student.enrollment:
            # Check if student has multiple enrollments
            if len(student.enrollment) > 1:
                multi_enrollment = "Yes"
            
            # FIXED: Handle both date and datetime objects properly
            def get_date_for_sorting(enrollment):
                """Convert date_joining to datetime for consistent sorting"""
                if not enrollment.date_joining:
                    return frappe.utils.datetime.datetime.min
                
                # If it's already a datetime, return as is
                if isinstance(enrollment.date_joining, frappe.utils.datetime.datetime):
                    return enrollment.date_joining
                
                # If it's a date, convert to datetime
                if isinstance(enrollment.date_joining, frappe.utils.datetime.date):
                    return frappe.utils.datetime.datetime.combine(enrollment.date_joining, frappe.utils.datetime.time.min)
                
                # If it's a string, try to parse it
                if isinstance(enrollment.date_joining, str):
                    try:
                        return frappe.utils.datetime.datetime.strptime(enrollment.date_joining, '%Y-%m-%d')
                    except ValueError:
                        try:
                            return frappe.utils.datetime.datetime.strptime(enrollment.date_joining, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            return frappe.utils.datetime.datetime.min
                
                # Fallback
                return frappe.utils.datetime.datetime.min
            
            # Get the latest enrollment using the fixed sorting function
            sorted_enrollments = sorted(
                student.enrollment,
                key=get_date_for_sorting,
                reverse=True
            )
            latest_enrollment = sorted_enrollments[0] if sorted_enrollments else None

        # Initialize response data
        response_data = {
            "name": student.name1 or None,
            "student_id": student.name,
            "glific_id": student.glific_id or None,
            "phone": student.phone or None,
            "language": None,
            "gender": student.gender or None,
            "grade": None,
            "course_level": None,
            "course_vertical": None,
            "course_vertical_short": None,
            "school": None,
            "city": None,
            "district": None,
            "batch_id": None,
            "batch_name": None,
            "batch_skeyword": None,
            "multi_enrollment": multi_enrollment  # NEW PARAMETER
        }

        # Get course vertical from Backend Students table (Primary Source)
        try:
            backend_student = frappe.get_all(
                "Backend Students",
                filters={"student_id": student.name, "processing_status": "Success"},
                fields=["course_vertical"],
                order_by="creation desc",
                limit=1
            )

            if backend_student and backend_student[0].course_vertical:
                vertical_doc = frappe.get_doc("Course Verticals", backend_student[0].course_vertical)
                response_data["course_vertical"] = vertical_doc.name1 if hasattr(vertical_doc, 'name1') else None
                response_data["course_vertical_short"] = vertical_doc.name2 if hasattr(vertical_doc, 'name2') else None

        except Exception as e:
            frappe.log_error(f"Error fetching course vertical from Backend Students: {str(e)}", "Student Minimal Details API Error")

        # Get language details
        if student.language:
            try:
                language_doc = frappe.get_doc("TAP Language", student.language)
                response_data["language"] = language_doc.language_name if hasattr(language_doc, 'language_name') else None
            except Exception as e:
                frappe.log_error(f"Error fetching language details: {str(e)}", "Student Minimal Details API Error")

        # Process enrollment details (using latest enrollment)
        if latest_enrollment:
            # Get grade from latest enrollment
            response_data["grade"] = str(latest_enrollment.grade) if latest_enrollment.grade else None

            # Get batch details
            if latest_enrollment.batch:
                try:
                    batch_doc = frappe.get_doc("Batch", latest_enrollment.batch)
                    response_data["batch_id"] = batch_doc.batch_id if hasattr(batch_doc, 'batch_id') else None
                    response_data["batch_name"] = batch_doc.name1 if hasattr(batch_doc, 'name1') else None

                    batch_onboarding_records = frappe.get_all(
                        "Batch onboarding",
                        filters={"batch": latest_enrollment.batch},
                        fields=["batch_skeyword"],
                        limit=1
                    )

                    if batch_onboarding_records:
                        response_data["batch_skeyword"] = batch_onboarding_records[0].batch_skeyword

                except Exception as e:
                    frappe.log_error(f"Error fetching batch details: {str(e)}", "Student Minimal Details API Error")

            # Get course level details
            if latest_enrollment.course:
                try:
                    course_doc = frappe.get_doc("Course Level", latest_enrollment.course)
                    course_name = course_doc.name1 if hasattr(course_doc, 'name1') else None

                    # If course vertical not found from Backend Students, get it from enrollment
                    if not response_data["course_vertical"] and hasattr(course_doc, 'vertical') and course_doc.vertical:
                        try:
                            vertical_doc = frappe.get_doc("Course Verticals", course_doc.vertical)
                            response_data["course_vertical"] = vertical_doc.name1 if hasattr(vertical_doc, 'name1') else None
                            response_data["course_vertical_short"] = vertical_doc.name2 if hasattr(vertical_doc, 'name2') else None
                            course_name = vertical_doc.name1 if not course_name else course_name
                        except Exception as ve:
                            frappe.log_error(f"Error fetching vertical details: {str(ve)}", "Student Minimal Details API Error")

                    response_data["course_level"] = course_name
                except Exception as e:
                    frappe.log_error(f"Error fetching course details: {str(e)}", "Student Minimal Details API Error")

            # Get school details
            school_id = latest_enrollment.school if latest_enrollment.school else student.school_id

            if school_id:
                try:
                    school_doc = frappe.get_doc("School", school_id)
                    response_data["school"] = school_doc.name1 if hasattr(school_doc, 'name1') else None

                    if hasattr(school_doc, 'city') and school_doc.city:
                        try:
                            city_doc = frappe.get_doc("City", school_doc.city)
                            response_data["city"] = city_doc.city_name if hasattr(city_doc, 'city_name') else None

                            if hasattr(city_doc, 'district') and city_doc.district:
                                try:
                                    district_doc = frappe.get_doc("District", city_doc.district)
                                    response_data["district"] = district_doc.district_name if hasattr(district_doc, 'district_name') else None
                                except Exception as de:
                                    frappe.log_error(f"Error fetching district details: {str(de)}", "Student Minimal Details API Error")
                        except Exception as ce:
                            frappe.log_error(f"Error fetching city details: {str(ce)}", "Student Minimal Details API Error")
                except Exception as se:
                    frappe.log_error(f"Error fetching school details: {str(se)}", "Student Minimal Details API Error")
        else:
            # If no enrollment found, try to get basic school info from student record
            if student.school_id:
                try:
                    school_doc = frappe.get_doc("School", student.school_id)
                    response_data["school"] = school_doc.name1 if hasattr(school_doc, 'name1') else None
                    response_data["grade"] = str(student.grade) if student.grade else None

                    if hasattr(school_doc, 'city') and school_doc.city:
                        try:
                            city_doc = frappe.get_doc("City", school_doc.city)
                            response_data["city"] = city_doc.city_name if hasattr(city_doc, 'city_name') else None

                            if hasattr(city_doc, 'district') and city_doc.district:
                                try:
                                    district_doc = frappe.get_doc("District", city_doc.district)
                                    response_data["district"] = district_doc.district_name if hasattr(district_doc, 'district_name') else None
                                except Exception as de:
                                    frappe.log_error(f"Error fetching district details: {str(de)}", "Student Minimal Details API Error")
                        except Exception as ce:
                            frappe.log_error(f"Error fetching city details: {str(ce)}", "Student Minimal Details API Error")
                except Exception as se:
                    frappe.log_error(f"Error fetching school details: {str(se)}", "Student Minimal Details API Error")

        # Find all fields with null values
        null_fields = [field for field, value in response_data.items() if value is None]
        # Convert to comma-separated string or null if empty
        response_data["null_data"] = ", ".join(null_fields) if null_fields else None

        # Add warning if multiple students found
        if multiple_students_warning:
            response_data["_warning"] = multiple_students_warning

        # Add search parameters used
        response_data["_search_params"] = {
            "glific_id": glific_id,
            "phone": phone,
            "name": name,
            "phone_fallback_used": phone_search_fallback
        }

        return response_data

    except frappe.AuthenticationError as e:
        frappe.local.response.http_status_code = 401
        frappe.log_error(
            f"Authentication error: {str(e)}",
            "Student Minimal Details API Error"
        )
        return {"error": str(e)}

    except Exception as e:
        frappe.local.response.http_status_code = 500
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error getting student minimal details: {str(e)}\n{error_traceback}",
            "Student Minimal Details API Error"
        )
        return {"error": str(e)}




def find_appropriate_course_level(student, course_vertical_id, grade=None):
    """
    Find the appropriate Course Level for a student based on:
    - Grade Course Level Mapping (NEW - primary method)
    - Course Vertical
    - Student's grade
    - Student type (New/Old)
    - Academic Year
    - Batch constraints (fallback)
    
    Args:
        student: Student document
        course_vertical_id: Course Vertical ID (system ID)
        grade: Grade to check (if different from student's current grade)
        
    Returns:
        dict: {"found": bool, "course_level_id": str or None, "error": str or None}
    """
    try:
        # Determine which grade to check
        grade_to_check = grade or student.grade
        if not grade_to_check:
            return {
                "found": False,
                "course_level_id": None,
                "error": "Student has no grade specified"
            }
        
        # Convert grade to string for mapping lookup
        try:
            grade_str = str(grade_to_check)
        except (ValueError, TypeError):
            return {
                "found": False,
                "course_level_id": None,
                "error": f"Invalid grade format: {grade_to_check}"
            }
        
        # NEW: Try Grade Course Level Mapping first
        try:
            # Determine student type using phone + name combination
            student_type = determine_student_type_api(student.phone, student.name1, course_vertical_id)
            
            # Get current academic year
            academic_year = get_current_academic_year_api()
            
            frappe.log_error(
                f"API: Course level mapping lookup: vertical={course_vertical_id}, grade={grade_str}, type={student_type}, year={academic_year}",
                "API Course Level Mapping Lookup"
            )
            
            # Try manual mapping with current academic year
            if academic_year:
                mapping = frappe.get_all(
                    "Grade Course Level Mapping",
                    filters={
                        "academic_year": academic_year,
                        "course_vertical": course_vertical_id,
                        "grade": grade_str,
                        "student_type": student_type,
                        "is_active": 1
                    },
                    fields=["assigned_course_level", "mapping_name"],
                    order_by="modified desc",
                    limit=1
                )
                
                if mapping:
                    frappe.log_error(
                        f"API: Found mapping: {mapping[0].mapping_name} -> {mapping[0].assigned_course_level}",
                        "API Course Level Mapping Found"
                    )
                    return {
                        "found": True,
                        "course_level_id": mapping[0].assigned_course_level,
                        "course_level_name": None,  # Will be fetched separately if needed
                        "error": None,
                        "method": "grade_mapping_with_year"
                    }
            
            # Try mapping with academic_year = null (flexible mappings)
            mapping_null = frappe.get_all(
                "Grade Course Level Mapping",
                filters={
                    "academic_year": ["is", "not set"],
                    "course_vertical": course_vertical_id,
                    "grade": grade_str,
                    "student_type": student_type,
                    "is_active": 1
                },
                fields=["assigned_course_level", "mapping_name"],
                order_by="modified desc",
                limit=1
            )
            
            if mapping_null:
                frappe.log_error(
                    f"API: Found flexible mapping: {mapping_null[0].mapping_name} -> {mapping_null[0].assigned_course_level}",
                    "API Course Level Flexible Mapping Found"
                )
                return {
                    "found": True,
                    "course_level_id": mapping_null[0].assigned_course_level,
                    "course_level_name": None,
                    "error": None,
                    "method": "grade_mapping_flexible"
                }
            
            frappe.log_error(
                f"API: No mapping found for vertical={course_vertical_id}, grade={grade_str}, type={student_type}, year={academic_year}. Using Stage Grades fallback.",
                "API Course Level Mapping Fallback"
            )
            
        except Exception as mapping_error:
            frappe.log_error(f"API: Error in course level mapping: {str(mapping_error)}", "API Course Level Mapping Error")
            # Continue to fallback logic
        
        # FALLBACK: Original Stage Grades logic
        # Convert grade to integer for stage logic
        try:
            grade_int = int(grade_str)
        except (ValueError, TypeError):
            return {
                "found": False,
                "course_level_id": None,
                "error": f"Invalid grade format for fallback: {grade_str}"
            }
        
        # Get all course levels for this vertical
        course_levels = frappe.get_all(
            "Course Level",
            filters={"vertical": course_vertical_id},
            fields=["name", "name1", "stage", "kit_less"]
        )
        
        if not course_levels:
            return {
                "found": False,
                "course_level_id": None,
                "error": f"No course levels found for the selected vertical"
            }
        
        # Find the student's batch for kit_less logic
        latest_batch = None
        kitless = False
        
        if hasattr(student, 'enrollment') and student.enrollment:
            sorted_enrollments = sorted(
                student.enrollment,
                key=lambda x: x.date_joining if x.date_joining else frappe.utils.datetime.datetime.min,
                reverse=True
            )
            if sorted_enrollments:
                latest_batch = sorted_enrollments[0].batch
        
        if not latest_batch:
            # Try to find from Backend Students
            backend_student = frappe.get_all(
                "Backend Students",
                filters={"student_id": student.name, "processing_status": "Success"},
                fields=["batch"],
                order_by="creation desc",
                limit=1
            )
            if backend_student:
                latest_batch = backend_student[0].batch
        
        # Get kitless status from batch
        if latest_batch:
            try:
                batch_onboarding = frappe.get_all(
                    "Batch onboarding",
                    filters={"batch": latest_batch},
                    fields=["kit_less"],
                    limit=1
                )
                if batch_onboarding:
                    kitless = batch_onboarding[0].kit_less
            except Exception as e:
                frappe.log_error(f"Error getting kitless status: {str(e)}", "Course Level Selection Error")
        
        # Check each course level for grade compatibility using Stage Grades
        valid_course_levels = []
        
        for cl in course_levels:
            if cl.stage:
                # Get stage details
                try:
                    stage = frappe.get_doc("Stage Grades", cl.stage)
                    
                    from_grade = int(stage.from_grade) if stage.from_grade else 1
                    to_grade = int(stage.to_grade) if stage.to_grade else 12
                    
                    # Check if student's grade is within stage range
                    if from_grade <= grade_int <= to_grade:
                        # Apply kit_less filtering if needed
                        if kitless and hasattr(cl, 'kit_less') and not cl.kit_less:
                            continue  # Skip non-kitless courses for kitless batches
                        
                        valid_course_levels.append({
                            "id": cl.name,
                            "name": cl.name1,
                            "priority": abs(grade_int - from_grade)  # Closer to from_grade = higher priority
                        })
                        
                except Exception as stage_error:
                    frappe.log_error(f"Error processing stage {cl.stage}: {str(stage_error)}", "Course Level Selection Error")
                    continue
        
        if not valid_course_levels:
            return {
                "found": False,
                "course_level_id": None,
                "error": f"No course level found matching grade {grade_str} for the selected vertical (fallback method)"
            }
        
        # Sort by priority (closest match to grade)
        valid_course_levels.sort(key=lambda x: x["priority"])
        
        # Return the best match
        return {
            "found": True,
            "course_level_id": valid_course_levels[0]["id"],
            "course_level_name": valid_course_levels[0]["name"],
            "error": None,
            "method": "stage_grades_fallback"
        }
        
    except Exception as e:
        frappe.log_error(f"Error finding course level: {str(e)}", "Course Level Selection Error")
        return {
            "found": False,
            "course_level_id": None,
            "error": f"Error finding course level: {str(e)}"
        }

def determine_student_type_api(phone_number, student_name, course_vertical):
    """
    Determine if student is New or Old based on previous enrollment in same vertical
    Uses phone + name1 combination to uniquely identify the student
    (Same logic as backend version but for API context)
    
    Args:
        phone_number: Student's phone number
        student_name: Student's name (name1 field)
        course_vertical: Course vertical name/ID
    
    Returns:
        "Old" if student has previous enrollment in same vertical, "New" otherwise
    """
    try:
        existing_enrollment = frappe.db.sql("""
            SELECT s.name 
            FROM `tabStudent` s
            INNER JOIN `tabEnrollment` e ON e.parent = s.name  
            INNER JOIN `tabCourse Level` cl ON cl.name = e.course
            INNER JOIN `tabCourse Verticals` cv ON cv.name = cl.vertical
            WHERE s.phone = %s AND s.name1 = %s AND cv.name = %s
            LIMIT 1
        """, (phone_number, student_name, course_vertical))
        
        student_type = "Old" if existing_enrollment else "New"
        
        frappe.log_error(
            f"API: Student type determination: phone={phone_number}, name={student_name}, vertical={course_vertical}, type={student_type}",
            "API Student Type Classification"
        )
        
        return student_type
        
    except Exception as e:
        frappe.log_error(f"API: Error determining student type: {str(e)}", "API Student Type Error")
        return "New"  # Default to New on error

def get_current_academic_year_api():
    """
    Get current academic year based on current date
    Academic year runs from April to March
    (Same logic as backend version)
    
    Returns:
        Academic year string in format "YYYY-YY" (e.g., "2025-26")
    """
    try:
        current_date = frappe.utils.getdate()
        
        if current_date.month >= 4:  # April onwards = new academic year
            academic_year = f"{current_date.year}-{str(current_date.year + 1)[-2:]}"
        else:
            academic_year = f"{current_date.year - 1}-{str(current_date.year)[-2:]}"
        
        frappe.log_error(f"API: Current academic year determined: {academic_year}", "API Academic Year Calculation")
        
        return academic_year
        
    except Exception as e:
        frappe.log_error(f"API: Error calculating academic year: {str(e)}", "API Academic Year Error")
        return None

@frappe.whitelist(allow_guest=False)
def update_student_fields(**_kw):
    """[DEPRECATED 2026-05-28] Use summer_program/dev_tools.update_student_state for QA-driven state changes, or the canonical state-machine transitions for production."""
    return journey_deprecated_response("update_student_fields", _kw)


def _DEPRECATED_update_student_fields_original(student_id=None, glific_id=None, phone=None, name=None, updates=None):
    """
    Update specific fields for a student and their latest enrollment
    
    Args:
        student_id (str, optional): Student ID (name field)
        glific_id (str, optional): Glific ID of student
        phone (str, optional): Phone number of student
        name (str, optional): Student name to help identify unique student
        updates (dict): Dictionary of fields to update with their new values
                       Allowed fields: gender, grade, course_level (vertical name2), language (language_name)
        
    Returns:
        dict: Success status with updated fields information
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
        
        # Validate updates parameter
        if not updates or not isinstance(updates, dict):
            frappe.local.response.http_status_code = 400
            return {
                "success": False,
                "error": "Updates parameter must be a non-empty dictionary"
            }
        
        # Parse updates if it comes as string (from API)
        if isinstance(updates, str):
            try:
                updates = json.loads(updates)
            except json.JSONDecodeError:
                frappe.local.response.http_status_code = 400
                return {
                    "success": False,
                    "error": "Invalid JSON format in updates parameter"
                }
        
        # Define allowed fields for update
        allowed_fields = ["gender", "grade", "course_vertical", "language"]
        
        # Validate that only allowed fields are being updated
        invalid_fields = [field for field in updates.keys() if field not in allowed_fields]
        if invalid_fields:
            frappe.local.response.http_status_code = 400
            return {
                "success": False,
                "error": f"Invalid fields for update: {', '.join(invalid_fields)}. Allowed fields: {', '.join(allowed_fields)}"
            }
        
        # Find the student (same logic as before)
        student = None
        student_records = []
        
        # [Previous student finding logic remains the same...]
        # If student_id is provided, use it directly
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
            
            # Phone fallback logic
            if not student_records and phone and str(phone).strip().startswith('91') and len(str(phone).strip()) == 12:
                phone_without_prefix = str(phone).strip()[2:]
                filters["phone"] = phone_without_prefix
                
                student_records = frappe.get_all(
                    "Student",
                    filters=filters,
                    fields=["name", "name1", "phone", "glific_id", "creation"],
                    order_by="creation desc"
                )
            
            # Name filtering logic
            if name and len(student_records) > 1:
                normalized_name = str(name).strip().lower()
                exact_matches = [s for s in student_records if s.name1 and s.name1.strip().lower() == normalized_name]
                
                if not exact_matches:
                    partial_matches = [s for s in student_records if s.name1 and normalized_name in s.name1.strip().lower()]
                    if partial_matches:
                        student_records = partial_matches
                else:
                    student_records = exact_matches
        
        if not student_records:
            frappe.local.response.http_status_code = 404
            return {
                "success": False,
                "error": "Student not found"
            }
        
        # Get the student document
        student = frappe.get_doc("Student", student_records[0].name)
        
        # Track what was updated
        updated_fields = {
            "student_level": {},
            "enrollment_level": {}
        }
        
        # Handle grade update FIRST (as it affects course level selection)
        if "grade" in updates:
            valid_grades = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"]
            grade_str = str(updates["grade"])
            if grade_str not in valid_grades:
                return {
                    "success": False,
                    "error": f"Invalid grade value. Allowed: {', '.join(valid_grades)}"
                }
            
            # Update student-level grade
            student.grade = grade_str
            updated_fields["student_level"]["grade"] = grade_str
            
            # Update latest enrollment grade if exists
            if hasattr(student, 'enrollment') and student.enrollment:
                sorted_enrollments = sorted(
                    student.enrollment,
                    key=lambda x: x.date_joining if x.date_joining else frappe.utils.datetime.datetime.min,
                    reverse=True
                )
                if sorted_enrollments:
                    latest_enrollment = sorted_enrollments[0]
                    latest_enrollment.grade = grade_str
                    updated_fields["enrollment_level"]["grade"] = grade_str
        
        # Update student-level fields
        if "gender" in updates:
            valid_genders = ["Male", "Female", "Others", "Not Available"]
            if updates["gender"] not in valid_genders:
                return {
                    "success": False,
                    "error": f"Invalid gender value. Allowed: {', '.join(valid_genders)}"
                }
            student.gender = updates["gender"]
            updated_fields["student_level"]["gender"] = updates["gender"]
        
        # Handle language update with language_name lookup
        if "language" in updates and updates["language"]:
            # Find TAP Language by language_name
            language_records = frappe.get_all(
                "TAP Language",
                filters={"language_name": updates["language"]},
                fields=["name", "language_name"]
            )
            
            if not language_records:
                return {
                    "success": False,
                    "error": f"Language '{updates['language']}' not found in TAP Language"
                }
            
            student.language = language_records[0].name
            updated_fields["student_level"]["language"] = updates["language"]  # Store the friendly name
        
        # Handle course_vertical update (now properly named)
        if "course_vertical" in updates and updates["course_vertical"]:
            # Find Course Vertical by name2
            vertical_records = frappe.get_all(
                "Course Verticals",
                filters={"name2": updates["course_vertical"]},
                fields=["name", "name1", "name2"]
            )
            
            if not vertical_records:
                return {
                    "success": False,
                    "error": f"Course Vertical '{updates['course_vertical']}' not found"
                }
            
            course_vertical_id = vertical_records[0].name
            
            # Get the current grade (might have been updated above)
            current_grade = student.grade
            
            # Find appropriate course level
            course_level_result = find_appropriate_course_level(student, course_vertical_id, current_grade)
            
            if not course_level_result["found"]:
                return {
                    "success": False,
                    "error": course_level_result["error"]
                }
            
            # Update latest enrollment with the found course level
            if hasattr(student, 'enrollment') and student.enrollment:
                sorted_enrollments = sorted(
                    student.enrollment,
                    key=lambda x: x.date_joining if x.date_joining else frappe.utils.datetime.datetime.min,
                    reverse=True
                )
                if sorted_enrollments:
                    latest_enrollment = sorted_enrollments[0]
                    latest_enrollment.course = course_level_result["course_level_id"]
                    updated_fields["enrollment_level"]["course"] = course_level_result["course_level_id"]
                    updated_fields["enrollment_level"]["course_name"] = course_level_result.get("course_level_name")
                    updated_fields["enrollment_level"]["course_vertical"] = updates["course_vertical"]
            else:
                frappe.log_error(
                    f"No enrollment found for student {student.name} to update course_level",
                    "Update API - No Enrollment"
                )
                return {
                    "success": False,
                    "error": "Student has no enrollment to update course level"
                }
        
        # Save the student document
        student.save()
        
        # Prepare simple flat response
        response = {
            "success": True,
            "message": "Student fields updated successfully",
            "student_id": student.name
        }
        
        # Add current values for all updatable fields
        # Gender
        response["gender"] = student.gender
        
        # Grade
        response["grade"] = student.grade
        
        # Language
        if student.language:
            try:
                language_doc = frappe.get_doc("TAP Language", student.language)
                response["language"] = language_doc.language_name if hasattr(language_doc, 'language_name') else None
            except:
                response["language"] = None
        else:
            response["language"] = None
        
        # Course Level and Vertical
        if hasattr(student, 'enrollment') and student.enrollment:
            sorted_enrollments = sorted(
                student.enrollment,
                key=lambda x: x.date_joining if x.date_joining else frappe.utils.datetime.datetime.min,
                reverse=True
            )
            if sorted_enrollments and sorted_enrollments[0].course:
                try:
                    course_doc = frappe.get_doc("Course Level", sorted_enrollments[0].course)
                    response["course_level"] = course_doc.name
                    response["course_level_name"] = course_doc.name1 if hasattr(course_doc, 'name1') else None
                    
                    # Get course vertical
                    if hasattr(course_doc, 'vertical') and course_doc.vertical:
                        vertical_doc = frappe.get_doc("Course Verticals", course_doc.vertical)
                        response["course_vertical"] = vertical_doc.name2 if hasattr(vertical_doc, 'name2') else None
                    else:
                        response["course_vertical"] = None
                except:
                    response["course_level"] = None
                    response["course_level_name"] = None
                    response["course_vertical"] = None
            else:
                response["course_level"] = None
                response["course_level_name"] = None
                response["course_vertical"] = None
        else:
            response["course_level"] = None
            response["course_level_name"] = None
            response["course_vertical"] = None
        
        # Add warning if multiple students were found
        if len(student_records) > 1:
            response["_warning"] = f"Multiple students found. Updated the most recently created one. Count: {len(student_records)}"
        
        return response
        
    except frappe.ValidationError as e:
        frappe.local.response.http_status_code = 400
        frappe.log_error(
            f"Validation error: {str(e)}",
            "Student Update API Validation Error"
        )
        return {
            "success": False,
            "error": str(e)
        }
    
    except frappe.AuthenticationError as e:
        frappe.local.response.http_status_code = 401
        frappe.log_error(
            f"Authentication error: {str(e)}",
            "Student Update API Error"
        )
        return {
            "success": False,
            "error": str(e)
        }
    
    except Exception as e:
        frappe.local.response.http_status_code = 500
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error updating student fields: {str(e)}\n{error_traceback}",
            "Student Update API Error"
        )
        return {
            "success": False,
            "error": str(e)
        }


@frappe.whitelist(allow_guest=False)
def get_siblings(**_kw):
    """[DEPRECATED 2026-05-28] No SP equivalent — sibling handling is now upstream in PE enrollment per SP design."""
    return journey_deprecated_response("get_siblings", _kw)


def _DEPRECATED_get_siblings_original(phone, glific_id=None):
    """
    Check for siblings (students with same phone number)
    
    Args:
        phone (str): Phone number (required)
        glific_id (str, optional): Glific ID for additional filtering
        
    Returns:
        dict: Siblings information with profile details
    """
    try:
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)
        
        # Validate phone parameter
        if not phone:
            frappe.local.response.http_status_code = 400
            return {"success": False, "error": "Phone number is required"}
        
        # Format phone number using existing logic
        formatted_phone = format_phone_number(phone)
        
        # Build filters
        filters = {"phone": formatted_phone}
        if glific_id:
            filters["glific_id"] = glific_id
        
        # Find all students with the same phone number
        student_records = frappe.get_all(
            "Student",
            filters=filters,
            fields=["name", "name1", "phone", "glific_id", "gender", "grade", "creation"],
            order_by="creation desc"
        )
        
        # If no records found, try phone without 91 prefix (fallback logic)
        if not student_records and formatted_phone and formatted_phone.startswith('91') and len(formatted_phone) == 12:
            phone_without_prefix = formatted_phone[2:]
            filters["phone"] = phone_without_prefix
            
            student_records = frappe.get_all(
                "Student",
                filters=filters,
                fields=["name", "name1", "phone", "glific_id", "gender", "grade", "creation"],
                order_by="creation desc"
            )
        
        if not student_records:
            frappe.local.response.http_status_code = 404
            return {"success": False, "error": "No students found with the provided phone number"}
        
        # Determine if multiple profiles exist
        profile_count = len(student_records)
        multiple_profiles = "Yes" if profile_count > 1 else "No"
        
        # Build profile details
        profile_details = {}
        
        for i, student_record in enumerate(student_records, 1):
            try:
                # Get full student document
                student = frappe.get_doc("Student", student_record.name)
                
                # Initialize profile data
                profile_data = {
                    "student_id": student.name,
                    "name": student.name1 or None,
                    "course": None,
                    "grade": str(student.grade) if student.grade else None
                }
                
                # Get course from latest enrollment
                course_name = None
                enrollment_grade = None
                
                if hasattr(student, 'enrollment') and student.enrollment:
                    # Get latest enrollment
                    sorted_enrollments = sorted(
                        student.enrollment,
                        key=lambda x: x.date_joining if x.date_joining else frappe.utils.datetime.datetime.min,
                        reverse=True
                    )
                    
                    if sorted_enrollments:
                        latest_enrollment = sorted_enrollments[0]
                        enrollment_grade = str(latest_enrollment.grade) if latest_enrollment.grade else None
                        
                        # Get course vertical name2 from course level
                        if latest_enrollment.course:
                            try:
                                course_doc = frappe.get_doc("Course Level", latest_enrollment.course)
                                if hasattr(course_doc, 'vertical') and course_doc.vertical:
                                    vertical_doc = frappe.get_doc("Course Verticals", course_doc.vertical)
                                    course_name = vertical_doc.name2 if hasattr(vertical_doc, 'name2') else None
                            except Exception as e:
                                frappe.log_error(f"Error fetching course details for student {student.name}: {str(e)}", "Siblings API Error")
                
                # Use enrollment grade if available, otherwise use student grade
                if enrollment_grade:
                    profile_data["grade"] = enrollment_grade
                
                # Set course name
                profile_data["course"] = course_name
                
                # Add to profile details with string key
                profile_details[str(i)] = profile_data
                
            except Exception as e:
                frappe.log_error(f"Error processing student {student_record.name}: {str(e)}", "Siblings API Error")
                # Add minimal profile data in case of error
                profile_details[str(i)] = {
                    "student_id": student_record.name,
                    "name": student_record.name1 or None,
                    "course": None,
                    "grade": str(student_record.grade) if student_record.grade else None
                }
        
        # Build response
        response = {
            "multiple_profiles": multiple_profiles,
            "count": str(profile_count),
            "profile_details": profile_details
        }
        
        return response
        
    except frappe.AuthenticationError as e:
        frappe.local.response.http_status_code = 401
        frappe.log_error(
            f"Authentication error: {str(e)}",
            "Siblings API Error"
        )
        return {"success": False, "error": str(e)}
    
    except Exception as e:
        frappe.local.response.http_status_code = 500
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error getting siblings: {str(e)}\n{error_traceback}",
            "Siblings API Error"
        )
        return {"success": False, "error": str(e)}



@frappe.whitelist(allow_guest=False)
def check_student(**_kw):
    """[DEPRECATED 2026-05-28] Use GET `/api/resource/Student?filters=[[\"phone\",\"=\",\"<phone>\"]]`."""
    return journey_deprecated_response("check_student", _kw)


def _DEPRECATED_check_student_original(phone):
    """
    Check if a student exists in the system and identify siblings.
    
    Args:
        phone (str): Phone number (10 or 12 digits with 91 prefix)
        
    Returns:
        dict: Student type classification and details
            - student_type: "new" | "single" | "multiple"
            - count: Number of students found (as string)
            - has_siblings: "Yes" | "No"
            - "1", "2", etc.: Student details (flat structure for Glific)
    """
    try:
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)
        
        # Validate phone parameter
        if not phone:
            frappe.local.response.http_status_code = 400
            return {"success": "No", "error": "Phone number is required"}
        
        # Format phone number
        formatted_phone = format_phone_number(phone)
        
        # Find all students with the same phone number
        student_records = frappe.get_all(
            "Student",
            filters={"phone": formatted_phone},
            fields=["name", "name1", "phone", "gender", "grade", "language", "creation"],
            order_by="creation desc"
        )
        
        # Fallback: If no records found with 91 prefix, try without it
        if not student_records and formatted_phone and formatted_phone.startswith('91') and len(formatted_phone) == 12:
            phone_without_prefix = formatted_phone[2:]
            student_records = frappe.get_all(
                "Student",
                filters={"phone": phone_without_prefix},
                fields=["name", "name1", "phone", "gender", "grade", "language", "creation"],
                order_by="creation desc"
            )
        
        # Determine student type
        count = len(student_records)
        
        if count == 0:
            student_type = "new"
        elif count == 1:
            student_type = "single"
        else:
            student_type = "multiple"
        
        has_siblings = "Yes" if count > 1 else "No"
        
        # Build response
        response = {
            "student_type": student_type,
            "count": str(count),
            "has_siblings": has_siblings
        }
        
        # Add student details directly under response (flat structure for Glific)
        for i, student_record in enumerate(student_records, 1):
            try:
                student_data = _get_student_details_flat(student_record)
                response[str(i)] = student_data
            except Exception as e:
                frappe.log_error(
                    f"Error processing student {student_record.name}: {str(e)}", 
                    "Check Student API Error"
                )
                # Add minimal data on error
                response[str(i)] = {
                    "student_id": student_record.name,
                    "name": student_record.name1 or None,
                    "gender": None,
                    "grade": None,
                    "language": None,
                    "course_vertical": None,
                    "course_vertical_short": None,
                    "course_level": None,
                    "batch": None,
                    "batch_active": None
                }
        
        return response
        
    except frappe.AuthenticationError as e:
        frappe.local.response.http_status_code = 401
        frappe.log_error(f"Authentication error: {str(e)}", "Check Student API Error")
        return {"success": "No", "error": str(e)}
    
    except Exception as e:
        frappe.local.response.http_status_code = 500
        error_traceback = traceback.format_exc()
        frappe.log_error(
            f"Error checking student: {str(e)}\n{error_traceback}",
            "Check Student API Error"
        )
        return {"success": "No", "error": "Internal server error"}


def _get_student_details_flat(student_record):
    """
    Get flat student details for Glific compatibility (max 2 levels).
    
    Args:
        student_record: Student record from frappe.get_all
        
    Returns:
        dict: Flat student details with all string values
    """
    # Get full student document
    student = frappe.get_doc("Student", student_record.name)
    
    # Initialize response with basic fields
    student_data = {
        "student_id": student.name,
        "name": student.name1 or None,
        "gender": student.gender or None,
        "grade": str(student.grade) if student.grade else None,
        "language": None,
        "course_vertical": None,
        "course_vertical_short": None,
        "course_level": None,
        "batch": None,
        "batch_active": None
    }
    
    # Get language name
    if student.language:
        try:
            language_doc = frappe.get_doc("TAP Language", student.language)
            student_data["language"] = language_doc.language_name if hasattr(language_doc, 'language_name') else None
        except Exception as e:
            frappe.log_error(f"Error fetching language: {str(e)}", "Check Student API Error")
    
    # Get latest enrollment details
    latest_enrollment = _get_latest_enrollment(student)
    
    if latest_enrollment:
        # Update grade from enrollment if available
        if latest_enrollment.grade:
            student_data["grade"] = str(latest_enrollment.grade)
        
        # Get batch details
        if latest_enrollment.batch:
            try:
                batch_doc = frappe.get_doc("Batch", latest_enrollment.batch)
                student_data["batch"] = batch_doc.name1 if hasattr(batch_doc, 'name1') else None
                
                # Check if batch is active
                batch_active = batch_doc.active if hasattr(batch_doc, 'active') else False
                student_data["batch_active"] = "Yes" if batch_active else "No"
            except Exception as e:
                frappe.log_error(f"Error fetching batch: {str(e)}", "Check Student API Error")
        
        # Get course level and vertical details
        if latest_enrollment.course:
            try:
                course_doc = frappe.get_doc("Course Level", latest_enrollment.course)
                student_data["course_level"] = course_doc.name1 if hasattr(course_doc, 'name1') else None
                
                # Get course vertical
                if hasattr(course_doc, 'vertical') and course_doc.vertical:
                    try:
                        vertical_doc = frappe.get_doc("Course Verticals", course_doc.vertical)
                        student_data["course_vertical"] = vertical_doc.name1 if hasattr(vertical_doc, 'name1') else None
                        student_data["course_vertical_short"] = vertical_doc.name2 if hasattr(vertical_doc, 'name2') else None
                    except Exception as e:
                        frappe.log_error(f"Error fetching vertical: {str(e)}", "Check Student API Error")
            except Exception as e:
                frappe.log_error(f"Error fetching course level: {str(e)}", "Check Student API Error")
    
    return student_data


def _get_latest_enrollment(student):
    """
    Get the latest enrollment for a student based on date_joining.
    
    Args:
        student: Student document
        
    Returns:
        Enrollment child record or None
    """
    if not hasattr(student, 'enrollment') or not student.enrollment:
        return None
    
    latest_enrollment = None
    
    for enrollment in student.enrollment:
        if not latest_enrollment:
            latest_enrollment = enrollment
        elif enrollment.date_joining and latest_enrollment.date_joining:
            if enrollment.date_joining > latest_enrollment.date_joining:
                latest_enrollment = enrollment
        elif enrollment.date_joining and not latest_enrollment.date_joining:
            # Prefer enrollment with date_joining
            latest_enrollment = enrollment
    
    return latest_enrollment
