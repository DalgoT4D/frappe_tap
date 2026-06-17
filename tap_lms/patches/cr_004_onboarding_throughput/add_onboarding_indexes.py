"""
CR-004 onboarding throughput: composite indexes on Student.phone and
Grade Course Level Mapping lookup columns, plus a backfill of
Backend Students.glific_sync_status from the historic processing_status
and Student.glific_id columns.

Idempotency:
- Both CREATE INDEX statements use IF NOT EXISTS (re-runs are no-ops).
- Backfill UPDATE only touches rows WHERE glific_sync_status IS NULL or ''
  so a second run is a no-op (L-021).
- has_column guard ensures the patch works on fresh installs where the
  glific_sync_status column may not yet exist (L-059).
- table_exists guard wraps has_column to prevent TableMissingError on a
  site where the table itself doesn't exist yet (L-061).

VACUUM ANALYZE is a manual post-deploy step (cannot run inside a txn block).
After bench migrate completes, run:
    psql -U <db_user> -d <db_name> -c 'VACUUM ANALYZE "tabStudent";'
    psql -U <db_user> -d <db_name> -c 'VACUUM ANALYZE "tabGrade Course Level Mapping";'
"""
import frappe


def execute():
    # PG txn hygiene — clear any poisoned transaction from upstream patches (L-030).
    frappe.db.rollback()

    # Self-heal: force the DocType JSON to sync so referenced columns are live
    # before any SQL runs (L-036).
    frappe.reload_doc("tap_lms", "doctype", "student")
    frappe.reload_doc("tap_lms", "doctype", "grade_course_level_mapping")
    frappe.reload_doc("tap_lms", "doctype", "backend_students")

    # ── Index 1: Student.phone ──────────────────────────────────────────────
    # Speeds up the dedup lookup in process_glific_contact /
    # find_existing_student_by_phone_and_name (currently a seqscan at scale).
    frappe.db.sql_ddl(
        'CREATE INDEX IF NOT EXISTS idx_student_phone '
        'ON "tabStudent" (phone)'
    )

    # ── Index 2: Grade Course Level Mapping composite lookup ────────────────
    # Covers the 5-column WHERE clause used by get_course_level_with_mapping_backend:
    # academic_year, course_vertical, grade, student_type, is_active.
    frappe.db.sql_ddl(
        'CREATE INDEX IF NOT EXISTS idx_gclm_lookup '
        'ON "tabGrade Course Level Mapping" '
        '(academic_year, course_vertical, grade, student_type, is_active)'
    )

    # ── Backfill glific_sync_status ─────────────────────────────────────────
    # Guard: has_column returns False (not an error) if the table exists but
    # the column doesn't; table_exists prevents TableMissingError on a fresh
    # install where the table was never created (L-059, L-061).
    if (frappe.db.table_exists("Backend Students")
            and frappe.db.has_column("Backend Students", "glific_sync_status")):

        # Case 1: processing_status='Success' AND linked Student has a glific_id
        # → mark 'synced'. Only touch rows still NULL/empty (idempotent, L-021).
        frappe.db.sql("""
            UPDATE "tabBackend Students" bs
               SET glific_sync_status = 'synced'
              FROM "tabStudent" s
             WHERE bs.student_id = s.name
               AND bs.processing_status = 'Success'
               AND COALESCE(s.glific_id, '') <> ''
               AND COALESCE(bs.glific_sync_status, '') = ''
        """)

        # Case 2: processing_status='Success' AND no Student glific_id (or no
        # matching Student) → mark 'failed'.
        # Uses NOT EXISTS sub-select to handle the case where the Student row
        # doesn't exist at all (student_id is NULL / dangling link).
        frappe.db.sql("""
            UPDATE "tabBackend Students" bs
               SET glific_sync_status = 'failed'
             WHERE bs.processing_status = 'Success'
               AND COALESCE(bs.glific_sync_status, '') = ''
               AND NOT EXISTS (
                   SELECT 1
                     FROM "tabStudent" s
                    WHERE s.name = bs.student_id
                      AND COALESCE(s.glific_id, '') <> ''
               )
        """)
        # All other rows (processing_status != 'Success') remain at the schema
        # default 'pending' — nothing to backfill.

    frappe.db.commit()

    frappe.logger().info(
        "add_onboarding_indexes: idx_student_phone and idx_gclm_lookup created "
        "(IF NOT EXISTS); glific_sync_status backfill completed."
    )
