"""
Batch Activation & Validation
tap_lms/summer_program/batch_activation.py

Step 4 of the pipeline:
  - Validate that a BatchProgramRun is ready for activation
  - Check collections, flow IDs, enrollment counts
  - Mark as active
  - Seed next_action_at on all PEs (staggered to avoid thundering herd)
"""
import frappe
import json
from frappe.utils import now_datetime, get_datetime, getdate

from tap_lms.summer_program.constants import (
    BPR_COLLECTIONS_READY,
    BPR_ACTIVE,
    VALIDATION_PASSED,
    VALIDATION_FAILED,
    ALL_ARCHETYPES,
    ARM_A,
    ARM_B,
    ACTION_FLOW_FIELD_MAP,
    ACTION_CONTENT_DELIVERY,
    PROGRAM_ACTIVE,
    STATE_NORMAL_CONTENT,
)
from tap_lms.summer_program.utils import staggered_action_time


# Chunk size for the bulk UPDATE in _seed_pe_actions. At 5000 PEs per chunk,
# the bulk UPDATE binds 1 + 2*5000 = 10001 params per query. Postgres's
# protocol parameter limit is 65535, so we have ~6.5x headroom. Do NOT raise
# this past ~30000 without revisiting that limit.
_SEED_CHUNK_SIZE = 5000


