"""
Vocallabs parent-call integration — CR-003.

tap_lms/summer_program/vocallabs.py

Per-call flow (Postman reference: createAuthToken → createContactGroup [unused
in steady state, contact group is configured once] → addMultipleContactsToGroup →
initiateVocallabsCall):

1. Get cached auth token, or fetch + cache for VoiceAgentSettings.auth_token_cache_ttl
2. Add parent contact to default group with the rendered status template
3. Initiate call with agent_id + returned prospect_id

Retry: 5 attempts via the P-007 retry/DLQ pattern shared with Glific sync
(state_machine._sync_contact_fields_job) and the feedback pipeline
(save_submission.enqueue_submission). DLQ to Frappe Error Log titled
"SP Vocallabs DLQ — manual replay required" with a structured payload
{student_id, pe_name, week, escalation_order, parent_phone, error}.

NEVER bubble exceptions; always log to Error Log and return False on failure
so the dispatcher continues toward drop (CR-003 §Edge case E4: "Vocallabs DLQ
during grace tail: just log and continue toward drop").

Phone resolution:
    Per CR-003 §"Phone resolution" the parent's phone is `Student.phone`
    (the student uses the parent's device). No `Student.parent_phone`
    field is added in this CR; a future CR can split this if Student
    ever gets its own phone.
"""
import json

import frappe
from frappe.utils import now_datetime

from tap_lms.summer_program.constants import (
    VOCALLABS_MAX_RETRIES,
    VOCALLABS_RETRY_LOG_TITLE,
    VOCALLABS_DLQ_LOG_TITLE,
    VOCALLABS_DUPLICATE_PROSPECT_LOG_TITLE,
    VOCALLABS_DUPLICATE_PROSPECT_CONSTRAINT,
    VOCALLABS_LOOKUP_PAGE_SIZE,
    VOCALLABS_LOOKUP_MAX_PAGES,
    VOCALLABS_LOOKUP_LOG_TITLE,
    VOCALLABS_HTTP_TIMEOUT_SECONDS,
    VOCALLABS_TOKEN_CACHE_KEY,
    VOCALLABS_DEFAULT_TOKEN_TTL,
)


class PermanentVocallabsError(Exception):
    """Raised for Vocallabs failures that MUST NOT be retried.

    Distinguishes "the parent's phone is already in the prospect group, so
    addMultipleContactsToGroup can never insert again" (permanent — retrying
    just wastes worker time and pollutes Error Log) from real transient
    failures like 502s, network blips, or auth issues (transient — retry).

    Today the only producer is the duplicate-prospect detection in
    `_call_vocallabs`. Until task #81 lands a Vocallabs lookup endpoint, the
    only safe action is: log it under a dedicated title, return False, and
    let the dispatcher continue toward drop on the existing schedule
    (CR-003 §E4 — a Vocallabs failure does NOT extend the grace window).
    """
    pass


# ════════════════════════════════════════════════════════════
# Public entry point
# ════════════════════════════════════════════════════════════


def initiate_parent_call(pe_name, escalation_step, retry_count=0):
    """Initiate a Vocallabs parent call for a PE's current escalation step.

    Frappe-enqueueable from `handle_escalation` when
    `escalation_step['escalation_type'] == 'parent_call'`.

    Args:
        pe_name: ProgramEnrollment document name.
        escalation_step: dict from _get_escalation_steps_for_pe; carries
            `escalation_order`, `escalation_type`, `hours_after_previous`,
            `points_awarded`.
        retry_count: internal — incremented on retry.

    Returns:
        True on successful Vocallabs hand-off, False on any failure
        (including all retries exhausted). False is also returned for
        config-level skips (Vocallabs disabled, no ParentCallConfig
        resolved). The dispatcher does NOT block on this — the grace
        clock keeps ticking and the PE proceeds toward drop on the
        normal schedule.
    """
    try:
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
    except frappe.DoesNotExistError:
        frappe.log_error(
            f"Vocallabs: PE {pe_name} not found; skipping parent call.",
            VOCALLABS_DLQ_LOG_TITLE,
        )
        return False

    # ── Settings check ──────────────────────────────────────
    settings = _get_voice_agent_settings()
    if not settings:
        frappe.log_error(
            f"Vocallabs: VoiceAgentSettings singleton missing for PE {pe_name}; skipping.",
            "SP Vocallabs Config",
        )
        return False

    if not getattr(settings, "enabled", 0):
        # Feature flag off — log + skip; this is NOT an error and NOT retried.
        frappe.log_error(
            f"Vocallabs disabled (VoiceAgentSettings.enabled=0); "
            f"skipping parent call for PE {pe_name}.",
            "SP Vocallabs Skipped",
        )
        return False

    # ── Resolve config ──────────────────────────────────────
    config = _resolve_parent_call_config(pe, pe.current_week or 1, settings)
    if not config:
        # Per CR-003 §E3: skip + warn. Config issue, not a runtime failure.
        frappe.log_error(
            f"Vocallabs: no ParentCallConfig resolved for PE {pe_name} "
            f"(week={pe.current_week}); neither per-LU content nor "
            f"VoiceAgentSettings.default_parent_call_config is set. "
            f"Skipping parent call; the cohort is NOT blocked.",
            "SP Vocallabs Config",
        )
        return False

    # ── Resolve student + parent phone ──────────────────────
    student = frappe.get_doc("Student", pe.student)
    parent_phone = (getattr(student, "phone", "") or "").strip()
    if not parent_phone:
        frappe.log_error(
            f"Vocallabs: Student {pe.student} has no phone; "
            f"cannot place parent call for PE {pe_name}.",
            "SP Vocallabs Config",
        )
        return False

    # ── Render status ──────────────────────────────────────
    welcome_greeting = _resolve_welcome_greeting(pe)
    if welcome_greeting == "None":
        frappe.info("Skipping parent call for Dormant/Arm B student per config.")
        return True

    status_text = _render_status_template(
        config.status_template or "",
        pe, student, escalation_step,
    )

    # ── Run the 3-step Vocallabs sequence ──────────────────
    try:
        token = _get_auth_token(settings)
        if not token:
            raise RuntimeError("Vocallabs auth token fetch returned empty token")

        # Task #81: pass the student doc so _call_vocallabs can read
        # Student.vocallabs_prospect_id (cached prospect_id from the first
        # ever parent_call to this phone) and skip the add-to-group step
        # when it's populated.
        call_response = _call_vocallabs(
            settings=settings,
            token=token,
            pe=pe,
            student=student,
            parent_phone=parent_phone,
            student_name=_student_display(student),
            status_text=status_text,
        )
        if not call_response:
            raise RuntimeError("Vocallabs call sequence returned no response")
    except Exception as e:
        return _handle_failure(
            pe=pe, escalation_step=escalation_step,
            parent_phone=parent_phone, error=e, retry_count=retry_count,
        )

    # Success — log a successful parent-call attempt for the funnel.
    # Field names per programeventlog.json: `enrollment` (reqd), `student`
    # (reqd), `batch` (reqd), `program_type`, `week`, `event_type`,
    # `trigger_source`, `details`. Earlier draft used `program_enrollment`
    # which silently failed Frappe validation — see code review B2.
    try:
        frappe.get_doc({
            "doctype": "ProgramEventLog",
            "enrollment": pe.name,
            "student": pe.student,
            "batch": pe.batch,
            "program_type": pe.program_type or "Summer",
            "week": pe.current_week,
            "event_type": "escalation_sent",
            "trigger_source": "scheduler",
            "created_at": now_datetime(),
            "details": json.dumps({
                "channel": "parent_call",
                "escalation_order": escalation_step.get("escalation_order"),
                "vocallabs_response": _safe_summary(call_response),
            }),
        }).insert(ignore_permissions=True)
    except frappe.ValidationError as log_err:
        # Narrow exception per L-007. Logging failure shouldn't kill the
        # success path; surface to Error Log so ops can backfill the funnel.
        frappe.log_error(
            f"Vocallabs: succeeded but event_log insert failed for PE {pe_name}: {log_err}",
            "SP Vocallabs Log",
        )

    return True


