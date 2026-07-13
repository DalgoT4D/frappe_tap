import json

import frappe


def _serialize_input_payload(input_payload):
    if input_payload is None:
        return "{}"

    if isinstance(input_payload, str):
        return input_payload

    try:
        return json.dumps(input_payload, default=str, ensure_ascii=True)
    except Exception:
        return str(input_payload)


def _write_api_failure(method_name, input_payload, error_trace=None):
    """Background worker target that writes API Failures rows."""
    try:
        doc = frappe.get_doc(
            {
                "doctype": "API Failures",
                "method_name": method_name,
                "input_payload": _serialize_input_payload(input_payload),
                "error": error_trace or frappe.get_traceback(),
                "resolved": 0,
            }
        )
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Failed to log API failure for {method_name}",
        )


def log_api_failure(method_name, input_payload, error_trace=None):
    """Enqueue best-effort API failure logging without blocking the caller."""
    frappe.flags.api_failure_logged = True
    try:
        frappe.enqueue(
            "tap_lms.utils.api_failures._write_api_failure",
            queue="short",
            timeout=60,
            enqueue_after_commit=False,
            method_name=method_name,
            input_payload=input_payload,
            error_trace=error_trace,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Failed to enqueue API failure log for {method_name}",
        )
