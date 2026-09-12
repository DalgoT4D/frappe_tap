"""
ElevenLabs Conversational AI — outbound call integration for Didi.

API docs: https://elevenlabs.io/docs/conversational-ai/api-reference/conversations/create-outbound-call

Authentication: xi-api-key header (not Bearer token)
Call initiation: POST /v1/convai/conversations/outbound-call
Outcomes: ElevenLabs webhook (configure on ElevenLabs dashboard)

Key difference from Vocallabs:
- No separate contact group step — variables sent directly in the call request
- Single API call to initiate (vs Vocallabs' addMultipleContactsToGroup + initiateCall)
- Dynamic variables injected via conversation_initiation_client_data.dynamic_variables
"""

import requests

import frappe
from frappe.utils import now_datetime


_MODULE = "Didi ElevenLabs"


def _get_settings():
    return frappe.get_single("ElevenLabsSettings")


def _get_agent_id(settings, language):
    """Resolve ElevenLabs agent UUID from language. Falls back to first enabled agent."""
    lang = (language or "").strip().lower()
    for row in settings.agents or []:
        if row.enabled and (row.language or "").strip().lower() == lang:
            return row.agent_id
    # Fallback: first enabled agent
    for row in settings.agents or []:
        if row.enabled:
            return row.agent_id
    return None