# ════════════════════════════════════════════════════════════
# Settings + auth token cache
# ════════════════════════════════════════════════════════════


def _get_voice_agent_settings():
    """Read the VoiceAgentSettings singleton once per invocation.

    Returns the doc or None if the singleton is missing.
    """
    try:
        return frappe.get_single("VoiceAgentSettings")
    except Exception as e:
        frappe.log_error(
            f"Vocallabs: failed to load VoiceAgentSettings: {e}",
            "SP Vocallabs Config",
        )
        return None


def _get_auth_token(settings):
    """Return a cached or freshly-minted Vocallabs auth token.

    Cache: `frappe.cache().get_value(VOCALLABS_TOKEN_CACHE_KEY)`, TTL =
    `settings.auth_token_cache_ttl` (default `VOCALLABS_DEFAULT_TOKEN_TTL`
    = 3600s if unset). The cache is keyed without per-tenant scoping
    because we have one Vocallabs account per Frappe site.

    Edge case E5 (cache stampede): with concurrent workers all seeing a
    missing token, multiple createAuthToken calls may fire. Vocallabs
    returns the same token regardless; the last-writer-wins for the
    cache key is fine. A single-flight lock is filed as a follow-up.
    """
    cached = frappe.cache().get_value(VOCALLABS_TOKEN_CACHE_KEY)
    if cached:
        return cached

    # Cache miss — fetch fresh token.
    service_url = (settings.service_url or "").rstrip("/")
    if not service_url:
        raise RuntimeError("Vocallabs: VoiceAgentSettings.service_url is unset")
    if not settings.client_id:
        raise RuntimeError("Vocallabs: VoiceAgentSettings.client_id is unset")

    # client_secret is a Password field; read via get_password so the
    # decrypted value is returned (not the encrypted blob).
    try:
        client_secret = settings.get_password("client_secret", raise_exception=False)
    except Exception:
        client_secret = None
    if not client_secret:
        raise RuntimeError("Vocallabs: VoiceAgentSettings.client_secret is unset")

    # Vocallabs auth contract (verified via Postman 2026-05-21):
    # POST {service_url}/b2b/createAuthToken/   <- note trailing slash
    # Body uses camelCase clientId/clientSecret (NOT snake_case).
    response = _http_post(
        url=f"{service_url}/b2b/createAuthToken/",
        payload={
            "clientId": settings.client_id,
            "clientSecret": client_secret,
        },
        headers={"Content-Type": "application/json"},
    )

    token = _extract_auth_token(response)
    if not token:
        raise RuntimeError(
            f"Vocallabs: createAuthToken returned no authToken; response={_safe_summary(response)}"
        )

    ttl = int(getattr(settings, "auth_token_cache_ttl", None) or VOCALLABS_DEFAULT_TOKEN_TTL)
    frappe.cache().set_value(VOCALLABS_TOKEN_CACHE_KEY, token, expires_in_sec=ttl)
    return token


def _extract_auth_token(response):
    """Vocallabs' createAuthToken response has shape `{authToken: "..."}`;
    defensive-extract so we don't crash on response-shape drift.

    Some Vocallabs endpoints wrap the response in `[{...}]` (observed for
    createContactGroup); unwrap defensively in case auth eventually adopts
    the same pattern.
    """
    if isinstance(response, list):
        response = response[0] if response else {}
    if not isinstance(response, dict):
        return None
    return (
        response.get("authToken")
        or response.get("auth_token")
        or response.get("token")
    )


