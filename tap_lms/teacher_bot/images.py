"""Attaching image files to a Teacher Submission.

All image data lives on the Teacher Submission record itself — no child table:

    image_urls       one URL per line
    image_count      derived on save
    source_row_ids   source row identifiers already attached, one per line
    source_time      timestamp of the source row the images came from

`attach_images()` is the shared entry point. The webhook endpoint below uses it,
and the BigQuery matching job will use the same function — both need identical
rules: cap the count, skip duplicates, never reject a file for its type.
"""

import frappe
from frappe.utils import add_to_date, get_datetime

from tap_lms.onboarding.utils import (
    _get_request_data,
    _phone_filter,
    _phone_for_response,
    _require_valid_phone,
    _respond,
)
from tap_lms.utils.api_failures import log_api_failure


# A teacher may send a burst. 20 is the agreed ceiling for V0 —
# raise it here once storage sizing across all schools is confirmed.
MAX_IMAGES_PER_SUBMISSION = 20

# --- matching by phone + timestamp -----------------------------------------
#
# The form is submitted first, the webhook fires a few seconds later, so the
# source timestamp is ALWAYS earlier than the submission's submitted_at:
#
#     10:29:01   form submitted   -> source_time (BigQuery)
#     10:29:30   webhook fired    -> submitted_at (Frappe)
#
# So we look FORWARD from the source time, never backward. Looking backward
# could only ever steal the previous submission's files.
#
# How far forward to accept. Set from measured drift, not instinct —
# run an analysis query on real data before changing it.
MATCH_WINDOW_MINUTES = 5

# If a second candidate submission sits this close to the first, we cannot
# tell them apart. Flag for review rather than guess: a wrong photo on a
# wrong class is worse than a missing one, because nobody ever spots it.
AMBIGUITY_GAP_SECONDS = 90

_MEDIA_TYPES = {"image", "video", "audio", "document"}

_EXTENSION_MEDIA_TYPES = {
    "jpg": "image", "jpeg": "image", "png": "image", "webp": "image", "heic": "image", "gif": "image",
    "mp4": "video", "mov": "video", "3gp": "video", "avi": "video", "mkv": "video",
    "ogg": "audio", "opus": "audio", "mp3": "audio", "m4a": "audio", "aac": "audio", "wav": "audio",
    "pdf": "document", "doc": "document", "docx": "document", "ppt": "document", "pptx": "document",
    "xls": "document", "xlsx": "document", "txt": "document", "csv": "document",
}


def detect_media_type(url, declared_type=None):
    """Trust a declared type when given, else guess from the file extension.

    Anything unrecognised is reported as "unknown" — a file is never dropped
    for having an unexpected type.
    """
    declared_type = str(declared_type or "").strip().lower()
    if declared_type in _MEDIA_TYPES:
        return declared_type
    if declared_type in {"photo", "picture", "sticker", "img"}:
        return "image"
    if declared_type in {"voice", "voice_note", "audio_note"}:
        return "audio"
    if declared_type in {"file", "pdf", "doc"}:
        return "document"

    parts = str(url or "").split("?")[0].rsplit(".", 1)
    if len(parts) == 2:
        return _EXTENSION_MEDIA_TYPES.get(parts[1].strip().lower(), "unknown")

    return "unknown"


def normalize_images(raw):
    """Accept every shape a caller might send; return a clean list of dicts.

    Handles a list of dicts, a list of URL strings, one dict, one URL string,
    and a newline or comma separated string.
    """
    if not raw:
        return []

    if isinstance(raw, str):
        raw = [part.strip() for part in raw.replace(",", "\n").split("\n") if part.strip()]
    elif isinstance(raw, dict):
        raw = [raw]

    if not isinstance(raw, (list, tuple)):
        return []

    rows = []
    for item in raw:
        if isinstance(item, str):
            url, declared_type, source_time, row_id = item.strip(), None, None, None
        elif isinstance(item, dict):
            url = str(
                item.get("url")
                or item.get("file_url")
                or item.get("image_url")
                or item.get("link")
                or ""
            ).strip()
            declared_type = item.get("type") or item.get("media_type")
            source_time = item.get("source_time") or item.get("timestamp")
            row_id = item.get("source_row_id") or item.get("bq_row_id") or item.get("row_id")
        else:
            continue

        if not url:
            continue

        rows.append({
            "file_url": url,
            "media_type": detect_media_type(url, declared_type),
            "source_time": source_time,
            "source_row_id": str(row_id).strip() if row_id else None,
        })

    return rows


