"""CR-027 — tests for the weekly content sweep + one-time backlog migration.

Two deliverables under test:
  1. migrations.sweep_migration.migrate_behind_students_to_escalation — the
     operator-run one-time backlog migration.
  2. scheduler.weekly_content_sweep — the Monday 06:00 UTC recurring cron.

Both demote "behind" students (normal_content_delivery, current_week <
calendar_week, weekly_video_done = 0) into normal_escalation. The demotion goes
through the state machine `transition(..., skip_glific=True)` (NOT
t2_start_escalation — see sweep_migration module docstring / L-081), with
escalation params resolved per-PE from ArchetypeConfig via
_get_escalation_steps_for_pe — exactly as the live dispatcher does. After the
per-PE transitions, the Glific collection move is done in BULK
(remove_contacts_from_group_bulk + add_contacts_to_group_bulk), NOT per-PE.

Test isolation notes:
  - The production code path uses per-row frappe.db.commit() / rollback().
    Tests MUST NOT let those run (L-017). Every write-path test patches
    frappe.db.commit AND frappe.db.rollback to no-op Mocks.
  - Glific is never hit: bulk helpers + create_or_get_collection + start flow
    are patched, and `transition` is either mocked or run with skip_glific=True
    (which by construction skips _enqueue_contact_field_sync +
    maintain_collections — asserted explicitly in
    test_no_per_pe_glific_enqueue_during_migration).
"""
import frappe
from unittest.mock import patch, MagicMock
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program.tests.factories import make_batch
from tap_lms.summer_program.glific_extensions import remove_contacts_from_group_bulk
from tap_lms.summer_program.constants import (
    ACTION_ESCALATION,
    BPR_ACTIVE,
    PROGRAM_ACTIVE,
    PATH_CORE,
    LABEL_CONTENT_DELIVERED,
    STATE_NORMAL_CONTENT,
    STATE_NORMAL_ESCALATION,
    STATE_PROGRAM_COMPLETED,
    STATE_PROGRAM_DROPPED,
    STATE_GRACE_WAITING,
)

SWEEP_MIG = "tap_lms.summer_program.migrations.sweep_migration"
SCHED = "tap_lms.summer_program.scheduler"
SM = "tap_lms.summer_program.state_machine"
GE = "tap_lms.summer_program.glific_extensions"

# Global counters so identifiers stay unique across tests even though
# FrappeTestCase rolls back the DB between them (L-062).
_SEQ = [0]


def _next():
    _SEQ[0] += 1
    return f"{_SEQ[0]:06d}"


def _mk_steps(order=1, etype="help_note_a", hours=24):
    """An ArchetypeConfig escalation-step list shaped like _get_escalation_steps
    returns."""
    return [{
        "escalation_order": order,
        "escalation_type": etype,
        "points_awarded": 0,
        "hours_after_previous": hours,
    }]


def _make_student():
    suffix = _next()
    s = frappe.new_doc("Student")
    s.name1 = f"SweepStu{suffix}"
    s.phone = f"+9197000{suffix}"
    s.archetype = "fence_sitter"
    s.experiment_arm = "arm_a"
    s.language = "English"
    s.glific_id = f"stu-glific-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(
    batch_name,
    *,
    resolved_flow_state=STATE_NORMAL_CONTENT,
    current_week=1,
    weekly_video_done=0,
    program_status=PROGRAM_ACTIVE,
    glific_id=None,
    current_path=PATH_CORE,
    archetype="fence_sitter",
    experiment_arm="arm_a",
):
    suffix = _next()
    student = _make_student()
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"{student}-{batch_name}-{suffix}"
    pe.student = student
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = glific_id if glific_id is not None else f"pe-glific-{suffix}"
    pe.archetype = archetype
    pe.experiment_arm = experiment_arm
    pe.current_path = current_path
    pe.current_tier = "Basic"
    pe.journey_label = LABEL_CONTENT_DELIVERED
    pe.program_status = program_status
    pe.resolved_flow_state = resolved_flow_state
    pe.current_week = current_week
    pe.weekly_video_done = weekly_video_done
    pe.insert(ignore_permissions=True)
    return pe.name


def _make_bpr(batch_name, content_delivery_flow=12345, status=BPR_ACTIVE,
              collections=None):
    """Create a BatchProgramRun. `collections` is an optional list of
    (kind, glific_group_id) tuples → active PGCollection child rows."""
    bpr = frappe.new_doc("BatchProgramRun")
    bpr.batch = batch_name
    bpr.status = status
    if content_delivery_flow is not None:
        bpr.content_delivery_flow = content_delivery_flow
    for kind, group_id in (collections or []):
        bpr.append("pg_collections", {
            "kind": kind,
            "glific_group_id": group_id,
            "collection_label": f"{kind}-{group_id}",
            "is_active": 1,
        })
    bpr.insert(ignore_permissions=True)
    return bpr.name


