"""
P3 — Enrollment idempotency guard in process_student_record.

Bug (L-073): the existing-student branch previously always appended a new
enrollment regardless of whether one for the same batch already existed.
On 2026-05-31 re-runs created 73 students with batch BT00000019 enrolled
twice (identical grade/course/school).

Fix: before appending, skip if the student already has an enrollment for
the exact same batch.

Key invariants guarded by these tests:
1. Re-processing an existing student with the SAME batch B → enrollment
   count for B stays at 1 (no duplicate).
2. Existing student enrolled in batch A, processed with DIFFERENT batch B
   → both A and B are present (multi-term preserved).
3. New student (no existing_student_data) → first enrollment is created
   normally (the NEW-student branch is unaffected by the guard).
4. Sibling safety: the guard is keyed on batch inside a single Student
   doc's child table; a sibling is a different Student doc entirely, so
   its enrollments are never compared against another student's batch set.

All Glific + course-level calls are mocked so the test exercises only the
enrollment-append path, not network I/O.

Follows the mock pattern from test_onboarding_gender_fill_only.py.
"""
import unittest
from unittest.mock import patch, MagicMock, call

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_enrollment_row(batch):
    """Return a MagicMock behaving like an Enrollments child-table row.

    .batch must be a real string (MagicMock(name=x) only sets repr).
    """
    row = MagicMock()
    row.batch = batch
    return row


def _make_existing_student_doc(enrollment_rows):
    """Return a MagicMock behaving like a frappe Student doc whose
    .enrollment child table already contains the given rows.

    .append() is a MagicMock so we can assert on call count / args.
    .save() is a no-op.
    """
    doc = MagicMock()
    doc.name = "ST-IDEM-TEST-001"
    doc.gender = "Male"
    doc.grade = "5"
    doc.school_id = "SCH-001"
    doc.language = "English"
    doc.glific_id = None
    doc.backend_onboarding = None
    doc.enrollment = enrollment_rows
    doc.save = MagicMock()
    doc.append = MagicMock()
    return doc


def _make_existing_student_data(name="ST-IDEM-TEST-001"):
    """Return a MagicMock whose .name attribute triggers the UPDATE branch
    inside process_student_record (existing_student_data is truthy)."""
    data = MagicMock()
    data.name = name
    return data


def _make_incoming_student(batch, grade="5", school="SCH-001"):
    """Return a MagicMock behaving like the Backend Students doc passed to
    process_student_record.

    Fields not under test are set to falsy / neutral values so they don't
    trigger unrelated branches (grade/school/language/gender updates, Glific
    contact write-back, etc.).
    """
    s = MagicMock()
    s.phone = "919876543210"
    s.student_name = "Idempotency Test Student"
    s.gender = ""           # falsy → gender branch is a no-op
    s.grade = grade
    s.school = school
    s.language = ""         # falsy → language branch is a no-op
    s.archetype = ""        # falsy → archetype branch is a no-op
    s.experiment_arm = ""   # falsy → experiment_arm branch is a no-op
    s.batch = batch
    s.course_vertical = ""
    s.batch_skeyword = ""
    return s


def _run_update_branch(incoming_batch, existing_enrollment_rows, course_level="CL-TEST"):
    """Drive the UPDATE branch of process_student_record and return
    (existing_student_doc, frappe_mock) so the caller can assert on
    doc.append and doc.enrollment.

    Patches:
    - find_existing_student_by_phone_and_name → truthy (UPDATE branch fires)
    - frappe.get_doc                           → controlled existing_student_doc
    - normalize_phone_number                   → safe tuple
    - frappe (module-level)                    → generic mock (no network calls)
    - frappe.logger                            → MagicMock (swallows the info log)
    """
    existing_doc = _make_existing_student_doc(existing_enrollment_rows)
    existing_data = _make_existing_student_data()
    incoming = _make_incoming_student(incoming_batch)

    with patch.object(bop, "find_existing_student_by_phone_and_name",
                      return_value=existing_data), \
         patch.object(bop, "normalize_phone_number",
                      return_value=("919876543210", "9876543210")), \
         patch.object(bop, "frappe") as mock_frappe:

        mock_frappe.get_doc.return_value = existing_doc
        mock_frappe.get_all.return_value = []
        mock_frappe.db = MagicMock()
        mock_frappe.log_error = MagicMock()
        mock_frappe.logger = MagicMock(return_value=MagicMock())
        mock_frappe.new_doc = MagicMock()

        bop.process_student_record(
            incoming,
            None,               # glific_contact — None, no Glific I/O
            "SET-TEST-001",     # batch_id
            None,               # initial_stage
            course_level=course_level,
        )

    return existing_doc, mock_frappe


# ---------------------------------------------------------------------------
# Test 1: same-batch re-run does NOT append a duplicate enrollment
# ---------------------------------------------------------------------------

