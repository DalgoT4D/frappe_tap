"""
Tests for CR-003 handle_escalation channel branching.

Covers:
  - help_note_a step fires SP_Escalation Glific flow (no Vocallabs)
  - voice_note step fires SP_Escalation Glific flow
  - parent_call step enqueues Vocallabs job, SKIPS SP_Escalation
  - escalation_order + escalation_type pushed to Glific BEFORE flow trigger
  - Step exhaustion routes to T5 (grace) regardless of submission_count
    (CR-006: T6 removed; remedial reserved for failed-feedback via T6b)
  - Remedial-side exhaustion routes to T11 (grace)
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime
from unittest.mock import patch, MagicMock

from tap_lms.summer_program.tests.factories import make_batch

from tap_lms.summer_program.constants import (
    ACTION_ESCALATION,
    LABEL_CONTENT_DELIVERED,
    PATH_CORE,
    PROGRAM_ACTIVE,
    STATE_GRACE_WAITING,
    STATE_NORMAL_CONTENT,
    STATE_NORMAL_ESCALATION,
    STATE_REMEDIAL_CONTENT,
    CF_ESCALATION_ORDER,
    CF_ESCALATION_TYPE,
)


def _ensure_batch():
    # Delegates to the shared factory (L-037) so this fixture inherits future
    # mandatory-field additions instead of breaking with MandatoryError.
    return make_batch(label="EscBranchBatch", batch_id="EBR01")


def _ensure_student(suffix):
    name = frappe.get_value("Student", {"phone": f"+9999800{suffix}"}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"EscBranchStudent{suffix}"
    s.phone = f"+9999800{suffix}"
    s.glific_id = f"glific-eb-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(batch_name, student_name, suffix, **kwargs):
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-EB-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-eb-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = kwargs.get("resolved_flow_state", STATE_NORMAL_CONTENT)
    pe.journey_label = kwargs.get("journey_label", LABEL_CONTENT_DELIVERED)
    pe.current_path = PATH_CORE
    pe.current_week = 1
    pe.current_tier = "Basic"
    pe.archetype = "submitter"
    pe.current_escalation_step = kwargs.get("current_escalation_step", 0)
    pe.submission_count = kwargs.get("submission_count", 0)
    pe.insert(ignore_permissions=True)
    return pe.name


def _step(order, etype, hours=24):
    return {
        "escalation_order": order,
        "escalation_type": etype,
        "points_awarded": 0,
        "hours_after_previous": hours,
    }


def _patch_escalation_steps(steps):
    """Patch _get_escalation_steps_for_pe to return our fixture steps."""
    return patch(
        "tap_lms.summer_program.pe_dispatcher._get_escalation_steps_for_pe",
        return_value=steps,
    )


class TestEscalationBranching(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def test_help_note_a_step_fires_glific_flow(self):
        """escalation_type='help_note_a' → SP_Escalation flow triggered,
        no Vocallabs enqueue."""
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("01")
        pe_name = _make_pe(self.batch_name, s, "01", current_escalation_step=0)

        with _patch_escalation_steps([_step(1, "help_note_a")]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow") as fake_trigger, \
             patch.object(frappe, "enqueue") as fake_enqueue, \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        # SP_Escalation flow was triggered.
        self.assertEqual(fake_trigger.call_count, 1)
        # No Vocallabs enqueue.
        vocallabs_calls = [
            c for c in fake_enqueue.call_args_list
            if "vocallabs" in str(c).lower()
        ]
        self.assertEqual(len(vocallabs_calls), 0)

    def test_voice_note_step_fires_glific_flow(self):
        """escalation_type='voice_note' → SP_Escalation flow triggered."""
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("02")
        pe_name = _make_pe(self.batch_name, s, "02", current_escalation_step=2)

        with _patch_escalation_steps([
            _step(1, "help_note_a"),
            _step(2, "help_note_b"),
            _step(3, "voice_note"),
        ]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow") as fake_trigger, \
             patch.object(frappe, "enqueue") as fake_enqueue, \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        # Voice_note still uses the SP_Escalation flow (Glific branches
        # internally on the contact fields).
        self.assertEqual(fake_trigger.call_count, 1)
        # No Vocallabs enqueue.
        vocallabs_calls = [
            c for c in fake_enqueue.call_args_list
            if "vocallabs" in str(c).lower()
        ]
        self.assertEqual(len(vocallabs_calls), 0)

    def test_parent_call_step_enqueues_vocallabs_skips_glific(self):
        """escalation_type='parent_call' → vocallabs.initiate_parent_call
        is enqueued, SP_Escalation flow is NOT triggered.
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("03")
        pe_name = _make_pe(self.batch_name, s, "03", current_escalation_step=3)

        with _patch_escalation_steps([
            _step(1, "help_note_a"),
            _step(2, "help_note_b"),
            _step(3, "voice_note"),
            _step(4, "parent_call"),
        ]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow") as fake_trigger, \
             patch.object(frappe, "enqueue") as fake_enqueue, \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        # SP_Escalation flow NOT triggered for parent_call.
        self.assertEqual(fake_trigger.call_count, 0)
        # Vocallabs job WAS enqueued.
        vocallabs_calls = [
            c for c in fake_enqueue.call_args_list
            if c.args
            and c.args[0] == "tap_lms.summer_program.vocallabs.initiate_parent_call"
        ]
        self.assertEqual(len(vocallabs_calls), 1)
        # Carries pe_name + step.
        kwargs = vocallabs_calls[0].kwargs
        self.assertEqual(kwargs["pe_name"], pe_name)
        self.assertEqual(kwargs["escalation_step"]["escalation_type"], "parent_call")

    def test_handle_escalation_writes_step_and_type_to_pe(self):
        """Post CR-003 follow-up: handle_escalation no longer calls a
        dedicated push helper. Instead, T2/T4/T8/T10 transitions write
        `current_escalation_step` AND `current_escalation_type` to the PE,
        and the standard `_enqueue_contact_field_sync` (called by transition)
        pushes BOTH to Glific via the per-transition sync.

        This test verifies the PE columns are populated by the time the
        Glific flow is triggered.
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("04")
        pe_name = _make_pe(self.batch_name, s, "04", current_escalation_step=0)

        with _patch_escalation_steps([_step(1, "help_note_a")]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow"), \
             patch.object(frappe, "enqueue"), \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.current_escalation_step, 1)
        self.assertEqual(pe.current_escalation_type, "help_note_a")

    def test_t4_sets_current_escalation_type_from_step_config(self):
        """T4 (next escalation step on the Core path) writes the step's
        `escalation_type` to PE.current_escalation_type. Verifies the
        dispatcher → transition wiring picks up the channel from the
        resolved step config (not a hardcoded default).
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("06")
        pe_name = _make_pe(
            self.batch_name, s, "06",
            resolved_flow_state=STATE_NORMAL_ESCALATION,
            current_escalation_step=1,  # already past step 1
        )

        with _patch_escalation_steps([
            _step(1, "help_note_a"),
            _step(2, "voice_note"),  # step 2 is voice_note
        ]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow"), \
             patch.object(frappe, "enqueue"), \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        # T4 advanced from step 1 → step 2 AND wrote the step-2 channel.
        self.assertEqual(pe.current_escalation_step, 2)
        self.assertEqual(pe.current_escalation_type, "voice_note")

    def test_per_transition_sync_includes_escalation_order_and_type(self):
        """After T2 fires inside handle_escalation, the contact-field sync
        map built by `_enqueue_contact_field_sync` contains BOTH
        `escalation_order` and `escalation_type`. This is the replacement
        for the old eager `_push_escalation_contact_fields` test — both
        fields now flow via the standard sync path.
        """
        from tap_lms.summer_program import pe_dispatcher, state_machine

        s = _ensure_student("07")
        pe_name = _make_pe(self.batch_name, s, "07", current_escalation_step=0)

        captured = {}

        def fake_enqueue_sync(pe):
            # Mirror the real builder so the test sees what would be pushed.
            captured["step"] = pe.current_escalation_step
            captured["type"] = pe.current_escalation_type

        with _patch_escalation_steps([_step(1, "help_note_b")]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow"), \
             patch.object(frappe, "enqueue"), \
             patch.object(state_machine, "_enqueue_contact_field_sync",
                          side_effect=fake_enqueue_sync):
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        self.assertEqual(captured["step"], 1)
        self.assertEqual(captured["type"], "help_note_b")

    def test_step_exhausted_routes_to_grace_with_activity(self):
        """When current_escalation_step >= len(steps) AND submission_count > 0,
        the handler routes to t5_escalation_to_grace.
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("05")
        pe_name = _make_pe(
            self.batch_name, s, "05",
            resolved_flow_state=STATE_NORMAL_ESCALATION,
            current_escalation_step=2,
            submission_count=1,  # has activity
        )

        with _patch_escalation_steps([
            _step(1, "help_note_a"),
            _step(2, "help_note_b"),
        ]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow"), \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"), \
             patch("tap_lms.summer_program.state_machine.t5_escalation_to_grace") as fake_t5:
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        self.assertEqual(fake_t5.call_count, 1)

    def test_escalation_exhaustion_zero_submissions_routes_to_grace(self):
        """CR-006: escalation exhaustion with submission_count=0 routes to T5
        (grace), not T6 (remedial). Pre-CR-006 this went to remedial; post-CR-006
        it goes to grace.
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("08")
        pe_name = _make_pe(
            self.batch_name, s, "08",
            resolved_flow_state=STATE_NORMAL_ESCALATION,
            current_escalation_step=3,  # last step already fired
            submission_count=0,  # never submitted
        )

        with _patch_escalation_steps([
            _step(1, "help_note_a", hours=24),
            _step(2, "help_note_b", hours=48),
            _step(3, "voice_note", hours=72),
        ]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow"), \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"), \
             patch("tap_lms.summer_program.state_machine.t5_escalation_to_grace") as fake_t5:
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        # CR-006: T5 fires for zero-submission exhaustion (was T6 pre-CR-006).
        self.assertEqual(fake_t5.call_count, 1)

    def test_remedial_side_exhaustion_routes_to_grace_t11(self):
        """CR-006 regression: remedial-side exhaustion still routes to T11
        (grace). Unchanged by CR-006 but tested defensively to ensure the
        dispatcher's exhaustion routing change didn't break the remedial branch.
        """
        from tap_lms.summer_program import pe_dispatcher
        from tap_lms.summer_program.constants import STATE_REMEDIAL_ESCALATION

        s = _ensure_student("09")
        pe_name = _make_pe(
            self.batch_name, s, "09",
            resolved_flow_state=STATE_REMEDIAL_ESCALATION,
            current_escalation_step=2,
            submission_count=0,
        )

        with _patch_escalation_steps([
            _step(1, "help_note_a"),
            _step(2, "help_note_b"),
        ]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow"), \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"), \
             patch("tap_lms.summer_program.state_machine.t11_remedial_to_grace") as fake_t11:
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        self.assertEqual(fake_t11.call_count, 1)

    def test_escalation_exhaustion_with_submissions_routes_to_grace(self):
        """CR-006: escalation exhaustion with submission_count>0 routes to T5
        (grace) — unchanged from pre-CR-006. Documented as a regression guard
        alongside the zero-submission test so both branches are explicit.
        """
        from tap_lms.summer_program import pe_dispatcher

        s = _ensure_student("10")
        pe_name = _make_pe(
            self.batch_name, s, "10",
            resolved_flow_state=STATE_NORMAL_ESCALATION,
            current_escalation_step=2,
            submission_count=2,  # had activity
        )

        with _patch_escalation_steps([
            _step(1, "help_note_a"),
            _step(2, "help_note_b"),
        ]), \
             patch.object(pe_dispatcher, "_get_flow_id", return_value="flow-esc"), \
             patch.object(pe_dispatcher, "_trigger_flow"), \
             patch("tap_lms.summer_program.state_machine._enqueue_contact_field_sync"), \
             patch("tap_lms.summer_program.state_machine.t5_escalation_to_grace") as fake_t5:
            row = frappe._dict({
                "name": pe_name,
                "next_action_type": ACTION_ESCALATION,
                "batch": self.batch_name,
                "journey_label": LABEL_CONTENT_DELIVERED,
            })
            pe_dispatcher.handle_escalation(row)

        self.assertEqual(fake_t5.call_count, 1)
