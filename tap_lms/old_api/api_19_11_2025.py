import frappe
import json
from frappe.utils import cint, today, get_url, now_datetime, getdate, cstr, get_datetime
from datetime import datetime, timedelta
import requests
import random
import string
import urllib.parse
from ..glific_integration import create_contact, start_contact_flow, get_contact_by_phone, update_contact_fields, add_contact_to_group, create_or_get_teacher_group_for_batch
from ..background_jobs import enqueue_glific_actions



def authenticate_api_key(api_key):
    try:
        # Check if the provided API key exists and is enabled
        api_key_doc = frappe.get_doc("API Key", {"key": api_key, "enabled": 1})
        return api_key_doc.name
    except frappe.DoesNotExistError:
        # Handle the case where the API key does not exist or is not enabled
        return None



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
                    glific_contact = get_contact_by_phone(teacher.phone_number)

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

                        new_contact = create_contact(
                            teacher.first_name or "Teacher",  # Fallback if first_name is empty
                            teacher.phone_number,
                            school_name,
                            model_name,
                            language_id,
                            batch_info["batch_id"]
                        )

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
                        group_added = add_contact_to_group(teacher.glific_id, teacher_group["group_id"])
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
def create_teacher_web():
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
        glific_contact = get_contact_by_phone(data['phone'])
        
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
            glific_contact = create_contact(
                data['firstName'],
                data['phone'],
                school_name,
                model_name,
                language_id,
                batch_id
            )

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
        # If no active batch onboarding, fall back to school's default model
        model_link = frappe.db.get_value("School", school_id, "model")
        frappe.logger().info(f"No active batch onboarding found. Using default model for school {school_id}")

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
                "phone_number": teacher_data.phone_number,
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

