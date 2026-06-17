"""
Tests for the 2026-05-19 language-handling rework:

  1. CF_LANGUAGE → CF_LANGUAGE_ID rename: SP custom field is now the
     Glific INTEGER language ID, not the language name. Avoids name
     collision with Glific's CORE `language` field.
  2. update_contact_fields accepts optional `language_id` and includes
     `languageId` in the updateContact mutation when provided.
  3. update_contact_fields WITHOUT `language_id` does NOT include the
     `languageId` key (preserves backward compatibility for callers that
     never need to touch core language).
  4. update_contact_fields with invalid language_id (non-integer) logs a
     warning and proceeds without setting the core language.
  5. _enqueue_contact_field_sync's recurring-sync payload does NOT push
     language at all (Phase 3a — language is pushed only at enrollment
     time + by backend onboarding).
"""
import frappe
import json
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch, MagicMock

from tap_lms.summer_program.constants import CF_LANGUAGE_ID


def _ensure_tap_language(name, code, glific_id):
    """Idempotent TAP Language row helper."""
    if frappe.db.exists("TAP Language", name):
        frappe.db.set_value("TAP Language", name, {
            "language_code": code,
            "glific_language_id": str(glific_id),
        })
        return name
    doc = frappe.new_doc("TAP Language")
    doc.language_name = name
    doc.language_code = code
    doc.glific_language_id = str(glific_id)
    doc.insert(ignore_permissions=True)
    return doc.name


# ════════════════════════════════════════════════════════════
# 1. Constant value sanity check
# ════════════════════════════════════════════════════════════

class TestLanguageIdConstant(FrappeTestCase):
    def test_cf_language_id_value(self):
        """CF_LANGUAGE_ID must be the string 'language_id' (not 'language').
        Asserts the 2026-05-19 rename hasn't been reverted."""
        self.assertEqual(CF_LANGUAGE_ID, "language_id")


# ════════════════════════════════════════════════════════════
# 2. update_contact_fields with language_id includes languageId in mutation
# ════════════════════════════════════════════════════════════

class TestUpdateContactFieldsLanguage(FrappeTestCase):
    """update_contact_fields must include languageId in the updateContact
    mutation input when callers pass language_id, and must NOT include it
    when callers omit the kwarg (backward compat for the dozens of pre-2026-05-19
    call sites that don't pass language_id)."""

    @patch("tap_lms.glific_integration.requests.post")
    def test_includes_languageId_when_passed(self, mock_post):
        from tap_lms.glific_integration import update_contact_fields

        # Mock both round-trips: fetch contact + write update
        mock_post.side_effect = [
            # Fetch response
            MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "data": {"contact": {"contact": {
                        "id": "13325", "name": "X", "fields": "{}",
                    }}}
                }),
            ),
            # Update response
            MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "data": {"updateContact": {"contact": {"id": "13325", "fields": "{}"}}}
                }),
            ),
        ]

        ok = update_contact_fields("13325", {"course_level": "X"}, language_id=5)
        self.assertTrue(ok)

        # Inspect the second POST (the updateContact mutation)
        update_call = mock_post.call_args_list[1]
        payload = update_call.kwargs.get("json") or update_call.args[1]
        mutation_input = payload["variables"]["input"]
        self.assertIn(
            "languageId", mutation_input,
            "updateContact mutation must include languageId when "
            "language_id kwarg is passed."
        )
        self.assertEqual(
            mutation_input["languageId"], 5,
            "languageId in mutation must equal the int form of the passed value."
        )

    @patch("tap_lms.glific_integration.requests.post")
    def test_omits_languageId_when_not_passed(self, mock_post):
        """Backward-compat: callers that don't pass language_id must not
        accidentally set core language. Verifies languageId key is absent
        from the mutation input."""
        from tap_lms.glific_integration import update_contact_fields

        mock_post.side_effect = [
            MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "data": {"contact": {"contact": {
                        "id": "13325", "name": "X", "fields": "{}",
                    }}}
                }),
            ),
            MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "data": {"updateContact": {"contact": {"id": "13325", "fields": "{}"}}}
                }),
            ),
        ]

        ok = update_contact_fields("13325", {"course_level": "X"})
        self.assertTrue(ok)

        update_call = mock_post.call_args_list[1]
        payload = update_call.kwargs.get("json") or update_call.args[1]
        mutation_input = payload["variables"]["input"]
        self.assertNotIn(
            "languageId", mutation_input,
            "updateContact mutation must NOT include languageId when caller "
            "didn't pass language_id (backward compatibility)."
        )

    @patch("tap_lms.glific_integration.requests.post")
    def test_skips_invalid_language_id_gracefully(self, mock_post):
        """A non-integer language_id (e.g. 'not-a-number') should NOT crash —
        log a warning and skip the core-language update, but still process
        the fields update."""
        from tap_lms.glific_integration import update_contact_fields

        mock_post.side_effect = [
            MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "data": {"contact": {"contact": {
                        "id": "13325", "name": "X", "fields": "{}",
                    }}}
                }),
            ),
            MagicMock(
                status_code=200,
                raise_for_status=MagicMock(),
                json=MagicMock(return_value={
                    "data": {"updateContact": {"contact": {"id": "13325", "fields": "{}"}}}
                }),
            ),
        ]

        # Passing a non-integer-coercible value should be tolerated
        ok = update_contact_fields("13325", {"course_level": "X"}, language_id="not-a-number")
        self.assertTrue(ok)

        update_call = mock_post.call_args_list[1]
        payload = update_call.kwargs.get("json") or update_call.args[1]
        mutation_input = payload["variables"]["input"]
        # Bad language_id is skipped, not propagated to mutation
        self.assertNotIn("languageId", mutation_input)


