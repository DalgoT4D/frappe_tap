"""
Tests for ArchetypeConfig validation.

Two surfaces are covered:

1. `validate_escalation_hours_fit_grace` (CR-003) — per-config helper that
   fails when sum(EscalationStep.hours_after_previous) exceeds
   batch.grace_window_days * 24.

2. `validate_archetype_config` (task #14, 2026-05-13) — per-tuple
   completeness check replacing the retired `validate_ab_config`. See
   ADR-004 audit log for the supersession context.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program.tests.factories import make_batch

from tap_lms.summer_program.constants import (
    LABEL_CONTENT_DELIVERED,
    PATH_CORE,
    PROGRAM_ACTIVE,
    STATE_NORMAL_CONTENT,
)
from tap_lms.summer_program.validators import (
    validate_archetype_config,
    validate_escalation_hours_fit_grace,
)


def _ensure_batch(name, grace_days, total_weeks=12):
    # Idempotent-update branch preserved: when the batch already exists this
    # helper re-asserts grace_window_days/total_weeks (tests call it with
    # varying grace values). Creation delegates to the shared factory (L-037)
    # so the fixture inherits future mandatory-field additions.
    existing = frappe.get_value("Batch", {"name1": name}, "name")
    if existing:
        frappe.db.set_value("Batch", existing, {
            "grace_window_days": grace_days,
            "total_weeks": total_weeks,
        }, update_modified=False)
        return existing
    return make_batch(
        # batch_id must be injective across callers: name[:6].upper() collided
        # (ValidatorOkBatch/Over/Empty all → "VALIDA"; ACVMissingTuple/Weeks →
        # "ACVMIS"; ACVEmptySteps/Type → "ACVEMP") against the tabBatch
        # batch_id unique constraint. Use the full alphanumeric name uppercased.
        label=name,
        batch_id="".join(ch for ch in name if ch.isalnum()).upper(),
        total_weeks=total_weeks,
        grace_window_days=grace_days,
    )


def _make_ac(batch_name, hours_list, archetype="submitter", arm="default",
             path="Core", escalation_type="help_note_a", total_weeks=12,
             include_week_rules=True):
    """Create an ArchetypeConfig with escalation_steps using the given
    hours_after_previous values.

    For task-#14 tests, also populate week_rules for weeks 1..total_weeks
    by default so the completeness check passes. Tests that need the
    missing-week_rules error path pass `include_week_rules=False` or a
    truncated total_weeks count.
    """
    ac = frappe.new_doc("ArchetypeConfig")
    ac.batch = batch_name
    ac.experiment_arm = arm
    ac.archetype = archetype
    ac.path = path
    ac.is_active = 1
    for i, hours in enumerate(hours_list, start=1):
        ac.append("escalation_steps", {
            "escalation_order": i,
            "escalation_type": escalation_type,
            "hours_after_previous": hours,
            "is_active": 1,
        })
    if include_week_rules:
        for w in range(1, total_weeks + 1):
            ac.append("week_rules", {
                "week": w,
                "expected_submission_type": "video",
            })
    ac.insert(ignore_permissions=True)
    return ac.name


def _ensure_student(suffix, archetype="submitter", arm="default"):
    """Create a Student row tagged with archetype + arm. The per-tuple
    validator reads from PE roster joined to Student."""
    phone = f"+9997000{suffix}"
    name = frappe.get_value("Student", {"phone": phone}, "name")
    if name:
        frappe.db.set_value("Student", name, {
            "archetype": archetype,
            "experiment_arm": arm,
        }, update_modified=False)
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"ACValStudent{suffix}"
    s.phone = phone
    s.glific_id = f"glific-acv-{suffix}"
    s.archetype = archetype
    s.experiment_arm = arm
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(batch_name, student_name, suffix):
    """Insert an active PE so the validator's in-use roster picks it up."""
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-ACV-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-acv-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.journey_label = LABEL_CONTENT_DELIVERED
    pe.current_path = PATH_CORE
    pe.current_week = 1
    pe.insert(ignore_permissions=True)
    return pe.name


def _full_archetype_set(batch_name, total_weeks=12,
                       archetypes=("submitter",), arms=("default",)):
    """Author all required ArchetypeConfig rows for the given archetype
    × arm × path matrix. Returns the list of config names."""
    names = []
    for archetype in archetypes:
        for arm in arms:
            for path in ("Core", "Remedial"):
                names.append(_make_ac(
                    batch_name, [24, 24, 24],
                    archetype=archetype, arm=arm, path=path,
                    total_weeks=total_weeks,
                ))
    return names