# ════════════════════════════════════════════════════════════
# Config resolution
# ════════════════════════════════════════════════════════════


def _resolve_parent_call_config(pe, current_week, settings):
    """Resolve the ParentCallConfig for the current week.

    Resolution chain per CR-003 §E3:
      1. Look up the week's LearningUnit; scan its UnitContentItem rows for
         one with `content_type == "ParentCallConfig"`. If found, return
         that ParentCallConfig doc.
      2. Fall back to `VoiceAgentSettings.default_parent_call_config`.
      3. Return None — caller logs a warning and skips the step.

    `_get_learning_unit` is the canonical helper in student_progression_sp.
    """
    from tap_lms.summer_program.student_progression_sp import _get_learning_unit

    tier = getattr(pe, "current_tier", None) or "Basic"
    if pe.course_level:
        try:
            learning_unit = _get_learning_unit(pe.course_level, current_week, tier)
        except Exception:
            learning_unit = None
        if learning_unit:
            item = frappe.db.get_value(
                "UnitContentItem",
                {
                    "parent": learning_unit,
                    "parenttype": "LearningUnit",
                    "content_type": "ParentCallConfig",
                },
                "content",
            )
            if item:
                try:
                    config = frappe.get_doc("ParentCallConfig", item)
                    # CR-003 §M4: `is_active` is the soft-disable flag. A
                    # disabled config means "don't fire this step"; fall
                    # through to the default. If the default is also
                    # inactive, the caller skips the step.
                    if getattr(config, "is_active", 1):
                        return config
                except frappe.DoesNotExistError:
                    # LU referenced a now-deleted config — fall through.
                    pass

    # Fallback to the singleton's default.
    default_name = getattr(settings, "default_parent_call_config", None)
    if default_name:
        try:
            default_config = frappe.get_doc("ParentCallConfig", default_name)
            if getattr(default_config, "is_active", 1):
                return default_config
        except frappe.DoesNotExistError:
            return None

    return None


# ════════════════════════════════════════════════════════════
# Template rendering
# ════════════════════════════════════════════════════════════


_TEMPLATE_VARS = (
    "student_name",
    "week",
    "archetype",
    "course_level",
    "path",
    "escalation_order",
    "escalation_type",
    "language",
)


def _render_status_template(template, pe, student, step):
    """Render the status_template with the documented variables.

    Supported variables (per CR-003 §"Parent-call integration"):
        {student_name}, {week}, {archetype}, {course_level}, {path},
        {escalation_order}, {escalation_type}, {language}

    Uses `str.format(**ctx)` so a missing variable in the template is fine
    (only the variables referenced are required). If the template
    references an UNDOCUMENTED variable, KeyError fires and we log to
    Error Log then return the raw template — the call still places (the
    operator can see the literal `{foo}` and fix the template).

    Note: `language` prefers the active ProgramEnrollment language and falls
    back to Student.language when ProgramEnrollment.language is empty.
    Useful for templates that branch wording, but the SPOKEN language of
    the call is determined by the Vocallabs Agent itself (the agent's
    `language` + `voice_id` config). To support multiple spoken languages
    you need one Vocallabs agent per language and a per-language agent_id
    lookup at call time — see task #48.
    """
    if not template:
        return ""

    pe_language = (pe.language or getattr(student, "language", "") or "").strip()
    ctx = {
        "student_name": _student_display(student),
        "week": str(pe.current_week or 0),
        "archetype": pe.archetype or "",
        "course_level": pe.course_level or "",
        "path": pe.current_path or "",
        "escalation_order": str(step.get("escalation_order", "") or ""),
        "escalation_type": step.get("escalation_type", "") or "",
        "language": pe_language,
        "welcome_greeting": _resolve_welcome_greeting(pe),
    }

    try:
        return template.format(**ctx)
    except KeyError as e:
        frappe.log_error(
            f"Vocallabs: status_template references undocumented variable "
            f"{e}; PE={pe.name}. Supported vars: {_TEMPLATE_VARS}. "
            f"Returning raw template.",
            "SP Vocallabs Template",
        )
        return template
    except Exception as e:
        frappe.log_error(
            f"Vocallabs: status_template render failed: {e}; PE={pe.name}.",
            "SP Vocallabs Template",
        )
        return template


def _student_display(student):
    """Per L-???: Student.name1 is the canonical display name field."""
    from tap_lms.summer_program.utils import get_student_display_name
    return get_student_display_name(student)


def _resolve_welcome_greeting(pe):
    pe_archetype = (pe.archetype or "").strip().casefold()
    pe_experiment_arm = (pe.experiment_arm or "").strip().casefold()

    if pe_archetype == "dormant" and pe_experiment_arm == "arm_b":
        return "None"
    if pe_archetype == "fence_sitter" and pe_experiment_arm == "arm_b":
        return "Vidya"
    return "TAP Buddy"


# ════════════════════════════════════════════════════════════
# HTTP — Vocallabs API
# ════════════════════════════════════════════════════════════


