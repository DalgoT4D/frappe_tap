import frappe
import json
from frappe.utils import cint, today, get_url, now_datetime, getdate, cstr, get_datetime
from datetime import datetime, timedelta
import requests
import random
import string
import urllib.parse
from .glific_integration import create_contact, start_contact_flow, get_contact_by_phone, update_contact_fields, add_contact_to_group, create_or_get_teacher_group_for_batch
from .background_jobs import enqueue_glific_actions



def authenticate_api_key(api_key):
    try:
        # Check if the provided API key exists and is enabled
        api_key_doc = frappe.get_doc("API Key", {"key": api_key, "enabled": 1})
        return api_key_doc.name
    except frappe.DoesNotExistError:
        # Handle the case where the API key does not exist or is not enabled
        return None



def canonicalize_response_phone(phone):
    if not phone:
        return phone

    phone_12, _phone_10 = normalize_phone_number(phone)
    return phone_12 or str(phone).strip()


def get_active_batch_for_school(school_id):
    today = frappe.utils.today()

    # Find active batch onboardings for this school
    active_batch_onboardings = frappe.get_all(
        "Batch onboarding",
        filters={
            "school": school_id,
            "batch": ["in", frappe.get_all("Batch",
                filters={"start_date": ["<=", today],
                         "end_date": [">=", today],
                         "active": 1},
                pluck="name")]
        },
        fields=["batch"],
        order_by="creation desc"
    )

    if active_batch_onboardings:
        # Return both batch name and batch_id
        batch_name = active_batch_onboardings[0].batch
        batch_id = frappe.db.get_value("Batch", batch_name, "batch_id")
        return {
            "batch_name": batch_name,
            "batch_id": batch_id
        }

    frappe.logger().error(f"No active batch found for school {school_id}")
    return {
        "batch_name": None,
        "batch_id": "no_active_batch_id"
    }





@frappe.whitelist(allow_guest=True)
def list_districts():
    try:
        # Get the JSON data from the request body
        data = json.loads(frappe.request.data)
        api_key = data.get('api_key')
        state = data.get('state')

        if not api_key or not state:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "API key and state are required"}

        if not authenticate_api_key(api_key):
            frappe.response.http_status_code = 401
            return {"status": "error", "message": "Invalid API key"}

        districts = frappe.get_all(
            "District",
            filters={"state": state},
            fields=["name", "district_name"]
        )

        response_data = {district.name: district.district_name for district in districts}

        frappe.response.http_status_code = 200
        return {"status": "success", "data": response_data}

    except Exception as e:
        frappe.log_error(f"List Districts Error: {str(e)}")
        frappe.response.http_status_code = 500
        return {"status": "error", "message": str(e)}

@frappe.whitelist(allow_guest=True)
def list_cities():
    try:
        # Get the JSON data from the request body
        data = json.loads(frappe.request.data)
        api_key = data.get('api_key')
        district = data.get('district')

        if not api_key or not district:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "API key and district are required"}

        if not authenticate_api_key(api_key):
            frappe.response.http_status_code = 401
            return {"status": "error", "message": "Invalid API key"}

        cities = frappe.get_all(
            "City",
            filters={"district": district},
            fields=["name", "city_name"]
        )

        response_data = {city.name: city.city_name for city in cities}

        frappe.response.http_status_code = 200
        return {"status": "success", "data": response_data}

    except Exception as e:
        frappe.log_error(f"List Cities Error: {str(e)}")
        frappe.response.http_status_code = 500
        return {"status": "error", "message": str(e)}



def send_whatsapp_message(phone_number, message):
    # Fetch Gupshup OTP Settings
    gupshup_settings = frappe.get_single("Gupshup OTP Settings")

    if not gupshup_settings:
        frappe.log_error("Gupshup OTP Settings not found")
        return False

    if not all([gupshup_settings.api_key, gupshup_settings.source_number,
                gupshup_settings.app_name, gupshup_settings.api_endpoint]):
        frappe.log_error("Incomplete Gupshup OTP Settings")
        return False

    url = gupshup_settings.api_endpoint

    payload = {
        "channel": "whatsapp",
        "source": gupshup_settings.source_number,
        "destination": phone_number,
        "message": json.dumps({"type": "text", "text": message}),
        "src.name": gupshup_settings.app_name
    }

    headers = {
        "apikey": gupshup_settings.api_key,
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache"
    }

    try:
        response = requests.post(url, data=payload, headers=headers)
        response.raise_for_status()  # Raise an exception for non-200 status codes
        return True
    except requests.exceptions.RequestException as e:
        frappe.log_error(f"Error sending WhatsApp message: {str(e)}")
        return False






@frappe.whitelist(allow_guest=True)
def get_school_name_keyword_list(api_key, start=0, limit=10):
    # Verify the API key
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")

    start = cint(start)
    limit = cint(limit)

    # Query the school doctype to fetch the name1 and keyword fields
    schools = frappe.db.get_all("School",
                                fields=["name", "name1", "keyword"],
                                limit_start=start,
                                limit_page_length=limit)

    # Fixed WhatsApp number
    whatsapp_number = "918454812392"

    # Prepare the response data
    response_data = []
    for school in schools:
        # Prepend "tapschool:" to the keyword
        keyword_with_prefix = f"tapschool:{school.keyword}"

        # Create the WhatsApp link using the fixed number and keyword
        whatsapp_link = f"https://api.whatsapp.com/send?phone={whatsapp_number}&text={keyword_with_prefix}"

        school_data = {
            "school_name": school.name1,
            "teacher_keyword": keyword_with_prefix,
            "whatsapp_link": whatsapp_link
        }
        response_data.append(school_data)

    # Return the response as a JSON object
    return response_data



@frappe.whitelist(allow_guest=True)
def verify_keyword():
    # Parse the request data
    data = frappe.request.get_json()

    # Verify the API key
    if not data or 'api_key' not in data or not authenticate_api_key(data['api_key']):
        frappe.response.http_status_code = 401
        frappe.response.update({
            "status": "failure",
            "school_name": None,
            "model": None,
            "error": "Invalid API key"
        })
        return

    if 'keyword' not in data:
        frappe.response.http_status_code = 400
        frappe.response.update({
            "status": "failure",
            "school_name": None,
            "model": None,
            "error": "Keyword parameter is missing"
        })
        return

    keyword = data['keyword']

    # Check if the keyword exists in the School doctype and retrieve the smodel and name1 fields
    school = frappe.db.get_value("School", {"keyword": keyword}, ["name1", "model"], as_dict=True)

    if school:
        frappe.response.http_status_code = 200
        frappe.response.update({
            "status": "success",
            "school_name": school.name1,
            "model": school.model
        })
    else:
        frappe.response.http_status_code = 404
        frappe.response.update({
            "status": "failure",
            "school_name": None,
            "model": None
        })



@frappe.whitelist(allow_guest=True)
def create_teacher(api_key, keyword, first_name, phone_number, glific_id, last_name=None, email=None, language=None):
    try:
        # Verify the API key
        if not authenticate_api_key(api_key):
            frappe.throw("Invalid API key")

        # Find the school based on the provided keyword
        school = frappe.db.get_value("School", {"keyword": keyword}, "name")
        if not school:
            return {
                "error": f"No school found with the keyword: {keyword}"
            }

        # Create a new teacher document
        teacher = frappe.new_doc("Teacher")
        teacher.first_name = first_name
        teacher.school = school
        teacher.phone_number = phone_number
        teacher.glific_id = glific_id  # Set the glific_id field (mandatory)

        # Set the optional fields if provided
        if last_name:
            teacher.last_name = last_name
        if email:
            teacher.email = email
        if language:
            teacher.language = language

        # Insert the teacher document
        teacher.insert(ignore_permissions=True)

        # Commit the changes
        frappe.db.commit()

        return {
            "message": "Teacher created successfully",
            "teacher_id": teacher.name
        }
    except frappe.DuplicateEntryError:
        return {
            "error": "Teacher with the same phone number already exists"
        }
    except Exception as e:
        return {
            "error": f"An error occurred while creating teacher: {str(e)}"
        }




@frappe.whitelist(allow_guest=True)
def list_batch_keyword(api_key):
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")

    current_date = getdate(today())
    whatsapp_number = "918454812392"
    response_data = []

    # Get all batch onboarding entries
    batch_onboarding_list = frappe.get_all(
        "Batch onboarding",
        fields=["batch", "school", "batch_skeyword"]
    )

    for onboarding in batch_onboarding_list:
        batch = frappe.get_doc("Batch", onboarding.batch)
        
        # Check if the batch is active and registration end date is in the future
        if batch.active and getdate(batch.regist_end_date) >= current_date:
            school_name = frappe.get_value("School", onboarding.school, "name1")
            keyword_with_prefix = f"tapschool:{onboarding.batch_skeyword}"
            batch_reg_link = f"https://api.whatsapp.com/send?phone={whatsapp_number}&text={keyword_with_prefix}"

            response_data.append({
                "School_name": school_name,
                "batch_keyword": onboarding.batch_skeyword,
                "batch_id": batch.batch_id,
                "Batch_regLink": batch_reg_link
            })

    return response_data





@frappe.whitelist(allow_guest=True)
@frappe.whitelist(allow_guest=True)
def create_student():
    try:
        # Get the data from the request
        api_key = frappe.form_dict.get('api_key')
        student_name = frappe.form_dict.get('student_name')
        phone = frappe.form_dict.get('phone')
        gender = frappe.form_dict.get('gender')
        grade = frappe.form_dict.get('grade')
        language_name = frappe.form_dict.get('language')
        batch_skeyword = frappe.form_dict.get('batch_skeyword')
        vertical = frappe.form_dict.get('vertical')
        glific_id = frappe.form_dict.get('glific_id')

        if not authenticate_api_key(api_key):
            frappe.response.status_code = 202
            return {"status": "error", "message": "Invalid API key"}

        # Validate required fields
        if not all([student_name, phone, gender, grade, language_name, batch_skeyword, vertical, glific_id]):
            frappe.response.status_code = 202
            return {"status": "error", "message": "All fields are required"}

        # Get the school and batch from batch_skeyword
        batch_onboarding = frappe.get_all(
            "Batch onboarding",
            filters={"batch_skeyword": batch_skeyword},
            fields=["name", "school", "batch", "kit_less"]
        )

        if not batch_onboarding:
            frappe.response.status_code = 202
            return {"status": "error", "message": "Invalid batch_skeyword"}

        school_id = batch_onboarding[0].school
        batch = batch_onboarding[0].batch
        kitless = batch_onboarding[0].kit_less

        # Check if the batch is active and registration end date is not passed
        batch_doc = frappe.get_doc("Batch", batch)
        current_date = getdate()

        if not batch_doc.active:
            frappe.response.status_code = 202
            return {"status": "error", "message": "The batch is not active"}

        if batch_doc.regist_end_date:
            try:
                regist_end_date = getdate(cstr(batch_doc.regist_end_date))
                if regist_end_date < current_date:
                    frappe.response.status_code = 202
                    return {"status": "error", "message": "Registration for this batch has ended"}
            except Exception as e:
                # Simple print for debugging, no frappe.log_error
                print(f"Error parsing registration end date: {str(e)}")
                frappe.response.status_code = 202
                return {"status": "error", "message": "Invalid registration end date format"}

        # Get the course vertical using the label
        course_vertical = frappe.get_all(
            "Course Verticals",
            filters={"name2": vertical},
            fields=["name"]
        )

        if not course_vertical:
            frappe.response.status_code = 202
            return {"status": "error", "message": "Invalid vertical label"}

        # Check if student with glific_id already exists
        existing_student = frappe.get_all(
            "Student",
            filters={"glific_id": glific_id},
            fields=["name", "name1", "phone"]
        )

        if existing_student:
            student = frappe.get_doc("Student", existing_student[0].name)
            
            # Check if name and phone match
            if student.name1 == student_name and student.phone == phone:
                # Update existing student
                student.grade = grade
                student.language = get_tap_language(language_name)
                student.school_id = school_id
                student.save(ignore_permissions=True)
            else:
                # Create new student
                student = create_new_student(student_name, phone, gender, school_id, grade, language_name, glific_id)
        else:
            # Create new student
            student = create_new_student(student_name, phone, gender, school_id, grade, language_name, glific_id)

        # Get the appropriate course level using new mapping-based logic
        try:
            course_level = get_course_level_with_mapping(
                course_vertical[0].name,
                grade,
                phone,        # Phone number
                student_name, # Student name for unique identification
                kitless       # For fallback logic
            )
            
            # REMOVED: Problematic logging - use print for debugging if needed
            # print(f"DEBUG: Course level selected: {course_level} for student {student_name}")
            
        except Exception as course_error:
            # REMOVED: Problematic logging - use print for debugging if needed  
            # print(f"DEBUG: Course level selection failed: {str(course_error)}")
            frappe.response.status_code = 202
            return {"status": "error", "message": f"Course selection failed: {str(course_error)}"}

        # Adding the enrollment details to the student
        student.append("enrollment", {
            "batch": batch,
            "level": frappe.db.get_value("Course Level", course_level, "level") if course_level else "",
            "grade": grade,
            "date_joining": now_datetime().date(),
            "school": school_id
        })

        student.save(ignore_permissions=True)

        return {
            "status": "success",
            "crm_student_id": student.name,
            "assigned_course_level": course_level
        }

    except frappe.ValidationError as e:
        # REMOVED: Problematic logging - use print for debugging if needed
        # print(f"DEBUG: Student Creation Validation Error: {str(e)}")
        frappe.response.status_code = 202
        return {"status": "error", "message": str(e)}
    except Exception as e:
        # REMOVED: Problematic logging - use print for debugging if needed
        # print(f"DEBUG: Student Creation Error: {str(e)}")
        frappe.response.status_code = 202
        return {"status": "error", "message": str(e)}


# Updated helper functions with cleaned logging

def determine_student_type(phone_number, student_name, course_vertical):
    """
    Determine if student is New or Old based on previous enrollment in same vertical
    Uses phone + name1 combination to uniquely identify the student
    
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
            INNER JOIN `tabStudent Enrollment` e ON e.parent = s.name  
            INNER JOIN `tabCourse Level` cl ON cl.name = e.course
            INNER JOIN `tabCourse Verticals` cv ON cv.name = cl.vertical
            WHERE s.phone = %s AND s.name1 = %s AND cv.name = %s
            LIMIT 1
        """, (phone_number, student_name, course_vertical))
        
        student_type = "Old" if existing_enrollment else "New"
        
        # REMOVED: Problematic logging - use print for debugging if needed
        # print(f"DEBUG: Student type: {student_type} for {student_name}")
        
        return student_type
        
    except Exception as e:
        # Simple print for debugging instead of frappe.log_error
        print(f"Error determining student type: {str(e)}")
        return "New"  # Default to New on error


def get_current_academic_year():
    """
    Get current academic year based on current date
    Academic year runs from April to March
    
    Returns:
        Academic year string in format "YYYY-YY" (e.g., "2025-26")
    """
    try:
        current_date = frappe.utils.getdate()
        
        if current_date.month >= 4:  # April onwards = new academic year
            academic_year = f"{current_date.year}-{str(current_date.year + 1)[-2:]}"
        else:
            academic_year = f"{current_date.year - 1}-{str(current_date.year)[-2:]}"
        
        # REMOVED: Problematic logging - use print for debugging if needed
        # print(f"DEBUG: Current academic year: {academic_year}")
        
        return academic_year
        
    except Exception as e:
        print(f"Error calculating academic year: {str(e)}")
        return None


def get_course_level_with_mapping(course_vertical, grade, phone_number, student_name, kitless):
    """
    Get course level using Grade Course Level Mapping with fallback to Stage Grades logic
    
    Args:
        course_vertical: Course vertical name/ID
        grade: Student grade
        phone_number: Student phone number
        student_name: Student name (for unique identification with phone)
        kitless: School's kit capability (for fallback logic)
    
    Returns:
        Course level name or raises exception
    """
    try:
        # Step 1: Determine student type using phone + name combination
        student_type = determine_student_type(phone_number, student_name, course_vertical)
        
        # Step 2: Get current academic year
        academic_year = get_current_academic_year()
        
        # REMOVED: Problematic logging - use print for debugging if needed
        # print(f"DEBUG: Course level mapping lookup: {course_vertical}, {grade}, {student_type}, {academic_year}")
        
        # Step 3: Try manual mapping with current academic year
        if academic_year:
            mapping = frappe.get_all(
                "Grade Course Level Mapping",
                filters={
                    "academic_year": academic_year,
                    "course_vertical": course_vertical,
                    "grade": grade,
                    "student_type": student_type,
                    "is_active": 1
                },
                fields=["assigned_course_level", "mapping_name"],
                order_by="modified desc",  # Last modified takes priority
                limit=1
            )
            
            if mapping:
                # REMOVED: Problematic logging - use print for debugging if needed
                # print(f"DEBUG: Found mapping: {mapping[0].mapping_name} -> {mapping[0].assigned_course_level}")
                return mapping[0].assigned_course_level
        
        # Step 4: Try mapping with academic_year = null (flexible mappings)
        mapping_null = frappe.get_all(
            "Grade Course Level Mapping",
            filters={
                "academic_year": ["is", "not set"],  # Null academic year
                "course_vertical": course_vertical,
                "grade": grade,
                "student_type": student_type,
                "is_active": 1
            },
            fields=["assigned_course_level", "mapping_name"],
            order_by="modified desc",
            limit=1
        )
        
        if mapping_null:
            # REMOVED: Problematic logging - use print for debugging if needed
            # print(f"DEBUG: Found flexible mapping: {mapping_null[0].assigned_course_level}")
            return mapping_null[0].assigned_course_level
        
        # Step 5: Log that no mapping was found, falling back
        # REMOVED: Problematic logging - use print for debugging if needed
        # print(f"DEBUG: No mapping found, using Stage Grades fallback")
        
        # Step 6: Fallback to current Stage Grades logic
        return get_course_level_original(course_vertical, grade, kitless)
        
    except Exception as e:
        # Simple print for debugging instead of frappe.log_error
        print(f"Error in course level mapping: {str(e)}")
        # On any error, fallback to original logic
        return get_course_level_original(course_vertical, grade, kitless)


