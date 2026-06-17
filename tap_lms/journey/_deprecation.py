"""
Journey API deprecation helper.
tap_lms/journey/_deprecation.py

Added 2026-05-28 pre-launch. The legacy `journey/*` whitelisted endpoints
(student_progression, student_api, student_preferences_api, api.py) are
replaced by the `summer_program/*` module for the live cohort. To prevent
stale callers (old Glific flows, test rigs, third-party probes) from
triggering side effects against the deprecated code paths during launch,
every legacy whitelisted endpoint now returns a structured deprecation
response immediately.

Behaviour per call:
  - Returns the api-standard-glific.md flat-map shape:
      success: False
      status:  "deprecated"
      error_detail: short explanation
      replacement: hint at the SP-module replacement (if known)
  - Writes a once-per-process-instance log line at INFO level so we can
    spot which old callers are still hitting us.
  - Absorbs ANY kwargs the caller sends (signature is `**_kw`) so unknown
    args don't trigger a TypeError that would mask the deprecation
    signal.

Internal jobs in `journey/background_jobs.py` are NOT deprecated — the
SP module's `complete_content` still enqueues them via frappe.enqueue.
This file deprecates only the whitelisted (Glific-callable) surface.
"""
import frappe


# Map old endpoint name → suggested SP replacement (best-effort; some old
# endpoints have no direct SP equivalent, in which case we just say so).
_REPLACEMENT_HINT = {
    # journey/student_progression.py — replaced by summer_program/student_progression_sp.py
    "get_next_content":             "tap_lms.summer_program.student_progression_sp.get_next_content",
    "get_content_details":          "tap_lms.summer_program.student_progression_sp.get_content_details",
    "complete_content":             "tap_lms.summer_program.student_progression_sp.complete_content",
    "start_quiz":                   "tap_lms.summer_program.student_progression_sp.start_quiz",
    "submit_answer":                "tap_lms.summer_program.student_progression_sp.submit_answer",
    "get_quiz_status":              "tap_lms.summer_program.student_progression_sp.get_quiz_status (if present) or get_student_state",
    "get_student_progress_overview": "tap_lms.summer_program.program_enrollment_api.get_student_state",
    "get_student_history":          "(no direct SP equivalent — query StudentContentLog / ProgramEventLog directly if needed)",
    # journey/api.py
    "track_interaction":            "(no SP equivalent — interactions now flow through state-machine transitions, not stage-based webhooks)",
    "update_student_stage":         "(no SP equivalent — use summer_program.dev_tools.update_student_state for QA, or the canonical state-machine transitions for production)",
    # journey/student_api.py
    "get_profile":                  "(use Frappe REST API: GET /api/resource/Student/<id>, or tap_lms.summer_program.program_enrollment_api.get_student_state)",
    "search":                       "(use Frappe REST API search: GET /api/method/frappe.client.get_list?doctype=Student&filters=...)",
    "get_student_glific_groups":    "(no SP equivalent — group membership is now maintained by summer_program.collection_membership)",
    "get_student_minimal_details":  "tap_lms.summer_program.program_enrollment_api.get_student_state",
    "update_student_fields":        "tap_lms.summer_program.dev_tools.update_student_state",
    "get_siblings":                 "(no SP equivalent — sibling handling is now upstream in PE enrollment per SP design)",
    "check_student":                "(use GET /api/resource/Student?filters=[[\"phone\",\"=\",\"<phone>\"]])",
    # journey/student_preferences_api.py
    "update_student_preferences":   "(no SP equivalent — preferred day/time was a legacy feature; the SP flow uses a fixed Tuesday cadence)",
    "get_student_preferences":      "(same as above — feature deprecated, no replacement)",
}


def journey_deprecated_response(endpoint_name, called_kwargs=None):
    """Return the standard journey-deprecation envelope.

    Args:
        endpoint_name: bare function name (no module prefix). Used in the
                       log line and to look up a replacement hint.
        called_kwargs: dict of kwargs the caller passed. Optional; if
                       provided, logged so we can see who's still calling
                       and with what.

    Returns:
        A flat dict per docs/api-standard-glific.md. Frappe's whitelist
        infrastructure wraps it in the `message` envelope for legacy
        clients; callers reading `@results.webhook.success` (flow editor)
        and `@results.webhook.status` see the deprecation immediately.
    """
    replacement = _REPLACEMENT_HINT.get(endpoint_name) or "(no SP-module replacement registered)"

    # Log once at INFO so we can grep `journey: deprecated call` in the logs.
    # Use frappe.logger().info (not log_error) — this is operational data,
    # not an error condition.
    try:
        frappe.logger("tap_lms_journey").info({
            "event": "journey_deprecated_endpoint_called",
            "endpoint": endpoint_name,
            "kwargs": called_kwargs or {},
            "replacement_hint": replacement,
        })
    except Exception:
        # Logging is best-effort — never let it break the response.
        pass

    return {
        "success": False,
        "status": "deprecated",
        "error_detail": (
            f"The `journey.{endpoint_name}` endpoint has been deprecated as of "
            f"2026-05-28 and no longer processes requests. The Summer Program "
            f"flow has replaced this surface."
        ),
        "replacement": replacement,
        "deprecated_at": "2026-05-28",
    }