def _call_vocallabs(settings, token, pe, student, parent_phone, student_name, status_text):
    """Run the Vocallabs sequence, reusing a cached prospect_id when possible.

    Cache-on-Student design (task #81):
      - `Student.vocallabs_prospect_id` stores the UUID Vocallabs returned the
        FIRST time addMultipleContactsToGroup ran for this student's parent
        phone.
      - On subsequent parent_calls (next week, repeat escalation step,
        second sibling sharing the same phone via a separate Student row
        that's already had its first call), we skip the add step entirely
        and go straight to initiateVocallabsCall with the cached id.
      - First-ever call: add step → cache returned id on Student → call.
      - First call but phone already in Vocallabs (cold cache + stale data
        in their system, e.g. test pollution): we hit the uniqueness
        violation and raise PermanentVocallabsError. The cold-cache
        recovery requires a Vocallabs lookup-by-phone endpoint (task #81
        remaining scope).

    Verified API contract (Postman 2026-05-21):

    Step 2 — POST {service_url}/b2b/vocallabs/addMultipleContactsToGroup
      Body: {
        "prospects": [
          {
            "name": <parent display name>,
            "phone": <E.164 phone>,
            "data": {
              "contact": <parent display name>,
              "student_name": <student name>,
              "status": <rendered status template>
            },
            "prospect_group_id": <default_contact_group_id>,
            "client_id": <VoiceAgentSettings.client_id>
          }
        ]
      }
      Response (verified): {
        "data": {
          "insert_vocallabs_prospects": {
            "affected_rows": 1,
            "returning": [{"id": "<uuid>", ...}]
          }
        }
      }

    Step 3 — POST {service_url}/b2b/vocallabs/initiateVocallabsCall
      Body: {"agentId": <agent_id>, "prospect_id": <uuid from step 2>}
      Note `prospect_id` is snake_case (not camelCase like `agentId`).

    Step 2.5 — POST {service_url}/b2b/vocallabs/updateContactData (ONLY on
    cache-hit path, to refresh the per-call data block before initiating)
      Body: {"prospect_id": <uuid>, "data": {"contact": ..., "student_name":
            ..., "status": ...}}
      The Vocallabs `data` block is bound to the prospect record (not the
      call), so when we re-use a cached prospect_id across weeks we must
      mutate it before each call to surface the CURRENT week's status_text
      to the agent. Failure here is non-blocking — the call still places
      with whatever data is on the prospect record (possibly stale).
      Sibling race caveat: if two siblings share a parent phone AND share
      a prospect_id, simultaneous escalations could race on this update.
      For MVP we accept the race (rare in practice; the team configures
      status_template to be sibling-agnostic when needed via
      ParentCallConfig content rather than the code).

    Note: the status_template comes from the team-configured
    ParentCallConfig (per-week via UnitContentItem on LearningUnit, else
    VoiceAgentSettings.default_parent_call_config). The code never
    hard-codes content — `_resolve_parent_call_config` + `_render_status_template`
    handle resolution. Whatever template the team configures for this
    week is what gets pushed via updateContactData.

    Returns the final call response dict on success; raises on failure
    (caller treats RuntimeError as transient and PermanentVocallabsError
    as no-retry).
    """
    service_url = (settings.service_url or "").rstrip("/")
    auth_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    parent_display = f"Parent of {student_name}" if student_name else "Parent"
    pe_language = (pe.language or getattr(student, "language", "") or "").strip()
    # The data block consumed by the Vocallabs agent at call time. Same
    # keys whether we're inserting (addMultipleContactsToGroup) or
    # refreshing (updateContactData) — only one source of truth.
    data_block = {
        "contact": parent_display,
        "student_name": student_name,
        "status": status_text,
        "language": pe_language,
        "archetype": pe.archetype or "",
        "experiment_arm": pe.experiment_arm or "",
    }
    agent_id = _resolve_agent_id(settings, pe_language)
    if not agent_id:
        raise RuntimeError(
            "Vocallabs: no agent_id resolved for parent call; "
            "configure VoiceAgentSettings.agents for this language or set "
            "VoiceAgentSettings.agent_id as fallback"
        )

    # ── Cache hit: refresh data then call ─────────────────
    cached_prospect_id = (getattr(student, "vocallabs_prospect_id", "") or "").strip()
    if cached_prospect_id:
        # Refresh the prospect's `data` block so the agent uses THIS call's
        # rendered template (per the currently-resolved ParentCallConfig for
        # this week + escalation), not whatever was set on the original
        # insert. Failure here is logged + swallowed — the call still places
        # with stale data (better than not calling at all).
        _refresh_contact_data_best_effort(
            service_url=service_url,
            auth_headers=auth_headers,
            prospect_id=cached_prospect_id,
            data_block=data_block,
        )
        # Note: we trust this id is still valid in Vocallabs. If Vocallabs
        # has purged it (manual deletion / TTL on their side), the
        # initiateVocallabsCall below will fail with a runtime error and
        # retry via the normal transient path. Until that becomes a real
        # problem, the cached id is treated as authoritative.
        return _post_initiate_call(
            service_url=service_url,
            auth_headers=auth_headers,
            agent_id=agent_id,
            prospect_id=cached_prospect_id,
        )

    # ── Cache miss: insert into prospect group ────────────
    # addMultipleContactsToGroup sets the data block fresh during insert,
    # so no separate updateContactData call is needed on this path.
    add_payload = {
        "prospects": [
            {
                "name": parent_display,
                "phone": parent_phone,
                # `data` is the per-contact variables block consumed by the
                # Vocallabs agent's prompt template at call time. Keys must
                # match what the agent prompt references — `contact`,
                # `student_name`, `status` per the Postman reference. Same
                # block used by the cache-hit refresh path above.
                "data": data_block,
                "prospect_group_id": settings.default_contact_group_id or "",
                "client_id": settings.client_id or "",
            },
        ],
    }
    add_response = _http_post(
        url=f"{service_url}/b2b/vocallabs/addMultipleContactsToGroup",
        payload=add_payload,
        headers=auth_headers,
    )
    prospect_id = _extract_prospect_id(add_response)
    if not prospect_id:
        # Task #80 + #81: when the Hasura uniqueness constraint fires,
        # the parent phone is already in the Vocallabs prospect group
        # from a prior insert (test pollution, earlier launch, or any
        # out-of-band add). We auto-recover by paginating getContacts
        # to find the existing prospect_id, then cache it on Student
        # so future calls hit the cache-hit path directly.
        #
        # Cost: each lookup is a sequential pagination over getContacts
        # (typically <5s for the test cohort, capped at ~1-2 min by
        # VOCALLABS_LOOKUP_MAX_PAGES). Fires ONLY on cold-cache encounter
        # with a previously inserted phone — once it succeeds and writes
        # the cache, every future call for that student is instant.
        #
        # If lookup also fails (unfamiliar response shape, phone not
        # actually present in the scanned range, HTTP error during
        # pagination), we fall through to PermanentVocallabsError so
        # behavior degrades to fail-fast — same as the pre-lookup
        # behavior. Visible in Error Log under the dedicated title.
        if _is_duplicate_prospect_response(add_response):
            existing_id = _lookup_prospect_id_by_phone(
                service_url=service_url,
                auth_headers=auth_headers,
                client_id=settings.client_id,
                prospect_group_id=settings.default_contact_group_id or "",
                phone=parent_phone,
            )
            if existing_id:
                # Found it — cache + refresh + call.
                _store_prospect_id(student, existing_id)
                _refresh_contact_data_best_effort(
                    service_url=service_url,
                    auth_headers=auth_headers,
                    prospect_id=existing_id,
                    data_block=data_block,
                )
                return _post_initiate_call(
                    service_url=service_url,
                    auth_headers=auth_headers,
                    agent_id=agent_id,
                    prospect_id=existing_id,
                )
            # Lookup failed — fall back to the pre-lookup behavior.
            raise PermanentVocallabsError(
                f"Vocallabs: parent phone already in prospect group "
                f"(constraint={VOCALLABS_DUPLICATE_PROSPECT_CONSTRAINT}); "
                f"Student.vocallabs_prospect_id was empty AND getContacts "
                f"lookup-by-phone did not return a match within "
                f"{VOCALLABS_LOOKUP_MAX_PAGES} pages "
                f"({VOCALLABS_LOOKUP_MAX_PAGES * VOCALLABS_LOOKUP_PAGE_SIZE} "
                f"contacts scanned). Call cannot be placed; operator may need "
                f"to backfill the prospect_id manually or verify the "
                f"Vocallabs response shape. add_response={_safe_summary(add_response)}"
            )
        raise RuntimeError(
            f"Vocallabs: addMultipleContactsToGroup returned no prospect_id; "
            f"response={_safe_summary(add_response)}"
        )

    # Cache the prospect_id BEFORE making the call. If the call step fails
    # (HTTP error, etc.), the retry can skip step 2 next time and recover.
    _store_prospect_id(student, prospect_id)

    return _post_initiate_call(
        service_url=service_url,
        auth_headers=auth_headers,
        agent_id=agent_id,
        prospect_id=prospect_id,
    )


