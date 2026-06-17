"""
Tests for FeedbackConsumer.send_glific_notification — specifically that it
resolves Student.glific_id from the Frappe Student doc name before calling
Glific's start_contact_flow.

Background (2026-05-19 fix): the RabbitMQ feedback-pipeline payload's
`student_id` field carries the Frappe Student doc name (e.g. "ST00051238"),
NOT a Glific contact ID. Earlier code passed that string directly to
Glific's startContactFlow mutation, which rejected it because Glific
addresses contacts by their internal numeric contactByPhone-style ID
(e.g. "13325"). The visible symptom was the generic
"Something unexpected has happened" error in frappe.log.

The fix resolves Student.glific_id and uses THAT as the contact_id.

Tests in this file:
  1. test_resolves_glific_id_from_student
       Asserts start_contact_flow is called with the Glific contact ID,
       not the Frappe Student doc name.
  2. test_skip_when_student_has_no_glific_id
       If the Student row has no glific_id, send_glific_notification must
       skip cleanly without calling start_contact_flow.
  3. test_skip_when_no_overall_feedback
       Negative control — empty feedback means no Glific call.
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch, MagicMock

from tap_lms.feedback_handler.feedback_consumer import FeedbackConsumer


def _ensure_glific_flow():
    """Create the 'feedback' Glific Flow row that send_glific_notification reads."""
    existing = frappe.get_value("Glific Flow", {"label": "feedback"}, "name")
    if existing:
        return existing
    doc = frappe.new_doc("Glific Flow")
    doc.label = "feedback"
    doc.flow_id = "test-feedback-flow-id-001"
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_student(suffix, glific_id):
    """Idempotent: looks up by phone (unique per suffix) and creates if missing.
    Always re-asserts glific_id so cross-test contamination doesn't pollute the
    lookup."""
    phone = f"+9999700{suffix}"
    existing = frappe.get_value("Student", {"phone": phone}, "name")
    if existing:
        frappe.db.set_value("Student", existing, {"glific_id": glific_id})
        return existing
    s = frappe.new_doc("Student")
    s.name1 = f"FCGlificStudent{suffix}"
    s.phone = phone
    s.glific_id = glific_id
    s.archetype = "fence_sitter"
    s.experiment_arm = "arm_a"
    s.language = "English"
    s.insert(ignore_permissions=True)
    return s.name


class TestFeedbackConsumerGlificResolution(FrappeTestCase):
    """send_glific_notification must address the contact by Glific contact ID,
    NOT by Frappe Student doc name."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            _ensure_glific_flow()
        except Exception:
            # If Glific Flow doctype doesn't exist on the test site, fall back
            # to letting the function find None — the no-flow guard skips
            # gracefully. The point of THIS test is the glific_id resolution
            # path, not the flow lookup.
            pass

    @patch("tap_lms.feedback_handler.feedback_consumer.start_contact_flow")
    def test_resolves_glific_id_from_student(self, mock_start_flow):
        """The contact_id passed to start_contact_flow must be the Glific
        contact ID resolved from Student.glific_id, NOT the Frappe Student
        doc name (which is what the message payload's 'student_id' carries)."""
        EXPECTED_GLIFIC_ID = "glific-fcg-001"
        FRAPPE_STUDENT_ID = _ensure_student("01", EXPECTED_GLIFIC_ID)

        mock_start_flow.return_value = True

        # Construct a FeedbackConsumer without invoking __init__ (it would
        # try to set up RabbitMQ, which we don't need for this unit test).
        consumer = FeedbackConsumer.__new__(FeedbackConsumer)

        consumer.send_glific_notification({
            "submission_id": "SUB-FCG-001",
            "student_id": FRAPPE_STUDENT_ID,
            "feedback": {"overall_feedback": "Great work on this submission!"},
        })

        # start_contact_flow must have been called with the Glific contact ID,
        # not the Frappe Student doc name.
        mock_start_flow.assert_called_once()
        kwargs = mock_start_flow.call_args.kwargs
        self.assertEqual(
            kwargs.get("contact_id"), EXPECTED_GLIFIC_ID,
            f"start_contact_flow received contact_id={kwargs.get('contact_id')!r}, "
            f"expected the resolved Glific contact ID {EXPECTED_GLIFIC_ID!r}."
        )
        self.assertNotEqual(
            kwargs.get("contact_id"), FRAPPE_STUDENT_ID,
            "start_contact_flow received the Frappe Student doc name as "
            "contact_id — regression of the 2026-05-19 bug."
        )

    @patch("tap_lms.feedback_handler.feedback_consumer.start_contact_flow")
    def test_skip_when_student_has_no_glific_id(self, mock_start_flow):
        """If the Student row exists but has no glific_id, the function
        must skip cleanly without calling Glific (otherwise Glific would
        get an empty contact_id and fail)."""
        # Create a Student with empty glific_id
        FRAPPE_STUDENT_ID = _ensure_student("02", "")

        consumer = FeedbackConsumer.__new__(FeedbackConsumer)

        consumer.send_glific_notification({
            "submission_id": "SUB-FCG-002",
            "student_id": FRAPPE_STUDENT_ID,
            "feedback": {"overall_feedback": "Some feedback text"},
        })

        mock_start_flow.assert_not_called()

    @patch("tap_lms.feedback_handler.feedback_consumer.start_contact_flow")
    def test_skip_when_no_overall_feedback(self, mock_start_flow):
        """Negative control: empty overall_feedback means no Glific call."""
        FRAPPE_STUDENT_ID = _ensure_student("03", "glific-fcg-003")

        consumer = FeedbackConsumer.__new__(FeedbackConsumer)

        consumer.send_glific_notification({
            "submission_id": "SUB-FCG-003",
            "student_id": FRAPPE_STUDENT_ID,
            "feedback": {"overall_feedback": ""},   # empty
        })

        mock_start_flow.assert_not_called()
