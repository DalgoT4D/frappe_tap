import frappe
from tap_lms.onboarding.utils import (
    _get_all_school_rows,
    _enqueue_glific_contact_sync,
    _ensure_teacher_enrollment,
    _get_language_id_to_name,
    _get_language_name_to_id,
    _get_latest_enrollment,
    _get_latest_school_batch_id,
    _get_request_data,
    _get_school_row_by_id,
    _get_school_row_from_input,
    _normalize_phone,
    _phone_for_response,
    _phone_filter,
    _require_valid_phone,
    _respond,
    _validate_api_key_or_respond,
    _validate_phone,
)
from tap_lms.utils.api_failures import log_api_failure


@frappe.whitelist(allow_guest=True)
def list_school_details():
    data = _get_request_data()
    try:
        api_key = data.get("api_key")

        if not _validate_api_key_or_respond(api_key):
            return

        schools = _get_all_school_rows()
        _respond(200, {"schools": schools})
    except frappe.ValidationError:
        frappe.db.rollback()
        log_api_failure("list_school_details", data, frappe.get_traceback())
        raise
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("list_school_details", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "list_school_details failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def check_teacher_exists():
    data = _get_request_data()
    try:
        phone = _normalize_phone(data.get("phone"))

        if not _validate_phone(phone):
            _respond(
                400,
                {"exists": False, "message": "Phone must be exactly 10 digits or 12 digits starting with 91"},
            )
            return

        exists = bool(frappe.db.exists("Teacher", _phone_filter("phone_number", phone)))
        _respond(200, {"exists": exists})
    except frappe.ValidationError:
        frappe.db.rollback()
        log_api_failure("check_teacher_exists", data, frappe.get_traceback())
        raise
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("check_teacher_exists", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "check_teacher_exists failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def get_teacher_details():
    data = _get_request_data()
    try:
        phone = _normalize_phone(data.get("phone"))

        if not _validate_phone(phone):
            _respond(400, {"message": "Phone must be exactly 10 digits or 12 digits starting with 91"})
            return

        teacher = frappe.db.get_value(
            "Teacher",
            _phone_filter("phone_number", phone),
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
            "phone": _phone_for_response(teacher.phone_number),
            "state": school_row["state"] if school_row else "",
            "district": school_row["district"] if school_row else "",
            "city": school_row["city"] if school_row else "",
            "school": school_row["school_name"] if school_row else "",
            "role": teacher.teacher_role or "",
            "language": _get_language_id_to_name(teacher.language),
        }
        _respond(200, payload)
    except frappe.ValidationError:
        frappe.db.rollback()
        log_api_failure("get_teacher_details", data, frappe.get_traceback())
        raise
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("get_teacher_details", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "get_teacher_details failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def update_teacher_details():
    data = _get_request_data()

    try:
        phone, phone_error = _require_valid_phone(data.get("phone"))
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return

        school_value = data.get("school")
        school_row = _get_school_row_from_input(school_value) if school_value else None
        if school_value and not school_row:
            _respond(404, {"status": "failure", "message": "School not found"})
            return

        teacher_name = frappe.db.get_value("Teacher", _phone_filter("phone_number", phone), "name")
        if not teacher_name:
            _respond(404, {"status": "failure", "message": "Teacher not found"})
            return

        teacher = frappe.get_doc("Teacher", teacher_name)
        teacher.first_name = data.get("firstName") or teacher.first_name
        teacher.last_name = data.get("lastName") or teacher.last_name
        teacher.phone_number = phone
        teacher.teacher_role = data.get("role") or teacher.teacher_role
        language_id, language_error = _get_language_name_to_id(data.get("language"))
        if language_error:
            _respond(language_error["code"], language_error["payload"])
            return
        teacher.language = language_id or teacher.language
        if school_row:
            teacher.school_id = school_row["school_id"]
            teacher.state = school_row["state_id"]
            teacher.teacher_batch = _get_latest_school_batch_id(school_row["school_id"])
            if teacher.teacher_batch:
                _ensure_teacher_enrollment(teacher, school_row, teacher.teacher_batch)

        teacher.save(ignore_permissions=True)
        _enqueue_glific_contact_sync("Teacher", teacher.name)
        frappe.db.commit()
        _respond(200, {"status": "success", "message": "Teacher details updated successfully."})
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("update_teacher_details", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "update_teacher_details failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def create_teacher_web():
    data = _get_request_data()

    try:
        if not _validate_api_key_or_respond(data.get("api_key")):
            return

        phone, phone_error = _require_valid_phone(data.get("phone"))
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return

        first_name = (data.get("firstName") or "").strip()
        school_value = (data.get("school") or "").strip()
        if not first_name or not school_value:
            _respond(400, {
                "status": "failure",
                "message": "Missing required field: firstName or school",
            })
            return

        if frappe.db.exists("Teacher", _phone_filter("phone_number", phone)):
            _respond(409, {
                "status": "failure",
                "message": "A teacher with this phone number already exists",
            })
            return

        school_row = _get_school_row_from_input(school_value)
        if not school_row:
            _respond(404, {"status": "failure", "message": "School not found"})
            return

        language_id, language_error = _get_language_name_to_id(data.get("language"))
        if language_error:
            _respond(language_error["code"], language_error["payload"])
            return

        teacher = frappe.get_doc(
            {
                "doctype": "Teacher",
                "first_name": first_name,
                "last_name": (data.get("lastName") or "").strip(),
                "gender": (data.get("gender") or "").strip(),
                "phone_number": phone,
                "teacher_role": (data.get("role") or "").strip(),
                "language": language_id,
                "school_id": school_row["school_id"],
                "state": school_row["state_id"],
                "teacher_batch": _get_latest_school_batch_id(school_row["school_id"]),
            }
        )
        if teacher.teacher_batch:
            _ensure_teacher_enrollment(teacher, school_row, teacher.teacher_batch)
        teacher.insert(ignore_permissions=True)
        _enqueue_glific_contact_sync("Teacher", teacher.name)
        frappe.db.commit()

        _respond(200, {
            "status": "success",
            "message": "Teacher created successfully.",
            "teacher_id": teacher.name,
        })
        return
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("create_teacher_web", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "create_teacher_web failed")
        _respond(500, {"status": "failure", "message": str(exc)})


@frappe.whitelist(allow_guest=True)
def teacher_whatsapp_response(phone_number):
    try:
        phone, phone_error = _require_valid_phone(phone_number)
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return

        teacher_name = frappe.db.get_value("Teacher", _phone_filter("phone_number", phone), "name")
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

        _respond(200, {
            "student_registration_url": (
                f"http://registration.theapprenticeproject.org/student/"
                f"{latest_enrollment.school or teacher.school_id}"
            ),
            "student_consent_url": (
                f"https://api.whatsapp.com/send?phone=918454812392&text=tapschool:"
                f"{latest_enrollment.school or teacher.school_id}"
            ),
        })
        return
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure(
            "teacher_whatsapp_response",
            {"phone_number": phone_number},
            frappe.get_traceback(),
        )
        frappe.log_error(frappe.get_traceback(), "teacher_whatsapp_response failed")
        _respond(500, {"status": "failure", "message": str(exc)})
