import json
import re

import frappe
from frappe.utils import getdate, now_datetime

from tap_lms.api import authenticate_api_key
from tap_lms.onboarding.glific_sync import (
    enqueue_registration_contact_sync,
    sync_registration_contact_to_glific as _sync_registration_contact_to_glific,
)


DELHI_BATCH = "BT00000024" 
PHONE_PATTERN = re.compile(r"^\d{10}$")
ALLOWED_STUDENT_COURSE_NAMES = {
    "Arts",
    "Coding",
    "Science Lab",
    "Financial Literacy",
}
DELHI_GRADE_COURSE_MAP = {
    "4": "Arts",
    "5": "Science Lab",
    "6": "Coding",
    "7": "Financial Literacy",
    "8": "Science Lab",
    "9": "Coding",
    "11": "Financial Literacy",
}


def _set_status(code):
    frappe.response.http_status_code = code


def _respond(code, payload):
    _set_status(code)
    frappe.response.update(payload)
    return


def _get_request_data():
    if getattr(frappe.request, "data", None):
        try:
            return json.loads(frappe.request.data)
        except Exception:
            pass

    if hasattr(frappe.request, "get_json"):
        try:
            data = frappe.request.get_json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    return dict(frappe.form_dict or {})


def _validate_api_key_or_respond(api_key):
    if not api_key:
        _respond(400, {"status": "failure", "message": "API key is required"})
        return False

    if not authenticate_api_key(api_key):
        _respond(401, {"status": "failure", "message": "Invalid API key"})
        return False

    return True


def _validate_phone(phone):
    return bool(phone and PHONE_PATTERN.fullmatch(str(phone).strip()))


def _require_valid_phone(phone):
    if not _validate_phone(phone):
        frappe.throw("Phone must be exactly 10 digits")
    return str(phone).strip()


def _get_language_name_to_id(language_name):
    if not language_name:
        return None

    language_id = frappe.db.get_value(
        "TAP Language",
        {"language_name": language_name},
        "name",
    )
    if not language_id:
        frappe.throw(f"Invalid language: {language_name}")
    return language_id


def _get_language_id_to_name(language_id):
    if not language_id:
        return ""
    return frappe.db.get_value("TAP Language", language_id, "language_name") or language_id