@frappe.whitelist()
def validate_bpr(bpr_name):
    """
    Run validation checks on a BatchProgramRun.
    Returns a validation report dict and updates the BPR.

    Checks:
      1. BPR is in collections_ready status
      2. All 8 archetype collections exist (4 archetypes × 2 arms)
      3. At least one flow ID is configured
      4. total_enrolled > 0
      5. Batch has start_date and total_weeks set
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)
    batch = frappe.get_doc("Batch", bpr.batch)

    errors = []
    warnings = []

    # 1. Status check
    if bpr.status != BPR_COLLECTIONS_READY:
        errors.append(
            f"BPR status must be '{BPR_COLLECTIONS_READY}', "
            f"currently '{bpr.status}'"
        )

    # 2. Collection completeness
    existing_labels = {c.collection_label for c in bpr.pg_collections}
    expected_count = len(ALL_ARCHETYPES) * 2  # 4 × 2 = 8
    if len(existing_labels) < expected_count:
        missing = expected_count - len(existing_labels)
        errors.append(
            f"Expected {expected_count} archetype collections, "
            f"found {len(existing_labels)} ({missing} missing)"
        )

    # Check each collection has students
    empty_collections = [
        c.collection_label
        for c in bpr.pg_collections
        if (c.student_count or 0) == 0
    ]
    if empty_collections:
        warnings.append(
            f"{len(empty_collections)} collections have 0 students: "
            f"{', '.join(empty_collections[:3])}{'...' if len(empty_collections) > 3 else ''}"
        )

    # 3. Flow IDs
    configured_flows = {}
    missing_flows = []
    for action_type, field_name in ACTION_FLOW_FIELD_MAP.items():
        flow_id = getattr(bpr, field_name, None)
        if flow_id:
            configured_flows[action_type] = flow_id
        else:
            missing_flows.append(action_type)

    if not configured_flows:
        errors.append("No Glific flow IDs configured on this BPR")
    elif missing_flows:
        warnings.append(
            f"Flows not configured: {', '.join(missing_flows)}"
        )

    # 4. Enrollment count
    if not bpr.total_enrolled or bpr.total_enrolled == 0:
        errors.append("No students enrolled (total_enrolled is 0)")

    # 4b. ProgramEnrollment records exist
    pe_count = frappe.db.count(
        "ProgramEnrollment",
        {"batch": bpr.batch, "program_status": ["!=", "dropped"]},
    )
    if pe_count == 0:
        errors.append(
            "No ProgramEnrollment records found. "
            "Run 'Start Program Enrollment' before activation."
        )
    elif pe_count < (bpr.total_enrolled or 0):
        warnings.append(
            f"Only {pe_count} ProgramEnrollment records for "
            f"{bpr.total_enrolled} enrolled students — "
            f"program enrollment may still be running"
        )

    # 5. Batch configuration
    if not batch.start_date:
        errors.append("Batch has no start_date set")
    if not batch.total_weeks:
        errors.append("Batch has no total_weeks set")

    # Build report
    report = {
        "timestamp": str(now_datetime()),
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "total_imported": bpr.total_imported or 0,
            "total_enrolled": bpr.total_enrolled or 0,
            "program_enrollments": pe_count,
            "collections": len(bpr.pg_collections),
            "configured_flows": configured_flows,
        },
        "passed": len(errors) == 0,
    }

    # Update BPR
    bpr.validation_status = VALIDATION_PASSED if report["passed"] else VALIDATION_FAILED
    bpr.validation_report = json.dumps(report, indent=2)
    bpr.save(ignore_permissions=True)
    frappe.db.commit()

    return report


@frappe.whitelist()
def activate_bpr(bpr_name):
    """
    Activate a validated BatchProgramRun.
    Requires validation_status == 'passed'.

    On activation:
      1. Sets BPR status = active
      2. Seeds next_action_at on ALL PEs for this batch (staggered with jitter)
         so the per-PE dispatcher picks them up for first content delivery.

    Returns:
        dict with success status, message, and seeded_count
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)

    if bpr.validation_status != VALIDATION_PASSED:
        return {
            "success": False,
            "message": "Cannot activate: validation has not passed. Run validate first.",
        }

    if bpr.status == BPR_ACTIVE:
        return {
            "success": False,
            "message": "BPR is already active.",
        }

    batch = frappe.get_doc("Batch", bpr.batch)

    # Task #14 (2026-05-13): hard-fail on per-tuple ArchetypeConfig
    # completeness before flipping the BPR to active. The old always-16
    # invariant was retired (ADR-004 supersession); this replacement scales
    # naturally to however many experiment_arms the batch actually uses.
    # `_validate_archetype_config_before_activation` throws ValidationError
    # with a multi-line list of every error-severity issue when invalid;
    # warnings (e.g., escalation hours > grace window) only show in the
    # admin preview API and don't block activation.
    from tap_lms.summer_program.validators import (
        _validate_archetype_config_before_activation,
    )
    _validate_archetype_config_before_activation(bpr.batch)

    bpr.status = BPR_ACTIVE
    bpr.activated_at = now_datetime()
    bpr.save(ignore_permissions=True)
    frappe.db.commit()

    # CR-005 (2026-05-15): create the 5 kind-keyed PGCollections + Glific
    # groups for this BPR (`main`, `escalation`, `binge_paused`,
    # `program_dropped`, `program_completed`). Idempotent — if a row or
    # group with the same label already exists, reuse it. Replaces the old
    # archetype-keyed PGCollection scheme (deactivated by the migration
    # patch). The first weekly cron tick after activation fires
    # SP_Content_Delivery on the `main` group; no fire-on-activation here.
    _ensure_kind_keyed_pg_collections(bpr)

    # BR-002 (2026-06-03): bulk-populate the kind-keyed collections (in practice
    # just `main` at a fresh activation) with the cohort's contacts. PEs created
    # by start_program_enrollment are inserted DIRECTLY into
    # normal_content_delivery — they never transition INTO it, so
    # maintain_collections never fires for them and `main` would stay empty until
    # students organically transition. BT00000019 (71,240 PEs) shipped with an
    # empty `main` collection on 2026-06-03 and an operator had to bulk-add by
    # hand.
    #
    # Runs on the `long` queue, NOT inline: at 71K contacts this is ~143 Glific
    # bulk calls (~4-5 min) — well past an HTTP timeout. Inline would risk
    # leaving `main` half-populated with status already=active and no retry path.
    # The job is idempotent (member_count SET not incremented; re-adding an
    # existing Glific member is a no-op) so an operator can safely re-enqueue it.
    frappe.enqueue(
        "tap_lms.summer_program.batch_activation._bulk_populate_kind_keyed_collections",
        queue="long",
        timeout=1800,
        bpr_name=bpr.name,
    )

    # CR-005 (locked decision #4, 2026-05-15): NO fire-on-activation for CONTENT
    # DELIVERY. Content delivery is batch-triggered every Tuesday 09:00 IST via
    # `scheduler.weekly_content_delivery_trigger` against the `main` collection.
    # (The bulk-population above is collection MEMBERSHIP, not content delivery —
    # the "no fire-on-activation" comment was always about the latter.)
    #
    # `_seed_pe_actions` is preserved as dead code (parallel to the
    # `handle_content_delivery` preservation pattern in pe_dispatcher.py) — can
    # be used for operator escape-hatch scenarios (e.g., manual catch-up push
    # for a single batch). Call it directly from `bench console` if needed.

    return {
        "success": True,
        "message": f"BatchProgramRun {bpr_name} is now active. "
                   f"Main collection is being populated in the background; "
                   f"first content delivery on next Tuesday 09:00 IST (collection-mode, CR-005).",
        "seeded_count": 0,
    }