def _active_bprs_in(batches):
    """Active BPRs restricted to the given batches — scopes the global sweep to
    a test's own fixtures. Filters in Python to sidestep the `= ANY(%s)`
    list-param re-wrap quirk (L-038)."""
    wanted = set(batches)
    rows = frappe.db.sql(
        """
        SELECT name, batch, content_delivery_flow
          FROM "tabBatchProgramRun"
         WHERE status = %s
        """,
        (BPR_ACTIVE,),
        as_dict=True,
    )
    return [r for r in rows if r["batch"] in wanted]


# ════════════════════════════════════════════════════════════
# remove_contacts_from_group_bulk (new helper)
# ════════════════════════════════════════════════════════════


class TestRemoveContactsFromGroupBulk(FrappeTestCase):
    def test_remove_contacts_from_group_bulk_returns_true_on_success(self):
        resp = MagicMock()
        resp.json.return_value = {
            "data": {"updateGroupContacts": {"groupContacts": [{"id": "1"}],
                                             "numberDeleted": 1}}
        }
        with patch(f"{GE}.get_glific_settings",
                   return_value=MagicMock(api_url="http://glific.test")), \
             patch(f"{GE}._glific_post_with_401_retry", return_value=resp) as m_post:
            ok = remove_contacts_from_group_bulk(["c1", "c2"], "g9")
        self.assertTrue(ok)
        # Mutation sends the ids under deleteContactIds, not addContactIds.
        _, payload = m_post.call_args[0]
        inp = payload["variables"]["input"]
        self.assertEqual(inp["deleteContactIds"], ["c1", "c2"])
        self.assertEqual(inp["addContactIds"], [])

    def test_remove_contacts_from_group_bulk_returns_false_on_errors(self):
        resp = MagicMock()
        resp.json.return_value = {"errors": [{"message": "boom"}]}
        with patch(f"{GE}.get_glific_settings",
                   return_value=MagicMock(api_url="http://glific.test")), \
             patch(f"{GE}._glific_post_with_401_retry", return_value=resp):
            ok = remove_contacts_from_group_bulk(["c1"], "g9")
        self.assertFalse(ok)

    def test_remove_contacts_from_group_bulk_empty_input_returns_false(self):
        # No network when there's nothing to do.
        with patch(f"{GE}._glific_post_with_401_retry") as m_post:
            self.assertFalse(remove_contacts_from_group_bulk([], "g9"))
            self.assertFalse(remove_contacts_from_group_bulk(["c1"], ""))
        m_post.assert_not_called()


# ════════════════════════════════════════════════════════════
# Migration tests
# ════════════════════════════════════════════════════════════


