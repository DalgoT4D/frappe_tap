"""
Glific Integration Extensions for Summer Program
tap_lms/summer_program/glific_extensions.py

New Glific GraphQL functions needed by the Summer Program.
These extend the existing tap_lms/glific_integration.py module.

IMPORTANT: Add these functions to the EXISTING glific_integration.py file,
or import the base helpers from there.
"""
import frappe
import json

from tap_lms.glific_integration import (
    get_glific_settings,
    get_glific_auth_headers,
    check_glific_group_exists,
    create_glific_group,
    _glific_post_with_401_retry,
    GLIFIC_TIMEOUT,
)


def start_group_flow(flow_id, group_id, default_results=None):
    """
    Trigger a Glific flow on an entire collection (group).
    One API call instead of N per-student calls.

    Args:
        flow_id: Glific flow ID (int or str)
        group_id: Glific collection/group ID (int or str)
        default_results: Optional dict of default results to pass to the flow

    Returns:
        True on success, False on failure
    """
    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    variables = {
        "flowId": str(flow_id),
        "groupId": str(group_id),
    }
    if default_results:
        variables["defaultResults"] = json.dumps(default_results)

    payload = {
        "query": """
        mutation startGroupFlow($flowId: ID!, $groupId: ID!, $defaultResults: Json) {
            startGroupFlow(flowId: $flowId, groupId: $groupId, defaultResults: $defaultResults) {
                success
                errors {
                    key
                    message
                }
            }
        }
        """,
        "variables": variables,
    }

    try:
        # CR-025: use session + 401-retry helper (was bare requests.post, no session, no timeout)
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(f"Glific API error in start_group_flow: {data['errors']}")
            return False

        success = data.get("data", {}).get("startGroupFlow", {}).get("success")
        if success:
            frappe.logger().info(
                f"Started group flow {flow_id} on collection {group_id}"
            )
            return True

        frappe.logger().error(f"start_group_flow failed. Response: {data}")
        return False

    except Exception as e:
        # L-035: surface to the Error Log (operator-visible), not just the bench
        # log. Contract preserved (callers rely on False) — we log loudly, not raise.
        try:
            frappe.log_error(
                f"start_group_flow error: flow={flow_id} group={group_id}: {e}",
                "Glific start_group_flow Error",
            )
        except Exception:
            frappe.logger().error(f"start_group_flow error (double-fault): {e}")
        return False


def add_contacts_to_group_bulk(contact_ids, group_id):
    """
    Add multiple contacts to a Glific collection in one API call.
    Wraps the same updateGroupContacts mutation used by add_contact_to_group
    but accepts a list of IDs.

    Args:
        contact_ids: list of Glific contact ID strings
        group_id: Glific group ID string

    Returns:
        True on success, False on failure
    """
    if not contact_ids or not group_id:
        return False

    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    payload = {
        "query": """
        mutation updateGroupContacts($input: GroupContactsInput!) {
          updateGroupContacts(input: $input) {
            groupContacts {
              id
            }
            numberDeleted
          }
        }
        """,
        "variables": {
            "input": {
                "groupId": str(group_id),
                "addContactIds": [str(cid) for cid in contact_ids],
                "deleteContactIds": [],
            }
        },
    }

    try:
        # CR-025: use session + 401-retry helper (was bare requests.post, no session, no timeout)
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(
                f"Glific API error in add_contacts_to_group_bulk: {data['errors']}"
            )
            return False

        result = data.get("data", {}).get("updateGroupContacts")
        if result is not None:
            frappe.logger().info(
                f"Bulk-added {len(contact_ids)} contacts to group {group_id}"
            )
            return True

        frappe.logger().error(f"add_contacts_to_group_bulk unexpected response: {data}")
        return False

    except Exception as e:
        # L-035: surface to the Error Log (operator-visible), not just the bench
        # log. Contract preserved (callers rely on False) — we log loudly, not raise.
        try:
            frappe.log_error(
                f"add_contacts_to_group_bulk error: group={group_id} "
                f"n_contacts={len(contact_ids) if contact_ids else 0}: {e}",
                "Glific add_contacts_to_group_bulk Error",
            )
        except Exception:
            frappe.logger().error(f"add_contacts_to_group_bulk error (double-fault): {e}")
        return False


