"""Vocallabs inbound webhook receiver (CR-030, governed by ADR-007).

Receives Vocallabs outgoing webhook events (`call.started`, `call.ended`,
`call.failed`, `data.collected`), maps each to our student/enrollment, and records
a `parent_call_outcome` ProgramEventLog event — the true connect/outcome signal
that augments the `escalation_sent` hand-off (fixes the reach funnel) and finally
captures the REAL Vocallabs call id (the one our initiate-time queue id never was).

ADR-007 contract:
  - Auth: URL `?token=` shared secret, constant-time compare. 401 on bad/missing
    token with NO body processing/logging. The secret lives on
    `VoiceAgentSettings.webhook_secret` (Password), falling back to site_config
    `vocallabs_webhook_secret`.
  - Response: HTTP 200 for every authenticated request — including unparseable or
    unmappable events (dead-lettered to Error Log) — so Vocallabs never retries a
    benign event (e.g. a call placed by another program on the shared account).
  - Idempotent on (real_call_id, event); duplicate deliveries are skipped.
  - Multi-tenant safe: events that don't map to one of our students are dead-lettered.

NOTE ON PAYLOAD SCHEMA: the exact field names are not yet confirmed from a live
Vocallabs payload (CR-030 OQ#1). Field extraction is therefore DEFENSIVE — it digs
across likely keys and containers. Set site_config `vocallabs_webhook_debug=1` to
log every raw inbound payload to Error Log (title below) during the test-call phase;
turn it off in steady state. Anything that fails to parse/map is dead-lettered with
the raw payload, so a test call is self-diagnosing.
"""

import hmac
import json

import frappe
from frappe.utils import now_datetime

WEBHOOK_RAW_LOG_TITLE = "Vocallabs Webhook Raw"          # debug-gated raw capture
WEBHOOK_DEADLETTER_LOG_TITLE = "Vocallabs Webhook Dead-letter"  # unmappable/parse-fail
_MAX_LOG_LEN = 12000


# ════════════════════════════════════════════════════════════
# Auth (ADR-007)
# ════════════════════════════════════════════════════════════

def _webhook_secret():
    """Resolve the shared secret: VoiceAgentSettings.webhook_secret, else site_config.

    Field first (ADR-007 pattern), site_config fallback for continuity with the
    Phase 0 capture setup. Returns "" if neither is set (auth then fails closed).
    """
    try:
        settings = frappe.get_single("VoiceAgentSettings")
        val = settings.get_password("webhook_secret", raise_exception=False)
        if val:
            return (val or "").strip()
    except Exception:
        pass
    return (frappe.conf.get("vocallabs_webhook_secret") or "").strip()


def _token_ok():
    """Constant-time check of the URL `?token=` against the configured secret.

    Fails closed on any missing value — a misconfiguration returns 401, never open.
    """
    expected = _webhook_secret()
    if not expected:
        return False
    got = ""
    if frappe.request is not None:
        got = (frappe.request.args.get("token") or "").strip()
    if not got:
        return False
    return hmac.compare_digest(got, expected)


# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════

def _read_raw():
    try:
        return frappe.request.get_data(as_text=True) if frappe.request is not None else ""
    except Exception as e:  # noqa: BLE001
        return "<unreadable body: %s>" % e


def _safe_log(title, message):
    """Log best-effort; never raise (a 500 would trigger a Vocallabs retry)."""
    try:
        if message and len(message) > _MAX_LOG_LEN:
            message = message[:_MAX_LOG_LEN] + "\n...[truncated]"
        frappe.log_error(message=message, title=title)
    except Exception:
        pass


def _capture_blob(body):
    """Debug snapshot of the inbound request for the test-call phase: method,
    query (token stripped), headers (Authorization/Cookie redacted), raw body.
    Headers are included so we can see whether Vocallabs signs requests (OQ#2)."""
    req = frappe.request
    method = getattr(req, "method", "") if req is not None else ""
    headers, query = {}, {}
    if req is not None:
        try:
            for k, v in req.headers.items():
                headers[k] = "<redacted>" if k.lower() in ("authorization", "cookie") else v
        except Exception:
            pass
        try:
            for k, v in req.args.items():
                if k.lower() != "token":  # never log the shared secret
                    query[k] = v
        except Exception:
            pass
    return json.dumps({"method": method, "query": query, "headers": headers, "body": body},
                      default=str, indent=2)


