"""
Tests for `summer_program.dev_tools` — the SP testing reset utilities.

Covers:
  1. reset_pe_to_state_0 — happy path: PE in week 3 / mid-escalation / with
     gamification points → reset to state 0 (normal_content_delivery, week 1,
     all counters and points zeroed, scheduler pointers cleared).
  2. reset_pe_to_state_0 — dry_run flag: snapshot only, no writes.
  3. reset_pe_to_state_0 — production-site safety guard: raises
     PermissionError when site name contains 'prod' unless override passed.
  4. reset_pe_to_state_0 — verifies maintain_collections is called with the
     correct from/to state delta (so CR-005 group membership reshuffles).
  5. list_pes_for_batch — read-only listing returns expected shape.
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from tap_lms.summer_program.constants import (
    LABEL_CONTENT_DELIVERED,
    LABEL_ENROLLED,
    PATH_CORE,
    PROGRAM_ACTIVE,
    PROGRAM_COMPLETED,
    STATE_NORMAL_CONTENT,
    STATE_NORMAL_ESCALATION,
)
from tap_lms.summer_program.dev_tools import (
    reset_pe_to_state_0,
    list_pes_for_batch,
    update_student_state,
    create_test_student_with_pe,
    _assert_dev_site,
)


# ════════════════════════════════════════════════════════════
# Test fixtures (mirrors test_state_machine.py shape)
# ════════════════════════════════════════════════════════════

def _ensure_batch():
    name = frappe.get_value("Batch", {"name1": "DevToolsTestBatch"}, "name")
    if name:
        return name
    batch = frappe.new_doc("Batch")
    batch.name1 = "DevToolsTestBatch"
    batch.start_date = "2026-01-01"
    batch.end_date = "2026-04-30"
    # Registration window (mandatory on Batch doctype as of current schema)
    batch.regist_start_date = "2025-12-01"
    batch.regist_end_date = "2025-12-31"
    batch.batch_id = "DTT01"
    batch.program_type = "Summer"
    batch.total_weeks = 12
    batch.current_calendar_week = 1
    batch.grace_window_days = 14
    batch.insert(ignore_permissions=True)
    return batch.name


def _ensure_student(suffix):
    name = frappe.get_value("Student", {"phone": f"+9999400{suffix}"}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"DevToolsTestStudent{suffix}"
    s.phone = f"+9999400{suffix}"
    s.glific_id = f"glific-dt-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_advanced_pe(batch_name, student_name, suffix):
    """Create a PE in a non-default state: week 3, mid-escalation, with
    gamification points + submission counts. Reset should zero all of these.
    """
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-DT-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-dt-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_ESCALATION
    pe.journey_label = LABEL_CONTENT_DELIVERED
    pe.current_path = PATH_CORE
    pe.current_week = 3
    pe.submission_count = 5
    pe.current_escalation_step = 2
    pe.last_escalation_step = 1
    pe.delivery_failure_count = 1
    pe.in_grace_window = 1
    # Task #85: set total_points, current_escalation_type, pause_reason so
    # the test PE genuinely exercises the fields that previously slipped
    # through the reset.
    pe.total_points = 145   # 30 + 40 + 50 + bonus → realistic week-3 cumulative
    pe.total_activity_points = 30
    pe.weekly_activity_points = 10
    pe.total_quiz_points = 40
    pe.weekly_quiz_points = 15
    pe.total_submission_points = 50
    pe.weekly_submission_points = 25
    pe.bonus_quiz_points = 25
    pe.current_streak = 3
    pe.special_gems = 4
    pe.weekly_video_done = 1
    pe.weekly_submission_done = 1
    pe.next_action_type = "escalation"
    pe.current_escalation_type = "parent_call"   # task #85
    pe.pause_reason = "binge_limit"               # task #85
    pe.insert(ignore_permissions=True)
    return pe


# ════════════════════════════════════════════════════════════
# 1. Happy-path reset
# ════════════════════════════════════════════════════════════

class TestResetPeToState0(FrappeTestCase):
    """Reset must move an advanced PE back to state 0 in one call."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    @patch("tap_lms.summer_program.dev_tools.maintain_collections")
    def test_reset_zeros_all_state_and_counters(
        self, mock_maintain, mock_reconcile, _mock_guard,
    ):
        student = _ensure_student("HP")
        pe = _make_advanced_pe(self.batch_name, student, "HP")

        # Task #82: reconcile_pe_to_glific returns a dict; the production
        # caller stashes it on the result. Mock with a sensible shape.
        mock_reconcile.return_value = {
            "pe": pe.name, "glific_id": pe.glific_id, "diff": [], "pushed": True,
        }

        result = reset_pe_to_state_0(
            student,
            delete_history=False,   # keep test isolated from history doctypes
            push_to_glific=True,
            verbose=False,
        )

        pe.reload()

        # Core state machine
        self.assertEqual(pe.resolved_flow_state, STATE_NORMAL_CONTENT)
        self.assertEqual(pe.journey_label, LABEL_ENROLLED)
        self.assertEqual(pe.program_status, PROGRAM_ACTIVE)
        self.assertEqual(pe.current_week, 1)
        self.assertEqual(pe.current_path, PATH_CORE)

        # Counters
        self.assertEqual(pe.submission_count, 0)
        self.assertEqual(pe.current_escalation_step, 0)
        self.assertEqual(pe.last_escalation_step, 0)
        self.assertEqual(pe.delivery_failure_count, 0)

        # Grace
        self.assertEqual(pe.in_grace_window, 0)
        self.assertIsNone(pe.grace_window_start)
        self.assertIsNone(pe.grace_window_end_at)

        # CR-002 v2 gamification — all zeroed
        self.assertEqual(pe.total_activity_points, 0)
        self.assertEqual(pe.weekly_activity_points, 0)
        self.assertEqual(pe.total_quiz_points, 0)
        self.assertEqual(pe.weekly_quiz_points, 0)
        self.assertEqual(pe.total_submission_points, 0)
        self.assertEqual(pe.weekly_submission_points, 0)
        self.assertEqual(pe.bonus_quiz_points, 0)
        self.assertEqual(pe.current_streak, 0)
        self.assertEqual(pe.special_gems, 0)
        self.assertEqual(pe.weekly_video_done, 0)
        self.assertEqual(pe.weekly_submission_done, 0)

        # Task #85: total_points is now zeroed alongside the per-stream
        # totals so the invariant stream_sum == total_points holds. Without
        # this, the reset state matched the ST00051295 audit drift exactly.
        self.assertEqual(
            pe.total_points, 0,
            "total_points must be zeroed too — fixes invariant break "
            "where stream totals were 0 but total_points stayed at the "
            "pre-reset value (task #85)",
        )

        # Scheduler pointers
        self.assertIsNone(pe.next_action_at)
        self.assertEqual(pe.next_action_type, "")

        # Task #85: escalation type + pause reason must be cleared.
        # Previously these strings carried over from the pre-reset state
        # (e.g., 'parent_call' / 'binge_limit'), causing Glific contact
        # state mismatches and confusing operational reports.
        self.assertEqual(
            pe.current_escalation_type, "",
            "current_escalation_type must be cleared on reset — "
            "current_escalation_step=0 with type='parent_call' is incoherent",
        )
        self.assertEqual(
            pe.pause_reason, "",
            "pause_reason must be cleared on reset — program_status is "
            "now active so the paused-for-X label shouldn't persist",
        )

        # CR-005 group membership delta — called with the previous state
        mock_maintain.assert_called_once()
        _, kwargs = mock_maintain.call_args
        self.assertEqual(kwargs["from_state"], STATE_NORMAL_ESCALATION)
        self.assertEqual(kwargs["to_state"], STATE_NORMAL_CONTENT)

        # Task #83: reset switched from async _enqueue_contact_field_sync
        # to synchronous reconcile_pe_to_glific. Verify the new target
        # was invoked with dry_run=False (live push).
        mock_reconcile.assert_called_once()
        _, kwargs = mock_reconcile.call_args
        self.assertFalse(kwargs.get("dry_run", True),
                         "reset must push live (dry_run=False)")

        # Return shape
        self.assertIn("before", result)
        self.assertIn("after", result)
        self.assertEqual(
            result["before"]["resolved_flow_state"], STATE_NORMAL_ESCALATION
        )
        self.assertEqual(
            result["after"]["resolved_flow_state"], STATE_NORMAL_CONTENT
        )

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    @patch("tap_lms.summer_program.dev_tools.maintain_collections")
    def test_reset_accepts_completed_program_status(
        self, mock_maintain, mock_reconcile, _mock_guard,
    ):
        student = _ensure_student("DONE")
        pe = _make_advanced_pe(self.batch_name, student, "DONE")
        pe.program_status = PROGRAM_COMPLETED
        pe.save(ignore_permissions=True)

        mock_reconcile.return_value = {
            "pe": pe.name, "glific_id": pe.glific_id, "diff": [], "pushed": True,
        }

        result = reset_pe_to_state_0(
            student,
            delete_history=False,
            push_to_glific=True,
            verbose=False,
        )

        pe.reload()
        self.assertEqual(result["before"]["program_status"], PROGRAM_COMPLETED)
        self.assertEqual(pe.program_status, PROGRAM_ACTIVE)
        self.assertEqual(pe.resolved_flow_state, STATE_NORMAL_CONTENT)
        mock_maintain.assert_called_once()
        mock_reconcile.assert_called_once()


