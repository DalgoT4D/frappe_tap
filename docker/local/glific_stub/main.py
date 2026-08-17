"""
infra/docker/glific_stub/main.py

FastAPI stub that mimics the Glific GraphQL API used by tap_lms.

Replaces the real Glific API in local development so:
  - No real WhatsApp messages are sent to students
  - No real Glific account credentials are needed
  - All GraphQL operations return realistic responses immediately
  - Every call is logged so developers can verify the pipeline reached
    the notification step

Glific uses a single GraphQL endpoint: POST /api
Authentication: POST /api/v1/session

Operations stubbed (all found in glific_integration.py):
  Mutations:
    createContact         — returns a fake contact with a deterministic ID
    updateContact         — returns success
    optinContact          — returns success
    startContactFlow      — returns success (the feedback delivery step)
    createGroup           — returns a fake group
    updateGroupContacts   — returns success

  Queries:
    contactByPhone        — returns a fake contact or null
    contact(id)           — returns a fake contact
    groups(filter)        — returns matching fake groups

Usage:
  Configured in docker-compose.local.yml as service "glific-stub".
  Set GLIFIC_API_URL=http://glific-stub:4000 in the Frappe dev container's
  environment — this overrides what is stored in the Glific Settings DocType.
  (Or seed the DocType with this URL during local setup.)
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# remove health check pings from logs
class _HealthCheckFilter(logging.Filter):
    def filter(self, record):
        return "/health" not in record.getMessage()


# ── Setup ─────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("glific-stub").addFilter(_HealthCheckFilter())

app = FastAPI(
    title="Glific Stub", description="Local Glific API stub for TAP LMS development"
)

# ── In-memory state ───────────────────────────────────────────────────────────
# Keeps fake contacts and groups alive for the duration of the process.
# Resets on container restart — intentional for a dev stub.

_contacts: Dict[str, Dict] = {}  # phone → contact
_groups: Dict[str, Dict] = {}  # label → group
_flow_calls: list = []  # audit log of all startContactFlow calls


def _make_contact_id(phone: str) -> str:
    """Deterministic fake contact ID from phone number."""
    return str(abs(hash(phone)) % 9_000_000 + 1_000_000)


def _make_contact(phone: str, name: str, language_id: int = 1) -> Dict:
    return {
        "id": _make_contact_id(phone),
        "name": name,
        "phone": phone,
        "languageId": language_id,
        "optinTime": datetime.now(timezone.utc).isoformat(),
        "optoutTime": None,
        "bspStatus": "SESSION_AND_HSM",
        "status": "VALID",
        "lastMessageAt": datetime.now(timezone.utc).isoformat(),
        "fields": "{}",
        "settings": "{}",
    }


def _make_group(label: str, description: str = "") -> Dict:
    return {
        "id": str(abs(hash(label)) % 900_000 + 100_000),
        "label": label,
        "description": description,
    }


# ── Authentication endpoint ───────────────────────────────────────────────────


@app.post("/api/v1/session")
async def session(request: Request):
    """
    Glific auth endpoint. Returns a stub token that never expires
    (well, expires in 100 years — effectively never for local dev).
    tap_lms caches the token in the Glific Settings DocType and
    refreshes when it expires, so a long-lived token avoids noise.
    """
    logger.info("AUTH: token requested")
    expiry = datetime.now(timezone.utc) + timedelta(days=36500)
    return JSONResponse(
        content={
            "data": {
                "access_token": "stub-access-token-local-dev",
                "renewal_token": "stub-renewal-token-local-dev",
                "token_expiry_time": expiry.isoformat(),
            }
        }
    )


# ── GraphQL endpoint ──────────────────────────────────────────────────────────


@app.post("/api")
async def graphql(request: Request):
    """
    Single GraphQL endpoint. Routes by operation name extracted from the query.
    Returns realistic stubbed responses for all operations used by tap_lms.
    """
    body = await request.json()
    query: str = body.get("query", "")
    variables: Dict = body.get("variables", {})

    # Route by operation name / first keyword in the query
    query_lower = query.lower().strip()

    # ── Mutations ─────────────────────────────────────────────────────────────

    if "startcontactflow" in query_lower:
        return _start_contact_flow(variables)

    if "createcontact" in query_lower:
        return _create_contact(variables)

    if "updatecontact" in query_lower:
        return _update_contact(variables)

    if "optincontact" in query_lower:
        return _optin_contact(variables)

    if "creategroup" in query_lower:
        return _create_group(variables)

    if "updategroupcontacts" in query_lower:
        return _update_group_contacts(variables)

    # ── Queries ───────────────────────────────────────────────────────────────

    if "contactbyphone" in query_lower:
        return _contact_by_phone(variables)

    if "contact(" in query_lower or "contact(id" in query_lower:
        return _get_contact(variables)

    if "groups(" in query_lower:
        return _list_groups(variables)

    # Unknown operation — return empty success so tap_lms doesn't crash
    logger.warning(
        f"UNKNOWN GraphQL operation — returning empty success. Query: {query[:120]}"
    )
    return JSONResponse(content={"data": {}})


# ── Mutation handlers ─────────────────────────────────────────────────────────


def _start_contact_flow(variables: Dict) -> JSONResponse:
    """
    The most important stub — this is what fires when a student receives
    feedback via WhatsApp. Log every call with full context so developers
    can verify the pipeline reached the notification step.
    """
    flow_id = variables.get("flowId")
    contact_id = variables.get("contactId")
    default_results_raw = variables.get("defaultResults", "{}")

    try:
        default_results = (
            json.loads(default_results_raw)
            if isinstance(default_results_raw, str)
            else default_results_raw
        )
    except Exception:
        default_results = {}

    call_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "flow_id": flow_id,
        "contact_id": contact_id,
        "submission_id": default_results.get("submission_id"),
        "feedback_preview": str(default_results.get("feedback", ""))[:120],
    }
    _flow_calls.append(call_record)

    logger.info(
        f"FLOW TRIGGERED ✓ | flow_id={flow_id} contact_id={contact_id} "
        f"submission_id={default_results.get('submission_id')} | "
        f"[WhatsApp message would be sent here in production]"
    )

    return JSONResponse(
        content={"data": {"startContactFlow": {"success": True, "errors": []}}}
    )


def _create_contact(variables: Dict) -> JSONResponse:
    inp = variables.get("input", {})
    phone = inp.get("phone", f"stub_{uuid.uuid4().hex[:8]}")
    name = inp.get("name", "Stub Student")
    language_id = inp.get("languageId", 1)

    contact = _make_contact(phone, name, language_id)
    _contacts[phone] = contact

    logger.info(f"CREATE CONTACT | name={name} phone={phone} id={contact['id']}")

    return JSONResponse(
        content={
            "data": {
                "createContact": {
                    "contact": {
                        "id": contact["id"],
                        "name": contact["name"],
                        "phone": contact["phone"],
                    },
                    "errors": [],
                }
            }
        }
    )


def _update_contact(variables: Dict) -> JSONResponse:
    contact_id = variables.get("id")
    inp = variables.get("input", {})

    # Update fields in our in-memory store if contact exists
    for phone, contact in _contacts.items():
        if contact["id"] == str(contact_id):
            if "fields" in inp:
                contact["fields"] = inp["fields"]
            break

    logger.info(f"UPDATE CONTACT | id={contact_id}")

    return JSONResponse(
        content={
            "data": {
                "updateContact": {
                    "contact": {"id": contact_id, "fields": inp.get("fields", "{}")},
                    "errors": [],
                }
            }
        }
    )


def _optin_contact(variables: Dict) -> JSONResponse:
    phone = variables.get("phone", "")
    name = variables.get("name", "")

    # Create contact if not already in store
    if phone not in _contacts:
        _contacts[phone] = _make_contact(phone, name)

    contact = _contacts[phone]
    logger.info(f"OPTIN CONTACT | phone={phone} name={name} id={contact['id']}")

    return JSONResponse(
        content={
            "data": {
                "optinContact": {
                    "contact": {
                        "id": contact["id"],
                        "phone": contact["phone"],
                        "name": contact["name"],
                        "lastMessageAt": contact["lastMessageAt"],
                        "optinTime": contact["optinTime"],
                        "bspStatus": contact["bspStatus"],
                    },
                    "errors": [],
                }
            }
        }
    )


def _create_group(variables: Dict) -> JSONResponse:
    inp = variables.get("input", {})
    label = inp.get("label", f"stub-group-{uuid.uuid4().hex[:6]}")
    description = inp.get("description", "")

    group = _make_group(label, description)
    _groups[label] = group

    logger.info(f"CREATE GROUP | label={label} id={group['id']}")

    return JSONResponse(
        content={"data": {"createGroup": {"group": group, "errors": []}}}
    )


def _update_group_contacts(variables: Dict) -> JSONResponse:
    inp = variables.get("input", {})
    group_id = inp.get("groupId")
    add_ids = inp.get("addContactIds", [])

    logger.info(f"ADD TO GROUP | group_id={group_id} contact_ids={add_ids}")

    return JSONResponse(
        content={
            "data": {
                "updateGroupContacts": {
                    "groupContacts": [{"id": str(uuid.uuid4())} for _ in add_ids],
                    "numberDeleted": 0,
                }
            }
        }
    )


# ── Query handlers ────────────────────────────────────────────────────────────


def _contact_by_phone(variables: Dict) -> JSONResponse:
    phone = variables.get("phone", "")
    contact = _contacts.get(phone)

    if contact:
        logger.info(f"CONTACT BY PHONE | phone={phone} → found id={contact['id']}")
    else:
        logger.info(f"CONTACT BY PHONE | phone={phone} → not found")

    return JSONResponse(content={"data": {"contactByPhone": {"contact": contact}}})


def _get_contact(variables: Dict) -> JSONResponse:
    contact_id = str(variables.get("id", ""))

    # Find by ID in our store
    found = None
    for contact in _contacts.values():
        if contact["id"] == contact_id:
            found = contact
            break

    return JSONResponse(content={"data": {"contact": {"contact": found}}})


def _list_groups(variables: Dict) -> JSONResponse:
    label_filter = variables.get("filter", {}).get("label", "")

    if label_filter:
        matching = [
            g for label, g in _groups.items() if label_filter.lower() in label.lower()
        ]
    else:
        matching = list(_groups.values())

    return JSONResponse(content={"data": {"groups": matching}})


# ── Audit log endpoint (bonus — useful for dev inspection) ───────────────────


@app.get("/stub/flow-calls")
def get_flow_calls():
    """
    Returns a log of all startContactFlow calls made during this session.
    Use this to verify the pipeline reached the WhatsApp notification step
    for a given submission_id:

      curl http://localhost:4000/stub/flow-calls | python3 -m json.tool
    """
    return JSONResponse(
        content={
            "total": len(_flow_calls),
            "calls": _flow_calls,
        }
    )


@app.get("/stub/contacts")
def get_contacts():
    """Returns all contacts created during this session."""
    return JSONResponse(
        content={"total": len(_contacts), "contacts": list(_contacts.values())}
    )


@app.get("/stub/reset")
def reset():
    """Clears all in-memory state. Useful between test runs."""
    _contacts.clear()
    _groups.clear()
    _flow_calls.clear()
    logger.info("STUB STATE RESET")
    return JSONResponse(content={"status": "reset"})


@app.get("/health")
def health():
    return JSONResponse(
        content={
            "status": "ok",
            "service": "glific-stub",
            "flow_calls_this_session": len(_flow_calls),
            "contacts_this_session": len(_contacts),
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4000, log_level="info")