def _get_school_row_by_id(school_id):
    rows = frappe.db.sql(
        """
        SELECT
            s.name AS school_id,
            s.name1 AS school_name,
            s.state AS state_id,
            COALESCE(st.state_name, s.state, '') AS state,
            COALESCE(d.district_name, s.district, '') AS district,
            COALESCE(c.city_name, s.city, '') AS city
        FROM `tabSchool` s
        LEFT JOIN `tabState` st ON st.name = s.state
        LEFT JOIN `tabDistrict` d ON d.name = s.district
        LEFT JOIN `tabCity` c ON c.name = s.city
        WHERE s.name = %s
        LIMIT 1
        """,
        (school_id,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _get_school_row_from_input(school_value):
    school_value = (school_value or "").strip()
    if not school_value:
        return None

    rows = frappe.db.sql(
        """
        SELECT
            s.name AS school_id,
            s.name1 AS school_name,
            s.state AS state_id,
            COALESCE(st.state_name, s.state, '') AS state,
            COALESCE(d.district_name, s.district, '') AS district,
            COALESCE(c.city_name, s.city, '') AS city
        FROM `tabSchool` s
        LEFT JOIN `tabState` st ON st.name = s.state
        LEFT JOIN `tabDistrict` d ON d.name = s.district
        LEFT JOIN `tabCity` c ON c.name = s.city
        WHERE s.name1 = %s
           OR CONCAT(s.name, ' - ', s.name1) = %s
        LIMIT 1
        """,
        (school_value, school_value),
        as_dict=True,
    )
    return rows[0] if rows else None


def _is_delhi_school(school_row):
    return (school_row.get("state") or "").strip().upper() == "DELHI"


def _ensure_teacher_enrollment(teacher_doc, school_row, batch_id):
    if not school_row or not batch_id:
        return

    for enrollment in teacher_doc.get("enrollment") or []:
        if enrollment.batch == batch_id and enrollment.school == school_row["school_id"]:
            return

    teacher_doc.append(
        "enrollment",
        {
            "batch": batch_id,
            "school": school_row["school_id"],
            "date_joining": now_datetime().date(),
            "whatsapp_response": 0,
        },
    )


def _get_course_name_from_course_level(course_level_name):
    if not course_level_name:
        return ""

    vertical_name = frappe.db.get_value("Course Level", course_level_name, "vertical")
    if not vertical_name:
        return ""

    return frappe.db.get_value("Course Verticals", vertical_name, "name2") or ""


def _get_latest_enrollment(doc):
    enrollments = list(doc.get("enrollment") or [])
    if not enrollments:
        return None

    def _sort_key(item):
        if item.date_joining:
            return (1, getdate(item.date_joining), item.idx or 0)
        return (0, getdate("1900-01-01"), item.idx or 0)

    return max(enrollments, key=_sort_key)


def _get_course_level_label_for_grade(grade):
    try:
        grade_num = int(str(grade).strip())
    except Exception:
        frappe.throw(f"Invalid grade: {grade}")

    if grade_num <= 5:
        return "Level 1"
    if 6 <= grade_num <= 8:
        return "Level 2"
    if 9 <= grade_num <= 10:
        return "Level 3"
    if 11 <= grade_num <= 12:
        return "Level 4"

    frappe.throw(f"Unsupported grade for course level mapping: {grade}")


def _get_course_vertical_and_level(course_name, grade):
    course_vertical = frappe.db.get_value(
        "Course Verticals",
        {"name2": course_name},
        ["name", "name2"],
        as_dict=True,
    )
    if not course_vertical:
        raise ValueError(f"Course vertical not found for course_name: {course_name}")

    level_label = _get_course_level_label_for_grade(grade)
    course_level_name = frappe.db.get_value(
        "Course Level",
        {"vertical": course_vertical.name, "level": level_label},
        "name",
    )
    if not course_level_name:
        raise ValueError(
            f"Matching course level not found for course_name={course_name}, grade={grade}, level={level_label}"
        )

    return course_vertical, level_label, course_level_name


def _get_school_course_vertical_names(school_id):
    if not school_id:
        return []

    school = frappe.get_doc("School", school_id)
    course_names = []

    for row in school.get("grade_course_verticals") or []:
        if not row.course_vertical:
            continue
        course_name = frappe.db.get_value(
            "Course Verticals",
            row.course_vertical,
            "name2",
        )
        if course_name:
            course_names.append(course_name)

    return course_names


def _enqueue_glific_contact_sync(doctype, docname):
    enqueue_registration_contact_sync(doctype, docname)


def sync_registration_contact_to_glific(doctype, docname, retry_count=0):
    return _sync_registration_contact_to_glific(doctype, docname, retry_count=retry_count)


@frappe.whitelist(allow_guest=True)
def list_school_details():
    # Expected input parameters:
    # - api_key
    data = _get_request_data()
    api_key = data.get("api_key")

    if not _validate_api_key_or_respond(api_key):
        return

    schools = frappe.db.sql(
        """
        SELECT
            s.name AS school_id,
            COALESCE(st.state_name, s.state, '') AS state,
            COALESCE(d.district_name, s.district, '') AS district,
            COALESCE(c.city_name, s.city, '') AS city,
            s.name1 AS school_name
        FROM `tabSchool` s
        LEFT JOIN `tabState` st ON st.name = s.state
        LEFT JOIN `tabDistrict` d ON d.name = s.district
        LEFT JOIN `tabCity` c ON c.name = s.city
        ORDER BY s.name1 ASC
        """,
        as_dict=True,
    )

    _respond(200, {"schools": schools})


@frappe.whitelist(allow_guest=True)
def check_teacher_exists():
    # Expected input parameters:
    # - phone
    data = _get_request_data()
    phone = str(data.get("phone") or "").strip()

    if not _validate_phone(phone):
        _respond(400, {"exists": False, "message": "Phone must be exactly 10 digits"})
        return

    exists = bool(frappe.db.exists("Teacher", {"phone_number": phone}))
    _respond(200, {"exists": exists})


@frappe.whitelist(allow_guest=True)
def get_teacher_details():
    # Expected input parameters:
    # - phone
    data = _get_request_data()
    phone = str(data.get("phone") or "").strip()

    if not _validate_phone(phone):
        _respond(400, {"message": "Phone must be exactly 10 digits"})
        return

    teacher = frappe.db.get_value(
        "Teacher",
        {"phone_number": phone},
        [
            "name",
            "first_name",
            "last_name",
            "phone_number",
            "school_id",
            "teacher_role",
            "language",
        ],
        as_dict=True,
    )

    if not teacher:
        _respond(404, {"message": "Teacher not found"})
        return

    school_row = _get_school_row_by_id(teacher.school_id) if teacher.school_id else None
    payload = {
        "firstName": teacher.first_name or "",
        "lastName": teacher.last_name or "",
        "phone": teacher.phone_number or "",
        "state": school_row["state"] if school_row else "",
        "district": school_row["district"] if school_row else "",
        "city": school_row["city"] if school_row else "",
        "school": school_row["school_name"] if school_row else "",
        "role": teacher.teacher_role or "",
        "language": _get_language_id_to_name(teacher.language),
    }
    _respond(200, payload)


@frappe.whitelist(allow_guest=True)
def update_teacher_details():
    # Expected input parameters:
    # - firstName
    # - lastName
    # - phone
    # - state
    # - district
    # - city
    # - school
    # - role
    # - language
    data = _get_request_data()

    try:
        phone = _require_valid_phone(data.get("phone"))
        school_value = data.get("school")
        school_row = _get_school_row_from_input(school_value) if school_value else None
        if school_value and not school_row:
            frappe.throw("School not found")

        teacher_name = frappe.db.get_value("Teacher", {"phone_number": phone}, "name")
        if not teacher_name:
            _respond(404, {"status": "failure", "message": "Teacher not found"})
            return

        teacher = frappe.get_doc("Teacher", teacher_name)
        teacher.first_name = data.get("firstName") or teacher.first_name
        teacher.last_name = data.get("lastName") or teacher.last_name
        teacher.phone_number = phone
        teacher.teacher_role = data.get("role") or teacher.teacher_role
        teacher.language = _get_language_name_to_id(data.get("language")) or teacher.language
        if school_row:
            teacher.school_id = school_row["school_id"]
            teacher.state = school_row["state_id"]
            if _is_delhi_school(school_row):
                teacher.teacher_batch = DELHI_BATCH
                _ensure_teacher_enrollment(teacher, school_row, DELHI_BATCH)

        teacher.save(ignore_permissions=True)
        _enqueue_glific_contact_sync("Teacher", teacher.name)
        frappe.db.commit()
        _respond(200, {"status": "success", "message": "Teacher details updated successfully."})
    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "update_teacher_details failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def create_teacher_web():
    # Expected input parameters:
    # - api_key
    # - firstName
    # - lastName
    # - gender
    # - phone
    # - state
    # - district
    # - city
    # - school
    # - role
    # - language
    data = _get_request_data()

    try:
        if not _validate_api_key_or_respond(data.get("api_key")):
            return

        phone = _require_valid_phone(data.get("phone"))
        first_name = (data.get("firstName") or "").strip()
        school_value = (data.get("school") or "").strip()
        requested_state = (data.get("state") or "").strip()
        is_delhi_registration = requested_state.upper() == "DELHI"

        if not first_name or not school_value:
            _set_status(400)
            return {
                "status": "failure",
                "message": "Missing required field: firstName or school",
            }

        if frappe.db.exists("Teacher", {"phone_number": phone}):
            _set_status(409)
            return {
                "status": "failure",
                "message": "A teacher with this phone number already exists",
            }

        school_row = _get_school_row_from_input(school_value)
        if not school_row:
            _set_status(404)
            return {"status": "failure", "message": "School not found"}

        teacher = frappe.get_doc(
            {
                "doctype": "Teacher",
                "first_name": first_name,
                "last_name": (data.get("lastName") or "").strip(),
                "gender": (data.get("gender") or "").strip(),
                "phone_number": phone,
                "teacher_role": (data.get("role") or "").strip(),
                "language": _get_language_name_to_id(data.get("language")),
                "school_id": school_row["school_id"],
                "state": school_row["state_id"],
                "teacher_batch": DELHI_BATCH if is_delhi_registration else None,
            }
        )
        if teacher.teacher_batch:
            _ensure_teacher_enrollment(teacher, school_row, teacher.teacher_batch)
        teacher.insert(ignore_permissions=True)
        _enqueue_glific_contact_sync("Teacher", teacher.name)
        frappe.db.commit()

        _set_status(200)
        return {
            "status": "success",
            "message": "Teacher created successfully.",
            "teacher_id": teacher.name,
        }
    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "create_teacher_web failed")
        _set_status(500)
        return {"status": "failure", "message": str(exc)}


@frappe.whitelist(allow_guest=True)
def create_student_web():
    # Expected input parameters:
    # - school_id
    # - student_name
    # - phone
    # - gender
    # - grade
    # - language
    data = _get_request_data()

    try:
        phone = _require_valid_phone(data.get("phone"))
        student_name = (data.get("student_name") or "").strip()
        school_id = (data.get("school_id") or "").strip()
        gender = (data.get("gender") or "").strip()
        grade = str(data.get("grade") or "").strip()

        required = [student_name, school_id, gender, grade, data.get("language")]
        if not all(required):
            _set_status(400)
            return {
                "status": "failure",
                "message": "school_id, student_name, phone, gender, grade and language are required.",
            }

        school_row = _get_school_row_by_id(school_id)
        if not school_row:
            _set_status(404)
            return {"status": "failure", "message": "School not found"}

        existing_student_name = frappe.db.get_value("Student", {"phone": phone}, "name")
        requested_language = (data.get("language") or "").strip()

        if existing_student_name:
            student = frappe.get_doc("Student", existing_student_name)
            response_school_row = (
                _get_school_row_by_id(student.school_id) if student.school_id else None
            )
            data_mismatch = {}
            existing_school_name = (
                response_school_row["school_name"] if response_school_row else ""
            )
            received_school_name = school_row["school_name"] if school_row else ""

            comparisons = {
                "school_name": (existing_school_name, received_school_name),
                "state": (student.state or "", school_row["state_id"] or ""),
                "student_name": (student.name1 or "", student_name),
                "gender": (student.gender or "", gender),
                "grade": (student.grade or "", grade),
                "language": (_get_language_id_to_name(student.language), requested_language),
            }

            for field_name, (existing_value, received_value) in comparisons.items():
                if str(existing_value or "").strip() != str(received_value or "").strip():
                    data_mismatch[field_name] = [existing_value, received_value]

            student.data_mismatch = data_mismatch
        else:
            language_id = _get_language_name_to_id(requested_language)
            student = frappe.get_doc(
                {
                    "doctype": "Student",
                    "name1": student_name,
                    "phone": phone,
                    "gender": gender,
                    "school_id": school_id,
                    "state": school_row["state_id"],
                    "grade": grade,
                    "language": language_id,
                    "joined_on": now_datetime().date(),
                    "status": "active",
                    "archetype": None,
                    "experiment_arm": None,
                }
            )
            response_school_row = school_row
            student.data_mismatch = {}

        if _is_delhi_school(school_row):
            enrollment_row = {
                "batch": DELHI_BATCH,
                "grade": grade,
                "date_joining": now_datetime().date(),
                "school": school_id,
                "whatsapp_response": 0,
            }
            delhi_course_name = DELHI_GRADE_COURSE_MAP.get(str(grade).strip())
            if delhi_course_name:
                _, _, course_level_name = _get_course_vertical_and_level(delhi_course_name, grade)
                enrollment_row["course"] = course_level_name

            has_matching_enrollment = any(
                enrollment.batch == DELHI_BATCH and enrollment.school == school_id
                for enrollment in (student.get("enrollment") or [])
            )
            if not has_matching_enrollment:
                student.append(
                    "enrollment",
                    enrollment_row,
                )

        if existing_student_name:
            student.save(ignore_permissions=True)
        else:
            student.insert(ignore_permissions=True)
        _enqueue_glific_contact_sync("Student", student.name)
        frappe.db.commit()

        _set_status(200)
        return {
            "status": "success",
            "message": (
                "Student enrollment added successfully."
                if existing_student_name
                else "Student registered successfully."
            ),
            "school_name": (
                response_school_row["school_name"] if response_school_row else ""
            ),
            "student_name": student.name1,
            "phone": student.phone,
            "gender": student.gender,
            "grade": student.grade,
            "language": _get_language_id_to_name(student.language),
        }
    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "create_student_web failed")
        _set_status(500)
        return {"status": "failure", "message": str(exc)}


