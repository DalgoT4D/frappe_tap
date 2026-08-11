import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _frappe_whitelist(**_kwargs):
    def decorator(fn):
        return fn

    return decorator


def _load_student_registration_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "student_registration.py"
    )
    module_name = "_student_registration_under_test"
    original_modules = {}

    frappe_stub = types.ModuleType("frappe")
    frappe_stub.db = MagicMock()
    frappe_stub.flags = types.SimpleNamespace()
    frappe_stub.response = {}
    frappe_stub.request = types.SimpleNamespace(data=None)
    frappe_stub.form_dict = {}
    frappe_stub.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})
    frappe_stub.whitelist = _frappe_whitelist
    frappe_stub.enqueue = MagicMock()
    frappe_stub.log_error = MagicMock()
    frappe_stub.get_traceback = MagicMock(return_value="")
    frappe_stub.as_json = MagicMock(return_value="{}")
    frappe_stub.get_doc = MagicMock()

    frappe_utils_stub = types.ModuleType("frappe.utils")
    frappe_utils_stub.now_datetime = MagicMock()

    student_stub = types.ModuleType("tap_lms.tap_lms.doctype.student.student")
    student_stub._reserve_next_student_name = MagicMock(return_value="ST00000001")

    onboarding_utils_stub = types.ModuleType("tap_lms.onboarding.utils")
    for name in (
        "_enqueue_glific_contact_sync",
        "_get_course_level_label_for_grade",
        "_get_language_id_to_name",
        "_get_language_name_to_id",
        "_get_latest_enrollment",
        "_get_request_data",
        "_get_school_course_vertical_names",
        "_get_school_row_by_id",
        "_phone_for_response",
        "_phone_filter",
        "_require_valid_phone",
        "_respond",
    ):
        setattr(onboarding_utils_stub, name, MagicMock())

    api_failures_stub = types.ModuleType("tap_lms.utils.api_failures")
    api_failures_stub.log_api_failure = MagicMock()

    stubs = {
        "frappe": frappe_stub,
        "frappe.utils": frappe_utils_stub,
        "tap_lms.tap_lms.doctype.student.student": student_stub,
        "tap_lms.onboarding.utils": onboarding_utils_stub,
        "tap_lms.utils.api_failures": api_failures_stub,
    }
    for name, module in stubs.items():
        original_modules[name] = sys.modules.get(name)
        sys.modules[name] = module

    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original_module in original_modules.items():
            if original_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original_module


student_registration = _load_student_registration_module()


class TestStudentRegistrationType(unittest.TestCase):
    def test_registration_type_flows_only_for_allowed_cities(self):
        for city in student_registration.SCHOOL_FLOW_REGISTRATION_CITIES:
            with self.subTest(city=city):
                self.assertEqual(
                    student_registration._get_school_registration_type(city),
                    "flow",
                )

    def test_registration_type_defaults_to_form(self):
        for city in (None, "", "DELHI", "UTTAR PRADESH", "Mumbai"):
            with self.subTest(city=city):
                self.assertEqual(
                    student_registration._get_school_registration_type(city),
                    "form",
                )

    def test_verify_school_by_id_uses_city_for_registration_type(self):
        school_row = {
            "school_id": "SCH-001",
            "school_name": "Test School",
            "state": "MAHARASHTRA",
            "district": "Test District",
            "city": "DoE Zone 27",
        }
        responses = []

        def collect_response(code, payload):
            responses.append((code, payload))

        with patch.object(
            student_registration,
            "_get_school_row_by_id",
            return_value=school_row,
        ), patch.object(student_registration, "_respond", side_effect=collect_response):
            student_registration.verify_school_by_id("SCH-001")

        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]["registration_type"], "flow")


if __name__ == "__main__":
    unittest.main()
