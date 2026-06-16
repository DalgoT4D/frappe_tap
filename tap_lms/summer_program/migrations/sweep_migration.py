"""CR-027 — one-time backlog migration + shared "behind-student demotion" helpers.

Context (2026-06-09, post-TLM25 launch on May 28):
  The launch fires `content_delivery_flow` against each archetype × arm
  collection ONCE. The state machine then drives students through weeks
  organically. But students who never engaged with week 1 stay in
  `normal_content_delivery` at `current_week = 1` forever — the calendar
  advances, yet nothing in the state machine pushes them anywhere. As of
  2026-06-09 that backlog is 63,449 PEs on BT00000019.

This module demotes those "behind" students into `normal_escalation` so they
receive the escalation treatment. The escalation parameters (`escalation_type`,
`hours_after_previous`) are resolved per-PE from ArchetypeConfig via
`_get_escalation_steps_for_pe` — NEVER hardcoded — so a sweep-demoted student
is indistinguishable from a dispatcher-demoted one.

── SCALE DECISION (locked 2026-06-09, handover) ──────────────────────────────
For a batch transition of this size the demotion does NOT go through the
high-level T-function `t2_start_escalation`, because that path always enqueues
TWO per-PE Glific jobs (contact-field sync + collection add/remove). At 62K PEs
that is ~124K background jobs ≈ 5 hours to drain, and risks CR-025 (401 token)
failures mid-run.

Instead this module calls the state machine's `transition(...)` DIRECTLY with
`skip_glific=True`, which:
  - performs the PE field UPDATE,
  - logs the transition to ProgramEventLog (audit preserved),
  - SKIPS `_enqueue_contact_field_sync` (per-PE Glific updateContact),
  - SKIPS `maintain_collections` (per-PE Glific group add/remove).

After all transitions commit, the Glific collection move is done in BULK:
  - `remove_contacts_from_group_bulk` (main collection), 500-batch
  - `add_contacts_to_group_bulk` (escalation collection), 500-batch
→ ~124 API calls per group ≈ minutes, not hours.

ACCEPTABLE TRADE-OFF: contact fields (`current_escalation_type`, …) lag — they
catch up on the student's next organic transition (e.g. T4 escalation step 2).
Routing still works because escalation_flow routes off collection membership,
which IS synced in bulk. See L-081.

`_demote_behind_pe` (per-candidate transition) and `_bulk_move_to_escalation`
(per-batch Glific move) are SHARED with `scheduler.weekly_content_sweep` Phase 1
— one implementation, not copy-pasted (anti-drift, L-074).
"""
import time

import frappe
from frappe.utils import add_to_date, now_datetime

from tap_lms.summer_program.constants import (
    ACTION_ESCALATION,
    BPR_ACTIVE,
    LABEL_CONTENT_DELIVERED,
    PROGRAM_ACTIVE,
    STATE_NORMAL_CONTENT,
    STATE_NORMAL_ESCALATION,
)
from tap_lms.summer_program.pe_dispatcher import _get_escalation_steps_for_pe
from tap_lms.summer_program.state_machine import transition


# Default escalation_type when an ArchetypeConfig step somehow has none — the
# exact same literal the live dispatcher falls back to (pe_dispatcher.py:361).
DEFAULT_ESCALATION_TYPE = "help_note_a"
# Default wait before the next escalation step, mirroring the dispatcher and
# the step-dict default produced by _get_escalation_steps (24h).
DEFAULT_NEXT_HOURS = 24


def _pick_first_escalation_step(steps):
    """Return the step config with `escalation_order == 1`.

    Mirrors the dispatcher's selection loop (pe_dispatcher.py:351-357), but
    pinned to the FIRST step (order 1) because a freshly-demoted PE always
    starts the chain at step 1. Falls back to the first list element if no
    step is explicitly tagged order 1 (defensive — same shape the dispatcher
    uses when int() coercion fails).
    """
    for s in steps:
        try:
            if int(s.get("escalation_order")) == 1:
                return s
        except (TypeError, ValueError):
            # A bad escalation_order on THIS step shouldn't prevent finding a
            # later step legitimately tagged order 1 — keep scanning, then
            # fall back to the first element.
            continue
    return steps[0]


