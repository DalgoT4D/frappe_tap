"""
Test the enrollment-time Glific contact-field push from `_process_pe_chunk`.

Guards two things:
  1. All 28 SP contact fields are present in the push (was 18 pre-2026-05-18).
     Catches future regressions where someone removes a field or forgets to
     add a new one — the cache should always be a complete 28-field bundle
     at enrollment time so Glific flows see consistent values from day-one.
  2. Gamification fields are initialized to "0" (not missing) so flows that
     read @contact.weekly_activity_points before the first activity see "0"
     rather than an empty/unset value.

Mocks frappe.enqueue to capture the fields dict that would be sent to
Glific without actually calling the API.
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from tap_lms.summer_program.constants import (
    # All 28 contact field names — explicit list so this test fails fast
    # if any CF_* constant is renamed or removed.
    CF_STUDENT_ID, CF_BATCH_ID, CF_ARCHETYPE, CF_LANGUAGE_ID,
    CF_EXPERIMENT_ARM, CF_COURSE_LEVEL, CF_STUDENT_NAME,
    CF_RESOLVED_FLOW_STATE, CF_CURRENT_WEEK, CF_CURRENT_PATH,
    CF_CURRENT_TIER, CF_PROGRAM_STATUS, CF_TOTAL_POINTS,
    CF_CURRENT_STREAK, CF_GRACE_WINDOW_END, CF_EXPECTED_SUBMISSION,
    CF_LAST_ESCALATION_STEP, CF_SUBMISSION_COUNT,
    CF_TOTAL_ACTIVITY_POINTS, CF_WEEKLY_ACTIVITY_POINTS,
    CF_TOTAL_QUIZ_POINTS, CF_WEEKLY_QUIZ_POINTS,
    CF_TOTAL_SUBMISSION_POINTS, CF_WEEKLY_SUBMISSION_POINTS,
    CF_SPECIAL_GEMS, CF_WEEKLY_SUBMISSION_DONE,
    CF_BONUS_QUIZ_POINTS,
    CF_WEEKLY_ENGAGEMENT_POINTS,
    CF_ESCALATION_ORDER, CF_ESCALATION_TYPE,
    STATE_NORMAL_CONTENT, PATH_CORE, PROGRAM_ACTIVE,
)


EXPECTED_FIELDS_AT_ENROLLMENT = {
    # Identity (7)
    CF_STUDENT_ID, CF_BATCH_ID, CF_ARCHETYPE, CF_LANGUAGE_ID,
    CF_EXPERIMENT_ARM, CF_COURSE_LEVEL, CF_STUDENT_NAME,
    # Base state (11)
    CF_RESOLVED_FLOW_STATE, CF_CURRENT_WEEK, CF_CURRENT_PATH,
    CF_CURRENT_TIER, CF_PROGRAM_STATUS, CF_TOTAL_POINTS,
    CF_CURRENT_STREAK, CF_GRACE_WINDOW_END, CF_EXPECTED_SUBMISSION,
    CF_LAST_ESCALATION_STEP, CF_SUBMISSION_COUNT,
    # CR-002 v2 gamification (8) + task #98 bonus_quiz_points (1)
    # + task #7 weekly_engagement_points (1) = 10
    # bonus_quiz_points was added 2026-05-25 so the Glific card's
    # @contact.fields.bonus_quiz_points resolves from day one of enrollment.
    # weekly_engagement_points added 2026-05-26 as a computed sum of
    # weekly_submission_points + weekly_activity_points (NOT stored on PE).
    CF_TOTAL_ACTIVITY_POINTS, CF_WEEKLY_ACTIVITY_POINTS,
    CF_TOTAL_QUIZ_POINTS, CF_WEEKLY_QUIZ_POINTS,
    CF_TOTAL_SUBMISSION_POINTS, CF_WEEKLY_SUBMISSION_POINTS,
    CF_SPECIAL_GEMS, CF_WEEKLY_SUBMISSION_DONE,
    CF_BONUS_QUIZ_POINTS,
    CF_WEEKLY_ENGAGEMENT_POINTS,
    # CR-003 escalation (2)
    CF_ESCALATION_ORDER, CF_ESCALATION_TYPE,
}


def _ensure_batch():
    name = frappe.get_value("Batch", {"name1": "EnrollPushTestBatch"}, "name")
    if name:
        return name
    batch = frappe.new_doc("Batch")
    batch.name1 = "EnrollPushTestBatch"
    batch.start_date = "2026-01-01"
    batch.end_date = "2026-04-30"
    # Registration window (mandatory on Batch doctype as of current schema)
    batch.regist_start_date = "2025-12-01"
    batch.regist_end_date = "2025-12-31"
    batch.batch_id = "EPT01"
    batch.program_type = "Summer"
    batch.total_weeks = 12
    batch.current_calendar_week = 1
    batch.grace_window_days = 14
    batch.insert(ignore_permissions=True)
    return batch.name


def _ensure_student(suffix):
    name = frappe.get_value("Student", {"phone": f"+9999500{suffix}"}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"EnrollPushStudent{suffix}"
    s.phone = f"+9999500{suffix}"
    s.glific_id = f"glific-ept-{suffix}"
    s.archetype = "fence_sitter"
    s.experiment_arm = "arm_a"
    s.language = "English"
    s.insert(ignore_permissions=True)
    return s.name


class TestEnrollmentContactFieldPush(FrappeTestCase):
    """The enrollment-time push must include all 28 SP contact fields."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.program_enrollment_api.frappe.enqueue")
    def test_push_has_all_28_fields(self, mock_enqueue):
        from tap_lms.summer_program.program_enrollment_api import _process_pe_chunk
        sid = _ensure_student("28F")

        _process_pe_chunk(
            bpr_name=None,    # not read by the per-student loop
            batch_name=self.batch_name,
            student_ids=[sid],
            chunk_index=0,
        )

        # Find the enqueue call that pushes contact fields (vs any other enqueue)
        push_calls = [
            c for c in mock_enqueue.call_args_list
            if c.args
            and c.args[0] == "tap_lms.summer_program.state_machine._sync_contact_fields_job"
        ]
        self.assertEqual(len(push_calls), 1,
                         "Expected exactly one contact-field sync enqueue per PE")

        fields = push_calls[0].kwargs["fields"]
        actual_keys = set(fields.keys())

        # Hard assertion — every expected field must be present
        missing = EXPECTED_FIELDS_AT_ENROLLMENT - actual_keys
        self.assertFalse(
            missing,
            f"Enrollment push is missing {len(missing)} fields: {sorted(missing)}"
        )

        # Also assert no unexpected extras (catches typos / wrong keys)
        extra = actual_keys - EXPECTED_FIELDS_AT_ENROLLMENT
        self.assertFalse(
            extra,
            f"Enrollment push has {len(extra)} unexpected fields: {sorted(extra)}"
        )

        # Spot-check critical default values
        self.assertEqual(fields[CF_RESOLVED_FLOW_STATE], STATE_NORMAL_CONTENT)
        self.assertEqual(fields[CF_CURRENT_WEEK], "1")
        self.assertEqual(fields[CF_CURRENT_PATH], PATH_CORE)
        self.assertEqual(fields[CF_PROGRAM_STATUS], PROGRAM_ACTIVE)
        self.assertEqual(fields[CF_SUBMISSION_COUNT], "0")
        self.assertEqual(fields[CF_LAST_ESCALATION_STEP], "0")

    @patch("tap_lms.summer_program.program_enrollment_api.frappe.enqueue")
    def test_gamification_fields_initialized_to_zero(self, mock_enqueue):
        """Gamification fields must be '0' at enrollment so flows reading
        @contact.weekly_activity_points before first activity see '0'
        rather than empty/unset."""
        from tap_lms.summer_program.program_enrollment_api import _process_pe_chunk
        sid = _ensure_student("GZ0")

        _process_pe_chunk(
            bpr_name=None,
            batch_name=self.batch_name,
            student_ids=[sid],
            chunk_index=0,
        )

        push_calls = [
            c for c in mock_enqueue.call_args_list
            if c.args
            and c.args[0] == "tap_lms.summer_program.state_machine._sync_contact_fields_job"
        ]
        self.assertEqual(len(push_calls), 1)
        fields = push_calls[0].kwargs["fields"]

        ZERO_INITIALIZED = (
            CF_TOTAL_ACTIVITY_POINTS, CF_WEEKLY_ACTIVITY_POINTS,
            CF_TOTAL_QUIZ_POINTS, CF_WEEKLY_QUIZ_POINTS,
            CF_TOTAL_SUBMISSION_POINTS, CF_WEEKLY_SUBMISSION_POINTS,
            CF_SPECIAL_GEMS, CF_WEEKLY_SUBMISSION_DONE,
            CF_BONUS_QUIZ_POINTS,  # task #98 (2026-05-25)
            CF_WEEKLY_ENGAGEMENT_POINTS,  # task #7 (2026-05-26) — computed, but seeded "0"
            CF_ESCALATION_ORDER,
        )
        for key in ZERO_INITIALIZED:
            self.assertEqual(
                fields.get(key), "0",
                f"Field {key} must be '0' at enrollment, got {fields.get(key)!r}"
            )

        # escalation_type is empty at enrollment (no step yet)
        self.assertEqual(fields.get(CF_ESCALATION_TYPE), "")
