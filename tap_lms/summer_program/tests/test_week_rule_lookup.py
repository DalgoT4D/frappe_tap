"""
Regression tests for the 2026-05-19 production bug:

    pe_dispatcher._get_week_rule was calling
    frappe.db.get_value("ArchetypeConfig", ..., ["expected_submission_type",
    "core_learning_unit", "remedial_learning_unit"]) directly on the parent
    table. None of those columns exist on ArchetypeConfig itself —
    expected_submission_type is on the WeekRule child table, and the other
    two are phantom names that exist on no doctype. Every dispatcher tick
    that reached week_advancement crashed with `column "expected_submission_type"
    does not exist`, silently failing for the whole cohort.

These tests pin the new contract: read the parent ArchetypeConfig by
(batch, archetype, experiment_arm, path, is_active=1) with a "default" arm
fallback, then read the WeekRule child by (parent, parenttype, week).
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program.tests.factories import make_batch, make_student


def _ensure_archetype_config(
    batch_name,
    archetype="fence_sitter",
    experiment_arm="default",
    path="Core",
    weeks_to_submissions=None,
):
    """Create or return an ArchetypeConfig with WeekRule children.

    weeks_to_submissions: dict mapping {week_int: expected_submission_type_str}.
    Defaults to {1: "video", 2: "image"} for the regression tests.
    """
    if weeks_to_submissions is None:
        weeks_to_submissions = {1: "video", 2: "image"}

    existing = frappe.db.get_value(
        "ArchetypeConfig",
        {
            "batch": batch_name,
            "archetype": archetype,
            "experiment_arm": experiment_arm,
            "path": path,
            "is_active": 1,
        },
        "name",
    )
    if existing:
        return existing

    ac = frappe.new_doc("ArchetypeConfig")
    ac.batch = batch_name
    ac.experiment_arm = experiment_arm
    ac.archetype = archetype
    ac.path = path
    ac.is_active = 1
    for week, sub_type in weeks_to_submissions.items():
        ac.append("week_rules", {
            "week": week,
            "expected_submission_type": sub_type,
        })
    ac.insert(ignore_permissions=True)
    return ac.name


class TestGetWeekRule(FrappeTestCase):
    """_get_week_rule must read from the WeekRule child table, not from
    ArchetypeConfig directly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = make_batch("WeekRuleLookupBatch", batch_id="WRLB")
        cls.batch = frappe.get_doc("Batch", cls.batch_name)

    def _make_pe_like(self, archetype="fence_sitter",
                       experiment_arm="default", current_path="Core",
                       name="fake-pe-001"):
        """Build a frappe._dict that quacks like a PE for the helper's needs."""
        return frappe._dict({
            "name": name,
            "archetype": archetype,
            "experiment_arm": experiment_arm,
            "current_path": current_path,
        })

    def test_reads_expected_submission_type_from_child_table(self):
        """The headline regression: _get_week_rule must successfully
        return expected_submission_type from the WeekRule child row."""
        from tap_lms.summer_program.pe_dispatcher import _get_week_rule

        _ensure_archetype_config(
            self.batch_name,
            archetype="fence_sitter",
            experiment_arm="default",
            path="Core",
            weeks_to_submissions={1: "video", 2: "image", 3: "photo_video_artefact"},
        )

        pe = self._make_pe_like(
            archetype="fence_sitter",
            experiment_arm="default",
            current_path="Core",
        )

        rule_w1 = _get_week_rule(pe, self.batch, 1)
        self.assertIsNotNone(rule_w1, "Expected rule for week 1 to be found")
        self.assertEqual(rule_w1.get("expected_submission_type"), "video")

        rule_w3 = _get_week_rule(pe, self.batch, 3)
        self.assertIsNotNone(rule_w3)
        self.assertEqual(
            rule_w3.get("expected_submission_type"), "photo_video_artefact"
        )

    def test_returns_none_when_week_not_in_rules(self):
        """If the WeekRule child has no row for the requested week,
        return None (callers gate on `if week_rule`)."""
        from tap_lms.summer_program.pe_dispatcher import _get_week_rule

        _ensure_archetype_config(
            self.batch_name,
            archetype="dormant",
            experiment_arm="default",
            path="Core",
            weeks_to_submissions={1: "video"},  # only week 1
        )

        pe = self._make_pe_like(
            archetype="dormant",
            experiment_arm="default",
            current_path="Core",
        )

        # Week 5 has no rule
        rule = _get_week_rule(pe, self.batch, 5)
        self.assertIsNone(rule)

    def test_falls_back_to_default_arm_when_pe_arm_has_no_config(self):
        """If no ArchetypeConfig exists for the PE's experiment_arm but a
        'default' arm config does, the helper should fall back to default."""
        from tap_lms.summer_program.pe_dispatcher import _get_week_rule

        _ensure_archetype_config(
            self.batch_name,
            archetype="lurker",
            experiment_arm="default",
            path="Core",
            weeks_to_submissions={1: "summary_text_voice"},
        )

        # PE is on a non-default arm with no matching config
        pe = self._make_pe_like(
            archetype="lurker",
            experiment_arm="arm_x_nonexistent",
            current_path="Core",
        )

        rule = _get_week_rule(pe, self.batch, 1)
        self.assertIsNotNone(rule, "Expected fallback to default arm")
        self.assertEqual(rule.get("expected_submission_type"), "summary_text_voice")

    def test_returns_none_when_no_archetype_config(self):
        """If neither the PE's arm nor 'default' has a matching config,
        return None."""
        from tap_lms.summer_program.pe_dispatcher import _get_week_rule

        pe = self._make_pe_like(
            archetype="archetype_that_has_no_config",
            experiment_arm="default",
            current_path="Core",
        )

        rule = _get_week_rule(pe, self.batch, 1)
        self.assertIsNone(rule)

    def test_path_filter_picks_remedial_when_pe_on_remedial(self):
        """A PE on Remedial path must read the Remedial ArchetypeConfig's
        WeekRule, not the Core one — even when both exist."""
        from tap_lms.summer_program.pe_dispatcher import _get_week_rule

        _ensure_archetype_config(
            self.batch_name,
            archetype="dabbler",
            experiment_arm="default",
            path="Core",
            weeks_to_submissions={1: "video"},
        )
        _ensure_archetype_config(
            self.batch_name,
            archetype="dabbler",
            experiment_arm="default",
            path="Remedial",
            weeks_to_submissions={1: "image"},
        )

        # PE on Remedial — must get the Remedial row
        pe = self._make_pe_like(
            archetype="dabbler",
            experiment_arm="default",
            current_path="Remedial",
        )
        rule = _get_week_rule(pe, self.batch, 1)
        self.assertIsNotNone(rule)
        self.assertEqual(
            rule.get("expected_submission_type"), "image",
            "Remedial path must return the Remedial WeekRule, not Core",
        )