def _seed_pe_actions(bpr, batch):
    """
    Seed next_action_at and next_action_type on all PEs for this batch.

    Uses batch.start_date as base time (or now() if start_date is today/past).
    Applies staggered jitter (30-min window) to prevent thundering herd.

    Only seeds PEs that:
      - Are in program_status = 'active'
      - Are in resolved_flow_state = 'normal_content_delivery'
      - Don't already have a next_action_at set (idempotency)
    """
    # Determine base delivery time
    start_date = batch.start_date
    if start_date and getdate(start_date) > getdate(now_datetime()):
        # Batch hasn't started yet — schedule for start_date at 09:00
        base_time = get_datetime(f"{start_date} 09:00:00")
    else:
        # Batch started already or start_date is today — deliver now
        base_time = now_datetime()

    # Get all PEs that need seeding.
    #
    # NOTE: uses raw SQL with `IS NULL` instead of Frappe's `["is", "not set"]`
    # filter. On Postgres, Frappe translates `["is", "not set"]` to
    # `coalesce(col, '') = ''`, which is a type error for timestamp columns
    # (`next_action_at` is `datetime`). MariaDB tolerates the type coercion;
    # Postgres throws `InvalidDatetimeFormat`. Same lesson applies anywhere
    # else we filter timestamp columns for nullness — prefer raw SQL.
    pe_list = frappe.db.sql(
        """
        SELECT name
          FROM "tabProgramEnrollment"
         WHERE batch = %s
           AND program_status = %s
           AND resolved_flow_state = %s
           AND next_action_at IS NULL
        """,
        (bpr.batch, PROGRAM_ACTIVE, STATE_NORMAL_CONTENT),
        as_dict=True,
    )

    if not pe_list:
        return 0

    # Bulk update with staggered times. Single UPDATE per chunk using the
    # Postgres VALUES pattern — at 100K students, the prior per-row set_value
    # loop produced ~200K DB queries (SELECT+UPDATE per PE); this produces
    # ~20 queries (one per 5000-row chunk).
    #
    # Why VALUES instead of CASE WHEN: each PE gets a deterministic per-name
    # jitter (via staggered_action_time), so we can't use a single SET expression.
    # VALUES lets us push the precomputed (pe_name, action_time) pairs to the DB
    # in one round-trip.
    #
    # Postgres-only syntax — see lessons.md (project is Postgres, not MariaDB).
    # We intentionally do NOT cast `v.action_time::timestamp` — Frappe's PG
    # driver binds Python datetime as a native timestamp value, so an explicit
    # cast is redundant and risks parse errors when the binding already-typed.
    # Real-DB execution against PG is the source of truth here; the unit tests
    # below assert structure, and bench run-tests catches any binding issue.
    seeded = 0

    for i in range(0, len(pe_list), _SEED_CHUNK_SIZE):
        chunk = pe_list[i:i + _SEED_CHUNK_SIZE]

        # Precompute the (name, time) tuples for this chunk.
        rows = [
            (pe_row.name, staggered_action_time(base_time, pe_row.name, window_minutes=30))
            for pe_row in chunk
        ]

        # Build the VALUES literal: "(%s, %s), (%s, %s), ..." and a flat
        # parameter list [name1, time1, name2, time2, ...].
        values_sql = ", ".join(["(%s, %s)"] * len(rows))
        flat_params = [item for row in rows for item in row]

        # Single UPDATE per chunk. The action_type is constant; passed first.
        # Idempotency guard: the WHERE clause re-checks `next_action_at IS NULL`
        # so that if two activations race (manual click vs. scheduler, retry-on-
        # timeout, etc.) the second run only touches PEs the first one missed.
        # Without this, a re-entrant call would scramble jitter on already-
        # seeded PEs.
        frappe.db.sql(
            f"""
            UPDATE `tabProgramEnrollment` AS pe
               SET next_action_at = v.action_time,
                   next_action_type = %s
              FROM (VALUES {values_sql}) AS v(name, action_time)
             WHERE pe.name = v.name
               AND pe.next_action_at IS NULL
            """,
            [ACTION_CONTENT_DELIVERY] + flat_params,
        )
        frappe.db.commit()
        seeded += len(chunk)

    return seeded


