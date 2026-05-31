"""
Regression test for gender-overwrite bug fix in process_student_record.

Bug (fixed 2026-05-31): the UPDATE branch in process_student_record previously
overwrote an existing student's gender whenever the incoming import row differed.
Real Female students were flipped to Male on re-import because import data is
unreliable.

Fix: fill-only semantics — gender is set ONLY when the existing student's gender
is blank; an already-set gender is never changed.

Fixed code (backend_onboarding_process.py ~line 1283):
    if student.gender and not existing_student.gender:
        existing_student.gender = student.gender
        updated_fields.append(f"gender: (blank)→{student.gender}")

Tests exercise the UPDATE branch (existing_student_data is truthy) with batch=""
so the enrollment append/save block is skipped, keeping fixtures minimal.
"""
import unittest
from unittest.mock import patch, MagicMock

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_existing_student(gender):
    """Return a MagicMock behaving like a Student doc loaded by frappe.get_doc.

    .name must be a real string (MagicMock(name=x) only sets repr, not attribute).
    .save() is a no-op mock — the test asserts on .gender directly after the call.
    .append() is a no-op mock (not reached when student.batch is falsy).
    """
    doc = MagicMock()
    doc.name = "ST-GENDER-TEST-001"
    doc.gender = gender
    doc.grade = "5"
    doc.school_id = "SCH-001"
    doc.language = "English"
    doc.glific_id = None
    doc.backend_onboarding = None
    doc.save = MagicMock()
    doc.append = MagicMock()
    return doc


def _make_existing_student_data(name="ST-GENDER-TEST-001"):
    """Return a MagicMock whose .name attribute triggers the UPDATE branch."""
    data = MagicMock()
    data.name = name
    return data


def _make_incoming_student(gender, batch=""):
    """Return a MagicMock behaving like the Backend Students doc passed to
    process_student_record.

    batch="" (falsy) skips the enrollment block so the test stays minimal.
    All fields not under test are set to falsy values so their update branches
    are no-ops.
    """
    s = MagicMock()
    s.phone = "919876543210"
    s.student_name = "Test Student"
    s.gender = gender
    s.grade = ""          # falsy → grade branch is a no-op
    s.school = ""         # falsy → school branch is a no-op
    s.language = ""       # falsy → language branch is a no-op
    s.archetype = ""      # falsy → archetype branch is a no-op
    s.experiment_arm = "" # falsy → experiment_arm branch is a no-op
    s.batch = batch       # falsy → enrollment block is skipped
    s.course_vertical = ""
    s.batch_skeyword = ""
    return s


def _run_update_branch(incoming_gender, existing_gender, course_level="CL-TEST"):
    """Exercise the UPDATE branch of process_student_record and return the
    existing_student mock so the caller can assert on .gender.

    Patches:
    - find_existing_student_by_phone_and_name  → returns truthy existing_student_data
    - frappe.get_doc                           → returns controlled existing_student mock
    - normalize_phone_number                   → returns safe tuple ("919876543210", "9876543210")
    - frappe (module-level)                    → generic mock so no network calls happen
    """
    existing_student = _make_existing_student(existing_gender)
    existing_student_data = _make_existing_student_data()
    incoming_student = _make_incoming_student(incoming_gender)

    with patch.object(bop, "find_existing_student_by_phone_and_name",
                      return_value=existing_student_data), \
         patch.object(bop, "normalize_phone_number",
                      return_value=("919876543210", "9876543210")), \
         patch.object(bop, "frappe") as mock_frappe:

        mock_frappe.get_doc.return_value = existing_student
        mock_frappe.get_all.return_value = []
        mock_frappe.db = MagicMock()
        mock_frappe.log_error = MagicMock()
        mock_frappe.logger = MagicMock(return_value=MagicMock())
        mock_frappe.new_doc = MagicMock()

        bop.process_student_record(
            incoming_student,
            None,               # glific_contact — None, no Glific I/O
            "SET-TEST-001",     # batch_id
            None,               # initial_stage
            course_level=course_level,
        )

    return existing_student


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestGenderFillOnlyRegression(unittest.TestCase):
    """Regression suite for the gender-overwrite fix.

    All four cases exercise the UPDATE branch (existing student found by phone+name).
    student.batch is falsy so the enrollment block is skipped; the test asserts
    directly on existing_student.gender after the call returns.
    """

    def test_existing_female_not_overwritten_by_incoming_male(self):
        """Core regression: existing Female must NOT be flipped to Male on re-import.

        This is the production bug: import rows often carry unreliable or wrong
        gender values. A student confirmed Female must stay Female even when the
        import row says Male.
        """
        existing = _run_update_branch(
            incoming_gender="Male",
            existing_gender="Female",
        )
        self.assertEqual(
            existing.gender, "Female",
            "Bug regression: existing Female student was overwritten to Male. "
            "The fill-only fix must prevent this."
        )

    def test_blank_existing_gender_is_filled(self):
        """Happy path: when the existing student has no gender set, the incoming
        gender IS applied (fill-only = populate when blank)."""
        existing = _run_update_branch(
            incoming_gender="Male",
            existing_gender="",
        )
        self.assertEqual(
            existing.gender, "Male",
            "A blank existing gender must be filled from the incoming import row."
        )

    def test_incoming_blank_gender_leaves_existing_untouched(self):
        """When the incoming import row carries no gender, the existing student's
        gender must remain unchanged (fill-only does nothing when source is blank)."""
        existing = _run_update_branch(
            incoming_gender="",
            existing_gender="Female",
        )
        self.assertEqual(
            existing.gender, "Female",
            "An incoming blank gender must not clear or alter an existing gender."
        )

    def test_existing_male_not_flipped_to_female(self):
        """Symmetry proof: fill-only is direction-agnostic. An existing Male must
        not be flipped to Female when the import row says Female."""
        existing = _run_update_branch(
            incoming_gender="Female",
            existing_gender="Male",
        )
        self.assertEqual(
            existing.gender, "Male",
            "An existing Male must not be overwritten with Female on re-import."
        )


if __name__ == "__main__":
    unittest.main()
