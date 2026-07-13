from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from typing import Callable, Iterable
from urllib.parse import urlparse

import frappe
import requests
from openpyxl import load_workbook
from psycopg2.extras import execute_values


# Bench console usage:
# from tap_lms.onboarding.bulk_student_registration import run_import
# run_import()
#
# Or override at call time:
# run_import(
#     spreadsheet_url="https://docs.google.com/spreadsheets/d/18VmTVTCwK2QNct3Y-aICdH7I6F2Zq8ltSWIIqIlRtgg/edit?gid=0#gid=0",
#     tab_names=["Existing Students", "New Students"],
#     sample_test=100,
# )

SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/18VmTVTCwK2QNct3Y-aICdH7I6F2Zq8ltSWIIqIlRtgg/edit?gid=0#gid=0"
TAB_NAMES = ["Existing Students", "New Students"]
SAMPLE_TEST = 0
BATCH_SIZE = 100
IMPORT_USER = "Administrator"

EXPECTED_COLUMNS = {
    "Student Name": "student_name_raw",
    "Contact No.": "contact_no_raw",
    "Gender": "gender_raw",
    "School ID": "school_id_raw",
    "Language": "language_raw",
    "Batch": "batch_raw",
    "Grade": "grade_raw",
    "Course": "course_raw",
}


@dataclass(frozen=True)
class RawRow:
    source_tab: str
    source_priority: int
    row_in_tab: int
    student_name_raw: str
    contact_no_raw: str
    gender_raw: str
    school_id_raw: str
    language_raw: str
    batch_raw: str
    grade_raw: str
    course_raw: str


def _emit(log_fn: Callable[[str], None] | None, message: str) -> None:
    if log_fn:
        log_fn(message)
    else:
        print(message)


def _emit_progress(
    progress_fn: Callable[[dict], None] | None,
    payload: dict,
) -> None:
    if progress_fn:
        progress_fn(payload)


def run_import(
    spreadsheet_url: str | None = None,
    tab_names: list[str] | None = None,
    sample_test: int | None = None,
    batch_size: int | None = None,
    import_user: str | None = None,
    log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[dict], None] | None = None,
) -> dict:
    spreadsheet_url = (spreadsheet_url or SPREADSHEET_URL or "").strip()
    tab_names = list(tab_names or TAB_NAMES or [])
    sample_test = int(SAMPLE_TEST if sample_test is None else sample_test)
    batch_size = int(BATCH_SIZE if batch_size is None else batch_size)
    import_user = (import_user or IMPORT_USER or frappe.session.user or "Administrator").strip()

    if not spreadsheet_url:
        raise ValueError("SPREADSHEET_URL is required")
    if not tab_names:
        raise ValueError("TAB_NAMES must contain at least one sheet name")
    if sample_test < 0:
        raise ValueError("sample_test must be >= 0")
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")

    import_date = date.today()
    started_at = frappe.utils.now_datetime()
    _emit(
        log_fn,
        f"[student-import] start import_date={import_date} "
        f"tabs={tab_names} sample_test={sample_test} batch_size={batch_size}"
    )
    try:
        workbook = _download_workbook(spreadsheet_url)
        rows = _read_selected_tabs(workbook, tab_names)
        if not rows:
            raise ValueError("No rows found in selected tabs")
        _emit(log_fn, f"[student-import] loaded raw rows={len(rows)}")

        _prepare_temp_tables()
        _bulk_insert_stage(rows)
        _build_clean_stage(sample_test)

        precheck = _run_prechecks()
        _print_precheck(precheck, log_fn=log_fn)
        _raise_if_blocking_precheck(precheck)

        total_effective_rows = precheck["effective_rows"]
        summary = {
            "updated_students": 0,
            "inserted_students": 0,
            "inserted_enrollments": 0,
            "batches_processed": 0,
            "effective_rows": total_effective_rows,
        }
        for batch_no, offset in enumerate(range(0, total_effective_rows, batch_size), start=1):
            _prepare_batch_subset(offset=offset, batch_size=batch_size)
            batch_summary = _execute_import(
                import_date=import_date,
                import_user=import_user,
                source_table="tmp_student_import_batch",
            )
            frappe.db.commit()
            summary["updated_students"] += batch_summary["updated_students"]
            summary["inserted_students"] += batch_summary["inserted_students"]
            summary["inserted_enrollments"] += batch_summary["inserted_enrollments"]
            summary["batches_processed"] = batch_no
            elapsed = frappe.utils.now_datetime() - started_at
            batch_message = (
                "[student-import] batch completed "
                f"batch_no={batch_no} "
                f"batch_rows={batch_summary['batch_rows']} "
                f"updated_in_batch={batch_summary['updated_students']} "
                f"inserted_in_batch={batch_summary['inserted_students']} "
                f"enrollments_in_batch={batch_summary['inserted_enrollments']} "
                f"processed_total={min(offset + batch_summary['batch_rows'], total_effective_rows)}/{total_effective_rows} "
                f"elapsed={elapsed}"
            )
            _emit(log_fn, batch_message)
            _emit_progress(progress_fn, {
                "event": "batch_completed",
                "batch_no": batch_no,
                "batch_rows": batch_summary["batch_rows"],
                "processed_total": min(offset + batch_summary["batch_rows"], total_effective_rows),
                "effective_rows": total_effective_rows,
                "elapsed": str(elapsed),
                "summary": dict(summary),
                "message": batch_message,
            })

        summary["import_date"] = str(import_date)
        summary["sample_test"] = sample_test
        summary["batch_size"] = batch_size
        summary["tabs"] = tab_names
        summary["elapsed"] = str(frappe.utils.now_datetime() - started_at)
        _emit(log_fn, f"[student-import] success summary={summary}")
        _emit_progress(progress_fn, {
            "event": "completed",
            "summary": dict(summary),
            "message": f"[student-import] success summary={summary}",
        })
        return summary
    except Exception:
        frappe.db.rollback()
        raise


