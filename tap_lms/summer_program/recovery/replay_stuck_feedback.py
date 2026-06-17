"""
CR-026 recovery: replay PEs stuck in `submitted_awaiting_feedback`.

── Why this exists ────────────────────────────────────────────────
The long-running `FeedbackConsumer` (an ad-hoc `scripts/console_consumer.py`
process, NOT supervisor-managed) holds a module-level cache of
`tap_lms.summer_program.utils`. When `safe_sp_api_error_response` was added to
utils after the consumer had already started, any feedback whose processing
reached a code path that first-imports `student_progression_sp` /
`save_submission` / `custom_messages` (each of which top-level-imports that
symbol) raised `ImportError: cannot import name 'safe_sp_api_error_response'`.
That bubbled up through `on_feedback_ready` → `process_feedback_ready` →
`process_message`, the message was NACKed/rejected, and the PE never advanced
out of `submitted_awaiting_feedback` even though its `Submission.status` was
`Completed`.

`on_feedback_ready` works correctly when called from a FRESH process (e.g. a
bench console with current code) — proven by manual replays. This utility
re-fires it for the accumulated backlog.

── Safety model ───────────────────────────────────────────────────
* Scope is REQUIRED to a single Batch — prevents accidental cross-batch replay.
* `dry_run=True` reports what would be replayed and changes nothing.
* A real run requires `i_know_this_is_destructive=True`.
* Each PE is committed independently, so one bad row never aborts the batch.
* Idempotent: `on_feedback_ready` returns `{"status": "no_pe"}` once a PE has
  left `submitted_awaiting_feedback`, so re-running is a no-op for already-fixed
  PEs.

── What this does NOT do ──────────────────────────────────────────
`on_feedback_ready` performs the STATE transition (T12 feedback_ready / T6b
remedial), the CR-007 point award, and the Glific contact-FIELD sync. It does
NOT re-send the student-facing Glific feedback MESSAGE (label="feedback") —
that is the consumer's `trigger_feedback_flow`. This utility unblocks
progression; resending the actual feedback message to students is a separate
operational decision.
"""

import frappe

from tap_lms.summer_program.constants import STATE_SUBMITTED_AWAITING

# Cap a single non-dry-run invocation so an operator can't accidentally fire the
# whole population in one call. Loop the call (or raise limit deliberately) for
# larger backlogs.
DEFAULT_LIMIT = 1000


def _find_stuck(batch_name, limit):
    """Return up to `limit` stuck PEs in `batch_name`, each paired with the most
    recent Completed Submission we can attribute to it.

    Matching is deliberately permissive (PE + student, Completed status); the
    correctness gate lives in `on_feedback_ready`, which re-validates the active
    PE state and the submission's week before transitioning (returning
    "no_pe" / "skipped" otherwise). DISTINCT ON keeps one submission per PE.
    """
    return frappe.db.sql(
        """
        SELECT DISTINCT ON (pe.name)
               pe.name   AS pe_name,
               pe.student AS student,
               s.name    AS submission_name
          FROM "tabProgramEnrollment" pe
          JOIN "tabSubmission" s
            ON s.student_id = pe.student
         WHERE pe.batch = %s
           AND pe.resolved_flow_state = %s
           AND s.status = 'Completed'
           -- Link fields store unset as NULL in Frappe (never ''), so the
           -- permissive branch only needs the IS NULL case.
           AND (s.program_enrollment = pe.name OR s.program_enrollment IS NULL)
           AND (s.week IS NULL
                OR pe.current_week IS NULL
                OR s.week = pe.current_week)
         ORDER BY pe.name, s.modified DESC
         LIMIT %s
        """,
        (batch_name, STATE_SUBMITTED_AWAITING, limit),
        as_dict=True,
    )


