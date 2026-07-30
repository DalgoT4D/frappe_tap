from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Sequence

import frappe
from google.cloud import storage
from google.oauth2 import service_account


GCP_CREDENTIALS_PROJECT_ID = "rubrics-data-migration"
FAILED_ROWS_GCP_PROJECT_ID = "axiomatic-treat-417617"
GLIFIC_CONTACTS_FOLDER = "Glific_contacts"
GLIFIC_CSV_HEADERS = [
    "name",
    "phone",
    "language",
    "delete",
    "school_id",
    "state",
    "model",
    "buddy_name",
    "batch_id",
    "grade",
    "level",
    "course",
]

GOOGLE_DRIVE_READONLY_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
)
GOOGLE_SHEETS_READWRITE_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
)


def get_gcs_settings(project_id: str):
    settings_name = frappe.db.get_value(
        "GCS Settings",
        {"project_id": project_id},
        "name",
    )
    if not settings_name:
        frappe.throw(f"GCS Settings not found for project_id '{project_id}'")
    return frappe.get_doc("GCS Settings", settings_name)


def get_google_service_account_credentials(
    project_id: str = GCP_CREDENTIALS_PROJECT_ID,
    scopes: Sequence[str] | None = None,
):
    settings = get_gcs_settings(project_id)
    credentials_dict = json.loads(settings.credentials_json)
    return service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=list(scopes or GOOGLE_DRIVE_READONLY_SCOPES),
    )


def get_google_service_account_email(project_id: str = GCP_CREDENTIALS_PROJECT_ID) -> str:
    settings_name = frappe.db.get_value(
        "GCS Settings",
        {"project_id": project_id},
        "name",
    )
    if not settings_name:
        return ""

    settings = frappe.get_doc("GCS Settings", settings_name)
    try:
        credentials_dict = json.loads(settings.credentials_json)
    except Exception:
        return ""
    return str(credentials_dict.get("client_email") or "").strip()


def upload_bytes_to_gcs(
    content: bytes,
    object_name: str,
    project_id: str,
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
) -> str:
    settings = get_gcs_settings(project_id)
    credentials_dict = json.loads(settings.credentials_json)
    client = storage.Client.from_service_account_info(credentials_dict)
    bucket = client.bucket(settings.bucket_name)
    blob = bucket.blob(object_name)
    blob.upload_from_string(content, content_type=content_type)
    return f"https://storage.cloud.google.com/{settings.bucket_name}/{object_name}"


def upload_glific_contact_csv(
    file_name: str,
    rows: list[dict],
    project_id: str = FAILED_ROWS_GCP_PROJECT_ID,
) -> str:
    return upload_bytes_to_gcs(
        render_glific_contact_csv(rows),
        f"{GLIFIC_CONTACTS_FOLDER}/{file_name}",
        project_id,
        content_type="text/csv",
    )


def render_glific_contact_csv(rows: list[dict]) -> bytes:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=GLIFIC_CSV_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
