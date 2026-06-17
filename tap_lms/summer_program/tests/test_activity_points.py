"""
Tests for CR-002 v2 activity-points handler.

Covers the four scenarios in CR §Test Plan for VideoClass completions:

  1. test_video_completion_awards_activity_points
  2. test_video_completion_idempotent_via_points_awarded
  3. test_three_videos_same_unit_award_thrice
  4. test_video_zero_points_skips_award (E11)

Glific contact-field sync is mocked via unittest.mock.patch so we never hit
the network. No frappe.db.commit() (L-017 — runner uses transaction rollback
for isolation).
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from tap_lms.summer_program.tests.factories import make_batch
from tap_lms.summer_program.activity_points import (
    handle_content_log,
    award_activity_points,
)
from tap_lms.summer_program.constants import (
    LABEL_CONTENT_DELIVERED,
    PATH_CORE,
    PROGRAM_ACTIVE,
    STATE_NORMAL_CONTENT,
)


# ════════════════════════════════════════════════════════════
# Test fixtures
# ════════════════════════════════════════════════════════════

def _ensure_batch():
    # Delegates to the shared factory (L-037) so this fixture inherits future
    # mandatory-field additions instead of breaking with MandatoryError.
    return make_batch(label="ActivityPointsTestBatch", batch_id="APT01")


def _ensure_student(suffix):
    name = frappe.get_value("Student", {"phone": f"+9999100{suffix}"}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"ActivityPointsTestStudent{suffix}"
    s.phone = f"+9999100{suffix}"
    s.glific_id = f"glific-actpts-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(batch_name, student_name, suffix):
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-ACTPTS-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-actpts-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.journey_label = LABEL_CONTENT_DELIVERED
    pe.current_path = PATH_CORE
    pe.current_week = 1
    pe.total_points = 0
    pe.total_activity_points = 0
    pe.weekly_activity_points = 0
    pe.weekly_video_done = 0
    pe.insert(ignore_permissions=True)
    return pe.name


def _make_video(suffix, points):
    """Insert a VideoClass row with the given points value."""
    video = frappe.new_doc("VideoClass")
    video.video_name = f"ActivityTestVideo-{suffix}"
    video.duration = "5:00"
    video.points = points
    video.insert(ignore_permissions=True)
    return video.name


def _make_scl(student, video_id, action="completed"):
    """Insert a StudentContentLog row pointing at the given VideoClass."""
    log = frappe.new_doc("StudentContentLog")
    log.student = student
    log.stage_no = 1
    log.content_type = "VideoClass"
    log.content_id = video_id
    log.content_name = "Activity Test Video"
    log.action = action
    log.tier = "Basic"
    log.insert(ignore_permissions=True)
    return log


# ════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════

class TestActivityPoints(FrappeTestCase):
    """CR-002 v2 §Test Plan — activity-points handler regression coverage."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    def test_video_completion_awards_activity_points(self, mock_sync):
        """First VideoClass completion bumps weekly and eager total counters,
        flips weekly_video_done, and writes scl.points_awarded."""
        student = _ensure_student("01")
        pe_name = _make_pe(self.batch_name, student, "01")
        video_id = _make_video("01", 10)
        scl = _make_scl(student, video_id)

        # The hook would fire automatically, but in tests doc_events may be
        # disabled or skipped. Call the handler explicitly to assert behavior.
        handle_content_log(scl)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_activity_points, 10)
        self.assertEqual(pe.weekly_activity_points, 10)
        self.assertEqual(pe.total_points, 10)
        self.assertEqual(pe.weekly_video_done, 1)

        scl.reload()
        self.assertEqual(scl.points_awarded, 10)

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    def test_video_completion_idempotent_via_points_awarded(self, mock_sync):
        """Re-running the handler on the same SCL row is a no-op:
        sees points_awarded > 0 and returns. PE counters do not double-bump."""
        student = _ensure_student("02")
        pe_name = _make_pe(self.batch_name, student, "02")
        video_id = _make_video("02", 10)
        scl = _make_scl(student, video_id)

        # First call awards 10
        handle_content_log(scl)
        scl.reload()
        self.assertEqual(scl.points_awarded, 10)

        # Second call should be a no-op (idempotency anchor)
        handle_content_log(scl)
        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_activity_points, 10,
                         "Re-running handler must not double-bump")
        self.assertEqual(pe.weekly_activity_points, 10)
        self.assertEqual(pe.total_points, 10)

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    def test_three_videos_same_unit_award_thrice(self, mock_sync):
        """Three SCL rows × VideoClass.points=10 award 30 total. The flag
        `weekly_video_done` stays 1 across all three (idempotent set)."""
        student = _ensure_student("03")
        pe_name = _make_pe(self.batch_name, student, "03")
        video_a = _make_video("03a", 10)
        video_b = _make_video("03b", 10)
        video_c = _make_video("03c", 10)

        for video_id in (video_a, video_b, video_c):
            scl = _make_scl(student, video_id)
            handle_content_log(scl)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_activity_points, 30)
        self.assertEqual(pe.weekly_activity_points, 30)
        self.assertEqual(pe.total_points, 30)
        self.assertEqual(pe.weekly_video_done, 1,
                         "weekly_video_done stays 1 across all three videos")

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    def test_video_zero_points_skips_award(self, mock_sync):
        """E11: VideoClass.points=0 → handler returns at the award-resolve
        step. NO PE update and NO weekly_video_done flag flip."""
        student = _ensure_student("04")
        pe_name = _make_pe(self.batch_name, student, "04")
        video_id = _make_video("04", 0)
        scl = _make_scl(student, video_id)

        handle_content_log(scl)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_activity_points, 0)
        self.assertEqual(pe.weekly_activity_points, 0)
        self.assertEqual(pe.total_points, 0)
        self.assertEqual(pe.weekly_video_done, 0,
                         "E11: zero-point video must NOT flip weekly_video_done")
        scl.reload()
        self.assertEqual(scl.points_awarded, 0,
                         "E11: no audit-field bump on zero-point video")

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    def test_non_video_content_log_ignored(self, mock_sync):
        """SCL rows with content_type != VideoClass are no-ops at entry."""
        student = _ensure_student("05")
        pe_name = _make_pe(self.batch_name, student, "05")
        # Insert an Assignment-typed SCL — handler should return at entry filter.
        log = frappe.new_doc("StudentContentLog")
        log.student = student
        log.stage_no = 1
        log.content_type = "Assignment"
        log.content_id = "ASN-X"
        log.content_name = "An assignment, not a video"
        log.action = "completed"
        log.insert(ignore_permissions=True)

        handle_content_log(log)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_activity_points, 0)
        self.assertEqual(pe.weekly_video_done, 0)