# ── Auto-activation (daily scheduler hook) ──────────────────


def check_auto_activate():
    """
    Daily scheduler hook: auto-activate BPRs whose batch.start_date is today or past.

    Finds BPRs in 'collections_ready' status with validation_status='passed'
    where batch.start_date <= today. Activates them and seeds PEs.

    Register in hooks.py:
        scheduler_events.daily: tap_lms.summer_program.batch_activation.check_auto_activate
    """
    today = getdate(now_datetime())

    # Find BPRs ready to activate
    candidates = frappe.db.sql(
        """
        SELECT bpr.name AS bpr_name, bpr.batch, b.start_date
        FROM `tabBatchProgramRun` bpr
        JOIN `tabBatch` b ON b.name = bpr.batch
        WHERE bpr.status = %s
          AND bpr.validation_status = %s
          AND b.start_date IS NOT NULL
          AND b.start_date <= %s
        """,
        (BPR_COLLECTIONS_READY, VALIDATION_PASSED, today),
        as_dict=True,
    )

    activated = 0
    for row in candidates:
        try:
            result = activate_bpr(row.bpr_name)
            if result.get("success"):
                activated += 1
                frappe.logger().info(
                    f"Auto-activated BPR {row.bpr_name} for batch {row.batch} "
                    f"(start_date={row.start_date}, seeded={result.get('seeded_count', 0)})"
                )
        except Exception as e:
            frappe.log_error(
                f"Auto-activate error for BPR {row.bpr_name}: {str(e)}",
                "SP Auto Activate",
            )

    return activated


# ── CR-005: kind-keyed PGCollection bootstrap ──

