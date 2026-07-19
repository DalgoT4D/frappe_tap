"""
Campaign queue processor — background job that places calls.

Called by VoiceCallCampaign.start_calls() via frappe.enqueue.
Processes one Pending row at a time. Checks campaign status before
each call so Pause takes effect immediately after the current call.

L-CP-001: This job runs as a long background task. Never place DB
          operations inside a tight loop without frappe.db.commit()
          between iterations — Postgres will hold the transaction open
          and block other writers.

L-CP-002: Pre-call situation recheck (when enabled): if the student's
          situation changed to a "positive" state (celebration, unknown)
          between queue generation and this call, skip them. They may
          have submitted after the queue was generated.

L-CP-003: After each call (success or failure), write a VoiceCallLog
          record immediately. The webhook updates the outcome later
          but the log gives us a complete audit trail regardless of
          whether the webhook fires.
"""

import time

import frappe
from frappe.utils import now_datetime

# Max retries per queue row before marking Permanently Failed
MAX_RETRIES = 2

# Seconds to wait between consecutive calls (basic rate control)
INTER_CALL_DELAY = 2

# Situations where a call should be skipped (student is fine / positive)
_SKIP_SITUATIONS = {"unknown", "celebration"}


def process_campaign_queue(campaign_name):
    """Entry point. Called by frappe.enqueue from VoiceCallCampaign.start_calls()."""
    try:
        _run(campaign_name)
    except Exception as exc:
        frappe.log_error(
            title="VoiceCallCampaign processor error",
            message=f"Campaign {campaign_name}: {exc}",
        )
        frappe.db.set_value("VoiceCallCampaign", campaign_name, "status", "Error")
        frappe.db.commit()


def _run(campaign_name):
    campaign = frappe.get_doc("VoiceCallCampaign", campaign_name)

    if campaign.status != "Running":
        return

    from tap_lms.summer_program.vocallabs import (
        initiate_parent_call, _get_voice_agent_settings,
    )
    from tap_lms.summer_program.voice_context import build_student_context, check_call_window

    settings = _get_voice_agent_settings()

    for row in campaign.call_queue:
        # ── Check campaign is still running ──────────────────────────────
        campaign.reload()
        if campaign.status != "Running":
            break

        if row.status != "Pending":
            continue

        # ── Call window check ─────────────────────────────────────────────
        # Use campaign override if set, otherwise global settings
        effective_settings = _effective_settings(campaign, settings)
        if not check_call_window(effective_settings):
            frappe.log_error(
                title="VoiceCallCampaign processor",
                message=f"Campaign {campaign_name} paused — outside call window. Will resume when window opens.",
            )
            campaign.db_set("status", "Paused")
            frappe.db.commit()
            break

        # ── Pre-call situation recheck (L-CP-002) ─────────────────────────
        if campaign.pre_call_recheck:
            try:
                pe = frappe.get_doc("ProgramEnrollment", row.enrollment)
                student = frappe.get_doc("Student", pe.student)
                ctx = build_student_context(pe, student)
                current_situation = ctx.get("situation", "unknown")

                if current_situation in _SKIP_SITUATIONS and current_situation != row.situation:
                    _update_row(campaign_name, row.name, "Skipped",
                                f"Situation changed from {row.situation} to {current_situation} — student no longer needs a call")
                    continue
            except Exception as recheck_exc:
                frappe.log_error(
                    title="VoiceCallCampaign pre-call recheck",
                    message=f"Row {row.name}: {recheck_exc}",
                )

        # ── Mark as Calling ───────────────────────────────────────────────
        _update_row(campaign_name, row.name, "Calling", call_placed_at=now_datetime())

        # ── Place the call ────────────────────────────────────────────────
        step = {
            "escalation_order": 1,
            "escalation_type": "parent_call",
            "hours_after_previous": 0,
            "points_awarded": 0,
        }

        try:
            result = initiate_parent_call(
                row.enrollment,
                step,
                campaign_name=campaign_name,
                queue_row_name=row.name,
            )
        except Exception as call_exc:
            result = False
            frappe.log_error(
                title="VoiceCallCampaign call error",
                message=f"Campaign {campaign_name}, row {row.name}: {call_exc}",
            )

        # ── Write VoiceCallHistory row on ProgramEnrollment ─────────────────
        try:
            _write_call_log(row, campaign_name, result)
        except Exception as log_exc:
            frappe.log_error(
                title="VoiceCallCampaign log write error",
                message=f"Row {row.name}: {log_exc}",
            )

        # ── Handle result ─────────────────────────────────────────────────
        if not result:
            retry_count = int(row.retry_count or 0) + 1
            if retry_count >= MAX_RETRIES:
                _update_row(campaign_name, row.name, "Permanently Failed",
                            f"Failed after {retry_count} attempts",
                            retry_count=retry_count)
            else:
                _update_row(campaign_name, row.name, "Failed",
                            f"Call failed (attempt {retry_count})",
                            retry_count=retry_count)
        # If result is True, webhook will update status to Answered/No Answer

        frappe.db.commit()
        time.sleep(INTER_CALL_DELAY)

    # ── Check if queue is fully processed ─────────────────────────────────
    campaign.reload()
    if campaign.status == "Running":
        pending_count = frappe.db.count(
            "VoiceCallQueue",
            {"parent": campaign_name, "parenttype": "VoiceCallCampaign",
             "status": ["in", ["Pending", "Calling"]]}
        )
        if pending_count == 0:
            campaign.db_set("status", "Complete")
            campaign.db_set("completed_at", now_datetime())
            frappe.db.commit()

            # Refresh analytics
            _refresh_campaign_stats(campaign_name)

            # Handle recurring campaigns
            _schedule_next_recurrence(campaign_name)


