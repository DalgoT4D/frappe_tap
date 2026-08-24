"""Pull image URLs from BigQuery and attach them to Teacher Submissions.

Glific writes every inbound WhatsApp message into BigQuery. The images a teacher
sends land there as `gcs_url` rows. Frappe has the submission record. Nothing
links the two directly, so this job matches them on phone + time order.

Why time order and not equality:

    10:29:01   she sends the last photo   -> last_media_at   (BigQuery)
    10:29:30   the webhook fires          -> submitted_at    (Frappe)

The webhook is always LATER, so we look forward from the last media message and
take the first submission that follows it. See images.find_submission().

Run manually:
    bench --site tap_lms.localhost execute \
        tap_lms.teacher_bot.bigquery_sync.sync_submission_images

Dry run (queries, matches, writes nothing):
    bench --site tap_lms.localhost execute \
        tap_lms.teacher_bot.bigquery_sync.sync_submission_images \
        --kwargs "{'dry_run': True}"
"""

import json

import frappe
from frappe.utils import get_datetime, now_datetime

from tap_lms.teacher_bot.images import attach_images, find_submission

SETTINGS_DOCTYPE = "BigQuery Settings"

# Grouping explained:
#   Glific labels the message that starts a flow step (flow_label). Every media
#   message after it belongs to that step. A running count of labelled messages
#   therefore numbers the submissions per contact.
#
#   The anchoring window runs over ALL inbound messages — the label must be
#   counted before non-media rows are dropped, or the numbering silently drifts.
#   LAST_VALUE(... IGNORE NULLS) carries the label onto the media rows so it
#   survives the filter.
QUERY_TEMPLATE = """
WITH msgs AS (
  SELECT
    m.id AS message_id,
    m.contact_phone,
    m.contact_name,
    m.flow_label,
    m.inserted_at,
    mm.gcs_url
  FROM `{messages_table}` AS m
  LEFT JOIN `{media_table}` AS mm
    ON m.media_id = mm.id
  WHERE m.flow = 'inbound'
    AND m.inserted_at >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL @lookback_days DAY)
),

anchored AS (
  SELECT
    *,
    COUNTIF(flow_label IS NOT NULL) OVER w AS submission_no,
    LAST_VALUE(flow_label IGNORE NULLS) OVER w AS submission_label
  FROM msgs
  WINDOW w AS (
    PARTITION BY contact_phone
    ORDER BY inserted_at, message_id
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  )
),

submissions AS (
  SELECT
    CONCAT(contact_phone, '-', CAST(submission_no AS STRING)) AS source_row_id,
    contact_phone,
    ANY_VALUE(contact_name)     AS contact_name,
    ANY_VALUE(submission_label) AS submission_label,
    MIN(inserted_at)            AS first_media_at,
    MAX(inserted_at)            AS last_media_at,
    COUNT(DISTINCT gcs_url)     AS media_count,
    ARRAY_AGG(DISTINCT gcs_url ORDER BY gcs_url) AS gcs_urls
  FROM anchored
  WHERE gcs_url IS NOT NULL
  GROUP BY contact_phone, submission_no
)

SELECT *
FROM submissions
WHERE submission_label LIKE @flow_label_filter
ORDER BY last_media_at DESC
LIMIT @batch_limit
"""


# ---------------------------------------------------------------------------
# settings and client
# ---------------------------------------------------------------------------

def get_settings():
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    return frappe._dict({
        "enabled": int(settings.enabled or 0),
        "project_id": (settings.project_id or "").strip(),
        "messages_table": (settings.messages_table or "").strip(),
        "media_table": (settings.media_table or "").strip(),
        "credentials_json": settings.get_password("credentials_json", raise_exception=False),
        "lookback_days": int(settings.lookback_days or 2),
        "match_window_minutes": int(settings.match_window_minutes or 5),
        "batch_limit": int(settings.batch_limit or 200),
        "source_timezone": (settings.source_timezone or "UTC").strip(),
        "flow_label_filter": (settings.flow_label_filter or "%image_capture%").strip(),
    })


def get_client(settings=None):
    """Build a BigQuery client from the credentials in Settings."""
    from google.cloud import bigquery
    from google.oauth2 import service_account

    settings = settings or get_settings()

    if not settings.credentials_json:
        raise ValueError("BigQuery Settings has no credentials_json")
    if not settings.project_id:
        raise ValueError("BigQuery Settings has no project_id")

    info = json.loads(settings.credentials_json)
    credentials = service_account.Credentials.from_service_account_info(info)
    return bigquery.Client(project=settings.project_id, credentials=credentials)


def test_connection():
    """Prove the credentials work before debugging anything else.

    bench --site tap_lms.localhost execute \
        tap_lms.teacher_bot.bigquery_sync.test_connection
    """
    settings = get_settings()
    client = get_client(settings)
    rows = list(client.query(
        f"SELECT COUNT(*) AS n FROM `{settings.messages_table}` "
        f"WHERE inserted_at >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 1 DAY)"
    ).result())
    count = rows[0]["n"] if rows else 0
    print(f"OK — {count} inbound messages in the last day")
    return count