def get_course_level_original(course_vertical, grade, kitless):
    """
    Original course level selection logic using Stage Grades
    """
    # REMOVED: Problematic logging - use print for debugging if needed
    # print(f"DEBUG: Using Stage Grades logic: {course_vertical}, {grade}, {kitless}")
    
    try:
        # Find stage by grade
        query = """
            SELECT name FROM `tabStage Grades`
            WHERE CAST(%s AS INTEGER) BETWEEN CAST(from_grade AS INTEGER) AND CAST(to_grade AS INTEGER)
        """
        stage = frappe.db.sql(query, grade, as_dict=True)

        if not stage:
            # Check if there is a specific stage for the given grade
            query = """
                SELECT name FROM `tabStage Grades`
                WHERE CAST(from_grade AS INTEGER) = CAST(%s AS INTEGER) 
                AND CAST(to_grade AS INTEGER) = CAST(%s AS INTEGER)
            """
            stage = frappe.db.sql(query, (grade, grade), as_dict=True)

            if not stage:
                frappe.throw("No matching stage found for the given grade")

        course_level = frappe.get_all(
            "Course Level",
            filters={
                "vertical": course_vertical,
                "stage": stage[0].name,
                "kit_less": kitless
            },
            fields=["name"],
            order_by="modified desc",
            limit=1
        )

        if not course_level and kitless:
            # If no course level found with kit_less enabled, search without considering kit_less
            course_level = frappe.get_all(
                "Course Level",
                filters={
                    "vertical": course_vertical,
                    "stage": stage[0].name
                },
                fields=["name"],
                order_by="modified desc",
                limit=1
            )

        if not course_level:
            frappe.throw("No matching course level found")

        return course_level[0].name
        
    except Exception as e:
        # Simple print for debugging instead of frappe.log_error
        print(f"Stage Grades fallback failed: {str(e)}")
        raise



def create_new_student(student_name, phone, gender, school_id, grade, language_name, glific_id):
    student = frappe.get_doc({
        "doctype": "Student",
        "name1": student_name,
        "phone": phone,
        "gender": gender,
        "school_id": school_id,
        "grade": grade,
        "language": get_tap_language(language_name),
        "glific_id": glific_id,
        "joined_on": now_datetime().date(),
        "status": "active"
    })

    student.insert(ignore_permissions=True)
    return student

def get_tap_language(language_name):
    tap_language = frappe.get_all(
        "TAP Language",
        filters={"language_name": language_name},
        fields=["name"]
    )

    if not tap_language:
        frappe.throw(f"No TAP Language found for language name: {language_name}")

    return tap_language[0].name





@frappe.whitelist(allow_guest=True)
def verify_batch_keyword():
    try:
        # Get the JSON data from the request body
        data = json.loads(frappe.request.data)
        api_key = data.get('api_key')
        batch_skeyword = data.get('batch_skeyword')

        if not api_key or not batch_skeyword:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "API key and batch_skeyword are required"}

        if not authenticate_api_key(api_key):
            frappe.response.http_status_code = 401
            return {"status": "error", "message": "Invalid API key"}

        batch_onboarding = frappe.get_all(
            "Batch onboarding",
            filters={"batch_skeyword": batch_skeyword},
            fields=["school", "batch", "model","kit_less"]
        )

        if not batch_onboarding:
            frappe.response.http_status_code = 202
            return {"status": "error", "message": "Invalid batch keyword"}

        batch_id = batch_onboarding[0].batch
        batch_doc = frappe.get_doc("Batch", batch_id)
        current_date = getdate()

        if not batch_doc.active:
            frappe.response.http_status_code = 202
            return {"status": "error", "message": "The batch is not active"}

        if batch_doc.regist_end_date:
            try:
                regist_end_date = getdate(cstr(batch_doc.regist_end_date))
                if regist_end_date < current_date:
                    frappe.response.http_status_code = 202
                    return {"status": "error", "message": "Registration for this batch has ended"}
            except Exception as e:
                frappe.log_error(f"Error parsing registration end date: {str(e)}")
                frappe.response.http_status_code = 500
                return {"status": "error", "message": "Invalid registration end date format"}

        school_name = cstr(frappe.get_value("School", batch_onboarding[0].school, "name1"))
        batch_id = cstr(frappe.get_value("Batch", batch_onboarding[0].batch, "batch_id"))
        tap_model = frappe.get_doc("Tap Models", batch_onboarding[0].model)
        kit_less = batch_onboarding[0].kit_less
        school_district = None
        district_id = frappe.get_value("School", batch_onboarding[0].school, "district")
        if district_id:
            school_district = frappe.get_value("District", district_id, "district_name")
        

        response = {
            "school_name": school_name,
            "school_district": school_district,
            "batch_id": batch_id,
            "tap_model_id": cstr(tap_model.name),
            "tap_model_name": cstr(tap_model.mname),
            "kit_less": kit_less,
            "status": "success"
        }

        frappe.response.http_status_code = 200
        return response

    except Exception as e:
        frappe.log_error(f"Verify Batch Keyword Error: {str(e)}")
        frappe.response.http_status_code = 500
        return {"status": "error", "message": str(e)}


@frappe.whitelist(allow_guest=True)
def grade_list(api_key, keyword):
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")

    # Find the batch onboarding document based on the batch_skeyword
    batch_onboarding = frappe.get_all(
        "Batch onboarding",
        filters={"batch_skeyword": keyword},
        fields=["name", "from_grade", "to_grade"]
    )

    if not batch_onboarding:
        frappe.throw("No batch found with the provided keyword")

    # Extract the from_grade and to_grade from the batch onboarding document
    from_grade = cint(batch_onboarding[0].from_grade)
    to_grade = cint(batch_onboarding[0].to_grade)

    # Generate a dictionary of grades based on the from_grade and to_grade
    grades = {}
    count = 0
    for i, grade in enumerate(range(from_grade, to_grade + 1), start=1):
        grades[str(i)] = str(grade)
        count += 1

    # Add the count to the grades dictionary
    grades["count"] = str(count)

    return grades


@frappe.whitelist(allow_guest=True)
def course_vertical_list():
    try:
        # Get JSON data from the request
        data = frappe.local.form_dict
        api_key = data.get('api_key')
        keyword = data.get('keyword')

        if not authenticate_api_key(api_key):
            frappe.throw("Invalid API key")

        batch_onboarding = frappe.get_all(
            "Batch onboarding",
            filters={"batch_skeyword": keyword},
            fields=["name"]
        )

        if not batch_onboarding:
            return {"error": "Invalid batch keyword"}

        batch_school_verticals = frappe.get_all(
            "Batch School Verticals",
            filters={"parent": batch_onboarding[0].name},
            fields=["course_vertical"]
        )

        response_data = {}
        for vertical in batch_school_verticals:
            course_vertical = frappe.get_doc("Course Verticals", vertical.course_vertical)
            response_data[course_vertical.vertical_id] = course_vertical.name2

        return response_data

    except Exception as e:
        frappe.log_error(f"Course Vertical List Error: {str(e)}")
        return {"status": "error", "message": str(e)}



@frappe.whitelist(allow_guest=True)
def list_schools():
    # Parse the request data
    data = frappe.request.get_json()

    # Verify the API key
    if not data or 'api_key' not in data or not authenticate_api_key(data['api_key']):
        frappe.response.http_status_code = 401
        frappe.response.update({
            "status": "failure",
            "schools": [],
            "error": "Invalid API key"
        })
        return

    district = data.get('district')
    city = data.get('city')

    filters = {}
    if district:
        filters['district'] = district
    if city:
        filters['city'] = city

    # Fetch schools based on filters
    schools = frappe.get_all("School", filters=filters, fields=["name1 as School_name"])

    if schools:
        frappe.response.http_status_code = 200
        frappe.response.update({
            "status": "success",
            "schools": schools
        })
    else:
        frappe.response.http_status_code = 404
        frappe.response.update({
            "status": "failure",
            "schools": [],
            "message": "No schools found for the given criteria"
        })



@frappe.whitelist(allow_guest=True)
def course_vertical_list_count():
    try:
        # Get JSON data from the request
        data = frappe.local.form_dict
        api_key = data.get('api_key')
        keyword = data.get('keyword')

        if not authenticate_api_key(api_key):
            frappe.throw("Invalid API key")

        batch_onboarding = frappe.get_all(
            "Batch onboarding",
            filters={"batch_skeyword": keyword},
            fields=["name"]
        )

        if not batch_onboarding:
            return {"error": "Invalid batch keyword"}

        batch_school_verticals = frappe.get_all(
            "Batch School Verticals",
            filters={"parent": batch_onboarding[0].name},
            fields=["course_vertical"]
        )

        response_data = {}
        count = 0

        for index, vertical in enumerate(batch_school_verticals, start=1):
            course_vertical = frappe.get_doc("Course Verticals", vertical.course_vertical)
            response_data[str(index)] = course_vertical.name2
            count += 1

        response_data["count"] = str(count)

        return response_data

    except Exception as e:
        frappe.log_error(f"Course Vertical List Count Error: {str(e)}")
        return {"status": "error", "message": str(e)}






@frappe.whitelist(allow_guest=True)
def send_otp_gs():
    data = frappe.request.get_json()
    
    if not data or 'api_key' not in data or not authenticate_api_key(data['api_key']):
        frappe.response.http_status_code = 401
        return {"status": "failure", "message": "Invalid API key"}
    
    if 'phone' not in data:
        frappe.response.http_status_code = 400
        return {"status": "failure", "message": "Phone number is required"}
    
    phone_number = data['phone']

    # Check if the phone number is already registered
    existing_teacher = frappe.get_all("Teacher", filters={"phone_number": phone_number}, fields=["name"])
    if existing_teacher:
        frappe.response.http_status_code = 409
        return {
            "status": "failure",
            "message": "A teacher with this phone number already exists",
            "existing_teacher_id": existing_teacher[0].name
        }

    otp = ''.join(random.choices(string.digits, k=4))
    
    # Store OTP in the database (you might want to create a new doctype for this)
    frappe.get_doc({
        "doctype": "OTP Verification",
        "phone_number": phone_number,
        "otp": otp,
        "expiry": now_datetime() + timedelta(minutes=15)
    }).insert(ignore_permissions=True)
    
    message = f"{otp} is your verification code"
    if send_whatsapp_message(phone_number, message):
        frappe.response.http_status_code = 200
        return {"status": "success", "message": "OTP sent successfully"}
    else:
        frappe.response.http_status_code = 500
        return {"status": "failure", "message": "Failed to send OTP"}







@frappe.whitelist(allow_guest=True)
def send_otp_v0():
    data = frappe.request.get_json()

    if not data or 'api_key' not in data or not authenticate_api_key(data['api_key']):
        frappe.response.http_status_code = 401
        return {"status": "failure", "message": "Invalid API key"}

    if 'phone' not in data:
        frappe.response.http_status_code = 400
        return {"status": "failure", "message": "Phone number is required"}

    phone_number = data['phone']

    # Check if the phone number is already registered
    existing_teacher = frappe.get_all("Teacher", filters={"phone_number": phone_number}, fields=["name"])
    if existing_teacher:
        frappe.response.http_status_code = 409
        return {
            "status": "failure",
            "message": "A teacher with this phone number already exists",
            "existing_teacher_id": existing_teacher[0].name
        }

    otp = ''.join(random.choices(string.digits, k=4))

    # Store OTP in the database
    frappe.get_doc({
        "doctype": "OTP Verification",
        "phone_number": phone_number,
        "otp": otp,
        "expiry": now_datetime() + timedelta(minutes=15)
    }).insert(ignore_permissions=True)

    # Send WhatsApp message using the API
    whatsapp_api_key = "J3tuS4rCqzcLiqt2SjyeiqYxjVLICnWb"  # Replace with your actual API key
    instance = "27715370"  # Replace with your actual instance ID
    message = f"Your OTP is: {otp}"
    
    api_url = f"https://chatspaz.com/api/v1/send/wa/message?api_key={whatsapp_api_key}&instance={instance}&to={phone_number}&type=text&message={message}"

    try:
        response = requests.get(api_url)
        response_data = response.json()

        if response_data.get("status") == "success":
            frappe.response.http_status_code = 200
            return {
                "status": "success",
                "message": "OTP sent successfully via WhatsApp",
                "whatsapp_message_id": response_data.get("id")
            }
        else:
            frappe.response.http_status_code = 500
            return {
                "status": "failure",
                "message": "Failed to send OTP via WhatsApp",
                "error": response_data.get("message")
            }

    except requests.RequestException as e:
        frappe.response.http_status_code = 500
        return {
            "status": "failure",
            "message": "Error occurred while sending OTP via WhatsApp",
            "error": str(e)
        }



@frappe.whitelist(allow_guest=True)
def send_otp():
    try:
        data = frappe.request.get_json()

        if not data or 'api_key' not in data or not authenticate_api_key(data['api_key']):
            frappe.response.http_status_code = 401
            return {"status": "failure", "message": "Invalid API key"}

        if 'phone' not in data:
            frappe.response.http_status_code = 400
            return {"status": "failure", "message": "Phone number is required"}

        phone_number = data['phone']

        # Check if the phone number is already registered
        existing_teacher = frappe.get_all("Teacher", 
                                        filters={"phone_number": phone_number}, 
                                        fields=["name", "school_id"])
        
        otp_context = {
            "action_type": "new_teacher",
            "teacher_id": None,
            "school_name": None,
            "batch_info": None
        }
        
        if existing_teacher:
            teacher = existing_teacher[0]
            
            # Get school from the teacher record
            school = teacher.school_id
            if not school:
                frappe.response.http_status_code = 400
                return {"status": "failure", "message": "Teacher has no school assigned"}
            
            # Get school name
            school_name = frappe.db.get_value("School", school, "name1")
            
            # Check if there's an active batch for this school
            batch_info = get_active_batch_for_school(school)
            
            if not batch_info["batch_id"] or batch_info["batch_id"] == "no_active_batch_id":
                frappe.response.http_status_code = 409
                return {
                    "status": "failure",
                    "message": "No active batch available for your school",
                    "code": "NO_ACTIVE_BATCH"
                }
            
            # Check if teacher is already in this batch's group
            group_label = f"teacher_batch_{batch_info['batch_id']}"
            existing_group_mapping = frappe.get_all(
                "Glific Teacher Group",
                filters={"batch": batch_info["batch_name"]},
                fields=["glific_group_id"]
            )
            
            if existing_group_mapping:
                # Check if teacher's Glific contact is in this group
                teacher_glific_id = frappe.db.get_value("Teacher", teacher.name, "glific_id")
                if teacher_glific_id:
                    # Optional: Check if they were part of this batch before
                    teacher_batch_history = frappe.get_all(
                        "Teacher Batch History",
                        filters={
                            "teacher": teacher.name,
                            "batch": batch_info["batch_name"],
                            "status": "Active"
                        }
                    )
                    
                    if teacher_batch_history:
                        frappe.response.http_status_code = 409
                        return {
                            "status": "failure",
                            "message": "You are already registered for this batch",
                            "code": "ALREADY_IN_BATCH",
                            "teacher_id": teacher.name,
                            "batch_id": batch_info["batch_id"]
                        }
            
            # Teacher exists but not in this batch - prepare for update
            otp_context = {
                "action_type": "update_batch",
                "teacher_id": teacher.name,
                "school_name": school_name,
                "school_id": school,
                "batch_info": batch_info
            }

        # If teacher doesn't exist, we'll need school_name in create_teacher_web
        # That will come from the web form after OTP verification

        #otp = ''.join(random.choices(string.digits, k=4))
        otp = '111'

        # Store OTP with context in the database
        try:
            otp_doc = frappe.get_doc({
                "doctype": "OTP Verification",
                "phone_number": phone_number,
                "otp": otp,
                "expiry": now_datetime() + timedelta(minutes=15),
                "context": json.dumps(otp_context)  # Store context as JSON
            })
            otp_doc.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as e:
            frappe.log_error(f"Failed to store OTP: {str(e)}", "OTP Storage Error")
            frappe.response.http_status_code = 500
            return {
                "status": "failure",
                "message": "Failed to store OTP in the database",
                "error": str(e)
            }

        # Send WhatsApp message using the API
        whatsapp_api_key = frappe.conf.get("whatsapp_api_key", "J3tuS4rCqzcLiqt2SjyeiqYxjVLICnWb")
        instance = frappe.conf.get("whatsapp_instance", "27715370")
        message = f"Your OTP is: {otp}"
        
        api_url = f"https://chatspaz.com/api/v1/send/wa/message?api_key={whatsapp_api_key}&instance={instance}&to={phone_number}&type=text&message={message}"

        try:
            #response = requests.get(api_url)
            #response_data = response.json()
            response_data = {
                "status": "success",
                "id": "12345",
                "message": "Static success response"
        }

            if response_data.get("status") == "success":
                frappe.response.http_status_code = 200
                return {
                    "status": "success",
                    "message": "OTP sent successfully via WhatsApp",
                    "action_type": otp_context["action_type"],
                    "is_existing_teacher": bool(existing_teacher),
                    "whatsapp_message_id": response_data.get("id")
                }
            else:
                frappe.log_error(f"WhatsApp API Error: {response_data.get('message')}", "WhatsApp API Error")
                frappe.response.http_status_code = 500
                return {
                    "status": "failure",
                    "message": "Failed to send OTP via WhatsApp",
                    "error": response_data.get("message")
                }

        except requests.RequestException as e:
            frappe.log_error(f"WhatsApp API Request Error: {str(e)}", "WhatsApp API Request Error")
            frappe.response.http_status_code = 500
            return {
                "status": "failure",
                "message": "Error occurred while sending OTP via WhatsApp",
                "error": str(e)
            }

    except Exception as e:
        frappe.log_error(f"Unexpected error in send_otp: {str(e)}", "Send OTP Error")
        frappe.response.http_status_code = 500
        return {
            "status": "failure",
            "message": "An unexpected error occurred",
            "error": str(e)
        }





