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
