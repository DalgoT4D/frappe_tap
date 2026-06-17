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

    Failure mode (task #29 hardening, 2026-05-22; B-2 / L-082 fix 2026-06-15):
      - If the ProgramEventLog insert fails (e.g. `details` JSON exceeds a
        column length, event_type outside the Select options, or a parent
        FK violation), the original 2026-05-22 hardening called
        `_safe_log_error` which started with an UNCONDITIONAL
        `frappe.db.rollback()` for L-030 defense. That rollback also
        discarded the caller's uncommitted work — the transition() call's
        set_value, the dispatcher's atomic claim UPDATE — while transition()
        returned True regardless. L-082 documented this as a latent
        silent-no-persist bug; the deep-review B-2 elevated it.
      - Now: wrap the PEL insert in a Postgres SAVEPOINT so a failure rolls
        back ONLY the PEL row, not the caller's transaction. Then truncate
        title + message before log_error, then fall back to
        `frappe.logger().error` if log_error itself raises. The file logger
        is independent of the Frappe DB layer so it survives a fully-poisoned
        txn. L-030 defense remains live at the per-endpoint level (L-077).
    """
    if isinstance(enrollment, str):
        enrollment = frappe.get_doc("ProgramEnrollment", enrollment)

    # B-2 (2026-06-15) — savepoint isolates PEL insert failures from the
    # caller's in-flight transaction. See _update_engagement and
    # _log_student_content_submission in save_submission.py for the same
    # pattern; this restores transition() / atomic-claim atomicity.
    sp = f"pel_{frappe.utils.random_string(6)}"
    try:
        frappe.db.savepoint(sp)
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
        frappe.db.release_savepoint(sp)
    except Exception as e:
        # Roll back ONLY the PEL insert; caller's transaction stays alive.
        try:
            frappe.db.rollback(save_point=sp)
        except Exception:
            # Savepoint rollback shouldn't fail in normal operation; if it
            # does (e.g. txn aborted by a prior statement), the L-077 helper
            # at the endpoint level will handle the broader rollback.
            pass
        _safe_log_error(
            title="SP Event Log",
            message=f"ProgramEventLog error: {str(e)}",
        )


def _safe_log_error(title, message):
    """Length-capped log_error with file-logger fallback (task #29).

    - Truncate title + message to defensive caps so over-long content
      from the originating error can't cascade another
      CharacterLengthExceededError.
    - Fall back to the file-backed logger if log_error still raises —
      that path is independent of the Frappe DB layer.

    NOTE (B-2 / L-082 fix, 2026-06-15): pre-2026-06-15 this helper started
    with an unconditional frappe.db.rollback() as L-030 defense. That
    rollback silently discarded the caller's in-flight transaction
    whenever a PEL insert failed for ANY reason. log_event now uses a
    savepoint so the failure scope stays inside log_event's responsibility,
    and the unconditional rollback was removed. L-030 defense remains live
    at the per-endpoint level via safe_sp_api_error_response (L-077).
    """
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
