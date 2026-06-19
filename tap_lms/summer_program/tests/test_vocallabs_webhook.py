"""
Tests for summer_program.vocallabs_webhook — CR-030 / ADR-007 inbound receiver.

Covers auth (ADR-007) + Phase 1 parse → map → record against the REAL Vocallabs
payload shape `{call_id, queue_id, status}` (no event field, no phone; mapped via
queue_id -> our escalation_sent log). DB-touching calls (_find_pe, get_doc,
_enrollment_by_queue_id) are mocked. Per L-017, no frappe.db.commit().
"""
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program import vocallabs_webhook as W

SECRET = "s3cr3t-token-value"


class _FakeArgs(dict):
    pass


class _FakeHeaders(dict):
    pass


def _fake_request(token=None, method="POST", body="{}", headers=None, extra_query=None):
    q = {}
    if token is not None:
        q["token"] = token
    if extra_query:
        q.update(extra_query)
    return SimpleNamespace(
        method=method, args=_FakeArgs(q), headers=_FakeHeaders(headers or {}),
        get_data=lambda as_text=False: body,
    )


def _fake_pe():
    return frappe._dict({
        "name": "PE-1", "student": "ST-1", "batch": "BT00000019",
        "program_type": "Summer", "current_week": 2,
    })


# ════════════════════════════════════════════════════════════
# Auth gate (ADR-007)
# ════════════════════════════════════════════════════════════

class TestVocallabsWebhookAuth(FrappeTestCase):
    def setUp(self):
        frappe.local.response.pop("http_status_code", None)

    def test_missing_secret_fails_closed(self):
        with patch.object(W, "_webhook_secret", return_value=""), \
             patch.object(frappe, "request", _fake_request(token="anything")), \
             patch.object(frappe, "log_error") as log:
            resp = W.receive()
        self.assertEqual(frappe.local.response.get("http_status_code"), 401)
        self.assertFalse(resp.get("ok"))
        log.assert_not_called()

    def test_missing_token_returns_401(self):
        with patch.object(W, "_webhook_secret", return_value=SECRET), \
             patch.object(frappe, "request", _fake_request(token=None)), \
             patch.object(frappe, "log_error") as log:
            W.receive()
        self.assertEqual(frappe.local.response.get("http_status_code"), 401)
        log.assert_not_called()

    def test_wrong_token_returns_401(self):
        with patch.object(W, "_webhook_secret", return_value=SECRET), \
             patch.object(frappe, "request", _fake_request(token="wrong")), \
             patch.object(frappe, "log_error") as log:
            W.receive()
        self.assertEqual(frappe.local.response.get("http_status_code"), 401)
        log.assert_not_called()

    def test_request_none_does_not_500(self):
        with patch.object(W, "_webhook_secret", return_value=SECRET), \
             patch.object(frappe, "request", None), \
             patch.object(frappe, "log_error") as log:
            W.receive()
        self.assertEqual(frappe.local.response.get("http_status_code"), 401)
        log.assert_not_called()


# ════════════════════════════════════════════════════════════
# Parsing + mapping helpers
# ════════════════════════════════════════════════════════════

class TestVocallabsWebhookHelpers(FrappeTestCase):
    def test_dig_top_level_and_nested(self):
        self.assertEqual(W._dig({"call_id": "x"}, ("call_id", "callId")), "x")
        self.assertEqual(W._dig({"data": {"status": "busy"}}, ("status",)), "busy")
        self.assertIsNone(W._dig({"a": 1}, ("missing",)))
        self.assertIsNone(W._dig("not-a-dict", ("x",)))

    def test_last10(self):
        self.assertEqual(W._last10("919999999999"), "9999999999")
        self.assertEqual(W._last10("+91 99999-99999"), "9999999999")

    def test_find_pe_by_queue_id(self):
        with patch.object(W, "_enrollment_by_queue_id", return_value="PE-1"), \
             patch.object(W, "_pe_by_name", return_value=_fake_pe()):
            pe = W._find_pe("queue-1", None, None)
        self.assertEqual(pe.name, "PE-1")

    def test_find_pe_none_when_unresolvable(self):
        with patch.object(W, "_enrollment_by_queue_id", return_value=None):
            pe = W._find_pe("queue-x", None, None)
        self.assertIsNone(pe)

    def test_already_logged_escapes_like_wildcards(self):
        captured = {}

        def fake_sql(*a, **k):
            captured["q"] = a[0] if a else k.get("query")
            captured["params"] = a[1] if len(a) > 1 else k.get("values")
            return []

        with patch.object(frappe.db, "sql", side_effect=fake_sql):
            self.assertFalse(W._already_logged("ab%c_d"))
        p0 = captured["params"][0]
        self.assertIn("ab\\%c\\_d", p0)          # % and _ escaped for LIKE
        self.assertIn('"real_call_id": "', p0)   # bounded by JSON key, not a bare substring
        self.assertIn("ESCAPE", captured["q"])