def _ensure_kind_keyed_pg_collections(bpr):
    """CR-005 (2026-05-15): idempotently create the 5 kind-keyed
    PGCollection rows + Glific groups for the BPR.

    PGCollection is a child table (istable=1) embedded under BatchProgramRun;
    the parent BPR is referenced via the standard Frappe `parent` column.
    The 5 kinds are imported from collection_membership.COLLECTION_KINDS so
    one canonical source defines the topology.

    Idempotent:
      - If a child row with (parent=bpr.name, kind) already exists, skip.
      - The Glific group lookup-or-create is handled by
        `create_group_if_missing` — re-runs find the existing group.
    """
    from tap_lms.glific_integration import create_group_if_missing
    from tap_lms.summer_program.collection_membership import COLLECTION_KINDS

    created = 0
    for kind in COLLECTION_KINDS:
        existing = frappe.db.exists(
            "PGCollection",
            {"parent": bpr.name, "kind": kind},
        )
        if existing:
            continue

        label = f"SP_{bpr.batch}_{kind}"
        glific_group_id = create_group_if_missing(
            label,
            description=f"CR-005 {kind} collection for BPR {bpr.name}",
        )
        if not glific_group_id:
            frappe.log_error(
                f"_ensure_kind_keyed_pg_collections: could not create or "
                f"resolve Glific group '{label}' for BPR {bpr.name}",
                "SP Collection Bootstrap",
            )
            continue

        pg_col = frappe.new_doc("PGCollection")
        pg_col.parent = bpr.name
        pg_col.parenttype = "BatchProgramRun"
        pg_col.parentfield = "pg_collections"
        pg_col.kind = kind
        pg_col.collection_label = label
        pg_col.glific_group_id = str(glific_group_id)
        pg_col.member_count = 0
        pg_col.is_active = 1
        pg_col.insert(ignore_permissions=True)
        created += 1

    if created:
        frappe.db.commit()
        frappe.logger().info(
            f"_ensure_kind_keyed_pg_collections: created {created} kind-keyed "
            f"PGCollection rows for BPR {bpr.name}"
        )
    return created


