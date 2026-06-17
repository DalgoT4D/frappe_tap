"""
ProgramEventLog Helper
tap_lms/summer_program/event_log.py

Creates ProgramEventLog records for audit trail.
Called by all state-changing operations.
"""
import frappe
import json
from frappe.utils import now_datetime


# Defensive caps for the fallback log_error path (task #29). The Error Log
# doctype's `error` (message) field is a Long Text, but on some installs a
# title or message that's pathologically long can still cascade
# CharacterLengthExceededError when Frappe formats it. Truncate before
# handing off to keep the safety net from itself becoming a bug source.
_LOG_ERROR_TITLE_CAP = 140
_LOG_ERROR_MSG_CAP = 1000


def log_event(
    enrollment,
    event_type,
    old_value=None,
    new_value=None,
    trigger_source="scheduler",
    details=None,
):
    """
    Create a ProgramEventLog entry.

    Args:
        enrollment: ProgramEnrollment doc or name
        event_type: one of the ProgramEventLog.event_type options
        old_value: previous state/value (optional)
        new_value: new state/value (optional)
        trigger_source: scheduler | glific_flow | flow_callback | admin | microservice
        details: dict of extra data (stored as JSON)

    Failure mode (task #29 hardening, 2026-05-22):
      - If the ProgramEventLog insert fails (e.g. `details` JSON exceeds a
        column length, event_type outside the Select options, or a parent
        FK violation), the except path used to call `frappe.log_error`
        unguarded. When the originating error itself contained the
        offending content (str(e) embedding the long details payload),
        log_error would cascade into another CharacterLengthExceededError
        and the original failure was silently dropped.
      - Now: rollback first (L-030 defense for any aborted Postgres txn),
        then truncate both title + message before log_error, then fall
        back to `frappe.logger().error` if log_error itself raises. The
        file logger is independent of the Frappe DB layer so it survives
        a fully-poisoned txn.
    """
    if isinstance(enrollment, str):
        enrollment = frappe.get_doc("ProgramEnrollment", enrollment)

    try:
        log = frappe.new_doc("ProgramEventLog")
        log.enrollment = enrollment.name
        log.student = enrollment.student
        log.batch = enrollment.batch
        log.program_type = enrollment.program_type
        log.week = enrollment.current_week or 0
        log.event_type = event_type
        log.old_value = str(old_value) if old_value else None
        log.new_value = str(new_value) if new_value else None
        log.trigger_source = trigger_source
        log.details = json.dumps(details) if details else None
        log.created_at = now_datetime()
        log.insert(ignore_permissions=True)
    except Exception as e:
        _safe_log_error(
            title="SP Event Log",
            message=f"ProgramEventLog error: {str(e)}",
        )


def _safe_log_error(title, message):
    """Defensive wrapper around frappe.log_error (task #29 / L-030).

    - Rollback first so a poisoned Postgres txn doesn't bubble through
      log_error itself.
    - Truncate title + message to defensive caps so over-long content
      from the originating error can't cascade another
      CharacterLengthExceededError.
    - Fall back to the file-backed logger if log_error still raises —
      that path is independent of the Frappe DB layer.
    """
    try:
        frappe.db.rollback()
    except Exception:
        # If rollback itself fails, we still want to surface the message
        # below; logger().error is DB-independent.
        pass

    safe_title = (title or "")[:_LOG_ERROR_TITLE_CAP] or "SP Event Log"
    safe_message = (message or "")[:_LOG_ERROR_MSG_CAP]

    try:
        frappe.log_error(safe_message, safe_title)
    except Exception:
        # frappe.log_error itself blew up — fall back to the file logger,
        # which doesn't touch the DB.
        frappe.logger().error(f"{safe_title}: {safe_message}")


def log_state_transition(enrollment, old_state, new_state, trigger_source="scheduler", details=None):
    """Convenience: log a resolved_flow_state transition."""
    log_event(
        enrollment,
        event_type="label_changed",
        old_value=old_state,
        new_value=new_state,
        trigger_source=trigger_source,
        details=details,
    )