def _dig(payload, keys, containers=("data", "call", "payload", "object", "result", "event_data")):
    """Return the first non-empty value for any of `keys`, searching the top level
    then one level into common container objects. Defensive against unknown nesting."""
    if not isinstance(payload, dict):
        return None
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return v
    for c in containers:
        sub = payload.get(c)
        if isinstance(sub, dict):
            for k in keys:
                v = sub.get(k)
                if v not in (None, ""):
                    return v
    return None


def _last10(phone):
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _enrollment_by_queue_id(queue_id):
    """The webhook's `queue_id` == the id we stored at initiate time (inside the
    `escalation_sent` log's `vocallabs_response`). Resolve it back to the
    enrollment that placed the call — the PRIMARY mapping for the real payload,
    which carries no phone. Escapes LIKE wildcards; bounded by quotes."""
    if not queue_id:
        return None
    # substring match on the UUID (globally unique). escalation_sent stores it
    # doubly-encoded (\"...\") inside vocallabs_response, so don't quote-bound.
    esc = queue_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = frappe.db.sql(
        """
        SELECT enrollment FROM "tabProgramEventLog"
        WHERE event_type = 'escalation_sent' AND details::text LIKE %s ESCAPE '\\'
        ORDER BY creation DESC LIMIT 1
        """,
        ("%" + esc + "%",),
    )
    return rows[0][0] if rows else None


def _pe_by_name(enrollment):
    row = frappe.db.get_value(
        "ProgramEnrollment", enrollment,
        ["name", "student", "batch", "program_type", "current_week"], as_dict=True,
    )
    return frappe._dict(row) if row else None


def _active_pe_for_student(student):
    pes = frappe.get_all(
        "ProgramEnrollment",
        filters={"student": student, "program_status": "active"},
        fields=["name", "student", "batch", "program_type", "current_week"],
        order_by="creation desc", limit=1,
    )
    return frappe._dict(pes[0]) if pes else None


def _find_pe(queue_id, prospect_id, phone_to):
    """Map a webhook to one of our enrollments. Priority:
      1. queue_id -> the `escalation_sent` log that placed the call (the real
         payload carries `queue_id`, not phone);
      2. cached prospect_id -> Student.vocallabs_prospect_id;
      3. parent phone (last-10) -> Student.phone -> most-recent active PE
         (fallback for a future payload shape that carries a phone)."""
    enr = _enrollment_by_queue_id(queue_id)
    if enr:
        pe = _pe_by_name(enr)
        if pe:
            return pe
    student = None
    if prospect_id:
        student = frappe.db.get_value("Student", {"vocallabs_prospect_id": prospect_id}, "name")
    if not student and phone_to:
        last10 = _last10(phone_to)
        if last10:
            srow = frappe.get_all(
                "Student", filters={"phone": ["like", "%" + last10]}, fields=["name"],
                order_by="creation asc", limit=1,  # stable tiebreak on shared-phone siblings
            )
            if srow:
                student = srow[0]["name"]
    if not student:
        return None
    return _active_pe_for_student(student)


def _already_logged(call_id):
    """Idempotency: dedupe on the real telephony call_id (one outcome per call).

    Matches the bounded JSON key/value (`"real_call_id": "<id>"`) rather than a
    bare substring, and escapes LIKE wildcards (`%` `_` `\\`) in call_id — so a
    wildcard char in the id, or a short id that is a substring of a longer one,
    can't false-positive and silently drop a real outcome.
    """
    if not call_id:
        return False
    esc = call_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = frappe.db.sql(
        """
        SELECT name FROM "tabProgramEventLog"
        WHERE event_type = 'parent_call_outcome'
          AND details::text LIKE %s ESCAPE '\\'
        LIMIT 1
        """,
        ('%"real_call_id": "' + esc + '"%',),
    )
    return bool(rows)