class TestArchetypeConfigEscalationValidator(FrappeTestCase):
    def test_passes_within_grace_window(self):
        """Sum of hours = 3 × 24 = 72h; grace = 14d = 336h → ok."""
        batch_name = _ensure_batch("ValidatorOkBatch", grace_days=14)
        ac_name = _make_ac(batch_name, [24, 24, 24])

        ok, err = validate_escalation_hours_fit_grace(ac_name)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_fails_exceeding_grace_window(self):
        """Sum of hours = 100 + 100 + 200 = 400h > 14d (336h) → fail."""
        batch_name = _ensure_batch("ValidatorOver", grace_days=14)
        ac_name = _make_ac(batch_name, [100, 100, 200])

        ok, err = validate_escalation_hours_fit_grace(ac_name)
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertIn("400h", err)
        self.assertIn("336h", err)

    def test_no_steps_is_valid(self):
        """An archetype with no escalation steps is acceptable — no nudges."""
        batch_name = _ensure_batch("ValidatorEmpty", grace_days=14)
        ac_name = _make_ac(batch_name, [])
        ok, err = validate_escalation_hours_fit_grace(ac_name)
        self.assertTrue(ok)
        self.assertIsNone(err)


# ════════════════════════════════════════════════════════════
# Task #14 (2026-05-13) — per-tuple completeness validator
# ════════════════════════════════════════════════════════════


