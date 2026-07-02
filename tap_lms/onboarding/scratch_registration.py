import json
import re

import frappe
from frappe.utils import now_datetime

from tap_lms.api import authenticate_api_key


DELHI_BATCH = "BT00000024" 
PHONE_PATTERN = re.compile(r"^\d{10}$")


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


def _get_school_row_by_name(school_name):
    rows = frappe.db.sql(
        """
        SELECT
            s.name AS school_id,
            s.name1 AS school_name,
            COALESCE(st.state_name, s.state, '') AS state,
            COALESCE(d.district_name, s.district, '') AS district,
            COALESCE(c.city_name, s.city, '') AS city
        FROM `tabSchool` s
        LEFT JOIN `tabState` st ON st.name = s.state
        LEFT JOIN `tabDistrict` d ON d.name = s.district
        LEFT JOIN `tabCity` c ON c.name = s.city
        WHERE s.name1 = %s
        LIMIT 1
        """,
        (school_name,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _is_delhi_school(school_row):
    return (school_row.get("state") or "").strip().upper() == "DELHI"


def _ensure_student_batch(student_doc, school_row):
    if not _is_delhi_school(school_row):
        return

    for enrollment in student_doc.get("enrollment") or []:
        if enrollment.batch == DELHI_BATCH:
            return

    student_doc.append(
        "enrollment",
        {
            "batch": DELHI_BATCH,
            "grade": student_doc.grade,
            "date_joining": now_datetime().date(),
            "school": school_row["school_id"],
        },
    )


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
        school_name = data.get("school")
        school_row = _get_school_row_by_name(school_name) if school_name else None
        if school_name and not school_row:
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
            if _is_delhi_school(school_row):
                teacher.teacher_batch = DELHI_BATCH

        teacher.save(ignore_permissions=True)
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
    # - phone
    # - role
    # - language
    # - School_name
    data = _get_request_data()

    try:
        if not _validate_api_key_or_respond(data.get("api_key")):
            return

        phone = _require_valid_phone(data.get("phone"))
        first_name = (data.get("firstName") or "").strip()
        school_name = (data.get("School_name") or "").strip()

        if not first_name or not school_name:
            _set_status(400)
            return {
                "status": "failure",
                "message": "Missing required field: firstName or School_name",
            }

        if frappe.db.exists("Teacher", {"phone_number": phone}):
            _set_status(409)
            return {
                "status": "failure",
                "message": "A teacher with this phone number already exists",
            }

        school_row = _get_school_row_by_name(school_name)
        if not school_row:
            _set_status(404)
            return {"status": "failure", "message": "School not found"}

        teacher = frappe.get_doc(
            {
                "doctype": "Teacher",
                "first_name": first_name,
                "last_name": (data.get("lastName") or "").strip(),
                "phone_number": phone,
                "teacher_role": (data.get("role") or "").strip(),
                "language": _get_language_name_to_id(data.get("language")),
                "school_id": school_row["school_id"],
                "teacher_batch": DELHI_BATCH if _is_delhi_school(school_row) else None,
            }
        )
        teacher.insert(ignore_permissions=True)
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

        student = frappe.get_doc(
            {
                "doctype": "Student",
                "name1": student_name,
                "phone": phone,
                "gender": gender,
                "school_id": school_id,
                "grade": grade,
                "language": _get_language_name_to_id(data.get("language")),
                "joined_on": now_datetime().date(),
                "status": "active",
            }
        )
        _ensure_student_batch(student, school_row)
        student.insert(ignore_permissions=True)
        frappe.db.commit()

        _set_status(200)
        return {
            "status": "success",
            "message": "Student registered successfully.",
        }
    except frappe.ValidationError:
        frappe.db.rollback()
        raise
    except Exception as exc:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "create_student_web failed")
        _set_status(500)
        return {"status": "failure", "message": str(exc)}
