"""
Tests for CR-002 v2 activity-points handler.

Covers the four scenarios in CR §Test Plan for VideoClass completions:

  1. test_video_completion_awards_activity_points
  2. test_video_completion_idempotent_via_points_awarded
  3. test_three_videos_same_unit_award_thrice
  4. test_video_zero_points_awards_zero_but_flips_done (E1 fix / CR-009;
     E11 retired — 0-point awards 0 but still flips weekly_video_done + grace)

Glific contact-field sync is mocked via unittest.mock.patch so we never hit
the network. No frappe.db.commit() (L-017 — runner uses transaction rollback
for isolation).
"""
import random

import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

# L-062: per-RUN unique token for unique-constrained fixture fields (phone,
# glific_id, VideoClass name). FrappeTestCase rolls back per test, but a run
# killed mid-way (e.g. the 2026-06-16 Postgres crash) leaves COMMITTED rows that
# then collide on re-run (idx_pe_batch_glific_id_active, VideoClass pkey) and
# poison the txn for every subsequent test. A fresh token per run keeps each
# run's fixtures disjoint from any leftover rows, so the module runs clean
# regardless of orphans. Also stops get_active_pe() from picking up an orphan
# PE for a reused student (run-unique students have no prior PEs).
_RUN = str(random.randint(100000, 999999))

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
    phone = f"+9199{_RUN}{suffix}"
    name = frappe.get_value("Student", {"phone": phone}, "name")
    if name:
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"ActivityPointsTestStudent{_RUN}{suffix}"
    s.phone = phone
    s.glific_id = f"glific-actpts-{_RUN}-{suffix}"
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(batch_name, student_name, suffix):
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-ACTPTS-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-actpts-{_RUN}-{suffix}"
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
    video.video_name = f"ActivityTestVideo-{_RUN}-{suffix}"
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
        # `handle_content_log` is the StudentContentLog `after_insert` hook
        # (hooks.py), so the insert itself awards — the production path. Do NOT
        # also call it explicitly: that would double-bump on a stale in-memory
        # doc (award writes points_awarded via frappe.db.set_value, DB-only).
        scl = _make_scl(student, video_id)

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

        # Insert fires the after_insert hook → awards 10 (production path).
        scl = _make_scl(student, video_id)
        scl.reload()
        self.assertEqual(scl.points_awarded, 10)

        # Re-invoke the handler with current state (mirrors a Frappe hook
        # re-fire, which reloads the doc) → must be a no-op via the
        # points_awarded > 0 anchor. NOT double-bumped.
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

        # Each insert fires the after_insert hook → awards 10 (no explicit call).
        for video_id in (video_a, video_b, video_c):
            _make_scl(student, video_id)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        self.assertEqual(pe.total_activity_points, 30)
        self.assertEqual(pe.weekly_activity_points, 30)
        self.assertEqual(pe.total_points, 30)
        self.assertEqual(pe.weekly_video_done, 1,
                         "weekly_video_done stays 1 across all three videos")

    @patch("tap_lms.summer_program.activity_points._enqueue_contact_field_sync")
    def test_video_zero_points_awards_zero_but_flips_done(self, mock_sync):
        """CR-009 (2026-05-23; E11 RETIRED): a 0-point VideoClass awards NO
        points (the bump is a no-op) but STILL flips weekly_video_done=1 and
        arms the grace clock — engagement = content watched; points are a
        separate reward dimension.

        Regression for E1 (2026-06-17): the stale `or 10` previously awarded 10
        for a 0/missing-points video. This pins points=0 AND the pipeline
        side-effects firing (the two halves of the bug-vs-intent split)."""
        student = _ensure_student("04")
        pe_name = _make_pe(self.batch_name, student, "04")
        video_id = _make_video("04", 0)
        # Insert fires the after_insert hook → the (0-point) award path runs.
        scl = _make_scl(student, video_id)

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        # E1: zero points → award 0 (was wrongly 10 under `or 10`)
        self.assertEqual(pe.total_activity_points, 0)
        self.assertEqual(pe.weekly_activity_points, 0)
        self.assertEqual(pe.total_points, 0)
        scl.reload()
        self.assertEqual(scl.points_awarded, 0,
                         "E1: 0-point video awards 0, not 10")
        # CR-009: but the engagement pipeline still fires
        self.assertEqual(pe.weekly_video_done, 1,
                         "CR-009: 0-point video still flips weekly_video_done")
        self.assertTrue(pe.grace_window_end_at,
                        "CR-009: grace clock armed on first video even at 0 points")

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
