"""
Tests for summer_program.vocallabs_webhook — CR-030 / ADR-007 inbound receiver.

Covers the security boundary (auth gate) and the Phase 1 parse → map → record
logic. DB-touching calls (_find_pe, frappe.get_doc) are mocked so the suite is
fast and needs no fixtures. Per L-017, no frappe.db.commit().
"""
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program import vocallabs_webhook as W

SECRET = "s3cr3t-token-value"


class _FakeArgs(dict):
    pass  # dict already has .get and .items


class _FakeHeaders(dict):
    pass


def _fake_request(token=None, method="POST", body="{}", headers=None, extra_query=None):
    q = {}
    if token is not None:
        q["token"] = token
    if extra_query:
        q.update(extra_query)
    return SimpleNamespace(
        method=method,
        args=_FakeArgs(q),
        headers=_FakeHeaders(headers or {}),
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
            resp = W.receive()
        self.assertEqual(frappe.local.response.get("http_status_code"), 401)
        log.assert_not_called()

    def test_wrong_token_returns_401(self):
        with patch.object(W, "_webhook_secret", return_value=SECRET), \
             patch.object(frappe, "request", _fake_request(token="wrong")), \
             patch.object(frappe, "log_error") as log:
            resp = W.receive()
        self.assertEqual(frappe.local.response.get("http_status_code"), 401)
        log.assert_not_called()

    def test_request_none_does_not_500(self):
        with patch.object(W, "_webhook_secret", return_value=SECRET), \
             patch.object(frappe, "request", None), \
             patch.object(frappe, "log_error") as log:
            resp = W.receive()  # must not raise
        self.assertEqual(frappe.local.response.get("http_status_code"), 401)
        log.assert_not_called()


# ════════════════════════════════════════════════════════════
# Parsing helpers (pure)
# ════════════════════════════════════════════════════════════

class TestVocallabsWebhookParsing(FrappeTestCase):
    def test_norm_event_variants(self):
        self.assertEqual(W._norm_event("call.ended"), "callended")
        self.assertEqual(W._norm_event("Call_Ended"), "callended")
        self.assertEqual(W._norm_event("data.collected"), "datacollected")
        self.assertEqual(W._norm_event(None), "")

    def test_dig_top_level_and_nested(self):
        self.assertEqual(W._dig({"call_id": "x"}, ("call_id", "callId")), "x")
        self.assertEqual(W._dig({"callId": "y"}, ("call_id", "callId")), "y")
        self.assertEqual(W._dig({"data": {"call_status": "busy"}}, ("call_status",)), "busy")
        self.assertIsNone(W._dig({"a": 1}, ("missing",)))
        self.assertIsNone(W._dig("not-a-dict", ("x",)))

    def test_last10(self):
        self.assertEqual(W._last10("919999999999"), "9999999999")
        self.assertEqual(W._last10("+91 99999-99999"), "9999999999")
        self.assertEqual(W._last10("123"), "123")


# ════════════════════════════════════════════════════════════
# Event processing (DB mocked)
# ════════════════════════════════════════════════════════════

class TestVocallabsWebhookProcessing(FrappeTestCase):
    def test_call_ended_mappable_inserts_outcome(self):
        payload = {"event": "call.ended", "call_id": "real-1",
                   "call_status": "Completed", "phone_to": "919999999999"}
        with patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=False), \
             patch.object(W, "now_datetime", return_value="2026-06-17 12:00:00"), \
             patch.object(frappe, "get_doc", return_value=MagicMock()) as gd:
            W._process_event("call.ended", payload, json.dumps(payload))
        gd.assert_called_once()
        doc = gd.call_args.args[0]
        self.assertEqual(doc["event_type"], "parent_call_outcome")
        self.assertEqual(doc["trigger_source"], "vocallabs_webhook")
        self.assertEqual(doc["enrollment"], "PE-1")
        details = json.loads(doc["details"])
        self.assertEqual(details["real_call_id"], "real-1")
        self.assertEqual(details["call_status"], "completed")  # lowercased

    def test_data_collected_records_action_outcome(self):
        payload = {"event": "data.collected", "call_id": "real-2",
                   "action_outcome": "parent_agreed", "call_summary": "ok",
                   "phone_to": "919999999999"}
        with patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=False), \
             patch.object(W, "now_datetime", return_value="2026-06-17 12:00:00"), \
             patch.object(frappe, "get_doc", return_value=MagicMock()) as gd:
            W._process_event("data.collected", payload, json.dumps(payload))
        details = json.loads(gd.call_args.args[0]["details"])
        self.assertEqual(details["action_outcome"], "parent_agreed")

    def test_unmappable_deadletters_no_insert(self):
        payload = {"event": "call.ended", "call_id": "real-3", "phone_to": "910000000000"}
        with patch.object(W, "_find_pe", return_value=None), \
             patch.object(frappe, "get_doc") as gd, \
             patch.object(frappe, "log_error") as log:
            W._process_event("call.ended", payload, json.dumps(payload))
        gd.assert_not_called()
        self.assertTrue(
            any(c.kwargs.get("title") == W.WEBHOOK_DEADLETTER_LOG_TITLE for c in log.call_args_list)
        )

    def test_call_started_is_ignored(self):
        with patch.object(W, "_find_pe") as fp, patch.object(frappe, "get_doc") as gd:
            W._process_event("call.started", {"event": "call.started"}, "{}")
        fp.assert_not_called()
        gd.assert_not_called()

    def test_idempotent_duplicate_skipped(self):
        payload = {"event": "call.ended", "call_id": "real-4",
                   "call_status": "no-answer", "phone_to": "919999999999"}
        with patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=True), \
             patch.object(frappe, "get_doc") as gd:
            W._process_event("call.ended", payload, json.dumps(payload))
        gd.assert_not_called()

    def test_call_failed_sets_status_fail(self):
        payload = {"event": "call.failed", "call_id": "real-5", "phone_to": "919999999999"}
        with patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=False), \
             patch.object(W, "now_datetime", return_value="2026-06-17 12:00:00"), \
             patch.object(frappe, "get_doc", return_value=MagicMock()) as gd:
            W._process_event("call.failed", payload, json.dumps(payload))
        details = json.loads(gd.call_args.args[0]["details"])
        self.assertEqual(details["call_status"], "fail")

    def test_already_logged_escapes_like_wildcards(self):
        captured = {}

        def fake_sql(*a, **k):
            captured["q"] = a[0] if a else k.get("query")
            captured["params"] = a[1] if len(a) > 1 else k.get("values")
            return []

        with patch.object(frappe.db, "sql", side_effect=fake_sql):
            self.assertFalse(W._already_logged("ab%c_d", "callended"))
        p0 = captured["params"][0]
        self.assertIn("ab\\%c\\_d", p0)          # % and _ escaped for LIKE
        self.assertIn('"real_call_id": "', p0)   # bounded by JSON key, not a bare substring
        self.assertIn("ESCAPE", captured["q"])


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

    def test_valid_call_ended_maps_and_inserts(self):
        body = json.dumps({"event": "call.ended", "call_id": "real-9",
                           "call_status": "completed", "phone_to": "919999999999"})
        with patch.object(W, "_webhook_secret", return_value=SECRET), \
             patch.object(frappe, "request", _fake_request(token=SECRET, body=body)), \
             patch.object(W, "_find_pe", return_value=_fake_pe()), \
             patch.object(W, "_already_logged", return_value=False), \
             patch.object(W, "now_datetime", return_value="2026-06-17 12:00:00"), \
             patch.object(frappe, "get_doc", return_value=MagicMock()) as gd:
            resp = W.receive()
        self.assertTrue(resp.get("ok"))
        gd.assert_called_once()
        self.assertEqual(gd.call_args.args[0]["event_type"], "parent_call_outcome")
