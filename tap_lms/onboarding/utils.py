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


def _normalize_phone(phone):
    phone = str(phone or "").strip()
    if len(phone) == 12 and phone.startswith("91"):
        phone = phone[2:]
    return phone


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
    return bool(PHONE_PATTERN.fullmatch(_normalize_phone(phone)))


def _require_valid_phone(phone):
    phone = _normalize_phone(phone)
    if not _validate_phone(phone):
        return None, {
            "code": 400,
            "payload": {
                "status": "failure",
                "message": "Phone must be exactly 10 digits or 12 digits starting with 91",
            },
        }
    return phone, None


def _get_language_name_to_id(language_name):
    if not language_name:
        return None, None

    language_id = frappe.db.get_value(
        "TAP Language",
        {"language_name": language_name},
        "name",
    )
    if not language_id:
        return None, {
            "code": 400,
            "payload": {
                "status": "failure",
                "message": f"Invalid language: {language_name}",
            },
        }
    return language_id, None


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


def _get_latest_enrollment(doc):
    enrollments = list(doc.get("enrollment") or [])
    if not enrollments:
        return None

    def _sort_key(item):
        if item.date_joining:
            return (1, getdate(item.date_joining), item.idx or 0)
        return (0, getdate("1900-01-01"), item.idx or 0)

    return max(enrollments, key=_sort_key)


def _enqueue_glific_contact_sync(doctype, docname):
    enqueue_registration_contact_sync(doctype, docname)


def sync_registration_contact_to_glific(doctype, docname, retry_count=0):
    return _sync_registration_contact_to_glific(doctype, docname, retry_count=retry_count)
