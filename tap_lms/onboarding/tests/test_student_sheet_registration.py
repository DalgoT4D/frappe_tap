import csv
import sys
import types
import unittest
from datetime import datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


frappe_stub = sys.modules.setdefault("frappe", types.ModuleType("frappe"))
frappe_stub.db = getattr(frappe_stub, "db", MagicMock())
frappe_stub.flags = getattr(frappe_stub, "flags", types.SimpleNamespace())
frappe_stub.session = getattr(
    frappe_stub,
    "session",
    types.SimpleNamespace(user="Administrator"),
)
frappe_stub.ValidationError = getattr(frappe_stub, "ValidationError", Exception)
frappe_stub.DoesNotExistError = getattr(frappe_stub, "DoesNotExistError", Exception)
frappe_stub.logger = getattr(
    frappe_stub,
    "logger",
    MagicMock(return_value=MagicMock()),
)
frappe_stub.get_doc = getattr(frappe_stub, "get_doc", MagicMock())
frappe_stub.throw = getattr(
    frappe_stub,
    "throw",
    MagicMock(side_effect=Exception),
)
frappe_stub.as_json = getattr(frappe_stub, "as_json", MagicMock())
frappe_stub.parse_json = getattr(frappe_stub, "parse_json", MagicMock())
frappe_stub.whitelist = getattr(
    frappe_stub,
    "whitelist",
    lambda *args, **_kwargs: (
        args[0] if args and callable(args[0]) else lambda fn: fn
    ),
)

frappe_utils_stub = sys.modules.setdefault(
    "frappe.utils",
    types.ModuleType("frappe.utils"),
)
frappe_utils_stub.now_datetime = getattr(
    frappe_utils_stub,
    "now_datetime",
    MagicMock(),
)
frappe_utils_stub.getdate = getattr(frappe_utils_stub, "getdate", lambda value: value)
frappe_stub.utils = getattr(frappe_stub, "utils", frappe_utils_stub)

frappe_model_stub = sys.modules.setdefault(
    "frappe.model",
    types.ModuleType("frappe.model"),
)
frappe_document_stub = sys.modules.setdefault(
    "frappe.model.document",
    types.ModuleType("frappe.model.document"),
)
frappe_document_stub.Document = getattr(frappe_document_stub, "Document", object)
frappe_model_stub.document = frappe_document_stub

try:
    import google.auth.transport.requests  # noqa: F401
except Exception:
    google_stub = sys.modules.setdefault("google", types.ModuleType("google"))
    google_auth_stub = types.ModuleType("google.auth")
    google_transport_stub = types.ModuleType("google.auth.transport")
    google_requests_stub = types.ModuleType("google.auth.transport.requests")
    google_requests_stub.AuthorizedSession = MagicMock()
    google_transport_stub.requests = google_requests_stub
    google_auth_stub.transport = google_transport_stub
    google_stub.auth = google_auth_stub
    sys.modules["google.auth"] = google_auth_stub
    sys.modules["google.auth.transport"] = google_transport_stub
    sys.modules["google.auth.transport.requests"] = google_requests_stub

try:
    import google.cloud.storage  # noqa: F401
except Exception:
    google_stub = sys.modules.setdefault("google", types.ModuleType("google"))
    google_cloud_stub = types.ModuleType("google.cloud")
    google_storage_stub = types.ModuleType("google.cloud.storage")
    google_storage_stub.Client = MagicMock()
    google_cloud_stub.storage = google_storage_stub
    google_stub.cloud = google_cloud_stub
    sys.modules["google.cloud"] = google_cloud_stub
    sys.modules["google.cloud.storage"] = google_storage_stub

try:
    import google.oauth2.service_account  # noqa: F401
except Exception:
    google_stub = sys.modules.setdefault("google", types.ModuleType("google"))
    google_oauth2_stub = types.ModuleType("google.oauth2")
    google_service_account_stub = types.ModuleType("google.oauth2.service_account")
    google_service_account_stub.Credentials = MagicMock()
    google_oauth2_stub.service_account = google_service_account_stub
    google_stub.oauth2 = google_oauth2_stub
    sys.modules["google.oauth2"] = google_oauth2_stub
    sys.modules["google.oauth2.service_account"] = google_service_account_stub

from tap_lms.onboarding import student_sheet_registration
from tap_lms.tap_lms.doctype.student_sheet_registration_job import student_sheet_registration_job


class FakeSchool:
    def __init__(self, enrollments):
        self._enrollments = enrollments

    def get(self, fieldname):
        if fieldname == "batch_enrollments":
            return self._enrollments
        return []


class TestStudentSheetRegistrationSchoolLookup(unittest.TestCase):
    def test_school_lookup_uses_glific_when_consent_missing(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_get_school_id_from_glific",
            return_value=("SCH-GLIFIC", ""),
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value="ST00000001",
        ) as find_existing_student:
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "SCH-GLIFIC")
        self.assertEqual(error, "")
        find_existing_student.assert_not_called()

    def test_school_lookup_errors_when_consent_and_glific_school_missing(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_get_school_id_from_glific",
            return_value=("", ""),
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value="ST00000001",
        ) as find_existing_student:
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "")
        self.assertEqual(error, "Student Consent not found and Glific contact school_id not found")
        find_existing_student.assert_not_called()

    def test_school_lookup_returns_glific_error_before_student_fallback(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_get_school_id_from_glific",
            return_value=("", "Glific contact school lookup failed: timeout"),
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value="ST00000001",
        ) as find_existing_student:
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "")
        self.assertEqual(error, "Glific contact school lookup failed: timeout")
        find_existing_student.assert_not_called()

    def test_school_lookup_errors_when_consent_and_glific_contact_missing(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_get_school_id_from_glific",
            return_value=("", ""),
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value=None,
        ):
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "")
        self.assertEqual(error, "Student Consent not found and Glific contact school_id not found")