def _demote_behind_pe(pe_doc, trigger_source):
    """Demote one behind PE into `normal_escalation` via `transition(...,
    skip_glific=True)` — NOT t2_start_escalation (see module docstring).

    Builds T2's exact updates dict (state_machine.py:553-559) and resolves
    escalation params from ArchetypeConfig the same way the dispatcher does.
    Does NOT commit and does NOT touch Glific — the caller owns the commit and
    the bulk collection move.

    Returns a (status, detail, glific_id) tuple:
      ("processed", None, <glific_id or None>)  — transitioned
      ("no_config", message, None)              — no ArchetypeConfig steps

    Raises on any unexpected error so the caller can rollback + record it.
    """
    steps = _get_escalation_steps_for_pe(pe_doc)
    if not steps:
        return (
            "no_config",
            f"{pe_doc.name}: no escalation_steps in ArchetypeConfig for "
            f"({pe_doc.experiment_arm}, {pe_doc.archetype}, {pe_doc.current_path})",
            None,
        )

    step_config = _pick_first_escalation_step(steps)
    escalation_type = step_config.get("escalation_type") or DEFAULT_ESCALATION_TYPE
    next_hours = float(step_config.get("hours_after_previous", DEFAULT_NEXT_HOURS))

    # T2's exact updates dict (mirror state_machine.t2_start_escalation:553-559).
    updates = {
        "current_escalation_step": 1,
        "current_escalation_type": escalation_type,
        "journey_label": LABEL_CONTENT_DELIVERED,
        "next_action_at": add_to_date(now_datetime(), hours=next_hours),
        "next_action_type": ACTION_ESCALATION,
    }
    transition(
        pe_doc,
        STATE_NORMAL_ESCALATION,
        trigger_source,
        updates,
        skip_glific=True,  # ← CRITICAL: no per-PE Glific jobs; bulk move handles it
    )
    # transition() reloads pe_doc, so glific_id is current here.
    return ("processed", None, pe_doc.glific_id or None)


def _resolve_collection_group_ids(bpr_name):
    """Resolve (main_group_id, escalation_group_id) for a BPR's active
    PGCollection rows. Either may be None if not configured."""
    main_group_id = frappe.db.get_value(
        "PGCollection",
        {"parent": bpr_name, "kind": "main", "is_active": 1},
        "glific_group_id",
    )
    escalation_group_id = frappe.db.get_value(
        "PGCollection",
        {"parent": bpr_name, "kind": "escalation", "is_active": 1},
        "glific_group_id",
    )
    return main_group_id, escalation_group_id


