"""
BigQuery -> Frappe sync for Glific contact context.

Populates StudentGlificContext with fields set by the WhatsApp flow
in Glific. Reads credentials from the BigQuerySettings singleton doctype
(TAP LMS > BigQuerySettings) instead of site_config.

Two BigQuery queries per run regardless of student count:
  Query 1: contacts table - sp_submission_link, last_flow_incomplete, last_assignment_name
  Query 2: messages table - last inbound message per phone (ROW_NUMBER window)

Cost: approx Rs 4-7 per run at current Glific data volume.
Scheduled Mon-Sat 12:00 UTC (5:30 PM IST) via hooks.py.
"""

import json

import frappe
from frappe.utils import now_datetime


_MODULE = "Didi BigQuery Sync"


def sync_bigquery_glific_context():
    """Entry point called by the scheduler (hooks.py cron)."""
    settings = _get_bq_settings()
    if not settings or not settings.enabled:
        return

    project = (settings.project_id or "").strip()
    dataset = (settings.dataset_id or "").strip()
    if not project or not dataset:
        frappe.log_error(
            title=_MODULE,
            message="project_id or dataset_id not set in BigQuerySettings. Skipping sync.",
        )
        return

    try:
        client = _get_bq_client(project, settings.service_account_json)
    except Exception as exc:
        frappe.log_error(title=_MODULE, message=f"Failed to initialise BigQuery client: {exc}")
        return

    try:
        _run_sync(client, project, dataset)
    except Exception as exc:
        frappe.log_error(title=_MODULE, message=f"Sync job failed: {exc}")


def _get_bq_settings():
    try:
        return frappe.get_single("BigQuerySettings")
    except Exception:
        return None


def _get_bq_client(project, service_account_json):
    """Build a BigQuery client from credentials stored in BigQuerySettings."""
    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account
    except ImportError:
        raise RuntimeError(
            "google-cloud-bigquery not installed. "
            "Run: pip install google-cloud-bigquery google-auth --break-system-packages"
        )

    if not service_account_json:
        raise RuntimeError(
            "Service Account JSON is empty in BigQuerySettings. "
            "Go to TAP LMS > BigQuerySettings and paste the credentials JSON."
        )

    creds_dict = json.loads(service_account_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
    )
    return bigquery.Client(project=project, credentials=credentials)


def _run_sync(client, project, dataset):
    """Core sync logic. Two BigQuery queries then bulk Frappe upsert."""

    # Query 1: active/paused Summer contacts
    contacts_query = f"""
        SELECT
            phone,
            JSON_VALUE(raw_fields, '$.sp_submission_link.value') AS sp_submission_link,
            JSON_VALUE(raw_fields, '$.last_flow_incomplete.value') AS last_flow_incomplete,
            JSON_VALUE(raw_fields, '$.SP_assigment_id.value')    AS last_assignment_name
        FROM `{project}.{dataset}.contacts`
        WHERE JSON_VALUE(raw_fields, '$.program_type.value') = 'Summer'
          AND JSON_VALUE(raw_fields, '$.program_status.value') IN ('active', 'paused')
    """
    contacts_rows = list(client.query(contacts_query).result())

    # Query 2: last inbound message per phone (single window query)
    inbound_query = f"""
        SELECT contact_phone, body AS last_inbound_message, inserted_at AS last_inbound_at
        FROM (
            SELECT contact_phone, body, inserted_at,
                   ROW_NUMBER() OVER (PARTITION BY contact_phone ORDER BY inserted_at DESC) AS rn
            FROM `{project}.{dataset}.messages`
            WHERE flow = 'inbound' AND body IS NOT NULL AND body != ''
        )
        WHERE rn = 1
    """
    inbound_by_phone = {
        row["contact_phone"]: row
        for row in client.query(inbound_query).result()
    }

    # Build phone -> Student name lookup from Frappe (single query)
    frappe_phones = frappe.db.get_all(
        "Student",
        fields=["name", "phone"],
        filters={"phone": ("is", "set")},
    )
    student_by_phone = {row.phone: row.name for row in frappe_phones}

    synced = 0
    skipped = 0
    errors = 0
    now = now_datetime()

    for row in contacts_rows:
        bq_phone = (row["phone"] or "").strip()
        # Strip 91 country code prefix
        clean_phone = bq_phone[2:] if bq_phone.startswith("91") and len(bq_phone) > 10 else bq_phone

        student_name = student_by_phone.get(clean_phone)
        if not student_name:
            skipped += 1
            continue

        inbound = inbound_by_phone.get(bq_phone, {})

        context_data = {
            "student":              student_name,
            "last_synced_at":       now,
            "sp_submission_link":   _clean(row.get("sp_submission_link")),
            "last_flow_incomplete": 1 if (row.get("last_flow_incomplete") or "").lower() == "true" else 0,
            "last_assignment_name": _clean(row.get("last_assignment_name")),
            "last_inbound_message": _clean(inbound.get("last_inbound_message")),
            "last_inbound_at":      inbound.get("last_inbound_at"),
        }

        try:
            existing = frappe.db.get_value(
                "StudentGlificContext", {"student": student_name}, "name"
            )
            if existing:
                frappe.db.set_value(
                    "StudentGlificContext", existing, context_data, update_modified=False
                )
            else:
                doc = frappe.new_doc("StudentGlificContext")
                doc.update(context_data)
                doc.insert(ignore_permissions=True)
            synced += 1
        except Exception as exc:
            errors += 1
            if errors <= 5:
                frappe.log_error(
                    title=f"{_MODULE} per-student error",
                    message=f"Student {student_name} (phone {clean_phone}): {exc}",
                )

    frappe.db.commit()

    msg = (
        f"Contacts from BigQuery: {len(contacts_rows)}. "
        f"Matched: {synced}. No Frappe match: {skipped}. Errors: {errors}."
    )
    if errors:
        frappe.log_error(title=f"{_MODULE} completed with errors", message=msg)
    else:
        frappe.logger().info(f"{_MODULE}: {msg}")


def _clean(val):
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None
