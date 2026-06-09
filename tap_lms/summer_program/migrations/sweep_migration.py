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
    COLLECTION_BATCH_SIZE,
    LABEL_CONTENT_DELIVERED,
    PROGRAM_ACTIVE,
    STATE_NORMAL_CONTENT,
    STATE_NORMAL_ESCALATION,
)
from tap_lms.summer_program.pe_dispatcher import _get_escalation_steps_for_pe
from tap_lms.summer_program.state_machine import transition
from tap_lms.summer_program.glific_extensions import (
    add_contacts_to_group_bulk,
    remove_contacts_from_group_bulk,
)


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
    """
    result = {"removed_from_main": 0, "added_to_escalation": 0, "errors": []}
    if not glific_ids:
        return result

    main_group_id, escalation_group_id = _resolve_collection_group_ids(bpr_name)

    # Phase B1 — remove from main.
    if main_group_id:
        for i in range(0, len(glific_ids), COLLECTION_BATCH_SIZE):
            chunk = glific_ids[i:i + COLLECTION_BATCH_SIZE]
            if remove_contacts_from_group_bulk(chunk, main_group_id):
                result["removed_from_main"] += len(chunk)
            elif len(result["errors"]) < 50:
                result["errors"].append(
                    f"BPR {bpr_name}: remove chunk @{i} from main {main_group_id} failed"
                )
    else:
        result["errors"].append(f"BPR {bpr_name}: no active 'main' PGCollection")

    # Phase B2 — add to escalation.
    if escalation_group_id:
        for i in range(0, len(glific_ids), COLLECTION_BATCH_SIZE):
            chunk = glific_ids[i:i + COLLECTION_BATCH_SIZE]
            if add_contacts_to_group_bulk(chunk, escalation_group_id):
                result["added_to_escalation"] += len(chunk)
            elif len(result["errors"]) < 50:
                result["errors"].append(
                    f"BPR {bpr_name}: add chunk @{i} to escalation "
                    f"{escalation_group_id} failed"
                )
    else:
        result["errors"].append(f"BPR {bpr_name}: no active 'escalation' PGCollection")

    frappe.logger().info(
        f"_bulk_move_to_escalation: BPR={bpr_name} "
        f"removed_from_main={result['removed_from_main']} "
        f"added_to_escalation={result['added_to_escalation']} "
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


def _coerce_bool(value):
    """Coerce a whitelisted-method argument to bool.

    Whitelisted methods called over HTTP receive strings, where the literal
    string "False" / "0" is truthy in Python. Treat the usual falsey spellings
    as False; everything else follows normal Python truthiness.
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none")
    return bool(value)


@frappe.whitelist()
def migrate_behind_students_to_escalation(
    batch_name,
    dry_run=True,
    limit=None,
    i_know_this_is_destructive=False,
):
    """One-time migration: demote behind students to `normal_escalation`.

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