# ════════════════════════════════════════════════════════════
# 2. Dry-run safety
# ════════════════════════════════════════════════════════════

class TestResetPeDryRun(FrappeTestCase):
    """dry_run=True must not modify the database."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    @patch("tap_lms.summer_program.dev_tools.maintain_collections")
    def test_dry_run_no_writes(self, mock_maintain, mock_reconcile, _mock_guard):
        student = _ensure_student("DRY")
        pe = _make_advanced_pe(self.batch_name, student, "DRY")

        result = reset_pe_to_state_0(student, dry_run=True, verbose=False)

        # State unchanged
        pe.reload()
        self.assertEqual(pe.resolved_flow_state, STATE_NORMAL_ESCALATION)
        self.assertEqual(pe.current_week, 3)
        self.assertEqual(pe.submission_count, 5)
        self.assertEqual(pe.current_streak, 3)

        # No Glific side effects — neither group reshuffles nor reconcile push
        mock_maintain.assert_not_called()
        mock_reconcile.assert_not_called()


# ════════════════════════════════════════════════════════════
# 3. Task #82 — current_expected_submission_type bug fix
# ════════════════════════════════════════════════════════════

class TestResetRecomputesExpectedSubmissionType(FrappeTestCase):
    """Task #82 (test-team report 2026-05-24): after reset_pe_to_state_0,
    current_expected_submission_type was being left at whatever value the
    PE carried from a later week (e.g., 'word_text_voice' from week 5)
    instead of being recomputed from the archetype's week-1 WeekRule.

    The normal enrollment flow at program_enrollment_api.py:268 derives
    this field via `_get_week1_submission_type(batch, archetype, arm)`;
    the reset must do the same so Glific flows see a coherent state-0
    contact bundle. Without it, the Glific flow asks for the wrong
    submission type and validation fails downstream.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()
        # Build an ArchetypeConfig + WeekRules so _get_week1_submission_type
        # has data to return. Skip if a config already exists from prior runs.
        existing = frappe.db.get_value("ArchetypeConfig", {
            "batch": cls.batch_name,
            "archetype": "fence_sitter",
            "experiment_arm": "default",
            "path": PATH_CORE,
        }, "name")
        if not existing:
            ac = frappe.new_doc("ArchetypeConfig")
            ac.batch = cls.batch_name
            ac.experiment_arm = "default"
            ac.archetype = "fence_sitter"
            ac.path = PATH_CORE
            ac.is_active = 1
            ac.append("week_rules", {
                "week": 1, "expected_submission_type": "image",
            })
            ac.append("week_rules", {
                "week": 5, "expected_submission_type": "word_text_voice",
            })
            ac.insert(ignore_permissions=True)

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    @patch("tap_lms.summer_program.dev_tools.maintain_collections")
    def test_reset_recomputes_to_archetype_week1_rule(
        self, mock_maintain, mock_reconcile, _mock_guard,
    ):
        """PE starts at week 5 with current_expected_submission_type='word_text_voice'.
        After reset, it must be 'image' — the week-1 rule for fence_sitter/default/Core.
        The stale 'word_text_voice' value is exactly the test-team-reported bug.
        """
        student = _ensure_student("EST1")
        pe = _make_advanced_pe(self.batch_name, student, "EST1")

        # Set up the bug scenario: PE at week 5 with stale submission type
        pe.current_week = 5
        pe.archetype = "fence_sitter"
        pe.experiment_arm = "default"
        pe.current_expected_submission_type = "word_text_voice"
        pe.save(ignore_permissions=True)
        self.assertEqual(pe.current_expected_submission_type, "word_text_voice",
                         "fixture sanity check")

        mock_reconcile.return_value = {
            "pe": pe.name, "glific_id": pe.glific_id, "diff": [], "pushed": True,
        }

        reset_pe_to_state_0(
            student, delete_history=False, push_to_glific=True, verbose=False,
        )

        pe.reload()
        self.assertEqual(
            pe.current_expected_submission_type, "image",
            "After reset, current_expected_submission_type MUST be the "
            "week-1 rule ('image'), NOT the stale value from week 5 "
            "('word_text_voice'). This is the bug the test team reported."
        )
        self.assertEqual(pe.current_week, 1,
                         "current_week reset to 1 (sanity)")
        # And the reconcile push fired with the corrected value.
        mock_reconcile.assert_called_once()

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    @patch("tap_lms.summer_program.dev_tools.maintain_collections")
    def test_reset_falls_back_to_empty_when_no_archetype_config(
        self, mock_maintain, mock_reconcile, _mock_guard,
    ):
        """If no ArchetypeConfig matches (e.g., unknown archetype/arm combo),
        the recompute returns None and we set the field to empty string —
        NOT leave a stale value. Better an empty string than a wrong value
        that confuses Glific flows."""
        student = _ensure_student("EST2")
        pe = _make_advanced_pe(self.batch_name, student, "EST2")

        # Archetype with no config in the fixture
        pe.current_week = 3
        pe.archetype = "submitter"   # no ArchetypeConfig for this combo
        pe.experiment_arm = "arm_a"  # nor this arm
        pe.current_expected_submission_type = "video"
        pe.save(ignore_permissions=True)

        mock_reconcile.return_value = {
            "pe": pe.name, "glific_id": pe.glific_id, "diff": [], "pushed": True,
        }

        reset_pe_to_state_0(
            student, delete_history=False, push_to_glific=True, verbose=False,
        )

        pe.reload()
        self.assertEqual(
            pe.current_expected_submission_type, "",
            "When no archetype config matches, fall back to empty — "
            "never leave a stale value behind"
        )

        # Return shape — before populated, after is None
        self.assertIsNotNone(result["before"])
        self.assertIsNone(result["after"])