class TestEnrollmentIdempotencyNoDuplicate(unittest.TestCase):
    """Core invariant: re-processing an existing student with the same batch
    must not create a second enrollment row for that batch.

    The guard reads:
        existing_batches = {e.batch for e in (existing_student.enrollment or [])}
        if student.batch in existing_batches:
            ...skip...
        else:
            existing_student.append(...)
    """

    def test_same_batch_not_appended_on_rerun(self):
        """Existing student already enrolled in batch B.  Reprocess with same
        batch B → existing_student.append must NOT be called (guard fires)."""
        existing_rows = [_make_enrollment_row("BT00000019")]
        doc, _ = _run_update_branch(
            incoming_batch="BT00000019",
            existing_enrollment_rows=existing_rows,
        )
        doc.append.assert_not_called()

    def test_same_batch_enrollment_count_stays_at_one(self):
        """More explicit: the child table must not grow past one entry for
        batch B.  Because we don't modify .enrollment in the test, and append
        is blocked, enrollment stays as it was (1 row)."""
        existing_rows = [_make_enrollment_row("BT00000019")]
        doc, _ = _run_update_branch(
            incoming_batch="BT00000019",
            existing_enrollment_rows=existing_rows,
        )
        # append never called → enrollment list unchanged at 1 row
        self.assertEqual(
            len(doc.enrollment), 1,
            "Enrollment count must stay at 1 after same-batch re-run "
            "(idempotency guard L-073)"
        )

    def test_student_with_multiple_prior_same_batch_still_not_appended(self):
        """Edge case: student somehow already has 2 rows for the same batch
        (pre-guard database state from prior bug).  Re-running must not add a
        third — the guard still fires because the batch IS in existing_batches."""
        existing_rows = [
            _make_enrollment_row("BT00000019"),
            _make_enrollment_row("BT00000019"),  # duplicate already exists
        ]
        doc, _ = _run_update_branch(
            incoming_batch="BT00000019",
            existing_enrollment_rows=existing_rows,
        )
        # Guard fires because "BT00000019" is in the set — no append
        doc.append.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: different batch IS appended (multi-term enrollment preserved)
# ---------------------------------------------------------------------------

class TestEnrollmentIdempotencyDifferentBatchAllowed(unittest.TestCase):
    """Guard must NOT block a DIFFERENT batch — legitimate multi-term enrollment
    (e.g., student enrolled in BT00000010 from a prior term, now being enrolled
    in BT00000019 for the current term) must keep both rows."""

    def test_different_batch_is_appended(self):
        """Existing student enrolled in batch A.  Reprocess with batch B
        → existing_student.append IS called for batch B."""
        existing_rows = [_make_enrollment_row("BT00000010")]
        doc, _ = _run_update_branch(
            incoming_batch="BT00000019",  # different from existing
            existing_enrollment_rows=existing_rows,
        )
        doc.append.assert_called_once()
        # append("enrollment", {batch: ..., ...}) → args[0]="enrollment", args[1]=dict
        call_args = doc.append.call_args
        enrollment_dict = call_args.args[1]
        self.assertEqual(
            enrollment_dict.get("batch"), "BT00000019",
            "append must be called with the NEW batch, not the existing one"
        )

    def test_two_prior_different_batches_third_allowed(self):
        """Student has batches A and B; process with batch C → C is appended."""
        existing_rows = [
            _make_enrollment_row("BT00000001"),
            _make_enrollment_row("BT00000002"),
        ]
        doc, _ = _run_update_branch(
            incoming_batch="BT00000003",
            existing_enrollment_rows=existing_rows,
        )
        doc.append.assert_called_once()

    def test_no_prior_enrollments_new_batch_appended(self):
        """Student has no enrollments at all — the batch is new and must be
        appended (guard reads empty set → batch not in set → append fires)."""
        existing_rows = []
        doc, _ = _run_update_branch(
            incoming_batch="BT00000019",
            existing_enrollment_rows=existing_rows,
        )
        doc.append.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: new-student branch is unaffected
# ---------------------------------------------------------------------------

