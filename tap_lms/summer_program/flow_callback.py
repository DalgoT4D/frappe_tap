"""
Flow Callback — update_flow_status API
tap_lms/summer_program/flow_callback.py

THE critical callback that every Glific flow calls on completion.
This is the bridge between Glific and the backend state machine.

Called by:
  - SP_Content_Delivery (on timeout → no_response, on completion)
  - SP_Escalation (on completion)
  - SP_Feedback_Delivery (on completion → triggers week_completed → week_advancement)
  - SP_Paused_Binge (on completion)
  - SP_Program_Complete (on completion)
  - SP_Submission (on completion)

CR-003: SP_Grace_Reminder and SP_Paused_Reengagement entries removed.
Grace reminders are gone (escalation steps within the week ARE the reminders).
Re-engagement is now inbound-only via SP_Incoming_Router (Glific routes a
rejoin path when program_status='dropped').

CR-003 follow-up (2026-05-13): SP_Grace_Entry retired. There is no longer a
separate "you've entered grace" Glific flow — the per-week escalation chain
inside the active week IS the reminder cadence. `_handle_grace_flow` deleted
along with the `SP_Grace_Entry` entry in `_get_handler`.
"""
import frappe
from frappe import _
from frappe.utils import now_datetime

from tap_lms.summer_program.constants import (
    STATE_NORMAL_CONTENT, STATE_NORMAL_ESCALATION,
    STATE_REMEDIAL_CONTENT, STATE_REMEDIAL_ESCALATION,
    STATE_SUBMITTED_AWAITING, STATE_FEEDBACK_READY,
    STATE_GRACE_WAITING, STATE_WEEK_COMPLETED,
    STATE_PAUSED_BINGE,
    PROGRAM_ACTIVE,
    ACTION_ESCALATION, ACTION_CONTENT_DELIVERY,
    TERMINAL_STATES,
)
from tap_lms.summer_program.state_machine import (
    get_active_pe,
    t1_content_no_response,
    t2_start_escalation,
    t13_feedback_delivered,
)
from tap_lms.summer_program.event_log import log_event
from tap_lms.summer_program.utils import sp_safe_endpoint, check_glific_placeholders


# ════════════════════════════════════════════════════════════
# RESPONSE HELPER — v3.0 contract
# ════════════════════════════════════════════════════════════
# Glific Integration Guide v3.0 §1.2: every webhook response from update_flow_status
# MUST return these four fields so Glific flows can read @results.webhook.* for
# immediate routing without waiting for the async contact-field sync.

def _response(pe, status_value, **extras):
    """Build a v3.0-compliant webhook response AND write it to frappe.local.response.

    Per docs/api-standard-glific.md Rule 1, whitelisted endpoints consumed by
    Glific write directly to local.response so Glific reads `@results.webhook.<field>`
    without the Frappe `message.` envelope. This helper does the write AND
    returns the dict so handlers can chain (`return _response(pe, "x")`) — the
    outer whitelisted method still returns None (Frappe accepts that), but the
    dict in local.response is what Glific sees.

    Glific Rule 6 (docs/api-standard-glific.md): the outcome field must be
    `status`, not `action`. As of task #73 (2026-XX-XX) we write BOTH keys for
    one release cycle so existing Glific flows that read
    `@results.webhook.action` keep working (L-009 — whitelisted-output renames
    are public-contract breaks without back-compat). DROP the `action` key in
    a follow-up CR after all SP_* flows are audited to read
    `@results.webhook.status` instead.

    Always emits: success, status, action (deprecated alias), resolved_flow_state,
    next_action_type, next_action_at, program_status. Additional fields can be
    passed as **extras.
    """
    base = {
        "success": True,
        "status": status_value,
        "action": status_value,  # DEPRECATED (task #73); remove after Glific flows audit
        "resolved_flow_state": pe.resolved_flow_state or "",
        "next_action_type": pe.next_action_type or "",
        "next_action_at": str(pe.next_action_at) if pe.next_action_at else "",
        "program_status": pe.program_status or "",
    }
    base.update(extras)
    frappe.local.response.update(base)
    return base