def remove_contacts_from_group_bulk(contact_ids, group_id):
    """
    Remove multiple contacts from a Glific collection in one API call.

    Mirrors `add_contacts_to_group_bulk` but flips the GroupContactsInput to
    use deleteContactIds instead of addContactIds. Same updateGroupContacts
    mutation under the hood.

    NOTE: uses `_glific_post_with_401_retry` (the CR-025 / L-078 session +
    token-refresh helper), NOT a bare `requests.post`. A bulk migration
    (CR-027, 62K contacts) is exactly when a token can expire mid-run — the
    401-retry path is mandatory here.

    Args:
        contact_ids: list of Glific contact ID strings
        group_id: Glific group ID string

    Returns:
        True on success, False on failure
    """
    if not contact_ids or not group_id:
        return False

    settings = get_glific_settings()
    url = f"{settings.api_url}/api"

    payload = {
        "query": """
        mutation updateGroupContacts($input: GroupContactsInput!) {
          updateGroupContacts(input: $input) {
            groupContacts {
              id
            }
            numberDeleted
          }
        }
        """,
        "variables": {
            "input": {
                "groupId": str(group_id),
                "addContactIds": [],
                "deleteContactIds": [str(cid) for cid in contact_ids],
            }
        },
    }

    try:
        response = _glific_post_with_401_retry(url, payload)
        data = response.json()

        if "errors" in data:
            frappe.logger().error(
                f"Glific API error in remove_contacts_from_group_bulk: {data['errors']}"
            )
            return False

        result = data.get("data", {}).get("updateGroupContacts")
        if result is not None:
            frappe.logger().info(
                f"Bulk-removed {len(contact_ids)} contacts from group {group_id}"
            )
            return True

        frappe.logger().error(
            f"remove_contacts_from_group_bulk unexpected response: {data}"
        )
        return False

    except Exception as e:
        # L-035: surface to the Error Log (operator-visible), not just the bench
        # log. Contract preserved (callers rely on False) — we log loudly, not raise.
        try:
            frappe.log_error(
                f"remove_contacts_from_group_bulk error: group={group_id} "
                f"n_contacts={len(contact_ids) if contact_ids else 0}: {e}",
                "Glific remove_contacts_from_group_bulk Error",
            )
        except Exception:
            frappe.logger().error(f"remove_contacts_from_group_bulk error (double-fault): {e}")
        return False


def create_or_get_collection(label, description=""):
    """
    Idempotent helper: return existing Glific group or create a new one.

    Returns:
        dict with {"id": ..., "label": ...} or None on failure
    """
    existing = check_glific_group_exists(label)
    if existing:
        return existing

    new_group = create_glific_group(label, description)
    if new_group:
        return new_group

    frappe.logger().error(f"Failed to create_or_get_collection: {label}")
    return None


