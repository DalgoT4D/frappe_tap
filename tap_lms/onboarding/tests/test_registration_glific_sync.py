from datetime import date
import json
import sys
from types import SimpleNamespace
import types
import unittest
import warnings
from unittest.mock import MagicMock, patch

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import frappe.utils  # noqa: F401
    except Exception:
        frappe_utils_available = False
    else:
        frappe_utils_available = True

if not frappe_utils_available:
    frappe_stub = types.ModuleType("frappe")
    frappe_stub.db = MagicMock()
    frappe_stub.get_single = MagicMock()
    frappe_stub.logger = MagicMock(return_value=MagicMock())
    frappe_utils_stub = types.ModuleType("frappe.utils")
    frappe_utils_stub.getdate = lambda value: value
    frappe_utils_stub.now_datetime = MagicMock()
    sys.modules["frappe"] = frappe_stub
    sys.modules["frappe.utils"] = frappe_utils_stub

from tap_lms.onboarding import glific_sync
from tap_lms import glific_integration


class FakeDoc(SimpleNamespace):
    def get(self, fieldname):
        return getattr(self, fieldname, None)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class TestRegistrationGlificSyncFields(unittest.TestCase):
    def test_teacher_fields_use_school_id_not_school(self):
        teacher = FakeDoc(
            first_name="Teacher One",
            teacher_role="Buddy",
            enrollment=[
                FakeDoc(
                    batch="BATCH-001",
                    school="SCH-001",
                    date_joining=date(2026, 1, 1),
                    idx=1,
                )
            ],
        )

        fields = glific_sync._build_teacher_glific_fields(
            teacher,
            {"state_name": "Maharashtra", "model_name": "Model A"},
            "SCH-001",
        )

        self.assertEqual(fields["school_id"], "SCH-001")
        self.assertNotIn("school", fields)
        self.assertEqual(fields["batch_id"], "BATCH-001")
        self.assertEqual(fields["role"], "Buddy")

    def test_student_fields_include_school_and_school_id_and_mapped_level(self):
        student = FakeDoc(
            name="ST00000001",
            name1="Student One",
            grade="5",
            enrollment=[
                FakeDoc(
                    batch="BATCH-002",
                    school="SCH-002",
                    vertical="CV-CODING",
                    grade="8",
                    date_joining=date(2026, 1, 1),
                    idx=1,
                )
            ],
        )

        with patch.object(glific_sync.frappe, "db", MagicMock()) as db:
            db.get_value.return_value = "Coding"
            fields = glific_sync._build_student_glific_fields(
                student,
                {"state_name": "Punjab", "model_name": "Model B"},
                "SCH-002",
            )

        self.assertEqual(fields["student_id"], "ST00000001")
        self.assertEqual(fields["school_id"], "SCH-002")
        self.assertEqual(fields["school"], "SCH-002")
        self.assertEqual(fields["grade"], "8")
        self.assertEqual(fields["level"], "Level 2")
        self.assertEqual(fields["course"], "Coding")

    def test_field_bootstrap_registers_school_school_id_and_level(self):
        original_bootstrapped = glific_sync._SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED
        glific_sync._SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED = False
        try:
            with patch.object(
                glific_sync,
                "register_contact_field",
                return_value=True,
            ) as register_contact_field:
                glific_sync._ensure_glific_registration_fields()

            shortcodes = [
                call.args[0]
                for call in register_contact_field.call_args_list
            ]
            self.assertIn("school_id", shortcodes)
            self.assertIn("school", shortcodes)
            self.assertIn("student_id", shortcodes)
            self.assertIn("level", shortcodes)
        finally:
            glific_sync._SCRATCH_REGISTRATION_FIELDS_BOOTSTRAPPED = original_bootstrapped

    def test_update_contact_fields_can_remove_legacy_school_field(self):
        existing_fields = json.dumps({
            "school": {"value": "Old School"},
            "other": {"value": "Keep Me"},
        })
        sent_fields = {}

        def post_side_effect(_url, payload):
            nonlocal sent_fields
            if "mutation updateContact" in payload["query"]:
                sent_fields = json.loads(
                    payload["variables"]["input"]["fields"]
                )
                return FakeResponse({
                    "data": {
                        "updateContact": {
                            "contact": {
                                "id": "123",
                                "fields": payload["variables"]["input"]["fields"],
                            },
                            "errors": [],
                        }
                    }
                })

            return FakeResponse({
                "data": {
                    "contact": {
                        "contact": {
                            "id": "123",
                            "name": "Student One",
                            "language": {"id": "1"},
                            "fields": json.dumps(sent_fields),
                        }
                    }
                }
            })

        with patch.object(
            glific_integration,
            "get_glific_settings",
            return_value=FakeDoc(api_url="https://glific.test"),
        ), patch.object(
            glific_integration,
            "_glific_post_with_401_retry",
            side_effect=post_side_effect,
        ):
            ok = glific_integration.update_contact_fields(
                "123",
                {"school_id": "SCH-002", "level": "Level 2"},
                contact_name="Student One",
                existing_fields=existing_fields,
                fields_to_remove=("school",),
            )

        self.assertTrue(ok)
        self.assertNotIn("school", sent_fields)
        self.assertEqual(sent_fields["school_id"]["value"], "SCH-002")
        self.assertEqual(sent_fields["level"]["value"], "Level 2")
        self.assertEqual(sent_fields["other"]["value"], "Keep Me")