class TestSweepMigration(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # calendar_week = 2 → week-1 students are "behind".
        cls.batch = make_batch(
            label="CR027MigBatch", batch_id="CR027M", current_calendar_week=2
        )

    def setUp(self):
        for pe in frappe.get_all(
            "ProgramEnrollment", filters={"batch": self.batch}, pluck="name"
        ):
            frappe.delete_doc("ProgramEnrollment", pe, force=True)
        for bpr in frappe.get_all(
            "BatchProgramRun", filters={"batch": self.batch}, pluck="name"
        ):
            frappe.delete_doc("BatchProgramRun", bpr, force=True)

    def _migrate(self, **kw):
        from tap_lms.summer_program.migrations import sweep_migration
        return sweep_migration.migrate_behind_students_to_escalation(self.batch, **kw)

    # ── filtering (dry-run, no writes) ──

    def test_migrate_dry_run_reports_count_without_changes(self):
        behind = _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}.transition") as m_tr:
            res = self._migrate(dry_run=True)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(res["processed"], 1)
        m_tr.assert_not_called()
        self.assertEqual(
            frappe.db.get_value("ProgramEnrollment", behind, "resolved_flow_state"),
            STATE_NORMAL_CONTENT,
        )

    def test_migrate_filters_to_behind_normal_content_delivery_only(self):
        _make_pe(self.batch, resolved_flow_state=STATE_NORMAL_CONTENT, current_week=1)
        _make_pe(self.batch, resolved_flow_state=STATE_NORMAL_ESCALATION, current_week=1)
        res = self._migrate(dry_run=True)
        self.assertEqual(res["candidates"], 1)

    def test_migrate_excludes_current_week_students(self):
        _make_pe(self.batch, current_week=2)
        res = self._migrate(dry_run=True)
        self.assertEqual(res["candidates"], 0)

    def test_migrate_excludes_ahead_students(self):
        _make_pe(self.batch, current_week=3)
        res = self._migrate(dry_run=True)
        self.assertEqual(res["candidates"], 0)

    def test_migrate_excludes_video_done_students(self):
        _make_pe(self.batch, current_week=1, weekly_video_done=1)
        res = self._migrate(dry_run=True)
        self.assertEqual(res["candidates"], 0)

    def test_migrate_excludes_terminal_states(self):
        _make_pe(self.batch, resolved_flow_state=STATE_PROGRAM_COMPLETED, current_week=1)
        _make_pe(self.batch, resolved_flow_state=STATE_PROGRAM_DROPPED, current_week=1)
        _make_pe(self.batch, resolved_flow_state=STATE_GRACE_WAITING, current_week=1)
        res = self._migrate(dry_run=True)
        self.assertEqual(res["candidates"], 0)

    # ── escalation_type resolution + transition args ──

    def test_migrate_resolves_escalation_type_from_archetype_config(self):
        _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe",
                   return_value=_mk_steps(etype="voice_note", hours=6)), \
             patch(f"{SWEEP_MIG}.transition") as m_tr, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            self._migrate(dry_run=False, i_know_this_is_destructive=True)
        updates = m_tr.call_args.args[3]
        self.assertEqual(updates["current_escalation_type"], "voice_note")

    def test_migrate_falls_back_to_help_note_a_when_step_config_missing_type(self):
        _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe",
                   return_value=_mk_steps(etype="")), \
             patch(f"{SWEEP_MIG}.transition") as m_tr, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            self._migrate(dry_run=False, i_know_this_is_destructive=True)
        updates = m_tr.call_args.args[3]
        self.assertEqual(updates["current_escalation_type"], "help_note_a")

    def test_migrate_skips_pe_when_no_escalation_steps_configured(self):
        _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=[]), \
             patch(f"{SWEEP_MIG}.transition") as m_tr, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._migrate(dry_run=False, i_know_this_is_destructive=True)
        m_tr.assert_not_called()
        self.assertEqual(res["no_config"], 1)
        self.assertEqual(res["processed"], 0)
        self.assertTrue(any("no escalation_steps" in e for e in res["errors"]))

    def test_migrate_calls_transition_with_correct_args_from_archetype_config(self):
        pe = _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe",
                   return_value=_mk_steps(order=1, etype="help_note_b", hours=12)), \
             patch(f"{SWEEP_MIG}.transition") as m_tr, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            self._migrate(dry_run=False, i_know_this_is_destructive=True)
        args, kwargs = m_tr.call_args
        self.assertEqual(args[0].name, pe)                 # PE doc
        self.assertEqual(args[1], STATE_NORMAL_ESCALATION)  # target state
        self.assertEqual(args[2], "sweep_migration")        # trigger_source
        updates = args[3]
        self.assertEqual(updates["current_escalation_step"], 1)
        self.assertEqual(updates["current_escalation_type"], "help_note_b")
        self.assertEqual(updates["next_action_type"], ACTION_ESCALATION)
        self.assertEqual(updates["journey_label"], LABEL_CONTENT_DELIVERED)
        self.assertTrue(kwargs["skip_glific"])              # ← CRITICAL

    def test_migration_uses_transition_with_skip_glific_true(self):
        _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}.transition") as m_tr, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            self._migrate(dry_run=False, i_know_this_is_destructive=True)
        self.assertTrue(m_tr.call_args.kwargs["skip_glific"])

    def test_no_per_pe_glific_enqueue_during_migration(self):
        """The real transition runs with skip_glific=True, so the per-PE Glific
        paths must NOT be invoked. This is the load-bearing scale guarantee."""
        _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SM}._enqueue_contact_field_sync") as m_sync, \
             patch(f"{SM}.maintain_collections") as m_maintain, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._migrate(dry_run=False, i_know_this_is_destructive=True)
        self.assertEqual(res["processed"], 1)
        m_sync.assert_not_called()
        m_maintain.assert_not_called()
        # State really flipped (real transition ran).
        # (bulk move had no active BPR in this test → recorded as an error,
        #  which is fine; we only assert the per-PE Glific path was skipped.)

    # ── commit / failure-isolation ──

    def test_migrate_per_row_commits(self):
        for _ in range(3):
            _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}.transition"), \
             patch.object(frappe.db, "commit") as m_commit, \
             patch.object(frappe.db, "rollback"):
            res = self._migrate(dry_run=False, i_know_this_is_destructive=True)
        self.assertEqual(res["processed"], 3)
        self.assertEqual(m_commit.call_count, 3)  # one per processed row

    def test_migrate_continues_after_individual_failure(self):
        for _ in range(3):
            _make_pe(self.batch, current_week=1)
        calls = {"n": 0}

        def flaky_transition(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return True

        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}.transition", side_effect=flaky_transition), \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._migrate(dry_run=False, i_know_this_is_destructive=True)
        self.assertEqual(res["processed"], 2)
        self.assertEqual(res["failed"], 1)
        self.assertEqual(calls["n"], 3)

    # ── guards ──

    def test_migrate_destructive_guard_blocks_real_run_without_flag(self):
        _make_pe(self.batch, current_week=1)
        with self.assertRaises(frappe.ValidationError):
            self._migrate(dry_run=False)

    def test_migrate_destructive_guard_rejects_truthy_string_false(self):
        _make_pe(self.batch, current_week=1)
        with self.assertRaises(frappe.ValidationError):
            self._migrate(dry_run="False", i_know_this_is_destructive="False")

    def test_migrate_requires_admin_role(self):
        # frappe.only_for is a no-op when flags.in_test is set, so assert the
        # guard is WIRED (called with the admin roles) rather than the raise.
        _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}.frappe.only_for") as m_only_for:
            self._migrate(dry_run=True)
        m_only_for.assert_called_once_with(["TAP Admin", "System Manager"])

    # ── Phase B bulk Glific collection move ──

    def test_migration_collects_glific_ids_for_phase_B(self):
        bpr = _make_bpr(self.batch, collections=[("main", "MAIN1"), ("escalation", "ESC1")])
        _make_pe(self.batch, current_week=1, glific_id="gid-A")
        _make_pe(self.batch, current_week=1, glific_id="gid-B")
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}.transition"), \
             patch(f"{SWEEP_MIG}.remove_contacts_from_group_bulk", return_value=True) as m_rm, \
             patch(f"{SWEEP_MIG}.add_contacts_to_group_bulk", return_value=True) as m_add, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._migrate(dry_run=False, i_know_this_is_destructive=True)
        # Both glific ids flow through to the bulk helpers.
        self.assertEqual(set(m_rm.call_args.args[0]), {"gid-A", "gid-B"})
        self.assertEqual(set(m_add.call_args.args[0]), {"gid-A", "gid-B"})
        self.assertEqual(res["bulk_move"]["removed_from_main"], 2)
        self.assertEqual(res["bulk_move"]["added_to_escalation"], 2)

    def test_migration_phase_b_bulk_removes_from_main(self):
        _make_bpr(self.batch, collections=[("main", "MAIN1"), ("escalation", "ESC1")])
        _make_pe(self.batch, current_week=1, glific_id="gid-A")
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}.transition"), \
             patch(f"{SWEEP_MIG}.remove_contacts_from_group_bulk", return_value=True) as m_rm, \
             patch(f"{SWEEP_MIG}.add_contacts_to_group_bulk", return_value=True), \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            self._migrate(dry_run=False, i_know_this_is_destructive=True)
        m_rm.assert_called_once_with(["gid-A"], "MAIN1")

    def test_migration_phase_b_bulk_adds_to_escalation(self):
        _make_bpr(self.batch, collections=[("main", "MAIN1"), ("escalation", "ESC1")])
        _make_pe(self.batch, current_week=1, glific_id="gid-A")
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}.transition"), \
             patch(f"{SWEEP_MIG}.remove_contacts_from_group_bulk", return_value=True), \
             patch(f"{SWEEP_MIG}.add_contacts_to_group_bulk", return_value=True) as m_add, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            self._migrate(dry_run=False, i_know_this_is_destructive=True)
        m_add.assert_called_once_with(["gid-A"], "ESC1")

    def test_migration_phase_b_no_active_bpr_records_warning(self):
        # Operator runs the migration before any BPR is active: PEs transition,
        # but there's no main/escalation collection to move them on Glific. The
        # bulk move must be SKIPPED (no Glific calls) and a warning recorded —
        # not a silent drop or a crash.
        _make_pe(self.batch, current_week=1, glific_id="gid-A")  # NO BPR created
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}.transition"), \
             patch(f"{SWEEP_MIG}.remove_contacts_from_group_bulk", return_value=True) as m_rm, \
             patch(f"{SWEEP_MIG}.add_contacts_to_group_bulk", return_value=True) as m_add, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._migrate(dry_run=False, i_know_this_is_destructive=True)
        self.assertEqual(res["processed"], 1)
        self.assertIsNone(res["bpr"])
        self.assertIsNone(res["bulk_move"])           # bulk move skipped
        m_rm.assert_not_called()
        m_add.assert_not_called()
        self.assertTrue(any("no active BPR" in e for e in res["errors"]))