def _update_row(campaign_name, row_name, status, error="", retry_count=None, call_placed_at=None):
    """Atomically update a VoiceCallQueue row's status fields."""
    update = {"status": status}
    if error:
        update["error_message"] = error
    if retry_count is not None:
        update["retry_count"] = retry_count
    if call_placed_at:
        update["call_placed_at"] = call_placed_at
    frappe.db.set_value("VoiceCallQueue", row_name, update, update_modified=False)


def _write_call_log(row, campaign_name, result):
    """Write a VoiceCallHistory row on the ProgramEnrollment.

    Uses direct SQL insert (same as _write_voice_call_log in vocallabs.py)
    to avoid loading the full PE document just to append a child row.
    """
    now = now_datetime()
    try:
        frappe.db.sql("""
            INSERT INTO "tabVoiceCallHistory"
                (name, parent, parenttype, parentfield, idx,
                 call_placed_at, situation, outcome, campaign,
                 agent_id, rendered_prompt, reengaged_within_48h,
                 creation, modified, modified_by, owner, docstatus)
            VALUES
                (%(name)s, %(parent)s, 'ProgramEnrollment', 'voice_call_history', 1,
                 %(call_placed_at)s, %(situation)s, %(outcome)s, %(campaign)s,
                 %(agent_id)s, %(rendered_prompt)s, 0,
                 %(now)s, %(now)s, 'Administrator', 'Administrator', 0)
        """, {
            "name":           frappe.generate_hash(length=10),
            "parent":         row.enrollment,
            "call_placed_at": now,
            "situation":      (row.situation or "")[:140],
            "outcome":        "placed",
            "campaign":       (campaign_name or "")[:140],
            "agent_id":       (row.agent_id or "")[:140],
            "rendered_prompt": (row.rendered_prompt or "")[:500],
            "now":            now,
        })
    except Exception as exc:
        frappe.log_error(
            title="campaign_processor _write_call_log",
            message=f"VoiceCallHistory insert failed for {row.enrollment}: {exc}",
        )


def _refresh_campaign_stats(campaign_name):
    """Recount queue statuses and update campaign stat fields."""
    campaign = frappe.get_doc("VoiceCallCampaign", campaign_name)
    campaign._refresh_stats()
    campaign.save(ignore_permissions=True)
    frappe.db.commit()


def _schedule_next_recurrence(campaign_name):
    """If recurrence is set, create a new campaign for the next run."""
    recurrence = frappe.db.get_value("VoiceCallCampaign", campaign_name, "recurrence")
    if not recurrence or recurrence == "One-time":
        return

    from frappe.utils import add_days

    next_at = add_days(now_datetime(), 1 if recurrence == "Daily" else 7)
    orig = frappe.get_doc("VoiceCallCampaign", campaign_name)

    new_campaign = frappe.new_doc("VoiceCallCampaign")
    new_campaign.campaign_name            = f"{orig.campaign_name} (auto)"
    new_campaign.status                   = "Draft"
    new_campaign.source_type              = orig.source_type
    new_campaign.source_batch             = orig.source_batch
    new_campaign.source_program           = orig.source_program
    new_campaign.language_filter          = orig.language_filter
    new_campaign.recurrence               = orig.recurrence
    new_campaign.scheduled_at             = next_at
    new_campaign.pre_call_recheck         = orig.pre_call_recheck
    new_campaign.max_concurrent_calls     = orig.max_concurrent_calls
    new_campaign.call_window_start_override = orig.call_window_start_override
    new_campaign.call_window_end_override   = orig.call_window_end_override

    for f in orig.nudge_type_filters:
        new_campaign.append("nudge_type_filters", {"nudge_type": f.nudge_type})

    new_campaign.insert(ignore_permissions=True)
    frappe.db.commit()

    frappe.log_error(
        title="VoiceCallCampaign recurring",
        message=f"Scheduled next recurrence: {new_campaign.name} at {next_at}",
    )