# ════════════════════════════════════════════════════════════
# Event processing — real {call_id, queue_id, status} shape (DB mocked)
# ════════════════════════════════════════════════════════════

class TestVocallabsWebhookProcessing(FrappeTestCase):
    def test_outcome_mappable_inserts(self):
        payload = {"call_id": "cc-real-1", "queue_id": "q-1", "status": "Completed"}
        with patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=False), \
             patch.object(W, "now_datetime", return_value="2026-06-19 12:00:00"), \
             patch.object(frappe, "get_doc", return_value=MagicMock()) as gd:
            W._process_event(payload, json.dumps(payload))
        gd.assert_called_once()
        doc = gd.call_args.args[0]
        self.assertEqual(doc["event_type"], "parent_call_outcome")
        self.assertEqual(doc["trigger_source"], "vocallabs_webhook")
        self.assertEqual(doc["enrollment"], "PE-1")
        details = json.loads(doc["details"])
        self.assertEqual(details["real_call_id"], "cc-real-1")
        self.assertEqual(details["queue_id"], "q-1")
        self.assertEqual(details["call_status"], "completed")  # lowercased

    def test_status_passthrough_no_answer(self):
        payload = {"call_id": "cc-2", "queue_id": "q-2", "status": "no-answer"}
        with patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=False), \
             patch.object(W, "now_datetime", return_value="2026-06-19 12:00:00"), \
             patch.object(frappe, "get_doc", return_value=MagicMock()) as gd:
            W._process_event(payload, json.dumps(payload))
        self.assertEqual(json.loads(gd.call_args.args[0]["details"])["call_status"], "no-answer")

    def test_action_outcome_without_status_records(self):
        payload = {"call_id": "cc-3", "queue_id": "q-3", "action_outcome": "parent_agreed"}
        with patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=False), \
             patch.object(W, "now_datetime", return_value="2026-06-19 12:00:00"), \
             patch.object(frappe, "get_doc", return_value=MagicMock()) as gd:
            W._process_event(payload, json.dumps(payload))
        self.assertEqual(json.loads(gd.call_args.args[0]["details"])["action_outcome"], "parent_agreed")

    def test_ping_without_status_or_outcome_skipped(self):
        payload = {"call_id": "cc-4", "queue_id": "q-4"}
        with patch.object(W, "_find_pe") as fp, patch.object(frappe, "get_doc") as gd:
            W._process_event(payload, json.dumps(payload))
        fp.assert_not_called()
        gd.assert_not_called()

    def test_unmappable_deadletters_no_insert(self):
        payload = {"call_id": "cc-5", "queue_id": "q-unknown", "status": "completed"}
        with patch.object(W, "_find_pe", return_value=None), \
             patch.object(frappe, "get_doc") as gd, \
             patch.object(frappe, "log_error") as log:
            W._process_event(payload, json.dumps(payload))
        gd.assert_not_called()
        self.assertTrue(
            any(c.kwargs.get("title") == W.WEBHOOK_DEADLETTER_LOG_TITLE for c in log.call_args_list)
        )

    def test_idempotent_duplicate_skipped(self):
        payload = {"call_id": "cc-6", "queue_id": "q-6", "status": "completed"}
        with patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=True), \
             patch.object(frappe, "get_doc") as gd:
            W._process_event(payload, json.dumps(payload))
        gd.assert_not_called()


# ════════════════════════════════════════════════════════════
# receive() end-to-end (auth + body + mocked map/insert)
# ════════════════════════════════════════════════════════════

class TestVocallabsWebhookReceive(FrappeTestCase):
    def setUp(self):
        frappe.local.response.pop("http_status_code", None)

    def test_valid_unparseable_body_acks_200_and_deadletters(self):
        with patch.object(W, "_webhook_secret", return_value=SECRET), \
             patch.object(frappe, "request", _fake_request(token=SECRET, body="not json")), \
             patch.object(frappe, "log_error") as log:
            resp = W.receive()
        self.assertTrue(resp.get("ok"))
        self.assertNotEqual(frappe.local.response.get("http_status_code"), 401)
        self.assertTrue(
            any(c.kwargs.get("title") == W.WEBHOOK_DEADLETTER_LOG_TITLE for c in log.call_args_list)
        )

    def test_valid_outcome_maps_and_inserts(self):
        body = json.dumps({"call_id": "cc-9", "queue_id": "q-9", "status": "completed"})
        with patch.object(W, "_webhook_secret", return_value=SECRET), \
             patch.object(frappe, "request", _fake_request(token=SECRET, body=body)), \
             patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=False), \
             patch.object(W, "now_datetime", return_value="2026-06-19 12:00:00"), \
             patch.object(frappe, "get_doc", return_value=MagicMock()) as gd:
            resp = W.receive()
        self.assertTrue(resp.get("ok"))
        gd.assert_called_once()
        self.assertEqual(gd.call_args.args[0]["event_type"], "parent_call_outcome")
