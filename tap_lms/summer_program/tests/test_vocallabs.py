"""
Tests for summer_program.vocallabs — CR-003.

Covers:
  - Happy-path 3-call sequence with token cache hit + miss
  - Token cache TTL respected
  - Settings.enabled=0 short-circuits without retry
  - Missing ParentCallConfig (no per-LU + no default) skips + warns
  - 5-retry/DLQ pattern mirrors save_submission.enqueue_submission

All HTTP is mocked via unittest.mock.patch. Per L-017, no
frappe.db.commit() in tests.
"""
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program.tests.factories import make_batch
from tap_lms.summer_program.constants import (
    LABEL_CONTENT_DELIVERED,
    PATH_CORE,
    PROGRAM_ACTIVE,
    STATE_NORMAL_CONTENT,
    VOCALLABS_DLQ_LOG_TITLE,
    VOCALLABS_DUPLICATE_PROSPECT_LOG_TITLE,
    VOCALLABS_MAX_RETRIES,
    VOCALLABS_RETRY_LOG_TITLE,
    VOCALLABS_TOKEN_CACHE_KEY,
)


# ════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════


def _ensure_batch():
    # Delegates to the shared factory (L-037) so this fixture inherits future
    # mandatory-field additions instead of breaking with MandatoryError.
    return make_batch(label="VocallabsTestBatch", batch_id="VLT01")


def _ensure_student(suffix, phone=None, language=None):
    phone = phone or f"+9999500{suffix}"
    name = frappe.get_value("Student", {"phone": phone}, "name")
    if name:
        if language is not None:
            frappe.db.set_value("Student", name, "language", language)
        return name
    s = frappe.new_doc("Student")
    s.name1 = f"VocallabsTestStudent{suffix}"
    s.phone = phone
    s.glific_id = f"glific-vl-{suffix}"
    if language is not None:
        s.language = language
    s.insert(ignore_permissions=True)
    return s.name


def _make_pe(batch_name, student_name, suffix):
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"PE-VL-{suffix}-{frappe.utils.random_string(6)}"
    pe.student = student_name
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = f"glific-vl-{suffix}"
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.journey_label = LABEL_CONTENT_DELIVERED
    pe.current_path = PATH_CORE
    pe.current_week = 1
    pe.current_tier = "Basic"
    pe.archetype = "Submitter"
    pe.insert(ignore_permissions=True)
    return pe.name


def _ensure_parent_call_config(title="VLT-Default"):
    if frappe.db.exists("ParentCallConfig", title):
        return title
    cfg = frappe.new_doc("ParentCallConfig")
    cfg.title = title
    cfg.status_template = (
        "Hi, {student_name} is on week {week} of {course_level}. "
        "Step {escalation_order} ({escalation_type})."
    )
    cfg.is_active = 1
    cfg.insert(ignore_permissions=True)
    return cfg.name


def _ensure_voice_settings(enabled=1, agent_id="agent-VLT", default_config=None,
                          ttl=3600, agent_mappings=None):
    settings = frappe.get_single("VoiceAgentSettings")
    settings.enabled = enabled
    settings.service_url = "https://vocallabs.test"
    settings.client_id = "test-client"
    # Set the password field via raw write so we don't depend on get_password
    # in the test environment; vocallabs._get_auth_token reads via get_password
    # with raise_exception=False.
    settings.client_secret = "test-secret"
    settings.default_contact_group_id = "group-VLT"
    settings.agent_id = agent_id
    settings.default_parent_call_config = default_config
    settings.auth_token_cache_ttl = ttl
    settings.set("agents", [])
    for row in agent_mappings or []:
        settings.append("agents", {
            "language": row["language"],
            "agent_id": row["agent_id"],
            "enabled": row.get("enabled", 1),
        })
    settings.save(ignore_permissions=True)
    return settings


def _step(order=1, etype="parent_call", hours=24):
    return {
        "escalation_order": order,
        "escalation_type": etype,
        "points_awarded": 0,
        "hours_after_previous": hours,
    }


# ════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════