# ════════════════════════════════════════════════════════════
# 3. Production-site safety guard
# ════════════════════════════════════════════════════════════

class TestSafetyGuard(FrappeTestCase):
    """_assert_dev_site refuses on production-suggestive site names."""

    def test_guard_raises_on_prod_site_name(self):
        with patch.object(frappe.local, "site", "tap_lms.prod"):
            with self.assertRaises(frappe.PermissionError) as ctx:
                _assert_dev_site(i_know_this_is_destructive=False)
            self.assertIn("prod", str(ctx.exception).lower())

    def test_guard_raises_on_live_site_name(self):
        with patch.object(frappe.local, "site", "tap-live.example.com"):
            with self.assertRaises(frappe.PermissionError):
                _assert_dev_site(i_know_this_is_destructive=False)

    def test_guard_override_bypasses_check(self):
        with patch.object(frappe.local, "site", "tap_lms.prod"):
            # Should NOT raise
            _assert_dev_site(i_know_this_is_destructive=True)

    def test_guard_passes_on_dev_site(self):
        with patch.object(frappe.local, "site", "tap_lms.dev"):
            # Should NOT raise
            _assert_dev_site(i_know_this_is_destructive=False)


# ════════════════════════════════════════════════════════════
# 4. list_pes_for_batch
# ════════════════════════════════════════════════════════════