def _bulk_move_to_escalation(bpr_name, glific_ids):
    """Bulk-move transitioned contacts from the `main` collection to the
    `escalation` collection for a BPR — in COLLECTION_BATCH_SIZE chunks.

    Replaces the ~2 per-PE Glific jobs that `maintain_collections` would have
    enqueued. Returns a dict with per-op counts + a sample of errors. Each
    chunk failure is logged but does not abort the rest (partial progress is
    better than none; a re-run is safe because group membership is idempotent).

    M-1 (2026-06-15): both halves now go through the shared circuit-breaker
    helpers (`bulk_remove_from_group_with_circuit_breaker` for Phase B1,
    `bulk_add_to_group_with_circuit_breaker` for Phase B2) so a sustained
    Glific 401/500 cascade trips the breaker instead of burning the rest of
    the run. The TODO from the CR-029 ship is closed.
    """
    from tap_lms.summer_program.glific_extensions import (
        bulk_add_to_group_with_circuit_breaker,
        bulk_remove_from_group_with_circuit_breaker,
    )

    result = {
        "removed_from_main": 0,
        "added_to_escalation": 0,
        "errors": [],
        "remove_circuit_tripped": False,
        "add_circuit_tripped": False,
    }
    if not glific_ids:
        return result

    main_group_id, escalation_group_id = _resolve_collection_group_ids(bpr_name)

    # Phase B1 — remove from main (with circuit breaker).
    if main_group_id:
        remove_summary = bulk_remove_from_group_with_circuit_breaker(
            glific_ids, main_group_id,
            op_label=f"sweep_migration.remove[BPR={bpr_name}]",
        )
        result["removed_from_main"] = remove_summary["removed"]
        result["remove_circuit_tripped"] = remove_summary["circuit_tripped"]
        for err in remove_summary["errors"][:50 - len(result["errors"])]:
            result["errors"].append(f"BPR {bpr_name}: {err}")
    else:
        result["errors"].append(f"BPR {bpr_name}: no active 'main' PGCollection")

    # Phase B2 — add to escalation (with circuit breaker).
    if escalation_group_id:
        add_summary = bulk_add_to_group_with_circuit_breaker(
            glific_ids, escalation_group_id,
            op_label=f"sweep_migration.add[BPR={bpr_name}]",
        )
        result["added_to_escalation"] = add_summary["added"]
        result["add_circuit_tripped"] = add_summary["circuit_tripped"]
        for err in add_summary["errors"][:50 - len(result["errors"])]:
            result["errors"].append(f"BPR {bpr_name}: {err}")
    else:
        result["errors"].append(f"BPR {bpr_name}: no active 'escalation' PGCollection")

    frappe.logger().info(
        f"_bulk_move_to_escalation: BPR={bpr_name} "
        f"removed_from_main={result['removed_from_main']} "
        f"added_to_escalation={result['added_to_escalation']} "
        f"remove_tripped={result['remove_circuit_tripped']} "
        f"add_tripped={result['add_circuit_tripped']} "
        f"errors={len(result['errors'])}"
    )
    return result


def find_behind_candidates(batch_name, calendar_week, limit=None):
    """Behind students for a batch: in `normal_content_delivery`, at a
    `current_week` strictly less than the batch's calendar week, with no
    weekly video done yet.

    - `resolved_flow_state = 'normal_content_delivery'` naturally excludes
      terminal states (program_completed / program_dropped), escalation,
      grace, paused, etc.
    - `current_week < calendar_week` excludes both on-track (==) and ahead (>).
    - `COALESCE(weekly_video_done, 0) = 0` excludes anyone who already engaged.

    Ordered by `name` so a `limit`-capped run is deterministic and a later
    full run is a strict superset.
    """
    sql = """
        SELECT name, glific_id
          FROM "tabProgramEnrollment"
         WHERE batch = %(batch)s
           AND program_status = %(active)s
           AND resolved_flow_state = %(state)s
           AND current_week < %(calendar_week)s
           AND COALESCE(weekly_video_done, 0) = 0
         ORDER BY name
    """
    params = {
        "batch": batch_name,
        "active": PROGRAM_ACTIVE,
        "state": STATE_NORMAL_CONTENT,
        "calendar_week": calendar_week,
    }
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = int(limit)

    return frappe.db.sql(sql, params, as_dict=True)


# CR-029 review (L-074): the prior inline `_coerce_bool` here was copy-pasted
# into main_collection_backfill.py — extracted to the package-level
# `coerce_bool` so a third migration module cannot copy it a third time.
# Local alias preserved so existing call sites in this file (and any external
# importer of sweep_migration._coerce_bool) don't break.
from tap_lms.summer_program.migrations import coerce_bool as _coerce_bool