@frappe.whitelist(allow_guest=True)
@sp_safe_endpoint("update_flow_status")
def update_flow_status(student_id, status, flow_name, metadata=None,
                       **_glific_kwargs):
    """
    API A4: update_flow_status

    Called by EVERY Glific flow on completion. This is how Glific tells the
    backend that a flow finished, and what the outcome was.

    `**_glific_kwargs` absorbs Glific-injected fields (organization_id, etc.)
    per task #89. Ignored at this layer.

    Args:
        student_id: Student document name or glific_id
        flow_name: Name of the Glific flow (e.g., 'SP_Content_Delivery')
        status: Flow outcome — 'completed', 'no_response', 'timeout', 'error'
        metadata: Optional dict with extra data from the flow

    Returns:
        dict with result and any next action scheduled
    """
    # CR-024 Layer 3: reject Glific placeholder strings on identifier params.
    # student_id and flow_name are identifier params. status is a controlled
    # enum value ('completed'/'no_response'/'timeout'/'error') — not checked.
    placeholder_hit = check_glific_placeholders(
        [("student_id", student_id), ("flow_name", flow_name)],
        api_name="update_flow_status",
        student_id=student_id,
    )
    if placeholder_hit:
        frappe.local.response.update(placeholder_hit)
        return

    student_id = _resolve_student(student_id)
    if not student_id:
        frappe.local.response.update({
            "success": False, "status": "not_found",
            "error_detail": "Student not found",
        })
        return

    pe = get_active_pe(student_id)
    if not pe:
        frappe.local.response.update({
            "success": False, "status": "no_active_enrollment",
            "error_detail": "No active ProgramEnrollment",
        })
        return

    # Track the flow delivery.
    # M-4 (2026-06-15): persist via set_value (bypasses save+hooks per L-039,
    # which is what we want here — these are diagnostic fields with no
    # downstream contact-field sync). Pre-fix, the in-memory assignments
    # were clobbered by downstream pe.reload() calls in every handler, so
    # the fields were never persisted despite operators reading them for
    # debugging.
    triggered_at = now_datetime()
    frappe.db.set_value(
        "ProgramEnrollment",
        pe.name,
        {
            "last_flow_triggered": flow_name,
            "last_flow_triggered_at": triggered_at,
        },
        update_modified=False,
    )
    pe.last_flow_triggered = flow_name
    pe.last_flow_triggered_at = triggered_at

    # Log the callback
    log_event(pe, "flow_completed", old_value=flow_name, new_value=status,
              trigger_source="flow_callback",
              details={"flow_name": flow_name, "status": status})

    # Route to the appropriate handler based on flow_name + status.
    # Handlers call _response(...) which writes to frappe.local.response and
    # returns the dict for convenience — we don't need the return value here.
    handler = _get_handler(flow_name, status)
    if handler:
        handler(pe, flow_name, status, metadata or {})
        # Removed mid-handler commit per L-017 — Frappe commits at request-end.
        return

    # Default: just acknowledge the callback.
    # Note: we pass the inbound flow `status` as `flow_status` because task #73
    # renamed the helper's outcome field to `status` (Glific Rule 6). Passing
    # `status=status` here would clobber the outcome key via **extras.
    #
    # Task #16 (2026-05-28, Esc R2): replaced `pe.save(ignore_permissions=True)`
    # with `pe.reload()`. The fallback acknowledgement doesn't mutate the PE —
    # the in-memory doc was loaded earlier in this request and may be stale
    # vs concurrent atomic SQL bumps from award handlers. `pe.save` would
    # write all columns from the stale doc, clobbering those bumps (L-011
    # violation). `pe.reload()` pulls fresh DB state so _response renders
    # the latest values.
    pe.reload()
    _response(pe, "acknowledged", flow_name=flow_name, flow_status=status)


def _get_handler(flow_name, status):
    """Route to specific handler based on flow name and status.

    CR-002 v2: `SP_Week_Summary` entry removed (vestigial flow path).
    CR-003: `SP_Grace_Reminder` and `SP_Paused_Reengagement` entries removed
    (flows deleted in coordination with Himani — grace reminders are gone and
    re-engagement is inbound-only). `_handle_info_flow` remains in use by
    `SP_Program_Complete`.

    CR-003 follow-up (2026-05-13): `SP_Grace_Entry` entry removed — the
    "you've entered grace" Glific flow is retired end-to-end. Escalation
    steps inside the week deliver the same reminder cadence.
    """
    handlers = {
        "SP_Content_Delivery": _handle_content_delivery,
        "SP_Escalation": _handle_escalation,
        "SP_Feedback_Delivery": _handle_feedback_delivery,
        "SP_Submission": _handle_submission_flow,
        "SP_Paused_Binge": _handle_binge_info,
        "SP_Program_Complete": _handle_info_flow,
    }
    return handlers.get(flow_name)


# ════════════════════════════════════════════════════════════
# FLOW-SPECIFIC HANDLERS
# ════════════════════════════════════════════════════════════

