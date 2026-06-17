"""
CR-007 regression tests — submission point award + validation gate.

What's being pinned (per user spec 2026-05-19 evening):

  AI validation always runs. Submission.result_status is populated by the
  microservice on every submission. WeekRule.submission_validation_enabled
  is the SINGLE GATE that controls TWO coupled behaviors:

    1. Whether failed/flagged submissions route to Remedial path
       (t6b_failed_feedback_to_remedial) vs. stay on Core (t12_feedback_ready).
    2. Whether submission points are gated by AI validity:
         - validation OFF (W1-2 by spec): Assignment.points_per_item always
         - validation ON, AI valid     : Assignment.points_per_item
         - validation ON, AI Failed/Flagged: 0 points
       Escalation submissions (sent_count >= 1) get
       EscalationStep.points_awarded regardless of validity.

  Streak / gems / weekly_submission_done bump on every submission regardless
  of validity — that happens at save_submission time via the submission
  transitions (T7/T9/T17/T3) with points=0, and is independent of this hook.

These tests mock the microservice + Glific layers and exercise only the
in-process logic of `feedback_consumer_hook.on_feedback_ready`. They do NOT
hit Glific, RabbitMQ, or any real flow.
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch, MagicMock


# ════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════

def _fake_pe(name="pe-test-001", current_escalation_step=0,
              current_week=1, current_path="Core"):
    """Minimal PE-like object with the fields _compute_submission_points reads.

    Using frappe._dict so attribute access works the way the helper expects.
    """
    return frappe._dict({
        "name": name,
        "student": "STU-CR007-001",
        "batch": "palv2-test-CR007",
        "archetype": "fence_sitter",
        "experiment_arm": "default",
        "current_path": current_path,
        "current_week": current_week,
        "current_escalation_step": current_escalation_step,
        "glific_id": "",
    })


def _mock_assignment_lookup(points_per_item):
    """Build a side_effect for frappe.db.get_value that returns appropriate
    values per (doctype, name, field) tuple — covering the calls the hook makes.
    """
    def side_effect(*args, **kwargs):
        # frappe.db.get_value("Submission", name, "field") or
        #                    ("Assignment", name, "field"), etc.
        if len(args) < 2:
            return None
        doctype, _name = args[0], args[1]
        field = args[2] if len(args) >= 3 else None
        if doctype == "Assignment" and field == "points_per_item":
            return points_per_item
        return None
    return side_effect


# ════════════════════════════════════════════════════════════
# 1. _compute_submission_points — the core logic
# ════════════════════════════════════════════════════════════

class TestComputeSubmissionPoints(FrappeTestCase):
    """Unit tests for the new point-resolution function. Mocks all Frappe
    DB lookups so we exercise pure logic."""

    def test_lax_mode_valid_returns_points_per_item(self):
        """W1-2 (validation OFF) + AI valid → Assignment.points_per_item."""
        from tap_lms.summer_program.feedback_consumer_hook import (
            _compute_submission_points,
        )
        pe = _fake_pe(current_escalation_step=0)

        with patch("tap_lms.summer_program.feedback_consumer_hook.frappe") as mock_frappe, \
             patch("tap_lms.summer_program.feedback_consumer_hook._get_week_rule_for_pe",
                   return_value={"submission_validation_enabled": 0}):
            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                25 if args[:1] == ("Assignment",) and args[2:3] == ("points_per_item",)
                else (1 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else "ASN-001" if args[:1] == ("Submission",) and args[2:3] == ("assign_id",)
                      else None)
            )
            points = _compute_submission_points(pe, "SUB-001", result_status="Success")

        self.assertEqual(points, 25)

    def test_lax_mode_failed_still_returns_points_per_item(self):
        """W1-2 (validation OFF): even an AI-failed submission gets full points.
        This is the user spec — lax mode preserves the per-item award regardless
        of AI verdict. Failed submissions also do NOT route to Remedial in lax mode
        (covered by TestRoutingGate below)."""
        from tap_lms.summer_program.feedback_consumer_hook import (
            _compute_submission_points,
        )
        pe = _fake_pe(current_escalation_step=0)

        with patch("tap_lms.summer_program.feedback_consumer_hook.frappe") as mock_frappe, \
             patch("tap_lms.summer_program.feedback_consumer_hook._get_week_rule_for_pe",
                   return_value={"submission_validation_enabled": 0}):
            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                25 if args[:1] == ("Assignment",) and args[2:3] == ("points_per_item",)
                else (1 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else "ASN-001" if args[:1] == ("Submission",) and args[2:3] == ("assign_id",)
                      else None)
            )
            points = _compute_submission_points(pe, "SUB-001", result_status="Failed")

        self.assertEqual(points, 25, "Validation OFF must preserve points_per_item even on Failed")

    def test_strict_mode_valid_returns_points_per_item(self):
        """W3+ (validation ON) + AI valid → Assignment.points_per_item."""
        from tap_lms.summer_program.feedback_consumer_hook import (
            _compute_submission_points,
        )
        pe = _fake_pe(current_escalation_step=0, current_week=3)

        with patch("tap_lms.summer_program.feedback_consumer_hook.frappe") as mock_frappe, \
             patch("tap_lms.summer_program.feedback_consumer_hook._get_week_rule_for_pe",
                   return_value={"submission_validation_enabled": 1}):
            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                25 if args[:1] == ("Assignment",) and args[2:3] == ("points_per_item",)
                else (3 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else "ASN-001" if args[:1] == ("Submission",) and args[2:3] == ("assign_id",)
                      else None)
            )
            points = _compute_submission_points(pe, "SUB-001", result_status="Success")

        self.assertEqual(points, 25)

    def test_strict_mode_failed_returns_zero(self):
        """W3+ (validation ON) + AI Failed → 0 points (and routes to Remedial)."""
        from tap_lms.summer_program.feedback_consumer_hook import (
            _compute_submission_points,
        )
        pe = _fake_pe(current_escalation_step=0, current_week=3)

        with patch("tap_lms.summer_program.feedback_consumer_hook.frappe") as mock_frappe, \
             patch("tap_lms.summer_program.feedback_consumer_hook._get_week_rule_for_pe",
                   return_value={"submission_validation_enabled": 1}):
            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                25 if args[:1] == ("Assignment",) and args[2:3] == ("points_per_item",)
                else (3 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else "ASN-001" if args[:1] == ("Submission",) and args[2:3] == ("assign_id",)
                      else None)
            )
            points = _compute_submission_points(pe, "SUB-001", result_status="Failed")

        self.assertEqual(points, 0)

    def test_strict_mode_flagged_returns_zero(self):
        """W3+ (validation ON) + AI 'Success - Flagged' → 0 points.
        Same routing/awarding as Failed — Flagged is a Failed-equivalent verdict."""
        from tap_lms.summer_program.feedback_consumer_hook import (
            _compute_submission_points,
        )
        pe = _fake_pe(current_escalation_step=0, current_week=3)

        with patch("tap_lms.summer_program.feedback_consumer_hook.frappe") as mock_frappe, \
             patch("tap_lms.summer_program.feedback_consumer_hook._get_week_rule_for_pe",
                   return_value={"submission_validation_enabled": 1}):
            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                25 if args[:1] == ("Assignment",) and args[2:3] == ("points_per_item",)
                else (3 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else "ASN-001" if args[:1] == ("Submission",) and args[2:3] == ("assign_id",)
                      else None)
            )
            points = _compute_submission_points(
                pe, "SUB-001", result_status="Success - Flagged"
            )

        self.assertEqual(points, 0)

    def test_escalation_uses_escalation_step_points_regardless_of_validity(self):
        """sent_count >= 1 → EscalationStep.points_awarded, independent of
        validation flag and result_status. Escalation is its own tier system.

        This test patches `_escalation_points` itself so it exercises only
        the dispatch in `_compute_submission_points`, NOT the indexing logic
        inside _escalation_points. The indexing contract (sent_count is
        1-indexed, steps is 0-indexed) is covered by
        TestEscalationPointsIndexing below.
        """
        from tap_lms.summer_program.feedback_consumer_hook import (
            _compute_submission_points,
        )
        pe = _fake_pe(current_escalation_step=2, current_week=3)

        # Fake escalation step config: [25, 15, 10, 5] (4 steps configured)
        # Per the fixed indexing (2026-05-22), sent_count=2 → steps[1] = 15.
        fake_steps = [
            {"points_awarded": 25},  # step 1 — sent_count=1 would return this
            {"points_awarded": 15},  # step 2 — sent_count=2 returns this
            {"points_awarded": 10},
            {"points_awarded": 5},
        ]

        with patch("tap_lms.summer_program.feedback_consumer_hook._escalation_points",
                   return_value=fake_steps[1]["points_awarded"]) as mock_esc:
            points = _compute_submission_points(pe, "SUB-001", result_status="Failed")

        # sent_count=2 → step index 1 (0-indexed) → step 2's reward → 15 points
        self.assertEqual(points, 15)
        mock_esc.assert_called_once()

    def test_missing_assign_id_returns_zero_with_warning(self):
        """If Submission.assign_id is empty/None, log a warning and award 0
        instead of crashing or guessing."""
        from tap_lms.summer_program.feedback_consumer_hook import (
            _compute_submission_points,
        )
        pe = _fake_pe(current_escalation_step=0)

        with patch("tap_lms.summer_program.feedback_consumer_hook.frappe") as mock_frappe, \
             patch("tap_lms.summer_program.feedback_consumer_hook._get_week_rule_for_pe",
                   return_value={"submission_validation_enabled": 0}):
            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                1 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                else None  # assign_id returns None
            )
            mock_frappe.logger.return_value = MagicMock()
            points = _compute_submission_points(pe, "SUB-001", result_status="Success")

        self.assertEqual(points, 0)
        mock_frappe.logger().warning.assert_called()

    def test_missing_week_rule_treated_as_lax(self):
        """If WeekRule is missing for the week (config gap), treat as
        validation OFF so the student still gets their points. Safer default."""
        from tap_lms.summer_program.feedback_consumer_hook import (
            _compute_submission_points,
        )
        pe = _fake_pe(current_escalation_step=0)

        with patch("tap_lms.summer_program.feedback_consumer_hook.frappe") as mock_frappe, \
             patch("tap_lms.summer_program.feedback_consumer_hook._get_week_rule_for_pe",
                   return_value=None):
            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                25 if args[:1] == ("Assignment",) and args[2:3] == ("points_per_item",)
                else (1 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else "ASN-001" if args[:1] == ("Submission",) and args[2:3] == ("assign_id",)
                      else None)
            )
            # Even with Failed result, no WeekRule → defaults to lax → full points
            points = _compute_submission_points(pe, "SUB-001", result_status="Failed")

        self.assertEqual(points, 25, "Missing WeekRule must default to lax (full points)")


# ════════════════════════════════════════════════════════════
# 2. Routing gate — does t6b vs t12 fire correctly?
# ════════════════════════════════════════════════════════════

class TestRoutingGate(FrappeTestCase):
    """Pin the t6b-vs-t12 routing decision in `on_feedback_ready`. The gate is:
    `submission_validation_enabled == 1` AND `result_status in (Failed, Flagged)`
    → t6b. Anything else → t12."""

    def _run_hook(self, result_status, validation_enabled):
        from tap_lms.summer_program import feedback_consumer_hook
        with patch.object(feedback_consumer_hook, "frappe") as mock_frappe, \
             patch.object(feedback_consumer_hook, "_get_week_rule_for_pe",
                          return_value={"submission_validation_enabled":
                                        1 if validation_enabled else 0}), \
             patch.object(feedback_consumer_hook, "_compute_submission_points",
                          return_value=0), \
             patch.object(feedback_consumer_hook, "_award_submission_points_atomic"), \
             patch.object(feedback_consumer_hook, "_sync_contact_fields"), \
             patch("tap_lms.summer_program.state_machine.t12_feedback_ready") as t12, \
             patch("tap_lms.summer_program.state_machine.t6b_failed_feedback_to_remedial") as t6b:

            # Build minimal DB plumbing
            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                "STU-001" if args[:1] == ("Submission",) and args[2:3] == ("student_id",)
                else (1 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else (result_status if args[:1] == ("Submission",) and args[2:3] == ("result_status",)
                            else ("PE-001" if args[:1] == ("ProgramEnrollment",)
                                  else None)))
            )

            pe_mock = MagicMock()
            pe_mock.name = "PE-001"
            pe_mock.current_week = 1
            pe_mock.glific_id = ""
            mock_frappe.get_doc.return_value = pe_mock

            result = feedback_consumer_hook.on_feedback_ready(
                "SUB-001", student_id="STU-001"
            )

        return result, t6b.called, t12.called

    def test_strict_mode_failed_routes_to_remedial(self):
        result, t6b_called, t12_called = self._run_hook(
            result_status="Failed", validation_enabled=True,
        )
        self.assertTrue(t6b_called, "Strict + Failed must call t6b")
        self.assertFalse(t12_called)
        self.assertEqual(result.get("branch"), "remedial")

    def test_strict_mode_flagged_routes_to_remedial(self):
        result, t6b_called, t12_called = self._run_hook(
            result_status="Success - Flagged", validation_enabled=True,
        )
        self.assertTrue(t6b_called, "Strict + Flagged must call t6b")
        self.assertFalse(t12_called)
        self.assertEqual(result.get("branch"), "remedial")

    def test_strict_mode_success_routes_to_feedback_ready(self):
        result, t6b_called, t12_called = self._run_hook(
            result_status="Success", validation_enabled=True,
        )
        self.assertFalse(t6b_called)
        self.assertTrue(t12_called, "Strict + Success must call t12")
        self.assertEqual(result.get("branch"), "feedback_ready")

    def test_lax_mode_failed_stays_on_core(self):
        """W1-2 spec: failed submissions DO NOT route to Remedial in lax mode.
        This is the regression test for the team's '1 bad submission =
        permanent Remedial' surprise scenario."""
        result, t6b_called, t12_called = self._run_hook(
            result_status="Failed", validation_enabled=False,
        )
        self.assertFalse(t6b_called, "Lax + Failed must NOT call t6b")
        self.assertTrue(t12_called)
        self.assertEqual(result.get("branch"), "feedback_ready")

    def test_lax_mode_flagged_stays_on_core(self):
        result, t6b_called, t12_called = self._run_hook(
            result_status="Success - Flagged", validation_enabled=False,
        )
        self.assertFalse(t6b_called)
        self.assertTrue(t12_called)
        self.assertEqual(result.get("branch"), "feedback_ready")