@frappe.whitelist()
def migrate_behind_students_to_escalation(
    batch_name,
    dry_run=True,
    limit=None,
    i_know_this_is_destructive=False,
):
    """One-time migration: demote behind students to `normal_escalation`.

    ⚠️ SUPERSEDED for large batches — this is the PER-PE loop variant, which
    crawled at ~1 PE/min on the 62K backlog under live DB contention (L-082).
    Use `migrate_behind_students_to_escalation_bulk` (set-based, seconds) for
    anything beyond a small/limited run. This per-PE version is kept only
    because it produces a real per-PE ProgramEventLog row per transition (full
    audit), which the bulk path trades away.

    Behind = `normal_content_delivery` with `current_week < calendar_week`
    and `weekly_video_done = 0` for the given batch.

    Phase A — per-PE state transition via `transition(..., skip_glific=True)`
              (NOT t2_start_escalation), escalation params resolved per-PE from
              ArchetypeConfig. Per-row commit. Collects glific_ids.
    Phase B — ONE bulk Glific collection move (remove from main, add to
              escalation) in 500-batches. See module docstring for why.

    Contact-field sync is INTENTIONALLY skipped (catches up on next organic
    transition; routing works via collection membership). See L-081.

    Args:
        batch_name: Batch doc name to migrate (e.g. 'BT00000019').
        dry_run: If True (default), report counts without changes.
        limit: Cap on PEs processed (None = unlimited).
        i_know_this_is_destructive: Must be True for a non-dry-run.

    Returns:
        dict with processed / failed / no_config / candidates counts, the
        bulk-move result, and sample errors.
    """
    # This endpoint demotes production PEs (state transitions + Glific moves).
    # The destructive-flag guard only protects against operator mistakes — it
    # does NOT stop an unauthorized HTTP caller who can set any param. Restrict
    # to admins so a TAP Student/Instructor cannot trigger a 63K demotion.
    # Mirrors recovery/replay_stuck_feedback.py:131.
    frappe.only_for(["TAP Admin", "System Manager"])

    dry_run = _coerce_bool(dry_run)
    destructive_ok = _coerce_bool(i_know_this_is_destructive)
    if limit is not None:
        limit = int(limit)

    if not dry_run and not destructive_ok:
        raise frappe.ValidationError(
            "Refusing to run a non-dry-run migration without "
            "i_know_this_is_destructive=True. Pass dry_run=True to preview, "
            "or i_know_this_is_destructive=True to actually demote students."
        )

    batch = frappe.get_doc("Batch", batch_name)
    calendar_week = batch.current_calendar_week
    if not calendar_week:
        raise frappe.ValidationError(
            f"Batch {batch_name} has no current_calendar_week set — cannot "
            f"determine which students are behind."
        )

    bpr_name = frappe.db.get_value(
        "BatchProgramRun", {"batch": batch_name, "status": BPR_ACTIVE}, "name"
    )

    candidates = find_behind_candidates(batch_name, calendar_week, limit=limit)
    total = len(candidates)

    summary = {
        "batch": batch_name,
        "bpr": bpr_name,
        "calendar_week": calendar_week,
        "dry_run": dry_run,
        "candidates": total,
        "processed": 0,
        "failed": 0,
        "no_config": 0,
        "bulk_move": None,
        "errors": [],
    }

    frappe.logger().info(
        f"migrate_behind_students_to_escalation: batch={batch_name} "
        f"bpr={bpr_name} calendar_week={calendar_week} candidates={total} "
        f"dry_run={dry_run} limit={limit}"
    )

    if total == 0:
        return summary

    transitioned_glific_ids = []
    start = time.monotonic()
    for idx, candidate in enumerate(candidates, start=1):
        pe_name = candidate["name"]
        try:
            pe_doc = frappe.get_doc("ProgramEnrollment", pe_name)

            if dry_run:
                # Resolve config read-only so the preview splits WOULD-process
                # vs WOULD-skip — but never transition, never commit, never
                # touch Glific.
                steps = _get_escalation_steps_for_pe(pe_doc)
                if not steps:
                    summary["no_config"] += 1
                    _record_error(
                        summary,
                        f"{pe_name}: no escalation_steps in ArchetypeConfig for "
                        f"({pe_doc.experiment_arm}, {pe_doc.archetype}, "
                        f"{pe_doc.current_path})",
                    )
                else:
                    summary["processed"] += 1
                    if pe_doc.glific_id:
                        transitioned_glific_ids.append(pe_doc.glific_id)
                continue

            status, detail, glific_id = _demote_behind_pe(
                pe_doc, trigger_source="sweep_migration"
            )
            if status == "no_config":
                summary["no_config"] += 1
                _record_error(summary, detail)
                frappe.db.rollback()
            else:
                summary["processed"] += 1
                if glific_id:
                    transitioned_glific_ids.append(glific_id)
                frappe.db.commit()  # per-row commit so a later failure keeps prior work
        except Exception as e:
            summary["failed"] += 1
            frappe.db.rollback()
            _record_error(summary, f"{pe_name}: {e}")
            try:
                frappe.log_error(
                    f"migrate_behind_students_to_escalation: PE {pe_name} failed: {e}",
                    "CR-027 Sweep Migration",
                )
            except Exception:
                frappe.logger().error(
                    f"migrate_behind_students_to_escalation double-fault on {pe_name}: {e}"
                )

        if idx % 100 == 0:
            elapsed = time.monotonic() - start
            rate = idx / elapsed if elapsed > 0 else 0.0
            remaining = total - idx
            eta_sec = (remaining / rate) if rate > 0 else 0.0
            frappe.logger().info(
                f"migrate_behind_students_to_escalation progress: {idx}/{total} "
                f"(processed={summary['processed']} failed={summary['failed']} "
                f"no_config={summary['no_config']}) rate={rate:.1f}/s "
                f"eta={eta_sec / 60:.1f}min"
            )

    # Phase B — bulk Glific collection move (skipped on dry_run).
    if not dry_run and transitioned_glific_ids:
        if bpr_name:
            summary["bulk_move"] = _bulk_move_to_escalation(
                bpr_name, transitioned_glific_ids
            )
            # Respect the 50-entry sample cap (consistent with _record_error).
            room = max(0, 50 - len(summary["errors"]))
            summary["errors"].extend(summary["bulk_move"]["errors"][:room])
        else:
            _record_error(
                summary,
                f"batch {batch_name}: no active BPR — cannot resolve "
                f"main/escalation collections for the bulk move. "
                f"{len(transitioned_glific_ids)} PEs were transitioned but NOT "
                f"moved on Glific; re-run the bulk move once a BPR is active.",
            )

    frappe.logger().info(
        f"migrate_behind_students_to_escalation DONE: batch={batch_name} "
        f"candidates={total} processed={summary['processed']} "
        f"failed={summary['failed']} no_config={summary['no_config']} "
        f"dry_run={dry_run} bulk_move={summary['bulk_move']}"
    )
    return summary


