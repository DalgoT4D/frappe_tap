"""
Tests for summer_program.scheduler

CR-003 retired `_run_grace_notifications` and `_run_reengagement` along with
the SP_Grace_Reminder and SP_Paused_Reengagement Glific flows. The previous
test suite in this file exercised those functions as Postgres-compat
regression coverage (L-002 / L-005). Both lessons are now part of the
project memory and won't regress without the lessons being reverted.

The grace-window mechanics are now covered by:
  - tests/test_grace_logic.py — T0/T14 clock arming, T5/T11 clock preservation,
    handle_grace_check expiry → t17_grace_expired
  - tests/test_cr_003_migration.py — paused_no_activity → program_dropped
    migration

The re-engagement path is gone entirely (re-engagement is now inbound-only
via SP_Incoming_Router, with the rejoin branch handled Glific-side).

This stub remains so a `bench run-tests` invocation finds the file and so
future engineers grepping for `test_scheduler` find the historical context.

BR-006 (2026-06-16) adds regression coverage below: `run_daily_actions` must
NOT re-fire content-delivery or escalation collection group-flows (those were
superseded by weekly_content_delivery_trigger + pe_dispatcher.handle_escalation).
These are pure control-flow tests (mock `start_group_flow`); no DB fixtures are
needed, so they use plain `unittest.TestCase` like the rest of this file.
"""
import unittest
from types import SimpleNamespace
from unittest import mock


class TestSchedulerRetired(unittest.TestCase):
    """Sentinel test documenting that `scheduler._run_grace_notifications`
    and `_run_reengagement` were removed in CR-003. Re-introducing them
    without the rest of the legacy grace/reminder flow would be a regression.
    """

    def test_grace_notifications_function_removed(self):
        from tap_lms.summer_program import scheduler
        self.assertFalse(
            hasattr(scheduler, "_run_grace_notifications"),
            "_run_grace_notifications was retired in CR-003. If you need "
            "weekly grace handling, see activity_points.award_activity_points "
            "(arms the grace clock on first VideoClass of week) and "
            "pe_dispatcher.handle_grace_check (drop path at expiry).",
        )

    def test_reengagement_function_removed(self):
        from tap_lms.summer_program import scheduler
        self.assertFalse(
            hasattr(scheduler, "_run_reengagement"),
            "_run_reengagement was retired in CR-003. Re-engagement is now "
            "inbound-only via the SP_Incoming_Router Glific flow; there is "
            "no proactive Frappe-side handler.",
        )


def _fake_active_bpr():
    """Minimal BPR-like stub: a `main` (live) collection + a legacy
    archetype×arm group, plus the flow ids run_daily_actions would fire."""
    return SimpleNamespace(
        name="BPR-BR006",
        batch="BATCH-BR006",
        content_delivery_flow=10001,
        escalation_flow=10002,
        program_complete_flow=10005,
        status="active",
        pg_collections=[
            SimpleNamespace(collection_label="main", glific_group_id="20529", archetype=None),
            SimpleNamespace(collection_label="dormant_arm_a", glific_group_id="20443", archetype="dormant"),
        ],
        pg_onboarding_sets=[],
    )


class TestRunDailyActionsNoCollectionOverlap(unittest.TestCase):
    """BR-006 regression: `run_daily_actions` must NOT re-fire content-delivery
    or escalation collection group-flows. Content delivery is owned by
    `weekly_content_delivery_trigger` (Tuesday) + `weekly_content_sweep`;
    escalation by `pe_dispatcher.handle_escalation` (per-PE). The only daily
    collection-fire that survives is the batch-level program-complete trigger
    at program end (kept solely for the BPR.status='completed' flag)."""

    def test_mid_program_tick_fires_no_group_flows(self):
        from tap_lms.summer_program import scheduler
        bpr = _fake_active_bpr()
        batch = SimpleNamespace(total_weeks=8, grace_window_days=0)
        fired = []
        with mock.patch.object(
            scheduler, "start_group_flow",
            side_effect=lambda *a, **k: fired.append(a) or True,
        ), mock.patch.object(scheduler, "_get_current_week", return_value=3):
            scheduler._process_bpr_actions(bpr, batch)
        self.assertEqual(
            fired, [],
            "BR-006 regression: a mid-program daily tick fired collection "
            f"group-flows (content/escalation overlap reintroduced): {fired}",
        )

    def test_program_complete_still_marks_bpr_at_program_end(self):
        """The one daily action retained: at program end the BPR is marked
        completed (sole BPR.status setter — gates the active-BPR schedulers).
        Per-student completion is handled separately (per-PE)."""
        from tap_lms.summer_program import scheduler
        bpr = _fake_active_bpr()
        bpr.save = mock.MagicMock()
        batch = SimpleNamespace(total_weeks=8, grace_window_days=0)
        with mock.patch.object(
            scheduler, "start_group_flow", side_effect=lambda *a, **k: True,
        ), mock.patch.object(scheduler, "_get_current_week", return_value=9), \
                mock.patch("frappe.db.commit"), mock.patch("frappe.logger"):
            scheduler._process_bpr_actions(bpr, batch)
        self.assertEqual(bpr.status, "completed")
        bpr.save.assert_called_once()
