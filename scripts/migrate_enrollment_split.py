#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time

import frappe


STUDENT_TARGET = "Student Enrollment"
TEACHER_TARGET = "Teacher Enrollment"
SOURCE_TABLE = "tabEnrollment"
COMMIT_EVERY = 1000


def _row_key(row: dict) -> tuple[str, str, str, int]:
    return (
        row["parent"],
        row["parenttype"],
        row["parentfield"],
        int(row["idx"]),
    )


def _existing_row_keys(target_doctype: str, rows: list[dict]) -> set[tuple[str, str, str, int]]:
    if not rows:
        return set()

    parenttype = rows[0]["parenttype"]
    parents = sorted({row["parent"] for row in rows})
    existing_rows = frappe.get_all(
        target_doctype,
        filters={
            "parenttype": parenttype,
            "parent": ["in", parents],
        },
        fields=["parent", "parenttype", "parentfield", "idx"],
        limit_page_length=max(len(parents) * 5, 50000),
    )
    return {
        (row["parent"], row["parenttype"], row["parentfield"], int(row["idx"]))
        for row in existing_rows
    }


def _insert_rows(target_doctype: str, rows: list[dict], fieldnames: list[str], start_time: float) -> int:
    created = 0
    existing_keys = _existing_row_keys(target_doctype, rows)
    for row in rows:
        key = _row_key(row)
        if key in existing_keys:
            continue

        doc = frappe.new_doc(target_doctype)
        doc.parent = row["parent"]
        doc.parenttype = row["parenttype"]
        doc.parentfield = row["parentfield"]
        doc.idx = row["idx"]
        for fieldname in fieldnames:
            doc.set(fieldname, row.get(fieldname))
        doc.insert(ignore_permissions=True)
        created += 1
        existing_keys.add(key)
        if created % COMMIT_EVERY == 0:
            frappe.db.commit()
            elapsed = time.time() - start_time
            print(
                f"[{target_doctype}] committed {created} rows; "
                f"elapsed={elapsed:.1f}s ({elapsed / 60:.1f} min)"
            )
    return created


def _backfill_student_enrollment_fields(rows: list[dict]) -> list[dict]:
    course_level_names = sorted({row.get("course") for row in rows if row.get("course")})
    course_level_cache = {
        row["name"]: row
        for row in frappe.get_all(
            "Course Level",
            filters={"name": ["in", course_level_names]} if course_level_names else None,
            fields=["name", "vertical", "level"],
            limit_page_length=max(len(course_level_names), 1),
        )
    }

    enriched: list[dict] = []
    for row in rows:
        row = dict(row)
        course_level = row.get("course")
        if course_level:
            course_level_doc = course_level_cache.get(course_level)
            if course_level_doc:
                row["vertical"] = course_level_doc.get("vertical") or row.get("vertical")
                row["level"] = course_level_doc.get("level") or ""
        enriched.append(row)
    return enriched


def execute() -> None:
    start_time = time.time()
    if not frappe.db.table_exists("Enrollment"):
        print("No legacy Enrollment table found. Nothing to migrate.")
        return

    student_rows = frappe.db.sql(
        f"""
        SELECT parent, parenttype, parentfield, idx, batch, vertical, course,
               grade, date_joining, school, whatsapp_response
        FROM `{SOURCE_TABLE}`
        WHERE parenttype = 'Student'
        ORDER BY parent, idx
        """,
        as_dict=True,
    )
    teacher_rows = frappe.db.sql(
        f"""
        SELECT parent, parenttype, parentfield, idx, batch,
               date_joining, school, whatsapp_response
        FROM `{SOURCE_TABLE}`
        WHERE parenttype = 'Teacher'
        ORDER BY parent, idx
        """,
        as_dict=True,
    )

    student_rows = _backfill_student_enrollment_fields(student_rows)
    created_students = _insert_rows(
        STUDENT_TARGET,
        student_rows,
        ["batch", "vertical", "level", "grade", "date_joining", "school", "whatsapp_response"],
        start_time,
    )
    created_teachers = _insert_rows(
        TEACHER_TARGET,
        teacher_rows,
        ["batch", "date_joining", "school", "whatsapp_response"],
        start_time,
    )
    frappe.db.commit()
    elapsed = time.time() - start_time
    print(
        f"[final] committed remaining rows; elapsed={elapsed:.1f}s ({elapsed / 60:.1f} min)"
    )
    print(
        f"Migrated legacy Enrollment rows: "
        f"student_created={created_students}, teacher_created={created_teachers}"
    )


