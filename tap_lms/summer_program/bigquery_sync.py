"""
BigQuery -> Frappe sync for Glific contact context.

Populates StudentGlificContext with fields set by the WhatsApp flow
in Glific that do not sync back to Frappe automatically. These are:
  - sp_submission_link  : what the student said when they clicked the
                          problem button in the WhatsApp flow
  - last_flow_incomplete: whether the flow broke mid-way
  - last_assignment_name: SP_assigment_id from Glific contact fields
  - last_inbound_message: body of the student's most recent inbound message
  - last_inbound_at     : timestamp of that message

BigQuery is queried once per run (batch, not per-student). Frappe is
written via frappe.db.set_value / frappe.get_doc().insert() for upsert.

Wired into hooks.py scheduler_events via sync_bigquery_glific_context().
Cadence set by VoiceAgentSettings.glific_sync_interval_hours (default 6h).

L-NNN notes
-----------
L-BQ-001: BigQuery client credentials must be in site_config as
          'bigquery_credentials_json' (a JSON string of the service account).
          Do NOT hardcode credentials. Read at runtime via frappe.conf.
L-BQ-002: Phone matching strips the leading 91 from BigQuery contact_phone
          (which stores numbers as 91XXXXXXXXXX) before matching to
          Student.phone (which stores without country code).
L-BQ-003: raw_glific_fields is stored as JSON snapshot for debugging. It is
          never read in production code — only the extracted fields above.
L-BQ-004: This job runs with ignore_permissions=True on inserts because it
          runs as the scheduler user, not a logged-in admin.
"""

import json

import frappe
from frappe.utils import now_datetime


_MODULE = "Didi BigQuery Sync"


def sync_bigquery_glific_context():
    """Entry point called by the scheduler (hooks.py cron).

    Guards:
    - VoiceAgentSettings.glific_sync_enabled must be 1.
    - BigQuery credentials must be configured in site_config.
    - Only runs for Summer program active/paused students.
    """
    settings = _get_settings()
    if not settings or not settings.glific_sync_enabled:
        return

    project = (settings.glific_bq_project or "").strip()
    dataset = (settings.glific_bq_dataset or "").strip()
    if not project or not dataset:
        frappe.log_error(
            title=_MODULE,
            message="glific_bq_project or glific_bq_dataset not set in VoiceAgentSettings. Skipping sync.",
        )
        return

    try:
        client = _get_bq_client(project)
    except Exception as exc:
        frappe.log_error(title=_MODULE, message=f"Failed to initialise BigQuery client: {exc}")
        return

    try:
        _run_sync(client, project, dataset)
    except Exception as exc:
        frappe.log_error(title=_MODULE, message=f"Sync job failed: {exc}")


def _get_settings():
    try:
        return frappe.get_single("VoiceAgentSettings")
    except Exception:
        return None


def _get_bq_client(project):
    """Build a BigQuery client from service account credentials in site_config.

    Credentials must be set in site_config.json as:
        "bigquery_credentials_json": "{...service account JSON as a string...}"
    """
    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account
    except ImportError:
        raise RuntimeError(
            "google-cloud-bigquery not installed. "
            "Run: pip install google-cloud-bigquery google-auth"
        )

    creds_json = frappe.conf.get("bigquery_credentials_json")
    if not creds_json:
        raise RuntimeError(
            "bigquery_credentials_json not found in site_config. "
            "Add the service account JSON string to site_config.json. (L-BQ-001)"
        )

    creds_dict = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
    )
    return bigquery.Client(project=project, credentials=credentials)