def bulk_add_to_group_with_circuit_breaker(
    contact_ids,
    group_id,
    *,
    chunk_size=None,
    max_consecutive_failures=3,
    op_label="bulk_add",
):
    """Chunked bulk-add to a Glific group with progress log + circuit breaker.

    Shared helper (L-074 anti-drift) used by:
      - activate_bpr's _bulk_populate_kind_keyed_collections (BR-002 fix path)
      - migrations/main_collection_backfill.backfill_main_collection (CR-029
        Phase 1 — operator backfill for already-activated BPRs that missed
        the bulk-populate)
      - TODO(CR-029-followup): the add-half of CR-027's
        sweep_migration._bulk_move_to_escalation

    Each chunk is its own try/except — a failure does not abort the whole run.
    But N (= max_consecutive_failures) consecutive failures trip the breaker
    and skip the rest, on the assumption that the Glific API is in a sustained
    bad state (token expired despite L-078 retry, network partition, quota
    exhaustion) where continuing burns tokens for no result. A successful
    chunk resets the consecutive counter, so a sporadic mid-run failure does
    not trip the breaker.

    Args:
        contact_ids: list of Glific contact ID strings. Empty list returns a
            zero-summary without touching Glific.
        group_id: Glific group ID string. Missing/empty group_id returns an
            error in the summary without attempting any add.
        chunk_size: int (default constants.COLLECTION_BATCH_SIZE = 500). This
            is the same chunk size every other SP bulk caller uses, so the
            default keeps cross-helper behavior aligned.
        max_consecutive_failures: int (default 3). After this many back-to-back
            False/exception results from add_contacts_to_group_bulk, the loop
            aborts; remaining chunks are reported as `skipped_after_trip`.
        op_label: short label used in log messages so an operator scanning
            the Error Log can tell which caller produced a given failure
            (e.g. "activate_bpr.main", "backfill.main").

    Returns dict (always — never raises):
        chunks_attempted    int — chunks the loop actually sent to Glific
        chunks_failed       int — chunks where add_contacts_to_group_bulk
                                  returned False or raised
        added               int — total contacts in the successful chunks
        errors              list[str] — capped at 50 sample messages
        circuit_tripped     bool — True if the loop aborted on consecutive
                                   failures
        skipped_after_trip  int — contacts in chunks not attempted because
                                  the breaker tripped (0 otherwise)
    """
    from tap_lms.summer_program.constants import COLLECTION_BATCH_SIZE

    summary = {
        "chunks_attempted": 0,
        "chunks_failed": 0,
        "added": 0,
        "errors": [],
        "circuit_tripped": False,
        "skipped_after_trip": 0,
    }

    if not contact_ids:
        return summary

    if not group_id:
        summary["errors"].append(
            f"{op_label}: missing group_id; no contacts added"
        )
        return summary

    if chunk_size is None:
        chunk_size = COLLECTION_BATCH_SIZE

    total = len(contact_ids)
    consecutive_failures = 0

    for start in range(0, total, chunk_size):
        chunk = contact_ids[start:start + chunk_size]

        if summary["circuit_tripped"]:
            summary["skipped_after_trip"] += len(chunk)
            continue

        summary["chunks_attempted"] += 1
        chunk_ok = False
        try:
            chunk_ok = bool(add_contacts_to_group_bulk(chunk, group_id))
        except Exception as e:
            # add_contacts_to_group_bulk catches its own exceptions and
            # returns False; this except is defense-in-depth for any future
            # refactor that lets one escape. Log loudly + treat as failure.
            try:
                frappe.log_error(
                    message=(
                        f"{op_label}: group={group_id} chunk_start={start} "
                        f"chunk_size={len(chunk)}: unexpected exception {e}"
                    ),
                    title="SP bulk_add_with_circuit_breaker Error",
                )
            except Exception:
                frappe.logger().error(
                    f"{op_label}: chunk@{start} unexpected exception "
                    f"(double-fault): {e}"
                )

        if chunk_ok:
            summary["added"] += len(chunk)
            consecutive_failures = 0
            frappe.logger().info(
                f"{op_label}: chunk@{start}/{total} OK (+{len(chunk)} added, "
                f"total_added={summary['added']}, group={group_id})"
            )
        else:
            summary["chunks_failed"] += 1
            consecutive_failures += 1
            if len(summary["errors"]) < 50:
                summary["errors"].append(
                    f"{op_label}: group={group_id} chunk_start={start} "
                    f"chunk_size={len(chunk)} failed"
                )
            frappe.logger().info(
                f"{op_label}: chunk@{start}/{total} FAILED "
                f"(consecutive_failures={consecutive_failures}/"
                f"{max_consecutive_failures}, group={group_id})"
            )
            if consecutive_failures >= max_consecutive_failures:
                summary["circuit_tripped"] = True
                try:
                    frappe.log_error(
                        message=(
                            f"{op_label}: circuit breaker tripped after "
                            f"{consecutive_failures} consecutive chunk failures; "
                            f"group={group_id} attempted={summary['chunks_attempted']} "
                            f"added={summary['added']} (remaining chunks will be "
                            f"counted as skipped_after_trip)"
                        ),
                        title="SP bulk_add_with_circuit_breaker Circuit Tripped",
                    )
                except Exception:
                    frappe.logger().error(
                        f"{op_label}: circuit tripped log failed (double-fault)"
                    )

    frappe.logger().info(
        f"{op_label} done: group={group_id} total={total} "
        f"chunks_attempted={summary['chunks_attempted']} "
        f"chunks_failed={summary['chunks_failed']} "
        f"added={summary['added']} circuit_tripped={summary['circuit_tripped']} "
        f"skipped_after_trip={summary['skipped_after_trip']}"
    )
    return summary


