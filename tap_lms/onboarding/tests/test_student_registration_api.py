import unittest
from time import sleep, time
from urllib.parse import urlsplit, urlunsplit

import requests


url = "https://tap-lms.theapprenticeproject.org/api/method/tap_lms.onboarding.student_registration"
Authorization = "token "

url = "http://tap_lms.localhost:8000/api/method/tap_lms.onboarding.student_registration"
Authorization = "token "

SCHOOL_ID = "Test-SC00001"
PHONE_NUMBER = "918978936605"
STUDENT_NAME = "API Test Student"
GENDER = "Female"
GRADE = "8"
LANGUAGE = "Hindi"
COURSE_NAME = "Coding"
TIMEOUT = 5
CALL_DELAY_SECONDS = 5
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1

METHOD_URLS = {
    "verify_school_by_id": "tap_lms.onboarding.student_registration.verify_school_by_id",
    "create_student_web": "tap_lms.onboarding.student_registration.create_student_web",
    "student_whatsapp_response": "tap_lms.onboarding.student_registration.student_whatsapp_response",
    "set_student_course_level": "tap_lms.onboarding.student_registration.set_student_course_level",
}

TRANSIENT_ERROR_SNIPPETS = (
    "could not serialize access due to concurrent update",
    "current transaction is aborted",
)


class TestStudentRegistrationAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_url = cls._build_root_url()
        cls.last_call_at = None
        cls.headers = {
            "Authorization": Authorization,
        }
        cls._validate_config()
        cls.create_status_code = None
        cls.create_response = None
        cls.create_response_text = ""

    @classmethod
    def _build_root_url(cls):
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @classmethod
    def _validate_config(cls):
        missing = []
        if not url.strip():
            missing.append("url")
        if not Authorization.strip() or Authorization == "••••••":
            missing.append("Authorization")
        if not SCHOOL_ID.strip():
            missing.append("SCHOOL_ID")
        if not PHONE_NUMBER.strip():
            missing.append("PHONE_NUMBER")
        if missing:
            raise unittest.SkipTest(
                f"Set these test configuration values first: {', '.join(missing)}"
            )

    @classmethod
    def _endpoint(cls, method_name):
        return f"{cls.root_url}/api/method/{METHOD_URLS[method_name]}"

    @classmethod
    def _request(cls, method_name, payload=None):
        if cls.last_call_at is not None:
            elapsed = time() - cls.last_call_at
            if elapsed < CALL_DELAY_SECONDS:
                sleep(CALL_DELAY_SECONDS - elapsed)

        response = requests.post(
            cls._endpoint(method_name),
            data=payload or {},
            headers=cls.headers,
            timeout=TIMEOUT,
        )
        cls.last_call_at = time()
        data = cls._parse_json(response)
        print(
            f"\n[{method_name}] status={response.status_code}\n"
            f"payload={payload or {}}\n"
            f"body={response.text}\n",
            flush=True,
        )
        return response, data

    @staticmethod
    def _parse_json(response):
        try:
            data = response.json()
        except ValueError as exc:
            raise AssertionError(
                f"Expected JSON response. status={response.status_code} body={response.text}"
            ) from exc

        if isinstance(data, dict) and isinstance(data.get("message"), dict):
            return data["message"]
        return data

    @staticmethod
    def _is_transient_failure(response, data):
        if response.status_code != 500:
            return False

        haystacks = [response.text]
        if isinstance(data, dict):
            message = data.get("message")
            if isinstance(message, str):
                haystacks.append(message)
        combined = "\n".join(str(item) for item in haystacks)
        return any(snippet in combined for snippet in TRANSIENT_ERROR_SNIPPETS)

    @classmethod
    def _request_with_retry(cls, method_name, payload=None):
        last_response = None
        last_data = None
        for attempt in range(1, MAX_RETRIES + 1):
            response, data = cls._request(method_name, payload)
            last_response = response
            last_data = data
            if not cls._is_transient_failure(response, data):
                return response, data
            if attempt < MAX_RETRIES:
                sleep(RETRY_DELAY_SECONDS)
        return last_response, last_data

    @classmethod
    def _create_student(cls):
        response, data = cls._request_with_retry(
            "create_student_web",
            {
                "school_id": SCHOOL_ID,
                "student_name": STUDENT_NAME,
                "phone": PHONE_NUMBER,
                "gender": GENDER,
                "grade": GRADE,
                "language": LANGUAGE,
            },
        )
        cls.create_status_code = response.status_code
        cls.create_response = data
        cls.create_response_text = response.text
        return response, data

    def assert_success(self, response, data):
        self.assertEqual(response.status_code, 200, data)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("status"), "success", data)

    def test_01_verify_school_by_id(self):
        response, data = self._request_with_retry(
            "verify_school_by_id",
            {
                "school_id": SCHOOL_ID,
                "phone_number": PHONE_NUMBER,
            },
        )

        self.assert_success(response, data)
        self.assertEqual(data["school_id"], SCHOOL_ID)
        self.assertIn("school_name", data)
        self.assertIn("state", data)
        self.assertIn("district", data)
        self.assertIn("city", data)
        self.assertIn("/student/", data["student_registration_url"])

    def test_02_create_student_web(self):
        response, data = self._create_student()

        self.assertEqual(
            response.status_code,
            200,
            f"create_student_web failed. body={self.create_response_text}",
        )
        self.assertEqual(data["status"], "success", data)
        self.assertIn(data["message"], {
            "Student registered successfully.",
            "Student enrollment added successfully.",
        })
        self.assertEqual(data["student_name"], STUDENT_NAME)
        self.assertEqual(data["phone"], PHONE_NUMBER)
        self.assertEqual(data["gender"], GENDER)
        self.assertEqual(str(data["grade"]), GRADE)
        self.assertEqual(data["language"], LANGUAGE)
        self.assertIn("school_name", data)

    def test_03_student_whatsapp_response(self):
        if self.create_status_code != 200:
            self.skipTest(
                f"Skipping because create_student_web failed: {self.create_response_text}"
            )

        response, data = self._request_with_retry(
            "student_whatsapp_response",
            {
                "phone_number": PHONE_NUMBER,
            },
        )

        self.assertEqual(response.status_code, 200, data)
        self.assertIsInstance(data, dict)
        self.assertIn("courses_num", data)
        self.assertIsInstance(data["courses_num"], int)
        self.assertIn("batch_id", data)
        for index in range(1, data["courses_num"] + 1):
            self.assertIn(f"course{index}", data)

    def test_04_set_student_course_level(self):
        if self.create_status_code != 200:
            self.skipTest(
                f"Skipping because create_student_web failed: {self.create_response_text}"
            )

        response, data = self._request_with_retry(
            "set_student_course_level",
            {
                "phone_number": PHONE_NUMBER,
                "course_name": COURSE_NAME,
            },
        )

        self.assert_success(response, data)
        self.assertEqual(data["phone"], PHONE_NUMBER)
        self.assertEqual(data["course_name"], COURSE_NAME)
        self.assertEqual(str(data["grade"]), GRADE)
        self.assertIn("level", data)
        self.assertIn("course_vertical", data)


if __name__ == "__main__":
    unittest.main()