@frappe.whitelist(allow_guest=True)
def send_otp_mock():
    data = frappe.request.get_json()

    if not data or 'api_key' not in data or not authenticate_api_key(data['api_key']):
        frappe.response.http_status_code = 401
        return {"status": "failure", "message": "Invalid API key"}

    if 'phone' not in data:
        frappe.response.http_status_code = 400
        return {"status": "failure", "message": "Phone number is required"}

    phone_number = data['phone']

    # Check if the phone number is already registered
    existing_teacher = frappe.get_all("Teacher", filters={"phone_number": phone_number}, fields=["name"])
    if existing_teacher:
        frappe.response.http_status_code = 409
        return {
            "status": "failure",
            "message": "A teacher with this phone number already exists",
            "existing_teacher_id": existing_teacher[0].name
        }

    otp = ''.join(random.choices(string.digits, k=4))

    # Store OTP in the database (you might want to create a new doctype for this)
    frappe.get_doc({
        "doctype": "OTP Verification",
        "phone_number": phone_number,
        "otp": otp,
        "expiry": now_datetime() + timedelta(minutes=15)
    }).insert(ignore_permissions=True)

    # Mock sending WhatsApp message by printing to console
    print(f"MOCK WHATSAPP MESSAGE: OTP {otp} sent to {phone_number}")

    frappe.response.http_status_code = 200
    return {
        "status": "success", 
        "message": "OTP sent successfully",
        "mock_otp": otp  # Include OTP in the response for testing
    }



@frappe.whitelist(allow_guest=True)
def verify_otp():
    try:
        data = frappe.request.get_json()

        if not data or 'api_key' not in data or not authenticate_api_key(data['api_key']):
            frappe.response.http_status_code = 401
            return {"status": "failure", "message": "Invalid API key"}

        if 'phone' not in data or 'otp' not in data:
            frappe.response.http_status_code = 400
            return {"status": "failure", "message": "Phone number and OTP are required"}

        phone_number = data['phone']
        #otp = data['otp']
        otp = '111'  # For testing purposes, accept the static OTP

        # Use a direct SQL query to get OTP with context
        verification = frappe.db.sql("""
            SELECT name, expiry, context, verified
            FROM `tabOTP Verification`
            WHERE phone_number = %s AND otp = %s
            ORDER BY creation DESC
            LIMIT 1
        """, (phone_number, otp), as_dict=1)

        if not verification:
            frappe.response.http_status_code = 400
            return {"status": "failure", "message": "Invalid OTP"}

        verification = verification[0]

        # Check if already verified
        if verification.verified:
            frappe.response.http_status_code = 400
            return {"status": "failure", "message": "OTP already used"}

        # Convert expiry to datetime and compare with current datetime
        if get_datetime(verification.expiry) < now_datetime():
            frappe.response.http_status_code = 400
            return {"status": "failure", "message": "OTP has expired"}

        # Mark the phone number as verified using raw SQL
        frappe.db.sql("""
            UPDATE `tabOTP Verification`
            SET verified = 1
            WHERE name = %s
        """, (verification.name,))

        # Parse the context
        context = json.loads(verification.context) if verification.context else {}
        action_type = context.get("action_type", "new_teacher")

        # Handle update_batch action directly in verify_otp
        if action_type == "update_batch":
            try:
                teacher_id = context.get("teacher_id")
                batch_info = context.get("batch_info")
                school_id = context.get("school_id")

                if not all([teacher_id, batch_info, school_id]):
                    frappe.response.http_status_code = 400
                    return {"status": "failure", "message": "Invalid context data"}

                # Get teacher document
                teacher = frappe.get_doc("Teacher", teacher_id)

                # Get model for the school (might have changed if batch has different model)
                model_name = get_model_for_school(school_id)

                # FIXED: Handle missing glific_id by creating/linking Glific contact
                if not teacher.glific_id:
                    frappe.logger().warning(f"Teacher {teacher_id} has no Glific ID. Attempting to create/link.")

                    # Try to find existing Glific contact by phone
                    try:
                        glific_contact = get_contact_by_phone(teacher.phone_number)
                    except requests.exceptions.RequestException as e:
                        frappe.log_error(
                            title="Glific timeout (degraded)",
                            message=f"verify_otp update_batch: get_contact_by_phone timed out for {teacher.phone_number}: {e}",
                        )
                        glific_contact = None

                    if glific_contact and 'id' in glific_contact:
                        # Found existing contact, link it
                        teacher.glific_id = glific_contact['id']
                        teacher.save(ignore_permissions=True)
                        frappe.logger().info(f"Linked teacher {teacher_id} to existing Glific contact {glific_contact['id']}")
                    else:
                        # No existing contact, create new one
                        school_name = frappe.db.get_value("School", school_id, "name1")

                        # Get language_id for Glific
                        language_id = None
                        if teacher.language:
                            language_id = frappe.db.get_value("TAP Language", teacher.language, "glific_language_id")

                        if not language_id:
                            language_id = frappe.db.get_value("TAP Language", {"language_name": "English"}, "glific_language_id")

                        if not language_id:
                            frappe.logger().warning("No English language found in TAP Language. Using None for language_id.")
                            language_id = None

                        try:
                            new_contact = create_contact(
                                teacher.first_name or "Teacher",  # Fallback if first_name is empty
                                teacher.phone_number,
                                school_name,
                                model_name,
                                language_id,
                                batch_info["batch_id"]
                            )
                        except requests.exceptions.RequestException as e:
                            frappe.log_error(
                                title="Glific timeout (degraded)",
                                message=f"verify_otp update_batch: create_contact timed out for {teacher.phone_number}: {e}",
                            )
                            new_contact = None

                        if new_contact and 'id' in new_contact:
                            teacher.glific_id = new_contact['id']
                            teacher.save(ignore_permissions=True)
                            frappe.logger().info(f"Created new Glific contact {new_contact['id']} for teacher {teacher_id}")
                        else:
                            frappe.logger().error(f"Failed to create Glific contact for teacher {teacher_id}")
                            # Continue without failing - we'll handle this gracefully

                # Update model and batch_id in Glific contact fields (only if we have glific_id)
                if teacher.glific_id:
                    fields_to_update = {
                        "model": model_name,
                        "batch_id": batch_info["batch_id"]
                    }

                    update_success = update_contact_fields(teacher.glific_id, fields_to_update)

                    if not update_success:
                        frappe.logger().warning(f"Failed to update Glific contact fields for teacher {teacher_id}")
                else:
                    frappe.logger().warning(f"Teacher {teacher_id} still has no Glific ID after creation attempts. Continuing without Glific operations.")

                # Add teacher to new batch group (only if we have glific_id)
                if teacher.glific_id:
                    teacher_group = create_or_get_teacher_group_for_batch(
                        batch_info["batch_name"],
                        batch_info["batch_id"]
                    )

                    if teacher_group:
                        try:
                            group_added = add_contact_to_group(teacher.glific_id, teacher_group["group_id"])
                        except requests.exceptions.RequestException as e:
                            frappe.log_error(
                                title="Glific timeout (degraded)",
                                message=f"verify_otp update_batch: add_contact_to_group timed out for teacher {teacher_id}: {e}",
                            )
                            group_added = False
                        if group_added:
                            frappe.logger().info(f"Teacher {teacher_id} added to group {teacher_group['label']}")
                        else:
                            frappe.logger().warning(f"Failed to add teacher {teacher_id} to group")

                # Create batch history record to track which batches teacher has joined
                try:
                    frappe.get_doc({
                        "doctype": "Teacher Batch History",
                        "teacher": teacher_id,
                        "batch": batch_info["batch_name"],
                        "batch_id": batch_info["batch_id"],
                        "status": "Active",
                        "joined_date": today()
                    }).insert(ignore_permissions=True)
                except Exception as e:
                    frappe.logger().warning(f"Could not create batch history: {str(e)}")

                # Enqueue background job for flow (only if we have glific_id)
                if teacher.glific_id:
                    school_name = frappe.db.get_value("School", school_id, "name1")

                    enqueue_glific_actions(
                        teacher.name,
                        phone_number,
                        teacher.first_name,
                        school_id,
                        school_name,
                        teacher.language,
                        model_name,
                        batch_info["batch_name"],
                        batch_info["batch_id"]
                    )

                frappe.db.commit()

                frappe.response.http_status_code = 200
                return {
                    "status": "success",
                    "message": "Successfully added to new batch",
                    "action_type": "update_batch",
                    "teacher_id": teacher_id,
                    "batch_id": batch_info["batch_id"],
                    "model": model_name,
                    "glific_contact_id": teacher.glific_id,
                    "has_glific": bool(teacher.glific_id)
                }

            except Exception as e:
                frappe.db.rollback()
                frappe.log_error(f"Error updating teacher batch in verify_otp: {str(e)}", "Teacher Batch Update Error")
                frappe.response.http_status_code = 500
                return {
                    "status": "failure",
                    "message": "Failed to add teacher to new batch",
                    "error": str(e)
                }

        # For new teacher, just verify and return success
        else:
            frappe.db.commit()
            frappe.response.http_status_code = 200
            return {
                "status": "success",
                "message": "Phone number verified successfully",
                "action_type": "new_teacher"
            }

    except Exception as e:
        frappe.log_error(f"OTP Verification Error: {str(e)}")
        frappe.response.http_status_code = 500
        return {"status": "failure", "message": "An error occurred during OTP verification"}



@frappe.whitelist(allow_guest=True)
def old_create_teacher_web():
    try:
        frappe.flags.ignore_permissions = True
        data = frappe.request.get_json()

        # Validate API key
        if 'api_key' not in data or not authenticate_api_key(data['api_key']):
            return {"status": "failure", "message": "Invalid API key"}

        # Validate required fields
        required_fields = ['firstName', 'phone', 'School_name']
        for field in required_fields:
            if field not in data:
                return {"status": "failure", "message": f"Missing required field: {field}"}

        # Check if the phone number is verified
        verification = frappe.db.get_value("OTP Verification",
            {"phone_number": data['phone'], "verified": 1}, "name")
        if not verification:
            return {"status": "failure", "message": "Phone number is not verified. Please verify your phone number first."}

        # Check if the phone number already exists in Frappe
        existing_teacher = frappe.db.get_value("Teacher", {"phone_number": data['phone']}, "name")
        if existing_teacher:
            return {
                "status": "failure",
                "message": "A teacher with this phone number already exists",
                "existing_teacher_id": existing_teacher
            }

        # Get the school_id based on the School_name
        school = frappe.db.get_value("School", {"name1": data['School_name']}, "name")
        if not school:
            return {"status": "failure", "message": "School not found"}

        # Get the appropriate model for the school
        model_name = get_model_for_school(school)

        # Create new Teacher document
        new_teacher = frappe.get_doc({
            "doctype": "Teacher",
            "first_name": data['firstName'],
            "last_name": data.get('lastName', ''),
            "phone_number": data['phone'],
            "language": data.get('language', ''),
            "school_id": school
        })

        new_teacher.insert(ignore_permissions=True)

        # Get the school name
        school_name = frappe.db.get_value("School", school, "name1")

        # Get the language ID from TAP Language
        language_id = frappe.db.get_value("TAP Language", data.get('language'), "glific_language_id")
        if not language_id:
            language_id = frappe.db.get_value("TAP Language", {"language_name": "English"}, "glific_language_id")  # Default to English if not found

        # Get the active batch ID for this school
        batch_info = get_active_batch_for_school(school)
        batch_id = batch_info["batch_id"]
        batch_name = batch_info["batch_name"]

        if not batch_id:
            frappe.logger().warning(f"No active batch found for school {school}. Using empty string for batch_id.")
            batch_id = ""  # Fallback to empty string if no batch found
            batch_name = ""  # Also set batch_name to empty string

        # Check if the phone number already exists in Glific
        try:
            glific_contact = get_contact_by_phone(data['phone'])
        except requests.exceptions.RequestException as e:
            frappe.log_error(
                title="Glific timeout (degraded)",
                message=f"create_teacher_web: get_contact_by_phone timed out for {data['phone']}: {e}",
            )
            glific_contact = None

        if glific_contact and 'id' in glific_contact:
            # Contact exists in Glific, update fields
            frappe.logger().info(f"Existing Glific contact found with ID: {glific_contact['id']}. Updating fields.")
            
            # Prepare fields to update
            fields_to_update = {
                "school": school_name,
                "model": model_name,
                "buddy_name": data['firstName'],
                "batch_id": batch_id
            }
                
            # Update the contact fields
            update_success = update_contact_fields(glific_contact['id'], fields_to_update)
            
            # Always associate the teacher with the Glific contact, even if update fails
            new_teacher.glific_id = glific_contact['id']
            new_teacher.save(ignore_permissions=True)
            
            # Enqueue Glific actions (optin and flow start) as a background job
            enqueue_glific_actions(
                new_teacher.name,
                data['phone'],
                data['firstName'],
                school,
                school_name,
                data.get('language', ''),
                model_name,
                batch_name,
                batch_id
            )
            
            frappe.db.commit()
            
            if update_success:
                return {
                    "status": "success",
                    "message": "Teacher created successfully, existing Glific contact updated and associated.",
                    "teacher_id": new_teacher.name,
                    "glific_contact_id": new_teacher.glific_id
                }
            else:
                # Still return success but with a warning about field updating
                return {
                    "status": "partial_success",
                    "message": "Teacher created and associated with existing Glific contact, but failed to update contact fields.",
                    "teacher_id": new_teacher.name,
                    "glific_contact_id": glific_contact['id']
                }
        
        # If we've already handled an existing contact, skip this section
        if not (glific_contact and 'id' in glific_contact):
            # No existing contact found, create a new one
            frappe.logger().info(f"Creating new Glific contact for teacher {new_teacher.name}")
            try:
                glific_contact = create_contact(
                    data['firstName'],
                    data['phone'],
                    school_name,
                    model_name,
                    language_id,
                    batch_id
                )
            except requests.exceptions.RequestException as e:
                frappe.log_error(
                    title="Glific timeout (degraded)",
                    message=f"create_teacher_web: create_contact timed out for {data['phone']}: {e}",
                )
                glific_contact = None

            if glific_contact and 'id' in glific_contact:
                new_teacher.glific_id = glific_contact['id']
                new_teacher.save(ignore_permissions=True)

                # Enqueue Glific actions (optin and flow start) as a background job
                # FIXED: Added batch_name and batch_id parameters
                enqueue_glific_actions(
                    new_teacher.name,
                    data['phone'],
                    data['firstName'],
                    school,
                    school_name,
                    data.get('language', ''),
                    model_name,
                    batch_name,  # ADDED: batch_name parameter
                    batch_id     # ADDED: batch_id parameter
                )

                frappe.db.commit()
                return {
                    "status": "success",
                    "message": "Teacher created successfully, Glific contact added. Optin and flow start initiated.",
                    "teacher_id": new_teacher.name,
                    "glific_contact_id": new_teacher.glific_id
                }
            else:
                # Keep the teacher but inform about the Glific contact failure
                frappe.db.commit()  # Commit to save the teacher record
                return {
                    "status": "partial_success",
                    "message": "Teacher created but failed to add Glific contact",
                    "teacher_id": new_teacher.name
                }
        
        # This should never be reached as all paths above have return statements
        frappe.db.commit()
        return {
            "status": "success", 
            "message": "Teacher created successfully",
            "teacher_id": new_teacher.name
        }

    except Exception as e:
        frappe.db.rollback()
        frappe.logger().error(f"Error in create_teacher_web: {str(e)}", exc_info=True)
        return {
            "status": "failure",
            "message": f"Error creating teacher: {str(e)}"
        }
    finally:
        frappe.flags.ignore_permissions = False
  




