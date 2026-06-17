"""
Tests for the sibling-skip behaviour in SP enrollment (interim, pre sibling-PRD).

Guards: at PE creation time, if another active/paused PE in the same batch
already uses the same glific_id, the candidate is treated as a sibling on a
shared household WhatsApp number and skipped. Backed by a partial unique
index in patches/v0_2/add_pe_glific_id_unique_index.py.

Tests in this file:
  1. test_chunk_skip_when_sibling_already_enrolled
  2. test_chunk_creates_pe_when_no_sibling
  3. test_chunk_does_not_skip_when_glific_id_empty
  4. test_single_api_skip_when_sibling_already_enrolled
  5. test_single_api_succeeds_when_no_sibling
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from tap_lms.summer_program.tests.factories import make_batch


def _ensure_batch():
    # Delegates to the shared factory (L-037) so this fixture inherits future
    # mandatory-field additions instead of breaking with MandatoryError.
    return make_batch(label="SiblingSkipTestBatch", batch_id="SST01")


def _ensure_student(suffix, glific_id, name1=None, archetype="fence_sitter"):
    """Idempotent: looks up by phone (unique per suffix) and creates if missing.
    Always re-asserts glific_id so cross-test contamination doesn't pollute the
    sibling lookup."""
    phone = f"+9999600{suffix}"
    existing = frappe.get_value("Student", {"phone": phone}, "name")
    if existing:
        # Re-set in case a previous test changed it
        frappe.db.set_value("Student", existing, {"glific_id": glific_id})
        return existing
    s = frappe.new_doc("Student")
    s.name1 = name1 or f"SiblingSkipStudent{suffix}"
    s.phone = phone
    s.glific_id = glific_id
    s.archetype = archetype
    s.experiment_arm = "arm_a"
    s.language = "English"
    s.insert(ignore_permissions=True)
    return s.name


def _make_active_pe(student_id, batch_name, glific_id):
    """Helper: directly insert an active PE for the sibling-already-enrolled
    pre-condition. Skips _process_pe_chunk entirely to avoid recursion."""
    from tap_lms.summer_program.constants import (
        STATE_NORMAL_CONTENT, LABEL_ENROLLED, PROGRAM_ACTIVE, PATH_CORE,
    )
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"{student_id}-{batch_name}-presibling"
    pe.student = student_id
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = glific_id or ""
    pe.archetype = "fence_sitter"
    pe.experiment_arm = "arm_a"
    pe.current_path = PATH_CORE
    pe.current_tier = "Basic"
    pe.journey_label = LABEL_ENROLLED
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.current_week = 1
    pe.insert(ignore_permissions=True)
    return pe.name


# ════════════════════════════════════════════════════════════
# 1-3. Chunk-path tests
# ════════════════════════════════════════════════════════════

class TestSiblingSkipChunkPath(FrappeTestCase):
    """The batch chunk worker (_process_pe_chunk) must skip duplicates
    on the same Glific contact within a batch."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.program_enrollment_api.frappe.enqueue")
    def test_chunk_skip_when_sibling_already_enrolled(self, _mock_enqueue):
        from tap_lms.summer_program.program_enrollment_api import _process_pe_chunk

        SHARED_GLIFIC = "glific-sib-shared-1"

        # Sibling A is already enrolled (set up via direct PE insert).
        sib_a = _ensure_student("SIBA", SHARED_GLIFIC, name1="SiblingA")
        _make_active_pe(sib_a, self.batch_name, SHARED_GLIFIC)

        # Sibling B has the same Glific contact (shared household phone) but
        # is a different Frappe Student record. _process_pe_chunk for sib_b
        # should skip — no new PE created.
        sib_b = _ensure_student("SIBB", SHARED_GLIFIC, name1="SiblingB")

        # Sanity: only one PE in batch before the chunk runs
        pe_count_before = frappe.db.count(
            "ProgramEnrollment",
            {"batch": self.batch_name, "glific_id": SHARED_GLIFIC,
             "program_status": ["in", ["active", "paused"]]},
        )
        self.assertEqual(pe_count_before, 1)

        # Run the chunk worker for sib_b
        _process_pe_chunk(
            bpr_name=None,
            batch_name=self.batch_name,
            student_ids=[sib_b],
            chunk_index=0,
        )

        # After: still only one active PE for this glific_id
        pe_count_after = frappe.db.count(
            "ProgramEnrollment",
            {"batch": self.batch_name, "glific_id": SHARED_GLIFIC,
             "program_status": ["in", ["active", "paused"]]},
        )
        self.assertEqual(
            pe_count_after, 1,
            "Sibling skip failed: second PE was created for the same glific_id"
        )

        # Sib_b should NOT have any PE in this batch
        sib_b_pe = frappe.db.exists(
            "ProgramEnrollment",
            {"student": sib_b, "batch": self.batch_name},
        )
        self.assertFalse(
            sib_b_pe,
            f"Sibling skip failed: PE {sib_b_pe} was created for sib_b"
        )

    @patch("tap_lms.summer_program.program_enrollment_api.frappe.enqueue")
    def test_chunk_creates_pe_when_no_sibling(self, _mock_enqueue):
        """Negative control: a unique glific_id should still create a PE."""
        from tap_lms.summer_program.program_enrollment_api import _process_pe_chunk

        UNIQUE_GLIFIC = "glific-uniq-2"
        student = _ensure_student("UNIQ", UNIQUE_GLIFIC, name1="UniqueStudent")

        _process_pe_chunk(
            bpr_name=None,
            batch_name=self.batch_name,
            student_ids=[student],
            chunk_index=0,
        )

        pe_name = frappe.db.exists(
            "ProgramEnrollment",
            {"student": student, "batch": self.batch_name},
        )
        self.assertTrue(
            pe_name,
            "Expected PE to be created for student with unique glific_id"
        )

    @patch("tap_lms.summer_program.program_enrollment_api.frappe.enqueue")
    def test_chunk_does_not_skip_when_glific_id_empty(self, _mock_enqueue):
        """Edge case: empty glific_id should NOT trigger sibling-skip.
        Multiple Students with no Glific contact can coexist in the batch."""
        from tap_lms.summer_program.program_enrollment_api import _process_pe_chunk

        # Two students both with empty glific_id — neither is a sibling
        s1 = _ensure_student("EMP1", "", name1="EmptyOne")
        s2 = _ensure_student("EMP2", "", name1="EmptyTwo")

        _process_pe_chunk(
            bpr_name=None,
            batch_name=self.batch_name,
            student_ids=[s1, s2],
            chunk_index=0,
        )

        # Both should have PEs
        self.assertTrue(frappe.db.exists("ProgramEnrollment",
            {"student": s1, "batch": self.batch_name}))
        self.assertTrue(frappe.db.exists("ProgramEnrollment",
            {"student": s2, "batch": self.batch_name}))


