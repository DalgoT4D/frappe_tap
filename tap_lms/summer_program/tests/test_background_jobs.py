import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _import_background_jobs_with_stubs():
    frappe = MagicMock()
    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.now_datetime = MagicMock(return_value="2026-05-28 15:20:31")
    frappe_utils.cint = lambda value: int(value or 0)
    frappe_utils.flt = lambda value: float(value or 0)

    sys.modules["frappe"] = frappe
    sys.modules["frappe.utils"] = frappe_utils
    sys.modules.pop("tap_lms.summer_program.background_jobs", None)
    return importlib.import_module("tap_lms.summer_program.background_jobs")


class TestBackgroundJobs(unittest.TestCase):
    def test_create_content_completion_log_inserts_without_commit(self):
        background_jobs = _import_background_jobs_with_stubs()
        fake_log = MagicMock()
        fake_log.name = "SCL-1"

        fake_frappe = MagicMock()
        fake_frappe.db.count.return_value = 2
        fake_frappe.get_doc.return_value = fake_log

        with patch.object(background_jobs, "frappe", fake_frappe), \
                patch.object(background_jobs, "get_content_name", return_value="Video 1"):
            log = background_jobs.create_content_completion_log(
                student_id="ST00051359",
                course_level="Scratch Jr Main 1-Coding-C53779",
                progress_name="SSP-1",
                content_type="VideoClass",
                content_id="VC-1",
                action="completed",
                time_spent_seconds=12,
                stage_no=1,
                tier="Basic",
                learning_unit="LU-1",
            )

        self.assertIs(log, fake_log)
        fake_log.insert.assert_called_once_with(ignore_permissions=True)
        fake_frappe.db.commit.assert_not_called()
        doc = fake_frappe.get_doc.call_args.args[0]
        self.assertEqual(doc["doctype"], "StudentContentLog")
        self.assertEqual(doc["student"], "ST00051359")
        self.assertEqual(doc["content_type"], "VideoClass")
        self.assertEqual(doc["content_id"], "VC-1")
        self.assertEqual(doc["action"], "completed")
        self.assertEqual(doc["attempt_number"], 3)

    def test_content_log_job_re_raises_after_logging_failure(self):
        background_jobs = _import_background_jobs_with_stubs()
        fake_log = MagicMock()
        fake_log.insert.side_effect = RuntimeError("insert failed")

        fake_frappe = MagicMock()
        fake_frappe.db.count.return_value = 0
        fake_frappe.get_doc.return_value = fake_log

        with patch.object(background_jobs, "frappe", fake_frappe), \
                patch.object(background_jobs, "get_content_name", return_value="Video 1"):
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                background_jobs.job_log_content_completion(
                    student_id="ST00051359",
                    course_level="Scratch Jr Main 1-Coding-C53779",
                    progress_name="SSP-1",
                    content_type="VideoClass",
                    content_id="VC-1",
                    action="completed",
                    stage_no=1,
                    tier="Basic",
                    learning_unit="LU-1",
                )

        fake_frappe.db.rollback.assert_called_once()
        fake_frappe.log_error.assert_called_once()
        message, title = fake_frappe.log_error.call_args.args
        self.assertEqual(title, "Background Job Error")
        self.assertIn("ST00051359", message)
        self.assertIn("VideoClass:VC-1", message)
        fake_frappe.db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
