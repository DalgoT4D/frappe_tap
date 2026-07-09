import frappe
from frappe.utils import now_datetime

from tap_lms.onboarding.utils import (
    DELHI_BATCH,
    _enqueue_glific_contact_sync,
    _get_language_id_to_name,
    _get_language_name_to_id,
    _get_latest_enrollment,
    _get_request_data,
    _get_school_row_by_id,
    _is_delhi_school,
    _require_valid_phone,
    _respond,
    _set_status,
)
from tap_lms.utils.api_failures import log_api_failure


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


def _get_course_name_from_course_level(course_level_name):
    if not course_level_name:
        return ""

    vertical_name = frappe.db.get_value("Course Level", course_level_name, "vertical")
    if not vertical_name:
        return ""

    return frappe.db.get_value("Course Verticals", vertical_name, "name2") or ""


def _get_course_level_label_for_grade(grade):
    try:
        grade_num = int(str(grade).strip())
    except Exception:
        return None, {
            "code": 400,
            "payload": {
                "status": "failure",
                "message": f"Invalid grade: {grade}",
            },
        }

    if grade_num <= 5:
        return "Level 1", None
    if 6 <= grade_num <= 8:
        return "Level 2", None
    if 9 <= grade_num <= 10:
        return "Level 3", None
    if 11 <= grade_num <= 12:
        return "Level 4", None

    return None, {
        "code": 400,
        "payload": {
            "status": "failure",
            "message": f"Unsupported grade for course level mapping: {grade}",
        },
    }


def _get_course_vertical_and_level(course_name, grade):
    course_vertical = frappe.db.get_value(
        "Course Verticals",
        {"name2": course_name},
        ["name", "name2"],
        as_dict=True,
    )
    if not course_vertical:
        return None, {
            "code": 404,
            "payload": {
                "status": "failure",
                "message": "Course vertical not found",
            },
        }

    level_label, level_error = _get_course_level_label_for_grade(grade)
    if level_error:
        return None, level_error
    course_level_name = frappe.db.get_value(
        "Course Level",
        {"vertical": course_vertical.name, "level": level_label},
        "name",
    )
    if not course_level_name:
        return None, {
            "code": 404,
            "payload": {
                "status": "failure",
                "message": "Matching course level not found",
                "course_name": course_name,
                "grade": grade,
                "level": level_label,
            },
        }

    return {
        "course_vertical": course_vertical,
        "level_label": level_label,
        "course_level_name": course_level_name,
    }, None


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


def _create_student_consent_record(phone_number, whatsapp_consent=0):
    phone_number, phone_error = _require_valid_phone(phone_number)
    if phone_error:
        frappe.log_error(phone_error["payload"]["message"], "create_student_consent_record failed")
        return
    consent_doc = frappe.get_doc(
        {
            "doctype": "Student Consent",
            "phone_number": str(phone_number).strip(),
            "whatsapp_consent": int(whatsapp_consent or 0),
        }
    )
    consent_doc.insert(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist(allow_guest=True)
def verify_school_by_id(school_id, phone_number=None):
    try:
        school_id = str(school_id or "").strip()
        if not school_id:
            _respond(400, {"status": "failure", "message": "school_id is required"})
            return

        if phone_number:
            phone_number, phone_error = _require_valid_phone(phone_number)
            if phone_error:
                _respond(phone_error["code"], phone_error["payload"])
                return

        school_row = _get_school_row_by_id(school_id)
        if not school_row:
            _respond(404, {"status": "failure", "message": "School not found"})
            return

        if phone_number:
            frappe.enqueue(
                "tap_lms.onboarding.student_registration._create_student_consent_record",
                queue="default",
                phone_number=phone_number,
                whatsapp_consent=0,
            )

        _respond(
            200,
            {
                "status": "success",
                "school_id": school_row["school_id"],
                "school_name": school_row["school_name"],
                "state": school_row["state"],
                "district": school_row["district"],
                "city": school_row["city"],
                "student_registration_url": (
                    f"http://registration.theapprenticeproject.org/student/{school_row['school_id']}"
                ),
            },
        )
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure(
            "verify_school_by_id",
            {"school_id": school_id, "phone_number": phone_number},
            frappe.get_traceback(),
        )
        frappe.log_error(frappe.get_traceback(), "verify_school_by_id failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def create_student_web():
    data = _get_request_data()

    try:
        phone, phone_error = _require_valid_phone(data.get("phone"))
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return
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
            language_id, language_error = _get_language_name_to_id(requested_language)
            if language_error:
                _respond(language_error["code"], language_error["payload"])
                return
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
                course_level_data, course_level_error = _get_course_vertical_and_level(
                    delhi_course_name, grade
                )
                if course_level_error:
                    _respond(course_level_error["code"], course_level_error["payload"])
                    return
                enrollment_row["course"] = course_level_data["course_level_name"]

            has_matching_enrollment = any(
                enrollment.batch == DELHI_BATCH and enrollment.school == school_id
                for enrollment in (student.get("enrollment") or [])
            )
            if not has_matching_enrollment:
                student.append("enrollment", enrollment_row)

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
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("create_student_web", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "create_student_web failed")
        _set_status(500)
        return {"status": "failure", "message": str(exc)}


@frappe.whitelist(allow_guest=True)
def student_whatsapp_response(phone_number):
    try:
        phone, phone_error = _require_valid_phone(phone_number)
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
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

        latest_enrollment.whatsapp_response = 1
        student.save(ignore_permissions=True)
        frappe.db.commit()

        latest_course_name = _get_course_name_from_course_level(latest_enrollment.course)
        if latest_course_name:
            return {
                "course1": latest_course_name,
                "courses_num": 1,
            }

        course_names = _get_school_course_vertical_names(latest_enrollment.school or student.school_id)
        response = {
            f"course{index}": course_name
            for index, course_name in enumerate(course_names, start=1)
        }
        response["courses_num"] = len(course_names)
        return response
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure(
            "student_whatsapp_response",
            {"phone_number": phone_number},
            frappe.get_traceback(),
        )
        frappe.log_error(frappe.get_traceback(), "student_whatsapp_response failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def set_student_course_level(phone_number, course_name):
    try:
        phone, phone_error = _require_valid_phone(phone_number)
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return
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

        course_level_data, course_level_error = _get_course_vertical_and_level(course_name, grade_value)
        if course_level_error:
            _respond(course_level_error["code"], course_level_error["payload"])
            return

        latest_enrollment.course = course_level_data["course_level_name"]
        student.save(ignore_permissions=True)
        _enqueue_glific_contact_sync("Student", student.name)
        frappe.db.commit()

        _respond(
            200,
            {
                "status": "success",
                "student_id": student.name,
                "phone": student.phone,
                "course_name": course_level_data["course_vertical"].name2,
                "grade": grade_value,
                "level": course_level_data["level_label"],
                "course_level": course_level_data["course_level_name"],
            },
        )
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure(
            "set_student_course_level",
            {"phone_number": phone_number, "course_name": course_name},
            frappe.get_traceback(),
        )
        frappe.log_error(frappe.get_traceback(), "set_student_course_level failed")
        _respond(500, {"status": "failure", "message": str(exc)})