def _bulk_populate_kind_keyed_collections(bpr_name):
    """BR-002 (2026-06-03): bulk-populate each kind-keyed PGCollection with the
    contacts whose current resolved_flow_state maps to that kind.

    Enqueued by `activate_bpr` on the `long` queue so the ~143 Glific bulk calls
    for a 71K cohort run off the request path. Uses `add_contacts_to_group_bulk`
    (one API call per COLLECTION_BATCH_SIZE chunk) for a ~60x speedup over the
    per-PE `_enqueue_group_write` path — the same helper `setup_collections`
    uses for the legacy archetype collections.

    Bucketing mirrors `maintain_collections` exactly — it is STATE-driven, not
    program_status-driven (L-055's distinction):
      - resolved_flow_state in MAIN_ELIGIBLE_STATES  -> 'main'
      - STATE_TO_AUDIT_KIND[resolved_flow_state]      -> that audit kind
    At a fresh activation every PE is in normal_content_delivery, so only 'main'
    is populated; the audit buckets are the correct generalization for an
    activation that runs after some PEs have already transitioned. (NOTE: the
    handoff's draft query filtered program_status IN ('active','paused'), which
    would have excluded program_dropped/program_completed PEs from their own
    audit collections — dropped here to match maintain_collections semantics.)

    Re-run semantics: built for activation (fresh cohort — `main` populated,
    audit buckets empty, every member_count seeded at 0 by
    _ensure_kind_keyed_pg_collections). `member_count` is SET (not incremented)
    to the count actually added, and re-adding an existing Glific member is a
    no-op, so re-running re-adds + re-counts the CURRENTLY-eligible contacts per
    kind without doubling. Note it is an ADD-only resync: it never removes a
    contact from a Glific group and never zeroes a kind it now finds empty (that
    would risk member_count lying about a group still holding members from later
    transitions) — removals/decrements are owned by maintain_collections.

    Per-batch Glific failures are logged and the job continues; if any batch
    failed, the job raises at the end so RQ records the failure (L-056,
    FailedJobRegistry) and an operator can re-run.
    """
    from tap_lms.summer_program.collection_membership import (
        MAIN_ELIGIBLE_STATES,
        STATE_TO_AUDIT_KIND,
    )
    from tap_lms.summer_program.glific_extensions import add_contacts_to_group_bulk
    from tap_lms.summer_program.constants import COLLECTION_BATCH_SIZE

    batch_name = frappe.db.get_value("BatchProgramRun", bpr_name, "batch")
    if not batch_name:
        frappe.log_error(
            message=f"_bulk_populate_kind_keyed_collections: BPR {bpr_name} "
                    f"not found or has no batch",
            title="SP activate_bpr bulk-populate",
        )
        return

    # Kind-keyed collections for this BPR. Read fresh from the DB — the rows were
    # just created by _ensure_kind_keyed_pg_collections and are NOT in any
    # in-memory child list on the BPR doc.
    cols = frappe.db.sql(
        """
        SELECT name, kind, glific_group_id
          FROM "tabPGCollection"
         WHERE parent = %s
           AND COALESCE(is_active, 0) = 1
        """,
        (bpr_name,),
        as_dict=True,
    )
    if not cols:
        # L-035: log the empty-path exit so "job ran but did nothing" is visible
        # — BR-002 itself was a silent empty-collection bug.
        frappe.logger().info(
            f"_bulk_populate_kind_keyed_collections: BPR {bpr_name} has no active "
            f"kind-keyed collections — nothing to populate"
        )
        return

    # All PEs in the batch with a usable Glific contact id. The NULL/empty
    # glific_id filter lives here in SQL (validated on real PG by bench run-tests).
    pes = frappe.db.sql(
        """
        SELECT name, glific_id, resolved_flow_state
          FROM "tabProgramEnrollment"
         WHERE batch = %s
           AND glific_id IS NOT NULL
           AND glific_id != ''
        """,
        (batch_name,),
        as_dict=True,
    )

    # Bucket glific_ids by collection kind (state-driven; mirrors maintain_collections).
    by_kind = {}
    for pe in pes:
        state = pe.get("resolved_flow_state")
        if state in MAIN_ELIGIBLE_STATES:
            by_kind.setdefault("main", []).append(pe["glific_id"])
        audit_kind = STATE_TO_AUDIT_KIND.get(state)
        if audit_kind:
            by_kind.setdefault(audit_kind, []).append(pe["glific_id"])

    failed_batches = 0
    populated = {}
    for col in cols:
        kind = col.get("kind")
        group_id = col.get("glific_group_id")
        glific_ids = by_kind.get(kind, [])
        if not glific_ids or not group_id:
            continue

        added = 0
        for j in range(0, len(glific_ids), COLLECTION_BATCH_SIZE):
            batch_ids = glific_ids[j:j + COLLECTION_BATCH_SIZE]
            if add_contacts_to_group_bulk(batch_ids, group_id):
                added += len(batch_ids)
            else:
                failed_batches += 1
                frappe.log_error(
                    message=(
                        f"BPR={bpr_name} kind={kind} group={group_id} "
                        f"batch_start={j} batch_size={len(batch_ids)} — "
                        f"add_contacts_to_group_bulk returned False"
                    ),
                    title="SP activate_bpr bulk-add failed",
                )

        # member_count is a denormalized counter (Glific is the SSOT). SET to the
        # count actually added so a re-run corrects drift rather than doubling.
        # Not a Glific-mapped field, so set_value needs no reconcile (L-039 N/A).
        frappe.db.set_value(
            "PGCollection", col["name"], "member_count", added,
            update_modified=False,
        )
        frappe.db.commit()
        populated[kind] = added
        frappe.logger().info(
            f"activate_bpr bulk-populated {kind} with {added} contacts "
            f"(group {group_id}, BPR {bpr_name})"
        )

    # L-035: structured completion log on the SUCCESS path too, so an operator
    # can confirm the job did its job (and how much) without a manual query.
    frappe.logger().info(
        f"_bulk_populate_kind_keyed_collections done: BPR={bpr_name} "
        f"total_added={sum(populated.values())} per_kind={populated} "
        f"failed_batches={failed_batches}"
    )

    if failed_batches:
        # L-056: surface terminal failure to RQ so it lands in FailedJobRegistry
        # and an operator can re-run. Successful batches are already committed;
        # the raise does not undo them.
        raise RuntimeError(
            f"_bulk_populate_kind_keyed_collections: {failed_batches} batch(es) "
            f"failed for BPR {bpr_name} — see Error Log 'SP activate_bpr bulk-add failed'"
        )