def get_course_level(course_vertical, grade, kitless):
    frappe.log_error(f"Input values: course_vertical={course_vertical}, grade={grade}, kitless={kitless}")

    query = """
        SELECT name FROM `tabStage Grades`
        WHERE CAST(%s AS INTEGER) BETWEEN CAST(from_grade AS INTEGER) AND CAST(to_grade AS INTEGER)
    """
    frappe.log_error(f"Stage query: {query[:100]}..." if len(query) > 100 else query)
    stage = frappe.db.sql(query, grade, as_dict=True)

    frappe.log_error(f"Stage result: {stage}")

    if not stage:
        # Check if there is a specific stage for the given grade
        query = """
            SELECT name FROM `tabStage Grades`
            WHERE CAST(from_grade AS INTEGER) = CAST(%s AS INTEGER) AND CAST(to_grade AS INTEGER) = CAST(%s AS INTEGER)
        """
        frappe.log_error(f"Specific stage query: {query[:100]}..." if len(query) > 100 else query)
        stage = frappe.db.sql(query, (grade, grade), as_dict=True)

        frappe.log_error(f"Specific stage result: {stage}")

        if not stage:
            frappe.throw("No matching stage found for the given grade")

    course_level = frappe.get_all(
        "Course Level",
        filters={
            "vertical": course_vertical,
            "stage": stage[0].name,
            "kit_less": kitless
        },
        fields=["name"],
        order_by="modified desc",
        limit=1
    )

    frappe.log_error(f"Course level query filters: vertical={course_vertical}, stage={stage[0].name}, kit_less={kitless}")
    frappe.log_error(f"Course level query: {frappe.as_json(course_level)}")

    if not course_level and kitless:
        # If no course level found with kit_less enabled, search for a course level without considering kit_less
        course_level = frappe.get_all(
            "Course Level",
            filters={
                "vertical": course_vertical,
                "stage": stage[0].name
            },
            fields=["name"],
            order_by="modified desc",
            limit=1
        )

        frappe.log_error(f"Fallback course level query filters: vertical={course_vertical}, stage={stage[0].name}")
        frappe.log_error(f"Fallback course level query: {frappe.as_json(course_level)}")

    if not course_level:
        frappe.throw("No matching course level found")

    return course_level[0].name







@frappe.whitelist(allow_guest=True)
def get_course_level_api():
    try:
        # Get the data from the request
        api_key = frappe.form_dict.get('api_key')
        grade = frappe.form_dict.get('grade')
        vertical = frappe.form_dict.get('vertical')
        batch_skeyword = frappe.form_dict.get('batch_skeyword')

        if not authenticate_api_key(api_key):
            frappe.throw("Invalid API key")

        # Validate required fields
        if not all([grade, vertical, batch_skeyword]):
            return {"status": "error", "message": "All fields are required"}

        # Get the school and batch from batch_skeyword
        batch_onboarding = frappe.get_all(
            "Batch onboarding",
            filters={"batch_skeyword": batch_skeyword},
            fields=["name", "kit_less"]
        )

        if not batch_onboarding:
            return {"status": "error", "message": "Invalid batch_skeyword"}

        kitless = batch_onboarding[0].kit_less

        # Get the course vertical using the label
        course_vertical = frappe.get_all(
            "Course Verticals",
            filters={"name2": vertical},
            fields=["name"]
        )

        if not course_vertical:
            return {"status": "error", "message": "Invalid vertical label"}

        # Get the appropriate course level based on the kitless option
        course_level = get_course_level(course_vertical[0].name, grade, kitless)

        return {
            "status": "success",
            "course_level": course_level
        }

    except frappe.ValidationError as e:
        frappe.log_error(f"Course Level API Validation Error: {str(e)}")
        return {"status": "error", "message": str(e)}
    except Exception as e:
        frappe.log_error(f"Course Level API Error: {str(e)}")
        return {"status": "error", "message": str(e)}




def get_model_for_school(school_id):
    today = frappe.utils.today()
    
    # Check for active batch onboardings
    active_batch_onboardings = frappe.get_all(
        "Batch onboarding",
        filters={
            "school": school_id,
            "batch": ["in", frappe.get_all("Batch", filters={"start_date": ["<=", today], "end_date": [">=", today], "active": 1}, pluck="name")]
        },
        fields=["model", "creation"],
        order_by="creation desc"
    )

    if active_batch_onboardings:
        # Use the model from the most recent active batch onboarding
        model_link = active_batch_onboardings[0].model
        frappe.logger().info(f"Using model from batch onboarding created on {active_batch_onboardings[0].creation} for school {school_id}")
    else:
        model_link = frappe.db.get_value("School", school_id, "model")
        frappe.logger().info(f"No active batch onboarding found. Using school model for school {school_id}")

    # Get the model name from Tap Models
    model_name = frappe.db.get_value("Tap Models", model_link, "mname")
    
    if not model_name:
        frappe.logger().error(f"No model name found for model link {model_link}")
        raise ValueError(f"No model name found for school {school_id}")

    return model_name




@frappe.whitelist(allow_guest=True)
def update_teacher_role():
    """
    Update teacher role based on glific_id

    Expected JSON payload:
    {
        "api_key": "your_api_key",
        "glific_id": "teacher_glific_id",
        "teacher_role": "HM|Nodal_Officer_POC|Teacher|Master_Trainers"
    }
    """
    try:
        # Get the JSON data from the request body
        data = json.loads(frappe.request.data)
        api_key = data.get('api_key')
        glific_id = data.get('glific_id')
        teacher_role = data.get('teacher_role')

        # Validate API key
        if not api_key:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "API key is required"}

        if not authenticate_api_key(api_key):
            frappe.response.http_status_code = 401
            return {"status": "error", "message": "Invalid API key"}

        # Validate required fields
        if not glific_id:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "glific_id is required"}

        if not teacher_role:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "teacher_role is required"}

        # Validate teacher_role value
        valid_roles = ["HM", "Nodal_Officer_POC", "Teacher", "Master_Trainers", "Zonal_Coordinator"]
        if teacher_role not in valid_roles:
            frappe.response.http_status_code = 400
            return {
                "status": "error",
                "message": f"Invalid teacher_role. Must be one of: {', '.join(valid_roles)}"
            }

        # Find teacher by glific_id
        teacher = frappe.get_all(
            "Teacher",
            filters={"glific_id": glific_id},
            fields=["name", "first_name", "last_name", "teacher_role", "school_id"]
        )

        if not teacher:
            frappe.response.http_status_code = 404
            return {
                "status": "error",
                "message": f"No teacher found with glific_id: {glific_id}"
            }

        teacher_doc = frappe.get_doc("Teacher", teacher[0].name)
        old_role = teacher_doc.teacher_role

        # Update teacher role
        teacher_doc.teacher_role = teacher_role
        teacher_doc.save(ignore_permissions=True)
        frappe.db.commit()

        # Get school name for response
        school_name = frappe.db.get_value("School", teacher_doc.school_id, "name1") if teacher_doc.school_id else None

        frappe.response.http_status_code = 200
        return {
            "status": "success",
            "message": "Teacher role updated successfully",
            "data": {
                "teacher_id": teacher_doc.name,
                "teacher_name": f"{teacher_doc.first_name} {teacher_doc.last_name}",
                "glific_id": glific_id,
                "old_role": old_role,
                "new_role": teacher_role,
                "school": school_name
            }
        }

    except Exception as e:
        frappe.log_error(f"Update Teacher Role Error: {str(e)}", "Update Teacher Role API")
        frappe.response.http_status_code = 500
        return {
            "status": "error",
            "message": "An unexpected error occurred",
            "error": str(e)
        }


@frappe.whitelist(allow_guest=True)
def get_teacher_by_glific_id():
    """
    Get teacher details by glific_id

    Expected JSON payload:
    {
        "api_key": "your_api_key",
        "glific_id": "teacher_glific_id"
    }
    """
    try:
        # Get the JSON data from the request body
        data = json.loads(frappe.request.data)
        api_key = data.get('api_key')
        glific_id = data.get('glific_id')

        # Validate API key
        if not api_key:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "API key is required"}

        if not authenticate_api_key(api_key):
            frappe.response.http_status_code = 401
            return {"status": "error", "message": "Invalid API key"}

        # Validate required fields
        if not glific_id:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "glific_id is required"}

        # Find teacher by glific_id
        teacher = frappe.get_all(
            "Teacher",
            filters={"glific_id": glific_id},
            fields=[
                "name", "first_name", "last_name", "teacher_role",
                "school_id", "phone_number", "email_id", "department",
                "language", "gender", "course_level"
            ]
        )

        if not teacher:
            frappe.response.http_status_code = 404
            return {
                "status": "error",
                "message": f"No teacher found with glific_id: {glific_id}"
            }

        teacher_data = teacher[0]

        # Get related data
        school_name = frappe.db.get_value("School", teacher_data.school_id, "name1") if teacher_data.school_id else None
        language_name = frappe.db.get_value("TAP Language", teacher_data.language, "language_name") if teacher_data.language else None
        course_level_name = frappe.db.get_value("Course Level", teacher_data.course_level, "name1") if teacher_data.course_level else None

        # Get teacher's active batches
        active_batches = frappe.db.sql("""
            SELECT
                tbh.batch,
                b.name1 as batch_name,
                b.batch_id,
                tbh.joined_date,
                tbh.status
            FROM `tabTeacher Batch History` tbh
            INNER JOIN `tabBatch` b ON b.name = tbh.batch
            WHERE tbh.teacher = %s AND tbh.status = 'Active'
            ORDER BY tbh.joined_date DESC
        """, teacher_data.name, as_dict=True)

        frappe.response.http_status_code = 200
        return {
            "status": "success",
            "data": {
                "teacher_id": teacher_data.name,
                "first_name": teacher_data.first_name,
                "last_name": teacher_data.last_name,
                "full_name": f"{teacher_data.first_name} {teacher_data.last_name}",
                "teacher_role": teacher_data.teacher_role,
                "glific_id": glific_id,
                "phone_number": canonicalize_response_phone(teacher_data.phone_number),
                "email_id": teacher_data.email_id,
                "department": teacher_data.department,
                "gender": teacher_data.gender,
                "school": {
                    "id": teacher_data.school_id,
                    "name": school_name
                },
                "language": {
                    "id": teacher_data.language,
                    "name": language_name
                },
                "course_level": {
                    "id": teacher_data.course_level,
                    "name": course_level_name
                },
                "active_batches": active_batches
            }
        }

    except Exception as e:
        frappe.log_error(f"Get Teacher by Glific ID Error: {str(e)}", "Get Teacher API")
        frappe.response.http_status_code = 500
        return {
            "status": "error",
            "message": "An unexpected error occurred",
            "error": str(e)
        }



@frappe.whitelist(allow_guest=True)
def get_school_city():
    """
    Get city information of a school based on school name

    Expected JSON payload:
    {
        "api_key": "your_api_key",
        "school_name": "school_name1_value"
    }
    """
    try:
        # Get the JSON data from the request body
        data = json.loads(frappe.request.data)
        api_key = data.get('api_key')
        school_name = data.get('school_name')

        # Validate API key
        if not api_key:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "API key is required"}

        if not authenticate_api_key(api_key):
            frappe.response.http_status_code = 401
            return {"status": "error", "message": "Invalid API key"}

        # Validate required fields
        if not school_name:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "school_name is required"}

        # Find school by name1
        school = frappe.get_all(
            "School",
            filters={"name1": school_name},
            fields=["name", "name1", "city", "state", "country", "address", "pin"]
        )

        if not school:
            frappe.response.http_status_code = 404
            return {
                "status": "error",
                "message": f"No school found with name: {school_name}"
            }

        school_data = school[0]

        # Check if school has city
        if not school_data.city:
            # Get country name if available even without city
            country_name = None
            if school_data.country:
                country_name = frappe.db.get_value("Country", school_data.country, "country_name")

            # Get state name if available even without city
            state_name = None
            if school_data.state:
                state_name = frappe.db.get_value("State", school_data.state, "state_name")

            frappe.response.http_status_code = 200
            return {
                "status": "success",
                "message": "School found but no city assigned",
                "school_id": school_data.name,
                "school_name": school_data.name1,
                "city": None,
                "city_name": None,
                "district": None,
                "district_name": None,
                "state": school_data.state,
                "state_name": state_name,
                "country": school_data.country,
                "country_name": country_name,
                "address": school_data.address,
                "pin": school_data.pin
            }

        # Get city details
        city_doc = frappe.get_doc("City", school_data.city)

        # Get district details if available
        district_name = None
        state_name_from_district = None
        if city_doc.district:
            district_doc = frappe.get_doc("District", city_doc.district)
            district_name = district_doc.district_name

            # Get state details from district if available
            if district_doc.state:
                state_doc = frappe.get_doc("State", district_doc.state)
                state_name_from_district = state_doc.state_name

        # Get state name directly from school if available, otherwise use from district
        state_name = None
        if school_data.state:
            state_name = frappe.db.get_value("State", school_data.state, "state_name")
        elif state_name_from_district:
            state_name = state_name_from_district

        # Get country name if available
        country_name = None
        if school_data.country:
            country_name = frappe.db.get_value("Country", school_data.country, "country_name")

        frappe.response.http_status_code = 200
        return {
            "status": "success",
            "message": "City information retrieved successfully",
            "school_id": school_data.name,
            "school_name": school_data.name1,
            "city": school_data.city,
            "city_name": city_doc.city_name,
            "district": city_doc.district,
            "district_name": district_name,
            "state": school_data.state,
            "state_name": state_name,
            "country": school_data.country,
            "country_name": country_name,
            "address": school_data.address,
            "pin": school_data.pin
        }

    except frappe.DoesNotExistError as e:
        frappe.log_error(f"Get School City Error - Document not found: {str(e)}", "Get School City API")
        frappe.response.http_status_code = 404
        return {
            "status": "error",
            "message": "Referenced location data not found",
            "error": str(e)
        }
    except Exception as e:
        frappe.log_error(f"Get School City Error: {str(e)}", "Get School City API")
        frappe.response.http_status_code = 500
        return {
            "status": "error",
            "message": "An unexpected error occurred",
            "error": str(e)
        }


@frappe.whitelist(allow_guest=True)
def search_schools_by_city():
    """
    Search schools by city name

    Expected JSON payload:
    {
        "api_key": "your_api_key",
        "city_name": "city_name_to_search"
    }
    """
    try:
        # Get the JSON data from the request body
        data = json.loads(frappe.request.data)
        api_key = data.get('api_key')
        city_name = data.get('city_name')

        # Validate API key
        if not api_key:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "API key is required"}

        if not authenticate_api_key(api_key):
            frappe.response.http_status_code = 401
            return {"status": "error", "message": "Invalid API key"}

        # Validate required fields
        if not city_name:
            frappe.response.http_status_code = 400
            return {"status": "error", "message": "city_name is required"}

        # Find city by name
        city = frappe.get_all(
            "City",
            filters={"city_name": city_name},
            fields=["name", "city_name", "district"]
        )

        if not city:
            frappe.response.http_status_code = 404
            return {
                "status": "error",
                "message": f"No city found with name: {city_name}"
            }

        city_id = city[0].name

        # Find all schools in this city
        schools = frappe.get_all(
            "School",
            filters={"city": city_id},
            fields=[
                "name", "name1", "type", "board", "status",
                "address", "pin", "headmaster_name", "headmaster_phone"
            ],
            order_by="name1"
        )

        # Get district and state information
        district_name = None
        state_name = None
        if city[0].district:
            district_doc = frappe.get_doc("District", city[0].district)
            district_name = district_doc.district_name
            if district_doc.state:
                state_doc = frappe.get_doc("State", district_doc.state)
                state_name = state_doc.state_name

        frappe.response.http_status_code = 200
        return {
            "status": "success",
            "message": f"Found {len(schools)} schools in {city_name}",
            "data": {
                "city": {
                    "id": city_id,
                    "name": city_name,
                    "district": district_name,
                    "state": state_name
                },
                "school_count": len(schools),
                "schools": schools
            }
        }

    except Exception as e:
        frappe.log_error(f"Search Schools by City Error: {str(e)}", "Search Schools API")
        frappe.response.http_status_code = 500
        return {
            "status": "error",
            "message": "An unexpected error occurred",
            "error": str(e)
        }


