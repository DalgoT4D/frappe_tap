"""
SP Collection Membership — state-driven Glific group writes.

CR-005 (2026-05-15) — Approach B: every state-machine transition calls
`maintain_collections(pe, from_state, to_state)` AFTER `pe.save()`. The
helper decides BOTH audit-collection and main-collection writes based on
the state delta. The weekly Tuesday cron only fires the flow — no
recompute, no reconcile.

Each Batch Program Run owns 5 PGCollection (kind-keyed) rows + 5 Glific
groups (`main`, `escalation`, `binge_paused`, `program_dropped`,
`program_completed`). PGCollection is a child table embedded under
BatchProgramRun; the parent BPR is referenced via the standard Frappe
child-table `parent` column.

All Glific calls go through `frappe.enqueue` with retry + DLQ per P-007.
The helper is idempotent — re-running with the same args is a no-op.
"""
import frappe

# 5 main-eligible states (locked decision #3 of CR-005)
MAIN_ELIGIBLE_STATES = {
    "normal_content_delivery",
    "submitted_awaiting_feedback",
    "feedback_ready",
    "week_completed",
    "remedial_content_delivery",
}

# State → audit kind mapping. None means no audit collection for this state.
STATE_TO_AUDIT_KIND = {
    "normal_escalation":   "escalation",
    "remedial_escalation": "escalation",
    "paused_binge":        "binge_paused",
    "program_completed":   "program_completed",
    "program_dropped":     "program_dropped",
    # grace_waiting → None (Gap-fill A per CR-005)
    # All 5 main-eligible states → None
}

# The 5 kinds, in canonical order (used by activate_bpr + migration patch).
COLLECTION_KINDS = (
    "main",
    "escalation",
    "binge_paused",
    "program_dropped",
    "program_completed",
)


def maintain_collections(pe, from_state, to_state):
    """Called from every state-machine transition after `pe.save()`.

    Decides BOTH audit and main collection writes from the state delta.
    Idempotent: re-running with the same args (or with from_state==to_state)
    is a no-op.

    Args:
        pe: ProgramEnrollment doc (needs `name`, `glific_id`, `batch`).
        from_state: PE's resolved_flow_state BEFORE the transition. May be
                    None for the initial T0 enrollment (treated as not
                    main-eligible and no audit kind).
        to_state:   PE's resolved_flow_state AFTER the transition.
    """
    if not pe.glific_id:
        return  # No Glific contact = no group membership to maintain

    from_main = (from_state in MAIN_ELIGIBLE_STATES) if from_state else False
    to_main = to_state in MAIN_ELIGIBLE_STATES
    from_audit = STATE_TO_AUDIT_KIND.get(from_state) if from_state else None
    to_audit = STATE_TO_AUDIT_KIND.get(to_state)

    # Main collection delta
    if from_main and not to_main:
        _enqueue_group_write(pe, kind="main", action="remove")
    elif to_main and not from_main:
        _enqueue_group_write(pe, kind="main", action="add")
    # Both main or both not-main → no main write

    # Audit collection delta
    if from_audit != to_audit:
        if from_audit:
            _enqueue_group_write(pe, kind=from_audit, action="remove")
        if to_audit:
            _enqueue_group_write(pe, kind=to_audit, action="add")
    # Same audit (or both None) → no audit write


def _enqueue_group_write(pe, kind, action):
    """Enqueue an add/remove Glific group call via P-007 retry+DLQ.

    Looks up the kind-keyed PGCollection for the PE's batch's active BPR.
    If the collection isn't yet created (mid-migration), silently skip —
    the migration patch will backfill on its sweep.
    """
    col = _get_pg_collection_by_kind(pe.batch, kind)
    if not col or not col.get("glific_group_id"):
        return  # collection not yet created — skip silently

    frappe.enqueue(
        "tap_lms.summer_program.collection_membership._group_write_job",
        queue="short",
        timeout=30,
        enqueue_after_commit=True,
        glific_group_id=str(col["glific_group_id"]),
        contact_id=str(pe.glific_id),
        action=action,
        pe_name=pe.name,
        pg_collection_name=col["name"],
        retry_count=0,
    )