def _resolve_agent_id(settings, language):
    """Resolve the Vocallabs agent_id from per-language mappings first.

    Matching is against the raw value stored in the call data block's
    `language` key, which is sourced from Student.language. That is
    typically the TAP Language docname in this codebase.
    """
    normalized = (language or "").strip().casefold()
    for row in getattr(settings, "agents", []) or []:
        if not getattr(row, "enabled", 0):
            continue
        row_language = (getattr(row, "language", "") or "").strip().casefold()
        if normalized and row_language == normalized:
            return (getattr(row, "agent_id", "") or "").strip()
    return (getattr(settings, "agent_id", "") or "").strip()


def _post_initiate_call(service_url, auth_headers, agent_id, prospect_id):
    """Step 3 — POST /b2b/vocallabs/initiateVocallabsCall.

    Split out so both the cache-hit and cache-miss branches share one
    code path. Returns the response dict (caller treats non-empty as
    success); raises on HTTP error.

    Note: `agentId` is camelCase but `prospect_id` is snake_case per the
    verified Postman contract. Inconsistent on Vocallabs' side; don't fix
    this to be consistent — match the API.
    """
    call_payload = {
        "agentId": agent_id,
        "prospect_id": prospect_id,
    }
    return _http_post(
        url=f"{service_url}/b2b/vocallabs/initiateVocallabsCall",
        payload=call_payload,
        headers=auth_headers,
    )