#akshay modified for glific chat bot ( Teacher Ativity)
@frappe.whitelist(allow_guest=True)
def glific_get_courses():
    try:
        # Fetch all courses sorted by vertical_id
        data = frappe.get_all(
            "Course Verticals",
            fields=["name", "name1", "name2", "vertical_id"],
            order_by="vertical_id asc"
        )

        # Format: 1. Name / 2. Name
        lines = []
        for idx, d in enumerate(data, start=1):
            lines.append(f"{d.name} ({d.vertical_id})")

        return {
            "courses_list": "\n".join(lines),
            "courses": data
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Glific Get Courses Error")
        return {"error": str(e)}


# your_app/api/glific_webhook.py

import frappe
import json

@frappe.whitelist(allow_guest=True)
def glific_list_grades(vertical: str = None):

    # ------------------------------------------------------------------
    # 1. Input validation
    # ------------------------------------------------------------------
    if not vertical:
        return {"error": "Missing required parameter: vertical"}

    vertical = frappe.utils.strip_html(vertical).strip()

    # ------------------------------------------------------------------
    # 2. Fetch all Course Levels for this vertical
    # ------------------------------------------------------------------
    course_levels = frappe.db.get_all(
        "Course Level",
        filters={"vertical": vertical},
        fields=["name", "stage", "name1"],  # name1 is your display field
        order_by="name"
    )

    if not course_levels:
        return {"results": []}

    results = []

    for cl in course_levels:
        stage_name = cl.get("stage")
        if not stage_name:
            continue

        try:
            stage = frappe.get_doc("Stage Grades", stage_name)
        except frappe.DoesNotExistError:
            continue

        from_grade_raw = stage.from_grade or ""
        to_grade_raw = stage.to_grade

        # Convert to integers safely
        try:
            from_grades = [int(x.strip()) for x in str(from_grade_raw).split(",") if x.strip()]
            to_grade = int(to_grade_raw) if to_grade_raw else None
        except (ValueError, TypeError):
            from_grades = []
            to_grade = None

        # ------------------------------------------------------------------
        # Smart Grade Range Logic: Expand 6→8 as 6,7,8
        # ------------------------------------------------------------------
        if to_grade is not None and from_grades:
            # Take the last from_grade as start (common pattern), or first
            start = from_grades[-1] if from_grades else to_grade
            end = to_grade

            # If it's a continuous range like 6 to 8 → include all in between
            if end >= start:
                expanded_grades = list(range(start, end + 1))
            else:
                expanded_grades = from_grades + [to_grade]  # fallback
        else:
            expanded_grades = from_grades

        # Remove duplicates and sort
        expanded_grades = sorted(set(expanded_grades))

        # Build final list of "Grade X"
        grade_parts = [f"{g}" for g in expanded_grades]

        # Format final string: "Grade 6,7 & 8" or "Grade 6,7 & 10"
        if len(grade_parts) > 1:
            grade_range = ", ".join(grade_parts[:-1]) + " & " + grade_parts[-1]
        elif len(grade_parts) == 1:
            grade_range = grade_parts[0]
        else:
            grade_range = "—"



        results.append({
            "label": f"Grade {grade_range}",
            "value": cl.name1 or cl.name
        })

    return {"results": results}

@frappe.whitelist(allow_guest=True)
def get_batch_keywords_by_phone(api_key,phone_number):
    """
    Get batch keywords for a teacher by phone number
    Logic:
    1. Get teacher by phone number
    2. Get their school_id
    3. Find batch onboarding records with that school
    4. Get the latest modified batch onboarding
    5. Return its keywords
    """
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")
    
    try:
        if not phone_number:
            return {
                "success": False,
                "message": "Phone number is required"
            }
        
        # Step 1: Get teacher by phone number
        teachers = frappe.get_all(
            "Teacher",
            filters={"phone_number": phone_number},
            fields=["name", "first_name", "last_name", "school_id", "phone_number"],
            limit=1
        )
        
        if not teachers:
            return {
                "success": False,
                "message": f"No teacher found with phone number {phone_number}"
            }
        
        teacher = teachers[0]
        school_id = teacher.school_id
        
        if not school_id:
            return {
                "success": False,
                "message": "Teacher does not have a school assigned"
            }
        
        # Step 2 & 3: Get batch onboarding records for this school, ordered by modified date
        batch_onboarding_records = frappe.get_all(
            "Batch onboarding",
            filters={"school": school_id},
            fields=["name",  "batch_skeyword"],
            order_by="modified desc",
            limit=1
        )
        
        if not batch_onboarding_records:
            return {
                "success": False,
                "message": f"No batch onboarding found for school {school_id}"
            }
        
        # Step 4: Get the latest modified batch onboarding
        latest_batch_onboarding = batch_onboarding_records[0]
        
        # Step 5: Return the keywords
        return {
            "success": True,
            "teacher_name": f"{teacher.first_name or ''} {teacher.last_name or ''}".strip(),
            "phone_number": canonicalize_response_phone(teacher.phone_number or phone_number),
            "school_id": school_id,
            "batch_onboarding_id": latest_batch_onboarding.name,
            "batch_id": latest_batch_onboarding.batch,
            "keywords": latest_batch_onboarding.batch_skeyword or "",
            "modified": latest_batch_onboarding.modified
        }
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Get Batch Keywords By Phone Error")
        return {
            "success": False,
            "message": str(e)
        }

# -*- coding: utf-8 -*-
# Copyright (c) 2025, DalgoT4D and contributors
# For license information, please see license.txt

"""
API to get YouTube URL based on course vertical, language, and grade
This follows the data flow through Grade Course Level Mapping, Course Level,
Batch, Learning Unit, and VideoClass doctypes.
"""

#from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.utils import cint, flt, today, getdate, add_days
from datetime import datetime, timedelta


@frappe.whitelist(allow_guest=True)
def get_youtube_url(api_key, course_vertical, phone_number, language, grade, batch_keyword=None):
    """
    Get YouTube URL based on course vertical, language, and grade.
    
    Args:
        course_vertical (str): Course vertical name (e.g., "Coding")
        language (str): Language for video translation (e.g., "English")
        grade (int/str): Grade number (e.g., 11)
        batch_keyword (str, optional): Batch keyword for week calculation
    
    Returns:
        dict: Contains youtube_url, learning_unit, week_no, and other details
        
    Example:
        frappe.call({
            method: "frappe_tap.api.get_youtube_url",
            args: {
                course_vertical: "Coding",
                language: "English",
                grade: 11,
                batch_keyword: "BLBA10UV"
            }
        })
    """
    try:
        # Validate inputs
        if not authenticate_api_key(api_key):
            frappe.throw("Invalid API key")
        if not course_vertical:
            return error_response("Course Vertical is required")
        if not language:
            return error_response("Language is required")
        if not grade:
            return error_response("Grade is required")
            
        # Convert grade to integer if string
        try:
            grade = cint(grade)
        except:
            return error_response("Grade must be a valid number")
        
        # Step 1: Get Assigned Course Level from Grade Course Level Mapping
        course_level = get_course_level_from_mapping(course_vertical, grade)
        
        if not course_level:
            return error_response(
                _("No course level mapping found for Course Vertical: {0} and Grade: {1}").format(
                    course_vertical, grade
                )
            )
        
        # Step 2: Calculate current week number if batch_keyword provided
        current_week = None
        if batch_keyword:
            try:
                current_week = get_current_week_from_teacher_flow(phone_number, batch_keyword)
            except Exception as e:
                return error_response(str(e))
        
        # Step 3: Get learning unit based on week number
        learning_unit_data = get_learning_unit_by_week(course_level, current_week)
        
        if not learning_unit_data:
            week_msg = f"week {current_week}" if current_week else "this course level"
            return error_response(
                _("No learning unit found for {0} in course level {1}").format(
                    week_msg, course_level
                )
            )
        
        learning_unit = learning_unit_data.get("learning_unit")
        week_no = learning_unit_data.get("week_no")
        
        # Step 4: Get VideoClass content from learning unit
        video_class_name = get_video_class_from_learning_unit(learning_unit)
        
        if not video_class_name:
            return error_response(
                _("No VideoClass content found in learning unit {0}").format(learning_unit)
            )
        
        # Step 5: Get YouTube URL from VideoClass based on language
        youtube_url = get_youtube_url_from_video_class(video_class_name, language)
        
        if not youtube_url:
            return error_response(
                _("No YouTube URL found for language '{0}' in VideoClass {1}").format(
                    language, video_class_name
                )
            )
        
        # Return complete information

        if week_no == current_week :
            return {
                "success": True,
                "data": {
                    "course_level": course_level,
                    "learning_unit": learning_unit,
                    "week_no": week_no,
                    "current_week": current_week,
                    "video_class": video_class_name,
                    "language": language,
                    "youtube_url": youtube_url
                }
            }
        else:
            return {
                "success": True,
                "data": {
                    "course_level": course_level,
                    "learning_unit": learning_unit,
                    "week_no": week_no,
                    "current_week": current_week,
                    "video_class": video_class_name,
                    "language": language,
                    "youtube_url": None
                }
            }
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Get YouTube URL Error"))
        return error_response(str(e))


def get_course_level_from_mapping(course_vertical, grade, student_type=None):
    """
    Get Assigned Course Level from Grade Course Level Mapping.
    Determines academic year automatically and uses student type if provided.
    
    Academic Year Logic:
    - If current month is April (4) or later: Use current year (e.g., 2025-26)
    - If current month is before April: Use previous year (e.g., 2024-25)
    
    Args:
        course_vertical (str): Course vertical name
        grade (int): Grade number
        student_type (str, optional): "New" or "Old". If not provided, tries both.
    
    Returns:
        str: Course level name or None
    """
    try:
        from frappe.utils import today, getdate
        
        current_date = getdate(today())
        current_year = current_date.year
        current_month = current_date.month
        
        # Determine academic year based on current month
        if current_month >= 4:  # April or later
            academic_year = f"{current_year}-{str(current_year + 1)[-2:]}"
        else:  # January to March
            academic_year = f"{current_year - 1}-{str(current_year)[-2:]}"
        
        # # If student_type is provided, use it
        # if student_type:
        #     mapping = frappe.db.get_value(
        #         "Grade Course Level Mapping",
        #         filters={
        #             "course_vertical": course_vertical,
        #             "grade": grade,
        #             "academic_year": academic_year,
        #             "student_type": student_type,
        #             "is_active": 1
        #         },
        #         fieldname="assigned_course_level"
        #     )
        #     return mapping
        
        # # If student_type not provided, try both "Old" and "New"
        # # Try "Old" first
        # mapping = frappe.db.get_value(
        #     "Grade Course Level Mapping",
        #     filters={
        #         "course_vertical": course_vertical,
        #         "grade": grade,
        #         "academic_year": academic_year,
        #         "student_type": "Old",
        #         "is_active": 1
        #     },
        #     fieldname="assigned_course_level"
        # )
        
        # # If not found with "Old", try "New"
        # if not mapping:
        mapping = frappe.db.get_value(
                "Grade Course Level Mapping",
                filters={
                    "course_vertical": course_vertical,
                    "grade": grade,
                    "academic_year": academic_year,
                    "student_type": "New",
                    "is_active": 1
                },
                fieldname="assigned_course_level"
            )
        
        return mapping
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Get Course Level Mapping Error"))
        return None

def get_current_week_from_teacher_flow(phone_number, batch_keyword):
    """
    Get current week number from WeeklyTeacherFlow Teachers child table.
    
    Args:
        phone_number (str): Teacher's phone number
        batch_keyword (str): Batch keyword
    
    Returns:
        int: Current week number or None
        
    Raises:
        frappe.DoesNotExistError: If teacher record not found
    """
    try:
        # First, get the parent WeeklyTeacherFlow document that matches the week range
        # Then query the child table for the specific teacher
        
        # Option 1: Query using SQL-like approach with child table
        from frappe.utils import today, getdate
        from frappe import _

        current_date = getdate(today())
        
        # Get all WeeklyTeacherFlow documents (or filter by date if needed)
        teacher_flows = frappe.get_all(
            "WeeklyTeacherFlow",
            filters={
                "week_start_date": ["<=", current_date],
                "week_end_date": [">=", current_date]
            },
            fields=["name", "week_start_date", "week_end_date"]
        )
        
        # For each flow, check if it has a teacher with matching phone and batch
        for flow in teacher_flows:
            # Get the full document to access child table
            flow_doc = frappe.get_doc("WeeklyTeacherFlow", flow.name)
            
            # Check if teachers child table exists
            if hasattr(flow_doc, 'teachers'):
                for teacher in flow_doc.teachers:
                    if (teacher.phone_number == phone_number and 
                        teacher.batch_keyword == batch_keyword):
                        # Found the matching teacher record
                        if hasattr(teacher, 'current_week_no'):
                            return teacher.current_week_no
                        else:
                            raise frappe.DoesNotExistError(
                                _("Current Week No field not found in teacher record")
                            )
        
        # If we reach here, no matching record was found
        raise frappe.DoesNotExistError(
            _("Teacher record not found for phone number '{0}' and batch keyword '{1}'").format(
                phone_number, batch_keyword
            )
        )
        
    except frappe.DoesNotExistError:
        raise
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Get Current Week from Teacher Flow Error"))
        raise Exception(_("Error getting current week: {0}").format(str(e)))

def calculate_current_week(batch_keyword):
    """
    Calculate current week number based on batch Regular Activity Start Date.
    
    The regular_activity_start_date is considered as the start of Week 1.
    
    Example:
    - regular_activity_start_date = 2025-01-11 (Saturday)
    - Week 1: 2025-01-11 to 2025-01-17
    - Week 2: 2025-01-18 to 2025-01-24
    - If today is 2025-01-20, it falls in Week 2
    
    Args:
        batch_keyword (str): Batch keyword to search in Batch Onboarding
    
    Returns:
        tuple: (week_number, regular_activity_start_date)
        
    Raises:
        frappe.DoesNotExistError: If batch onboarding or batch not found
    """
    try:
        from frappe.utils import getdate, today
        
        # Step 1: Find Batch Onboarding by batch_keyword
        batch_onboarding = frappe.db.get_value(
            "Batch onboarding",
            filters={
                "batch_skeyword": batch_keyword  # Fixed: was "batch_skeyword"
            },
            fieldname=["batch", "name"],
            as_dict=True
        )
        
        if not batch_onboarding:
            raise frappe.DoesNotExistError(
                _("Batch Onboarding with keyword '{0}' not found").format(
                    batch_keyword
                )
            )
        
        # Step 2: Get the batch name from Batch Onboarding
        batch_name = batch_onboarding.batch
        
        if not batch_name:
            raise frappe.DoesNotExistError(
                _("No Batch linked in Batch Onboarding with keyword '{0}'").format(
                    batch_keyword
                )
            )
        
        # Step 3: Get regular_activity_start_date from Batch
        batch_data = frappe.db.get_value(
            "Batch",
            filters={
                "name": batch_name
            },
            fieldname=["regular_activity_start_date", "name"],
            as_dict=True
        )
        
        if not batch_data or not batch_data.regular_activity_start_date:
            raise frappe.DoesNotExistError(
                _("Batch '{0}' not found or Regular Activity Start Date not set").format(
                    batch_name
                )
            )
        
        # Get start date and current date
        start_date = batch_data.regular_activity_start_date
        current_date = getdate(today())
        
        # Convert to date object if string
        if isinstance(start_date, str):
            start_date = getdate(start_date)
        
        # Calculate days elapsed from start date
        delta_days = (current_date - start_date).days
        
        # If current date is before start date, return week 1
        if delta_days < 0:
            return 1, start_date
        
        # Calculate week number
        # Week 1 starts on regular_activity_start_date (day 0-6)
        # Week 2 starts on day 7 (day 7-13)
        # Week 3 starts on day 14 (day 14-20)
        # Formula: week_no = (days_elapsed // 7) + 1
        week_no = (delta_days // 7) + 1
        
        return week_no, start_date
        
    except frappe.DoesNotExistError:
        raise
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Calculate Week Error"))
        raise Exception(_("Error calculating week: {0}").format(str(e)))




def get_learning_unit_by_week(course_level, current_week_no):
    """
    Get learning unit from Course Level based on current week number.
    Matches the week_no from learning_units child table with current_week_no.
    
    Args:
        course_level (str): Course level name
        current_week_no (int): Current week number from WeeklyTeacherFlow
    
    Returns:
        dict: {"learning_unit": str, "week_no": int} or None
    """
    try:
        from frappe.utils import cint
        
        # Get the Course Level document
        course_level_doc = frappe.get_doc("Course Level", course_level)
        
        # Check if learning_units child table exists
        if not hasattr(course_level_doc, 'learning_units'):
            frappe.log_error(
                f"Course Level '{course_level}' does not have 'learning_units' child table",
                _("Get Learning Unit Error")
            )
            return None
        
        # Get all learning units from child table
        learning_units = []
        for lu in course_level_doc.learning_units:
            learning_units.append({
                "learning_unit": lu.learning_unit,
                "week_no": cint(lu.week_no)
            })
        
        if not learning_units:
            frappe.log_error(
                f"No learning units found in Course Level '{course_level}'",
                _("Get Learning Unit Error")
            )
            return None
        
        # If current_week_no is provided, find exact match
        if current_week_no is not None:
            current_week_no = cint(current_week_no)
            
            # First, try to find exact match
            for lu in learning_units:
                if lu.get("week_no") == current_week_no:
                    return lu
            
        return learning_units[0]
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Get Learning Unit Error"))
        return None


def get_video_class_from_learning_unit(learning_unit):
    """
    Get VideoClass content from Learning Unit content items.
    
    Args:
        learning_unit (str): Learning unit name
    
    Returns:
        str: VideoClass name or None
    """
    try:
        # Get the learning unit document
        lu_doc = frappe.get_doc("LearningUnit", learning_unit)
        
        # Check if there's a content_items child table
        if hasattr(lu_doc, 'content_items'):
            for item in lu_doc.content_items:
                if item.content_type == "VideoClass":
                    return item.content
        
        return None
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Get VideoClass Error"))
        return None


def get_youtube_url_from_video_class(video_class_name, language):
    """
    Get YouTube URL from VideoClass based on language.
    
    Args:
        video_class_name (str): VideoClass document name
        language (str): Language for translation
    
    Returns:
        str: YouTube URL or None
    """
    try:
        # Get the VideoClass document
        vc_doc = frappe.get_doc("VideoClass", video_class_name)
        
        # Check if there's a video_translations child table
        if hasattr(vc_doc, 'video_translations'):
            for translation in vc_doc.video_translations:
                if translation.language == language:
                    return translation.video_youtube_url
        
        return None
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Get YouTube URL from VideoClass Error"))
        return None


def error_response(message):
    """
    Create standardized error response.
    
    Args:
        message (str): Error message
    
    Returns:
        dict: Error response
    """
    return {
        "success": False,
        "error": message,
        "data": None
    }


# Alternative JSON-based API endpoint
# @frappe.whitelist(allow_guest=False)
# def get_youtube_url_json(**kwargs):
#     """
#     Get YouTube URL - JSON version for easier API calls.
#     Accepts parameters as JSON body or form data.
    
#     Example POST request:
#         POST /api/method/frappe_tap.api.get_youtube_url_json
#         {
#             "course_vertical": "Coding",
#             "language": "English",
#             "grade": 11,
#             "batch_keyword": "BLBA10UV"
#         }
#     """
#     course_vertical = kwargs.get("course_vertical")
#     language = kwargs.get("language")
#     grade = kwargs.get("grade")
#     batch_keyword = kwargs.get("batch_keyword")
    
#     return get_youtube_url(course_vertical, language, grade, batch_keyword)


# # Utility function to get available languages for a course
# @frappe.whitelist(allow_guest=False)
# def get_available_languages(course_vertical, grade):
#     """
#     Get list of available languages for a given course vertical and grade.
    
#     Args:
#         course_vertical (str): Course vertical name
#         grade (int): Grade number
    
#     Returns:
#         dict: List of available languages
#     """
#     try:
#         # Get course level
#         course_level = get_course_level_from_mapping(course_vertical, grade)
        
#         if not course_level:
#             return error_response("No course level mapping found")
        
#         # Get all learning units
#         course_level_doc = frappe.get_doc("Course Level", course_level)
        
#         languages = set()
        
#         if hasattr(course_level_doc, 'learning_units'):
#             for lu_row in course_level_doc.learning_units:
#                 lu_doc = frappe.get_doc("LearningUnit", lu_row.learning_unit)
                
#                 if hasattr(lu_doc, 'content_items'):
#                     for item in lu_doc.content_items:
#                         if item.content_type == "VideoClass":
#                             vc_doc = frappe.get_doc("VideoClass", item.content)
                            
#                             if hasattr(vc_doc, 'video_translations'):
#                                 for translation in vc_doc.video_translations:
#                                     if translation.language:
#                                         languages.add(translation.language)
        
#         return {
#             "success": True,
#             "data": {
#                 "course_vertical": course_vertical,
#                 "grade": grade,
#                 "course_level": course_level,
#                 "available_languages": sorted(list(languages))
#             }
#         }
        
#     except Exception as e:
#         frappe.log_error(frappe.get_traceback(), _("Get Available Languages Error"))
#         return error_response(str(e))


# # Utility function to get learning units for a course
# @frappe.whitelist(allow_guest=False)
# def get_learning_units(course_vertical, grade):
#     """
#     Get all learning units for a given course vertical and grade.
    
#     Args:
#         course_vertical (str): Course vertical name
#         grade (int): Grade number
    
#     Returns:
#         dict: List of learning units with week numbers
#     """
#     try:
#         # Get course level
#         course_level = get_course_level_from_mapping(course_vertical, grade)
        
#         if not course_level:
#             return error_response("No course level mapping found")
        
#         # Get all learning units
#         course_level_doc = frappe.get_doc("Course Level", course_level)
        
#         learning_units = []
        
#         if hasattr(course_level_doc, 'learning_units'):
#             for lu in course_level_doc.learning_units:
#                 learning_units.append({
#                     "learning_unit": lu.learning_unit,
#                     "week_no": lu.week_no,
#                     "idx": lu.idx
#                 })
        
#         return {
#             "success": True,
#             "data": {
#                 "course_vertical": course_vertical,
#                 "grade": grade,
#                 "course_level": course_level,
#                 "learning_units": learning_units,
#                 "total_units": len(learning_units)
#             }
#         }
        
#     except Exception as e:
#         frappe.log_error(frappe.get_traceback(), _("Get Learning Units Error"))
#         return error_response(str(e))



# -*- coding: utf-8 -*-
# Copyright (c) 2024, TAP LMS and contributors
# For license information, please see license.txt

"""
Student Lookup API - Add to tap_lms/api.py or tap_lms/journey/api.py
"""

import frappe
from frappe import _


def normalize_phone_number(phone):
    """
    Normalize phone number to handle both 10-digit and 12-digit formats
    """
    if not phone:
        return None, None
    
    phone = str(phone).strip().replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    phone = ''.join(filter(str.isdigit, phone))
    
    if len(phone) == 10:
        phone_12 = f"91{phone}"
        phone_10 = phone
    elif len(phone) == 12 and phone.startswith('91'):
        phone_12 = phone
        phone_10 = phone[2:]
    elif len(phone) == 11 and phone.startswith('1'):
        phone_12 = f"9{phone}"
        phone_10 = phone[1:]
    else:
        return None, None
    
    return phone_12, phone_10


@frappe.whitelist(allow_guest=True)
def get_student_by_phone_and_name(api_key, phone_number, buddy_name):
    """
    Get student details by phone number and name
    
    Args:
        phone_number (str): Phone number (10 or 12 digits)
        buddy_name (str): Student's name
        
    Returns:
        dict: Student information including course_level, batch, phone_number, student_id
    """
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")
    try:
        # Validate inputs
        if not phone_number or not buddy_name:
            return {
                "success": False,
                "error": "Both phone_number and buddy_name are required"
            }
        
        # Normalize phone number
        phone_12, phone_10 = normalize_phone_number(phone_number)
        
        if not phone_12 or not phone_10:
            return {
                "success": False,
                "error": "Invalid phone number format. Use 10 digits or 12 digits with 91 prefix"
            }
        
        # Search for student by name and phone
        students = frappe.get_all(
            "Student",
            filters=[
                ["name1", "=", buddy_name],
                ["phone", "in", [phone_10, phone_12]]
            ],
            fields=["name", "name1", "phone", "grade", "school_id", "status"],
            limit=1
        )
        
        if not students:
            return {
                "success": False,
                "error": f"No student found with name '{buddy_name}' and phone number '{phone_number}'"
            }
        
        student = students[0]
        
        # Get student document to access enrollment child table
        student_doc = frappe.get_doc("Student", student.name)
        
        # Check if student has enrollments
        if not student_doc.enrollment or len(student_doc.enrollment) == 0:
            return {
                "success": True,
                "data": {
                    "student_id": student.name,
                    "phone_number": canonicalize_response_phone(student.phone),
                    "buddy_name": student.name1,
                    "current_grade": student.grade,
                    "current_school": student.school_id,
                    "status": student.status,
                    "course_level": None,
                    "batch": None,
                    "batch_name": None,
                    "message": "Student found but has no enrollments"
                }
            }
        
        # Get the latest enrollment (sorted by date_joining)
        latest_enrollment = None
        for enrollment in student_doc.enrollment:
            if not latest_enrollment:
                latest_enrollment = enrollment
            elif enrollment.date_joining and latest_enrollment.date_joining:
                if enrollment.date_joining > latest_enrollment.date_joining:
                    latest_enrollment = enrollment
        
        # Get batch details if batch exists
        batch_name = None
        batch_active = None
        if latest_enrollment and latest_enrollment.batch:
            try:
                batch_doc = frappe.get_doc("Batch", latest_enrollment.batch)
                batch_name = batch_doc.name1 if hasattr(batch_doc, 'name1') else None
                batch_active = batch_doc.active if hasattr(batch_doc, 'active') else None
            except:
                pass
        
        # Return response
        return {
            "success": True,
            "data": {
                "student_id": student.name,
                "phone_number": canonicalize_response_phone(student.phone),
                "buddy_name": student.name1,
                "course_level": latest_enrollment.course if latest_enrollment else None,
                "batch": latest_enrollment.batch if latest_enrollment else None,
                "batch_name": batch_name,
                "batch_active": bool(batch_active) if batch_active is not None else None,
                #"current_grade": student.grade,
                "enrollment_grade": latest_enrollment.grade if latest_enrollment else None,
                "enrollment_school": latest_enrollment.school if latest_enrollment else None,
                "date_joining": str(latest_enrollment.date_joining) if latest_enrollment and latest_enrollment.date_joining else None,
                "status": student.status
            }
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error in get_student_by_phone_and_name: {str(e)}\n"
            f"Phone: {phone_number}, Name: {buddy_name}",
            "Get Student API Error"
        )
        return {
            "success": False,
            "error": f"Internal server error: {str(e)}"
        }


@frappe.whitelist(allow_guest=False)
def get_student_all_enrollments(phone_number, buddy_name):
    """
    Get student with all their enrollments
    
    Args:
        phone_number (str): Phone number (10 or 12 digits)
        buddy_name (str): Student's name
        
    Returns:
        dict: Student information with list of all enrollments
    """
    try:
        # Validate inputs
        if not phone_number or not buddy_name:
            return {
                "success": False,
                "error": "Both phone_number and buddy_name are required"
            }
        
        # Normalize phone number
        phone_12, phone_10 = normalize_phone_number(phone_number)
        
        if not phone_12 or not phone_10:
            return {
                "success": False,
                "error": "Invalid phone number format"
            }
        
        # Search for student
        students = frappe.get_all(
            "Student",
            filters=[
                ["name1", "=", buddy_name],
                ["phone", "in", [phone_10, phone_12]]
            ],
            fields=["name", "name1", "phone", "grade", "school_id", "status"],
            limit=1
        )
        
        if not students:
            return {
                "success": False,
                "error": f"No student found with name '{buddy_name}' and phone number '{phone_number}'"
            }
        
        student = students[0]
        
        # Get student document with enrollments
        student_doc = frappe.get_doc("Student", student.name)
        
        # Build enrollment list
        enrollment_list = []
        if student_doc.enrollment:
            for enrollment in student_doc.enrollment:
                # Get batch details
                batch_name = None
                batch_active = None
                batch_start_date = None
                batch_end_date = None
                
                if enrollment.batch:
                    try:
                        batch_doc = frappe.get_doc("Batch", enrollment.batch)
                        batch_name = batch_doc.name1 if hasattr(batch_doc, 'name1') else None
                        batch_active = batch_doc.active if hasattr(batch_doc, 'active') else None
                        batch_start_date = str(batch_doc.start_date) if hasattr(batch_doc, 'start_date') and batch_doc.start_date else None
                        batch_end_date = str(batch_doc.end_date) if hasattr(batch_doc, 'end_date') and batch_doc.end_date else None
                    except:
                        pass
                
                enrollment_list.append({
                    "course_level": enrollment.course,
                    "batch": enrollment.batch,
                    "batch_name": batch_name,
                    "batch_active": bool(batch_active) if batch_active is not None else None,
                    "batch_start_date": batch_start_date,
                    "batch_end_date": batch_end_date,
                    "enrollment_grade": enrollment.grade,
                    "enrollment_school": enrollment.school,
                    "date_joining": str(enrollment.date_joining) if enrollment.date_joining else None
                })
        
        return {
            "success": True,
            "data": {
                "student_id": student.name,
                "phone_number": canonicalize_response_phone(student.phone),
                "buddy_name": student.name1,
                "current_grade": student.grade,
                "current_school": student.school_id,
                "status": student.status,
                "total_enrollments": len(enrollment_list),
                "enrollments": enrollment_list
            }
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error in get_student_all_enrollments: {str(e)}\n"
            f"Phone: {phone_number}, Name: {buddy_name}",
            "Get Student API Error"
        )
        return {
            "success": False,
            "error": f"Internal server error: {str(e)}"
        }


@frappe.whitelist(allow_guest=True)
def get_student_video_content(api_key, course_level, batch, student_id, language, content_type):
    """
    Get video content for a student based on their current week
    
    Args:
        course_level (str): Course Level ID
        batch (str): Batch ID  
        student_id (str): Student ID
        language (str): Language code/ID (TAP Language)
        content_type (str): "Youtube" or "Plio"
        
    Returns:
        dict: Video information including name, link, and ID
    """
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")
    try:
        # Validate inputs
        if not all([course_level, batch, student_id, language, content_type]):
            return {
                "success": False,
                "error": "All parameters are required: course_level, batch, student_id, language, content_type"
            }
        
        # Validate content_type
        if content_type not in ["Youtube", "Plio"]:
            return {
                "success": False,
                "error": "content_type must be either 'Youtube' or 'Plio'"
            }
        
        # Step 1: Get current week number for the student
        current_week = get_student_current_week(student_id, batch)
        
        if not current_week:
            return {
                "success": False,
                "error": f"Could not find current week for student {student_id} in batch {batch}"
            }
        
        # Step 2: Get learning units for this course level and week
        learning_units = get_learning_units_by_week(course_level, current_week)
        
        if not learning_units:
            return {
                "success": False,
                "error": f"No learning units found for course level {course_level} week {current_week}"
            }
        
        # Step 3: Get video classes from learning units
        videos = []
        for learning_unit in learning_units:
            video_classes = get_video_classes_from_unit(learning_unit)
            
            for video_class in video_classes:
                # Step 4: Get video translation or original video
                video_info = get_video_with_translation(
                    video_class, 
                    language, 
                    content_type
                )
                
                if video_info:
                    videos.append(video_info)
        
        if not videos:
            return {
                "success": False,
                "error": f"No videos found for the given parameters"
            }
        
        # Return single video if only one, otherwise return list
        return {
            "success": True,
            "current_week": current_week,
            "video_count": len(videos),
            "data": videos if len(videos) > 1 else videos[0]
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error in get_student_video_content: {str(e)}\n"
            f"course_level: {course_level}, batch: {batch}, student: {student_id}",
            "Student Video Content API Error"
        )
        return {
            "success": False,
            "error": f"Internal server error: {str(e)}"
        }


def get_student_current_week(student_id, batch):
    """
    Get the current week number for a student from Weekly Student Flow
    
    Args:
        student_id: Student ID
        batch: Batch ID
        
    Returns:
        int: Current week number or None
    """
    try:
        # Find Weekly Student Flow for this batch
        current_date = getdate()  # Today's date

        weekly_flows = frappe.get_all(
            "Weekly Student Flow",
            filters=[
                ["week_end_date", ">=", current_date]
            ],
            fields=["name", "week_start_date", "week_end_date"],
            limit=1
        )
        
        if not weekly_flows:
            frappe.log_error(
                f"No Weekly Student Flow found for batch {batch}",
                "Student Video Content API"
            )
            return None
        
        # Get the document
        flow_doc = frappe.get_doc("Weekly Student Flow", weekly_flows[0].name)
        
        # Search for student in the child table
        if hasattr(flow_doc, 'students') and flow_doc.students:
            for student_row in flow_doc.students:
                if student_row.student == student_id:
                    return student_row.current_week_no
        
        frappe.log_error(
            f"Student {student_id} not found in Weekly Student Flow for batch {batch}",
            "Student Video Content API"
        )
        return None
        
    except Exception as e:
        frappe.log_error(
            f"Error getting current week: {str(e)}",
            "Student Video Content API"
        )
        return None


def get_learning_units_by_week(course_level, week_no):
    """
    Get learning units for a specific course level and week number
    
    Args:
        course_level: Course Level ID
        week_no: Week number
        
    Returns:
        list: List of learning unit IDs
    """
    try:
        # Get Course Level document
        course_doc = frappe.get_doc("Course Level", course_level)
        
        learning_units = []
        
        # Check if learning_units child table exists
        if hasattr(course_doc, 'learning_units') and course_doc.learning_units:
            for unit_row in course_doc.learning_units:
                if unit_row.week_no == week_no and unit_row.learning_unit:
                    learning_units.append(unit_row.learning_unit)
        
        return learning_units
        
    except Exception as e:
        frappe.log_error(
            f"Error getting learning units: {str(e)}",
            "Student Video Content API"
        )
        return []


def get_video_classes_from_unit(learning_unit_id):
    """
    Get VideoClass IDs from a learning unit's content items
    
    Args:
        learning_unit_id: LearningUnit ID
        
    Returns:
        list: List of VideoClass IDs
    """
    try:
        # Get LearningUnit document
        unit_doc = frappe.get_doc("LearningUnit", learning_unit_id)
        
        video_classes = []
        
        # Check content_items child table
        if hasattr(unit_doc, 'content_items') and unit_doc.content_items:
            for content_item in unit_doc.content_items:
                if content_item.content_type == "VideoClass" and content_item.content:
                    video_classes.append(content_item.content)
        
        return video_classes
        
    except Exception as e:
        frappe.log_error(
            f"Error getting video classes from unit: {str(e)}",
            "Student Video Content API"
        )
        return []


def get_video_with_translation(video_class_id, language, content_type):
    """
    Get video information with translation if available
    
    Args:
        video_class_id: VideoClass ID
        language: Language for translation
        content_type: "Youtube" or "Plio"
        
    Returns:
        dict: Video information with name, link, ID
    """
    try:
        # Get VideoClass document
        video_doc = frappe.get_doc("VideoClass", video_class_id)
        
        video_name = video_doc.video_name
        video_link = None
        translated = False
        
        # Try to find translation for the specified language
        if hasattr(video_doc, 'video_translations') and video_doc.video_translations:
            for translation in video_doc.video_translations:
                if translation.language == language:
                    # Use translated name if available
                    if translation.translated_name:
                        video_name = translation.translated_name
                        translated = True
                    
                    # Get URL based on content_type
                    if content_type == "Youtube" and translation.video_youtube_url:
                        video_link = translation.video_youtube_url
                        break
                    elif content_type == "Plio" and translation.video_plio_url:
                        video_link = translation.video_plio_url
                        break
        
        # Fallback to original video if no translation found
        if not video_link:
            if content_type == "Youtube":
                video_link = video_doc.video_youtube_url
            elif content_type == "Plio":
                video_link = video_doc.video_plio_url
        
        # Skip if no link available
        if not video_link:
            return None
        
        return {
            "video_id": video_class_id,
            "video_name": video_name,
            "video_link": video_link,
            "content_type": content_type,
            "language": language,
            "translated": translated
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error getting video with translation: {str(e)}",
            "Student Video Content API"
        )
        return None


@frappe.whitelist(allow_guest=False)
def get_student_all_week_videos(course_level, batch, student_id, language, content_type):
    """
    Get all videos for all weeks (not just current week)
    
    Args:
        course_level (str): Course Level ID
        batch (str): Batch ID
        student_id (str): Student ID
        language (str): Language code/ID
        content_type (str): "Youtube" or "Plio"
        
    Returns:
        dict: Videos grouped by week number
    """
    try:
        # Validate inputs
        if not all([course_level, batch, student_id, language, content_type]):
            return {
                "success": False,
                "error": "All parameters are required"
            }
        
        # Get current week for context
        current_week = get_student_current_week(student_id, batch)
        
        # Get Course Level document
        course_doc = frappe.get_doc("Course Level", course_level)
        
        # Group learning units by week
        weeks_data = {}
        
        if hasattr(course_doc, 'learning_units') and course_doc.learning_units:
            for unit_row in course_doc.learning_units:
                week_no = unit_row.week_no
                
                if week_no not in weeks_data:
                    weeks_data[week_no] = []
                
                # Get videos for this unit
                video_classes = get_video_classes_from_unit(unit_row.learning_unit)
                
                for video_class in video_classes:
                    video_info = get_video_with_translation(
                        video_class,
                        language,
                        content_type
                    )
                    
                    if video_info:
                        weeks_data[week_no].append(video_info)
        
        # Format response
        result = []
        for week_no in sorted(weeks_data.keys()):
            result.append({
                "week_no": week_no,
                "is_current_week": week_no == current_week,
                "video_count": len(weeks_data[week_no]),
                "videos": weeks_data[week_no]
            })
        
        return {
            "success": True,
            "current_week": current_week,
            "total_weeks": len(result),
            "data": result
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error in get_student_all_week_videos: {str(e)}",
            "Student Video Content API Error"
        )
        return {
            "success": False,
            "error": f"Internal server error: {str(e)}"
        }





# @frappe.whitelist(allow_guest=True)
# def get_student_quiz_content(course_level, batch, student_id, language, week_start_date=None, week_end_date=None):
#     """
#     Get quiz content for a student based on their current week
    
#     Args:
#         course_level (str): Course Level ID
#         batch (str): Batch ID  
#         student_id (str): Student ID
#         language (str): Language code/ID (TAP Language)
#         week_start_date (str, optional): Week start date for filtering (YYYY-MM-DD)
#         week_end_date (str, optional): Week end date for filtering (YYYY-MM-DD)
        
#     Returns:
#         dict: Quiz information including quiz_id, quiz_name, and questions
#     """
#     try:
#         # Validate inputs
#         if not all([course_level, batch, student_id, language]):
#             return {
#                 "success": False,
#                 "error": "All parameters are required: course_level, batch, student_id, language"
#             }
        
#         # Step 1: Get current week number for the student
#         current_week = get_student_current_week(student_id, batch)
        
#         if not current_week:
#             return {
#                 "success": False,
#                 "error": f"Could not find current week for student {student_id} in batch {batch}"
#             }
        
#         # Step 2: Get learning units for this course level and week
#         learning_units = get_learning_units_by_week(course_level, current_week)
        
#         if not learning_units:
#             return {
#                 "success": False,
#                 "error": f"No learning units found for course level {course_level} week {current_week}"
#             }
        
#         # Step 3: Get quizzes from learning units
#         quizzes = []
#         for learning_unit in learning_units:
#             quiz_list = get_quizzes_from_unit(learning_unit)
            
#             for quiz_id in quiz_list:
#                 # Step 4: Get quiz with questions and translations
#                 quiz_info = get_quiz_with_questions(quiz_id, language)
                
#                 if quiz_info:
#                     quizzes.append(quiz_info)
        
#         if not quizzes:
#             return {
#                 "success": False,
#                 "error": f"No quizzes found for the given parameters"
#             }
        
#         # Return single quiz if only one, otherwise return list
#         return {
#             "success": True,
#             "current_week": current_week,
#             "quiz_count": len(quizzes),
#             "data": quizzes if len(quizzes) > 1 else quizzes[0]
#         }
        
#     except Exception as e:
#         frappe.log_error(
#             f"Error in get_student_quiz_content: {str(e)}\n"
#             f"course_level: {course_level}, batch: {batch}, student: {student_id}",
#             "Student Quiz Content API Error"
#         )
#         return {
#             "success": False,
#             "error": f"Internal server error: {str(e)}"
#         }





# def get_learning_units_by_week(course_level, week_no):
#     """
#     Get learning units for a specific course level and week number
    
#     Args:
#         course_level: Course Level ID
#         week_no: Week number
        
#     Returns:
#         list: List of learning unit IDs
#     """
#     try:
#         # Get Course Level document
#         course_doc = frappe.get_doc("Course Level", course_level)
        
#         learning_units = []
        
#         # Check if learning_units child table exists
#         if hasattr(course_doc, 'learning_units') and course_doc.learning_units:
#             for unit_row in course_doc.learning_units:
#                 if unit_row.week_no == week_no and unit_row.learning_unit:
#                     learning_units.append(unit_row.learning_unit)
        
#         return learning_units
        
#     except Exception as e:
#         frappe.log_error(
#             f"Error getting learning units: {str(e)}",
#             "Student Quiz Content API"
#         )
#         return []


# def get_quizzes_from_unit(learning_unit_id):
#     """
#     Get Quiz IDs from a learning unit's content items
    
#     Args:
#         learning_unit_id: LearningUnit ID
        
#     Returns:
#         list: List of Quiz IDs
#     """
#     try:
#         # Get LearningUnit document
#         unit_doc = frappe.get_doc("LearningUnit", learning_unit_id)
        
#         quizzes = []
        
#         # Check content_items child table
#         if hasattr(unit_doc, 'content_items') and unit_doc.content_items:
#             for content_item in unit_doc.content_items:
#                 if content_item.content_type == "Quiz" and content_item.content:
#                     quizzes.append(content_item.content)
        
#         return quizzes
        
#     except Exception as e:
#         frappe.log_error(
#             f"Error getting quizzes from unit: {str(e)}",
#             "Student Quiz Content API"
#         )
#         return []


# def get_quiz_with_questions(quiz_id, language):
#     """
#     Get quiz information with questions and translations
    
#     Args:
#         quiz_id: Quiz ID
#         language: Language for translation
        
#     Returns:
#         dict: Quiz information with questions list
#     """
#     try:
#         # Get Quiz document
#         quiz_doc = frappe.get_doc("Quiz", quiz_id)
        
#         quiz_name = quiz_doc.quiz_name
        
#         # Get questions from quiz
#         questions_list = []
        
#         if hasattr(quiz_doc, 'questions') and quiz_doc.questions:
#             for question_row in quiz_doc.questions:
#                 question_id = question_row.question
#                 question_number = question_row.question_number
                
#                 if not question_id:
#                     continue
                
#                 # Get question details and check for translation
#                 question_text = get_question_text(question_id, language)
                
#                 if question_text:
#                     questions_list.append({
#                         "order": question_number,
#                         "question_id": question_id,
#                         "question_text": question_text
#                     })
        
#         # Skip if no questions
#         if not questions_list:
#             return None
        
#         # Sort by order
#         questions_list.sort(key=lambda x: x["order"])
        
#         return {
#             "quiz_id": quiz_id,
#             "quiz_name": quiz_name,
#             "total_questions": len(questions_list),
#             "questions": questions_list,
#             "language": language
#         }
        
#     except Exception as e:
#         frappe.log_error(
#             f"Error getting quiz with questions: {str(e)}",
#             "Student Quiz Content API"
#         )
#         return None


# def get_question_text(question_id, language):
#     """
#     Get question text with translation if available
    
#     Args:
#         question_id: QuizQuestion ID
#         language: Language for translation
        
#     Returns:
#         str: Question text (translated if available)
#     """
#     try:
#         # Get QuizQuestion document
#         question_doc = frappe.get_doc("QuizQuestion", question_id)
        
#         question_text = question_doc.question
#         translated = False
        
#         # Try to find translation for the specified language
#         if hasattr(question_doc, 'question_translations') and question_doc.question_translations:
#             for translation in question_doc.question_translations:
#                 if translation.language == language and translation.translated_question:
#                     question_text = translation.translated_question
#                     translated = True
#                     break
        
#         return question_text
        
#     except Exception as e:
#         frappe.log_error(
#             f"Error getting question text: {str(e)}",
#             "Student Quiz Content API"
#         )
#         return None


@frappe.whitelist(allow_guest=True)
def get_student_quiz_content(api_key, course_level, batch, student_id, language, week_start_date=None, week_end_date=None):
    """
    Get quiz content for a student based on their current week
    
    Args:
        course_level (str): Course Level ID
        batch (str): Batch ID  
        student_id (str): Student ID
        language (str): Language code/ID (TAP Language)
        week_start_date (str, optional): Week start date for filtering (YYYY-MM-DD)
        week_end_date (str, optional): Week end date for filtering (YYYY-MM-DD)
        
    Returns:
        dict: Quiz information including quiz_id, quiz_name, and questions
    """
    try:
        # Validate inputs
        if not authenticate_api_key(api_key):
            frappe.throw("Invalid API key")
        if not all([course_level, batch, student_id, language]):
            return {
                "success": False,
                "error": "All parameters are required: course_level, batch, student_id, language"
            }
        
        # Step 1: Get current week number for the student
        current_week = get_student_current_week(student_id, batch)
        
        if not current_week:
            return {
                "success": False,
                "error": f"Could not find current week for student {student_id} in batch {batch}"
            }
        
        # Step 2: Get learning units for this course level and week
        learning_units = get_learning_units_by_week(course_level, current_week)
        
        if not learning_units:
            return {
                "success": False,
                "error": f"No learning units found for course level {course_level} week {current_week}"
            }
        
        # Step 3: Get quizzes from learning units
        quizzes = []
        for learning_unit in learning_units:
            quiz_list = get_quizzes_from_unit(learning_unit)
            
            for quiz_id in quiz_list:
                # Step 4: Get quiz with questions and translations
                quiz_info = get_quiz_with_questions(quiz_id, language)
                
                if quiz_info:
                    quizzes.append(quiz_info)
        
        if not quizzes:
            return {
                "success": False,
                "error": f"No quizzes found for the given parameters"
            }
        
        # Build flat response structure - always use quiz_1, quiz_2 format
        response = {
            "success": True,
            "current_week": current_week,
            "quiz_count": len(quizzes)
        }
        
        # Add quizzes as quiz_1, quiz_2, etc. (even for single quiz)
        for idx, quiz in enumerate(quizzes, start=1):
            quiz_key = f"quiz_{idx}"
            quiz_data = {
                "quiz_id": quiz["quiz_id"],
                "quiz_name": quiz["quiz_name"],
                "total_questions": quiz["total_questions"]
            }
            
            # Add questions as individual fields within each quiz
            for question in quiz["questions"]:
                question_key = f"question_{question['order']}"
                quiz_data[question_key] = question["question_text"]
            
            quiz_data["language"] = quiz["language"]
            response[quiz_key] = quiz_data
        
        return response
        
    except Exception as e:
        frappe.log_error(
            f"Error in get_student_quiz_content: {str(e)}\n"
            f"course_level: {course_level}, batch: {batch}, student: {student_id}",
            "Student Quiz Content API Error"
        )
        return {
            "success": False,
            "error": f"Internal server error: {str(e)}"
        }


@frappe.whitelist(allow_guest=False)
def get_quiz_by_id(api_key, student_id, quiz_id, language=None):
    """
    Get quiz content by quiz_id for a student.
    Same response format as get_student_quiz_content.
    
    Request Body (JSON):
        {
            "student_id": "ST00001388",
            "quiz_id": "BasicQuiz_Quiz_B-basic",
            "language": "English"  // optional
        }
    
    Returns:
        dict: Quiz information with questions in flat format
    """
    try:
        # Get data from request body
        # data = frappe.request.get_json() if frappe.request.data else {}
        
        # student_id = data.get("student_id")
        # quiz_id = data.get("quiz_id")
        # language = data.get("language")


        if api_key:
            # Validate API key
           if not authenticate_api_key(api_key):
                return {
                    "success": False,
                    "error": "Invalid API key"
                }

        # Validate inputs
        if not student_id or not quiz_id:
            return {
                "success": False,
                "error": "student_id and quiz_id are required"
            }
        
        # Resolve student ID
        resolved_student = resolve_student_id(student_id)
        if not resolved_student:
            return {
                "success": False,
                "error": f"Student not found for identifier: {student_id}"
            }
        
        # Get student's language if not provided
        if not language:
            student_lang = frappe.db.get_value("Student", resolved_student, "language")
            language = student_lang if student_lang else "English"
        
        # Check if quiz exists
        if not frappe.db.exists("Quiz", quiz_id):
            return {
                "success": False,
                "error": f"Quiz '{quiz_id}' not found"
            }
        
        # Get quiz with questions
        quiz_info = _get_quiz_with_questions_flat(quiz_id, language)
        
        if not quiz_info:
            return {
                "success": False,
                "error": f"No questions found for quiz '{quiz_id}'"
            }
        
        # Build flat response structure (same as get_student_quiz_content)
        response = {
            "success": True,
            "quiz_count": 1
        }
        
        # Add quiz as quiz_1 with questions as question_1, question_2, etc.
        quiz_data = {
            "quiz_id": quiz_info["quiz_id"],
            "quiz_name": quiz_info["quiz_name"],
            "total_questions": quiz_info["total_questions"]
        }
        
        # Add questions as individual fields
        for question in quiz_info["questions"]:
            question_key = f"question_{question['order']}"
            quiz_data[question_key] = question["question_text"]
        
        quiz_data["language"] = quiz_info["language"]
        response["quiz_1"] = quiz_data
        
        return response
        
    except Exception as e:
        frappe.log_error(
            f"get_quiz_by_id error: {str(e)}\n"
            f"student_id: {student_id}, quiz_id: {quiz_id}",
            "Student Progression API Error"
        )
        return {
            "success": False,
            "error": str(e)
        }


def _get_quiz_with_questions_flat(quiz_id: str, language: str) -> dict:
    """
    Get quiz information with questions and translations.
    Helper function for get_quiz_by_id.
    
    Args:
        quiz_id: Quiz ID
        language: Language for translation
        
    Returns:
        dict: Quiz information with questions list
    """
    try:
        # Get Quiz document
        quiz_doc = frappe.get_doc("Quiz", quiz_id)
        
        quiz_name = quiz_doc.quiz_name
        
        # Get questions from quiz
        questions_list = []
        
        if hasattr(quiz_doc, 'questions') and quiz_doc.questions:
            for question_row in quiz_doc.questions:
                question_id = question_row.question
                question_number = question_row.question_number if hasattr(question_row, 'question_number') else question_row.idx
                
                if not question_id:
                    continue
                
                # Get question details and check for translation
                question_text = _get_question_text_translated(question_id, language)
                
                if question_text:
                    questions_list.append({
                        "order": question_number,
                        "question_id": question_id,
                        "question_text": question_row.question
                    })
        
        # Skip if no questions
        if not questions_list:
            return None
        
        # Sort by order
        questions_list.sort(key=lambda x: x["order"])
        
        return {
            "quiz_id": quiz_id,
            "quiz_name": quiz_name,
            "total_questions": len(questions_list),
            "questions": questions_list,
            "language": language
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error getting quiz with questions: {str(e)}",
            "Student Progression API"
        )
        return None


def _get_question_text_translated(question_id: str, language: str) -> str:
    """
    Get question text with translation if available.
    
    Args:
        question_id: QuizQuestion ID
        language: Language for translation
        
    Returns:
        str: Question text (translated if available)
    """
    try:
        # Get QuizQuestion document
        question_doc = frappe.get_doc("QuizQuestion", question_id)
        
        question_text = question_doc.question
        
        # Try to find translation for the specified language
        if language and hasattr(question_doc, 'question_translations') and question_doc.question_translations:
            for translation in question_doc.question_translations:
                if translation.language == language and translation.translated_question:
                    question_text = translation.translated_question
                    break
        
        return question_text
        
    except Exception as e:
        frappe.log_error(
            f"Error getting question text: {str(e)}",
            "Student Progression API"
        )
        return None

def resolve_student_id(identifier: str) -> str:
    """
    Resolve various identifiers to student ID.
    
    Args:
        identifier: Can be student ID, Glific ID, or phone number
    
    Returns:
        str: Student document ID or None
    """
    if not identifier:
        return None
        
    # Check if it's already a valid student ID
    if frappe.db.exists("Student", identifier):
        return identifier
    
    # Try Glific ID
    student = frappe.db.get_value("Student", {"glific_id": identifier}, "name")
    if student:
        return student
    
    # Try phone number
    student = frappe.db.get_value("Student", {"phone": identifier}, "name")
    if student:
        return student
    
    # Try phone with country code variations
    if identifier.startswith("91") and len(identifier) > 10:
        phone_without_code = identifier[2:]
        student = frappe.db.get_value("Student", {"phone": phone_without_code}, "name")
        if student:
            return student
    
    return None

def get_quizzes_from_unit(learning_unit_id):
    """
    Get Quiz IDs from a learning unit's content items
    
    Args:
        learning_unit_id: LearningUnit ID
        
    Returns:
        list: List of Quiz IDs
    """
    try:
        # Get LearningUnit document
        unit_doc = frappe.get_doc("LearningUnit", learning_unit_id)
        
        quizzes = []
        
        # Check content_items child table
        if hasattr(unit_doc, 'content_items') and unit_doc.content_items:
            for content_item in unit_doc.content_items:
                if content_item.content_type == "Quiz" and content_item.content:
                    quizzes.append(content_item.content)
        
        return quizzes
        
    except Exception as e:
        frappe.log_error(
            f"Error getting quizzes from unit: {str(e)}",
            "Student Quiz Content API"
        )
        return []


def get_quiz_with_questions(quiz_id, language):
    """
    Get quiz information with questions and translations
    
    Args:
        quiz_id: Quiz ID
        language: Language for translation
        
    Returns:
        dict: Quiz information with questions list
    """
    try:
        # Get Quiz document
        quiz_doc = frappe.get_doc("Quiz", quiz_id)
        
        quiz_name = quiz_doc.quiz_name
        
        # Get questions from quiz
        questions_list = []
        
        if hasattr(quiz_doc, 'questions') and quiz_doc.questions:
            for question_row in quiz_doc.questions:
                question_id = question_row.question
                question_number = question_row.question_number
                
                if not question_id:
                    continue
                
                # Get question details and check for translation
                question_text = get_question_text(question_id, language)
                
                if question_text:
                    questions_list.append({
                        "order": question_number,
                        "question_id": question_id,
                        "question_text": question_text
                    })
        
        # Skip if no questions
        if not questions_list:
            return None
        
        # Sort by order
        questions_list.sort(key=lambda x: x["order"])
        
        return {
            "quiz_id": quiz_id,
            "quiz_name": quiz_name,
            "total_questions": len(questions_list),
            "questions": questions_list,
            "language": language
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error getting quiz with questions: {str(e)}",
            "Student Quiz Content API"
        )
        return None


def get_question_text(question_id, language):
    """
    Get question text with translation if available
    
    Args:
        question_id: QuizQuestion ID
        language: Language for translation
        
    Returns:
        str: Question text (translated if available)
    """
    try:
        # Get QuizQuestion document
        question_doc = frappe.get_doc("QuizQuestion", question_id)
        
        question_text = question_doc.question
        translated = False
        
        # Try to find translation for the specified language
        if hasattr(question_doc, 'question_translations') and question_doc.question_translations:
            for translation in question_doc.question_translations:
                if translation.language == language and translation.translated_question:
                    question_text = translation.translated_question
                    translated = True
                    break
        
        return question_text
        
    except Exception as e:
        frappe.log_error(
            f"Error getting question text: {str(e)}",
            "Student Quiz Content API"
        )
        return None


# @frappe.whitelist(allow_guest=True)
# def get_quiz_question_details(question_id, language):
#     """
#     Get quiz question details with options and translations
    
#     Args:
#         question_id (str): QuizQuestion ID
#         language (str): Language (TAP Language) - required
        
#     Returns:
#         dict: Question details with options and answer in specified language
#     """
#     try:
#         # Validate inputs
#         if not question_id or not language:
#             return {
#                 "success": False,
#                 "error": "Both question_id and language are required"
#             }
        
#         # Get QuizQuestion document
#         try:
#             question_doc = frappe.get_doc("QuizQuestion", question_id)
#         except frappe.DoesNotExistError:
#             return {
#                 "success": False,
#                 "error": f"Question '{question_id}' not found"
#             }
        
#         # Get question text in requested language
#         question_text = get_question_text_by_language(question_doc, language)
        
#         # Get options with translations for requested language
#         options = get_options_by_language(question_doc, language)
        
#         # Get correct answer
#         correct_option_number = question_doc.correct_option
#         answer = get_correct_answer_by_language(options, correct_option_number)
        
#         # Build response with dynamic language
#         response = {
#             "success": True,
#             "question_id": question_id,
#             "question": question_text,  # Dynamic question based on language
#             "question_type": question_doc.question_type,
#             "points": question_doc.points if hasattr(question_doc, 'points') else 0,
#             "language": language,
#             "options": options,
#             "answer": answer,
#             "correct_option_number": correct_option_number
#         }
        
#         return response
        
#     except Exception as e:
#         frappe.log_error(
#             f"Error in get_quiz_question_details: {str(e)}\n"
#             f"question_id: {question_id}, language: {language}",
#             "Quiz Question Details API Error"
#         )
#         return {
#             "success": False,
#             "error": f"Internal server error: {str(e)}"
#         }


# def get_question_text_by_language(question_doc, language):
#     """
#     Get question text in specified language
    
#     Args:
#         question_doc: QuizQuestion document
#         language: Language name (e.g., "English", "Hindi")
        
#     Returns:
#         str: Question text in requested language
#     """
#     # Try to find translation for the specified language
#     if hasattr(question_doc, 'question_translations') and question_doc.question_translations:
#         for translation in question_doc.question_translations:
#             if translation.language == language and translation.translated_question:
#                 return translation.translated_question
    
#     # Fallback to original question if translation not found
#     return question_doc.question


# def get_options_by_language(question_doc, language):
#     """
#     Get all options in specified language
    
#     Args:
#         question_doc: QuizQuestion document
#         language: Language name
        
#     Returns:
#         list: List of option objects with translations for specified language
#     """
#     options_list = []
    
#     # Check if question has options child table
#     if not hasattr(question_doc, 'options') or not question_doc.options:
#         return options_list
    
#     # Get options from child table
#     for option_row in question_doc.options:
#         option_id = option_row.options  # Link to QuizOption
#         order_number = option_row.order_number
        
#         if not option_id:
#             continue
        
#         try:
#             # Get QuizOption document
#             option_doc = frappe.get_doc("QuizOption", option_id)
            
#             # Get translated option text
#             option_text = get_option_translation(option_doc, language)
#             if not option_text:
#                 option_text = option_doc.option_text  # Fallback to original
            
#             option_data = {
#                 "option_id": option_id,
#                 "option_number": order_number if order_number else option_doc.option_number,
#                 "option_text": option_text
#             }
            
#             options_list.append(option_data)
            
#         except Exception as e:
#             frappe.log_error(
#                 f"Error getting option details: {str(e)}",
#                 "Quiz Question Details API"
#             )
#             continue
    
#     # Sort by option_number
#     options_list.sort(key=lambda x: x["option_number"])
    
#     return options_list


# def get_option_translation(option_doc, language):
#     """
#     Get option translation for specific language
    
#     Args:
#         option_doc: QuizOption document
#         language: Language name
        
#     Returns:
#         str: Translated option text or None
#     """
#     if not hasattr(option_doc, 'option_translations') or not option_doc.option_translations:
#         return None
    
#     for translation in option_doc.option_translations:
#         if translation.language == language and translation.translated_option:
#             return translation.translated_option
    
#     return None


# def get_correct_answer_by_language(options, correct_option_number):
#     """
#     Get the correct answer details from options
    
#     Args:
#         options: List of option objects
#         correct_option_number: Correct option number
        
#     Returns:
#         dict: Correct answer details
#     """
#     if not correct_option_number or not options:
#         return None
    
#     for option in options:
#         if option["option_number"] == correct_option_number:
#             return {
#                 "correct_option_number": correct_option_number,
#                 "option_id": option["option_id"],
#                 "option_text": option["option_text"]
#             }
    
#     return {
#         "correct_option_number": correct_option_number,
#         "message": "Correct option details not found in options list"
#     }

@frappe.whitelist(allow_guest=True)
def get_quiz_question_details(api_key, question_id, language):
    """
    Get quiz question details with options and translations
    
    Args:
        question_id (str): QuizQuestion ID
        language (str): Language (TAP Language) - required
        
    Returns:
        dict: Question details with options and answer in specified language
    """
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")
    
    try:
        # Validate inputs
        if not question_id or not language:
            return {
                "message": {
                    "success": False,
                    "error": "Both question_id and language are required"
                }
            }
        
        # Get QuizQuestion document
        try:
            question_doc = frappe.get_doc("QuizQuestion", question_id)
        except frappe.DoesNotExistError:
            return {
                "message": {
                    "success": False,
                    "error": f"Question '{question_id}' not found"
                }
            }
        
        # Get question text in requested language
        question_text = get_question_text_by_language(question_doc, language)
        
        # Get options with translations for requested language
        options = get_options_by_language(question_doc, language)
        
        # Get correct answer
        correct_option_number = question_doc.correct_option
        
        # Build response with dynamic language
        response_data = {
            "success": True,
            "question_id": question_id,
            "question": strip_html_tags(question_text),
            "question_type": question_doc.question_type,
            "language": language
        }
        
        # Add options as option_a, option_b, option_c, etc.
        option_letters = ['a', 'b', 'c', 'd', 'e', 'f']  # Support up to 6 options
        correct_option_text = None
        correct_option_letter = None
        
        for idx, option in enumerate(options):
            if idx < len(option_letters):
                option_key = f"option_{option_letters[idx]}"
                response_data[option_key] = option["option_text"]
                
                # Check if this is the correct option
                if option["option_number"] == correct_option_number:
                    correct_option_text = option["option_text"]
                    correct_option_letter = option_letters[idx].upper()
        
        # Add correct option details
        if correct_option_letter:
            response_data["correct_option"] = correct_option_letter
            response_data["correct_option_text"] = correct_option_text
        
        return {"message": response_data}
        
    except Exception as e:
        frappe.log_error(
            f"Error in get_quiz_question_details: {str(e)}\n"
            f"question_id: {question_id}, language: {language}",
            "Quiz Question Details API Error"
        )
        return {
            "message": {
                "success": False,
                "error": f"Internal server error: {str(e)}"
            }
        }

import re
from html import unescape

def strip_html_tags(html_text):
    """Remove HTML tags from text using regex"""
    if not html_text:
        return ""
    
    # Remove HTML tags
    clean = re.sub(r'<[^>]+>', '', html_text)
    # Decode HTML entities like &nbsp; &amp; etc
    clean = unescape(clean)
    # Remove extra whitespace
    clean = ' '.join(clean.strip().split())
    
    return clean


def get_question_text_by_language(question_doc, language):
    """
    Get question text in specified language
    
    Args:
        question_doc: QuizQuestion document
        language: Language name (e.g., "English", "Hindi")
        
    Returns:
        str: Question text in requested language
    """
    # Try to find translation for the specified language
    if hasattr(question_doc, 'question_translations') and question_doc.question_translations:
        for translation in question_doc.question_translations:
            if translation.language == language and translation.translated_question:
                return translation.translated_question
    
    # Fallback to original question if translation not found
    return question_doc.question


def get_options_by_language(question_doc, language):
    """
    Get all options in specified language
    
    Args:
        question_doc: QuizQuestion document
        language: Language name
        
    Returns:
        list: List of option objects with translations for specified language
    """
    options_list = []
    
    # Check if question has options child table
    if not hasattr(question_doc, 'options') or not question_doc.options:
        return options_list
    
    # Get options from child table
    for option_row in question_doc.options:
        option_id = option_row.options  # Link to QuizOption
        order_number = option_row.order_number
        
        if not option_id:
            continue
        
        try:
            # Get QuizOption document
            option_doc = frappe.get_doc("QuizOption", option_id)
            
            # Get translated option text
            option_text = get_option_translation(option_doc, language)
            if not option_text:
                option_text = option_doc.option_text  # Fallback to original
            
            option_data = {
                "option_id": option_id,
                "option_number": order_number if order_number else option_doc.option_number,
                "option_text": option_text
            }
            
            options_list.append(option_data)
            
        except Exception as e:
            frappe.log_error(
                f"Error getting option details: {str(e)}",
                "Quiz Question Details API"
            )
            continue
    
    # Sort by option_number
    options_list.sort(key=lambda x: x["option_number"])
    
    return options_list


def get_option_translation(option_doc, language):
    """
    Get option translation for specific language
    
    Args:
        option_doc: QuizOption document
        language: Language name
        
    Returns:
        str: Translated option text or None
    """
    if not hasattr(option_doc, 'option_translations') or not option_doc.option_translations:
        return None
    
    for translation in option_doc.option_translations:
        if translation.language == language and translation.translated_option:
            return translation.translated_option
    
    return None

@frappe.whitelist(allow_guest=False)
def get_multiple_questions(question_ids, language):
    """
    Get details for multiple questions at once
    
    Args:
        question_ids (str or list): Comma-separated question IDs or list
        language (str): Language for translations - required
        
    Returns:
        dict: Multiple question details
    """
    try:
        # Validate language
        if not language:
            return {
                "success": False,
                "error": "language parameter is required"
            }
        
        # Parse question_ids
        if isinstance(question_ids, str):
            question_ids = [q.strip() for q in question_ids.split(',') if q.strip()]
        
        if not question_ids:
            return {
                "success": False,
                "error": "No question IDs provided"
            }
        
        # Get details for each question
        questions = []
        for question_id in question_ids:
            result = get_quiz_question_details(question_id, language)
            if result.get("success"):
                questions.append(result)
        
        return {
            "success": True,
            "language": language,
            "total_questions": len(questions),
            "questions": questions
        }
        
    except Exception as e:
        frappe.log_error(
            f"Error in get_multiple_questions: {str(e)}",
            "Quiz Question Details API Error"
        )
        return {
            "success": False,
            "error": f"Internal server error: {str(e)}"
        }

@frappe.whitelist(allow_guest=True)
def get_teacher_batch_by_phone(api_key, phone_number, type="teacher"):
    """
    API to get teacher details and their batch information by phone number
    
    Args:
        phone_number (str): Teacher's phone number
        type (str): User type, should be "teacher"
    
    Returns:
        dict: Teacher details with batch information
    """
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")
    try:
        # Validate type parameter
        if type.lower() != "teacher":
            return {
                "success": False,
                "message": "Invalid type parameter. Must be 'teacher'"
            }
        
        # Validate phone number
        if not phone_number:
            return {
                "success": False,
                "message": "Phone number is required"
            }
        
        # Query teacher by phone number
        teachers = frappe.get_all(
            "Teacher",
            filters={"phone_number": phone_number},
            fields=[
                "name",
                "first_name",
                "last_name",
                "school",
                "teacher_batch",
                "phone_number",
            ]
        )
        
        if not teachers:
            return {
                "success": False,
                "message": f"No teacher found with phone number: {phone_number}"
            }
        
        teacher = teachers[0]
        
        # Get batch details if teacher has a batch assigned
        batch_details = None
        if teacher.get("teacher_batch"):
            batch = frappe.get_doc("Batch", teacher.get("teacher_batch"))
            
            batch_details = {
                "batch_id": batch.name,
                "batch_name": batch.name,
                "title": batch.title,
                "start_date": batch.start_date,
                "end_date": batch.end_date,
                "active": batch.active,
                "batch_id_field": batch.batch_id,
                "registration_end_date": batch.regist_end_date,
                "engagement_end_date": batch.engagement_end_dae if hasattr(batch, 'engagement_end_dae') else None,
                "regular_activity_start_date": batch.regular_activity_start_date if hasattr(batch, 'regular_activity_start_date') else None
            }
        
        # Prepare flat response structure with only essential fields
        response = {
            "success": True,
            "teacher_id": teacher.name,
            "teacher_name": f"{teacher.first_name or ''} {teacher.last_name or ''}".strip(),
        }
        
        # Add batch details to the same level if batch exists
        if batch_details:
            response.update({
                "batch_id": batch_details["batch_id"],
                "batch_name": batch_details["batch_name"],
                "batch_title": batch_details["title"],
                "batch_start_date": batch_details["start_date"],
                "batch_end_date": batch_details["end_date"],
                "active": batch_details["active"],
                "batch_id_field": batch_details["batch_id_field"],
                "registration_end_date": batch_details["registration_end_date"],
                "engagement_end_date": batch_details["engagement_end_date"],
                "regular_activity_start_date": batch_details["regular_activity_start_date"]
            })
        
        return response
        
    except Exception as e:
        frappe.log_error(f"Error in get_teacher_by_phone: {str(e)}", "Teacher Batch API Error")
        return {
            "success": False,
            "message": f"An error occurred: {str(e)}"
        }