def _download_workbook(spreadsheet_url: str):
    download_url = _build_download_url(spreadsheet_url)
    response = requests.get(download_url, timeout=120)
    response.raise_for_status()
    return load_workbook(BytesIO(response.content), read_only=True, data_only=True)


def _build_download_url(spreadsheet_url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", spreadsheet_url)
    if match:
        sheet_id = match.group(1)
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"

    parsed = urlparse(spreadsheet_url)
    if parsed.scheme in {"http", "https"}:
        return spreadsheet_url
    raise ValueError("Unsupported spreadsheet URL")


def _read_selected_tabs(workbook, tab_names: list[str]) -> list[RawRow]:
    missing_tabs = [tab_name for tab_name in tab_names if tab_name not in workbook.sheetnames]
    if missing_tabs:
        raise ValueError(
            f"Tabs not found in workbook: {missing_tabs}. "
            f"Available tabs: {workbook.sheetnames}"
        )

    rows: list[RawRow] = []
    for priority, tab_name in enumerate(tab_names, start=1):
        ws = workbook[tab_name]
        header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header:
            raise ValueError(f"Tab '{tab_name}' is empty")

        header_map = _resolve_header_map(tab_name, header)
        for row_in_tab, row_values in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            row_dict = {
                canonical_key: _stringify(row_values[idx] if idx < len(row_values) else None)
                for canonical_key, idx in header_map.items()
            }
            if not any(value.strip() for value in row_dict.values()):
                continue
            rows.append(
                RawRow(
                    source_tab=tab_name,
                    source_priority=priority,
                    row_in_tab=row_in_tab,
                    student_name_raw=row_dict["student_name_raw"],
                    contact_no_raw=row_dict["contact_no_raw"],
                    gender_raw=row_dict["gender_raw"],
                    school_id_raw=row_dict["school_id_raw"],
                    language_raw=row_dict["language_raw"],
                    batch_raw=row_dict["batch_raw"],
                    grade_raw=row_dict["grade_raw"],
                    course_raw=row_dict["course_raw"],
                )
            )
    return rows


def _resolve_header_map(tab_name: str, header: Iterable[object]) -> dict[str, int]:
    normalized = {str(value).strip(): idx for idx, value in enumerate(header) if value is not None}
    missing = [column for column in EXPECTED_COLUMNS if column not in normalized]
    if missing:
        raise ValueError(
            f"Tab '{tab_name}' is missing required columns: {missing}. "
            f"Found columns: {list(normalized.keys())}"
        )
    return {target_key: normalized[column_name] for column_name, target_key in EXPECTED_COLUMNS.items()}


def _stringify(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _prepare_temp_tables() -> None:
    sql = """
    DROP TABLE IF EXISTS tmp_student_import_raw;
    DROP TABLE IF EXISTS tmp_student_import_clean;
    DROP TABLE IF EXISTS tmp_student_import_effective;
    DROP TABLE IF EXISTS tmp_student_import_match;
    DROP TABLE IF EXISTS tmp_student_import_to_insert;
    DROP TABLE IF EXISTS tmp_student_import_inserted;
    DROP TABLE IF EXISTS tmp_student_import_resolved;

    CREATE TEMP TABLE tmp_student_import_raw (
        source_tab text NOT NULL,
        source_priority integer NOT NULL,
        row_in_tab integer NOT NULL,
        student_name_raw text,
        contact_no_raw text,
        gender_raw text,
        school_id_raw text,
        language_raw text,
        batch_raw text,
        grade_raw text,
        course_raw text
    );
    """
    frappe.db.sql(sql)


def _bulk_insert_stage(rows: list[RawRow], chunk_size: int = 2000) -> None:
    conn = getattr(frappe.db, "_conn", None)
    if conn is None:
        raise RuntimeError("Frappe database connection is not available")

    insert_sql = """
        INSERT INTO tmp_student_import_raw (
            source_tab,
            source_priority,
            row_in_tab,
            student_name_raw,
            contact_no_raw,
            gender_raw,
            school_id_raw,
            language_raw,
            batch_raw,
            grade_raw,
            course_raw
        ) VALUES %s
    """
    values = [
        (
            row.source_tab,
            row.source_priority,
            row.row_in_tab,
            row.student_name_raw,
            row.contact_no_raw,
            row.gender_raw,
            row.school_id_raw,
            row.language_raw,
            row.batch_raw,
            row.grade_raw,
            row.course_raw,
        )
        for row in rows
    ]

    with conn.cursor() as cursor:
        for start in range(0, len(values), chunk_size):
            execute_values(cursor, insert_sql, values[start:start + chunk_size], page_size=chunk_size)


def _build_clean_stage(sample_test: int) -> None:
    sample_limit_sql = ""
    params: tuple[object, ...] = ()
    if sample_test > 0:
        sample_limit_sql = "LIMIT %s"
        params = (sample_test,)

    sql = f"""
    CREATE TEMP TABLE tmp_student_import_clean AS
    WITH base AS (
        SELECT
            source_tab,
            source_priority,
            row_in_tab,
            trim(coalesce(student_name_raw, '')) AS student_name_raw,
            regexp_replace(
                regexp_replace(trim(coalesce(contact_no_raw, '')), '\\.0+$', ''),
                '\\D',
                '',
                'g'
            ) AS phone_digits,
            trim(coalesce(gender_raw, '')) AS gender_raw,
            nullif(trim(coalesce(school_id_raw, '')), '') AS school_id_in,
            nullif(trim(coalesce(language_raw, '')), '') AS language_name_in,
            nullif(trim(coalesce(batch_raw, '')), '') AS batch_in,
            nullif(trim(coalesce(grade_raw, '')), '') AS grade_in,
            nullif(trim(coalesce(course_raw, '')), '') AS course_name_in
        FROM tmp_student_import_raw
    ),
    phones AS (
        SELECT
            *,
            CASE
                WHEN length(phone_digits) = 10 THEN '91' || phone_digits
                WHEN length(phone_digits) = 12 AND left(phone_digits, 2) = '91' THEN phone_digits
                ELSE NULL
            END AS phone_12,
            CASE
                WHEN length(phone_digits) = 10 THEN phone_digits
                WHEN length(phone_digits) = 12 AND left(phone_digits, 2) = '91' THEN right(phone_digits, 10)
                ELSE NULL
            END AS phone_10
        FROM base
    ),
    cleaned AS (
        SELECT
            *,
            btrim(
                regexp_replace(
                    regexp_replace(student_name_raw, '[^A-Za-z ]+', '', 'g'),
                    '\\s+',
                    ' ',
                    'g'
                )
            ) AS cleaned_name_tmp,
            CASE upper(gender_raw)
                WHEN 'M' THEN 'Male'
                WHEN 'MALE' THEN 'Male'
                WHEN 'F' THEN 'Female'
                WHEN 'FEMALE' THEN 'Female'
                ELSE NULL
            END AS normalized_gender
        FROM phones
    )
    SELECT
        source_tab,
        source_priority,
        row_in_tab,
        student_name_raw,
        CASE
            WHEN cleaned_name_tmp IS NULL OR cleaned_name_tmp = '' THEN 'Champ'
            ELSE cleaned_name_tmp
        END AS cleaned_name,
        phone_12,
        phone_10,
        gender_raw,
        normalized_gender,
        school_id_in,
        language_name_in,
        batch_in,
        CASE
            WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
            ELSE grade_in
        END AS grade_in,
        CASE
            WHEN lower(coalesce(course_name_in, '')) = 'science' THEN 'Science Lab'
            ELSE course_name_in
        END AS course_name_in,
        CASE
            WHEN (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            ) ~ '^\\d+$'
            AND (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            )::int BETWEEN 1 AND 3 THEN 'Level 0'
            WHEN (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            ) ~ '^\\d+$'
            AND (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            )::int BETWEEN 4 AND 5 THEN 'Level 1'
            WHEN (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            ) ~ '^\\d+$'
            AND (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            )::int BETWEEN 6 AND 8 THEN 'Level 2'
            WHEN (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            ) ~ '^\\d+$'
            AND (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            )::int BETWEEN 9 AND 10 THEN 'Level 3'
            WHEN (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            ) ~ '^\\d+$'
            AND (
                CASE
                    WHEN grade_in ~ '^\\d+\\.0+$' THEN split_part(grade_in, '.', 1)
                    ELSE grade_in
                END
            )::int BETWEEN 11 AND 12 THEN 'Level 4'
            ELSE NULL
        END AS derived_level
    FROM cleaned
    """
    frappe.db.sql(sql)

    frappe.db.sql("""
        CREATE TEMP TABLE tmp_student_import_effective AS
        SELECT *
        FROM (
            SELECT *
            FROM (
                SELECT
                    c.*,
                    row_number() OVER (
                        PARTITION BY phone_12
                        ORDER BY source_priority, row_in_tab
                    ) AS phone_rank
                FROM tmp_student_import_clean c
                WHERE c.phone_12 IS NOT NULL
            ) ranked
            WHERE phone_rank = 1
            ORDER BY source_priority, row_in_tab
        ) limited
    """)

    if sample_limit_sql:
        frappe.db.sql(f"""
            CREATE TEMP TABLE tmp_student_import_effective_limited AS
            SELECT *
            FROM tmp_student_import_effective
            ORDER BY source_priority, row_in_tab
            {sample_limit_sql}
        """, params)
        frappe.db.sql("DROP TABLE tmp_student_import_effective")
        frappe.db.sql("""
            ALTER TABLE tmp_student_import_effective_limited
            RENAME TO tmp_student_import_effective
        """)


def _run_prechecks() -> dict:
    return {
        "raw_rows": _scalar("SELECT count(*) FROM tmp_student_import_raw"),
        "effective_rows": _scalar("SELECT count(*) FROM tmp_student_import_effective"),
        "invalid_phone_rows": _scalar("""
            SELECT count(*)
            FROM tmp_student_import_clean
            WHERE phone_12 IS NULL
        """),
        "duplicate_input_phones": _scalar("""
            SELECT count(*)
            FROM (
                SELECT phone_12
                FROM tmp_student_import_clean
                WHERE phone_12 IS NOT NULL
                GROUP BY phone_12
                HAVING count(*) > 1
            ) x
        """),
        "ignored_gender_rows": _scalar("""
            SELECT count(*)
            FROM tmp_student_import_effective
            WHERE normalized_gender IS NULL
        """),
        "invalid_grade_rows": _scalar("""
            SELECT count(*)
            FROM tmp_student_import_effective
            WHERE derived_level IS NULL
        """),
        "missing_school_rows": _scalar("""
            SELECT count(*)
            FROM tmp_student_import_effective e
            WHERE NOT EXISTS (
                SELECT 1
                FROM "tabSchool" s
                WHERE s.name = e.school_id_in
                   OR substring(s.name from '(SC[0-9]+)$') = e.school_id_in
            )
        """),
        "missing_batch_rows": _scalar("""
            SELECT count(*)
            FROM tmp_student_import_effective e
            WHERE NOT EXISTS (
                SELECT 1
                FROM "tabBatch" b
                WHERE b.name = e.batch_in
                   OR b.batch_id = e.batch_in
            )
        """),
        "missing_language_rows": _scalar("""
            SELECT count(*)
            FROM tmp_student_import_effective e
            LEFT JOIN "tabTAP Language" l
              ON l.language_name = e.language_name_in
            WHERE l.name IS NULL
        """),
        "missing_vertical_rows": _scalar("""
            SELECT count(*)
            FROM tmp_student_import_effective e
            WHERE NOT EXISTS (
                SELECT 1
                FROM "tabCourse Verticals" cv
                WHERE cv.name2 = e.course_name_in
                   OR cv.name = e.course_name_in
                   OR cv.vertical_id = e.course_name_in
            )
        """),
        "ambiguous_existing_phone_matches": _scalar("""
            SELECT count(*)
            FROM (
                SELECT
                    e.phone_12
                FROM tmp_student_import_effective e
                JOIN "tabStudent" s
                  ON s.phone IN (e.phone_12, e.phone_10)
                GROUP BY e.phone_12
                HAVING count(*) > 1
            ) x
        """),
    }


def _print_precheck(precheck: dict, log_fn: Callable[[str], None] | None = None) -> None:
    _emit(log_fn, "[student-import] precheck")
    for key, value in precheck.items():
        _emit(log_fn, f"  - {key}: {value}")


def _raise_if_blocking_precheck(precheck: dict) -> None:
    blocking = [
        "invalid_phone_rows",
        "invalid_grade_rows",
        "missing_school_rows",
        "missing_batch_rows",
        "missing_language_rows",
        "missing_vertical_rows",
        "ambiguous_existing_phone_matches",
    ]
    failures = {key: precheck[key] for key in blocking if precheck.get(key)}
    if failures:
        raise ValueError(f"Precheck failed: {failures}")


def _prepare_batch_subset(offset: int, batch_size: int) -> None:
    frappe.db.sql("DROP TABLE IF EXISTS tmp_student_import_batch")
    frappe.db.sql("""
        CREATE TEMP TABLE tmp_student_import_batch AS
        SELECT *
        FROM tmp_student_import_effective
        ORDER BY source_priority, row_in_tab
        OFFSET %s
        LIMIT %s
    """, (offset, batch_size))


def _execute_import(import_date: date, import_user: str, source_table: str = "tmp_student_import_effective") -> dict:
    source_sql = f"""
        CREATE TEMP TABLE tmp_student_import_match AS
        SELECT
            e.source_tab,
            e.source_priority,
            e.row_in_tab,
            e.cleaned_name,
            e.phone_12,
            e.phone_10,
            e.normalized_gender,
            school.name AS school_id,
            e.grade_in,
            batch.name AS batch_name,
            e.derived_level,
            l.name AS language_id,
            cv.name AS vertical_id,
            s.name AS student_id
        FROM {source_table} e
        JOIN "tabTAP Language" l
          ON l.language_name = e.language_name_in
        JOIN LATERAL (
            SELECT cv.name, cv.name2, cv.vertical_id
            FROM "tabCourse Verticals" cv
            WHERE cv.name2 = e.course_name_in
               OR cv.name = e.course_name_in
               OR cv.vertical_id = e.course_name_in
            ORDER BY cv.name
            LIMIT 1
        ) cv ON true
        JOIN LATERAL (
            SELECT s2.name
            FROM "tabSchool" s2
            WHERE s2.name = e.school_id_in
               OR substring(s2.name from '(SC[0-9]+)$') = e.school_id_in
            ORDER BY s2.name
            LIMIT 1
        ) school ON true
        JOIN LATERAL (
            SELECT b.name
            FROM "tabBatch" b
            WHERE b.name = e.batch_in
               OR b.batch_id = e.batch_in
            ORDER BY b.name
            LIMIT 1
        ) batch ON true
        LEFT JOIN "tabStudent" s
          ON s.phone IN (e.phone_12, e.phone_10)
    """
    frappe.db.sql("DROP TABLE IF EXISTS tmp_student_import_match")
    frappe.db.sql(source_sql)

    updated_count = _update_existing_students(import_user=import_user)
    inserted_count = _insert_new_students(import_date=import_date, import_user=import_user)
    enrollment_count = _insert_enrollments(import_date=import_date, import_user=import_user)

    return {
        "batch_rows": _scalar(f"SELECT count(*) FROM {source_table}"),
        "updated_students": updated_count,
        "inserted_students": inserted_count,
        "inserted_enrollments": enrollment_count,
    }


def _update_existing_students(import_user: str) -> int:
    rows = frappe.db.sql("""
        UPDATE "tabStudent" st
           SET name1 = m.cleaned_name,
               school_id = m.school_id,
               language = m.language_id,
               grade = m.grade_in,
               whatsapp_consent = 1,
               gender = CASE
                           WHEN m.normalized_gender IS NOT NULL THEN m.normalized_gender
                           ELSE st.gender
                        END,
               modified = now(),
               modified_by = %s
          FROM tmp_student_import_match m
         WHERE st.name = m.student_id
    """, (import_user,))
    return rows if isinstance(rows, int) else _scalar("""
        SELECT count(*)
        FROM tmp_student_import_match
        WHERE student_id IS NOT NULL
    """)


def _insert_new_students(import_date: date, import_user: str) -> int:
    frappe.db.sql("DROP TABLE IF EXISTS tmp_student_import_to_insert")
    frappe.db.sql("""
        CREATE TEMP TABLE tmp_student_import_to_insert AS
        SELECT *
        FROM tmp_student_import_match
        WHERE student_id IS NULL
    """)

    frappe.db.sql("DROP TABLE IF EXISTS tmp_student_import_inserted")
    total_to_insert = _scalar("SELECT count(*) FROM tmp_student_import_to_insert")
    if not total_to_insert:
        frappe.db.sql("""
            CREATE TEMP TABLE tmp_student_import_inserted (
                student_id text,
                phone_12 text
            )
        """)
        return 0

    _sync_student_series_counter()

    frappe.db.sql("""
        CREATE TEMP TABLE tmp_student_import_inserted (
            student_id text,
            phone_12 text
        )
    """)

    frappe.db.sql("""
        WITH cnt AS (
            SELECT count(*) AS n
            FROM tmp_student_import_to_insert
        ),
        bump AS (
            UPDATE "tabSeries"
               SET current = current + (SELECT n FROM cnt)
             WHERE name = 'ST'
         RETURNING current
        ),
        numbered AS (
            SELECT
                t.*,
                row_number() OVER (ORDER BY source_priority, row_in_tab) AS rn,
                (SELECT current FROM bump) - (SELECT n FROM cnt) AS base_no
            FROM tmp_student_import_to_insert t
        ),
        inserted AS (
            INSERT INTO "tabStudent" (
                name,
                creation,
                modified,
                modified_by,
                owner,
                docstatus,
                name1,
                phone,
                gender,
                school_id,
                grade,
                joined_on,
                status,
                language,
                whatsapp_consent
            )
            SELECT
                'ST' || lpad((base_no + rn)::text, 8, '0') AS name,
                now(),
                now(),
                %(import_user)s,
                %(import_user)s,
                0,
                cleaned_name,
                phone_12,
                normalized_gender,
                school_id,
                grade_in,
                %(import_date)s,
                'active',
                language_id,
                0
            FROM numbered
            RETURNING name, phone
        )
        INSERT INTO tmp_student_import_inserted (student_id, phone_12)
        SELECT name, phone
        FROM inserted
    """, {
        "import_user": import_user,
        "import_date": import_date,
    })

    return total_to_insert


def _sync_student_series_counter() -> None:
    """Self-heal the ST counter from real Student rows before reserving IDs.

    Some sites have tabSeries.current behind the actual max Student name.
    If we trust the stale counter, bulk inserts can reuse an existing ST id
    and fail on the Student primary key.
    """
    frappe.db.sql("""
        INSERT INTO "tabSeries" (name, current)
        VALUES ('ST', 0)
        ON CONFLICT (name) DO NOTHING
    """)
    frappe.db.sql("""
        WITH max_student AS (
            SELECT COALESCE(
                max(CASE
                    WHEN name ~ '^ST[0-9]{8}$' THEN substring(name from 3)::integer
                    ELSE 0
                END),
                0
            ) AS max_no
            FROM "tabStudent"
        )
        UPDATE "tabSeries" ts
           SET current = GREATEST(ts.current, ms.max_no)
          FROM max_student ms
         WHERE ts.name = 'ST'
    """)


def _insert_enrollments(import_date: date, import_user: str) -> int:
    frappe.db.sql("DROP TABLE IF EXISTS tmp_student_import_resolved")
    frappe.db.sql("""
        CREATE TEMP TABLE tmp_student_import_resolved AS
        SELECT
            m.source_tab,
            m.source_priority,
            m.row_in_tab,
            m.phone_12,
            m.school_id,
            m.grade_in,
            m.batch_name,
            m.derived_level,
            m.vertical_id,
            m.student_id,
            true AS existed_before_import
        FROM tmp_student_import_match m
        WHERE m.student_id IS NOT NULL

        UNION ALL

        SELECT
            t.source_tab,
            t.source_priority,
            t.row_in_tab,
            t.phone_12,
            t.school_id,
            t.grade_in,
            t.batch_name,
            t.derived_level,
            t.vertical_id,
            i.student_id,
            false AS existed_before_import
        FROM tmp_student_import_to_insert t
        JOIN tmp_student_import_inserted i
          ON i.phone_12 = t.phone_12
    """)

    total_rows = _scalar("SELECT count(*) FROM tmp_student_import_resolved")
    if not total_rows:
        return 0

    frappe.db.sql("""
        INSERT INTO "tabStudent Enrollment" (
            name,
            creation,
            modified,
            modified_by,
            owner,
            docstatus,
            parent,
            parentfield,
            parenttype,
            idx,
            batch,
            vertical,
            level,
            grade,
            date_joining,
            school,
            whatsapp_response
        )
        SELECT
            md5(
                clock_timestamp()::text
                || random()::text
                || r.student_id
                || coalesce(r.batch_name, '')
                || r.source_tab
                || r.row_in_tab::text
            ) AS name,
            now(),
            now(),
            %(import_user)s,
            %(import_user)s,
            0,
            r.student_id,
            'enrollment',
            'Student',
            coalesce((
                SELECT max(e.idx)
                FROM "tabStudent Enrollment" e
                WHERE e.parent = r.student_id
            ), 0) + 1 AS idx,
            r.batch_name,
            r.vertical_id,
            r.derived_level,
            r.grade_in,
            %(import_date)s,
            r.school_id,
            0
        FROM tmp_student_import_resolved r
    """, {
        "import_user": import_user,
        "import_date": import_date,
    })
    return total_rows


def _scalar(sql: str, params: object | None = None) -> int:
    result = frappe.db.sql(sql, params)
    return int(result[0][0]) if result else 0
