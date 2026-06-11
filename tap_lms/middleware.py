# tap_lms/middleware.py
#
# Frappe request lifecycle hooks registered in hooks.py.
# Captures HTTP request latency, status codes, unhandled exceptions,
# and student/Glific identifiers from request parameters.
#
# Registered via:
#   before_request = ["tap_lms.middleware.before_request"]
#   after_request  = ["tap_lms.middleware.after_request"]
#   on_exception   = ["tap_lms.middleware.on_exception"]

import time
import traceback
import frappe
from tap_lms.monitoring import record_request, emit_structured_log

# Fields Glific sends that identify a student.
# Checked in order — first one found is used.
_STUDENT_ID_FIELDS = ("student_id", "glific_id", "phone", "name1")
_GLIFIC_ID_FIELDS  = ("glific_id",)


def _extract_student_context():
    """
    Extract student_id and glific_id from the current request parameters.

    Glific sends one of several identifiers depending on the endpoint:
      - glific_id   — WhatsApp contact ID in Glific (most common)
      - student_id  — Frappe Student document name
      - phone       — student's phone number
      - name1       — student's name (used in submit_artwork)

    Returns (student_id, glific_id) — either may be None if not present.
    """
    try:
        form_dict = getattr(frappe.local, "form_dict", {}) or {}

        student_id = None
        for field in _STUDENT_ID_FIELDS:
            val = form_dict.get(field)
            if val and str(val).strip():
                student_id = str(val).strip()
                break

        glific_id = form_dict.get("glific_id")
        if glific_id:
            glific_id = str(glific_id).strip() or None

        return student_id, glific_id
    except Exception:
        return None, None


def before_request() -> None:
    """Store request start time on frappe.local for duration calculation."""
    try:
        frappe.local._sre_t0 = time.monotonic()
    except Exception:
        pass


def after_request() -> None:
    """
    Calculate request duration and emit a structured http_request log line.
    Called after every request completes, regardless of status code.

    Includes student_id and glific_id from request params so every
    Glific→tap_lms API call is attributable to a specific student.
    """
    try:
        t0 = getattr(frappe.local, "_sre_t0", None)
        duration_ms = (time.monotonic() - t0) * 1000 if t0 is not None else None

        req = getattr(frappe.local, "request", None)
        path = req.path if req else "unknown"
        method = req.method if req else "unknown"

        response = getattr(frappe.local, "response", None)
        if isinstance(response, dict):
            status_code = response.get("http_status_code", 200)
        else:
            status_code = 200

        student_id, glific_id = _extract_student_context()

        record_request(
            path=path,
            method=method,
            status_code=int(status_code),
            duration_ms=duration_ms,
            student_id=student_id,
            glific_id=glific_id,
        )
    except Exception:
        pass  # monitoring must never crash the app


def on_exception() -> None:
    """
    Emit a structured error log on any unhandled exception.
    Includes student context so errors are attributable to a student.
    The full traceback is included so Cloud Logging can surface it.
    """
    try:
        req = getattr(frappe.local, "request", None)
        student_id, glific_id = _extract_student_context()

        emit_structured_log(
            severity="ERROR",
            message="unhandled_exception",
            path=req.path if req else "unknown",
            method=req.method if req else "unknown",
            student_id=student_id,
            glific_id=glific_id,
            traceback=traceback.format_exc(),
        )
    except Exception:
        pass
