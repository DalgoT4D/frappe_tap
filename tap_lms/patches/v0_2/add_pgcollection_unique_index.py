"""
CR-005 follow-up (2026-05-16) — partial unique index on PGCollection
to enforce (parent, kind) uniqueness at the DB level for active rows.

The pgcollection.py `validate` hook is the friendly first line of defense
(raises DuplicateEntryError with a clear message). This index is the hard
guarantee: even if two concurrent `activate_bpr` workers each pass validate
before either commits, only one INSERT can succeed.

Partial index means:
  - Legacy archetype-keyed rows (kind IS NULL / '') don't participate.
  - Deactivated rows (is_active = 0) don't participate.
  Both can duplicate freely without firing the constraint.

Postgres-only (project DB). Idempotent: `IF NOT EXISTS` makes re-runs
no-ops. Patch is registered post-pgcollection.json doctype sync, so the
columns (`kind`, `is_active`) are guaranteed to exist.
"""
import frappe


INDEX_NAME = "idx_pgcollection_parent_kind_active"


def execute():
    # CLAUDE.md: PG txn hygiene — clear any poisoned state from prior patches.
    frappe.db.rollback()

    # Reload the doctype so the columns we reference are guaranteed live in
    # the schema. Same defensive move as backfill_pg_collection_kinds.py —
    # idempotent and cheap.
    frappe.reload_doc("tap_lms", "doctype", "pgcollection")

    # `IF NOT EXISTS` makes this idempotent. Re-running the patch is a no-op.
    # WHERE clause: only kind-keyed rows (non-NULL, non-empty kind) that are
    # currently active participate. Legacy rows and deactivated rows are out.
    frappe.db.sql(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
            ON "tabPGCollection" (parent, kind)
         WHERE kind IS NOT NULL
           AND kind != ''
           AND is_active = 1
        """
    )
    frappe.db.commit()

    frappe.logger().info(
        f"add_pgcollection_unique_index: index {INDEX_NAME} ensured "
        f"on tabPGCollection(parent, kind) WHERE kind IS NOT NULL AND is_active = 1"
    )