# ════════════════════════════════════════════════════════════
# Set-based bulk migration (Option A — the fast alternative)
# ════════════════════════════════════════════════════════════


class TestBulkSweepMigration(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch = make_batch(
            label="CR027BulkBatch", batch_id="CR027B", current_calendar_week=2
        )

    def setUp(self):
        for pe in frappe.get_all(
            "ProgramEnrollment", filters={"batch": self.batch}, pluck="name"
        ):
            frappe.delete_doc("ProgramEnrollment", pe, force=True)
        for bpr in frappe.get_all(
            "BatchProgramRun", filters={"batch": self.batch}, pluck="name"
        ):
            frappe.delete_doc("BatchProgramRun", bpr, force=True)

    def _bulk(self, **kw):
        from tap_lms.summer_program.migrations import sweep_migration
        return sweep_migration.migrate_behind_students_to_escalation_bulk(self.batch, **kw)

    def test_bulk_dry_run_reports_combos_without_writes(self):
        behind = _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()):
            res = self._bulk(dry_run=True)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(res["demoted"], 1)            # would-demote
        self.assertEqual(len(res["combos"]), 1)
        self.assertEqual(res["combos"][0]["escalation_type"], "help_note_a")
        # No writes.
        self.assertEqual(
            frappe.db.get_value("ProgramEnrollment", behind, "resolved_flow_state"),
            STATE_NORMAL_CONTENT,
        )

    def test_bulk_excludes_non_behind(self):
        _make_pe(self.batch, current_week=1)                       # behind ✓
        _make_pe(self.batch, current_week=2)                       # on-track ✗
        _make_pe(self.batch, current_week=3)                       # ahead ✗
        _make_pe(self.batch, current_week=1, weekly_video_done=1)  # engaged ✗
        _make_pe(self.batch, resolved_flow_state=STATE_PROGRAM_DROPPED, current_week=1)  # terminal ✗
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()):
            res = self._bulk(dry_run=True)
        self.assertEqual(res["candidates"], 1)

    def test_bulk_demotes_behind_set_based(self):
        from datetime import timedelta
        from frappe.utils import now_datetime, get_datetime
        a = _make_pe(self.batch, current_week=1)
        b = _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe",
                   return_value=_mk_steps(etype="help_note_a", hours=24)), \
             patch(f"{SWEEP_MIG}._bulk_move_to_escalation",
                   return_value={"removed_from_main": 0, "added_to_escalation": 0, "errors": []}), \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._bulk(dry_run=False, i_know_this_is_destructive=True)
        self.assertEqual(res["demoted"], 2)
        for pe in (a, b):
            row = frappe.db.get_value(
                "ProgramEnrollment", pe,
                ["resolved_flow_state", "current_escalation_step",
                 "current_escalation_type", "next_action_type", "next_action_at",
                 "journey_label"],
                as_dict=True,
            )
            self.assertEqual(row.resolved_flow_state, STATE_NORMAL_ESCALATION)
            self.assertEqual(row.current_escalation_step, 1)
            self.assertEqual(row.current_escalation_type, "help_note_a")
            self.assertEqual(row.next_action_type, ACTION_ESCALATION)
            self.assertEqual(row.journey_label, LABEL_CONTENT_DELIVERED)
            # Jitter window: NOW() + 24h + up to 30min (L-013 anti-herd).
            # Assert BOTH bounds so a wrong multiplier / missing jitter is caught.
            nxt = get_datetime(row.next_action_at)
            self.assertGreater(nxt, now_datetime() + timedelta(hours=23))
            self.assertLess(nxt, now_datetime() + timedelta(hours=25))

    def test_bulk_resolves_escalation_type_per_combo(self):
        pe_a = _make_pe(self.batch, current_week=1, experiment_arm="arm_a")
        pe_b = _make_pe(self.batch, current_week=1, experiment_arm="arm_b")

        def by_arm(pe_doc):
            return _mk_steps(etype="voice_note") if pe_doc.experiment_arm == "arm_b" \
                else _mk_steps(etype="help_note_a")

        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", side_effect=by_arm), \
             patch(f"{SWEEP_MIG}._bulk_move_to_escalation",
                   return_value={"removed_from_main": 0, "added_to_escalation": 0, "errors": []}), \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._bulk(dry_run=False, i_know_this_is_destructive=True)
        self.assertEqual(res["demoted"], 2)
        self.assertEqual(
            frappe.db.get_value("ProgramEnrollment", pe_a, "current_escalation_type"),
            "help_note_a",
        )
        self.assertEqual(
            frappe.db.get_value("ProgramEnrollment", pe_b, "current_escalation_type"),
            "voice_note",
        )

    def test_bulk_skips_combo_with_no_config(self):
        behind = _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=[]), \
             patch(f"{SWEEP_MIG}._bulk_move_to_escalation") as m_bulk, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._bulk(dry_run=False, i_know_this_is_destructive=True)
        self.assertEqual(res["demoted"], 0)
        self.assertEqual(res["no_config_pes"], 1)
        # The PE is left untouched.
        self.assertEqual(
            frappe.db.get_value("ProgramEnrollment", behind, "resolved_flow_state"),
            STATE_NORMAL_CONTENT,
        )
        m_bulk.assert_not_called()  # no glific ids changed → no bulk move

    def test_bulk_collects_glific_ids_and_calls_bulk_move(self):
        _make_bpr(self.batch, collections=[("main", "MAIN1"), ("escalation", "ESC1")])
        _make_pe(self.batch, current_week=1, glific_id="gid-A")
        _make_pe(self.batch, current_week=1, glific_id="gid-B")
        with patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}._bulk_move_to_escalation",
                   return_value={"removed_from_main": 2, "added_to_escalation": 2, "errors": []}) as m_bulk, \
             patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
            res = self._bulk(dry_run=False, i_know_this_is_destructive=True)
        m_bulk.assert_called_once()
        bpr_arg, ids_arg = m_bulk.call_args.args
        self.assertEqual(set(ids_arg), {"gid-A", "gid-B"})

    def test_bulk_destructive_guard_blocks_real_run_without_flag(self):
        _make_pe(self.batch, current_week=1)
        with self.assertRaises(frappe.ValidationError):
            self._bulk(dry_run=False)

    def test_bulk_requires_admin_role(self):
        _make_pe(self.batch, current_week=1)
        with patch(f"{SWEEP_MIG}.frappe.only_for") as m_only_for:
            self._bulk(dry_run=True)
        m_only_for.assert_called_once_with(["TAP Admin", "System Manager"])