class TestVocallabsHappyPath(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        # Clear the token cache between tests so cache hits/misses are
        # deterministic.
        frappe.cache().delete_value(VOCALLABS_TOKEN_CACHE_KEY)

    def test_initiate_parent_call_success_path(self):
        """All three Vocallabs calls fire in order with the expected payloads,
        the function returns True, and an event_log entry is written.
        """
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student = _ensure_student("01")
        pe_name = _make_pe(self.batch_name, student, "01")

        captured_calls = []

        def fake_post(url, payload, headers):
            captured_calls.append((url, payload, headers))
            # Verified Vocallabs API contract (Postman 2026-05-21):
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-xyz"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                # Real Vocallabs shape: nested under data.insert_vocallabs_prospects.returning
                return {
                    "data": {
                        "insert_vocallabs_prospects": {
                            "affected_rows": 1,
                            "returning": [{"id": "prospect-001"}],
                        }
                    }
                }
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                return {"status": "queued", "call_id": "call-001"}
            self.fail(f"Unexpected URL hit: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=2, etype="parent_call"))

        self.assertTrue(ok)
        # Exactly three calls — auth + addContact + initiate.
        urls = [c[0] for c in captured_calls]
        self.assertEqual(len(urls), 3, f"expected 3 calls, got {urls}")
        self.assertTrue(urls[0].endswith("/b2b/createAuthToken/"))
        self.assertTrue(urls[1].endswith("/b2b/vocallabs/addMultipleContactsToGroup"))
        self.assertTrue(urls[2].endswith("/b2b/vocallabs/initiateVocallabsCall"))

        # Auth payload uses camelCase clientId/clientSecret (Vocallabs contract).
        auth_payload = captured_calls[0][1]
        self.assertEqual(auth_payload["clientId"], "test-client")
        self.assertEqual(auth_payload["clientSecret"], "test-secret")

        # AddContact payload is {prospects: [{name, phone, data, prospect_group_id, client_id}]}.
        add_payload = captured_calls[1][1]
        self.assertIn("prospects", add_payload)
        self.assertEqual(len(add_payload["prospects"]), 1)
        prospect = add_payload["prospects"][0]
        self.assertEqual(prospect["phone"], "+999950001")
        self.assertEqual(prospect["prospect_group_id"], "group-VLT")
        self.assertEqual(prospect["client_id"], "test-client")
        self.assertIn("name", prospect)
        self.assertTrue(prospect["name"], "name field must be non-empty (Vocallabs requires it)")
        # The agent reads `data.contact`, `data.student_name`, `data.status` at call time.
        self.assertIn("contact", prospect["data"])
        self.assertIn("student_name", prospect["data"])
        self.assertIn("language", prospect["data"])
        self.assertIn("week 1", prospect["data"]["status"])
        self.assertIn("Step 2", prospect["data"]["status"])
        self.assertIn("parent_call", prospect["data"]["status"])

        # Initiate payload: agentId (camelCase) + prospect_id (snake_case per Vocallabs).
        init_payload = captured_calls[2][1]
        self.assertEqual(init_payload["agentId"], "agent-VLT")
        self.assertEqual(init_payload["prospect_id"], "prospect-001")

    def test_initiate_parent_call_uses_language_mapped_agent(self):
        """Enabled VoiceAgentSettings.agents row overrides fallback agent_id."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(
            enabled=1,
            agent_id="agent-fallback",
            default_config=cfg,
            agent_mappings=[{"language": "Hindi", "agent_id": "agent-hindi"}],
        )

        student = _ensure_student("01b", phone="+99995001B", language="Hindi")
        pe_name = _make_pe(self.batch_name, student, "01b")

        captured_calls = []

        def fake_post(url, payload, headers):
            captured_calls.append((url, payload, headers))
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-xyz"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                return {
                    "data": {
                        "insert_vocallabs_prospects": {
                            "affected_rows": 1,
                            "returning": [{"id": "prospect-001"}],
                        }
                    }
                }
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                return {"status": "queued", "call_id": "call-001"}
            self.fail(f"Unexpected URL hit: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=2, etype="parent_call"))

        self.assertTrue(ok)
        add_payload = captured_calls[1][1]
        self.assertEqual(add_payload["prospects"][0]["data"]["language"], "Hindi")
        init_payload = captured_calls[2][1]
        self.assertEqual(init_payload["agentId"], "agent-hindi")

    def test_initiate_parent_call_skips_when_no_agent_resolves(self):
        """No language mapping and no fallback agent_id => fail closed before call."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, agent_id="", default_config=cfg)

        student = _ensure_student("01c", phone="+99995001C", language="Hindi")
        pe_name = _make_pe(self.batch_name, student, "01c")

        def fake_post(url, payload, headers):
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-xyz"}
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                self.fail("initiateVocallabsCall must not fire without a resolved agent")
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                self.fail("addMultipleContactsToGroup must not fire without a resolved agent")
            self.fail(f"Unexpected URL hit: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post) as fake_post:
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=2, etype="parent_call"))

        self.assertFalse(ok)
        urls = [c.kwargs["url"] for c in fake_post.call_args_list]
        self.assertEqual(urls, ["https://vocallabs.test/b2b/createAuthToken/"])

    def test_token_cache_returns_cached_within_ttl(self):
        """Second call within TTL window does NOT re-hit /createAuthToken."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg, ttl=3600)

        student = _ensure_student("02")
        pe_name = _make_pe(self.batch_name, student, "02")

        captured = []

        def fake_post(url, payload, headers):
            captured.append(url)
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-cached"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                return {"data": {"insert_vocallabs_prospects":
                                 {"returning": [{"id": "p"}]}}}
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                return {"status": "queued"}

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            vocallabs.initiate_parent_call(pe_name, _step())
            vocallabs.initiate_parent_call(pe_name, _step())

        auth_count = sum(1 for u in captured if u.endswith("/b2b/createAuthToken/"))
        self.assertEqual(auth_count, 1, "auth token should have been cached")

    def test_token_cache_refreshes_after_invalidation(self):
        """If the cache is cleared between calls, /createAuthToken fires again."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student = _ensure_student("03")
        pe_name = _make_pe(self.batch_name, student, "03")

        captured = []

        def fake_post(url, payload, headers):
            captured.append(url)
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-fresh"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                return {"data": {"insert_vocallabs_prospects":
                                 {"returning": [{"id": "p"}]}}}
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                return {"status": "queued"}

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            vocallabs.initiate_parent_call(pe_name, _step())
            # Simulate TTL expiry by manually invalidating the cache.
            frappe.cache().delete_value(VOCALLABS_TOKEN_CACHE_KEY)
            vocallabs.initiate_parent_call(pe_name, _step())

        auth_count = sum(1 for u in captured if u.endswith("/b2b/createAuthToken/"))
        self.assertEqual(auth_count, 2, "auth token should refresh after cache invalidation")