def bulk_remove_from_group_with_circuit_breaker(
    contact_ids,
    group_id,
    *,
    chunk_size=None,
    max_consecutive_failures=3,
    op_label="bulk_remove",
):
    """Chunked bulk-remove from a Glific group with progress log + circuit breaker.

    Mirror of `bulk_add_to_group_with_circuit_breaker` for the remove half
    (CR-029-followup / M-1 — completes the L-074 anti-drift refactor that
    started in CR-029). Shared by:
      - migrations/sweep_migration._bulk_move_to_escalation (Phase B1
        — remove from main before adding to escalation)
      - any future caller that needs a defended remove

    Behavior identical to the add variant: per-chunk try/except, consecutive-
    failure circuit breaker (default 3), zero-summary on empty input or
    missing group_id, never raises.

    Returns dict (always — never raises):
        chunks_attempted    int
        chunks_failed       int
        removed             int — total contacts in the successful chunks
        errors              list[str] — capped at 50 sample messages
        circuit_tripped     bool
        skipped_after_trip  int
    """
    from tap_lms.summer_program.constants import COLLECTION_BATCH_SIZE

    summary = {
        "chunks_attempted": 0,
        "chunks_failed": 0,
        "removed": 0,
        "errors": [],
        "circuit_tripped": False,
        "skipped_after_trip": 0,
    }

    if not contact_ids:
        return summary

    if not group_id:
        summary["errors"].append(
            f"{op_label}: missing group_id; no contacts removed"
        )
        return summary

    if chunk_size is None:
        chunk_size = COLLECTION_BATCH_SIZE

    total = len(contact_ids)
    consecutive_failures = 0

    for start in range(0, total, chunk_size):
        chunk = contact_ids[start:start + chunk_size]

        if summary["circuit_tripped"]:
            summary["skipped_after_trip"] += len(chunk)
            continue

        summary["chunks_attempted"] += 1
        chunk_ok = False
        try:
            chunk_ok = bool(remove_contacts_from_group_bulk(chunk, group_id))
        except Exception as e:
            try:
                frappe.log_error(
                    message=(
                        f"{op_label}: group={group_id} chunk_start={start} "
                        f"chunk_size={len(chunk)}: unexpected exception {e}"
                    ),
                    title="SP bulk_remove_with_circuit_breaker Error",
                )
            except Exception:
                frappe.logger().error(
                    f"{op_label}: chunk@{start} unexpected exception "
                    f"(double-fault): {e}"
                )

        if chunk_ok:
            summary["removed"] += len(chunk)
            consecutive_failures = 0
            frappe.logger().info(
                f"{op_label}: chunk@{start}/{total} OK (-{len(chunk)} removed, "
                f"total_removed={summary['removed']}, group={group_id})"
            )
        else:
            summary["chunks_failed"] += 1
            consecutive_failures += 1
            if len(summary["errors"]) < 50:
                summary["errors"].append(
                    f"{op_label}: group={group_id} chunk_start={start} "
                    f"chunk_size={len(chunk)} failed"
                )
            frappe.logger().info(
                f"{op_label}: chunk@{start}/{total} FAILED "
                f"(consecutive_failures={consecutive_failures}/"
                f"{max_consecutive_failures}, group={group_id})"
            )
            if consecutive_failures >= max_consecutive_failures:
                summary["circuit_tripped"] = True
                try:
                    frappe.log_error(
                        message=(
                            f"{op_label}: circuit breaker tripped after "
                            f"{consecutive_failures} consecutive chunk failures; "
                            f"group={group_id} attempted={summary['chunks_attempted']} "
                            f"removed={summary['removed']} (remaining chunks will be "
                            f"counted as skipped_after_trip)"
                        ),
                        title="SP bulk_remove_with_circuit_breaker Circuit Tripped",
                    )
                except Exception:
                    frappe.logger().error(
                        f"{op_label}: circuit tripped log failed (double-fault)"
                    )

    frappe.logger().info(
        f"{op_label} done: group={group_id} total={total} "
        f"chunks_attempted={summary['chunks_attempted']} "
        f"chunks_failed={summary['chunks_failed']} "
        f"removed={summary['removed']} circuit_tripped={summary['circuit_tripped']} "
        f"skipped_after_trip={summary['skipped_after_trip']}"
    )
    return summary