# ════════════════════════════════════════════════════════════
# 4-5. Single-PE API tests
# ════════════════════════════════════════════════════════════

class TestSiblingSkipSingleAPI(FrappeTestCase):
    """create_program_enrollment (the whitelisted single-PE API) must
    return a structured skip response when a sibling already exists."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    @patch("tap_lms.summer_program.program_enrollment_api.frappe.enqueue")
    def test_single_api_skip_when_sibling_already_enrolled(self, _mock_enqueue):
        from tap_lms.summer_program.program_enrollment_api import create_program_enrollment

        SHARED_GLIFIC = "glific-sib-shared-single"
        sib_a = _ensure_student("SIB1A", SHARED_GLIFIC, name1="SingleAPISibA")
        existing_pe = _make_active_pe(sib_a, self.batch_name, SHARED_GLIFIC)

        sib_b = _ensure_student("SIB1B", SHARED_GLIFIC, name1="SingleAPISibB")

        result = create_program_enrollment(
            student_id=sib_b,
            batch_id=self.batch_name,
        )

        # Skip response shape
        self.assertFalse(result.get("success"))
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "sibling_enrolled")
        self.assertEqual(result.get("existing_pe"), existing_pe)

        # No new PE created for sib_b
        self.assertFalse(frappe.db.exists("ProgramEnrollment",
            {"student": sib_b, "batch": self.batch_name}))

    @patch("tap_lms.summer_program.program_enrollment_api.frappe.enqueue")
    def test_single_api_succeeds_when_no_sibling(self, _mock_enqueue):
        from tap_lms.summer_program.program_enrollment_api import create_program_enrollment

        UNIQUE_GLIFIC = "glific-uniq-single"
        student = _ensure_student("UNIQ1", UNIQUE_GLIFIC, name1="SingleAPIUnique")

        result = create_program_enrollment(
            student_id=student,
            batch_id=self.batch_name,
        )

        self.assertTrue(result.get("success"))
        self.assertFalse(result.get("skipped"))
        self.assertTrue(frappe.db.exists("ProgramEnrollment",
            {"student": student, "batch": self.batch_name}))