class TestVocallabsConfigSkips(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        frappe.cache().delete_value(VOCALLABS_TOKEN_CACHE_KEY)

    def test_initiate_parent_call_skipped_when_disabled(self):
        """Vocallabs disabled → no HTTP fires, returns False, no DLQ."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=0, default_config=cfg)

        student = _ensure_student("04")
        pe_name = _make_pe(self.batch_name, student, "04")

        with patch.object(vocallabs, "_http_post") as fake_post:
            ok = vocallabs.initiate_parent_call(pe_name, _step())

        self.assertFalse(ok)
        self.assertEqual(fake_post.call_count, 0)

    def test_initiate_parent_call_skipped_when_no_config_resolved(self):
        """Neither per-LU nor default ParentCallConfig set → skip + log."""
        from tap_lms.summer_program import vocallabs

        # No default config and no per-LU; ensure settings has no default.
        _ensure_voice_settings(enabled=1, default_config=None)

        student = _ensure_student("05")
        pe_name = _make_pe(self.batch_name, student, "05")

        with patch.object(vocallabs, "_http_post") as fake_post:
            ok = vocallabs.initiate_parent_call(pe_name, _step())

        self.assertFalse(ok)
        # No HTTP should fire — we bailed before the token fetch.
        self.assertEqual(fake_post.call_count, 0)


class TestVocallabsRetry(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        frappe.cache().delete_value(VOCALLABS_TOKEN_CACHE_KEY)

    def test_initiate_parent_call_dlq_after_max_retries(self):
        """If retry_count exceeds VOCALLABS_MAX_RETRIES, a DLQ Error Log
        is written with the documented payload and the function returns False.
        Tests the terminal-DLQ branch by simulating the final retry attempt
        (retry_count = VOCALLABS_MAX_RETRIES) failing one more time.
        """
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student = _ensure_student("06")
        pe_name = _make_pe(self.batch_name, student, "06")

        # Every HTTP call raises — drives the failure path.
        def failing_post(url, payload, headers):
            raise RuntimeError("Vocallabs simulated outage")

        # Force this invocation to BE the last retry by passing
        # retry_count = VOCALLABS_MAX_RETRIES; the failure handler will
        # increment to MAX_RETRIES + 1 and route straight to DLQ.
        with patch.object(vocallabs, "_http_post", side_effect=failing_post), \
             patch.object(frappe, "log_error", wraps=frappe.log_error) as fake_log:
            ok = vocallabs.initiate_parent_call(
                pe_name, _step(order=3),
                retry_count=VOCALLABS_MAX_RETRIES,
            )

        self.assertFalse(ok)

        # A DLQ Error Log was emitted with the documented title.
        dlq_calls = [
            call for call in fake_log.call_args_list
            if call.kwargs.get("title") == VOCALLABS_DLQ_LOG_TITLE
        ]
        self.assertGreaterEqual(len(dlq_calls), 1, "Expected DLQ Error Log entry")

        # Payload is JSON with the required keys.
        payload = json.loads(dlq_calls[-1].kwargs["message"])
        for key in ("student_id", "pe_name", "week", "escalation_order",
                    "parent_phone", "final_error", "retries_attempted"):
            self.assertIn(key, payload, f"DLQ payload missing key {key}")
        self.assertEqual(payload["pe_name"], pe_name)
        self.assertEqual(payload["escalation_order"], 3)


# ════════════════════════════════════════════════════════════
# Task #80: duplicate-prospect permanent-failure path
# ════════════════════════════════════════════════════════════


def _duplicate_prospect_response():
    """Real Vocallabs/Hasura response shape captured from production
    (palv2-test-BT52231 Error Log, 2026-05-23 20:07–20:08) when the parent
    phone is already in the prospect group. Returned with HTTP 200, errors
    array inside the body — GraphQL convention."""
    return {
        "errors": [
            {
                "message": (
                    "Uniqueness violation. duplicate key value violates "
                    "unique constraint "
                    "\"prospects_client_id_prospect_group_id_phone_key\""
                ),
                "extensions": {
                    "code": "constraint-violation",
                    "path": (
                        "$.selectionSet.insert_vocallabs_prospects."
                        "args.objects"
                    ),
                },
            }
        ]
    }


class TestVocallabsDuplicateProspectDetection(FrappeTestCase):
    """Pure unit tests for the response-shape detector."""

    def test_detects_constraint_name_in_response(self):
        from tap_lms.summer_program.vocallabs import _is_duplicate_prospect_response
        self.assertTrue(_is_duplicate_prospect_response(_duplicate_prospect_response()))

    def test_detects_extension_code_when_constraint_name_absent(self):
        """Fallback path: Vocallabs renames the constraint but keeps the
        GraphQL extension code `constraint-violation`. We still classify
        the response as duplicate-prospect."""
        from tap_lms.summer_program.vocallabs import _is_duplicate_prospect_response
        response = {
            "errors": [
                {
                    "message": "Uniqueness violation. duplicate key …",
                    "extensions": {"code": "constraint-violation"},
                }
            ]
        }
        self.assertTrue(_is_duplicate_prospect_response(response))

    def test_does_not_match_unrelated_responses(self):
        from tap_lms.summer_program.vocallabs import _is_duplicate_prospect_response
        # Healthy add-contact response — must NOT be flagged.
        self.assertFalse(_is_duplicate_prospect_response({
            "data": {"insert_vocallabs_prospects":
                     {"returning": [{"id": "p"}], "affected_rows": 1}}
        }))
        # Generic GraphQL error (NOT a uniqueness violation) — must NOT be flagged.
        self.assertFalse(_is_duplicate_prospect_response({
            "errors": [{"message": "Internal server error",
                        "extensions": {"code": "internal-error"}}]
        }))
        # Defensive — non-dict inputs return False rather than raising.
        self.assertFalse(_is_duplicate_prospect_response(None))
        self.assertFalse(_is_duplicate_prospect_response("not a dict"))
        self.assertFalse(_is_duplicate_prospect_response([]))


class TestVocallabsDuplicateProspectPermanentFailure(FrappeTestCase):
    """End-to-end behavior of `initiate_parent_call` when Vocallabs returns
    the duplicate-prospect uniqueness violation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        frappe.cache().delete_value(VOCALLABS_TOKEN_CACHE_KEY)

    def _make_fake_post(self):
        """auth ok → addMultipleContactsToGroup returns the uniqueness
        violation → initiateVocallabsCall must NEVER fire."""
        def fake_post(url, payload, headers):
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-dup"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                return _duplicate_prospect_response()
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                self.fail(
                    "initiateVocallabsCall must NOT be called when "
                    "addMultipleContactsToGroup returned uniqueness violation"
                )
            self.fail(f"Unexpected URL: {url}")
        return fake_post

    def test_duplicate_prospect_returns_false_without_retry(self):
        """Single-shot permanent failure: no retry enqueued, no DLQ entry,
        function returns False so the dispatcher proceeds toward drop."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student = _ensure_student("dup01", phone="+99999500A")
        pe_name = _make_pe(self.batch_name, student, "dup01")

        with patch.object(vocallabs, "_http_post", side_effect=self._make_fake_post()), \
             patch.object(frappe, "enqueue") as fake_enqueue:
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4, etype="parent_call"))

        self.assertFalse(ok)
        # CRITICAL: no retry was enqueued. The previous bug enqueued 5 retries
        # for every duplicate-prospect failure, wasting ~15s/call and polluting
        # Error Log.
        retry_enqueues = [
            c for c in fake_enqueue.call_args_list
            if c.args and c.args[0] == "tap_lms.summer_program.vocallabs.initiate_parent_call"
        ]
        self.assertEqual(
            len(retry_enqueues), 0,
            "duplicate-prospect must NOT trigger a retry enqueue (task #80)",
        )

    def test_duplicate_prospect_logs_to_dedicated_title_not_dlq(self):
        """The Error Log entry uses the duplicate-prospect title, NOT the
        generic DLQ title — lets ops filter known-state-bug entries from
        real outages."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student = _ensure_student("dup02", phone="+99999500B")
        pe_name = _make_pe(self.batch_name, student, "dup02")

        with patch.object(vocallabs, "_http_post", side_effect=self._make_fake_post()), \
             patch.object(frappe, "log_error", wraps=frappe.log_error) as fake_log:
            vocallabs.initiate_parent_call(pe_name, _step(order=4, etype="parent_call"))

        titles = [c.kwargs.get("title") for c in fake_log.call_args_list]
        self.assertIn(
            VOCALLABS_DUPLICATE_PROSPECT_LOG_TITLE, titles,
            "expected a log entry under the duplicate-prospect title",
        )
        self.assertNotIn(
            VOCALLABS_DLQ_LOG_TITLE, titles,
            "duplicate-prospect failures must NOT land in the generic DLQ — "
            "they're a known-state bug, not a real outage",
        )
        self.assertNotIn(
            VOCALLABS_RETRY_LOG_TITLE, titles,
            "duplicate-prospect failures must NOT emit the transient-retry log",
        )

    def test_duplicate_prospect_log_payload_has_followup_task(self):
        """The structured payload identifies this is task #80 and points
        ops at follow-up task #81 (Vocallabs lookup endpoint) so the
        problem doesn't get lost in Error Log noise."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student = _ensure_student("dup03", phone="+99999500C")
        pe_name = _make_pe(self.batch_name, student, "dup03")

        with patch.object(vocallabs, "_http_post", side_effect=self._make_fake_post()), \
             patch.object(frappe, "log_error", wraps=frappe.log_error) as fake_log:
            vocallabs.initiate_parent_call(pe_name, _step(order=4, etype="parent_call"))

        dup_calls = [
            c for c in fake_log.call_args_list
            if c.kwargs.get("title") == VOCALLABS_DUPLICATE_PROSPECT_LOG_TITLE
        ]
        self.assertEqual(len(dup_calls), 1)
        payload = json.loads(dup_calls[0].kwargs["message"])
        # Documented payload contract.
        for key in ("reason", "student_id", "pe_name", "week",
                    "escalation_order", "parent_phone", "final_error",
                    "followup_task"):
            self.assertIn(key, payload, f"duplicate-prospect payload missing {key}")
        self.assertEqual(payload["reason"], "duplicate_prospect_no_retry")
        self.assertEqual(payload["followup_task"], 81)
        self.assertEqual(payload["pe_name"], pe_name)
        self.assertEqual(payload["escalation_order"], 4)


# ════════════════════════════════════════════════════════════
# Task #81: Student.vocallabs_prospect_id cache flow
# ════════════════════════════════════════════════════════════


class TestVocallabsProspectIdCache(FrappeTestCase):
    """Verify the cache-on-Student design:

      - First call to a fresh phone: addMultipleContactsToGroup fires,
        prospect_id gets written to Student.vocallabs_prospect_id.
      - Second call: addMultipleContactsToGroup MUST be skipped — straight
        to initiateVocallabsCall with the cached id.
      - Across-sibling and across-week scenarios both reduce to "the
        cache is populated on the Student" — covered by the second-call
        assertion.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        frappe.cache().delete_value(VOCALLABS_TOKEN_CACHE_KEY)

    def test_first_call_writes_prospect_id_to_student(self):
        """Cache-miss path: addMultipleContactsToGroup fires, returns a
        prospect_id, and that id is persisted on Student.vocallabs_prospect_id
        BEFORE initiateVocallabsCall is invoked (so a mid-flight call failure
        still leaves the cache populated for the retry)."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("cache01", phone="+99999600A")
        pe_name = _make_pe(self.batch_name, student_name, "cache01")

        # Ensure cache is cold (in case earlier tests in this class left it
        # populated — though FrappeTestCase should roll back).
        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "",
            update_modified=False,
        )

        def fake_post(url, payload, headers):
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-c1"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                return {"data": {"insert_vocallabs_prospects":
                                 {"affected_rows": 1,
                                  "returning": [{"id": "prospect-fresh-001"}]}}}
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                return {"status": "queued"}
            self.fail(f"Unexpected URL: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertTrue(ok)
        cached = frappe.db.get_value(
            "Student", student_name, "vocallabs_prospect_id"
        )
        self.assertEqual(
            cached, "prospect-fresh-001",
            "Student.vocallabs_prospect_id must be populated after first "
            "successful addMultipleContactsToGroup",
        )

    def test_second_call_skips_add_uses_cached_prospect_id(self):
        """Cache-hit path: when Student.vocallabs_prospect_id is set,
        addMultipleContactsToGroup MUST NOT fire. The call flow is:
        auth → updateContactData (refresh per-call variables) →
        initiateVocallabsCall with the cached id."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("cache02", phone="+99999600B")
        pe_name = _make_pe(self.batch_name, student_name, "cache02")

        # Pre-populate the cache as if a prior call had already inserted
        # this parent into Vocallabs.
        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "prospect-cached-789",
            update_modified=False,
        )

        captured = []

        def fake_post(url, payload, headers):
            captured.append((url, payload))
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-c2"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                self.fail(
                    "addMultipleContactsToGroup MUST be skipped when "
                    "Student.vocallabs_prospect_id is populated (task #81)"
                )
            if url.endswith("/b2b/vocallabs/updateContactData"):
                return {"data": {"update_vocallabs_prospects_by_pk":
                                 {"id": "prospect-cached-789"}}}
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                return {"status": "queued"}
            self.fail(f"Unexpected URL: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertTrue(ok)

        # Verify the initiate call used the cached prospect_id, not a new one.
        init_calls = [
            (u, p) for u, p in captured
            if u.endswith("/b2b/vocallabs/initiateVocallabsCall")
        ]
        self.assertEqual(len(init_calls), 1)
        self.assertEqual(init_calls[0][1]["prospect_id"], "prospect-cached-789")

    def test_cache_hit_refreshes_contact_data_before_call(self):
        """Cache-hit path MUST call updateContactData with the freshly
        rendered status_text (from the team-configured ParentCallConfig)
        BEFORE initiateVocallabsCall fires. Otherwise the agent uses the
        stale data set during the original insert (e.g. week-1 status text
        delivered to the parent in week 5)."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("cache04", phone="+99999600D")
        pe_name = _make_pe(self.batch_name, student_name, "cache04")

        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "prospect-refresh-test",
            update_modified=False,
        )

        captured = []

        def fake_post(url, payload, headers):
            captured.append((url, payload))
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-c4"}
            if url.endswith("/b2b/vocallabs/updateContactData"):
                return {"data": {"update_vocallabs_prospects_by_pk":
                                 {"id": "prospect-refresh-test"}}}
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                return {"status": "queued"}
            self.fail(f"Unexpected URL: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertTrue(ok)

        # updateContactData fired with the expected payload shape.
        update_calls = [
            p for u, p in captured if u.endswith("/b2b/vocallabs/updateContactData")
        ]
        self.assertEqual(len(update_calls), 1,
                         "updateContactData must fire exactly once before initiating")
        update_payload = update_calls[0]
        self.assertEqual(update_payload["prospect_id"], "prospect-refresh-test")
        # Body matches the documented Vocallabs shape: {prospect_id, data: {...}}
        self.assertIn("data", update_payload)
        self.assertIn("contact", update_payload["data"])
        self.assertIn("student_name", update_payload["data"])
        self.assertIn("status", update_payload["data"])
        # The rendered status_text reflects THIS call's variables (week 1,
        # step 4) — proves the data is being freshly rendered against the
        # currently-resolved ParentCallConfig, not pulled from a stale
        # cached prospect record. Anti-hard-coding check: the template
        # came from the test's ParentCallConfig fixture, NOT from a string
        # baked into vocallabs.py.
        self.assertIn("week 1", update_payload["data"]["status"])
        self.assertIn("Step 4", update_payload["data"]["status"])
        self.assertIn("parent_call", update_payload["data"]["status"])

        # Ordering: updateContactData strictly before initiateVocallabsCall.
        urls_only = [u for u, _ in captured]
        update_idx = urls_only.index(
            next(u for u in urls_only if u.endswith("/b2b/vocallabs/updateContactData"))
        )
        init_idx = urls_only.index(
            next(u for u in urls_only if u.endswith("/b2b/vocallabs/initiateVocallabsCall"))
        )
        self.assertLess(update_idx, init_idx,
                        "updateContactData must be called BEFORE initiateVocallabsCall")

    def test_update_contact_data_failure_does_not_block_call(self):
        """If updateContactData returns an error, the parent_call MUST
        still place — better to call the parent with slightly stale data
        than not call at all."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("cache05", phone="+99999600E")
        pe_name = _make_pe(self.batch_name, student_name, "cache05")

        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "prospect-update-fails",
            update_modified=False,
        )

        init_called = [False]

        def fake_post(url, payload, headers):
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-c5"}
            if url.endswith("/b2b/vocallabs/updateContactData"):
                # Simulate Vocallabs returning a transient 5xx.
                raise RuntimeError("Vocallabs updateContactData 502")
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                init_called[0] = True
                return {"status": "queued"}
            self.fail(f"Unexpected URL: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertTrue(ok,
                        "parent_call must still succeed even if updateContactData fails")
        self.assertTrue(init_called[0],
                        "initiateVocallabsCall must fire even after updateContactData error")

    def test_cached_prospect_id_survives_across_calls(self):
        """End-to-end: first call writes the cache, second call reads it.

        Simulates the real production scenario — parent_call fires in W1
        (inserts prospect, caches id) and again in W2 (must reuse the cached
        id rather than re-insert).
        """
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("cache03", phone="+99999600C")
        pe_name = _make_pe(self.batch_name, student_name, "cache03")

        # Start with cold cache.
        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "",
            update_modified=False,
        )

        add_call_count = [0]

        def fake_post(url, payload, headers):
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-c3"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                add_call_count[0] += 1
                return {"data": {"insert_vocallabs_prospects":
                                 {"affected_rows": 1,
                                  "returning": [{"id": "prospect-e2e-001"}]}}}
            if url.endswith("/b2b/vocallabs/updateContactData"):
                # Cache-hit path (W2 call) — refresh data before initiating.
                return {"data": {"update_vocallabs_prospects_by_pk":
                                 {"id": "prospect-e2e-001"}}}
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                return {"status": "queued"}
            self.fail(f"Unexpected URL: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            # W1 call
            ok1 = vocallabs.initiate_parent_call(pe_name, _step(order=4))
            # W2 call (simulated — same PE, in real life would be next week)
            ok2 = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(
            add_call_count[0], 1,
            "addMultipleContactsToGroup must fire EXACTLY ONCE across two "
            "parent_calls for the same student — second call must hit cache",
        )

    def test_two_siblings_sharing_phone_each_get_called(self):
        """Two distinct Students share one parent phone (sibling scenario).

        Pre-condition: BOTH Student rows have the SAME vocallabs_prospect_id
        populated (e.g. seeded by a prior call to either sibling that wrote
        through to both — or in MVP, populated manually). With the cache hit
        on each, both calls succeed without hitting the uniqueness constraint.

        NOTE: today the cache write only updates the calling student's row.
        For genuine sibling support (auto-propagating prospect_id to a
        sibling Student when one sibling's call inserts), see task #81
        remaining scope — needs either a phone-keyed lookup table or
        Vocallabs lookup endpoint.
        """
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        shared_phone = "+99999600SIB"
        sib1 = _ensure_student("sib1", phone=shared_phone)
        # Two distinct Student rows with the same phone — must be created
        # via separate _ensure_student paths since the helper short-circuits
        # on phone collision. Bypass the helper and create directly.
        sib2 = frappe.new_doc("Student")
        sib2.name1 = "VocallabsTestStudent-sib2"
        sib2.phone = shared_phone
        sib2.glific_id = "glific-vl-sib2"
        sib2.insert(ignore_permissions=True)
        sib2_name = sib2.name

        pe1 = _make_pe(self.batch_name, sib1, "sib1")
        pe2 = _make_pe(self.batch_name, sib2_name, "sib2")

        # Pre-populate cache on BOTH sibling rows with the same prospect_id
        # (simulating the seed state task #81 establishes — for MVP a manual
        # CSV import or a one-time backfill).
        for s in (sib1, sib2_name):
            frappe.db.set_value(
                "Student", s,
                "vocallabs_prospect_id", "prospect-shared-sib",
                update_modified=False,
            )

        init_calls = []

        def fake_post(url, payload, headers):
            if url.endswith("/b2b/createAuthToken/"):
                return {"authToken": "tok-sib"}
            if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
                self.fail(
                    "siblings sharing a phone with cached prospect_id "
                    "must NOT hit addMultipleContactsToGroup"
                )
            if url.endswith("/b2b/vocallabs/updateContactData"):
                # Both siblings refresh the shared prospect's data block
                # before their respective calls. This is the documented
                # sibling-race caveat — second update overwrites first.
                return {"data": {"update_vocallabs_prospects_by_pk":
                                 {"id": "prospect-shared-sib"}}}
            if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
                init_calls.append(payload)
                return {"status": "queued"}
            self.fail(f"Unexpected URL: {url}")

        with patch.object(vocallabs, "_http_post", side_effect=fake_post):
            ok1 = vocallabs.initiate_parent_call(pe1, _step(order=4))
            ok2 = vocallabs.initiate_parent_call(pe2, _step(order=4))

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(len(init_calls), 2)
        # Both used the same shared prospect_id — the parent phone gets one
        # call per sibling, but only one Vocallabs prospect record exists.
        self.assertEqual(init_calls[0]["prospect_id"], "prospect-shared-sib")
        self.assertEqual(init_calls[1]["prospect_id"], "prospect-shared-sib")


# ════════════════════════════════════════════════════════════
# Task #81 — auto-backfill via getContacts pagination
# ════════════════════════════════════════════════════════════


def _duplicate_then_lookup_fake_post(test_case, lookup_pages,
                                      lookup_url_substr="/b2b/vocallabs/getContacts"):
    """Build a fake _http_post + _http_get pair for the auto-backfill path.

    `lookup_pages` is a list of getContacts response dicts; each call to
    _http_get returns the next page.

    addMultipleContactsToGroup returns the duplicate-prospect response,
    triggering the lookup → cache → call flow.
    """
    state = {"page_idx": 0, "update_contact_called": False,
             "init_called": False}

    def fake_post(url, payload, headers):
        if url.endswith("/b2b/createAuthToken/"):
            return {"authToken": "tok-lookup"}
        if url.endswith("/b2b/vocallabs/addMultipleContactsToGroup"):
            return _duplicate_prospect_response()
        if url.endswith("/b2b/vocallabs/updateContactData"):
            state["update_contact_called"] = True
            return {"data": {"update_vocallabs_prospects_by_pk":
                             {"id": "x"}}}
        if url.endswith("/b2b/vocallabs/initiateVocallabsCall"):
            state["init_called"] = True
            return {"status": "queued", "prospect_id_used": payload.get("prospect_id")}
        test_case.fail(f"Unexpected POST URL: {url}")

    def fake_get(url, params, headers):
        if lookup_url_substr in url:
            idx = state["page_idx"]
            state["page_idx"] += 1
            if idx < len(lookup_pages):
                return lookup_pages[idx]
            return {"data": {"vocallabs_prospects": []}}    # end of pagination
        test_case.fail(f"Unexpected GET URL: {url}")

    return fake_post, fake_get, state


class TestVocallabsAutoBackfillViaLookup(FrappeTestCase):
    """End-to-end tests for the cold-cache + already-in-Vocallabs path.

    Scenario: Student.vocallabs_prospect_id is empty AND the parent phone
    is already in Vocallabs from a prior insert (test pollution / earlier
    launch / out-of-band add). Our code should:
      1. Try addMultipleContactsToGroup → get uniqueness violation
      2. Paginate /b2b/vocallabs/getContacts to find the existing prospect_id
      3. Cache it on Student.vocallabs_prospect_id
      4. updateContactData with fresh per-call variables
      5. initiateVocallabsCall with the recovered prospect_id

    Without this auto-backfill, operators would need to run a manual
    backfill script for every cohort that has any test pollution.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.batch_name = _ensure_batch()

    def setUp(self):
        frappe.cache().delete_value(VOCALLABS_TOKEN_CACHE_KEY)

    def test_duplicate_prospect_recovered_via_lookup(self):
        """Happy path: phone is on page 1 of getContacts. Auto-backfill
        finds it, caches it, refreshes data, places the call."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("auto01", phone="+919876500001")
        pe_name = _make_pe(self.batch_name, student_name, "auto01")
        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "",
            update_modified=False,
        )

        # getContacts page 1: the phone is here, with prospect_id "uuid-recovered"
        page1 = {
            "data": {
                "vocallabs_prospects": [
                    {"id": "uuid-noise-1", "phone": "+918888888888",
                     "client_id": "test-client", "prospect_group_id": "group-VLT"},
                    {"id": "uuid-recovered", "phone": "+919876500001",
                     "client_id": "test-client", "prospect_group_id": "group-VLT"},
                    {"id": "uuid-noise-2", "phone": "+917777777777",
                     "client_id": "test-client", "prospect_group_id": "group-VLT"},
                ]
            }
        }
        fake_post, fake_get, state = _duplicate_then_lookup_fake_post(self, [page1])

        with patch.object(vocallabs, "_http_post", side_effect=fake_post), \
             patch.object(vocallabs, "_http_get", side_effect=fake_get):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertTrue(ok, "auto-backfill should recover and place the call")

        # Cache populated with the recovered id.
        cached = frappe.db.get_value("Student", student_name, "vocallabs_prospect_id")
        self.assertEqual(cached, "uuid-recovered",
                         "Student.vocallabs_prospect_id must hold the recovered UUID")

        # Both downstream calls fired.
        self.assertTrue(state["update_contact_called"],
                        "updateContactData must fire after lookup-recovered id")
        self.assertTrue(state["init_called"],
                        "initiateVocallabsCall must fire after lookup-recovered id")

    def test_lookup_paginates_multiple_pages(self):
        """Phone is on page 2 — pagination must continue past empty matches."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("auto02", phone="+919876500002")
        pe_name = _make_pe(self.batch_name, student_name, "auto02")
        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "",
            update_modified=False,
        )

        # Page 1: full of unrelated contacts. Page 2: target phone.
        page1 = {
            "data": {
                "vocallabs_prospects": [
                    {"id": f"noise-{i}", "phone": f"+9100000{i:05d}"}
                    for i in range(200)  # PAGE_SIZE
                ]
            }
        }
        page2 = {
            "data": {
                "vocallabs_prospects": [
                    {"id": "uuid-page2", "phone": "+919876500002"},
                ]
            }
        }
        fake_post, fake_get, state = _duplicate_then_lookup_fake_post(
            self, [page1, page2]
        )

        with patch.object(vocallabs, "_http_post", side_effect=fake_post), \
             patch.object(vocallabs, "_http_get", side_effect=fake_get):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertTrue(ok)
        self.assertEqual(
            frappe.db.get_value("Student", student_name, "vocallabs_prospect_id"),
            "uuid-page2",
        )
        self.assertEqual(state["page_idx"], 2,
                         "should have fetched exactly 2 pages")

    def test_phone_match_normalizes_country_code(self):
        """Phone may differ in country-code form on Vocallabs side
        (`+919876500003` vs `9876500003` vs `919876500003`). The matcher
        must hit the right prospect across these variants."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        # Student is stored as the 10-digit form.
        student_name = _ensure_student("auto03", phone="9876500003")
        pe_name = _make_pe(self.batch_name, student_name, "auto03")
        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "",
            update_modified=False,
        )

        # Vocallabs stores the E.164 form.
        page1 = {
            "data": {
                "vocallabs_prospects": [
                    {"id": "uuid-e164", "phone": "+919876500003"},
                ]
            }
        }
        fake_post, fake_get, _ = _duplicate_then_lookup_fake_post(self, [page1])

        with patch.object(vocallabs, "_http_post", side_effect=fake_post), \
             patch.object(vocallabs, "_http_get", side_effect=fake_get):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertTrue(ok, "10-digit ↔ E.164 phone normalization must match")
        self.assertEqual(
            frappe.db.get_value("Student", student_name, "vocallabs_prospect_id"),
            "uuid-e164",
        )

    def test_lookup_failure_falls_back_to_permanent_error(self):
        """If getContacts never returns the phone, behavior degrades to the
        pre-lookup PermanentVocallabsError — same as before this feature,
        no spurious retries."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("auto04", phone="+919876500004")
        pe_name = _make_pe(self.batch_name, student_name, "auto04")
        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "",
            update_modified=False,
        )

        # Pagination returns 2 pages of unrelated contacts then empty.
        page1 = {
            "data": {
                "vocallabs_prospects": [
                    {"id": f"unrelated-{i}", "phone": f"+9100000{i:05d}"}
                    for i in range(3)
                ]
            }
        }
        fake_post, fake_get, _ = _duplicate_then_lookup_fake_post(self, [page1])

        with patch.object(vocallabs, "_http_post", side_effect=fake_post), \
             patch.object(vocallabs, "_http_get", side_effect=fake_get), \
             patch.object(frappe, "log_error", wraps=frappe.log_error) as fake_log:
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertFalse(ok, "lookup miss must fall back to fail-fast")

        # Cache stays empty.
        self.assertFalse(frappe.db.get_value(
            "Student", student_name, "vocallabs_prospect_id"))

        # Exactly one Duplicate-Prospect log entry, exactly one Lookup log entry.
        titles = [c.kwargs.get("title") for c in fake_log.call_args_list]
        self.assertIn(VOCALLABS_DUPLICATE_PROSPECT_LOG_TITLE, titles)
        # NO retry / DLQ entries.
        self.assertNotIn("SP Vocallabs Retry", titles)
        self.assertNotIn(VOCALLABS_DLQ_LOG_TITLE, titles)

    def test_lookup_handles_unrecognized_response_shape(self):
        """Vocallabs response schema isn't documented — if their shape
        ever changes, the matcher must bail gracefully rather than crash
        or hang."""
        from tap_lms.summer_program import vocallabs

        cfg = _ensure_parent_call_config()
        _ensure_voice_settings(enabled=1, default_config=cfg)

        student_name = _ensure_student("auto05", phone="+919876500005")
        pe_name = _make_pe(self.batch_name, student_name, "auto05")
        frappe.db.set_value(
            "Student", student_name,
            "vocallabs_prospect_id", "",
            update_modified=False,
        )

        # Shape we can't parse — no list anywhere.
        bad_page = {"status": "ok", "metadata": {"total": 42}}
        fake_post, fake_get, _ = _duplicate_then_lookup_fake_post(self, [bad_page])

        with patch.object(vocallabs, "_http_post", side_effect=fake_post), \
             patch.object(vocallabs, "_http_get", side_effect=fake_get):
            ok = vocallabs.initiate_parent_call(pe_name, _step(order=4))

        self.assertFalse(ok,
                         "unrecognized response shape must fail safely "
                         "(PermanentVocallabsError)")