def _record_error(summary, message):
    """Track a sample of error/no_config messages without unbounded growth.

    Always counts (the caller bumps the counters); keeps only the first 50
    messages in the returned summary so a 60k-row run doesn't build a giant
    list in memory or in the operator's console output.
    """
    if len(summary["errors"]) < 50:
        summary["errors"].append(message)


# ════════════════════════════════════════════════════════════
# SET-BASED bulk demotion (the simple, fast alternative)
# Added 2026-06-09 after the per-PE loop version hung at ~1 PE/min under
# live-system DB contention (62K rows × per-row get_doc + 158ms config lookup
# + per-row commit). This version does the state change as ~8 combo-targeted
# SQL UPDATEs (one per distinct experiment_arm × archetype × current_path),
# then the SAME bulk Glific collection move. Runtime: seconds of SQL + minutes
# of Glific, instead of hours.
# ════════════════════════════════════════════════════════════


def _behind_combos(batch_name, calendar_week):
    """Distinct (experiment_arm, archetype, current_path) combos among the
    behind candidates for a batch. There are at most ~8 (2 arms × 2 archetypes
    × 2 paths), so resolving escalation params once per combo collapses the
    62K per-PE ArchetypeConfig lookups to a handful."""
    return frappe.db.sql(
        """
        SELECT COALESCE(experiment_arm, '') AS experiment_arm,
               COALESCE(archetype, '')      AS archetype,
               COALESCE(current_path, '')   AS current_path,
               COUNT(*)                     AS n
          FROM "tabProgramEnrollment"
         WHERE batch = %(batch)s
           AND program_status = %(active)s
           AND resolved_flow_state = %(state)s
           AND current_week < %(calendar_week)s
           AND COALESCE(weekly_video_done, 0) = 0
         GROUP BY 1, 2, 3
         ORDER BY 1, 2, 3
        """,
        {
            "batch": batch_name,
            "active": PROGRAM_ACTIVE,
            "state": STATE_NORMAL_CONTENT,
            "calendar_week": calendar_week,
        },
        as_dict=True,
    )


