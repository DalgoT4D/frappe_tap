import frappe
from frappe.utils import getdate

from tap_lms.glific_integration import (
    create_contact,
    get_contact_by_phone,
    optin_contact,
    register_contact_field,
    update_contact_fields,
)
from tap_lms.school_utils import get_school_state_model_details


GLIFIC_SYNC_MAX_RETRIES = 3
_SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED = False


def _get_language_id_to_name(language_id):
    if not language_id:
        return ""
    return frappe.db.get_value("TAP Language", language_id, "language_name") or language_id


def _get_glific_language_id(language_name):
    if language_name:
        language_docname = frappe.db.get_value(
            "TAP Language",
            {"language_name": language_name},
            "name",
        )
        if language_docname:
            language_id = frappe.db.get_value(
                "TAP Language",
                language_docname,
                "glific_language_id",
            )
            if language_id:
                return language_id

    return frappe.db.get_value(
        "TAP Language",
        {"language_name": "English"},
        "glific_language_id",
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


def _get_student_batch_id(student_doc):
    for enrollment in student_doc.get("enrollment") or []:
        if enrollment.batch:
            return enrollment.batch
    return ""


def _get_student_course_name(student_doc):
    latest_enrollment = _get_latest_enrollment(student_doc)
    if not latest_enrollment or not latest_enrollment.course:
        return ""

    vertical_name = frappe.db.get_value(
        "Course Level",
        latest_enrollment.course,
        "vertical",
    )
    if not vertical_name:
        return ""

    return frappe.db.get_value("Course Verticals", vertical_name, "name2") or ""


def _build_teacher_glific_fields(teacher_doc, school_meta):
    return {
        "school": school_meta["school_name"],
        "state": school_meta["state_name"],
        "model": school_meta["model_name"],
        "buddy_name": (teacher_doc.first_name or "").strip(),
        "batch_id": teacher_doc.teacher_batch or "",
        "role": teacher_doc.teacher_role or "",
    }


def _build_student_glific_fields(student_doc, school_meta):
    return {
        "school": school_meta["school_name"],
        "state": school_meta["state_name"],
        "model": school_meta["model_name"],
        "buddy_name": (student_doc.name1 or "").strip(),
        "batch_id": _get_student_batch_id(student_doc),
        "grade": student_doc.grade or "",
        "course": _get_student_course_name(student_doc),
    }


def enqueue_registration_contact_sync(doctype, docname):
    frappe.enqueue(
        "tap_lms.onboarding.glific_sync.sync_registration_contact_to_glific",
        queue="short",
        timeout=60,
        enqueue_after_commit=True,
        doctype=doctype,
        docname=docname,
        retry_count=0,
    )


def _ensure_glific_registration_fields():
    global _SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED

    if _SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED:
        return

    required_fields = (
        ("school", "School"),
        ("state", "State"),
        ("model", "Model"),
        ("buddy_name", "Buddy Name"),
        ("batch_id", "Batch ID"),
        ("role", "Role"),
        ("grade", "Grade"),
        ("course", "Course"),
    )

    for shortcode, display_name in required_fields:
        if not register_contact_field(shortcode, display_name):
            raise RuntimeError(f"Failed to register Glific contact field: {shortcode}")

    _SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED = True


def sync_registration_contact_to_glific(doctype, docname, retry_count=0):
    try:
        if doctype not in {"Teacher", "Student"}:
            raise ValueError(f"Unsupported doctype for Glific sync: {doctype}")

        _ensure_glific_registration_fields()
        doc = frappe.get_doc(doctype, docname)
        school_meta = get_school_state_model_details(getattr(doc, "school_id", None))
        phone = getattr(doc, "phone_number", None) or getattr(doc, "phone", None)

        if not phone:
            raise ValueError(f"{doctype} {docname} has no phone number")

        if doctype == "Teacher":
            contact_name = (doc.first_name or "").strip() or docname
            fields_to_update = _build_teacher_glific_fields(doc, school_meta)
        else:
            contact_name = (doc.name1 or "").strip() or docname
            fields_to_update = _build_student_glific_fields(doc, school_meta)

        language_name = _get_language_id_to_name(doc.language)
        language_id = _get_glific_language_id(language_name)
        glific_contact = get_contact_by_phone(phone)

        if glific_contact and glific_contact.get("id"):
            glific_id = str(glific_contact["id"])
            if str(getattr(doc, "glific_id", "") or "").strip() != glific_id:
                frappe.db.set_value(doctype, docname, "glific_id", glific_id)
        else:
            glific_contact = create_contact(
                contact_name,
                phone,
                school_meta["school_name"],
                school_meta["model_name"],
                language_id,
                fields_to_update["batch_id"],
            )
            if not glific_contact or not glific_contact.get("id"):
                raise RuntimeError(f"Failed to create or link Glific contact for {doctype} {docname}")

            glific_id = str(glific_contact["id"])
            frappe.db.set_value(doctype, docname, "glific_id", glific_id)

        if doctype == "Teacher" and not optin_contact(phone, contact_name):
            raise RuntimeError(f"optin_contact returned False for Teacher {docname} ({phone})")

        ok = update_contact_fields(
            glific_id,
            fields_to_update,
            language_id=language_id,
            sync_status_doctype=doctype,
            sync_status_docname=docname,
        )
        if not ok:
            raise RuntimeError(
                f"update_contact_fields returned False for {doctype} {docname} ({glific_id})"
            )

        frappe.db.commit()
    except Exception as exc:
        retry_count = (retry_count or 0) + 1
        if retry_count <= GLIFIC_SYNC_MAX_RETRIES:
            try:
                from datetime import timedelta
                from frappe.utils.background_jobs import get_queue

                get_queue("short").enqueue_in(
                    timedelta(seconds=min(60, 2 ** retry_count)),
                    "tap_lms.onboarding.glific_sync.sync_registration_contact_to_glific",
                    kwargs={
                        "doctype": doctype,
                        "docname": docname,
                        "retry_count": retry_count,
                    },
                    job_timeout=60,
                )
            except Exception:
                frappe.enqueue(
                    "tap_lms.onboarding.glific_sync.sync_registration_contact_to_glific",
                    queue="short",
                    timeout=60,
                    doctype=doctype,
                    docname=docname,
                    retry_count=retry_count,
                )
            return

        frappe.db.rollback()
        frappe.log_error(
            title="Scratch registration Glific sync failed",
            message=(
                f"doctype={doctype}, docname={docname}, "
                f"retry_count={retry_count}, error={exc}"
            ),
        )
