"""
Regression tests for the 2026-05-19 production bug:

    _get_active_bpr_for_student was reading Student.enrollment (legacy
    child table) instead of ProgramEnrollment, so students enrolled via
    start_program_enrollment for a NEW SP batch returned no active BPR
    from get_next_content even when they had an active PE on that batch.

The fix made the helper canonical-source-of-truth: read ProgramEnrollment
with program_status in (active, paused). These tests pin that contract.

Also covers _get_course_level_for_student, which had the same bug and now
reads exclusively from ProgramEnrollment.course_level.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program.constants import BPR_ACTIVE
from tap_lms.summer_program.tests.factories import (
    make_batch,
    make_student,
    make_active_pe,
)


# Phone prefix unique to this test file — confirmed unused by other
# tap_lms test files at time of writing. The +9999XYZ convention space
# in factories.py is already exhausted, so we drop down one digit-block
# to the +9998XYZ space which is mostly free.
_PHONE_PREFIX = "+9998100"


def _ensure_active_bpr(batch_name):
    """Idempotent: create or return an active BPR on the given batch."""
    existing = frappe.db.get_value(
        "BatchProgramRun",
        {"batch": batch_name, "status": BPR_ACTIVE},
        "name",
    )
    if existing:
        return existing
    bpr = frappe.new_doc("BatchProgramRun")
    bpr.batch = batch_name
    bpr.status = BPR_ACTIVE
    bpr.insert(ignore_permissions=True)
    return bpr.name


class TestActiveBprResolution(FrappeTestCase):
    """_get_active_bpr_for_student must resolve PEs via ProgramEnrollment,
    not via the legacy Student.enrollment child table."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sp_batch = make_batch("ActiveBprSpBatch", batch_id="ABPRSP")
        cls.sp_bpr = _ensure_active_bpr(cls.sp_batch)

    def test_returns_batch_and_bpr_when_pe_active(self):
        """Student with an active PE but EMPTY Student.enrollment must still
        resolve the active BPR. This is the production scenario that
        Shivansh hit on 2026-05-19 — start_program_enrollment created the
        PE but did NOT populate the legacy child table."""
        from tap_lms.summer_program.student_progression_sp import (
            _get_active_bpr_for_student,
        )

        sid = make_student(suffix="EMPTY", phone_prefix=_PHONE_PREFIX)
        make_active_pe(sid, self.sp_batch)

        student = frappe.get_doc("Student", sid)
        # Sanity: legacy child table is empty for this student
        self.assertEqual(len(student.enrollment or []), 0,
                         "Test precondition: Student.enrollment must be empty")

        batch, bpr = _get_active_bpr_for_student(student)
        self.assertIsNotNone(batch, "Expected active SP batch to be resolved")
        self.assertEqual(batch.name, self.sp_batch)
        self.assertIsNotNone(bpr, "Expected active BPR to be resolved")
        self.assertEqual(bpr.status, BPR_ACTIVE)

    def test_returns_none_when_no_pe(self):
        """Student with neither PE nor legacy enrollment → no batch, no BPR."""
        from tap_lms.summer_program.student_progression_sp import (
            _get_active_bpr_for_student,
        )

        sid = make_student(suffix="NOPE", phone_prefix=_PHONE_PREFIX)
        student = frappe.get_doc("Student", sid)
        batch, bpr = _get_active_bpr_for_student(student)
        self.assertIsNone(batch)
        self.assertIsNone(bpr)

    def test_ignores_non_summer_batch(self):
        """A PE on a non-Summer batch must NOT be returned by the
        SP-specific helper (program_type filter)."""
        from tap_lms.summer_program.student_progression_sp import (
            _get_active_bpr_for_student,
        )

        # "Regular" is the only valid non-Summer Batch.program_type
        # option in current schema (options: "Summer\nRegular").
        non_sp_batch = make_batch(
            "ActiveBprRegularBatch", batch_id="ABPRRG", program_type="Regular",
        )
        _ensure_active_bpr(non_sp_batch)
        sid = make_student(suffix="REG", phone_prefix=_PHONE_PREFIX)
        make_active_pe(sid, non_sp_batch)

        student = frappe.get_doc("Student", sid)
        batch, bpr = _get_active_bpr_for_student(student)
        self.assertIsNone(
            batch,
            "Helper must skip non-Summer batches (program_type filter)",
        )
        self.assertIsNone(bpr)

    def test_ignores_dropped_pe(self):
        """A dropped/completed PE must not be returned — filter is
        program_status in (active, paused)."""
        from tap_lms.summer_program.student_progression_sp import (
            _get_active_bpr_for_student,
        )

        sid = make_student(suffix="DROP", phone_prefix=_PHONE_PREFIX)
        pe_name = make_active_pe(sid, self.sp_batch)
        # Manually flip to dropped (bypasses state machine — fine for this
        # filter-coverage test).
        frappe.db.set_value(
            "ProgramEnrollment", pe_name, "program_status", "dropped"
        )

        student = frappe.get_doc("Student", sid)
        batch, bpr = _get_active_bpr_for_student(student)
        self.assertIsNone(batch)
        self.assertIsNone(bpr)


class TestCourseLevelResolution(FrappeTestCase):
    """_get_course_level_for_student must read course_level from
    ProgramEnrollment for the active/paused PE matching (student, batch)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch = make_batch("CourseLevelTestBatch", batch_id="CLTB")
        # We don't strictly need a real Course Level row — the helper
        # returns the linked string. Placeholder value is fine.
        cls.placeholder_course_level = "ReadingFluency-Literacy-C0001"

    def test_reads_course_level_from_pe(self):
        from tap_lms.summer_program.student_progression_sp import (
            _get_course_level_for_student,
        )

        sid = make_student(suffix="PECL", phone_prefix=_PHONE_PREFIX)
        pe_name = make_active_pe(sid, self.batch)
        frappe.db.set_value(
            "ProgramEnrollment", pe_name,
            "course_level", self.placeholder_course_level,
        )

        student = frappe.get_doc("Student", sid)
        batch = frappe.get_doc("Batch", self.batch)
        course = _get_course_level_for_student(student, batch)
        self.assertEqual(course, self.placeholder_course_level)

    def test_returns_none_when_no_pe(self):
        from tap_lms.summer_program.student_progression_sp import (
            _get_course_level_for_student,
        )

        sid = make_student(suffix="NOCL", phone_prefix=_PHONE_PREFIX)
        student = frappe.get_doc("Student", sid)
        batch = frappe.get_doc("Batch", self.batch)
        course = _get_course_level_for_student(student, batch)
        self.assertIsNone(course)
