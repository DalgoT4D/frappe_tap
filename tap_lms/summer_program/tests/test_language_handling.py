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

    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_includes_languageId_when_passed(self, mock_session, mock_headers):
        """CR-025: update_contact_fields now routes through _glific_post_with_401_retry
        which calls _GLIFIC_SESSION.post (not bare requests.post). Mock target updated.
        Also mock get_glific_auth_headers to skip the token-expiry check."""
        from tap_lms.glific_integration import update_contact_fields

        mock_headers.return_value = {"authorization": "test-token"}

        # Mock all round-trips: write update + verify fetch
        update_resp = MagicMock(status_code=200, ok=True)
        update_resp.raise_for_status = MagicMock()
        update_resp.json.return_value = {
            "data": {"updateContact": {"contact": {"id": "13325", "fields": "{}"}}}
        }
        verify_resp = MagicMock(status_code=200, ok=True)
        verify_resp.raise_for_status = MagicMock()
        verify_resp.json.return_value = {
            "data": {"contact": {"contact": {
                "id": "13325",
                "name": "X",
                "language": {"id": "5"},
                "fields": json.dumps({"course_level": {"value": "X"}}),
            }}}
        }
        mock_session.post.side_effect = [update_resp, verify_resp]

        with patch("tap_lms.glific_integration.get_glific_settings") as mock_settings:
            mock_settings.return_value.api_url = "https://api.glific.example.com"
            ok = update_contact_fields("13325", {"course_level": "X"}, language_id=5)

        self.assertTrue(ok)

        update_call = mock_session.post.call_args_list[0]
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

    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_omits_languageId_when_not_passed(self, mock_session, mock_headers):
        """Backward-compat: callers that don't pass language_id must not
        accidentally set core language. Verifies languageId key is absent
        from the mutation input.

        CR-025: mock target updated from requests.post to _GLIFIC_SESSION.
        """
        from tap_lms.glific_integration import update_contact_fields

        mock_headers.return_value = {"authorization": "test-token"}

        update_resp = MagicMock(status_code=200, ok=True)
        update_resp.raise_for_status = MagicMock()
        update_resp.json.return_value = {
            "data": {"updateContact": {"contact": {"id": "13325", "fields": "{}"}}}
        }
        verify_resp = MagicMock(status_code=200, ok=True)
        verify_resp.raise_for_status = MagicMock()
        verify_resp.json.return_value = {
            "data": {"contact": {"contact": {
                "id": "13325",
                "name": "X",
                "language": {"id": "1"},
                "fields": json.dumps({"course_level": {"value": "X"}}),
            }}}
        }
        mock_session.post.side_effect = [update_resp, verify_resp]

        with patch("tap_lms.glific_integration.get_glific_settings") as mock_settings:
            mock_settings.return_value.api_url = "https://api.glific.example.com"
            ok = update_contact_fields("13325", {"course_level": "X"})

        self.assertTrue(ok)

        update_call = mock_session.post.call_args_list[0]
        payload = update_call.kwargs.get("json") or update_call.args[1]
        mutation_input = payload["variables"]["input"]
        self.assertNotIn(
            "languageId", mutation_input,
            "updateContact mutation must NOT include languageId when caller "
            "didn't pass language_id (backward compatibility)."
        )

    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_skips_invalid_language_id_gracefully(self, mock_session, mock_headers):
        """A non-integer language_id (e.g. 'not-a-number') should NOT crash —
        log a warning and skip the core-language update, but still process
        the fields update.

        CR-025: mock target updated from requests.post to _GLIFIC_SESSION.
        """
        from tap_lms.glific_integration import update_contact_fields

        mock_headers.return_value = {"authorization": "test-token"}

        update_resp = MagicMock(status_code=200, ok=True)
        update_resp.raise_for_status = MagicMock()
        update_resp.json.return_value = {
            "data": {"updateContact": {"contact": {"id": "13325", "fields": "{}"}}}
        }
        verify_resp = MagicMock(status_code=200, ok=True)
        verify_resp.raise_for_status = MagicMock()
        verify_resp.json.return_value = {
            "data": {"contact": {"contact": {
                "id": "13325",
                "name": "X",
                "language": {"id": "1"},
                "fields": json.dumps({"course_level": {"value": "X"}}),
            }}}
        }
        mock_session.post.side_effect = [update_resp, verify_resp]

        with patch("tap_lms.glific_integration.get_glific_settings") as mock_settings:
            mock_settings.return_value.api_url = "https://api.glific.example.com"
            # Passing a non-integer-coercible value should be tolerated
            ok = update_contact_fields("13325", {"course_level": "X"}, language_id="not-a-number")

        self.assertTrue(ok)

        update_call = mock_session.post.call_args_list[0]
        payload = update_call.kwargs.get("json") or update_call.args[1]
        mutation_input = payload["variables"]["input"]
        # Bad language_id is skipped, not propagated to mutation
        self.assertNotIn("languageId", mutation_input)

    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_fails_when_post_update_fetch_does_not_reflect_write(self, mock_session, mock_headers):
        """Verification must use a fresh post-update fetch, not the mutation body."""
        from tap_lms.glific_integration import update_contact_fields

        mock_headers.return_value = {"authorization": "test-token"}

        update_resp = MagicMock(status_code=200, ok=True)
        update_resp.raise_for_status = MagicMock()
        update_resp.json.return_value = {
            "data": {"updateContact": {"contact": {
                "id": "13325",
                "fields": json.dumps({"course_level": {"value": "X"}}),
            }}}
        }
        verify_resp = MagicMock(status_code=200, ok=True)
        verify_resp.raise_for_status = MagicMock()
        verify_resp.json.return_value = {
            "data": {"contact": {"contact": {
                "id": "13325",
                "name": "X",
                "language": {"id": "1"},
                "fields": "{}",
            }}}
        }
        mock_session.post.side_effect = [update_resp, verify_resp]

        with patch("tap_lms.glific_integration.get_glific_settings") as mock_settings:
            mock_settings.return_value.api_url = "https://api.glific.example.com"
            ok = update_contact_fields("13325", {"course_level": "X"})

        self.assertFalse(ok)

    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    @patch("tap_lms.glific_integration.create_contact")
    def test_creates_new_contact_when_update_target_is_missing(
        self, mock_create_contact, mock_session, mock_headers
    ):
        from tap_lms.glific_integration import update_contact_fields

        mock_headers.return_value = {"authorization": "test-token"}

        missing_update_resp = MagicMock(status_code=200, ok=True)
        missing_update_resp.raise_for_status = MagicMock()
        missing_update_resp.json.return_value = {
            "data": {"updateContact": {"contact": None, "errors": [
                {"key": "contact", "message": "Contact not found"},
            ]}}
        }
        retry_update_resp = MagicMock(status_code=200, ok=True)
        retry_update_resp.raise_for_status = MagicMock()
        retry_update_resp.json.return_value = {
            "data": {"updateContact": {"contact": {"id": "999", "fields": "{}"}, "errors": []}}
        }
        verify_resp = MagicMock(status_code=200, ok=True)
        verify_resp.raise_for_status = MagicMock()
        verify_resp.json.return_value = {
            "data": {"contact": {"contact": {
                "id": "999",
                "name": "X",
                "language": {"id": "5"},
                "fields": json.dumps({"course_level": {"value": "X"}}),
            }}}
        }
        mock_session.post.side_effect = [missing_update_resp, retry_update_resp, verify_resp]
        mock_create_contact.return_value = {"id": "999"}

        with patch("tap_lms.glific_integration.get_glific_settings") as mock_settings:
            mock_settings.return_value.api_url = "https://api.glific.example.com"
            ok = update_contact_fields(
                "13325",
                {"course_level": "X"},
                language_id=5,
                create_contact_input={
                    "name": "X",
                    "phone": "9999999999",
                    "school_name": "School",
                    "model_name": "Model",
                    "batch_id": "B1",
                },
            )

        self.assertTrue(ok)
        mock_create_contact.assert_called_once_with("X", "9999999999", "School", "Model", 5, "B1")
        retry_payload = mock_session.post.call_args_list[1].kwargs.get("json") or mock_session.post.call_args_list[1].args[1]
        self.assertEqual(retry_payload["variables"]["id"], "999")


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