# ════════════════════════════════════════════════════════════
# 3. End-to-end: atomic SQL award fires through on_feedback_ready
# ════════════════════════════════════════════════════════════

class TestAtomicAwardEndToEnd(FrappeTestCase):
    """Pins the full chain: on_feedback_ready → _compute_submission_points
    (non-zero) → _award_submission_points_atomic called with the computed
    value. Code-review caught that TestRoutingGate alone doesn't prove the
    atomic SQL bump actually executes with real points (it mocks
    _compute_submission_points to 0, which short-circuits the bump).

    This test mocks _compute_submission_points to return 25 and asserts
    _award_submission_points_atomic was called with (pe_name, 25).
    """

    def test_on_feedback_ready_awards_points_atomically_when_computed_nonzero(self):
        from tap_lms.summer_program import feedback_consumer_hook

        with patch.object(feedback_consumer_hook, "frappe") as mock_frappe, \
             patch.object(feedback_consumer_hook, "_get_week_rule_for_pe",
                          return_value={"submission_validation_enabled": 0}), \
             patch.object(feedback_consumer_hook, "_compute_submission_points",
                          return_value=25) as mock_compute, \
             patch.object(feedback_consumer_hook, "_award_submission_points_atomic") as mock_award, \
             patch.object(feedback_consumer_hook, "_sync_contact_fields"), \
             patch("tap_lms.summer_program.state_machine.t12_feedback_ready") as t12:

            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                "STU-001" if args[:1] == ("Submission",) and args[2:3] == ("student_id",)
                else (1 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else ("Success" if args[:1] == ("Submission",) and args[2:3] == ("result_status",)
                            else ("PE-001" if args[:1] == ("ProgramEnrollment",)
                                  else None)))
            )

            pe_mock = MagicMock()
            pe_mock.name = "PE-001"
            pe_mock.current_week = 1
            pe_mock.glific_id = ""
            mock_frappe.get_doc.return_value = pe_mock

            result = feedback_consumer_hook.on_feedback_ready(
                "SUB-001", student_id="STU-001"
            )

        # _compute_submission_points was called once with the PE, submission, status
        mock_compute.assert_called_once()
        # _award_submission_points_atomic was called with (pe.name, 25)
        mock_award.assert_called_once_with("PE-001", 25)
        # t12 fires (not t6b — result was Success)
        t12.assert_called_once()
        # Response payload reports the points awarded
        self.assertEqual(result.get("points_awarded"), 25)

    def test_on_feedback_ready_skips_atomic_award_when_points_zero(self):
        """Confirm short-circuit: when _compute_submission_points returns 0
        (e.g., strict-mode Failed), the atomic UPDATE is NOT executed.
        Prevents pointless DB round-trips."""
        from tap_lms.summer_program import feedback_consumer_hook

        with patch.object(feedback_consumer_hook, "frappe") as mock_frappe, \
             patch.object(feedback_consumer_hook, "_get_week_rule_for_pe",
                          return_value={"submission_validation_enabled": 1}), \
             patch.object(feedback_consumer_hook, "_compute_submission_points",
                          return_value=0), \
             patch.object(feedback_consumer_hook, "_award_submission_points_atomic") as mock_award, \
             patch.object(feedback_consumer_hook, "_sync_contact_fields"), \
             patch("tap_lms.summer_program.state_machine.t12_feedback_ready"), \
             patch("tap_lms.summer_program.state_machine.t6b_failed_feedback_to_remedial"):

            mock_frappe.db.get_value.side_effect = lambda *args, **kwargs: (
                "STU-001" if args[:1] == ("Submission",) and args[2:3] == ("student_id",)
                else (3 if args[:1] == ("Submission",) and args[2:3] == ("week",)
                      else ("Failed" if args[:1] == ("Submission",) and args[2:3] == ("result_status",)
                            else ("PE-001" if args[:1] == ("ProgramEnrollment",)
                                  else None)))
            )

            pe_mock = MagicMock()
            pe_mock.name = "PE-001"
            pe_mock.current_week = 3
            pe_mock.glific_id = ""
            mock_frappe.get_doc.return_value = pe_mock

            feedback_consumer_hook.on_feedback_ready(
                "SUB-001", student_id="STU-001"
            )

        # Award helper NEVER called when computed points == 0
        mock_award.assert_not_called()