class TestListPesForBatch(FrappeTestCase):
    """list_pes_for_batch is read-only and returns the expected shape."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def test_list_returns_active_and_paused_only(self):
        student_active = _ensure_student("LST_A")
        student_dropped = _ensure_student("LST_D")
        pe_active = _make_advanced_pe(self.batch_name, student_active, "LSTA")
        pe_dropped = _make_advanced_pe(self.batch_name, student_dropped, "LSTD")
        pe_dropped.program_status = "dropped"
        pe_dropped.save(ignore_permissions=True)

        rows = list_pes_for_batch(self.batch_name)

        pe_names = {r["pe"] for r in rows}
        self.assertIn(pe_active.name, pe_names)
        self.assertNotIn(
            pe_dropped.name, pe_names,
            "Dropped PEs should not appear in the listing",
        )

        # Shape check — each row has the expected keys
        for r in rows:
            for key in (
                "pe", "student", "resolved_flow_state",
                "current_week", "current_path", "submission_count",
            ):
                self.assertIn(key, r)


# ════════════════════════════════════════════════════════════
# 4. Task #84 — update_student_state recomputes expected submission type
# ════════════════════════════════════════════════════════════

class TestUpdateStudentStateRecomputesExpectedSubmissionType(FrappeTestCase):
    """Task #84: when update_student_state changes any of
    (current_week, current_path, archetype, experiment_arm), the
    PE.current_expected_submission_type MUST be recomputed from the
    WeekRule for the NEW (archetype, arm, path, week) combination.
    Without this, fast-forwarding via dev_tools leaves a stale
    submission type that Glific flows then misuse downstream.

    All tests mock reconcile_pe_to_glific so we don't make real HTTP
    calls; the assertion is purely about the PE state post-update.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()
        # Two ArchetypeConfigs with distinct WeekRules so the test can
        # observe the recompute taking the new (archetype, arm, path, week)
        # tuple into account.
        for archetype, weeks in [
            ("fence_sitter", {1: "image", 2: "word_text_voice", 3: "video"}),
            ("dormant", {1: "video", 2: "image", 3: "audio"}),
        ]:
            existing = frappe.db.get_value("ArchetypeConfig", {
                "batch": cls.batch_name,
                "archetype": archetype,
                "experiment_arm": "default",
                "path": PATH_CORE,
            }, "name")
            if existing:
                continue
            ac = frappe.new_doc("ArchetypeConfig")
            ac.batch = cls.batch_name
            ac.experiment_arm = "default"
            ac.archetype = archetype
            ac.path = PATH_CORE
            ac.is_active = 1
            for w, st in weeks.items():
                ac.append("week_rules", {
                    "week": w, "expected_submission_type": st,
                })
            ac.insert(ignore_permissions=True)

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    def test_current_week_change_recomputes_submission_type(
        self, mock_reconcile, _mock_guard,
    ):
        """Caller passes current_week=3. The PE's
        current_expected_submission_type must change from 'image' (week 1)
        to 'video' (week 3) per the fence_sitter/default/Core rule."""
        student = _ensure_student("US1")
        pe = _make_advanced_pe(self.batch_name, student, "US1")
        # Start at week 1 with expected='image' matching the fence_sitter
        # rule for the fixture (so we can observe the recompute clearly).
        pe.current_week = 1
        pe.archetype = "fence_sitter"
        pe.experiment_arm = "default"
        pe.current_expected_submission_type = "image"
        pe.save(ignore_permissions=True)

        mock_reconcile.return_value = {
            "pe": pe.name, "glific_id": pe.glific_id, "diff": [], "pushed": True,
        }

        result = update_student_state(student, current_week=3)
        pe.reload()

        self.assertEqual(pe.current_week, 3)
        self.assertEqual(
            pe.current_expected_submission_type, "video",
            "current_week change must trigger recompute to week-3 rule",
        )
        self.assertIn("pe.current_expected_submission_type", result["applied"])
        self.assertEqual(
            result["applied"]["pe.current_expected_submission_type"], "video",
        )
        mock_reconcile.assert_called_once()

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    def test_archetype_change_recomputes_submission_type(
        self, mock_reconcile, _mock_guard,
    ):
        """Changing archetype from fence_sitter → dormant at week 1 must
        flip the rule from 'image' (fence_sitter week 1) to 'video'
        (dormant week 1)."""
        student = _ensure_student("US2")
        pe = _make_advanced_pe(self.batch_name, student, "US2")
        pe.current_week = 1
        pe.archetype = "fence_sitter"
        pe.experiment_arm = "default"
        pe.current_expected_submission_type = "image"
        pe.save(ignore_permissions=True)

        # Also update the Student row so the validation passes.
        frappe.db.set_value("Student", student, {
            "archetype": "fence_sitter", "experiment_arm": "default",
        })

        mock_reconcile.return_value = {
            "pe": pe.name, "glific_id": pe.glific_id, "diff": [], "pushed": True,
        }

        result = update_student_state(student, archetype="dormant")
        pe.reload()

        self.assertEqual(pe.archetype, "dormant")
        self.assertEqual(
            pe.current_expected_submission_type, "video",
            "archetype change must trigger recompute to new archetype's "
            "week-1 rule (dormant: video)",
        )

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    def test_no_relevant_change_does_not_recompute(
        self, mock_reconcile, _mock_guard,
    ):
        """If the caller only changes program_status (NOT week / path /
        archetype / arm), current_expected_submission_type must stay
        untouched — we should not gratuitously rewrite it on every
        update call."""
        student = _ensure_student("US3")
        pe = _make_advanced_pe(self.batch_name, student, "US3")
        pe.current_week = 2
        pe.archetype = "fence_sitter"
        pe.experiment_arm = "default"
        # Set a value that does NOT match any WeekRule — proves the
        # recompute logic isn't firing.
        pe.current_expected_submission_type = "manual_override_value"
        pe.save(ignore_permissions=True)

        mock_reconcile.return_value = {
            "pe": pe.name, "glific_id": pe.glific_id, "diff": [], "pushed": True,
        }

        result = update_student_state(student, program_status="paused")
        pe.reload()

        self.assertEqual(pe.program_status, "paused")
        self.assertEqual(
            pe.current_expected_submission_type, "manual_override_value",
            "program_status-only change must NOT recompute "
            "current_expected_submission_type",
        )
        self.assertNotIn(
            "pe.current_expected_submission_type", result["applied"],
            "applied diff must not include current_expected_submission_type "
            "when no relevant field changed",
        )

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    def test_irregular_submitter_archetype_now_accepted(self, _mock_guard):
        """Task #84 follow-up: _VALID_ARCHETYPES is now sourced from
        constants.ALL_ARCHETYPES. The canonical value 'irregular_submitter'
        must be accepted (previously rejected because the hardcoded list
        had 'lurker' but missed 'irregular_submitter')."""
        student = _ensure_student("US4")
        _make_advanced_pe(self.batch_name, student, "US4")

        # Dry-run so we don't have to mock reconcile — we just want to
        # confirm validation accepts the canonical archetype name.
        result = update_student_state(
            student, archetype="irregular_submitter", dry_run=True,
        )
        self.assertEqual(result["dry_run"], True)
        # If we got here without a ValidationError, the canonical value
        # is now accepted by validation.


