"""
CR-003 — Per-week grace, escalation channels, parent-call integration.

Source CR: docs/change-requests/CR-003-per-week-grace-escalation-channels-parent-call.md
Tasks: T-03-09 (this patch)

What this patch does
--------------------
1. Backfills EscalationStep.escalation_type from the legacy message_type column.
2. Deletes orphaned tabVoiceAgentMapping rows (the doctype itself is dropped via
   JSON removal + bench migrate; this patch clears rows first to avoid FK noise).
3. Migrates legacy PEs in STATE_PAUSED_NO_ACTIVITY -> STATE_PROGRAM_DROPPED with
   a drop_reason of 'reengagement_exhausted' (re_engagement_count >= 3) or
   'grace_expired' (otherwise).
4. Nulls any next_action_type pointing at retired actions (grace_reminder,
   re_engagement) — defensive cleanup in case dispatcher in-flight state lingered.
5. Inserts a ProgramEventLog audit row for each migrated PE.
6. Reconciles each migrated PE's Glific contact fields (L-039).

Schema changes (doctype JSON, NOT done here)
--------------------------------------------
T-03-01..04 cover the DocType UI edits on the bench:
  - EscalationStep.escalation_type (new), .message_type (drop after this patch runs)
  - ParentCallConfig.status_template (rename from prompt_template),
    .call_params_json (drop), .max_duration (drop), .is_active (new)
  - VoiceAgentSettings.agent_id (new), .default_parent_call_config (new),
    .auth_token_cache_ttl (new), .agent_mappings (drop child table)
  - VoiceAgentMapping doctype (delete folder)
  - ProgramEnrollment.re_engagement_count (drop)

This patch is the data side; the schema side is operator + Frappe schema sync.

Idempotency
-----------
Frappe PatchLog gates re-execution. Within-run guards:
  - escalation_type backfill: WHERE escalation_type IS NULL OR escalation_type = ''
  - paused-PE migration: WHERE resolved_flow_state = 'paused_no_activity'
    AND program_status = 'active' (post-run, program_status flips to 'dropped'
    so the WHERE no longer matches)
  - next_action_type cleanup: WHERE next_action_type IN ('grace_reminder',
    're_engagement') (post-run, those values are NULL so WHERE no longer matches)

Postgres notes
--------------
- All table names quoted ("tabX") per skills/frappe-postgres/SKILL.md.
- ProgramEventLog FK column is `enrollment`, NOT `program_enrollment` (L-052).
- `details` is JSON-typed; insert via frappe.get_doc to get correct serialization.
- frappe.db.set_value bypasses Glific contact-field sync (L-039); we follow up
  with reconcile_pe_to_glific for every migrated PE.

L-046 safety
------------
This is not a lazy->eager state-derived backfill; it's a one-way terminal
transition. There is no double-count risk because the source state
(paused_no_activity) and the target state (program_dropped) are mutually
exclusive — a PE cannot be in both. The verification query at the end confirms
zero remaining paused_no_activity rows in program_status='active' state.
"""

import frappe
from frappe.utils import now_datetime


