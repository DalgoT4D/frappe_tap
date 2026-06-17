"""CR-029 — one-time MAIN collection backfill for already-activated BPRs.

Context (2026-06-15): the BR-002 fix that bulk-populates the `main` Glific
collection at `activate_bpr` time landed 2026-06-03 — AFTER BT00000019
activated on 2026-05-28. So BT00000019's `main` collection was never
populated by the activation path; only organic state-machine transitions
(`maintain_collections`) + an operator's hand-add have populated it since.
As of 2026-06-15 the gap is ~5K (65,029 in MAIN vs ~70K main-eligible PEs).

This module provides an operator-run backfill that adds all currently
main-eligible PEs (with a non-empty `glific_id`) to the MAIN Glific group
for a given batch — idempotent (re-running is safe — Glific's
updateGroupContacts.addContactIds is a no-op for contacts already in the
group; `member_count` is SET, not incremented).

Chunked add + circuit breaker delegated to the shared helper
`glific_extensions.bulk_add_to_group_with_circuit_breaker` (L-074 anti-drift —
same helper `activate_bpr`'s `_bulk_populate_kind_keyed_collections` uses).
"""
import frappe

from tap_lms.summer_program.collection_membership import MAIN_ELIGIBLE_STATES
from tap_lms.summer_program.constants import BPR_ACTIVE, PROGRAM_ACTIVE
from tap_lms.summer_program.glific_extensions import (
    bulk_add_to_group_with_circuit_breaker,
)
from tap_lms.summer_program.migrations import coerce_bool