def _run_sync(client, project, dataset):
    """Core sync logic. Runs one BigQuery query then upserts Frappe records."""

    # ── Step 1: fetch contact fields for active/paused Summer students ──
    contacts_query = f"""
        SELECT
            phone,
            JSON_VALUE(raw_fields, '$.sp_submission_link.value') AS sp_submission_link,
            JSON_VALUE(raw_fields, '$.last_flow_incomplete.value') AS last_flow_incomplete,
            JSON_VALUE(raw_fields, '$.SP_assigment_id.value')    AS last_assignment_name,
            raw_fields
        FROM `{project}.{dataset}.contacts`
        WHERE JSON_VALUE(raw_fields, '$.program_type.value') = 'Summer'
          AND JSON_VALUE(raw_fields, '$.program_status.value') IN ('active', 'paused')
    """
    contacts_rows = list(client.query(contacts_query).result())

    # ── Step 2: fetch last inbound message per phone ────────────────────
    # One query for all active phones instead of N per-student queries.
    inbound_query = f"""
        SELECT
            contact_phone,
            body             AS last_inbound_message,
            inserted_at      AS last_inbound_at
        FROM (
            SELECT
                contact_phone,
                body,
                inserted_at,
                ROW_NUMBER() OVER (PARTITION BY contact_phone ORDER BY inserted_at DESC) AS rn
            FROM `{project}.{dataset}.messages`
            WHERE flow = 'inbound'
              AND body IS NOT NULL
              AND body != ''
        )
        WHERE rn = 1
    """
    inbound_by_phone = {
        row["contact_phone"]: row
        for row in client.query(inbound_query).result()
    }

    # ── Step 3: build phone -> Student lookup from Frappe ───────────────
    # Strip leading 91 from BigQuery phone (L-BQ-002).
    frappe_phones = frappe.db.get_all(
        "Student",
        fields=["name", "phone"],
        filters={"phone": ("is", "set")},
    )
    student_by_phone = {row.phone: row.name for row in frappe_phones}

    synced = 0
    skipped = 0
    errors = 0

    # ── Step 4: upsert StudentGlificContext per matched student ─────────
    now = now_datetime()
    for row in contacts_rows:
        bq_phone = (row["phone"] or "").strip()

        # Strip 91 country code prefix (L-BQ-002)
        clean_phone = bq_phone.lstrip("91") if bq_phone.startswith("91") and len(bq_phone) > 10 else bq_phone

        student_name = student_by_phone.get(clean_phone)
        if not student_name:
            skipped += 1
            continue

        inbound = inbound_by_phone.get(bq_phone, {})

        context_data = {
            "student": student_name,
            "last_synced_at": now,
            "sp_submission_link": _clean_text(row.get("sp_submission_link")),
            "last_flow_incomplete": 1 if (row.get("last_flow_incomplete") or "").lower() == "true" else 0,
            "last_assignment_name": _clean_text(row.get("last_assignment_name")),
            "last_inbound_message": _clean_text(inbound.get("last_inbound_message")),
            "last_inbound_at": inbound.get("last_inbound_at"),
            "raw_glific_fields": row.get("raw_fields"),
        }

        try:
            existing = frappe.db.get_value("StudentGlificContext", {"student": student_name}, "name")
            if existing:
                frappe.db.set_value("StudentGlificContext", existing, context_data, update_modified=False)
            else:
                doc = frappe.new_doc("StudentGlificContext")
                doc.update(context_data)
                doc.insert(ignore_permissions=True)
            synced += 1
        except Exception as exc:
            errors += 1
            if errors <= 5:
                frappe.log_error(
                    title=f"{_MODULE} — per-student error",
                    message=f"Student {student_name} (phone {clean_phone}): {exc}",
                )

    frappe.db.commit()

    msg = (
        f"Contacts from BigQuery: {len(contacts_rows)}. "
        f"Matched to Frappe students: {synced}. "
        f"No Frappe match: {skipped}. "
        f"Errors: {errors}."
    )
    if errors:
        frappe.log_error(title=f"{_MODULE} — completed with errors", message=msg)
    else:
        frappe.logger().info(f"{_MODULE}: {msg}")


def _clean_text(val):
    """Return None for empty/null values so Frappe stores null not empty string."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None