@frappe.whitelist(allow_guest=True)
def teacher_whatsapp_response(phone_number):
    try:
        phone = _require_valid_phone(phone_number)
        teacher_name = frappe.db.get_value("Teacher", {"phone_number": phone}, "name")
        if not teacher_name:
            _respond(404, {"status": "failure", "message": "Teacher not found"})
            return

        teacher = frappe.get_doc("Teacher", teacher_name)
        latest_enrollment = _get_latest_enrollment(teacher)
        if not latest_enrollment:
            _respond(404, {"status": "failure", "message": "Teacher enrollment not found"})
            return

        latest_enrollment.whatsapp_response = 1
        teacher.save(ignore_permissions=True)
        frappe.db.commit()

        return {
            "student_registration_url": (
                f"http://registration.theapprenticeproject.org/student/"
                f"{latest_enrollment.school or teacher.school_id}"
            )
        }
    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "teacher_whatsapp_response failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def student_whatsapp_response(phone_number):
    try:
        phone = _require_valid_phone(phone_number)
        student_name = frappe.db.get_value("Student", {"phone": phone}, "name")
        if not student_name:
            _respond(404, {"status": "failure", "message": "Student not found"})
            return

        student = frappe.get_doc("Student", student_name)
        latest_enrollment = _get_latest_enrollment(student)
        if not latest_enrollment:
            _respond(404, {"status": "failure", "message": "Student enrollment not found"})
            return

        latest_enrollment.whatsapp_response = 1
        student.save(ignore_permissions=True)
        frappe.db.commit()

        latest_course_name = _get_course_name_from_course_level(latest_enrollment.course)
        if latest_course_name:
            return {"course1": latest_course_name}

        course_names = _get_school_course_vertical_names(latest_enrollment.school or student.school_id)
        return {
            f"course{index}": course_name
            for index, course_name in enumerate(course_names, start=1)
        }
    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "student_whatsapp_response failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def set_student_course_level(phone_number, course_name):
    try:
        phone = _require_valid_phone(phone_number)
        course_name = (course_name or "").strip()

        if course_name not in ALLOWED_STUDENT_COURSE_NAMES:
            _respond(
                400,
                {
                    "status": "failure",
                    "message": "Invalid course_name",
                    "allowed_values": sorted(ALLOWED_STUDENT_COURSE_NAMES),
                },
            )
            return

        student_name = frappe.db.get_value("Student", {"phone": phone}, "name")
        if not student_name:
            _respond(404, {"status": "failure", "message": "Student not found"})
            return

        student = frappe.get_doc("Student", student_name)
        latest_enrollment = _get_latest_enrollment(student)
        if not latest_enrollment:
            _respond(404, {"status": "failure", "message": "Student enrollment not found"})
            return

        grade_value = latest_enrollment.grade or student.grade
        if not grade_value:
            _respond(400, {"status": "failure", "message": "Student grade not found"})
            return

        try:
            course_vertical, level_label, course_level_name = _get_course_vertical_and_level(
                course_name, grade_value
            )
        except frappe.ValidationError:
            raise
        except Exception as exc:
            message = str(exc)
            if "Course vertical not found" in message:
                _respond(404, {"status": "failure", "message": "Course vertical not found"})
                return
            if "Matching course level not found" in message:
                _respond(
                    404,
                    {
                        "status": "failure",
                        "message": "Matching course level not found",
                        "course_name": course_name,
                        "grade": grade_value,
                        "level": _get_course_level_label_for_grade(grade_value),
                    },
                )
                return
            raise

        latest_enrollment.course = course_level_name
        student.save(ignore_permissions=True)
        _enqueue_glific_contact_sync("Student", student.name)
        frappe.db.commit()

        _respond(
            200,
            {
                "status": "success",
                "student_id": student.name,
                "phone": student.phone,
                "course_name": course_vertical.name2,
                "grade": grade_value,
                "level": level_label,
                "course_level": course_level_name,
            },
        )
    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "set_student_course_level failed")
        _respond(500, {"status": "failure", "message": str(exc)})