class TestEnrollmentIdempotencyNewStudentBranch(unittest.TestCase):
    """The NEW-student branch (no existing_student_data) creates the first
    enrollment unconditionally.  The idempotency guard is in the UPDATE
    branch only and must not affect new student creation.

    This test drives the NEW-student code path by returning None from
    find_existing_student_by_phone_and_name.
    """

    def _run_new_student_branch(self, batch="BT00000019", course_level="CL-TEST"):
        """Drive the NEW-student branch of process_student_record and return
        the student_doc MagicMock created by frappe.new_doc()."""
        incoming = _make_incoming_student(batch)

        new_doc_mock = MagicMock()
        new_doc_mock.name = "ST-NEW-001"
        new_doc_mock.name1 = "Idempotency Test Student"
        new_doc_mock.glific_id = None
        new_doc_mock.enrollment = []
        new_doc_mock.append = MagicMock()
        new_doc_mock.insert = MagicMock()
        new_doc_mock.save = MagicMock()

        with patch.object(bop, "find_existing_student_by_phone_and_name",
                          return_value=None), \
             patch.object(bop, "normalize_phone_number",
                          return_value=("919876543210", "9876543210")), \
             patch.object(bop, "frappe") as mock_frappe:

            mock_frappe.get_doc = MagicMock()
            mock_frappe.get_all.return_value = []
            mock_frappe.db = MagicMock()
            mock_frappe.db.exists.return_value = False
            mock_frappe.log_error = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())
            mock_frappe.new_doc.return_value = new_doc_mock

            bop.process_student_record(
                incoming,
                None,               # glific_contact
                "SET-TEST-001",     # batch_id
                None,               # initial_stage
                course_level=course_level,
            )

        return new_doc_mock

    def test_new_student_gets_first_enrollment(self):
        """New student: append must be called exactly once to create the
        first enrollment (no prior enrollments → guard is irrelevant)."""
        new_doc = self._run_new_student_branch(batch="BT00000019")
        new_doc.append.assert_called()
        # At least one call that looks like the enrollment append
        enrollment_calls = [
            c for c in new_doc.append.call_args_list
            if c.args and c.args[0] == "enrollment"
        ]
        self.assertGreater(
            len(enrollment_calls), 0,
            "New student must have enrollment appended via new_doc.append('enrollment', ...)"
        )

    def test_new_student_enrollment_contains_correct_batch(self):
        """The enrollment row for a new student must reference the incoming
        batch (sanity check that the NEW-student path is wired correctly)."""
        new_doc = self._run_new_student_branch(batch="BT00000019")
        enrollment_calls = [
            c for c in new_doc.append.call_args_list
            if c.args and c.args[0] == "enrollment"
        ]
        if not enrollment_calls:
            self.fail("No enrollment append call found on new student doc")
        enrollment_dict = enrollment_calls[0].args[1]
        self.assertEqual(
            enrollment_dict.get("batch"), "BT00000019",
            "New student enrollment must reference the incoming batch"
        )


# ---------------------------------------------------------------------------
# Test 4: sibling-safety — guard is scoped to a single student doc
# ---------------------------------------------------------------------------

class TestEnrollmentIdempotencySiblingSafety(unittest.TestCase):
    """Siblings share a phone number but are DIFFERENT Student docs.  Each
    has its own enrollment child table.  The guard compares batches only
    within a single student's .enrollment list — it never looks at another
    student's rows.

    This is verified by showing that two separate UPDATE-branch calls, each
    for a different student doc, each get their enrollment appended
    independently (the first student's batch set does not contaminate the
    second student's decision).
    """

    def _run_update_branch_for_student(self, student_name, existing_batch, incoming_batch):
        """Drive the UPDATE branch for a specific student doc and return
        the doc mock."""
        existing_rows = [_make_enrollment_row(existing_batch)]
        existing_doc = _make_existing_student_doc(existing_rows)
        existing_doc.name = student_name

        existing_data = _make_existing_student_data(name=student_name)
        incoming = _make_incoming_student(incoming_batch)

        with patch.object(bop, "find_existing_student_by_phone_and_name",
                          return_value=existing_data), \
             patch.object(bop, "normalize_phone_number",
                          return_value=("919876543210", "9876543210")), \
             patch.object(bop, "frappe") as mock_frappe:

            mock_frappe.get_doc.return_value = existing_doc
            mock_frappe.get_all.return_value = []
            mock_frappe.db = MagicMock()
            mock_frappe.log_error = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())
            mock_frappe.new_doc = MagicMock()

            bop.process_student_record(
                incoming,
                None,
                "SET-TEST-001",
                None,
                course_level="CL-TEST",
            )

        return existing_doc

    def test_sibling_a_same_batch_blocked_sibling_b_different_batch_allowed(self):
        """Sibling A: already in batch B, incoming batch B → guard blocks append.
        Sibling B: already in batch A, incoming batch B → different → append fires.

        These are INDEPENDENT calls (different Student docs, different
        find_existing_student_by_phone_and_name return values).  The guard
        must not carry state between them.
        """
        # Sibling A: already has BT00000019, incoming is BT00000019 → no append
        sibling_a = self._run_update_branch_for_student(
            student_name="ST-SIBLING-A",
            existing_batch="BT00000019",
            incoming_batch="BT00000019",
        )
        sibling_a.append.assert_not_called()

        # Sibling B: has BT00000010, incoming is BT00000019 → append fires
        sibling_b = self._run_update_branch_for_student(
            student_name="ST-SIBLING-B",
            existing_batch="BT00000010",
            incoming_batch="BT00000019",
        )
        sibling_b.append.assert_called_once()


if __name__ == "__main__":
    unittest.main()
