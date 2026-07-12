#!/usr/bin/env python3
from __future__ import annotations

import argparse

import frappe


STUDENT_TARGET = "Student Enrollment"
TEACHER_TARGET = "Teacher Enrollment"
SOURCE_TABLE = "tabEnrollment"


def _insert_rows(target_doctype: str, rows: list[dict], fieldnames: list[str]) -> int:
    created = 0
    for row in rows:
        exists = frappe.db.exists(
            target_doctype,
            {
                "parent": row["parent"],
                "parenttype": row["parenttype"],
                "parentfield": row["parentfield"],
                "idx": row["idx"],
            },
        )
        if exists:
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
    return created


def _backfill_student_enrollment_fields(rows: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for row in rows:
        row = dict(row)
        course_level = row.get("course")
        if course_level:
            course_level_doc = frappe.db.get_value(
                "Course Level",
                course_level,
                ["vertical", "level"],
                as_dict=True,
            )
            if course_level_doc:
                row["vertical"] = course_level_doc.get("vertical") or row.get("vertical")
                row["level"] = course_level_doc.get("level") or ""
        enriched.append(row)
    return enriched


def execute() -> None:
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
    )
    created_teachers = _insert_rows(
        TEACHER_TARGET,
        teacher_rows,
        ["batch", "date_joining", "school", "whatsapp_response"],
    )
    frappe.db.commit()
    print(
        f"Migrated legacy Enrollment rows: "
        f"student_created={created_students}, teacher_created={created_teachers}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy Enrollment rows to Student/Teacher Enrollment.")
    parser.add_argument("--site", required=True, help="Frappe site name")
    parser.add_argument("--student", help="Student document name to migrate")
    parser.add_argument("--teacher", help="Teacher document name to migrate")
    args = parser.parse_args()

    frappe.init(site=args.site)
    frappe.connect()
    try:
        if args.student or args.teacher:
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
                )
            else:
                created_teachers = 0

            frappe.db.commit()
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