def execute_teachers_only() -> None:
    start_time = time.time()
    if not frappe.db.table_exists("Enrollment"):
        print("No legacy Enrollment table found. Nothing to migrate.")
        return

    teacher_rows = frappe.db.sql(
        f"""
        SELECT parent, parenttype, parentfield, idx, batch,
               date_joining, school, whatsapp_response
        FROM `{SOURCE_TABLE}`
        WHERE parenttype = 'Teacher'
        ORDER BY parent, idx
        """,
        as_dict=True,
    )

    created_teachers = _insert_rows(
        TEACHER_TARGET,
        teacher_rows,
        ["batch", "date_joining", "school", "whatsapp_response"],
        start_time,
    )
    frappe.db.commit()
    elapsed = time.time() - start_time
    print(
        f"[final] committed remaining rows; elapsed={elapsed:.1f}s ({elapsed / 60:.1f} min)"
    )
    print(
        f"Migrated teacher-only legacy Enrollment rows: "
        f"teacher_created={created_teachers}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy Enrollment rows to Student/Teacher Enrollment.")
    parser.add_argument("--site", required=True, help="Frappe site name")
    parser.add_argument("--student", help="Student document name to migrate")
    parser.add_argument("--teacher", help="Teacher document name to migrate")
    parser.add_argument(
        "--teachers-only",
        action="store_true",
        help="Migrate all legacy teacher enrollment rows only",
    )
    args = parser.parse_args()

    if args.teachers_only and (args.student or args.teacher):
        parser.error("--teachers-only cannot be combined with --student or --teacher")

    frappe.init(site=args.site)
    frappe.connect()
    try:
        start_time = time.time()
        if args.teachers_only:
            execute_teachers_only()
        elif args.student or args.teacher:
            if args.student:
                student_rows = frappe.db.sql(
                    f"""
                    SELECT parent, parenttype, parentfield, idx, batch, vertical, course,
                           grade, date_joining, school, whatsapp_response
                    FROM `{SOURCE_TABLE}`
                    WHERE parenttype = 'Student' AND parent = %s
                    ORDER BY parent, idx
                    """,
                    (args.student,),
                    as_dict=True,
                )
                student_rows = _backfill_student_enrollment_fields(student_rows)
                created_students = _insert_rows(
                    STUDENT_TARGET,
                    student_rows,
                    ["batch", "vertical", "level", "grade", "date_joining", "school", "whatsapp_response"],
                    start_time,
                )
            else:
                created_students = 0

            if args.teacher:
                teacher_rows = frappe.db.sql(
                    f"""
                    SELECT parent, parenttype, parentfield, idx, batch,
                           date_joining, school, whatsapp_response
                    FROM `{SOURCE_TABLE}`
                    WHERE parenttype = 'Teacher' AND parent = %s
                    ORDER BY parent, idx
                    """,
                    (args.teacher,),
                    as_dict=True,
                )
                created_teachers = _insert_rows(
                    TEACHER_TARGET,
                    teacher_rows,
                    ["batch", "date_joining", "school", "whatsapp_response"],
                    start_time,
                )
            else:
                created_teachers = 0

            frappe.db.commit()
            elapsed = time.time() - start_time
            print(
                f"[final] committed remaining rows; elapsed={elapsed:.1f}s ({elapsed / 60:.1f} min)"
            )
            print(
                f"Migrated selected legacy Enrollment rows: "
                f"student_created={created_students}, teacher_created={created_teachers}"
            )
        else:
            execute()
        return 0
    finally:
        frappe.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