def _count_by_branch(processed):
    """Tally transition outcomes by branch (feedback_ready / remedial / …)."""
    from collections import Counter

    c = Counter()
    for p in processed:
        result = p.get("result") or {}
        # status is one of: transitioned / no_pe / skipped / error
        key = result.get("branch") or result.get("status") or "unknown"
        c[key] += 1
    return dict(c)


@frappe.whitelist()
def replay_stuck_submitted_awaiting_feedback(
    batch_name,
    dry_run=True,
    limit=DEFAULT_LIMIT,
    i_know_this_is_destructive=False,
):
    """Re-fire `on_feedback_ready` for PEs stuck in `submitted_awaiting_feedback`.

    Args:
        batch_name: Batch to scope the replay to (REQUIRED).
        dry_run: When truthy (default), report the candidates and change nothing.
        limit: Max PEs to process in one call (default 1000).
        i_know_this_is_destructive: Must be truthy for a non-dry-run replay.

    Returns:
        dry_run  -> {"dry_run": True, "would_replay": N, "sample": [...]}
        real run -> {"processed": N, "failed": M, "transitions": {...},
                     "failures_sample": [...]}
        guard    -> {"error": "..."}
    """
    # Whitelisted args arrive as strings — coerce explicitly.
    dry_run = frappe.utils.sbool(dry_run) if isinstance(dry_run, str) else bool(dry_run)
    i_know_this_is_destructive = (
        frappe.utils.sbool(i_know_this_is_destructive)
        if isinstance(i_know_this_is_destructive, str)
        else bool(i_know_this_is_destructive)
    )
    limit = frappe.utils.cint(limit) or DEFAULT_LIMIT

    # This endpoint mutates production state (state transitions + point awards).
    # The destructive-flag guard only protects against operator mistakes — it
    # does NOT protect against unauthorized HTTP callers, who can set any param.
    # Restrict to admins so a TAP Student/Instructor cannot trigger a replay.
    frappe.only_for(["TAP Admin", "System Manager"])

    if not batch_name:
        return {"error": "batch_name is required"}
    if not frappe.db.exists("Batch", batch_name):
        return {"error": f"Batch {batch_name} not found"}
    if not dry_run and not i_know_this_is_destructive:
        return {
            "error": "Refusing to replay: pass dry_run=True to preview, or "
            "i_know_this_is_destructive=True to actually replay."
        }

    # Start from a clean transaction — never inherit an aborted one (L-030).
    frappe.db.rollback()

    stuck = _find_stuck(batch_name, limit)

    if dry_run:
        return {
            "dry_run": True,
            "batch": batch_name,
            "would_replay": len(stuck),
            "sample": [r["pe_name"] for r in stuck[:5]],
        }

    from tap_lms.summer_program import feedback_consumer_hook

    processed = []
    failed = []
    for r in stuck:
        try:
            result = feedback_consumer_hook.on_feedback_ready(
                r["submission_name"], student_id=r["student"]
            )
            processed.append({"pe": r["pe_name"], "result": result})
            # Commit each success so a later failure can't roll back fixed PEs.
            frappe.db.commit()
        except Exception as e:
            # on_feedback_ready already rolls back + logs internally, but defend
            # the loop anyway so one pathological row never aborts the batch.
            frappe.db.rollback()
            failed.append({"pe": r["pe_name"], "error": str(e)[:300]})
            # Persist the per-row failure durably (the in-loop rollback above
            # would otherwise erase a same-txn log — L-080). Commit so it
            # survives, then continue with a clean txn for the next row.
            try:
                frappe.log_error(
                    message=f"replay_stuck: PE {r['pe_name']} "
                    f"(submission {r['submission_name']}) failed: {str(e)[:500]}",
                    title="CR-026 Replay Failure",
                )
                frappe.db.commit()
            except Exception:
                frappe.db.rollback()

    return {
        "dry_run": False,
        "batch": batch_name,
        "processed": len(processed),
        "failed": len(failed),
        "transitions": _count_by_branch(processed),
        "failures_sample": failed[:10],
    }
