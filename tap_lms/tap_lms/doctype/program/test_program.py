import frappe
import unittest
from frappe.utils import add_days, nowdate


class TestProgram(unittest.TestCase):

    def setUp(self):
        self.program_data = {
            "doctype": "Program",
            "program": "Test Program",
        }
        self.batch_base = {
            "doctype": "Batch",
            "name1": "Test Batch",
            "batch_id": "BATCH-T001",
            "start_date": nowdate(),
            "end_date": add_days(nowdate(), 30),
            "active": 1,
            "regist_end_date": add_days(nowdate(), -1),
            "current_calendar_week": 1,
        }
        self.course_level_base = {
            "doctype": "Course Level",
            "name1": "Test Course Level",
        }

    def tearDown(self):
        frappe.db.rollback()

    def make_program(self, **overrides):
        data = {**self.program_data, **overrides}
        doc = frappe.get_doc(data)
        doc.insert(ignore_permissions=True)
        return doc

    def make_batch(self, program_name, **overrides):
        data = {**self.batch_base, "program": program_name, **overrides}
        doc = frappe.get_doc(data)
        doc.insert(ignore_permissions=True)
        return doc

    def make_course_level(self, program_name, **overrides):
        data = {**self.course_level_base, "program": program_name, **overrides}
        doc = frappe.get_doc(data)
        doc.insert(ignore_permissions=True)
        return doc

    def test_program_creation(self):
        doc = self.make_program()
        self.assertTrue(frappe.db.exists("Program", doc.name))

    def test_multiple_batches_link_to_one_program(self):
        prog = self.make_program()
        self.make_batch(prog.name, batch_id="BATCH-MULTI-001", name1="Batch One")
        self.make_batch(prog.name, batch_id="BATCH-MULTI-002", name1="Batch Two")
        batches = frappe.get_all("Batch", filters={"program": prog.name})
        self.assertEqual(len(batches), 2)

    def test_multiple_course_levels_link_to_one_program(self):
        prog = self.make_program()
        self.make_course_level(prog.name, name1="Course Level One")
        self.make_course_level(prog.name, name1="Course Level Two")
        course_levels = frappe.get_all("Course Level", filters={"program": prog.name})
        self.assertEqual(len(course_levels), 2)

    def test_get_batches_returns_correct_program(self):
        prog = self.make_program()
        self.make_batch(prog.name, batch_id="BATCH-GET-001", name1="Batch Get")
        result = frappe.call(
            "tap_lms.tap_lms.doctype.program.program.get_batches",
            program_name=prog.name,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["batch_id"], "BATCH-GET-001")

    def test_get_batches_empty_for_new_program(self):
        prog = self.make_program()
        result = frappe.call(
            "tap_lms.tap_lms.doctype.program.program.get_batches",
            program_name=prog.name,
        )
        self.assertEqual(result, [])

    def test_get_course_levels_returns_correct_program(self):
        prog = self.make_program()
        self.make_course_level(prog.name, name1="Level Alpha")
        result = frappe.call(
            "tap_lms.tap_lms.doctype.program.program.get_course_levels",
            program_name=prog.name,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name1"], "Level Alpha")

    def test_get_course_levels_empty_for_new_program(self):
        prog = self.make_program()
        result = frappe.call(
            "tap_lms.tap_lms.doctype.program.program.get_course_levels",
            program_name=prog.name,
        )
        self.assertEqual(result, [])

    def test_get_program_summary_batch_counts(self):
        prog = self.make_program()
        self.make_batch(prog.name, batch_id="BATCH-SUM-001", name1="Active Batch", active=1)
        self.make_batch(prog.name, batch_id="BATCH-SUM-002", name1="Inactive Batch", active=0)
        result = frappe.call(
            "tap_lms.tap_lms.doctype.program.program.get_program_summary",
            program_name=prog.name,
        )
        self.assertEqual(result["total_batches"], 2)
        self.assertEqual(result["active_batches"], 1)

    def test_get_program_summary_course_level_count(self):
        prog = self.make_program()
        self.make_course_level(prog.name, name1="Level One")
        self.make_course_level(prog.name, name1="Level Two")
        result = frappe.call(
            "tap_lms.tap_lms.doctype.program.program.get_program_summary",
            program_name=prog.name,
        )
        self.assertEqual(result["total_course_levels"], 2)

    def test_get_program_summary_fields(self):
        prog = self.make_program()
        result = frappe.call(
            "tap_lms.tap_lms.doctype.program.program.get_program_summary",
            program_name=prog.name,
        )
        self.assertIn("program", result)
        self.assertIn("total_batches", result)
        self.assertIn("active_batches", result)
        self.assertIn("total_course_levels", result)
        self.assertIn("batches", result)
        self.assertIn("course_levels", result)

    def test_batch_requires_program_link(self):
        with self.assertRaises(frappe.exceptions.ValidationError):
            frappe.get_doc({
                **self.batch_base,
                "batch_id": "BATCH-NOLINK-001",
                "name1": "No Link Batch",
            }).insert(ignore_permissions=True)

    def test_course_level_requires_program_link(self):
        with self.assertRaises(frappe.exceptions.ValidationError):
            frappe.get_doc({
                **self.course_level_base,
                "name1": "No Link Level",
            }).insert(ignore_permissions=True)