# ════════════════════════════════════════════════════════════
# 5. Task #87 — create_test_student_with_pe (one-call Student + PE)
# ════════════════════════════════════════════════════════════

class TestCreateTestStudentWithPe(FrappeTestCase):
    """Task #87: completes the dev_tools quartet.

    Verifies the minimal-input contract (name + phone + batch), idempotency
    on (phone, name1) for Student and (student, batch) for PE, validation
    of archetype/arm, and the default skip_glific_sync=True behavior.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    def test_minimal_input_creates_student_and_pe(
        self, mock_reconcile, _mock_guard,
    ):
        """Three required args (name, phone, batch) is enough — every other
        field gets a sensible default. Verifies the headline ergonomic of
        this API: 1-line student onboarding for tests."""
        result = create_test_student_with_pe(
            name=f"CreateTest1-{frappe.utils.random_string(4)}",
            phone="+919999700001",
            batch=self.batch_name,
        )

        self.assertEqual(result["dry_run"], False)
        self.assertTrue(result["created_student"])
        self.assertTrue(result["created_pe"])
        self.assertIsNotNone(result["student_id"])
        self.assertIsNotNone(result["pe_name"])

        # PE should match _process_pe_chunk's enrollment-time defaults.
        pe = frappe.get_doc("ProgramEnrollment", result["pe_name"])
        self.assertEqual(pe.current_week, 1)
        self.assertEqual(pe.current_path, PATH_CORE)
        self.assertEqual(pe.current_tier, "Basic")
        self.assertEqual(pe.resolved_flow_state, STATE_NORMAL_CONTENT)
        self.assertEqual(pe.journey_label, LABEL_ENROLLED)
        self.assertEqual(pe.program_status, PROGRAM_ACTIVE)
        self.assertEqual(pe.archetype, "submitter")
        self.assertEqual(pe.experiment_arm, "default")
        self.assertEqual(pe.total_points, 0)
        self.assertEqual(pe.current_escalation_step, 0)

        # Student.phone normalized to 12-digit form.
        student = frappe.get_doc("Student", result["student_id"])
        self.assertEqual(student.phone, "919999700001")
        self.assertEqual(student.archetype, "submitter")

        # skip_glific_sync=True default → reconcile NOT called.
        mock_reconcile.assert_not_called()

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    def test_idempotent_on_rerun(self, mock_reconcile, _mock_guard):
        """Second call with the same (name, phone, batch) reuses both
        Student and PE rather than creating duplicates. Critical for
        test workflows that run repeatedly against the same fixture."""
        kwargs = {
            "name": f"CreateTest2-{frappe.utils.random_string(4)}",
            "phone": "+919999700002",
            "batch": self.batch_name,
        }

        first = create_test_student_with_pe(**kwargs)
        second = create_test_student_with_pe(**kwargs)

        # Same student and PE returned both times
        self.assertEqual(first["student_id"], second["student_id"])
        self.assertEqual(first["pe_name"], second["pe_name"])

        # First call created everything, second call reused
        self.assertTrue(first["created_student"])
        self.assertTrue(first["created_pe"])
        self.assertFalse(second["created_student"])
        self.assertFalse(second["created_pe"])

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    def test_glific_sync_when_enabled(self, mock_reconcile, _mock_guard):
        """When skip_glific_sync=False AND glific_id is set, reconcile runs.
        Mirrors the production sync path so tests can exercise it
        deliberately without polluting the default fast-path."""
        mock_reconcile.return_value = {
            "pe": "x", "glific_id": "test-glific-001",
            "diff": [], "pushed": True,
        }

        result = create_test_student_with_pe(
            name=f"CreateTest3-{frappe.utils.random_string(4)}",
            phone="+919999700003",
            batch=self.batch_name,
            glific_id="test-glific-001",
            skip_glific_sync=False,
        )

        self.assertTrue(result["created_pe"])
        self.assertTrue(result["glific_synced"])
        mock_reconcile.assert_called_once()

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    def test_validates_archetype_and_batch(self, _mock_guard):
        """Bad enum values and missing batch raise ValidationError early —
        before any DB writes. Saves time on typo'd test invocations."""
        # Invalid archetype
        with self.assertRaises(frappe.ValidationError):
            create_test_student_with_pe(
                name="CreateTest4-bad-archetype",
                phone="+919999700004",
                batch=self.batch_name,
                archetype="not_a_real_archetype",
            )

        # Invalid arm
        with self.assertRaises(frappe.ValidationError):
            create_test_student_with_pe(
                name="CreateTest4-bad-arm",
                phone="+919999700005",
                batch=self.batch_name,
                experiment_arm="not_a_real_arm",
            )

        # Missing batch
        with self.assertRaises(frappe.ValidationError):
            create_test_student_with_pe(
                name="CreateTest4-no-batch",
                phone="+919999700006",
                batch="DoesNotExistBatch",
            )

        # Empty required field
        with self.assertRaises(frappe.ValidationError):
            create_test_student_with_pe(
                name="", phone="+919999700007", batch=self.batch_name,
            )

    @patch("tap_lms.summer_program.dev_tools._assert_dev_site")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    def test_dry_run_no_writes(self, mock_reconcile, _mock_guard):
        """dry_run=True returns the computed payload without touching DB.
        Useful for previewing what an op would produce without committing."""
        before_student_count = frappe.db.count("Student")
        before_pe_count = frappe.db.count("ProgramEnrollment")

        result = create_test_student_with_pe(
            name=f"CreateTest5-dry-{frappe.utils.random_string(4)}",
            phone="+919999700008",
            batch=self.batch_name,
            dry_run=True,
        )

        self.assertEqual(result["dry_run"], True)
        self.assertIsNone(result["student_id"])
        self.assertIsNone(result["pe_name"])
        self.assertIn("would_use", result)
        # No DB writes
        self.assertEqual(frappe.db.count("Student"), before_student_count)
        self.assertEqual(frappe.db.count("ProgramEnrollment"), before_pe_count)
        mock_reconcile.assert_not_called()