# ---------------------------------------------------------------------------
# timezone
# ---------------------------------------------------------------------------

def to_system_time(value, source_timezone="UTC"):
    """Convert a BigQuery DATETIME into Frappe's system timezone.

    BigQuery DATETIME carries no timezone, so the source zone has to be stated
    in Settings. Frappe stores naive local time. Get this wrong and every match
    fails while the code looks perfectly correct — it is the first thing to
    check if everything comes back not_found.
    """
    if value is None:
        return None

    naive = get_datetime(value)

    try:
        from zoneinfo import ZoneInfo

        system_timezone = frappe.utils.get_time_zone() or "UTC"
        if source_timezone == system_timezone:
            return naive

        aware = naive.replace(tzinfo=ZoneInfo(source_timezone))
        return aware.astimezone(ZoneInfo(system_timezone)).replace(tzinfo=None)
    except Exception:
        frappe.log_error(
            f"Timezone conversion failed: {source_timezone} -> system. "
            f"Using the raw value.\n\n{frappe.get_traceback()}",
            "bigquery_sync timezone",
        )
        return naive


# ---------------------------------------------------------------------------
# the job
# ---------------------------------------------------------------------------

def fetch_rows(settings=None, client=None):
    """Run the grouping query and return the submission-level rows."""
    from google.cloud import bigquery

    settings = settings or get_settings()
    client = client or get_client(settings)

    query = QUERY_TEMPLATE.format(
        messages_table=settings.messages_table,
        media_table=settings.media_table,
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("lookback_days", "INT64", settings.lookback_days),
            bigquery.ScalarQueryParameter("flow_label_filter", "STRING", settings.flow_label_filter),
            bigquery.ScalarQueryParameter("batch_limit", "INT64", settings.batch_limit),
        ]
    )
    return list(client.query(query, job_config=job_config).result())


def sync_submission_images(dry_run=False, lookback_days=None):
    """Match BigQuery media rows to submissions and attach the URLs.

    Safe to run as often as you like: a source_row_id already attached is
    skipped, so nothing is ever duplicated.
    """
    summary = frappe._dict(
        rows=0, matched=0, attached=0, skipped=0,
        not_found=0, ambiguous=0, errors=0,
    )

    settings = get_settings()
    if not settings.enabled:
        return dict(summary, message="BigQuery Settings is not enabled")

    if lookback_days:
        settings.lookback_days = int(lookback_days)

    try:
        rows = fetch_rows(settings)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "bigquery_sync: query failed")
        summary.errors += 1
        return dict(summary, message="Query failed — see Error Log")

    summary.rows = len(rows)

    for row in rows:
        try:
            phone = str(row["contact_phone"] or "").strip()
            source_row_id = str(row["source_row_id"] or "").strip()
            urls = [url for url in (row["gcs_urls"] or []) if url]
            if not phone or not urls:
                continue

            # Match forward from the LAST media message: the webhook fires
            # after she finishes sending, so a long burst does not widen the gap.
            last_media_at = to_system_time(row["last_media_at"], settings.source_timezone)

            submission_name, status, candidates = find_submission(
                phone, last_media_at, settings.match_window_minutes
            )

            if status == "not_found":
                summary.not_found += 1
                continue

            if status == "ambiguous":
                summary.ambiguous += 1
                if not dry_run:
                    for candidate in candidates[:2]:
                        frappe.db.set_value(
                            "Teacher Submission", candidate.name,
                            "media_match_status", "Ambiguous",
                        )
                    frappe.db.commit()
                continue

            summary.matched += 1
            if dry_run:
                continue

            image_rows = [
                {
                    "file_url": url,
                    "media_type": "image",
                    "source_time": last_media_at,
                    "source_row_id": source_row_id,
                }
                for url in urls
            ]

            doc = frappe.get_doc("Teacher Submission", submission_name)
            added, skipped, _capped = attach_images(doc, image_rows)
            doc.save(ignore_permissions=True)
            frappe.db.commit()

            summary.attached += added
            summary.skipped += skipped
        except Exception:
            frappe.db.rollback()
            summary.errors += 1
            frappe.log_error(
                f"row={row.get('source_row_id')}\n\n{frappe.get_traceback()}",
                "bigquery_sync: row failed",
            )

    if not dry_run:
        _record_run(summary)

    return dict(summary)


def _record_run(summary):
    """Write the counts back to Settings so a rising not_found is visible."""
    try:
        settings = frappe.get_single(SETTINGS_DOCTYPE)
        settings.last_run_at = now_datetime()
        settings.last_run_summary = (
            f"rows={summary.rows} matched={summary.matched} "
            f"attached={summary.attached} skipped={summary.skipped} "
            f"not_found={summary.not_found} ambiguous={summary.ambiguous} "
            f"errors={summary.errors}"
        )
        settings.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "bigquery_sync: could not record run")