# ════════════════════════════════════════════════════════════
# Event processing
# ════════════════════════════════════════════════════════════

def _process_event(payload, raw_text):
    """Record a call outcome. The real Vocallabs payload is
    `{call_id, queue_id, status}` (no `event` field, no phone): `call_id` is the
    REAL telephony id, `queue_id` is the id we stored at initiate time, `status`
    is the call_status. We map via queue_id and record a `parent_call_outcome`."""
    real_call_id = _dig(payload, ("call_id", "callId", "id"))
    queue_id = _dig(payload, ("queue_id", "queueId", "queue"))
    status = _dig(payload, ("status", "call_status"))
    if isinstance(status, str):
        status = status.strip().lower()
    action_outcome = _dig(payload, ("action_outcome", "outcome"))
    summary = _dig(payload, ("call_summary", "summary"))
    duration = _dig(payload, ("duration",))
    phone_to = _dig(payload, ("phone_to", "phoneTo", "to", "phone"))
    prospect_id = _dig(payload, ("prospect_id", "prospectId"))

    # Outcome events carry a status (or a post-call action_outcome). Skip pings.
    if not status and not action_outcome:
        return

    pe = _find_pe(queue_id, prospect_id, phone_to)
    if not pe:
        _safe_log(
            WEBHOOK_DEADLETTER_LOG_TITLE,
            "unmappable real_call_id=%s queue_id=%s status=%s\n%s"
            % (real_call_id, queue_id, status, raw_text),
        )
        return

    if _already_logged(real_call_id):
        return  # duplicate delivery for this call — skip

    if isinstance(summary, str) and len(summary) > 1000:
        summary = summary[:1000] + "…"

    details = {
        "real_call_id": real_call_id,
        "queue_id": queue_id,
        "call_status": status or None,
        "action_outcome": action_outcome,
        "call_summary": summary,
        "duration": duration,
        "phone_to": phone_to,
        "prospect_id": prospect_id,
    }
    frappe.get_doc({
        "doctype": "ProgramEventLog",
        "enrollment": pe.name,
        "student": pe.student,
        "batch": pe.batch,
        "program_type": pe.program_type or "Summer",
        "week": pe.current_week,
        "event_type": "parent_call_outcome",
        "trigger_source": "vocallabs_webhook",
        "created_at": now_datetime(),
        "details": json.dumps(details, default=str),
    }).insert(ignore_permissions=True)  # guest endpoint; server authorizes via the token check above


# ════════════════════════════════════════════════════════════
# Endpoint
# ════════════════════════════════════════════════════════════

@frappe.whitelist(allow_guest=True)
def receive(*args, **kwargs):
    """Inbound Vocallabs webhook. `*args, **kwargs` absorb Frappe's JSON-body binding;
    the raw body is read directly from the request."""
    # ── Auth (ADR-007): 401 + no processing/logging on failure ──
    if not _token_ok():
        frappe.local.response["http_status_code"] = 401
        return {"ok": False, "error": "unauthorized"}

    raw = _read_raw()

    # Debug-gated raw capture — on during the test-call phase, off in steady state.
    if frappe.conf.get("vocallabs_webhook_debug"):
        _safe_log(WEBHOOK_RAW_LOG_TITLE, _capture_blob(raw))

    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        _safe_log(WEBHOOK_DEADLETTER_LOG_TITLE, "unparseable JSON body:\n" + raw)
        return {"ok": True}

    if not isinstance(payload, dict):
        _safe_log(WEBHOOK_DEADLETTER_LOG_TITLE, "non-object payload:\n" + raw)
        return {"ok": True}

    try:
        _process_event(payload, raw)
    except Exception as e:  # noqa: BLE001 — L-030: never let an error poison the txn / 500
        frappe.db.rollback()
        _safe_log(WEBHOOK_DEADLETTER_LOG_TITLE, "process error: %s\n%s" % (e, raw))

    # ADR-007: 200 acknowledges receipt regardless of mapping outcome.
    return {"ok": True}