def initiate_elevenlabs_call(pe_name, campaign_name, queue_row_name):
    """Place an outbound call via ElevenLabs Conversational AI.

    Entry point called by campaign_processor.py when campaign.provider == 'ElevenLabs'.

    Args:
        pe_name: ProgramEnrollment name
        campaign_name: VoiceCallCampaign name (for history tagging)
        queue_row_name: VoiceCallQueue row name (for status updates)

    Returns:
        True if call was initiated successfully, False otherwise.
    """
    settings = _get_settings()

    if not settings.enabled:
        frappe.log_error(
            title=f"{_MODULE} — disabled",
            message="ElevenLabsSettings.enabled is 0. Call skipped.",
        )
        return False

    pe = frappe.get_doc("ProgramEnrollment", pe_name)
    student = frappe.get_doc("Student", pe.student)

    # ── Blocklist check ──────────────────────────────────────────────────
    from tap_lms.tap_lms.doctype.voicecallblocklist.voicecallblocklist import is_blocked
    if is_blocked(student.phone):
        _skip_queue_row(queue_row_name, "Phone number is in no-call blocklist.")
        return False

    # ── Daily dedup check ────────────────────────────────────────────────
    from frappe.utils import today as frappe_today, get_datetime
    today_start = get_datetime(f"{frappe_today()} 00:00:00")
    already_called = frappe.db.sql("""
        SELECT COUNT(*) FROM "tabVoiceCallHistory"
        WHERE parent = %(pe_name)s
          AND parenttype = 'ProgramEnrollment'
          AND call_placed_at >= %(today_start)s
          AND outcome != 'blocked'
    """, {"pe_name": pe.name, "today_start": today_start}, as_list=True)[0][0]

    if already_called:
        _skip_queue_row(queue_row_name, "Already called today by another campaign.")
        return False

    # ── Build context ────────────────────────────────────────────────────
    from tap_lms.summer_program.voice_context import build_student_context
    ctx = build_student_context(pe, student)

    # ── Resolve agent ────────────────────────────────────────────────────
    agent_id = _get_agent_id(settings, pe.language)
    if not agent_id:
        frappe.log_error(
            title=f"{_MODULE} — no agent",
            message=f"No ElevenLabs agent configured for language '{pe.language}'. PE: {pe.name}",
        )
        _skip_queue_row(queue_row_name, f"No ElevenLabs agent for language: {pe.language}")
        return False

    # ── Format phone for ElevenLabs (needs +91 prefix) ──────────────────
    phone = (student.phone or "").strip()
    if not phone.startswith("+"):
        phone = f"+91{phone}"

    # ── Build dynamic variables dict ─────────────────────────────────────
    dynamic_vars = {
        "student_name":           ctx.get("student_name", student.name1 or ""),
        "welcome_greeting":       ctx.get("welcome_greeting", "TAP Buddy"),
        "situation":              ctx.get("situation", ""),
        "status":                 ctx.get("status", ""),
        "submission_count":       str(ctx.get("submission_count", "0")),
        "streak":                 str(ctx.get("streak", "0")),
        "submission_ask":         ctx.get("submission_ask", ""),
        "course":                 ctx.get("course", ""),
        "grace_deadline":         ctx.get("grace_deadline", ""),
        "last_problem_reported":  ctx.get("last_problem_reported", ""),
        "last_message":           ctx.get("last_message", ""),
        "grade_group":            ctx.get("grade_group", "student_direct"),
        "call_attempt":           str(ctx.get("call_attempt", "1")),
    }

    # ── Fire the call ────────────────────────────────────────────────────
    url = f"{settings.base_url.rstrip('/')}/v1/convai/conversations/outbound-call"
    headers = {
        "xi-api-key": settings.get_password("api_key"),
        "Content-Type": "application/json",
    }
    payload = {
        "agent_id": agent_id,
        "agent_phone_number_id": settings.phone_number_id,
        "to_number": phone,
        "conversation_initiation_client_data": {
            "dynamic_variables": dynamic_vars,
        },
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        frappe.log_error(
            title=f"{_MODULE} — API error",
            message=f"PE {pe.name}: {exc}",
        )
        _fail_queue_row(queue_row_name, str(exc))
        return False

    conversation_id = result.get("conversation_id") or result.get("id") or ""

    # ── Write call history ───────────────────────────────────────────────
    now = now_datetime()
    try:
        frappe.db.sql("""
            INSERT INTO "tabVoiceCallHistory"
                (name, parent, parenttype, parentfield, idx,
                 call_placed_at, situation, outcome, campaign,
                 agent_id, rendered_prompt, reengaged_within_48h,
                 vocallabs_call_id, provider,
                 creation, modified, modified_by, owner, docstatus)
            VALUES
                (%(name)s, %(parent)s, 'ProgramEnrollment', 'voice_call_history', 1,
                 %(now)s, %(situation)s, 'placed', %(campaign)s,
                 %(agent_id)s, %(prompt)s, 0,
                 %(call_id)s, 'ElevenLabs',
                 %(now)s, %(now)s, 'Administrator', 'Administrator', 0)
        """, {
            "name":      frappe.generate_hash(length=10),
            "parent":    pe.name,
            "now":       now,
            "situation": (ctx.get("situation") or "")[:140],
            "campaign":  (campaign_name or "")[:140],
            "agent_id":  (agent_id or "")[:140],
            "prompt":    (dynamic_vars.get("status") or "")[:500],
            "call_id":   conversation_id[:140] if conversation_id else "",
        })
    except Exception as exc:
        frappe.log_error(
            title=f"{_MODULE} — history write failed",
            message=f"PE {pe.name}: {exc}",
        )

    # ── Update queue row ─────────────────────────────────────────────────
    if queue_row_name:
        frappe.db.set_value(
            "VoiceCallQueue", queue_row_name,
            {"status": "Calling", "call_placed_at": now},
            update_modified=False,
        )

    # ── Update PE call tracking ──────────────────────────────────────────
    frappe.db.set_value(
        "ProgramEnrollment", pe.name,
        {
            "last_call_at":      now,
            "total_call_count":  (pe.total_call_count or 0) + 1,
            "weekly_call_count": (pe.weekly_call_count or 0) + 1,
        },
        update_modified=False,
    )

    frappe.logger().info(
        f"{_MODULE}: call placed for PE {pe.name} "
        f"(conversation_id={conversation_id}, agent={agent_id})"
    )
    return True


def _skip_queue_row(queue_row_name, reason):
    if queue_row_name:
        frappe.db.set_value(
            "VoiceCallQueue", queue_row_name,
            {"status": "Skipped", "error_message": reason[:500]},
            update_modified=False,
        )


def _fail_queue_row(queue_row_name, reason):
    if queue_row_name:
        frappe.db.set_value(
            "VoiceCallQueue", queue_row_name,
            {"status": "Failed", "error_message": reason[:500]},
            update_modified=False,
        )
