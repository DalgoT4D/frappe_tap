"""
CR-005 (2026-05-15) — backfill kind-keyed PGCollection rows + Glific groups
for existing active BPRs, place every active PE into the right collection,
and deactivate legacy archetype-keyed PGCollection rows.

Idempotent: re-running the patch is safe. Every step looks up before
inserting, and the per-PE membership enqueue is itself idempotent on the
Glific side (re-adding an existing member is a no-op).

Postgres-only (project DB). Per CLAUDE.md txn hygiene, this patch calls
`frappe.db.rollback()` up-front to release any prior failed transaction
state before running its own work.
"""
import frappe

from tap_lms.summer_program.collection_membership import (
    MAIN_ELIGIBLE_STATES,
    STATE_TO_AUDIT_KIND,
    COLLECTION_KINDS,
)


def execute():
    # CLAUDE.md: PG txn hygiene — a poisoned txn from an earlier patch in
    # the chain would otherwise block every query in this patch.
    frappe.db.rollback()

    # CR-005 (2026-05-16): force the PGCollection doctype JSON to sync to the
    # schema BEFORE we query/write the new `kind`, `is_active`, `member_count`
    # columns. Otherwise this patch races the doctype sync that normally runs
    # after pre_model_sync patches — and on Frappe versions where the
    # `[post_model_sync]` marker in patches.txt is not honored (or the marker
    # was lost), the columns won't exist when we hit `frappe.db.exists(...)`.
    # `reload_doc` is idempotent and safe to call from a patch.
    frappe.reload_doc("tap_lms", "doctype", "pgcollection")

    # ── 1. Create the 5 kind-keyed PGCollections + Glific groups per active BPR ──
    active_bprs = frappe.get_all(
        "BatchProgramRun",
        filters={"status": "active"},
        fields=["name", "batch"],
    )

    from tap_lms.glific_integration import create_group_if_missing

    for bpr in active_bprs:
        for kind in COLLECTION_KINDS:
            existing = frappe.db.exists(
                "PGCollection",
                {"parent": bpr["name"], "kind": kind},
            )
            if existing:
                continue

            label = f"SP_{bpr['batch']}_{kind}"
            glific_group_id = create_group_if_missing(
                label,
                description=f"CR-005 {kind} collection for BPR {bpr['name']}",
            )
            if not glific_group_id:
                # Group bootstrap failed — skip this row; the patch is
                # rerunnable so the next attempt picks it up.
                continue

            pg_col = frappe.new_doc("PGCollection")
            pg_col.parent = bpr["name"]
            pg_col.parenttype = "BatchProgramRun"
            pg_col.parentfield = "pg_collections"
            pg_col.kind = kind
            pg_col.collection_label = label
            pg_col.glific_group_id = str(glific_group_id)
            pg_col.member_count = 0
            pg_col.is_active = 1
            pg_col.insert(ignore_permissions=True)

    frappe.db.commit()

    # ── 2. Enqueue add-to-group jobs for every active PE → its target kind ──
    # We don't call Glific synchronously inside the patch. Instead we lean on
    # `_enqueue_group_write` (P-007 retry+DLQ) so the patch returns quickly
    # and the actual API writes happen in background workers, with the same
    # retry/DLQ contract as production state transitions.
    pes = frappe.db.sql(
        """
        SELECT pe.name, pe.glific_id, pe.batch, pe.resolved_flow_state
          FROM "tabProgramEnrollment" pe
          JOIN "tabBatchProgramRun" bpr ON bpr.batch = pe.batch
         WHERE bpr.status = 'active'
           AND pe.glific_id IS NOT NULL
           AND pe.glific_id != ''
           AND pe.program_status IN ('active', 'paused')
        """,
        as_dict=True,
    )

    from tap_lms.summer_program.collection_membership import _enqueue_group_write

    enqueued = 0
    for pe in pes:
        # Build a tiny doc-like object the helper can consume. We don't need
        # the full ProgramEnrollment doc — only name, glific_id, batch.
        pe_doc = frappe._dict({
            "name": pe["name"],
            "glific_id": pe["glific_id"],
            "batch": pe["batch"],
        })
        state = pe["resolved_flow_state"]

        if state in MAIN_ELIGIBLE_STATES:
            _enqueue_group_write(pe_doc, kind="main", action="add")
            enqueued += 1

        audit_kind = STATE_TO_AUDIT_KIND.get(state)
        if audit_kind:
            _enqueue_group_write(pe_doc, kind=audit_kind, action="add")
            enqueued += 1

    # ── 3. Deactivate legacy archetype-keyed rows on those BPRs ──
    # Legacy rows have `kind IS NULL` / '' and an archetype/arm value.
    # Setting `is_active = 0` makes them invisible to the new code path
    # without losing the historical record.
    #
    # L-005 (Postgres + Frappe modify_values): use `IN %s` with a tuple param.
    # Earlier draft used `ANY(%s::text[])` with a list, but Frappe's
    # `modify_values()` rewrites a (list,) param to [(tuple,)] before psycopg2
    # sees it, which then renders the tuple as composite syntax `('x')` not
    # array syntax `'{x}'` — Postgres rejects the cast to text[] with
    # "malformed array literal". `IN %s` + tuple is the canonical Postgres-safe
    # pattern Frappe's modify_values was designed for.
    legacy_bpr_names = [b["name"] for b in active_bprs]
    if legacy_bpr_names:
        frappe.db.sql(
            """
            UPDATE "tabPGCollection"
               SET is_active = 0
             WHERE parent IN %s
               AND (kind IS NULL OR kind = '')
            """,
            (tuple(legacy_bpr_names),),
        )

    frappe.db.commit()
    print(
        f"CR-005 migration complete: {len(active_bprs)} BPRs touched, "
        f"{len(pes)} PEs scanned, {enqueued} group-write jobs enqueued."
    )