@frappe.whitelist()
def backfill_main_collection(
    batch_name,
    dry_run=True,
    i_know_this_is_destructive=False,
):
    """Backfill the MAIN Glific collection for a batch.

    Adds every currently main-eligible PE in `batch_name` (with a non-empty
    `glific_id`) to the `main` PGCollection's Glific group. Idempotent —
    Glific treats `updateGroupContacts.addContactIds` as a no-op for contacts
    already in the group, and `member_count` is SET (not incremented) to the
    count actually added.

    Args:
        batch_name: Batch doc name (e.g. 'BT00000019').
        dry_run: If True (default), report counts without writing.
        i_know_this_is_destructive: Must be True for a non-dry-run. The
            destructive label is conservative — the operation only adds to a
            collection (no removes, no PE state changes) — but the guard
            matches the rest of the operator-run migration suite (CR-027).

    Returns:
        dict {
            batch, bpr, dry_run, candidates, group_id,
            chunks_attempted, chunks_failed, added,
            circuit_tripped, skipped_after_trip,
            member_count_after, errors
        }.
        On a missing BPR or missing 'main' PGCollection the function returns
        early with `candidates=0` and an error message — no raise.
    """
    frappe.only_for(["TAP Admin", "System Manager"])

    dry_run = coerce_bool(dry_run)
    destructive_ok = coerce_bool(i_know_this_is_destructive)

    if not dry_run and not destructive_ok:
        raise frappe.ValidationError(
            "Refusing to run a non-dry-run main-collection backfill without "
            "i_know_this_is_destructive=True. Pass dry_run=True to preview."
        )

    summary = {
        "batch": batch_name,
        "bpr": None,
        "dry_run": dry_run,
        "candidates": 0,
        "group_id": None,
        "chunks_attempted": 0,
        "chunks_failed": 0,
        "added": 0,
        "circuit_tripped": False,
        "skipped_after_trip": 0,
        "member_count_after": None,
        "errors": [],
    }

    bpr_name = frappe.db.get_value(
        "BatchProgramRun", {"batch": batch_name, "status": BPR_ACTIVE}, "name"
    )
    summary["bpr"] = bpr_name
    if not bpr_name:
        summary["errors"].append(
            f"no active BPR for batch {batch_name} — cannot resolve "
            f"main PGCollection"
        )
        return summary

    main_col = frappe.db.sql(
        """
        SELECT name, glific_group_id
          FROM "tabPGCollection"
         WHERE parent = %s
           AND kind = 'main'
           AND COALESCE(is_active, 0) = 1
         LIMIT 1
        """,
        (bpr_name,),
        as_dict=True,
    )
    if not main_col or not main_col[0].get("glific_group_id"):
        summary["errors"].append(
            f"BPR {bpr_name} has no active 'main' PGCollection — "
            f"_ensure_kind_keyed_pg_collections must run first"
        )
        return summary

    pg_collection_name = main_col[0]["name"]
    group_id = main_col[0]["glific_group_id"]
    summary["group_id"] = group_id

    # Main-eligible filter mirrors maintain_collections (state-driven, L-055).
    # IN %s with a tuple parameter is the canonical safe pattern on this Frappe
    # version (L-038) for set-based filters on frappe.db.sql.
    pes = frappe.db.sql(
        """
        SELECT name, glific_id
          FROM "tabProgramEnrollment"
         WHERE batch = %s
           AND program_status = %s
           AND resolved_flow_state IN %s
           AND glific_id IS NOT NULL
           AND glific_id != ''
         ORDER BY name
        """,
        (batch_name, PROGRAM_ACTIVE, tuple(sorted(MAIN_ELIGIBLE_STATES))),
        as_dict=True,
    )

    glific_ids = [row["glific_id"] for row in pes]
    summary["candidates"] = len(glific_ids)

    frappe.logger().info(
        f"backfill_main_collection: batch={batch_name} bpr={bpr_name} "
        f"group={group_id} candidates={summary['candidates']} dry_run={dry_run}"
    )

    if dry_run:
        # Dry-run: report candidates without writing. member_count_after stays
        # None to signal "we didn't measure or write — operator should re-run
        # without dry_run to actually backfill".
        return summary

    if not glific_ids:
        # Real-run with 0 candidates: the helper would have returned added=0,
        # so mirror that contract — `added` and `member_count_after` are both
        # 0 (consistent with `chunks_attempted=0`). The DB member_count column
        # is intentionally NOT touched: there may be legitimately enrolled
        # contacts in MAIN that fell outside our active+main-eligible filter
        # (e.g. paused-status PEs in a main-eligible state); zeroing the
        # counter would lie about them.
        summary["member_count_after"] = 0
        return summary

    result = bulk_add_to_group_with_circuit_breaker(
        glific_ids, group_id, op_label=f"backfill.main.{batch_name}",
    )
    summary["chunks_attempted"] = result["chunks_attempted"]
    summary["chunks_failed"] = result["chunks_failed"]
    summary["added"] = result["added"]
    summary["circuit_tripped"] = result["circuit_tripped"]
    summary["skipped_after_trip"] = result["skipped_after_trip"]
    # Cap to 50 to mirror the helper's cap; keeps the summary compact for a
    # 60K-row operator run.
    summary["errors"].extend(result["errors"][:50])

    # member_count is a denormalized counter; SET (not increment) so a re-run
    # corrects drift rather than doubling. Not a Glific-mapped field — L-039
    # reconcile does not apply.
    frappe.db.set_value(
        "PGCollection", pg_collection_name, "member_count", result["added"],
        update_modified=False,
    )
    frappe.db.commit()
    summary["member_count_after"] = result["added"]

    # Coarse audit log so operators can find this run by title later (L-080:
    # commit before relying on the log being durable; commit above handles it).
    try:
        frappe.log_error(
            message=(
                f"backfill_main_collection: batch={batch_name} bpr={bpr_name} "
                f"group={group_id} candidates={summary['candidates']} "
                f"added={summary['added']} chunks_failed={summary['chunks_failed']} "
                f"circuit_tripped={summary['circuit_tripped']} "
                f"skipped_after_trip={summary['skipped_after_trip']}"
            ),
            title="CR-029 Main Collection Backfill",
        )
        frappe.db.commit()
    except Exception:
        frappe.logger().error(
            f"backfill_main_collection audit log failed: {summary}"
        )

    frappe.logger().info(
        f"backfill_main_collection DONE: batch={batch_name} "
        f"candidates={summary['candidates']} added={summary['added']} "
        f"chunks_failed={summary['chunks_failed']} "
        f"circuit_tripped={summary['circuit_tripped']}"
    )
    return summary