def _lookup_prospect_id_by_phone(service_url, auth_headers, client_id,
                                  prospect_group_id, phone):
    """Paginate /b2b/vocallabs/getContacts looking for an existing prospect
    that matches our (client_id, prospect_group_id, phone) tuple.

    Fires from the cache-miss duplicate-prospect branch in _call_vocallabs —
    Vocallabs has told us via the Hasura uniqueness violation that the
    prospect exists; we just need its UUID. The docs (verified 2026-05-24)
    show getContacts only accepts `limit`+`offset` query params with no
    filter, so we paginate client-side and match phones.

    Phone matching:
      - Exact string match first (handles E.164 / non-E.164 consistency
        when both sides use the same form).
      - Falls back to last-10-digits match (handles country-code drift —
        "9411795145" vs "919411795145" vs "+919411795145" all hash to
        "9411795145"). 10 digits is enough to disambiguate Indian numbers,
        which is the launch scope.
      - When the contact record exposes client_id / prospect_group_id,
        we further constrain to those — so this is safe to run against a
        Vocallabs account with multiple groups or multi-tenant clients.

    Returns the prospect UUID string on success, None on:
      - Phone not found within MAX_PAGES * PAGE_SIZE contacts scanned
      - HTTP error during pagination (logged + treated as not found so
        the caller falls back to PermanentVocallabsError)
      - Unrecognized response shape (logged + treated as not found)

    All Error Log entries use VOCALLABS_LOOKUP_LOG_TITLE so ops can
    separately track lookup health (e.g. monitor for a sudden spike if
    Vocallabs changes their response schema).
    """
    target_digits = "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]
    if not target_digits:
        return None

    pages_scanned = 0
    contacts_scanned = 0
    try:
        for page in range(VOCALLABS_LOOKUP_MAX_PAGES):
            offset = page * VOCALLABS_LOOKUP_PAGE_SIZE
            response = _http_get(
                url=f"{service_url}/b2b/vocallabs/getContacts",
                params={"limit": VOCALLABS_LOOKUP_PAGE_SIZE, "offset": offset},
                headers=auth_headers,
            )
            pages_scanned += 1
            contacts = _extract_contacts_list(response)
            if contacts is None:
                # Shape unrecognized on the first response — log + bail.
                # Don't keep paginating against a misunderstood endpoint.
                frappe.log_error(
                    f"Vocallabs: lookup-by-phone unrecognized getContacts "
                    f"response shape at offset={offset}; "
                    f"response={_safe_summary(response)}",
                    VOCALLABS_LOOKUP_LOG_TITLE,
                )
                return None
            if not contacts:
                # Empty list = end of pagination.
                break
            contacts_scanned += len(contacts)

            for c in contacts:
                if not isinstance(c, dict):
                    continue
                c_phone = str(
                    c.get("phone") or c.get("phone_number") or ""
                ).strip()
                if not c_phone:
                    continue
                c_digits = "".join(ch for ch in c_phone if ch.isdigit())[-10:]
                if c_phone != phone and c_digits != target_digits:
                    continue
                # Optional tighteners — skip mismatched client/group when
                # Vocallabs exposes those fields.
                c_client = c.get("client_id")
                c_group = c.get("prospect_group_id")
                if c_client and client_id and c_client != client_id:
                    continue
                if c_group and prospect_group_id and c_group != prospect_group_id:
                    continue
                pid = c.get("id") or c.get("prospect_id") or c.get("uuid")
                if pid:
                    return pid

            # Short page = end of data.
            if len(contacts) < VOCALLABS_LOOKUP_PAGE_SIZE:
                break
    except Exception as e:
        frappe.log_error(
            f"Vocallabs: lookup-by-phone pagination failed at page "
            f"{pages_scanned} (offset={pages_scanned * VOCALLABS_LOOKUP_PAGE_SIZE}) "
            f"for phone={phone}: {e}. Returning None — caller will raise "
            f"PermanentVocallabsError so the dispatcher keeps moving.",
            VOCALLABS_LOOKUP_LOG_TITLE,
        )
        return None

    # Scanned all available pages without a match.
    frappe.log_error(
        f"Vocallabs: phone {phone} not found in getContacts after "
        f"{pages_scanned} pages ({contacts_scanned} contacts scanned). "
        f"Either the phone really isn't on Vocallabs (response-shape bug?), "
        f"or it's beyond the {VOCALLABS_LOOKUP_MAX_PAGES}-page cap. Operator "
        f"may need to backfill Student.vocallabs_prospect_id manually.",
        VOCALLABS_LOOKUP_LOG_TITLE,
    )
    return None