def _resolve_combo_escalation(batch_name, combo):
    """Resolve (escalation_type, hours) for one combo using a representative
    behind PE + the SAME `_get_escalation_steps_for_pe` the dispatcher uses
    (so the result is identical to what the per-PE path would have produced).

    Returns (escalation_type, hours) or (None, None) if the combo has no
    ArchetypeConfig escalation_steps — caller treats that as no_config and
    leaves those PEs untouched.

    INVARIANT (L-029): `_get_escalation_steps_for_pe` resolves escalation from
    the representative PE's STUDENT (`student.archetype` / `student.experiment_arm`),
    while the demotion UPDATE filters on the PE columns. These agree because
    archetype/experiment_arm are upstream-supplied and set ONCE at enrollment,
    never reassigned by the SP. If a bulk Student correction has run since
    enrollment (so Student ≠ PE copy), use the per-PE migration instead — it
    reads the Student per row.
    """
    # Any active normal_content_delivery PE of this combo works — escalation
    # resolution depends only on (arm, archetype, path), not on current_week or
    # weekly_video_done. `program_status = active` mirrors `_behind_combos` so
    # we never pick a paused/dropped row as the representative. COALESCE
    # matching mirrors the UPDATE filter exactly (empty-string combo value
    # represents a NULL column).
    rep = frappe.db.sql(
        """
        SELECT name FROM "tabProgramEnrollment"
         WHERE batch = %(batch)s
           AND program_status = %(active)s
           AND resolved_flow_state = %(state)s
           AND COALESCE(experiment_arm, '') = %(arm)s
           AND COALESCE(archetype, '')      = %(arch)s
           AND COALESCE(current_path, '')   = %(path)s
         LIMIT 1
        """,
        {
            "batch": batch_name,
            "active": PROGRAM_ACTIVE,
            "state": STATE_NORMAL_CONTENT,
            "arm": combo["experiment_arm"] or "",
            "arch": combo["archetype"] or "",
            "path": combo["current_path"] or "",
        },
    )
    if not rep:
        return (None, None)

    pe_doc = frappe.get_doc("ProgramEnrollment", rep[0][0])
    steps = _get_escalation_steps_for_pe(pe_doc)
    if not steps:
        return (None, None)

    step_config = _pick_first_escalation_step(steps)
    escalation_type = step_config.get("escalation_type") or DEFAULT_ESCALATION_TYPE
    hours = float(step_config.get("hours_after_previous", DEFAULT_NEXT_HOURS))
    return (escalation_type, hours)


