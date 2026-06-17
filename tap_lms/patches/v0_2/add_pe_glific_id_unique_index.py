"""
Sibling-skip defense-in-depth (2026-05-18) — partial unique index on
ProgramEnrollment (batch, glific_id) for active/paused PEs.

Backing index for the application-level sibling skip in `_process_pe_chunk`
and `create_program_enrollment` (program_enrollment_api.py). The app-level
check is the friendly first line; this DB constraint is the hard guarantee
against race conditions where two parallel chunk workers might both pass
their own pre-flight check before either commits.

WHY:
  - Two Frappe Students can legitimately share a `glific_id` (siblings on
    the same household WhatsApp number).
  - Per the pending sibling PRD, the interim policy is "one PE per Glific
    contact per batch" — second sibling is skipped at enrollment time.
  - At application level we already do the check (skip + log), but parallel
    workers could race past it. This partial unique index makes the race
    impossible at the DB layer.

PARTIAL — only kind-keyed cases participate:
  - `glific_id IS NOT NULL AND glific_id != ''` so students without a Glific
    contact (legacy / pre-Glific data) are out of scope.
  - `program_status IN ('active','paused')` so a dropped PE doesn't block a
    re-enrollment for the same sibling (a sibling can replace a dropped one
    in a future batch revision).

Postgres-only. Idempotent: `IF NOT EXISTS` makes re-runs no-ops.
"""
import frappe


INDEX_NAME = "idx_pe_batch_glific_id_active"


def execute():
    # PG txn hygiene — clear any poisoned txn state from earlier patches.
    frappe.db.rollback()

    # Reload the doctype so the columns we reference (batch, glific_id,
    # program_status) are guaranteed live in the schema. Idempotent + cheap.
    frappe.reload_doc("tap_lms", "doctype", "programenrollment")

    # IF NOT EXISTS makes the patch idempotent. Re-running is a no-op.
    # The WHERE clause makes this a partial index — only active/paused PEs
    # with a real glific_id participate in the uniqueness constraint.
    frappe.db.sql(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
            ON "tabProgramEnrollment" (batch, glific_id)
         WHERE glific_id IS NOT NULL
           AND glific_id != ''
           AND program_status IN ('active', 'paused')
        """
    )
    frappe.db.commit()

    frappe.logger().info(
        f"add_pe_glific_id_unique_index: index {INDEX_NAME} ensured "
        f"on tabProgramEnrollment(batch, glific_id) "
        f"WHERE glific_id IS NOT NULL AND program_status IN ('active','paused')"
    )