class TestValidateArchetypeConfig(FrappeTestCase):
    """Tests for `validate_archetype_config` — the per-tuple completeness
    check that replaces the retired `validate_ab_config` always-16 rule.
    """

    def _wipe_batch_data(self, batch_name):
        """Remove ArchetypeConfigs + PEs for this batch (test isolation
        beyond FrappeTestCase's rollback; some test data is reused
        across classes via _ensure_batch lookup-by-name1)."""
        for ac in frappe.get_all(
            "ArchetypeConfig", filters={"batch": batch_name}, pluck="name"
        ):
            frappe.delete_doc("ArchetypeConfig", ac, force=True)
        for pe in frappe.get_all(
            "ProgramEnrollment", filters={"batch": batch_name}, pluck="name"
        ):
            frappe.delete_doc("ProgramEnrollment", pe, force=True)

    def test_validate_archetype_config_valid_default_arm_only(self):
        """Batch with default-arm-only roster + 2 active configs (Core +
        Remedial for the one in-use archetype) → valid, no issues.

        The MVP default cohort sits here: one archetype value across all
        students, default arm, both paths configured.
        """
        batch_name = _ensure_batch("ACVDefaultOk", grace_days=14, total_weeks=4)
        self._wipe_batch_data(batch_name)

        s = _ensure_student("V01", archetype="submitter", arm="default")
        _make_pe(batch_name, s, "V01")

        # 2 configs: submitter × default × {Core, Remedial}
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Core", total_weeks=4)
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Remedial", total_weeks=4)

        result = validate_archetype_config(batch_name)
        self.assertTrue(result["valid"], msg=f"unexpected issues: {result['issues']}")
        self.assertEqual(result["issues"], [])

    def test_validate_archetype_config_invalid_missing_tuple(self):
        """If the roster needs a (submitter, default, Remedial) config and
        only Core is authored, the validator reports an error."""
        batch_name = _ensure_batch("ACVMissingTuple", grace_days=14, total_weeks=4)
        self._wipe_batch_data(batch_name)

        s = _ensure_student("V02", archetype="submitter", arm="default")
        _make_pe(batch_name, s, "V02")

        # Only Core authored; Remedial missing.
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Core", total_weeks=4)

        result = validate_archetype_config(batch_name)
        self.assertFalse(result["valid"])
        missing = [
            i for i in result["issues"]
            if i["severity"] == "error" and "No active ArchetypeConfig" in i["problem"]
        ]
        self.assertTrue(missing, msg=f"expected a missing-config error, got: {result['issues']}")
        # Tuple reported should be the Remedial one.
        self.assertEqual(missing[0]["tuple"], ("submitter", "default", "Remedial"))

    def test_validate_archetype_config_invalid_empty_escalation_steps(self):
        """A config exists for the tuple but has no active escalation
        steps → error."""
        batch_name = _ensure_batch("ACVEmptySteps", grace_days=14, total_weeks=4)
        self._wipe_batch_data(batch_name)

        s = _ensure_student("V03", archetype="submitter", arm="default")
        _make_pe(batch_name, s, "V03")

        _make_ac(batch_name, [], archetype="submitter", arm="default",
                 path="Core", total_weeks=4)
        _make_ac(batch_name, [], archetype="submitter", arm="default",
                 path="Remedial", total_weeks=4)

        result = validate_archetype_config(batch_name)
        self.assertFalse(result["valid"])
        empty_steps = [
            i for i in result["issues"]
            if "escalation_steps is empty" in i["problem"]
        ]
        # One per tuple (Core + Remedial both empty).
        self.assertEqual(len(empty_steps), 2)

    def test_validate_archetype_config_warning_hours_exceed_grace(self):
        """Sum of escalation hours > grace window → warning, NOT error.
        Warnings surface in the issues list but don't block — `valid=True`
        as long as there are no error-severity issues. The activation gate
        (`_validate_archetype_config_before_activation`) only blocks on
        errors; the preview API returns warnings for admin review.
        """
        batch_name = _ensure_batch("ACVHoursWarn", grace_days=14, total_weeks=4)
        self._wipe_batch_data(batch_name)

        s = _ensure_student("V04", archetype="submitter", arm="default")
        _make_pe(batch_name, s, "V04")

        # 100+100+200 = 400h > 14d (336h) → warning.
        _make_ac(batch_name, [100, 100, 200], archetype="submitter",
                 arm="default", path="Core", total_weeks=4)
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Remedial", total_weeks=4)

        result = validate_archetype_config(batch_name)
        # Warnings don't break validity.
        self.assertTrue(result["valid"])
        warnings = [i for i in result["issues"] if i["severity"] == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["tuple"], ("submitter", "default", "Core"))

    def test_validate_archetype_config_error_missing_week_rules(self):
        """week_rules don't cover weeks 1..total_weeks → error."""
        batch_name = _ensure_batch("ACVMissingWeeks", grace_days=14, total_weeks=4)
        self._wipe_batch_data(batch_name)

        s = _ensure_student("V05", archetype="submitter", arm="default")
        _make_pe(batch_name, s, "V05")

        # Author configs without any week_rules — _make_ac honors
        # include_week_rules=False to drop them entirely.
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Core", total_weeks=4,
                 include_week_rules=False)
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Remedial", total_weeks=4,
                 include_week_rules=False)

        result = validate_archetype_config(batch_name)
        self.assertFalse(result["valid"])
        missing_weeks = [
            i for i in result["issues"]
            if "week_rules missing weeks" in i["problem"]
        ]
        self.assertEqual(len(missing_weeks), 2)  # both tuples report the gap

    def test_validate_archetype_config_error_empty_escalation_type(self):
        """An active escalation step with empty escalation_type → error."""
        batch_name = _ensure_batch("ACVEmptyType", grace_days=14, total_weeks=4)
        self._wipe_batch_data(batch_name)

        s = _ensure_student("V06", archetype="submitter", arm="default")
        _make_pe(batch_name, s, "V06")

        # escalation_type="" forces the empty-type branch.
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Core", total_weeks=4,
                 escalation_type="")
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Remedial", total_weeks=4)

        result = validate_archetype_config(batch_name)
        self.assertFalse(result["valid"])
        empty_type = [
            i for i in result["issues"]
            if "empty escalation_type" in i["problem"]
        ]
        # One per tuple that has the empty type (Core).
        self.assertEqual(len(empty_type), 1)
        self.assertEqual(empty_type[0]["tuple"], ("submitter", "default", "Core"))

    def test_validate_archetype_config_supports_multiple_arms(self):
        """Batch with default + arm_a students requires configs for BOTH
        arms × Core + Remedial. If arm_a configs are missing, errors fire
        for arm_a tuples but the default tuples stay valid."""
        batch_name = _ensure_batch("ACVMultiArms", grace_days=14, total_weeks=4)
        self._wipe_batch_data(batch_name)

        s_default = _ensure_student("V07", archetype="submitter", arm="default")
        s_arm_a = _ensure_student("V08", archetype="submitter", arm="arm_a")
        _make_pe(batch_name, s_default, "V07")
        _make_pe(batch_name, s_arm_a, "V08")

        # Author configs for default only — arm_a missing.
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Core", total_weeks=4)
        _make_ac(batch_name, [24, 24, 24], archetype="submitter",
                 arm="default", path="Remedial", total_weeks=4)

        result = validate_archetype_config(batch_name)
        self.assertFalse(result["valid"])
        # 2 missing tuples expected: (submitter, arm_a, Core) and
        # (submitter, arm_a, Remedial).
        missing = [
            i for i in result["issues"]
            if i["severity"] == "error" and "No active ArchetypeConfig" in i["problem"]
        ]
        self.assertEqual(len(missing), 2)
        missing_tuples = {i["tuple"] for i in missing}
        self.assertIn(("submitter", "arm_a", "Core"), missing_tuples)
        self.assertIn(("submitter", "arm_a", "Remedial"), missing_tuples)