def _group_write_job(
    glific_group_id,
    contact_id,
    action,
    pe_name,
    pg_collection_name=None,
    retry_count=0,
):
    """Background worker — actual Glific API call with retry + DLQ (P-007).

    Failures re-enqueue up to GLIFIC_SYNC_MAX_RETRIES; exhausted retries
    land in the DLQ log so operators can replay manually.

    On successful API call, atomically maintains `member_count` on the
    PGCollection row (CR-005 follow-up 2026-05-16): +1 on add,
    GREATEST(0, count - 1) on remove. The GREATEST guard prevents the
    counter from going negative if a remove fires for a contact that
    was never in the group (idempotent on the Glific side, but our
    local counter shouldn't lie about it).

    `pg_collection_name` is optional — old in-flight jobs enqueued before
    this kwarg landed will pass None, in which case we skip the counter
    update. They still get retry+DLQ behaviour on the Glific call itself.
    """
    from tap_lms.summer_program.constants import (
        GLIFIC_SYNC_MAX_RETRIES,
        GLIFIC_SYNC_DLQ_LOG_TITLE,
    )
    from tap_lms.glific_integration import (
        add_contact_to_group,
        remove_contact_from_group,
    )

    try:
        if action == "add":
            add_contact_to_group(contact_id, glific_group_id)
        elif action == "remove":
            remove_contact_from_group(contact_id, glific_group_id)

        # Glific call succeeded — maintain member_count atomically.
        # Skipped on old in-flight jobs (pg_collection_name=None).
        if pg_collection_name:
            _bump_member_count(pg_collection_name, action)

    except Exception as e:
        retry_count = (retry_count or 0) + 1
        if retry_count <= GLIFIC_SYNC_MAX_RETRIES:
            try:
                frappe.enqueue(
                    "tap_lms.summer_program.collection_membership._group_write_job",
                    queue="short",
                    timeout=30,
                    glific_group_id=glific_group_id,
                    contact_id=contact_id,
                    action=action,
                    pe_name=pe_name,
                    pg_collection_name=pg_collection_name,
                    retry_count=retry_count,
                )
            except Exception as enqueue_err:
                frappe.log_error(
                    f"Collection membership DLQ (double-fault): "
                    f"action={action}, group={glific_group_id}, "
                    f"contact={contact_id}, pe={pe_name}, "
                    f"retries={retry_count}, error={e}, "
                    f"enqueue_err={enqueue_err}",
                    GLIFIC_SYNC_DLQ_LOG_TITLE,
                )
        else:
            frappe.log_error(
                f"Collection membership DLQ: action={action}, "
                f"group={glific_group_id}, contact={contact_id}, "
                f"pe={pe_name}, retries={retry_count}, error={e}",
                GLIFIC_SYNC_DLQ_LOG_TITLE,
            )


def _bump_member_count(pg_collection_name, action):
    """Atomically maintain PGCollection.member_count.

    Single-statement UPDATE so two concurrent workers can't clobber each
    other. `GREATEST(0, ...)` on remove guards against the counter going
    negative if Glific reports success for a remove of a non-member.

    NOTE: `member_count` is a denormalized counter, not the source of truth.
    Glific itself is the SSOT for group membership. We maintain this column
    so the weekly cron can cheaply skip empty BPRs without an API round-trip.
    If it ever drifts by ±1 due to a partial failure, that's expected — the
    cron's `member_count > 0` check still gates correctly.
    """
    if action == "add":
        frappe.db.sql(
            """
            UPDATE "tabPGCollection"
               SET member_count = COALESCE(member_count, 0) + 1
             WHERE name = %s
            """,
            (pg_collection_name,),
        )
    elif action == "remove":
        frappe.db.sql(
            """
            UPDATE "tabPGCollection"
               SET member_count = GREATEST(0, COALESCE(member_count, 0) - 1)
             WHERE name = %s
            """,
            (pg_collection_name,),
        )


def _get_pg_collection_by_kind(batch_name, kind):
    """Look up the kind-keyed PGCollection for the given batch's active BPR.

    PGCollection is a child table embedded under BatchProgramRun. The
    standard Frappe child-table `parent` column holds the BPR name; we
    join via `parent` against the BPR's status.
    """
    bpr_name = _get_active_bpr(batch_name)
    if not bpr_name:
        return None

    rows = frappe.db.sql(
        """
        SELECT name, glific_group_id, collection_label, member_count
          FROM "tabPGCollection"
         WHERE parent = %s
           AND kind = %s
           AND COALESCE(is_active, 0) = 1
         LIMIT 1
        """,
        (bpr_name, kind),
        as_dict=True,
    )
    return rows[0] if rows else None


def _get_active_bpr(batch_name):
    """Return the active BPR name for a batch, or None."""
    if not batch_name:
        return None
    return frappe.db.get_value(
        "BatchProgramRun",
        {"batch": batch_name, "status": "active"},
        "name",
    )