def _extract_contacts_list(response):
    """Defensively pull the list of contact dicts out of a getContacts
    response.

    Docs don't show the response schema, so we try the common Hasura/
    Vocallabs shapes in priority order. Returns:
      - list (possibly empty) of contact dicts on success
      - None if we can't find a list anywhere — caller treats this as
        "unrecognized shape, stop paginating"

    The empty-list-vs-None distinction matters: empty list = legitimate
    end-of-pagination (don't log an error); None = response is genuinely
    not what we expected (log an error and bail).
    """
    if response is None:
        return None
    if isinstance(response, list):
        return response

    if not isinstance(response, dict):
        return None

    # Hasura-style: data.vocallabs_prospects (matches the
    # insert_vocallabs_prospects shape from addMultipleContactsToGroup —
    # likely the same table queried directly).
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("vocallabs_prospects", "prospects", "contacts"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        # Single-list-value shape: {"data": {"X": [...]}}
        for v in data.values():
            if isinstance(v, list):
                return v
            # One more level: {"data": {"X": {"prospects": [...]}}}
            if isinstance(v, dict):
                for nested_key in ("prospects", "vocallabs_prospects",
                                   "data", "result"):
                    nv = v.get(nested_key)
                    if isinstance(nv, list):
                        return nv
    elif isinstance(data, list):
        return data

    # Top-level list keys: {"contacts": [...]} / {"prospects": [...]}
    for key in ("contacts", "prospects", "result", "items"):
        v = response.get(key)
        if isinstance(v, list):
            return v

    return None


def _http_get(url, params, headers):
    """GET equivalent of _http_post. Tries the Frappe helper first
    (centralized outbound HTTP audit), falls back to raw requests with the
    same 10s timeout.
    """
    try:
        from frappe.integrations.utils import make_get_request
        return make_get_request(url, params=params, headers=headers)
    except ImportError:
        pass
    except Exception as e:
        raise RuntimeError(f"Vocallabs HTTP GET error (Frappe helper): {e}")

    try:
        import requests
    except ImportError:
        raise RuntimeError(
            "Vocallabs: requests library not available and "
            "frappe.integrations.utils.make_get_request not importable."
        )

    response = requests.get(
        url, params=params, headers=headers,
        timeout=VOCALLABS_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(
            f"Vocallabs: non-JSON response body from {url}: "
            f"{response.text[:500]!r}"
        )


def _refresh_contact_data_best_effort(service_url, auth_headers, prospect_id, data_block):
    """POST /b2b/vocallabs/updateContactData to refresh the prospect's data
    block right before initiating the call (cache-hit path only).

    Why this exists: Vocallabs binds the per-call variables (`contact`,
    `student_name`, `status`) to the PROSPECT record, not the call. When
    we re-use a cached prospect_id across weeks, the original `data` from
    addMultipleContactsToGroup is stale — week 1's rendered status_text
    is still there when we call in week 3. This step pushes the freshly
    rendered text (from the currently resolved ParentCallConfig for THIS
    week) onto the prospect before the call fires.

    Best-effort by design: any failure here is logged + swallowed and the
    call proceeds with whatever data is on the prospect record. Failing
    closed (refusing to call on update failure) would cost us a parent
    contact attempt that we could have made; failing open just means the
    agent might speak slightly stale variables.

    Body shape (per Vocallabs docs):
        POST /b2b/vocallabs/updateContactData
        {"prospect_id": <uuid>, "data": {<key>: <value>, ...}}

    The team configures status_template via ParentCallConfig (per-week via
    UnitContentItem on LearningUnit, else the default on VoiceAgentSettings).
    Whatever's resolved + rendered for this week shows up in data_block —
    the code does not hard-code any content.
    """
    try:
        _http_post(
            url=f"{service_url}/b2b/vocallabs/updateContactData",
            payload={"prospect_id": prospect_id, "data": data_block},
            headers=auth_headers,
        )
    except Exception as e:
        # Non-blocking: the call still places. Status text on the prospect
        # record may be stale (from a prior week / step), but that's better
        # than not calling the parent at all.
        frappe.log_error(
            f"Vocallabs: updateContactData failed for prospect={prospect_id}: {e}. "
            f"Call will proceed with stale data on prospect record.",
            "SP Vocallabs UpdateData",
        )


def _store_prospect_id(student, prospect_id):
    """Persist the Vocallabs prospect_id on Student.vocallabs_prospect_id.

    Uses `frappe.db.set_value` (not student.save()) to avoid running the
    full doc lifecycle — this is a single-column write happening in a
    background job and we don't want it to fire validation, hooks, or
    timestamp bumps.

    Failures here MUST NOT bubble — we've already obtained a valid
    prospect_id from Vocallabs and the call will still place. A failed
    cache write just means the next call will try addMultipleContacts
    again and re-cache (or fail with PermanentVocallabsError, which is
    visible in Error Log under the dedicated title).
    """
    try:
        frappe.db.set_value(
            "Student", student.name,
            "vocallabs_prospect_id", prospect_id,
            update_modified=False,
        )
        # Reflect it on the in-memory doc too so a re-entrant call within
        # the same transaction sees the cached value without a DB round-trip.
        try:
            student.vocallabs_prospect_id = prospect_id
        except Exception:
            pass
    except Exception as e:
        frappe.log_error(
            f"Vocallabs: failed to cache prospect_id={prospect_id} on "
            f"Student {student.name}: {e}. Call still placed; next call "
            f"will re-add to Vocallabs and likely hit duplicate-prospect.",
            "SP Vocallabs Cache",
        )


def _extract_prospect_id(response):
    """Extract the prospect UUID from Vocallabs' addMultipleContactsToGroup
    response.

    Verified shape (Postman 2026-05-21):
      {"data": {"insert_vocallabs_prospects": {"affected_rows": 1,
                                               "returning": [{"id": "<uuid>", ...}]}}}

    Some Vocallabs endpoints (e.g., createContactGroup) wrap the response
    in a single-element list `[{...}]` instead of returning the object
    directly — unwrap defensively so we work with either shape.
    """
    if isinstance(response, list):
        response = response[0] if response else {}
    if not isinstance(response, dict):
        return None

    # Primary: the documented nested shape.
    data = response.get("data") or {}
    ins = data.get("insert_vocallabs_prospects") or {}
    returning = ins.get("returning") or []
    if returning and isinstance(returning, list):
        first = returning[0] or {}
        if first.get("id"):
            return first["id"]

    # Defensive fallbacks for shape drift.
    return (
        response.get("prospect_id")
        or response.get("prospectId")
        or response.get("id")
        or data.get("prospect_id")
        or data.get("id")
    )


def _is_duplicate_prospect_response(response):
    """Return True if the addMultipleContactsToGroup response is the Hasura
    uniqueness-constraint violation indicating the parent's phone is already
    in the prospect group.

    Verified shape (production, 2026-05-23):
        {"errors": [
          {"message": "Uniqueness violation. duplicate key value violates
                       unique constraint
                       \\"prospects_client_id_prospect_group_id_phone_key\\"",
           "extensions": {"code": "constraint-violation",
                          "path": "$.selectionSet.insert_vocallabs_prospects.args.objects"}}
        ]}

    Matching strategy:
      - Substring search for the constraint name anywhere in the response —
        most specific signal (Vocallabs may change the wrapper format but
        the underlying constraint name is stable).
      - Fallback: extensions.code == "constraint-violation" in any errors[]
        entry. Catches the case where Vocallabs renames the constraint but
        keeps the GraphQL extension code.

    Defensive against shape variation: lists are unwrapped, non-dicts return
    False rather than raising.
    """
    if response is None:
        return False
    if isinstance(response, list):
        response = response[0] if response else {}
    if not isinstance(response, dict):
        return False

    # Cheap path: serialise the whole response and look for the constraint
    # name. Catches the message anywhere — top-level errors[], nested under
    # data, or in some future repackaging.
    try:
        text = json.dumps(response, default=str)
    except Exception:
        text = str(response)
    if VOCALLABS_DUPLICATE_PROSPECT_CONSTRAINT in text:
        return True

    # Fallback: walk errors[] and check the GraphQL extension code.
    errors = response.get("errors")
    if isinstance(errors, list):
        for err in errors:
            if not isinstance(err, dict):
                continue
            extensions = err.get("extensions") or {}
            if extensions.get("code") == "constraint-violation":
                return True
    return False


def _http_post(url, payload, headers):
    """POST with a 10s timeout. Tries `frappe.integrations.utils.make_post_request`
    first (so request audit + retry-policy plumbing applies); falls back to
    `requests.post` if the helper is unavailable.

    Returns the parsed JSON response (dict) or raises.
    """
    # Try Frappe's helper first — it centralizes outbound HTTP audit.
    try:
        from frappe.integrations.utils import make_post_request
        return make_post_request(
            url,
            data=json.dumps(payload),
            headers=headers,
        )
    except ImportError:
        pass
    except Exception as e:
        # Frappe's helper raised something; bubble it so the caller can retry.
        raise RuntimeError(f"Vocallabs HTTP error (Frappe helper): {e}")

    # Fallback to raw requests.
    try:
        import requests
    except ImportError:
        raise RuntimeError(
            "Vocallabs: requests library not available and "
            "frappe.integrations.utils.make_post_request not importable."
        )

    response = requests.post(
        url, data=json.dumps(payload),
        headers=headers, timeout=VOCALLABS_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(
            f"Vocallabs: non-JSON response body from {url}: {response.text[:500]!r}"
        )


# ════════════════════════════════════════════════════════════
# Retry + DLQ (P-007 / L-015)
# ════════════════════════════════════════════════════════════


def _handle_failure(pe, escalation_step, parent_phone, error, retry_count):
    """Mirror of state_machine._sync_contact_fields_job's retry policy:
    5 retries on transient failures, then DLQ to Error Log with a payload
    the operator can replay manually.

    Permanent failures (task #80 — `PermanentVocallabsError`) short-circuit:
    they get a single Error Log entry under a dedicated title and return
    False immediately. Retrying cannot help (e.g., the duplicate-prospect
    constraint will always conflict with the same payload), so retries
    just waste worker cycles and inflate Error Log noise.

    Per CR-003 §E4 a Vocallabs DLQ does NOT extend the grace window. The
    PE proceeds toward drop on its normal schedule.
    """
    pe_name = pe.name
    escalation_order = escalation_step.get("escalation_order")
    week = pe.current_week

    # ── Permanent-failure short-circuit (task #80) ──────────
    if isinstance(error, PermanentVocallabsError):
        frappe.log_error(
            title=VOCALLABS_DUPLICATE_PROSPECT_LOG_TITLE,
            message=json.dumps({
                "reason": "duplicate_prospect_no_retry",
                "student_id": pe.student,
                "pe_name": pe_name,
                "week": week,
                "escalation_order": escalation_order,
                "parent_phone": parent_phone,
                "final_error": str(error),
                # No retries_attempted — this is a single-shot permanent fail.
                # Follow-up: task #81 (lookup existing prospect_id).
                "followup_task": 81,
            }, indent=2, default=str),
        )
        return False

    retry_count = (retry_count or 0) + 1

    if retry_count <= VOCALLABS_MAX_RETRIES:
        # Log the transient + re-enqueue (no backoff for parity with the
        # Glific sync retry policy in state_machine.py).
        frappe.log_error(
            title=VOCALLABS_RETRY_LOG_TITLE,
            message=(
                f"Vocallabs transient failure "
                f"(attempt {retry_count}/{VOCALLABS_MAX_RETRIES + 1}) "
                f"for PE {pe_name} (student={pe.student}, "
                f"week={week}, escalation_order={escalation_order}, "
                f"parent_phone={parent_phone}): {error}"
            ),
        )
        try:
            frappe.enqueue(
                "tap_lms.summer_program.vocallabs.initiate_parent_call",
                queue="long",
                timeout=300,
                pe_name=pe_name,
                escalation_step=escalation_step,
                retry_count=retry_count,
            )
            return False
        except Exception as enqueue_err:
            # Double-fault — surface to DLQ immediately so the call request
            # isn't silently lost (mirrors state_machine.py's double-fault).
            frappe.log_error(
                title=VOCALLABS_DLQ_LOG_TITLE,
                message=json.dumps({
                    "reason": "double_fault_enqueue_failed",
                    "student_id": pe.student,
                    "pe_name": pe_name,
                    "week": week,
                    "escalation_order": escalation_order,
                    "parent_phone": parent_phone,
                    "final_error": str(error),
                    "enqueue_error": str(enqueue_err),
                    "retries_attempted": retry_count,
                }, indent=2, default=str),
            )
            return False

    # Retries exhausted → permanent DLQ.
    frappe.log_error(
        title=VOCALLABS_DLQ_LOG_TITLE,
        message=json.dumps({
            "student_id": pe.student,
            "pe_name": pe_name,
            "week": week,
            "escalation_order": escalation_order,
            "parent_phone": parent_phone,
            "final_error": str(error),
            "retries_attempted": retry_count,
        }, indent=2, default=str),
    )
    return False


# ════════════════════════════════════════════════════════════
# Misc
# ════════════════════════════════════════════════════════════


def _safe_summary(obj, max_len=500):
    """Truncate any JSON-serializable object for log-friendliness."""
    try:
        text = json.dumps(obj, default=str)
    except Exception:
        text = str(obj)
    if len(text) > max_len:
        return text[:max_len] + "...[truncated]"
    return text