# ════════════════════════════════════════════════════════════
# Weekly sweep tests
# ════════════════════════════════════════════════════════════


def _patch_phase2_glific(group_id="999"):
    return {
        "create": patch(f"{SCHED}.create_or_get_collection",
                        return_value={"id": group_id, "label": "x"}),
        "bulk": patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True),
        "flow": patch(f"{SCHED}.start_group_flow", return_value=True),
    }


class TestWeeklySweep(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch = make_batch(
            label="CR027SweepBatch", batch_id="CR027S", current_calendar_week=2
        )

    def setUp(self):
        for pe in frappe.get_all(
            "ProgramEnrollment", filters={"batch": self.batch}, pluck="name"
        ):
            frappe.delete_doc("ProgramEnrollment", pe, force=True)
        for bpr in frappe.get_all(
            "BatchProgramRun", filters={"batch": self.batch}, pluck="name"
        ):
            frappe.delete_doc("BatchProgramRun", bpr, force=True)
        # Scope the global sweep to THIS test's batch.
        self._abprs_patcher = patch(
            f"{SCHED}._active_bprs_for_sweep",
            side_effect=lambda: _active_bprs_in([self.batch]),
        )
        self._abprs_patcher.start()
        self.addCleanup(self._abprs_patcher.stop)

    def _run_sweep(self):
        from tap_lms.summer_program import scheduler
        return scheduler.weekly_content_sweep()

    def test_weekly_sweep_iterates_all_active_bprs(self):
        from tap_lms.summer_program import scheduler
        self._abprs_patcher.stop()
        b2 = make_batch(label="CR027SweepBatch2", batch_id="CR027S2",
                        current_calendar_week=2)
        a1 = _make_bpr(self.batch)
        a2 = _make_bpr(b2)
        draft = _make_bpr(self.batch, status="draft")
        try:
            names = {r["name"] for r in scheduler._active_bprs_for_sweep()}
            self.assertIn(a1, names)
            self.assertIn(a2, names)
            self.assertNotIn(draft, names)
        finally:
            self._abprs_patcher.start()
            for bpr in frappe.get_all("BatchProgramRun", filters={"batch": b2},
                                      pluck="name"):
                frappe.delete_doc("BatchProgramRun", bpr, force=True)

    def test_weekly_sweep_phase1_demotes_behind_students(self):
        # Phase 1 now delegates to the shared SET-BASED _bulk_demote_batch;
        # let it run the real UPDATE (Glific bulk move stubbed) and assert the
        # behind PE actually flipped to normal_escalation.
        _make_bpr(self.batch)
        behind = _make_pe(self.batch, current_week=1)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}._bulk_move_to_escalation",
                   return_value={"removed_from_main": 1, "added_to_escalation": 1, "errors": []}), \
             _patch_phase2_glific()["create"], _patch_phase2_glific()["bulk"], \
             _patch_phase2_glific()["flow"]:
            res = self._run_sweep()
        self.assertEqual(res["phase1_demoted"], 1)
        self.assertEqual(
            frappe.db.get_value("ProgramEnrollment", behind, "resolved_flow_state"),
            STATE_NORMAL_ESCALATION,
        )

    def test_weekly_sweep_phase1_uses_archetype_config_for_escalation_type(self):
        _make_bpr(self.batch)
        behind = _make_pe(self.batch, current_week=1)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe",
                   return_value=_mk_steps(etype="parent_call", hours=48)), \
             patch(f"{SWEEP_MIG}._bulk_move_to_escalation",
                   return_value={"removed_from_main": 1, "added_to_escalation": 1, "errors": []}), \
             _patch_phase2_glific()["create"], _patch_phase2_glific()["bulk"], \
             _patch_phase2_glific()["flow"]:
            self._run_sweep()
        self.assertEqual(
            frappe.db.get_value("ProgramEnrollment", behind, "current_escalation_type"),
            "parent_call",
        )

    def test_sweep_phase1_delegates_to_bulk_demote_batch(self):
        # The unification: Phase 1 calls the SAME _bulk_demote_batch the
        # one-time migration uses, once per active BPR, with (batch, cal, bpr).
        bpr = _make_bpr(self.batch)
        _make_pe(self.batch, current_week=1, glific_id="gid-sweep-1")
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}._bulk_demote_batch",
                   return_value={"candidates": 1, "combos": [], "demoted": 1,
                                 "no_config_pes": 0,
                                 "bulk_move": {"removed_from_main": 1,
                                               "added_to_escalation": 1, "errors": []},
                                 "errors": []}) as m_bulk, \
             _patch_phase2_glific()["create"], _patch_phase2_glific()["bulk"], \
             _patch_phase2_glific()["flow"]:
            res = self._run_sweep()
        m_bulk.assert_called_once()
        args, kwargs = m_bulk.call_args
        self.assertEqual(args[0], self.batch)      # batch_name
        self.assertEqual(args[1], 2)               # calendar_week (fixture)
        self.assertEqual(args[2], bpr)             # bpr_name
        self.assertFalse(kwargs.get("dry_run", False))
        self.assertEqual(res["phase1_demoted"], 1)
        self.assertEqual(res["phase1_added_to_escalation"], 1)

    def test_sweep_phase1_isolates_per_bpr_failure(self):
        # A crash in one BPR's demotion must bump phase1_failed, not abort the
        # rest of the run — Phase 2 still proceeds for that BPR.
        _make_bpr(self.batch)
        _make_pe(self.batch, current_week=1)   # behind (Phase 1)
        _make_pe(self.batch, current_week=2)   # current-week (Phase 2)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}._bulk_demote_batch", side_effect=RuntimeError("boom")), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}) as m_create, \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True), \
             patch(f"{SCHED}.start_group_flow", return_value=True):
            res = self._run_sweep()
        self.assertEqual(res["phase1_failed"], 1)
        self.assertEqual(res["phase1_demoted"], 0)
        m_create.assert_called_once()          # Phase 2 still ran for the BPR

    def test_weekly_sweep_phase2_builds_temp_sweep_group(self):
        _make_bpr(self.batch)
        _make_pe(self.batch, current_week=2)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}) as m_create, \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True), \
             patch(f"{SCHED}.start_group_flow", return_value=True):
            self._run_sweep()
        m_create.assert_called_once()
        self.assertIn("sweep_wk2", m_create.call_args.kwargs["label"])
        self.assertIn(self.batch, m_create.call_args.kwargs["label"])

    def test_weekly_sweep_phase2_bulk_adds_candidates(self):
        _make_bpr(self.batch)
        _make_pe(self.batch, current_week=2, glific_id="glific-cur-1")
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}), \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True) as m_bulk, \
             patch(f"{SCHED}.start_group_flow", return_value=True):
            self._run_sweep()
        contact_ids, group_id = m_bulk.call_args.args[0], m_bulk.call_args.args[1]
        self.assertEqual(contact_ids, ["glific-cur-1"])
        self.assertEqual(group_id, "777")

    def test_weekly_sweep_phase2_triggers_content_flow(self):
        _make_bpr(self.batch, content_delivery_flow=55555)
        _make_pe(self.batch, current_week=2)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}), \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True), \
             patch(f"{SCHED}.start_group_flow", return_value=True) as m_flow:
            res = self._run_sweep()
        m_flow.assert_called_once_with(flow_id="55555", group_id="777")
        self.assertEqual(res["phase2_triggered"], 1)

    def test_weekly_sweep_phase2_idempotent_when_run_twice_same_week(self):
        _make_bpr(self.batch)
        pe = _make_pe(self.batch, current_week=2, weekly_video_done=0)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}), \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True), \
             patch(f"{SCHED}.start_group_flow", return_value=True) as m_flow:
            res1 = self._run_sweep()
            self.assertEqual(res1["phase2_triggered"], 1)
            # Simulate the Glific flow delivering the video.
            frappe.db.set_value("ProgramEnrollment", pe, "weekly_video_done", 1)
            m_flow.reset_mock()
            res2 = self._run_sweep()
        self.assertEqual(res2["phase2_triggered"], 0)
        self.assertEqual(res2["bprs_with_no_phase2_candidates"], 1)
        m_flow.assert_not_called()

    def test_weekly_sweep_skips_bpr_without_content_delivery_flow(self):
        _make_bpr(self.batch, content_delivery_flow=None)
        _make_pe(self.batch, current_week=2)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}), \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True), \
             patch(f"{SCHED}.start_group_flow", return_value=True) as m_flow:
            res = self._run_sweep()
        self.assertEqual(res["bprs_without_content_flow"], 1)
        m_flow.assert_not_called()

    def test_weekly_sweep_skips_bpr_with_no_phase2_candidates(self):
        _make_bpr(self.batch)
        _make_pe(self.batch, current_week=2, weekly_video_done=1)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}) as m_create, \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True), \
             patch(f"{SCHED}.start_group_flow", return_value=True):
            res = self._run_sweep()
        self.assertEqual(res["bprs_with_no_phase2_candidates"], 1)
        m_create.assert_not_called()

    def test_weekly_sweep_summary_logged_on_completion(self):
        _make_bpr(self.batch)
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}), \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True), \
             patch(f"{SCHED}.start_group_flow", return_value=True), \
             patch(f"{SCHED}.frappe.log_error") as m_log:
            self._run_sweep()
        titles = [c.args[1] if len(c.args) > 1 else c.kwargs.get("title")
                  for c in m_log.call_args_list]
        self.assertIn("CR-027 Weekly Sweep Summary", titles)


