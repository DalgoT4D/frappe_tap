import csv
import sys
import types
import unittest
from io import StringIO
from unittest.mock import MagicMock


if "frappe" not in sys.modules:
    frappe_stub = types.ModuleType("frappe")
    frappe_stub.db = MagicMock()
    frappe_stub.throw = MagicMock(side_effect=Exception)
    sys.modules["frappe"] = frappe_stub

try:
    import google.cloud.storage  # noqa: F401
except Exception:
    google_stub = types.ModuleType("google")
    google_cloud_stub = types.ModuleType("google.cloud")
    google_storage_stub = types.ModuleType("google.cloud.storage")
    google_storage_stub.Client = MagicMock()
    google_cloud_stub.storage = google_storage_stub
    google_stub.cloud = google_cloud_stub
    sys.modules.setdefault("google", google_stub)
    sys.modules.setdefault("google.cloud", google_cloud_stub)
    sys.modules.setdefault("google.cloud.storage", google_storage_stub)

try:
    import google.oauth2.service_account  # noqa: F401
except Exception:
    google_oauth2_stub = types.ModuleType("google.oauth2")
    google_service_account_stub = types.ModuleType("google.oauth2.service_account")
    google_service_account_stub.Credentials = MagicMock()
    google_oauth2_stub.service_account = google_service_account_stub
    sys.modules.setdefault("google.oauth2", google_oauth2_stub)
    sys.modules.setdefault("google.oauth2.service_account", google_service_account_stub)

from tap_lms.onboarding.backend_upload_utils import (
    GLIFIC_EXISTING_STUDENT_CSV_HEADERS,
    GLIFIC_NEW_STUDENT_CSV_HEADERS,
    render_glific_contact_csv,
)


class TestGlificContactCsvHeaders(unittest.TestCase):
    def test_new_student_contact_csv_includes_language_and_delete(self):
        row = {
            "name": "New Student",
            "phone": "919999999999",
            "language": "Hindi",
            "delete": 0,
            "school_id": "SCH-001",
            "student_id": "ST00000001",
        }

        rendered = render_glific_contact_csv([row], headers=GLIFIC_NEW_STUDENT_CSV_HEADERS)
        csv_rows = list(csv.reader(StringIO(rendered.decode("utf-8"))))

        self.assertEqual(csv_rows[0][:4], ["name", "phone", "language", "delete"])
        self.assertIn("school_id", csv_rows[0])
        self.assertIn("student_id", csv_rows[0])
        self.assertEqual(csv_rows[1][csv_rows[0].index("delete")], "0")
        self.assertEqual(csv_rows[1][csv_rows[0].index("student_id")], "ST00000001")

    def test_existing_student_contact_csv_excludes_language_and_delete(self):
        row = {
            "name": "Existing Student",
            "phone": "918888888888",
            "language": "Hindi",
            "delete": "0",
            "school_id": "SCH-002",
            "student_id": "ST00000002",
        }

        rendered = render_glific_contact_csv([row], headers=GLIFIC_EXISTING_STUDENT_CSV_HEADERS)
        csv_rows = list(csv.reader(StringIO(rendered.decode("utf-8"))))

        self.assertEqual(csv_rows[0][:2], ["name", "phone"])
        self.assertNotIn("language", csv_rows[0])
        self.assertNotIn("delete", csv_rows[0])
        self.assertIn("school_id", csv_rows[0])
        self.assertIn("student_id", csv_rows[0])
        self.assertEqual(csv_rows[1][csv_rows[0].index("student_id")], "ST00000002")


if __name__ == "__main__":
    unittest.main()