def archive_old_call_history(max_rows_per_enrollment=50):
    """Trim VoiceCallHistory child rows per enrollment to prevent unbounded growth.

    Called manually or on a weekly cron. Keeps the most recent max_rows_per_enrollment
    rows per enrollment. For a student called weekly for 2 years (100 calls), this
    keeps the last 50 — enough history for analytics while bounding PE load size.

    Safe to run any time — uses DELETE with ROW_NUMBER() to keep newest rows.
    """
    frappe.db.sql("""
        DELETE FROM "tabVoiceCallHistory"
        WHERE name IN (
            SELECT name FROM (
                SELECT name,
                       ROW_NUMBER() OVER (
                           PARTITION BY parent
                           ORDER BY call_placed_at DESC
                       ) AS rn
                FROM "tabVoiceCallHistory"
                WHERE parenttype = 'ProgramEnrollment'
            ) ranked
            WHERE rn > %(max_rows)s
        )
    """, {"max_rows": max_rows_per_enrollment})

    frappe.db.commit()
    frappe.logger().info(
        f"archive_old_call_history: trimmed enrollments to {max_rows_per_enrollment} most recent call rows."
    )


def _effective_settings(campaign, settings):
    """Return a settings-like object with campaign overrides applied."""
    import frappe as _frappe

    effective = _frappe._dict({
        "call_window_start": campaign.call_window_start_override or settings.call_window_start,
        "call_window_end":   campaign.call_window_end_override   or settings.call_window_end,
        "max_calls_per_student_per_week": settings.max_calls_per_student_per_week,
        "call_cooldown_hours": settings.call_cooldown_hours,
    })
    return effective


# ── Scheduled job: check for campaigns due to run ─────────────────────────

def trigger_scheduled_campaigns():
    """Called hourly by hooks.py. Starts any campaigns whose scheduled_at has passed."""
    due = frappe.db.get_all(
        "VoiceCallCampaign",
        filters={
            "status": ["in", ["Draft", "Ready"]],
            "scheduled_at": ["<=", now_datetime()],
        },
        pluck="name",
    )

    for campaign_name in due:
        try:
            campaign = frappe.get_doc("VoiceCallCampaign", campaign_name)
            if campaign.status == "Draft":
                result = campaign.generate_queue()
                if not result.get("ok"):
                    continue

            campaign.reload()
            campaign.start_calls()
        except Exception as exc:
            frappe.log_error(
                title="VoiceCallCampaign scheduled trigger",
                message=f"Campaign {campaign_name}: {exc}",
            )


# ── 48h re-engagement checker ─────────────────────────────────────────────

def check_reengagement():
    """Called by hourly scheduler. Updates VoiceCallHistory.reengaged_within_48h.

    Finds VoiceCallHistory rows where:
    - outcome = answered
    - reengaged_within_48h = 0
    - call placed within the last 48 hours

    For each, checks if a submission_received ProgramEventLog event exists
    after the call. If yes, marks the history row as re-engaged.
    """
    from frappe.utils import add_to_date

    cutoff = add_to_date(now_datetime(), hours=-48)

    # Single JOIN query instead of N exists() calls per row.
    # Finds VoiceCallHistory rows that have a matching submission_received
    # event in ProgramEventLog after the call was placed.
    reengaged_names = frappe.db.sql("""
        SELECT DISTINCT vh.name
        FROM "tabVoiceCallHistory" vh
        INNER JOIN "tabProgramEventLog" pel
            ON pel.enrollment = vh.parent
            AND pel.event_type = 'submission_received'
            AND pel.created_at >= vh.call_placed_at
        WHERE vh.reengaged_within_48h = 0
          AND vh.outcome = 'answered'
          AND vh.call_placed_at >= %(cutoff)s
    """, {"cutoff": cutoff}, as_dict=False)

    updated = 0
    for (row_name,) in reengaged_names:
        frappe.db.set_value(
            "VoiceCallHistory", row_name, "reengaged_within_48h", 1,
            update_modified=False,
        )
        updated += 1

    if updated:
        frappe.db.commit()
        frappe.logger().info(
            f"Didi re-engagement check: marked {updated} calls as re-engaged."
        )
