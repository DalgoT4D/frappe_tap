"""Tests for tap_lms.teacher_bot.api — start_submission and save_course_grade.

Runs against the local site, no HTTP and no Glific:
the endpoints are called as plain Python and `frappe.response` is inspected.

Run:
    bench --site tap_lms.localhost run-tests \
        --module tap_lms.teacher_bot.tests.test_teacher_bot_api

The endpoints call frappe.db.commit(), which defeats the test framework's
automatic rollback — so every record created here is deleted in tearDown.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from tap_lms.teacher_bot.api import save_course_grade, start_submission


KNOWN_PHONE = "919000000001"
UNKNOWN_PHONE = "919000000002"
STATE = "TB Test State"
DISTRICT = "TB Test District"
SCHOOL_NAME = "TB Test School"


def call(endpoint, payload):
    """Invoke a whitelisted endpoint and return (status_code, response_dict).

    The endpoints read their input with `_get_request_data()`, which falls back
    to `frappe.form_dict` when there is no HTTP request — so we set that.
    """
    old_form_dict = frappe.local.form_dict
    old_response = frappe.local.response
    old_request = getattr(frappe.local, "request", None)

    frappe.local.request = None
    frappe.local.form_dict = frappe._dict(payload)
    frappe.local.response = frappe._dict()
    frappe.flags.api_failure_logged = False

    try:
        endpoint()
        response = dict(frappe.local.response)
    finally:
        frappe.local.form_dict = old_form_dict
        frappe.local.response = old_response
        frappe.local.request = old_request

    return response.get("http_status_code", 200), response


class TestTeacherBotAPI(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.school = cls._ensure_school()
        cls.teacher = cls._ensure_teacher(cls.school)
        cls.course = cls._any_course()
        frappe.db.commit()

    # ---------------- fixtures ----------------

    @staticmethod
    def _ensure_school():
        if not frappe.db.exists("State", STATE):
            frappe.get_doc({"doctype": "State", "state_name": STATE}).insert(
                ignore_permissions=True
            )
        if not frappe.db.exists("District", DISTRICT):
            frappe.get_doc(
                {"doctype": "District", "district_name": DISTRICT, "state": STATE}
            ).insert(ignore_permissions=True)

        school = frappe.db.get_value("School", {"name1": SCHOOL_NAME}, "name")
        if school:
            return school

        return frappe.get_doc({
            "doctype": "School",
            "name1": SCHOOL_NAME,
            "type": "GOVT",
            "district": DISTRICT,
            "state": STATE,
        }).insert(ignore_permissions=True).name

    @staticmethod
    def _ensure_teacher(school):
        teacher = frappe.db.get_value("Teacher", {"phone_number": KNOWN_PHONE}, "name")
        if teacher:
            return teacher

        return frappe.get_doc({
            "doctype": "Teacher",
            "first_name": "TBTest",
            "last_name": "Teacher",
            "phone_number": KNOWN_PHONE,
            "school_id": school,
            "teacher_role": "Teacher",
        }).insert(ignore_permissions=True).name

    @staticmethod
    def _any_course():
        """Course Verticals is reference data — skip the suite if it is empty."""
        courses = frappe.get_all("Course Verticals", pluck="name", limit=1)
        return courses[0] if courses else None

    def tearDown(self):
        """The endpoints commit, so clean up by hand."""
        for name in frappe.get_all(
            "Teacher Submission",
            filters={"phone_number": ["in", [KNOWN_PHONE, UNKNOWN_PHONE]]},
            pluck="name",
        ):
            frappe.delete_doc("Teacher Submission", name, force=True, ignore_permissions=True)
        frappe.db.commit()

    # ---------------- start_submission ----------------

    def test_known_phone_creates_submission(self):
        code, response = call(start_submission, {"phone": KNOWN_PHONE})

        self.assertEqual(code, 200)
        self.assertEqual(response["status"], "success")
        self.assertTrue(response["known"])
        self.assertTrue(response["submission_id"].startswith("TSUB-"))

        doc = frappe.get_doc("Teacher Submission", response["submission_id"])
        self.assertEqual(doc.teacher, self.teacher)
        self.assertEqual(doc.school_id, self.school)
        self.assertEqual(doc.status, "Started")
        self.assertEqual(doc.is_unknown_number, 0)
        self.assertTrue(doc.submitted_at)

    def test_ten_digit_phone_matches_stored_twelve_digit(self):
        """Glific may omit the country code — the lookup must still find her."""
        code, response = call(start_submission, {"phone": KNOWN_PHONE[2:]})

        self.assertEqual(code, 200)
        self.assertTrue(response["known"])
        self.assertEqual(response["phone"], KNOWN_PHONE)

    def test_unknown_phone_still_creates_flagged_submission(self):
        """PRD section 5: never go silent, never refuse."""
        code, response = call(start_submission, {"phone": UNKNOWN_PHONE})

        self.assertEqual(code, 200)
        self.assertFalse(response["known"])

        doc = frappe.get_doc("Teacher Submission", response["submission_id"])
        self.assertEqual(doc.is_unknown_number, 1)
        self.assertFalse(doc.teacher)
        self.assertFalse(doc.school_id)

    def test_glific_timestamp_is_used(self):
        sent_at = "2026-08-19 21:45:00"
        code, response = call(
            start_submission, {"phone": KNOWN_PHONE, "submitted_at": sent_at}
        )

        self.assertEqual(code, 200)
        doc = frappe.get_doc("Teacher Submission", response["submission_id"])
        self.assertEqual(get_datetime(doc.submitted_at), get_datetime(sent_at))

    def test_malformed_timestamp_is_rejected(self):
        """Fail loudly rather than silently substituting server time."""
        code, response = call(
            start_submission, {"phone": KNOWN_PHONE, "submitted_at": "not-a-date"}
        )

        self.assertEqual(code, 400)
        self.assertEqual(response["status"], "failure")

    def test_bad_phone_is_rejected(self):
        for bad_phone in ["", "123", "91900000000123", "abcdefghij"]:
            with self.subTest(phone=bad_phone):
                code, _ = call(start_submission, {"phone": bad_phone})
                self.assertEqual(code, 400)

    def test_each_call_gets_its_own_id(self):
        _, first = call(start_submission, {"phone": KNOWN_PHONE})
        _, second = call(start_submission, {"phone": KNOWN_PHONE})
        self.assertNotEqual(first["submission_id"], second["submission_id"])

    # ---------------- save_course_grade ----------------

    def _new_submission(self):
        _, response = call(start_submission, {"phone": KNOWN_PHONE})
        return response["submission_id"]

    def test_saves_course_and_grade(self):
        if not self.course:
            self.skipTest("no Course Verticals records on this site")

        submission_id = self._new_submission()
        code, response = call(save_course_grade, {
            "submission_id": submission_id,
            "course": self.course,
            "grade": "7",
        })

        self.assertEqual(code, 200)
        self.assertEqual(response["submission_status"], "Details Added")

        doc = frappe.get_doc("Teacher Submission", submission_id)
        self.assertEqual(doc.course, self.course)
        self.assertEqual(doc.grade, "7")
        self.assertEqual(doc.status, "Details Added")

    def test_calling_twice_overwrites_instead_of_failing(self):
        """Glific retries webhooks — a repeat call must not error."""
        if not self.course:
            self.skipTest("no Course Verticals records on this site")

        submission_id = self._new_submission()
        payload = {"submission_id": submission_id, "course": self.course, "grade": "7"}

        first_code, _ = call(save_course_grade, payload)
        second_code, _ = call(save_course_grade, payload)

        self.assertEqual(first_code, 200)
        self.assertEqual(second_code, 200)

    def test_missing_submission_id(self):
        code, _ = call(save_course_grade, {"course": "Arts", "grade": "7"})
        self.assertEqual(code, 400)

    def test_unknown_submission_id(self):
        code, _ = call(save_course_grade, {
            "submission_id": "TSUB-DOES-NOT-EXIST",
            "course": "Arts",
            "grade": "7",
        })
        self.assertEqual(code, 404)

    def test_missing_course_or_grade(self):
        submission_id = self._new_submission()

        code, _ = call(save_course_grade, {"submission_id": submission_id, "grade": "7"})
        self.assertEqual(code, 400)

        code, _ = call(save_course_grade, {"submission_id": submission_id, "course": "Arts"})
        self.assertEqual(code, 400)

    def test_unknown_course_returns_allowed_values(self):
        """The error should tell the bot builder what IS valid."""
        submission_id = self._new_submission()
        code, response = call(save_course_grade, {
            "submission_id": submission_id,
            "course": "Underwater Basket Weaving",
            "grade": "7",
        })

        self.assertEqual(code, 400)
        self.assertIn("allowed_values", response)

    def test_invalid_grades_are_rejected(self):
        if not self.course:
            self.skipTest("no Course Verticals records on this site")

        submission_id = self._new_submission()
        for bad_grade in ["0", "13", "seven", ""]:
            with self.subTest(grade=bad_grade):
                code, _ = call(save_course_grade, {
                    "submission_id": submission_id,
                    "course": self.course,
                    "grade": bad_grade,
                })
                self.assertEqual(code, 400)

    def test_submission_stays_started_when_details_are_rejected(self):
        """A failed second call must not half-update the record."""
        submission_id = self._new_submission()
        call(save_course_grade, {
            "submission_id": submission_id,
            "course": "Not A Course",
            "grade": "7",
        })

        doc = frappe.get_doc("Teacher Submission", submission_id)
        self.assertEqual(doc.status, "Started")
        self.assertFalse(doc.course)