def execute():
    # ── Step 1. Backfill EscalationStep.escalation_type from message_type ──
    # Map: 'help_note_a' -> 'help_note_a', 'help_note_b' -> 'help_note_b',
    # anything else -> 'help_note_a' (safe default; voice_note and parent_call
    # are new escalation types that no historical row would have used).
    #
    # L-059/L-061 guard: `message_type` was DROPPED from EscalationStep JSON
    # as part of CR-003's schema reshape. On migration deploys (servers where
    # the pre-CR-003 schema was live) the column still exists in the DB because
    # Frappe doesn't auto-drop columns. On fresh installs the table itself may
    # not exist (tabEscalationStep is never created). frappe.db.has_column raises
    # TableMissingError when the table doesn't exist (L-061), so we MUST guard
    # with table_exists first. The two-part guard makes this patch idempotent
    # across both deployment shapes — on fresh installs it cleanly skips Step 1.
    escalation_backfill_count = 0
    if (frappe.db.table_exists("EscalationStep")
            and frappe.db.has_column("EscalationStep", "message_type")):
        backfilled = frappe.db.sql(
            """
            UPDATE "tabEscalationStep"
               SET escalation_type = CASE
                       WHEN message_type = 'help_note_a' THEN 'help_note_a'
                       WHEN message_type = 'help_note_b' THEN 'help_note_b'
                       ELSE 'help_note_a'
                   END
             WHERE escalation_type IS NULL OR escalation_type = ''
            RETURNING name
            """
        )
        escalation_backfill_count = len(backfilled or [])

    # ── Step 2. Delete orphaned VoiceAgentMapping rows ─────────────────────
    # The doctype JSON deletion is done out-of-band (T-03-03); this patch
    # ensures no orphaned rows remain when Frappe drops the table.
    voice_agent_rows_deleted = 0
    if frappe.db.table_exists("VoiceAgentMapping"):
        result = frappe.db.sql(
            'DELETE FROM "tabVoiceAgentMapping" RETURNING name'
        )
        voice_agent_rows_deleted = len(result or [])

    # ── Step 3. Migrate paused_no_activity PEs to program_dropped ──────────
    # Per CR-003 §Proposed behavior: the paused_no_activity state is retired.
    # Students drop directly at grace expiry. Existing paused PEs need to be
    # transitioned to the terminal dropped state and emit an audit event.
    # L-059/L-061 guard (same as Step 1): re_engagement_count is DROPPED from
    # the CR-003 ProgramEnrollment JSON. On migration deploys the column still
    # physically exists (Frappe never auto-drops), so we read it. On fresh
    # installs / dev DBs built from the post-CR-003 schema the column was never
    # created, and an unguarded SELECT raises UndefinedColumn and aborts migrate.
    # When the column is absent there is no re-engagement history to read, so
    # every paused PE falls back to re_engagement_count = 0 (-> 'grace_expired').
    # table_exists FIRST, then has_column — L-061: has_column raises
    # TableMissingError (not False) on a missing table. Mirrors Step 1.
    # reeng_expr is one of two hardcoded literals; no untrusted input.
    reeng_expr = (
        "COALESCE(re_engagement_count, 0)"
        if (frappe.db.table_exists("ProgramEnrollment")
            and frappe.db.has_column("ProgramEnrollment", "re_engagement_count"))
        else "0"
    )
    paused = frappe.db.sql(
        f"""
        SELECT name, student, batch, {reeng_expr} AS re_engagement_count
          FROM "tabProgramEnrollment"
         WHERE resolved_flow_state = 'paused_no_activity'
           AND program_status = 'active'
        """,
        as_dict=True,
    )

    migrated_pes = []
    glific_sync_failed = []
    for pe in paused:
        reason = "reengagement_exhausted" if pe.re_engagement_count >= 3 else "grace_expired"

        # 3a. State transition via set_value (bypasses hook chain per L-039;
        # we explicitly reconcile to Glific below).
        frappe.db.set_value(
            "ProgramEnrollment",
            pe.name,
            {
                "resolved_flow_state": "program_dropped",
                "program_status": "dropped",
                "drop_reason": reason,
                "next_action_at": None,
                "next_action_type": None,
                "journey_label": "dropped",
                "last_label_change_at": now_datetime(),
            },
            update_modified=False,
        )

        # 3b. Audit event in ProgramEventLog.
        # NOTE: the FK column on tabProgramEventLog is `enrollment`, NOT
        # `program_enrollment` (L-052). frappe.get_doc translates DocType
        # field names ('enrollment') correctly regardless.
        # student + batch are reqd:1 on ProgramEventLog and .insert() runs full
        # validation, so both must be set or it raises MandatoryError. They're
        # pulled from the PE row in the Step 3 SELECT above.
        frappe.get_doc(
            {
                "doctype": "ProgramEventLog",
                "enrollment": pe.name,
                "student": pe.student,
                "batch": pe.batch,
                "event_type": "program_dropped",
                "details": {"reason": reason, "source": "CR-003 migration"},
            }
        ).insert(ignore_permissions=True)

        migrated_pes.append((pe.name, reason))

    # 3c. Reconcile each migrated PE's Glific contact fields (L-039).
    # We import lazily because this patch must not import summer_program at
    # module load time (could cause circular import during bench migrate).
    if migrated_pes:
        try:
            from tap_lms.summer_program.dev_tools import reconcile_pe_to_glific

            for pe_name, _reason in migrated_pes:
                try:
                    reconcile_pe_to_glific(pe_name)
                except Exception as exc:
                    glific_sync_failed.append((pe_name, str(exc)))
        except ImportError:
            # If the reconcile helper isn't available, log and continue —
            # the next state transition will sync naturally (L-040 fallback).
            glific_sync_failed = [
                (pe_name, "reconcile_pe_to_glific unavailable at migrate time")
                for pe_name, _ in migrated_pes
            ]

    # ── Step 4. Null retired next_action_type values ───────────────────────
    # Defensive cleanup for in-flight dispatcher state that referenced retired
    # actions. Both grace_reminder and re_engagement are deleted from the
    # DISPATCHERS dict by CR-003 T-03-07; clearing here prevents AttributeError
    # at dispatcher claim time on legacy rows.
    legacy_action_clear = frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET next_action_at = NULL,
               next_action_type = NULL
         WHERE next_action_type IN ('grace_reminder', 're_engagement')
        RETURNING name
        """
    )
    legacy_action_count = len(legacy_action_clear or [])

    frappe.db.commit()

    # ── Step 5. Verification — confirm acceptance criteria ─────────────────
    remaining_paused_active = frappe.db.sql(
        """
        SELECT count(*) FROM "tabProgramEnrollment"
         WHERE resolved_flow_state = 'paused_no_activity'
           AND program_status = 'active'
        """
    )[0][0]

    if remaining_paused_active > 0:
        # CR-003 Acceptance Criterion: "SELECT count(*) ... returns 0".
        # If we get here, something raced or a PE was created mid-migration.
        # Fail loud so the operator notices.
        frappe.log_error(
            title="SP CR-003 migration FAILED — paused PEs remain",
            message=(
                f"After migration: {remaining_paused_active} PEs still in "
                f"resolved_flow_state='paused_no_activity' AND program_status='active'. "
                f"Acceptance criterion violated. Investigate before declaring CR-003 shipped."
            ),
        )
        raise frappe.ValidationError(
            f"CR-003 migration failed: {remaining_paused_active} paused-active "
            f"PEs remain. Re-run after investigation."
        )

    # Quiet-success summary log. Per L-042, Error Log column is `method`/title.
    summary = (
        f"escalation_type backfill: {escalation_backfill_count} rows. "
        f"VoiceAgentMapping rows deleted: {voice_agent_rows_deleted}. "
        f"Paused-PEs migrated to dropped: {len(migrated_pes)}. "
        f"Legacy next_action_type cleared: {legacy_action_count}. "
        f"Remaining paused-active PEs: {remaining_paused_active} (target: 0)."
    )
    if glific_sync_failed:
        summary += (
            f" Glific reconcile failures (will sync on next transition per L-040): "
            f"{len(glific_sync_failed)}; first 5: {glific_sync_failed[:5]!r}"
        )

    frappe.log_error(
        title="SP CR-003 migration complete",
        message=summary,
    )
