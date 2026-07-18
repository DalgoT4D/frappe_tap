"""
VoiceCallCampaign — Campaign management controller.

Lifecycle: Draft → Generating → Ready → Running → Paused/Complete/Error

Buttons visible in the Frappe desk (added via voicecallcampaign.js):
  Generate Queue  → generate_queue()    — builds the call queue from filters
  Start Calls     → start_calls()       — enqueues background processor
  Pause           → pause_calls()       — stops the processor mid-run
  Resume          → start_calls()       — same as start, picks up from Pending
  Clear Completed → clear_completed()   — removes Answered rows from queue
  Sync BigQuery   → sync_bigquery_now() — triggers immediate Glific context sync

All whitelist methods return {"ok": True, "message": "..."} for the JS caller.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, add_to_date


# ── Priority ordering for queue generation ────────────────────────────────
_SITUATION_PRIORITY = {
    "program_paused":          1,
    "deadline_live":           2,
    "streak_at_risk":          3,
    "complex_submission_stuck": 4,
    "returning_not_submitted": 5,
    "dropped_mid_flow":        6,
    "first_timer":             7,
    "celebration":             8,
    "unknown":                 99,
}

# Situations where the call should be SKIPPED (student is doing fine / positive)
_SKIP_SITUATIONS = {"unknown", "celebration"}


class VoiceCallCampaign(Document):

    # ── Frappe lifecycle ──────────────────────────────────────────────────

    def validate(self):
        if self.source_type == "Batch" and not self.source_batch:
            frappe.throw("Source Batch is required when Source Type is Batch.")
        if self.source_type == "Program" and not self.source_program:
            frappe.throw("Source Program is required when Source Type is Program.")

    def before_save(self):
        self._refresh_stats()

    # ── Public whitelist methods ──────────────────────────────────────────

    @frappe.whitelist()
    def generate_queue(self):
        """Build the call queue from filters.

        Queries ProgramEnrollments matching the campaign's filters, computes
        situation for each student, resolves template + agent, applies cooldown
        check, and populates the call_queue child table.

        Ordered by situation priority (urgent deadlines first).
        Duplicate detection: warns if student already has a Calling row in
        another active campaign today.
        """
        if self.status not in ("Draft", "Ready"):
            return {"ok": False, "message": f"Cannot regenerate queue — status is {self.status}."}

        self.db_set("status", "Generating")
        frappe.db.commit()

        try:
            enrollments = self._fetch_enrollments()
            queue_rows = self._build_queue_rows(enrollments)

            # Sort by situation priority
            queue_rows.sort(key=lambda r: _SITUATION_PRIORITY.get(r["situation"], 99))

            # Clear existing queue and repopulate
            self.set("call_queue", [])
            for r in queue_rows:
                self.append("call_queue", r)

            self.total_students = len(queue_rows)
            self.calls_pending  = len([r for r in queue_rows if r["status"] == "Pending"])
            self.calls_skipped  = len([r for r in queue_rows if r["status"] == "Skipped"])
            self.calls_placed   = 0
            self.calls_answered = 0
            self.calls_no_answer = 0
            self.calls_failed   = 0
            self.db_set("status", "Ready")
            self.save(ignore_permissions=True)
            frappe.db.commit()

            return {
                "ok": True,
                "message": f"Queue generated: {self.calls_pending} students to call, {self.calls_skipped} skipped (cooldown/already done).",
                "total": self.total_students,
                "pending": self.calls_pending,
            }

        except Exception as exc:
            self.db_set("status", "Error")
            frappe.db.commit()
            frappe.log_error(title="VoiceCallCampaign generate_queue", message=str(exc))
            return {"ok": False, "message": f"Queue generation failed: {exc}"}

    @frappe.whitelist()
    def start_calls(self):
        """Enqueue the background processor to place calls."""
        if self.status not in ("Ready", "Paused"):
            return {"ok": False, "message": f"Cannot start — status is {self.status}."}

        pending = [r for r in self.call_queue if r.status == "Pending"]
        if not pending:
            return {"ok": False, "message": "No Pending rows in queue. Generate queue first."}

        self.db_set("status", "Running")
        if not self.started_at:
            self.db_set("started_at", now_datetime())
        frappe.db.commit()

        frappe.enqueue(
            "tap_lms.summer_program.campaign_processor.process_campaign_queue",
            queue="long",
            timeout=7200,  # 2h max; a large campaign may take a while
            enqueue_after_commit=True,
            campaign_name=self.name,
        )

        return {"ok": True, "message": f"Started. Processing {len(pending)} pending calls in background."}

    @frappe.whitelist()
    def pause_calls(self):
        """Pause the campaign. The background job checks this before each call."""
        if self.status != "Running":
            return {"ok": False, "message": "Campaign is not running."}
        self.db_set("status", "Paused")
        frappe.db.commit()
        return {"ok": True, "message": "Campaign paused. Current call (if any) will finish, then stop."}

    @frappe.whitelist()
    def clear_completed(self):
        """Remove Answered rows from the queue to reduce visual clutter."""
        before = len(self.call_queue)
        self.set("call_queue", [r for r in self.call_queue if r.status != "Answered"])
        after = len(self.call_queue)
        self._refresh_stats()
        self.save(ignore_permissions=True)
        frappe.db.commit()
        return {"ok": True, "message": f"Cleared {before - after} answered rows."}

    @frappe.whitelist()
    def retry_failed(self):
        """Reset Failed rows back to Pending so they get retried on next Start."""
        count = 0
        for row in self.call_queue:
            if row.status == "Failed" and int(row.retry_count or 0) < 3:
                row.status = "Pending"
                row.error_message = ""
                count += 1
        self._refresh_stats()
        self.save(ignore_permissions=True)
        frappe.db.commit()
        return {"ok": True, "message": f"Reset {count} failed rows to Pending."}

    @frappe.whitelist()
    def sync_bigquery_now(self):
        """Trigger an immediate BigQuery sync for Glific context."""
        try:
            from tap_lms.summer_program.bigquery_sync import sync_bigquery_glific_context
            sync_bigquery_glific_context()
            return {"ok": True, "message": "BigQuery sync triggered. Check Error Log for results."}
        except Exception as exc:
            return {"ok": False, "message": f"Sync failed: {exc}"}

    # ── Private helpers ───────────────────────────────────────────────────

    def _fetch_enrollments(self):
        """Return a list of ProgramEnrollment names matching this campaign's filters."""
        filters = {
            "program_type": "Summer",
            "program_status": ["in", ["active", "paused"]],
        }

        if self.source_type == "Batch" and self.source_batch:
            filters["batch"] = self.source_batch
        elif self.source_type == "Program" and self.source_program:
            # Get all batches under this program
            batches = frappe.db.get_all(
                "Batch",
                filters={"program": self.source_program},
                pluck="name",
            )
            if not batches:
                return []
            filters["batch"] = ["in", batches]

        if self.language_filter:
            filters["language"] = self.language_filter

        return frappe.db.get_all(
            "ProgramEnrollment",
            filters=filters,
            fields=["name", "student", "language", "archetype", "current_week",
                    "submission_count", "current_streak", "weekly_submission_done",
                    "current_escalation_type", "resolved_flow_state", "program_status",
                    "current_expected_submission_type", "total_points", "special_gems",
                    "grace_window_end_at", "last_call_at", "weekly_call_count", "total_call_count"],
        )

    def _build_queue_rows(self, enrollments):
        """For each enrollment, compute situation, resolve template, check filters.

        Uses lightweight field reads instead of full document loads to avoid
        158K frappe.get_doc() calls for 79K students. Student data is fetched
        in a single bulk query keyed by student name.
        """
        from tap_lms.summer_program.voice_context import build_student_context, check_rate_limit
        from tap_lms.summer_program.vocallabs import (
            _get_voice_agent_settings, _resolve_parent_call_config, _resolve_agent_id,
            _render_status_template,
        )

        settings = _get_voice_agent_settings()
        allowed_situations = self._allowed_situations()
        rows = []
        step_dummy = {"escalation_order": 1, "escalation_type": "parent_call",
                      "hours_after_previous": 0, "points_awarded": 0}

        # Bulk-load student data in one query instead of N frappe.get_doc() calls
        student_names = list({e.student for e in enrollments if e.student})
        student_map = {}
        if student_names:
            for batch_start in range(0, len(student_names), 500):
                batch = student_names[batch_start:batch_start + 500]
                rows_batch = frappe.db.get_all(
                    "Student",
                    filters={"name": ["in", batch]},
                    fields=["name", "name1", "phone", "grade", "language", "school_id"],
                )
                for s in rows_batch:
                    student_map[s.name] = s

        for pe_data in enrollments:
            pe = frappe._dict(pe_data)
            student = student_map.get(pe_data.student)
            if not student:
                continue

            # Build context (computes situation)
            ctx = build_student_context(pe, student)
            situation = ctx.get("situation", "unknown")

            # Apply nudge type filter
            if allowed_situations and situation not in allowed_situations:
                continue

            # Check rate limit / cooldown
            if not check_rate_limit(pe, settings):
                rows.append(self._queue_row(pe, student, ctx, situation, None, "Cooldown",
                                            "Rate limited — cooldown active or weekly cap reached"))
                continue

            # Duplicate detection — warn if calling same enrollment today in another active campaign
            existing_calling = frappe.db.exists(
                "VoiceCallQueue",
                {"enrollment": pe.name, "status": "Calling",
                 "parent": ["!=", self.name], "parenttype": "VoiceCallCampaign"},
            )
            if existing_calling:
                rows.append(self._queue_row(pe, student, ctx, situation, None, "Skipped",
                                            "Already calling in another active campaign"))
                continue

            # Resolve config + agent
            config = _resolve_parent_call_config(pe, pe.current_week or 1, settings, situation=situation)
            agent_id = _resolve_agent_id(settings, pe.language or "")

            rendered = ""
            if config and config.status_template:
                rendered = _render_status_template(config.status_template, pe, student, step_dummy)

            status = "Pending"
            error = ""
            if not agent_id:
                status = "Skipped"
                error = f"No Vocallabs agent mapped for language '{pe.language}'"
            elif not config:
                status = "Skipped"
                error = "No ParentCallConfig resolved — set VoiceNudgeConfig.language_templates"

            rows.append(self._queue_row(pe, student, ctx, situation, agent_id, status, error, rendered))

        return rows

    def _queue_row(self, pe, student, ctx, situation, agent_id, status, error="", rendered=""):
        return {
            "enrollment":      pe.name,
            "student":         pe.student,
            "student_name":    student.name1 or "",
            "language":        pe.language or "",
            "archetype":       pe.archetype or "",
            "situation":       situation,
            "agent_id":        agent_id or "",
            "rendered_prompt": rendered,
            "status":          status,
            "error_message":   error,
            "retry_count":     0,
        }

    @frappe.whitelist()
    def refresh_analytics(self):
        """Compute per-situation analytics from VoiceCallLog and update the campaign.

        Queries all VoiceCallLog rows for this campaign, groups by situation,
        and computes: calls placed, answered, no_answer, failed, re-engaged
        within 48h, and re-engagement rate (reengaged / answered * 100).

        Re-engagement rate is the primary KPI — it measures whether Didi's
        calls are actually causing students to submit.
        """
        logs = frappe.db.get_all(
            "VoiceCallHistory",
            filters={"campaign": self.name, "parenttype": "ProgramEnrollment"},
            fields=["situation", "outcome", "reengaged_within_48h"],
        )

        if not logs:
            return {"ok": False, "message": "No call history yet for this campaign. Calls must have been placed first."}

        # Group by situation
        by_situation = {}
        for log in logs:
            s = (log.situation or "unknown").strip()
            if s not in by_situation:
                by_situation[s] = {"placed": 0, "answered": 0, "no_answer": 0,
                                   "failed": 0, "reengaged": 0}
            by_situation[s]["placed"] += 1
            if log.outcome == "answered":
                by_situation[s]["answered"] += 1
            elif log.outcome == "no_answer":
                by_situation[s]["no_answer"] += 1
            elif log.outcome in ("failed", "skipped"):
                by_situation[s]["failed"] += 1
            if log.reengaged_within_48h:
                by_situation[s]["reengaged"] += 1

        # Rebuild analytics child table
        self.set("analytics", [])
        for situation in sorted(by_situation.keys()):
            c = by_situation[situation]
            rate = round(c["reengaged"] / c["answered"] * 100, 1) if c["answered"] else 0.0
            self.append("analytics", {
                "situation":         situation,
                "calls_placed":      c["placed"],
                "answered":          c["answered"],
                "no_answer":         c["no_answer"],
                "failed":            c["failed"],
                "reengaged":         c["reengaged"],
                "reengagement_rate": rate,
            })

        # Overall rate
        total_answered  = sum(c["answered"]   for c in by_situation.values())
        total_reengaged = sum(c["reengaged"]  for c in by_situation.values())
        self.re_engagement_rate = round(
            total_reengaged / total_answered * 100 if total_answered else 0.0, 1
        )
        self.analytics_last_updated = now_datetime()
        self.save(ignore_permissions=True)
        frappe.db.commit()

        return {
            "ok": True,
            "message": (
                f"Analytics updated. Overall re-engagement rate: {self.re_engagement_rate}% "
                f"({total_reengaged} of {total_answered} answered calls led to submission within 48h)."
            ),
            "re_engagement_rate": self.re_engagement_rate,
        }

    def _allowed_situations(self):
        """Return set of allowed situation labels from nudge_type_filters, or None if no filter."""
        if not self.nudge_type_filters:
            return None
        situations = set()
        for row in self.nudge_type_filters:
            label = frappe.db.get_value("VoiceNudgeConfig", row.nudge_type, "situation_label")
            if label:
                situations.add(label)
        return situations if situations else None

    @frappe.whitelist()
    def archive_queue(self):
        """Delete all queue rows for completed campaigns older than 30 days.

        Prevents tabVoiceCallQueue from growing unbounded over many campaign runs.
        Safe to call anytime — only touches Complete/Error campaigns older than 30 days.
        """
        from frappe.utils import add_to_date
        cutoff = add_to_date(frappe.utils.now_datetime(), days=-30)

        old_campaigns = frappe.db.get_all(
            "VoiceCallCampaign",
            filters={"status": ["in", ["Complete", "Error"]], "completed_at": ["<", cutoff]},
            pluck="name",
        )
        deleted = 0
        for campaign_name in old_campaigns:
            count = frappe.db.count(
                "VoiceCallQueue",
                {"parent": campaign_name, "parenttype": "VoiceCallCampaign"},
            )
            frappe.db.delete(
                "VoiceCallQueue",
                {"parent": campaign_name, "parenttype": "VoiceCallCampaign"},
            )
            deleted += count

        frappe.db.commit()
        return {
            "ok": True,
            "message": f"Archived {deleted} queue rows from {len(old_campaigns)} old campaigns.",
        }

    def _refresh_stats(self):
        counts = {"Pending": 0, "Calling": 0, "Answered": 0,
                  "No Answer": 0, "Failed": 0, "Skipped": 0,
                  "Cooldown": 0, "Permanently Failed": 0}
        for row in self.call_queue:
            counts[row.status] = counts.get(row.status, 0) + 1
        self.total_students  = len(self.call_queue)
        self.calls_pending   = counts["Pending"]
        self.calls_placed    = (counts["Answered"] + counts["No Answer"] +
                                counts["Failed"] + counts["Permanently Failed"])
        self.calls_answered  = counts["Answered"]
        self.calls_no_answer = counts["No Answer"]
        self.calls_failed    = counts["Failed"] + counts["Permanently Failed"]
        self.calls_skipped   = counts["Skipped"] + counts["Cooldown"]