# ════════════════════════════════════════════════════════════
# 3. Recurring sync does NOT push language (Phase 3a)
# ════════════════════════════════════════════════════════════

class TestRecurringSyncSkipsLanguage(FrappeTestCase):
    """Phase 3a: _enqueue_contact_field_sync builds the 21-field STATE
    payload for every state-machine transition. Language must NOT be in
    this payload — it's set ONCE at PE creation (enrollment-time push)
    and updated by backend onboarding's path, not on every transition."""

    def test_enqueue_contact_field_sync_payload_has_no_language(self):
        from tap_lms.summer_program.state_machine import _enqueue_contact_field_sync

        # Minimal PE-like object — _enqueue_contact_field_sync reads attrs
        # to build the payload then calls frappe.enqueue. We mock the enqueue
        # call and inspect the captured `fields` kwarg.
        pe = MagicMock()
        pe.name = "test-pe-001"
        pe.student = "ST-X"
        pe.glific_id = "13325"
        pe.resolved_flow_state = "normal_content_delivery"
        pe.current_week = 1
        pe.current_path = "Core"
        pe.current_tier = "Basic"
        pe.program_status = "active"
        pe.total_points = 0
        pe.current_streak = 0
        pe.grace_window_end_at = None
        pe.current_expected_submission_type = ""
        pe.current_escalation_step = 0
        pe.submission_count = 0
        pe.total_activity_points = 0
        pe.weekly_activity_points = 0
        pe.total_quiz_points = 0
        pe.weekly_quiz_points = 0
        pe.total_submission_points = 0
        pe.weekly_submission_points = 0
        pe.special_gems = 0
        pe.weekly_submission_done = 0
        pe.current_escalation_type = ""

        with patch("tap_lms.summer_program.state_machine.frappe.enqueue") as mock_enqueue:
            _enqueue_contact_field_sync(pe)

        self.assertEqual(mock_enqueue.call_count, 1)
        fields = mock_enqueue.call_args.kwargs["fields"]

        self.assertNotIn(
            "language", fields,
            "Recurring sync must NOT push the legacy `language` key "
            "(Phase 3a — language is set at enrollment + by backend onboarding)."
        )
        self.assertNotIn(
            "language_id", fields,
            "Recurring sync must NOT push `language_id` either — only "
            "enrollment-time push and backend onboarding update language."
        )
