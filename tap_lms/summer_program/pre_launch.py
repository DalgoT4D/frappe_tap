"""
Pre-launch gates + operational watchdogs.

Two categories live here:

1. Pre-launch one-shot audits (run via `bench execute` before first cohort).
   These clean up dev/test data and audit for stale scheduler state.
   Idempotent — re-running after the first sweep is a no-op.

2. Recurring watchdogs (registered in hooks.py as cron jobs).
   These run continuously after launch to catch silent-failure conditions
   that don't otherwise surface — e.g., PEs stuck in `feedback_ready` because
   Glific dropped the F5 callback. Watchdogs only LOG; they never auto-fix
   (silent auto-fixes can mask real bugs and produce worse outcomes).

Usage:
    bench --site tap_lms.dev execute \\
        tap_lms.summer_program.pre_launch.audit_and_null_stale_next_action_at

    # Dry-run mode (audit only, no writes):
    bench --site tap_lms.dev execute \\
        tap_lms.summer_program.pre_launch.audit_and_null_stale_next_action_at \\
        --kwargs '{"dry_run": True}'

    # Watchdog can also be run on-demand:
    bench --site tap_lms.dev execute \\
        tap_lms.summer_program.pre_launch.feedback_ready_watchdog
"""
import frappe


def audit_and_null_stale_next_action_at(dry_run=False, stale_days=7):
    """Audit + null stale `next_action_at` values on ProgramEnrollment.

    A `next_action_at` is "stale" if either:
      (a) it points to a moment more than `stale_days` ago — the scheduler
          would otherwise fire all those overdue actions in a thundering herd
          the moment the first cron tick runs, OR
      (b) the PE is in a terminal state (program_completed, program_dropped) —
          terminal PEs shouldn't have any pending scheduled action; the
          presence of one indicates state-machine bookkeeping that never
          got cleaned up.

    Both buckets get audited (count + per-state breakdown logged) and then
    nulled in a single atomic UPDATE.

    `dry_run=True` runs the audit and returns the counts without writing.

    Returns a dict: {
        "stale_overdue": N,         # bucket (a)
        "stale_terminal": N,        # bucket (b)
        "total_nulled": N,          # 0 if dry_run
        "by_state": {state: count, ...},
    }
    """
    # Audit query: counts only, no writes. Postgres-safe — no IN-trap, no
    # list parameters.
    overdue_rows = frappe.db.sql(
        """
        SELECT resolved_flow_state, COUNT(*) AS n
          FROM "tabProgramEnrollment"
         WHERE next_action_at IS NOT NULL
           AND next_action_at < (NOW() - (%s || ' days')::interval)
           AND resolved_flow_state NOT IN ('program_completed', 'program_dropped')
         GROUP BY resolved_flow_state
        """,
        (str(stale_days),),
        as_dict=True,
    )
    terminal_rows = frappe.db.sql(
        """
        SELECT resolved_flow_state, COUNT(*) AS n
          FROM "tabProgramEnrollment"
         WHERE next_action_at IS NOT NULL
           AND resolved_flow_state IN ('program_completed', 'program_dropped')
         GROUP BY resolved_flow_state
        """,
        as_dict=True,
    )

    stale_overdue = sum(r["n"] for r in overdue_rows)
    stale_terminal = sum(r["n"] for r in terminal_rows)
    by_state = {r["resolved_flow_state"]: r["n"] for r in (overdue_rows + terminal_rows)}

    result = {
        "stale_overdue": stale_overdue,
        "stale_terminal": stale_terminal,
        "total_nulled": 0,
        "by_state": by_state,
    }

    if dry_run:
        frappe.logger().info(
            f"audit_and_null_stale_next_action_at (DRY RUN): "
            f"overdue={stale_overdue}, terminal={stale_terminal}, "
            f"by_state={by_state}"
        )
        return result

    # Write: single UPDATE that covers both buckets.
    frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET next_action_at = NULL
         WHERE next_action_at IS NOT NULL
           AND (
                next_action_at < (NOW() - (%s || ' days')::interval)
             OR resolved_flow_state IN ('program_completed', 'program_dropped')
           )
        """,
        (str(stale_days),),
    )
    frappe.db.commit()

    result["total_nulled"] = stale_overdue + stale_terminal

    frappe.logger().info(
        f"audit_and_null_stale_next_action_at: nulled {result['total_nulled']} "
        f"rows (overdue={stale_overdue}, terminal={stale_terminal}). "
        f"By state: {by_state}"
    )
    return result


# ════════════════════════════════════════════════════════════
# Watchdog — feedback_ready stuck PEs (task #56, 2026-05-16)
# ════════════════════════════════════════════════════════════

def feedback_ready_watchdog(stuck_hours=2):
    """Find PEs stuck in `feedback_ready` longer than `stuck_hours`.

    Normal flow: PE enters `feedback_ready` when the FeedbackConsumer pipeline
    runs `on_feedback_ready` → `t12_feedback_ready`. Glific then fires the F5
    flow which sends the feedback to the student; on student acknowledgement
    Glific's webhook calls `update_flow_status(action="feedback_complete")`,
    which transitions the PE to `week_completed`.

    Stuck scenario: F5 callback never arrives — Glific dropped the message,
    the student blocked the bot, the webhook delivery failed silently, etc.
    The PE stays in `feedback_ready` indefinitely; downstream week-advancement
    never fires.

    This watchdog LOGS the stuck PEs as a structured Error Log entry so
    operators can replay the F5 flow manually. It does NOT auto-transition —
    silently advancing the student to `week_completed` would skip feedback
    delivery (the whole reason `feedback_ready` exists). Operator action:
    either retrigger F5 via Glific UI, or call `update_flow_status` with
    action='feedback_complete' from a bench shell.

    Registered as an hourly cron in hooks.py.

    Args:
        stuck_hours: Hours since last `modified` before a PE counts as stuck.
                     Default 2 hours — feedback round-trip should be <30 min
                     in healthy state, so 2h is a clear anomaly without
                     false-positive spam in the first hour.

    Returns: {"stuck_count": N, "stuck_pes": [pe_name, ...]}
    """
    rows = frappe.db.sql(
        """
        SELECT name, student, batch, modified
          FROM "tabProgramEnrollment"
         WHERE resolved_flow_state = 'feedback_ready'
           AND program_status IN ('active', 'paused')
           AND modified < (NOW() - (%s || ' hours')::interval)
        """,
        (str(stuck_hours),),
        as_dict=True,
    )
    if not rows:
        return {"stuck_count": 0, "stuck_pes": []}

    # One log entry per stuck PE for actionability — operators can grep by PE
    # name. Bundling them into a single log entry would lose the individual
    # search hit.
    for row in rows:
        frappe.log_error(
            title="SP Feedback Watchdog — F5 callback likely dropped",
            message=(
                f"PE {row['name']} (student={row['student']}, "
                f"batch={row['batch']}) has been in feedback_ready since "
                f"{row['modified']} ({stuck_hours}h+ ago). The Glific F5 "
                f"callback (update_flow_status action='feedback_complete') "
                f"likely failed silently.\n\n"
                f"Operator action options:\n"
                f"  1. Retrigger F5 flow for this contact via Glific UI.\n"
                f"  2. Manually call update_flow_status with action="
                f"'feedback_complete' to nudge the state machine.\n"
                f"This watchdog does NOT auto-transition — silently moving "
                f"the student forward would skip feedback delivery."
            ),
        )

    return {"stuck_count": len(rows), "stuck_pes": [r["name"] for r in rows]}