def _bulk_demote_batch(batch_name, calendar_week, bpr_name, dry_run=False):
    """SET-BASED demotion of all behind PEs in ONE batch — the single shared
    demotion mechanism (L-074), used by BOTH the one-time whitelisted migration
    AND the weekly sweep's Phase 1.

    Behind = normal_content_delivery, current_week < calendar_week,
    weekly_video_done = 0. The state change is ONE SQL UPDATE per distinct
    (arm, archetype, path) combo (~8 statements) — NOT a per-row Python loop
    (which hung at ~1 PE/min on 62K under live contention; see L-082).
    Escalation params (escalation_type, hours) are resolved ONCE per combo from
    ArchetypeConfig (same `_get_escalation_steps_for_pe` the dispatcher uses).
    `next_action_at` gets ±30min jitter (L-013). Then the SAME chunked bulk
    Glific collection move (`_bulk_move_to_escalation`) runs.

    Trade-offs (deliberate, same posture as L-081):
      - Bypasses `transition()` → no per-PE save hooks / no per-PE
        ProgramEventLog row from THIS step. resolved_flow_state is the visible
        outcome; each PE gets a real per-PE audit row from its NEXT
        dispatcher-driven escalation step within hours.
      - Contact-field sync skipped (routing works via the collection membership
        moved in bulk here).

    Commits internally (before the Glific move, so PE state is durable even if
    Glific fails). On `dry_run` does NO writes — only reports per-combo counts
    + resolved escalation_type.

    Args:
        batch_name: Batch doc name.
        calendar_week: Batch.current_calendar_week (passed in so callers that
                       already loaded the batch don't re-fetch).
        bpr_name: active BPR name for the batch (for the Glific collection IDs);
                  may be None — then PEs are demoted but the bulk move is
                  skipped + flagged.
        dry_run: preview only.

    Returns:
        dict: {candidates, combos, demoted, no_config_pes, bulk_move, errors}.
    """
    combos = _behind_combos(batch_name, calendar_week)
    result = {
        "candidates": sum(c["n"] for c in combos),
        "combos": [],
        "demoted": 0,
        "no_config_pes": 0,
        "bulk_move": None,
        "errors": [],
    }

    changed_glific_ids = []
    label = LABEL_CONTENT_DELIVERED
    action = ACTION_ESCALATION
    user = frappe.session.user

    for combo in combos:
        etype, hours = _resolve_combo_escalation(batch_name, combo)
        combo_report = {
            "experiment_arm": combo["experiment_arm"],
            "archetype": combo["archetype"],
            "current_path": combo["current_path"],
            "candidates": combo["n"],
            "escalation_type": etype,
            "hours_after_previous": hours,
            "demoted": 0,
        }

        if etype is None:
            # No ArchetypeConfig escalation_steps for this combo — leave these
            # PEs untouched; the SP team fixes the config.
            result["no_config_pes"] += combo["n"]
            _record_error(
                result,
                f"no escalation_steps in ArchetypeConfig for "
                f"(arm={combo['experiment_arm']}, archetype={combo['archetype']}, "
                f"path={combo['current_path']}) — {combo['n']} PEs left in "
                f"normal_content_delivery",
            )
            result["combos"].append(combo_report)
            continue

        if dry_run:
            combo_report["demoted"] = combo["n"]  # would-demote count
            result["demoted"] += combo["n"]
            result["combos"].append(combo_report)
            continue

        # One set-based UPDATE for the whole combo. RETURNING collects exactly
        # the rows we changed (precise — no guessing which normal_escalation
        # rows were pre-existing). NULL arm/archetype/path matched via COALESCE.
        rows = frappe.db.sql(
            """
            UPDATE "tabProgramEnrollment"
               SET resolved_flow_state   = %(esc_state)s,
                   current_escalation_step = 1,
                   current_escalation_type = %(etype)s,
                   journey_label         = %(label)s,
                   next_action_type      = %(action)s,
                   next_action_at        = NOW()
                                           + (%(hours)s * interval '1 hour')
                                           + (random() * interval '30 minutes'),
                   last_label_change_at  = NOW(),
                   modified              = NOW(),
                   modified_by           = %(user)s
             WHERE batch = %(batch)s
               AND program_status = %(active)s
               AND resolved_flow_state = %(content_state)s
               AND current_week < %(cal)s
               AND COALESCE(weekly_video_done, 0) = 0
               AND COALESCE(experiment_arm, '') = %(arm)s
               AND COALESCE(archetype, '')      = %(arch)s
               AND COALESCE(current_path, '')   = %(path)s
            RETURNING glific_id
            """,
            {
                "esc_state": STATE_NORMAL_ESCALATION,
                "etype": etype,
                "label": label,
                "action": action,
                "hours": hours,
                "user": user,
                "batch": batch_name,
                "active": PROGRAM_ACTIVE,
                "content_state": STATE_NORMAL_CONTENT,
                "cal": calendar_week,
                "arm": combo["experiment_arm"] or "",
                "arch": combo["archetype"] or "",
                "path": combo["current_path"] or "",
            },
            as_dict=True,
        )
        n_changed = len(rows)
        combo_report["demoted"] = n_changed
        result["demoted"] += n_changed
        changed_glific_ids.extend(
            r["glific_id"] for r in rows if r.get("glific_id")
        )
        result["combos"].append(combo_report)
        frappe.logger().info(
            f"_bulk_demote_batch: batch={batch_name} combo "
            f"(arm={combo['experiment_arm']}, archetype={combo['archetype']}, "
            f"path={combo['current_path']}) → {n_changed} demoted, "
            f"escalation_type={etype}"
        )

    if not dry_run:
        frappe.db.commit()  # persist all combo UPDATEs before the Glific move

        if changed_glific_ids:
            if bpr_name:
                result["bulk_move"] = _bulk_move_to_escalation(
                    bpr_name, changed_glific_ids
                )
                room = max(0, 50 - len(result["errors"]))
                result["errors"].extend(result["bulk_move"]["errors"][:room])
            else:
                _record_error(
                    result,
                    f"batch {batch_name}: no active BPR — {len(changed_glific_ids)} "
                    f"PEs demoted but NOT moved on Glific; re-run the bulk move "
                    f"once a BPR is active.",
                )

    return result


