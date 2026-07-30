# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from tap_lms.onboarding.student_sheet_registration import (
    prepare_student_sheet_registration,
    upload_prepared_student_sheet_registration,
)


class StudentSheetRegistrationJob(Document):
    pass


def _set_glific_contact_files(docname: str, files: list[dict]) -> None:
    doc = frappe.get_doc("Student Sheet Registration Job", docname)
    doc.set("glific_contact_files", [])
    for file_row in files:
        doc.append("glific_contact_files", {
            "file_name": str(file_row.get("file_name") or ""),
            "file_path": str(file_row.get("file_path") or ""),
            "row_count": int(file_row.get("row_count") or 0),
        })
    doc.save(ignore_permissions=True)


def _append_log(docname: str, message: str) -> None:
    timestamp = frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M:%S")
    existing = frappe.db.get_value("Student Sheet Registration Job", docname, "processing_log")
    if isinstance(existing, str) and existing.strip():
        try:
            existing = json.loads(existing)
        except Exception:
            existing = {"entries": [{"timestamp": None, "message": existing}]}

    if isinstance(existing, dict):
        raw_entries = existing.get("entries")
        log_entries = raw_entries if isinstance(raw_entries, list) else []
    elif isinstance(existing, list):
        log_entries = existing
    else:
        log_entries = []

    log_entries.append({
        "timestamp": timestamp,
        "message": message,
    })
    frappe.db.set_value(
        "Student Sheet Registration Job",
        docname,
        "processing_log",
        json.dumps({"entries": log_entries}, ensure_ascii=True),
        update_modified=False,
    )


def _set_job_state(docname: str, **updates) -> None:
    for key in ("processing_log", "summary_json", "prepared_rows_json"):
        if key in updates and not isinstance(updates[key], str):
            updates[key] = json.dumps(updates[key], ensure_ascii=True)
    frappe.db.set_value("Student Sheet Registration Job", docname, updates, update_modified=False)


def _set_summary_counts(docname: str, summary: dict) -> None:
    count_fields = (
        "raw_rows",
        "prepared_rows",
        "uploaded_rows",
        "failed_rows",
        "duplicate_rows",
        "skipped_done_rows",
    )
    updates = {
        fieldname: int(summary.get(fieldname) or 0)
        for fieldname in count_fields
        if fieldname in summary
    }
    if "prepared_file_url" in summary:
        updates["prepared_file_url"] = str(summary.get("prepared_file_url") or "")
    if "failed_rows_file_url" in summary:
        updates["failed_rows_file_url"] = str(summary.get("failed_rows_file_url") or "")
    frappe.db.set_value("Student Sheet Registration Job", docname, updates, update_modified=False)


@frappe.whitelist()
def start_prepare_student_sheet_registration_job(docname: str) -> dict:
    doc = frappe.get_doc("Student Sheet Registration Job", docname)
    if doc.status in {"Preparing", "Uploading"}:
        frappe.throw("This student sheet registration job is already running.")

    _set_job_state(
        docname,
        status="Preparing",
        started_at=None,
        completed_at=None,
        raw_rows=0,
        prepared_rows=0,
        uploaded_rows=0,
        failed_rows=0,
        duplicate_rows=0,
        skipped_done_rows=0,
        prepared_file_url="",
        failed_rows_file_url="",
        summary_json="",
        last_error="",
        prepared_rows_json="[]",
        processing_log={"entries": []},
    )
    _set_glific_contact_files(docname, [])
    frappe.db.commit()

    job = frappe.enqueue(
        "tap_lms.tap_lms.doctype.student_sheet_registration_job."
        "student_sheet_registration_job.run_prepare_student_sheet_registration_job",
        queue="long",
        timeout=7200,
        job_name=f"student_sheet_registration_prepare_{docname}",
        docname=docname,
    )
    return {"job_id": job.id, "status": "Preparing"}


@frappe.whitelist()
def start_upload_student_sheet_registration_job(docname: str) -> dict:
    doc = frappe.get_doc("Student Sheet Registration Job", docname)
    if doc.status in {"Preparing", "Uploading"}:
        frappe.throw("This student sheet registration job is already running.")
    if not doc.prepared_rows_json:
        frappe.throw("Prepare Data must be run before Complete Upload.")

    _set_job_state(
        docname,
        status="Uploading",
        started_at=frappe.utils.now_datetime(),
        completed_at=None,
        uploaded_rows=0,
        failed_rows=0,
        failed_rows_file_url="",
        summary_json="",
        last_error="",
    )
    _set_glific_contact_files(docname, [])
    frappe.db.commit()

    job = frappe.enqueue(
        "tap_lms.tap_lms.doctype.student_sheet_registration_job."
        "student_sheet_registration_job.run_upload_student_sheet_registration_job",
        queue="long",
        timeout=7200,
        job_name=f"student_sheet_registration_upload_{docname}",
        docname=docname,
    )
    return {"job_id": job.id, "status": "Uploading"}


def run_prepare_student_sheet_registration_job(docname: str) -> dict:
    started_at = frappe.utils.now_datetime()
    try:
        _set_job_state(
            docname,
            status="Preparing",
            started_at=started_at,
            completed_at=None,
            last_error="",
        )
        frappe.db.commit()

        result = prepare_student_sheet_registration(log_fn=lambda message: _append_log(docname, message))
        summary = result.get("summary") or {}
        _set_summary_counts(docname, summary)
        _set_job_state(
            docname,
            status="Prepared",
            completed_at=frappe.utils.now_datetime(),
            summary_json=json.dumps(summary, indent=2, sort_keys=True),
            prepared_rows_json=json.dumps(result.get("prepared_rows") or [], ensure_ascii=True),
            last_error="",
        )
        frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback()
        error_message = frappe.get_traceback()
        _append_log(docname, f"[student-sheet-registration] prepare_failed\n{error_message}")
        _set_job_state(
            docname,
            status="Failed",
            completed_at=frappe.utils.now_datetime(),
            last_error=error_message,
        )
        frappe.db.commit()
        raise


def run_upload_student_sheet_registration_job(docname: str) -> dict:
    doc = frappe.get_doc("Student Sheet Registration Job", docname)
    started_at = frappe.utils.now_datetime()
    try:
        _set_job_state(
            docname,
            status="Uploading",
            started_at=started_at,
            completed_at=None,
            last_error="",
        )
        frappe.db.commit()

        prepared_rows = json.loads(doc.prepared_rows_json or "[]")
        if not isinstance(prepared_rows, list):
            raise ValueError("prepared_rows_json must be a JSON array")

        result = upload_prepared_student_sheet_registration(
            prepared_rows,
            import_user=doc.owner or "Administrator",
            log_fn=lambda message: _append_log(docname, message),
        )
        _set_summary_counts(docname, result)
        _set_glific_contact_files(docname, result.get("glific_contact_files") or [])
        _set_job_state(
            docname,
            status="Completed",
            completed_at=frappe.utils.now_datetime(),
            summary_json=json.dumps(result, indent=2, sort_keys=True),
            last_error="",
        )
        frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback()
        error_message = frappe.get_traceback()
        _append_log(docname, f"[student-sheet-registration] upload_failed\n{error_message}")
        _set_job_state(
            docname,
            status="Failed",
            completed_at=frappe.utils.now_datetime(),
            last_error=error_message,
        )
        frappe.db.commit()
        raise