def _handle_content_delivery(pe, flow_name, status, metadata):
    """
    Handle SP_Content_Delivery completion.

    - no_response/timeout: student didn't engage → schedule first escalation
    - completed: student tapped button and flow ended normally
    """
    if status in ("no_response", "timeout"):
        # Student didn't respond within wait window → start escalation
        # Get first escalation step timing from ArchetypeConfig
        step = _get_first_escalation_step(pe)
        t1_content_no_response(pe, step, "flow_callback")
        # The transition wrote to DB; re-read so the response reflects
        # post-transition state (next_action_at, next_action_type, etc.).
        pe.reload()
        return _response(pe, "escalation_scheduled")

    # completed — content was delivered, student tapped. Flow ended normally.
    # If submission happened within the flow, save_submission already handled
    # state. Just confirm delivery.
    # Task #16 (2026-05-28, Esc R2): pe.save → pe.reload — see fallback in
    # update_flow_status for rationale.
    pe.reload()
    return _response(pe, "delivery_confirmed")


def _handle_escalation(pe, flow_name, status, metadata):
    """
    Handle SP_Escalation completion.

    - completed/timeout: escalation delivered, wait expired. No submission.
    - If submission happened during wait, save_submission already handled state.
    """
    # Escalation was delivered. If student hasn't submitted, the scheduler
    # will check next_action_at for the next escalation step.
    # Task #16 (2026-05-28, Esc R2): pe.save → pe.reload — the dispatcher's
    # T2/T4/T8/T10 escalation handlers already incremented
    # current_escalation_step via SQL UPDATE; reload pulls that fresh
    # value so the log_event + response carry the right number.
    pe.reload()

    log_event(pe, "escalation_sent", trigger_source="flow_callback",
              details={"step": pe.current_escalation_step})

    return _response(
        pe,
        "escalation_confirmed",
        last_escalation_step=pe.current_escalation_step,
    )


def _handle_feedback_delivery(pe, flow_name, status, metadata):
    """
    Handle SP_Feedback_Delivery completion.

    CRITICAL: This callback triggers week_completed → week_advancement.
    Without this callback, the student gets stuck in feedback_ready.
    """
    if pe.resolved_flow_state != STATE_FEEDBACK_READY:
        # State already moved (race condition or retry) — just acknowledge.
        # Task #16 (2026-05-28, Esc R2): pe.save → pe.reload — same L-011
        # safety reasoning as the other handlers; the race itself proves
        # pe is stale, so reload makes the response reflect what actually
        # happened.
        pe.reload()
        return _response(pe, "already_advanced")

    # T13: feedback_ready → week_completed → schedule week_advancement
    t13_feedback_delivered(pe, "flow_callback")
    # Re-read so the response reflects post-T13 state (current_week may have
    # incremented, resolved_flow_state moved to week_completed, etc.).
    pe.reload()

    log_event(pe, "feedback_delivered", trigger_source="flow_callback")

    return _response(pe, "week_completed", current_week=pe.current_week)


def _handle_submission_flow(pe, flow_name, status, metadata):
    """
    Handle SP_Submission sub-flow completion.
    The actual submission was already recorded via save_submission API.
    This just confirms the flow ended.
    """
    # Task #16 (2026-05-28, Esc R2): pe.save → pe.reload — save_submission
    # ran earlier in the request OR via a separate save; either way our
    # in-memory doc is likely stale relative to the submission state.
    pe.reload()
    return _response(pe, "submission_flow_completed")


# CR-003 follow-up (2026-05-13): `_handle_grace_flow` deleted along with the
# SP_Grace_Entry Glific flow. The grace clock is now armed by the
# activity-points handler on the week's first VideoClass completion and
# cleared by primary submissions; no separate Glific "you've entered grace"
# notification is sent. The escalation chain inside the active week IS the
# reminder cadence.

# CR-003: _handle_reengagement deleted along with the SP_Paused_Reengagement
# flow. Re-engagement is now inbound-only via SP_Incoming_Router.


def _handle_binge_info(pe, flow_name, status, metadata):
    """Handle SP_Paused_Binge completion. Informational only."""
    # Task #16 (2026-05-28, Esc R2): pe.save → pe.reload — see other handlers.
    pe.reload()
    return _response(pe, "binge_info_delivered")


def _handle_info_flow(pe, flow_name, status, metadata):
    """Handle informational flows (Week Summary, Program Complete)."""
    # Task #16 (2026-05-28, Esc R2): pe.save → pe.reload — see other handlers.
    pe.reload()
    return _response(pe, "info_delivered")


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════

def _resolve_student(identifier):
    """Delegate to shared utility."""
    from tap_lms.summer_program.utils import resolve_student
    return resolve_student(identifier)


def _get_first_escalation_step(pe):
    """Get the first escalation step config for this student's archetype."""
    from tap_lms.summer_program.student_progression_sp import _get_escalation_steps
    student = frappe.get_doc("Student", pe.student)
    batch = frappe.get_doc("Batch", pe.batch)
    steps = _get_escalation_steps(student, batch)
    return steps[0] if steps else None