# ════════════════════════════════════════════════════════════
# 4. _escalation_points — the indexing contract (CRITICAL — 2026-05-22)
# ════════════════════════════════════════════════════════════

class TestEscalationPointsIndexing(FrappeTestCase):
    """Pin the indexing contract for `_escalation_points`.

    `sent_count` (== `pe.current_escalation_step`) is 1-indexed: the
    dispatcher writes `next_step = current_step + 1` via t2/t4/t8/t10. The
    dispatcher itself reads `steps[next_step - 1]` (pe_dispatcher.py:351)
    when firing a step — so step N's config lives at `steps[N - 1]`. A
    student who responds late after escalation step 1 fired has
    `current_escalation_step == 1` and must receive `steps[0].points_awarded`.

    The previous indexing was `min(sent_count, len(steps)-1)` which
    returned `steps[1].points_awarded` (step 2's reward) for sent_count==1.
    Bug masked by single-step configs where `len(steps)-1` clamped the
    index back to 0. Fix (2026-05-22): subtract 1 before clamping.
    """

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_step_1_returns_first_step_points(self, mock_get_steps, mock_frappe):
        """sent_count=1 → steps[0].points_awarded (regression for the
        off-by-one). With chain [25, 15, 10, 5], step 1's reward is 25."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points
        mock_get_steps.return_value = [
            {"points_awarded": 25},  # step 1
            {"points_awarded": 15},  # step 2
            {"points_awarded": 10},  # step 3
            {"points_awarded": 5},   # step 4
        ]
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )

        pe = _fake_pe(current_escalation_step=1)
        points = _escalation_points(pe, sent_count=1)

        self.assertEqual(points, 25,
                         "sent_count=1 must return steps[0].points_awarded "
                         "(the first step's reward)")

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_step_2_returns_second_step_points(self, mock_get_steps, mock_frappe):
        """sent_count=2 → steps[1].points_awarded. With chain [25, 15, 10, 5],
        step 2's reward is 15."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points
        mock_get_steps.return_value = [
            {"points_awarded": 25},
            {"points_awarded": 15},
            {"points_awarded": 10},
            {"points_awarded": 5},
        ]
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )

        pe = _fake_pe(current_escalation_step=2)
        points = _escalation_points(pe, sent_count=2)

        self.assertEqual(points, 15,
                         "sent_count=2 must return steps[1].points_awarded")

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_step_beyond_chain_saturates_at_last(self, mock_get_steps, mock_frappe):
        """sent_count larger than configured chain → clamp to last step
        (preserves pre-CR-007 saturation behavior). With 3-step chain
        [25, 15, 10], sent_count=5 must return steps[-1] == 10."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points
        mock_get_steps.return_value = [
            {"points_awarded": 25},
            {"points_awarded": 15},
            {"points_awarded": 10},
        ]
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )

        pe = _fake_pe(current_escalation_step=5)
        points = _escalation_points(pe, sent_count=5)

        self.assertEqual(points, 10,
                         "sent_count beyond chain length must clamp to "
                         "last step's points_awarded")

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_empty_step_config_returns_zero(self, mock_get_steps, mock_frappe):
        """No EscalationStep config → return 0 (don't crash on negative
        index lookup or empty list access)."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points
        mock_get_steps.return_value = []
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )

        pe = _fake_pe(current_escalation_step=1)
        points = _escalation_points(pe, sent_count=1)

        self.assertEqual(points, 0)

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_sent_count_zero_does_not_underflow(self, mock_get_steps, mock_frappe):
        """Defensive: sent_count=0 should never hit `_escalation_points` in
        production (the caller short-circuits to the on-time path), but if
        it does, the clamp must prevent a negative index. Returns steps[0]."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points
        mock_get_steps.return_value = [
            {"points_awarded": 25},
            {"points_awarded": 15},
        ]
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )

        pe = _fake_pe(current_escalation_step=0)
        points = _escalation_points(pe, sent_count=0)

        # max(0-1, 0) = 0 → steps[0].points_awarded
        self.assertEqual(points, 25,
                         "sent_count=0 (defensive) must clamp to steps[0], "
                         "not underflow to steps[-1]")


# ════════════════════════════════════════════════════════════
# 5. Path-aware escalation lookup (task #68 — 2026-05-22)
# ════════════════════════════════════════════════════════════

class TestPathAwareEscalationLookup(FrappeTestCase):
    """Pin the Core / Remedial split on escalation step lookup.

    Diagnostic against palv2-test-BT52231 showed every Remedial-path
    ArchetypeConfig has populated escalation_steps with different counts
    than Core (e.g. arm_b/fence_sitter/Remedial has 5 steps vs Core's 4).
    Pre-fix, `_get_escalation_steps` hardcoded PATH_CORE, so Remedial
    students received Core's cadence + Core's point rewards.

    After the 2026-05-22 fix, `_get_escalation_steps` accepts a `path`
    parameter and callers thread `pe.current_path` through. If Remedial
    config has no steps, callers fall back to Core silently (warning
    logged) so the student still gets a nudge.
    """

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_core_path_pulls_core_config(self, mock_get_steps, mock_frappe):
        """PE.current_path = 'Core' → _get_escalation_steps called with
        path='Core'."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points
        mock_get_steps.return_value = [{"points_awarded": 25}]
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )

        pe = _fake_pe(current_escalation_step=1, current_path="Core")
        _escalation_points(pe, sent_count=1)

        mock_get_steps.assert_called_once()
        # Inspect the path kwarg (or positional arg) passed to _get_escalation_steps
        call = mock_get_steps.call_args
        path_passed = call.kwargs.get("path") or (call.args[2] if len(call.args) >= 3 else None)
        self.assertEqual(path_passed, "Core",
                         "Core PE must pull Core's escalation config")

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_remedial_path_pulls_remedial_config(self, mock_get_steps, mock_frappe):
        """PE.current_path = 'Remedial' → _get_escalation_steps called with
        path='Remedial' (not silently defaulted to Core)."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points
        # Non-empty so the fallback branch doesn't activate
        mock_get_steps.return_value = [
            {"points_awarded": 12},  # Remedial step 1 — distinct from Core
            {"points_awarded": 8},
        ]
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )

        pe = _fake_pe(current_escalation_step=1, current_path="Remedial")
        points = _escalation_points(pe, sent_count=1)

        mock_get_steps.assert_called_once()
        call = mock_get_steps.call_args
        path_passed = call.kwargs.get("path") or (call.args[2] if len(call.args) >= 3 else None)
        self.assertEqual(path_passed, "Remedial",
                         "Remedial PE must pull Remedial's escalation config")
        self.assertEqual(points, 12,
                         "Remedial chain's step-1 reward must be returned, "
                         "not Core's")

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_empty_remedial_config_falls_back_to_core(self, mock_get_steps, mock_frappe):
        """If a Remedial PE's ArchetypeConfig has no escalation_steps
        configured, fall back to Core so the student still gets a reward
        (with an operator-visible warning logged)."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points

        # Two calls expected: first with path='Remedial' returns []; fallback
        # call with path='Core' returns the actual chain.
        mock_get_steps.side_effect = [
            [],  # Remedial config is empty
            [{"points_awarded": 25}, {"points_awarded": 15}],  # Core fallback
        ]
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )
        mock_frappe.logger.return_value = MagicMock()

        pe = _fake_pe(current_escalation_step=1, current_path="Remedial")
        points = _escalation_points(pe, sent_count=1)

        self.assertEqual(mock_get_steps.call_count, 2,
                         "Empty Remedial config must trigger Core fallback "
                         "(second _get_escalation_steps call)")
        # Verify the fallback call passed path=Core
        second_call = mock_get_steps.call_args_list[1]
        path_passed = (second_call.kwargs.get("path")
                       or (second_call.args[2] if len(second_call.args) >= 3 else None))
        self.assertEqual(path_passed, "Core")
        self.assertEqual(points, 25, "Fallback must return Core's step-1 reward")
        # Operator-visible warning
        mock_frappe.logger().warning.assert_called()

    @patch("tap_lms.summer_program.feedback_consumer_hook.frappe")
    @patch("tap_lms.summer_program.student_progression_sp._get_escalation_steps")
    def test_missing_current_path_defaults_to_core(self, mock_get_steps, mock_frappe):
        """Defensive: a PE with empty/None current_path (legacy data) must
        default to Core lookup, not crash on the None being passed to the
        ArchetypeConfig Select filter."""
        from tap_lms.summer_program.feedback_consumer_hook import _escalation_points
        mock_get_steps.return_value = [{"points_awarded": 25}]
        mock_frappe.get_doc.return_value = MagicMock(
            archetype="fence_sitter", experiment_arm="default",
        )

        pe = _fake_pe(current_escalation_step=1, current_path="")  # empty
        _escalation_points(pe, sent_count=1)

        call = mock_get_steps.call_args
        path_passed = call.kwargs.get("path") or (call.args[2] if len(call.args) >= 3 else None)
        self.assertEqual(path_passed, "Core",
                         "Empty current_path must default to Core, not pass "
                         "an empty string through to the config lookup")