def _lines(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _safe_datetime(value):
    if not value:
        return None
    try:
        return get_datetime(value)
    except Exception:
        return None


def attach_images(doc, image_rows, mark_matched=True):
    """Append URLs to doc.image_urls, skipping anything already there.

    Duplicates are recognised by URL, or by source row id when one is given —
    the second matters for the BigQuery job, where the same source row must
    never be attached twice even if its URL changed.

    Returns (added, skipped, capped).
    """
    urls = _lines(doc.image_urls)
    row_ids = _lines(doc.source_row_ids)

    seen_urls = set(urls)
    seen_row_ids = set(row_ids)

    added = skipped = capped = 0
    earliest_source_time = _safe_datetime(doc.source_time)

    for image_row in image_rows:
        url = image_row["file_url"]
        row_id = image_row.get("source_row_id")

        if url in seen_urls or (row_id and row_id in seen_row_ids):
            skipped += 1
            continue

        if len(urls) >= MAX_IMAGES_PER_SUBMISSION:
            capped += 1
            continue

        urls.append(url)
        seen_urls.add(url)
        added += 1

        if row_id and row_id not in seen_row_ids:
            row_ids.append(row_id)
            seen_row_ids.add(row_id)

        source_time = _safe_datetime(image_row.get("source_time"))
        if source_time and (not earliest_source_time or source_time < earliest_source_time):
            earliest_source_time = source_time

    doc.image_urls = "\n".join(urls)
    doc.source_row_ids = "\n".join(row_ids) or None
    if earliest_source_time:
        doc.source_time = earliest_source_time
    if mark_matched and urls:
        doc.media_match_status = "Matched"

    return added, skipped, capped


def get_image_urls(doc_or_name):
    """Convenience reader: the URLs of a submission as a Python list."""
    if isinstance(doc_or_name, str):
        value = frappe.db.get_value("Teacher Submission", doc_or_name, "image_urls")
    else:
        value = doc_or_name.image_urls
    return _lines(value)


# ---------------------------------------------------------------------------
# Matching: phone + source timestamp -> the right submission
# ---------------------------------------------------------------------------

def find_submission(phone, source_time, window_minutes=None):
    """Find the submission these files belong to.

    The rule: for this phone, take the FIRST submission whose submitted_at is
    at or after `source_time`, within the window. Ordering, not proximity —
    which is why a 3 second delay and a 40 second delay both resolve correctly.

    Returns (submission_name, status, candidates) where status is one of
    "matched", "not_found" or "ambiguous".
    """
    window_minutes = window_minutes or MATCH_WINDOW_MINUTES
    window_end = add_to_date(source_time, minutes=window_minutes)

    phone_filter = _phone_filter("phone_number", phone) or {"phone_number": phone}
    filters = dict(phone_filter)
    filters["submitted_at"] = ["between", [source_time, window_end]]

    candidates = frappe.get_all(
        "Teacher Submission",
        filters=filters,
        fields=["name", "submitted_at"],
        order_by="submitted_at asc",
        limit=5,
    )

    if not candidates:
        return None, "not_found", []

    if len(candidates) > 1:
        gap = (
            get_datetime(candidates[1].submitted_at)
            - get_datetime(candidates[0].submitted_at)
        ).total_seconds()
        if gap <= AMBIGUITY_GAP_SECONDS:
            return None, "ambiguous", candidates

    return candidates[0].name, "matched", candidates


def _mark_status(submission_name, status):
    frappe.db.set_value("Teacher Submission", submission_name, "media_match_status", status)


# ---------------------------------------------------------------------------
# Endpoint — attach images using phone + source timestamp
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def save_images_by_phone():
    """Attach files to a submission identified by phone + source timestamp.

    For callers that do not know the submission_id — the BigQuery matching job,
    or any pipeline that only has the WhatsApp form's own phone and time.

    Expected body:
        phone           required
        source_time     required; the form-submit time from the source system
        images          required; list of URLs, or of objects
        window_minutes  optional; overrides MATCH_WINDOW_MINUTES for this call

    Always HTTP 200 when the input is valid. Read `match_status` to see what
    happened: matched / not_found / ambiguous. Nothing is attached unless the
    match is unambiguous.
    """
    data = _get_request_data()
    try:
        phone, phone_error = _require_valid_phone(data.get("phone"))
        if phone_error:
            _respond(phone_error["code"], phone_error["payload"])
            return

        raw_source_time = data.get("source_time") or data.get("timestamp")
        if not raw_source_time:
            _respond(400, {"status": "failure", "message": "source_time is required"})
            return

        source_time = _safe_datetime(raw_source_time)
        if not source_time:
            _respond(400, {
                "status": "failure",
                "message": f"Invalid source_time: {raw_source_time}",
            })
            return

        image_rows = normalize_images(data.get("images") or data.get("media"))
        if not image_rows:
            _respond(400, {
                "status": "failure",
                "message": "images is required and must contain at least one URL",
            })
            return

        window_minutes = data.get("window_minutes")
        try:
            window_minutes = int(window_minutes) if window_minutes else None
        except Exception:
            window_minutes = None

        submission_name, match_status, candidates = find_submission(
            phone, source_time, window_minutes
        )

        base = {
            "status": "success",
            "match_status": match_status,
            "phone": _phone_for_response(phone),
            "source_time": str(source_time),
            "window_minutes": window_minutes or MATCH_WINDOW_MINUTES,
        }

        if match_status == "not_found":
            _respond(200, {
                **base,
                "message": (
                    "No submission for this phone within the window. "
                    "The webhook may not have arrived yet — retry on the next run."
                ),
                "attached": 0,
            })
            return

        if match_status == "ambiguous":
            for candidate in candidates[:2]:
                _mark_status(candidate.name, "Ambiguous")
            frappe.db.commit()
            _respond(200, {
                **base,
                "message": "More than one submission fits; flagged for review, nothing attached.",
                "candidates": [candidate.name for candidate in candidates],
                "attached": 0,
            })
            return

        # Default the source time onto every row that did not carry its own,
        # so source_time on the record reflects the form, not the match.
        for image_row in image_rows:
            if not image_row.get("source_time"):
                image_row["source_time"] = source_time

        doc = frappe.get_doc("Teacher Submission", submission_name)
        added, skipped, capped = attach_images(doc, image_rows)
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        payload = {
            **base,
            "submission_id": doc.name,
            "submitted_at": str(doc.submitted_at),
            "attached": added,
            "skipped_duplicates": skipped,
            "image_count": doc.image_count,
            "max_images": MAX_IMAGES_PER_SUBMISSION,
        }
        if capped:
            payload["rejected_over_cap"] = capped

        _respond(200, payload)
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("save_images_by_phone", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "save_images_by_phone failed")
        _respond(500, {"status": "failure", "message": str(exc)})


# ---------------------------------------------------------------------------
# Endpoint — attach images to an existing submission
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def save_submission_images():
    """Attach one or more images to a submission.

    Expected body:
        submission_id  required, from start_submission
        images         required; a list of URLs, or of objects such as
                       {"url": ..., "type": ..., "source_time": ...,
                        "source_row_id": ...}

    Safe to call repeatedly: files already attached are skipped rather than
    duplicated, so a Glific retry or a re-run of the matching job is harmless.
    """
    data = _get_request_data()
    try:
        submission_id = str(data.get("submission_id") or "").strip()
        if not submission_id:
            _respond(400, {"status": "failure", "message": "submission_id is required"})
            return

        if not frappe.db.exists("Teacher Submission", submission_id):
            _respond(404, {"status": "failure", "message": "Submission not found"})
            return

        image_rows = normalize_images(data.get("images") or data.get("media"))
        if not image_rows:
            _respond(400, {
                "status": "failure",
                "message": "images is required and must contain at least one URL",
            })
            return

        doc = frappe.get_doc("Teacher Submission", submission_id)
        added, skipped, capped = attach_images(doc, image_rows)
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        payload = {
            "status": "success",
            "submission_id": doc.name,
            "added": added,
            "skipped_duplicates": skipped,
            "image_count": doc.image_count,
            "max_images": MAX_IMAGES_PER_SUBMISSION,
        }
        if capped:
            payload["rejected_over_cap"] = capped
            payload["message"] = (
                f"{capped} file(s) not stored: the cap of "
                f"{MAX_IMAGES_PER_SUBMISSION} per submission was reached."
            )

        _respond(200, payload)
    except Exception as exc:
        frappe.db.rollback()
        log_api_failure("save_submission_images", data, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "save_submission_images failed")
        _respond(500, {"status": "failure", "message": str(exc)})