@frappe.whitelist()
def migrate_behind_students_to_escalation_bulk(
    batch_name,
    dry_run=True,
    i_know_this_is_destructive=False,
):
    """SET-BASED one-time backlog demotion (operator-run).

    Thin wrapper over the shared `_bulk_demote_batch` — adds the role guard,
    destructive-flag guard, batch/calendar_week/BPR resolution, a coarse audit
    summary to the Error Log, and the summary envelope. The actual demotion
    logic lives in `_bulk_demote_batch` so the one-time migration and the
    weekly sweep's Phase 1 share ONE implementation (L-074).

    Args:
        batch_name: Batch doc name (e.g. 'BT00000019').
        dry_run: If True (default), preview without writing.
        i_know_this_is_destructive: Must be True for a non-dry-run.

    Returns:
        dict summary (batch, bpr, calendar_week, dry_run, candidates, combos,
        demoted, no_config_pes, bulk_move, errors).
    """
    frappe.only_for(["TAP Admin", "System Manager"])

    dry_run = _coerce_bool(dry_run)
    destructive_ok = _coerce_bool(i_know_this_is_destructive)

    if not dry_run and not destructive_ok:
        raise frappe.ValidationError(
            "Refusing to run a non-dry-run bulk migration without "
            "i_know_this_is_destructive=True. Pass dry_run=True to preview."
        )

    batch = frappe.get_doc("Batch", batch_name)
    calendar_week = batch.current_calendar_week
    if not calendar_week:
        raise frappe.ValidationError(
            f"Batch {batch_name} has no current_calendar_week set."
        )

    bpr_name = frappe.db.get_value(
        "BatchProgramRun", {"batch": batch_name, "status": BPR_ACTIVE}, "name"
    )

    core = _bulk_demote_batch(batch_name, calendar_week, bpr_name, dry_run=dry_run)
    summary = {
        "batch": batch_name,
        "bpr": bpr_name,
        "calendar_week": calendar_week,
        "dry_run": dry_run,
        **core,
    }

    if not dry_run:
        # Coarse audit: one Error Log summary (per-PE audit rows arrive from
        # each PE's next dispatcher-driven escalation step). L-080: log AFTER
        # the commit (inside _bulk_demote_batch) so the record survives.
        try:
            frappe.log_error(
                f"bulk demotion: batch={batch_name} demoted={summary['demoted']} "
                f"no_config_pes={summary['no_config_pes']} "
                f"bulk_move={summary['bulk_move']}",
                "CR-027 Bulk Sweep Migration",
            )
            frappe.db.commit()
        except Exception:
            frappe.logger().error(f"bulk demotion summary log failed: {summary}")

    frappe.logger().info(
        f"migrate_behind_students_to_escalation_bulk DONE: batch={batch_name} "
        f"candidates={summary['candidates']} demoted={summary['demoted']} "
        f"no_config_pes={summary['no_config_pes']} dry_run={dry_run} "
        f"bulk_move={summary['bulk_move']}"
    )
    return summary
