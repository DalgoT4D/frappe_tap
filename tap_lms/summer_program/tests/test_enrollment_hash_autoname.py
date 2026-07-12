"""
CR-2026-06-19 review — the Student Enrollment child doctype uses hash autoname.

The backend onboarding path appends one enrollment per student
(`process_student_record`).  Its old `format:ER{########}` autoname locked the
shared `tabSeries.current` row FOR UPDATE on every insert, so running >1 `long`
worker serialised new-enrollment creation across workers on the single `ER`
counter and could raise SerializationFailure under contention — the same
mechanism BR-003 fixed for the hot SP doctypes (L-071 / L-075).

This guards against an accidental revert of the autoname.  (Unlike BR-003's
StudentStageProgress test, Student Enrollment is a child table — `istable: 1` — so it
isn't inserted standalone here; the meta assertion plus the framework's
hash-naming behaviour cover the conversion, and nothing in the codebase
constructs or parses `ER…` names — verified 2026-06-19.)

Note: `Student` itself is intentionally NOT converted — `Student.name`
(`ST00051383`) IS the canonical student ID (L-031).
"""
import frappe
from frappe.tests.utils import FrappeTestCase


class TestEnrollmentHashAutoname(FrappeTestCase):

    def test_enrollment_meta_autoname_is_hash(self):
        meta = frappe.get_meta("Student Enrollment")
        self.assertEqual(
            meta.autoname, "hash",
            "Student Enrollment must use hash autoname (CR-2026-06-19) — counter "
            "autoname reintroduces tabSeries FOR UPDATE contention under "
            "parallel onboarding workers (L-075)."
        )

    def test_student_remains_counter_named(self):
        """Guard the deliberate asymmetry: Student keeps its ST counter (L-031 —
        Student.name is the canonical student ID and must stay ST########)."""
        meta = frappe.get_meta("Student")
        self.assertTrue(
            (meta.autoname or "").startswith("format:ST"),
            "Student.name must remain the ST-prefixed canonical ID (L-031); "
            f"do NOT convert Student to hash (autoname={meta.autoname!r})."
        )


if __name__ == "__main__":
    import unittest
    unittest.main()