# ════════════════════════════════════════════════════════════
# 6. Defensive error-path hardening (task #69 — 2026-05-22)
# ════════════════════════════════════════════════════════════

class TestFeedbackHookErrorPathHardening(FrappeTestCase):
    """Pin task #69's defensive rollback + log_error fallback.

    The `except Exception` block in `on_feedback_ready` previously called
    `frappe.log_error` unguarded. If the originating exception poisoned
    the Postgres txn (L-030 InFailedSqlTransaction), log_error itself
    would fail and the error was silently dropped. After the 2026-05-22
    fix:
      - rollback fires before log_error (defensive; harmless if already clean)
      - log_error is wrapped in try/except; on failure, falls back to
        `frappe.logger().error` which is DB-independent
      - message is truncated to a defensive cap to keep pathologically
        long error strings from cascading CharacterLengthExceededError
    """

    def test_rollback_called_before_log_error_on_exception(self):
        """When the hook raises, frappe.db.rollback must be called BEFORE
        frappe.log_error so a poisoned txn doesn't break the safety net."""
        from tap_lms.summer_program import feedback_consumer_hook
        call_order = []

        with patch.object(feedback_consumer_hook, "frappe") as mock_frappe:
            mock_frappe.db.rollback = MagicMock(
                side_effect=lambda *a, **kw: call_order.append("rollback"))
            mock_frappe.log_error = MagicMock(
                side_effect=lambda *a, **kw: call_order.append("log_error"))
            mock_frappe.logger.return_value = MagicMock()
            # Force the early student-id resolution to raise so we hit
            # the except branch deterministically.
            mock_frappe.db.get_value.side_effect = Exception("boom")

            result = feedback_consumer_hook.on_feedback_ready("SUB-X")

        # rollback must precede log_error in the call order
        self.assertIn("rollback", call_order)
        self.assertIn("log_error", call_order)
        self.assertLess(call_order.index("rollback"),
                        call_order.index("log_error"),
                        "rollback must fire before log_error")
        self.assertEqual(result["status"], "error")

    def test_log_error_failure_falls_back_to_logger(self):
        """If frappe.log_error itself raises (cascading from the same
        poisoned-txn or length-exceeded condition that caused the
        original failure), fall back to frappe.logger().error so the
        diagnostic is still surfaced — just to the file log instead of
        the Error Log doctype."""
        from tap_lms.summer_program import feedback_consumer_hook
        logger_mock = MagicMock()

        with patch.object(feedback_consumer_hook, "frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = Exception("original error")
            mock_frappe.log_error.side_effect = Exception("log_error broke too")
            mock_frappe.logger.return_value = logger_mock

            result = feedback_consumer_hook.on_feedback_ready("SUB-X")

        # logger().error must have been called as the fallback
        logger_mock.error.assert_called()
        # And the hook itself returns the error envelope, not a crash
        self.assertEqual(result["status"], "error")


class TestLogEventTruncationGuard(FrappeTestCase):
    """Pin task #29's defensive `_safe_log_error` helper in event_log.

    The helper guards against three failure modes:
      1. Poisoned Postgres txn (rollback first).
      2. Over-long title or message (truncate to defensive caps before
         handing to frappe.log_error).
      3. frappe.log_error itself raising (fall back to file logger).
    """

    def test_log_event_failure_does_not_crash_caller(self):
        """When the ProgramEventLog insert fails for any reason, log_event
        must not propagate the exception to its caller. The error is
        surfaced via the defensive _safe_log_error path."""
        from tap_lms.summer_program import event_log

        # Build a fake enrollment doc — log_event only reads its
        # attributes, doesn't insert it itself.
        fake_enrollment = MagicMock()
        fake_enrollment.name = "PE-001"
        fake_enrollment.student = "STU-001"
        fake_enrollment.batch = "BAT-001"
        fake_enrollment.program_type = "summer"
        fake_enrollment.current_week = 1

        with patch.object(event_log, "frappe") as mock_frappe:
            mock_frappe.new_doc.return_value = MagicMock(
                insert=MagicMock(side_effect=Exception("doctype constraint")))
            mock_frappe.db.rollback = MagicMock()
            mock_frappe.log_error = MagicMock()
            mock_frappe.logger.return_value = MagicMock()

            # Must not raise
            event_log.log_event(
                fake_enrollment, event_type="anything",
                trigger_source="test",
                details={"k": "v" * 10000},  # pathologically long details
            )

        mock_frappe.db.rollback.assert_called()
        mock_frappe.log_error.assert_called()

    def test_safe_log_error_truncates_message_to_cap(self):
        """The defensive cap on the message string keeps a runaway error
        from itself blowing up the Error Log doctype's length limits."""
        from tap_lms.summer_program import event_log

        long_message = "x" * 5000

        with patch.object(event_log, "frappe") as mock_frappe:
            mock_frappe.db.rollback = MagicMock()
            mock_frappe.log_error = MagicMock()

            event_log._safe_log_error(title="test", message=long_message)

        # log_error should have been called with a truncated message
        call_args = mock_frappe.log_error.call_args
        passed_message = call_args.args[0]
        self.assertLessEqual(len(passed_message), event_log._LOG_ERROR_MSG_CAP,
                             "Message must be truncated to the defensive cap")

    def test_safe_log_error_falls_back_to_logger_when_log_error_raises(self):
        """If frappe.log_error itself fails (cascading length error,
        poisoned txn, etc.), the file logger catches the diagnostic."""
        from tap_lms.summer_program import event_log
        file_logger = MagicMock()

        with patch.object(event_log, "frappe") as mock_frappe:
            mock_frappe.db.rollback = MagicMock()
            mock_frappe.log_error = MagicMock(
                side_effect=Exception("log_error itself failed"))
            mock_frappe.logger.return_value = file_logger

            # Must not raise
            event_log._safe_log_error(title="t", message="m")

        file_logger.error.assert_called()