class TestStudentSheetRegistrationEnrollmentSelection(unittest.TestCase):
    def test_enrollment_selection_uses_latest_doj_before_registration_timestamp(self):
        batch_1 = SimpleNamespace(batch_number="BATCH-1", doj="2026-08-02 00:00:00", idx=1)
        batch_2 = SimpleNamespace(batch_number="BATCH-2", doj="2026-08-17 00:00:00", idx=2)

        with patch.object(
            student_sheet_registration.frappe,
            "get_doc",
            return_value=FakeSchool([batch_1, batch_2]),
        ):
            enrollment, error = student_sheet_registration._get_school_enrollment_for_registration(
                "SCH-001",
                "19/08/2026 10:00:00",
            )
            earlier_enrollment, earlier_error = (
                student_sheet_registration._get_school_enrollment_for_registration(
                    "SCH-001",
                    "16/08/2026 10:00:00",
                )
            )

        self.assertEqual(enrollment.batch_number, "BATCH-2")
        self.assertEqual(error, "")
        self.assertEqual(earlier_enrollment.batch_number, "BATCH-1")
        self.assertEqual(earlier_error, "")

    def test_enrollment_selection_fails_before_first_school_enrollment(self):
        batch_1 = SimpleNamespace(batch_number="BATCH-1", doj="2026-08-02 00:00:00", idx=1)

        with patch.object(
            student_sheet_registration.frappe,
            "get_doc",
            return_value=FakeSchool([batch_1]),
        ):
            enrollment, error = student_sheet_registration._get_school_enrollment_for_registration(
                "SCH-001",
                "2026-08-01 10:00:00",
            )

        self.assertIsNone(enrollment)
        self.assertIn("School Batch Enrollment not found on or before", error)


class TestStudentSheetRegistrationProcessStatus(unittest.TestCase):
    def test_duplicate_rows_are_complete_process_status(self):
        self.assertEqual(
            student_sheet_registration._process_status_for_row({"message": "Duplicate contact_phone_number"}),
            "complete",
        )

    def test_non_duplicate_errors_are_fail_process_status(self):
        self.assertEqual(
            student_sheet_registration._process_status_for_row({"message": "Invalid grade"}),
            "fail",
        )


class TestStudentSheetRegistrationNotDoneCsv(unittest.TestCase):
    def test_not_done_csv_includes_registration_and_process_status(self):
        rendered = student_sheet_registration._render_not_done_rows_csv([{
            "language": "Hindi",
            "spreadsheet_title": "Registrations",
            "sheet_title": "Hindi",
            "row_number": 5,
            "timestamp": "2026-08-20 10:00:00",
            "student_name": "Student One",
            "contact_phone_number": "9876543210",
            "phone": "919876543210",
            "gender": "Female",
            "grade": "7",
            "school_id": "SCH-001",
            "batch": "BATCH-001",
            "course_vertical": "CV-001",
            "course_names": ["Course A"],
            "level": "Level 2",
            "prepare_status": "Error",
            "message": "Invalid grade",
        }])
        rows = list(csv.DictReader(StringIO(rendered.decode("utf-8"))))

        self.assertEqual(rows[0]["Registration Status"], "Invalid grade")
        self.assertEqual(rows[0]["Process Status"], "fail")
        self.assertEqual(rows[0]["Canonical Phone"], "919876543210")

    def test_not_done_csv_upload_uses_csv_content_type(self):
        with patch.object(
            student_sheet_registration.frappe.utils,
            "now_datetime",
            return_value=datetime(2026, 8, 21, 12, 30, 0),
        ), patch.object(
            student_sheet_registration,
            "_upload_bytes_to_gcs",
            return_value="https://example.com/not-done.csv",
        ) as upload:
            file_url = student_sheet_registration._create_and_upload_not_done_rows_csv([
                {"message": "Invalid grade"}
            ])

        self.assertEqual(file_url, "https://example.com/not-done.csv")
        self.assertEqual(
            upload.call_args.args[1],
            "student-sheet-registration/not-done/"
            "student_sheet_registration_not_done_20260821_123000.csv",
        )
        self.assertEqual(upload.call_args.kwargs["content_type"], "text/csv")


class TestStudentSheetRegistrationCronCounts(unittest.TestCase):
    def test_duplicate_rows_have_separate_count(self):
        counts = student_sheet_registration_job._cron_log_counts({
            "raw_rows": 5,
            "uploaded_rows": 2,
            "skipped_done_rows": 1,
            "duplicate_rows": 1,
            "failed_rows": 2,
        })

        self.assertEqual(counts["processed_rows"], 5)
        self.assertEqual(counts["successful_rows"], 3)
        self.assertEqual(counts["duplicate_rows"], 1)
        self.assertEqual(counts["failed_rows"], 1)

    def test_summary_counts_persist_not_done_rows_file_url(self):
        with patch.object(student_sheet_registration_job.frappe.db, "set_value") as set_value:
            student_sheet_registration_job._set_summary_counts(
                "SSR-00001",
                {"not_done_rows_file_url": "https://example.com/not-done.csv"},
            )

        updates = set_value.call_args.args[2]
        self.assertEqual(
            updates["not_done_rows_file_url"],
            "https://example.com/not-done.csv",
        )


if __name__ == "__main__":
    unittest.main()