# ════════════════════════════════════════════════════════════
# Integration — migration then sweep, no double-processing
# ════════════════════════════════════════════════════════════


class TestMigrationThenSweep(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch = make_batch(
            label="CR027IntBatch", batch_id="CR027I", current_calendar_week=2
        )

    def setUp(self):
        for pe in frappe.get_all(
            "ProgramEnrollment", filters={"batch": self.batch}, pluck="name"
        ):
            frappe.delete_doc("ProgramEnrollment", pe, force=True)
        for bpr in frappe.get_all(
            "BatchProgramRun", filters={"batch": self.batch}, pluck="name"
        ):
            frappe.delete_doc("BatchProgramRun", bpr, force=True)

    def test_full_cycle_migration_then_sweep_no_double_processing(self):
        """The migration demotes a behind student to normal_escalation (real
        transition, skip_glific). The subsequent sweep must NOT re-demote that
        student — they are no longer in normal_content_delivery."""
        from tap_lms.summer_program.migrations import sweep_migration
        from tap_lms.summer_program import scheduler

        _make_bpr(self.batch, collections=[("main", "MAIN1"), ("escalation", "ESC1")])
        behind = _make_pe(self.batch, current_week=1)

        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SM}._enqueue_contact_field_sync"), \
             patch(f"{SM}.maintain_collections"), \
             patch(f"{SWEEP_MIG}.remove_contacts_from_group_bulk", return_value=True), \
             patch(f"{SWEEP_MIG}.add_contacts_to_group_bulk", return_value=True):
            mig = sweep_migration.migrate_behind_students_to_escalation(
                self.batch, dry_run=False, i_know_this_is_destructive=True)

        self.assertEqual(mig["processed"], 1)
        self.assertEqual(
            frappe.db.get_value("ProgramEnrollment", behind, "resolved_flow_state"),
            STATE_NORMAL_ESCALATION,
        )

        # Let the real SET-BASED _bulk_demote_batch run (Glific bulk move
        # stubbed in sweep_migration). It should find 0 behind candidates.
        with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"), \
             patch(f"{SCHED}._active_bprs_for_sweep",
                   side_effect=lambda: _active_bprs_in([self.batch])), \
             patch(f"{SWEEP_MIG}._get_escalation_steps_for_pe", return_value=_mk_steps()), \
             patch(f"{SWEEP_MIG}._bulk_move_to_escalation",
                   return_value={"removed_from_main": 0, "added_to_escalation": 0, "errors": []}), \
             patch(f"{SCHED}.create_or_get_collection",
                   return_value={"id": "777", "label": "x"}), \
             patch(f"{SCHED}.add_contacts_to_group_bulk", return_value=True), \
             patch(f"{SCHED}.start_group_flow", return_value=True):
            sweep = scheduler.weekly_content_sweep()

        self.assertEqual(sweep["phase1_demoted"], 0)
