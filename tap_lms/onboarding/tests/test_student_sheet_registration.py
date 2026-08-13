import sys
import types
import unittest
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


class TestStudentSheetRegistrationSchoolLookup(unittest.TestCase):
    def test_school_lookup_uses_student_school_when_consent_missing(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
        ), patch.object(
            student_sheet_registration,
            "_find_existing_student",
            return_value="ST00000001",
        ), patch.object(
            student_sheet_registration.frappe.db,
            "get_value",
            return_value="SCH-001",
        ):
            school_id, error = student_sheet_registration._get_school_id_for_registration(
                "919876543210",
                "Student One",
            )

        self.assertEqual(school_id, "SCH-001")
        self.assertEqual(error, "")

    def test_school_lookup_errors_when_consent_and_student_missing(self):
        with patch.object(
            student_sheet_registration,
            "_get_latest_student_consent",
            return_value=None,
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
        self.assertEqual(
            error,
            "Student Consent not found and Student not found for contact_phone_number",
        )


if __name__ == "__main__":
    unittest.main()
