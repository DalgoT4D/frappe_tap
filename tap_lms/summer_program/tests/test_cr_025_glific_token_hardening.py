"""
CR-025: Glific sync 401-token-rejection hardening — test suite.

Root cause (2026-06-09): when Glific's access token expires mid-session,
every API POST returns HTTP 401.  _sync_contact_fields_job's 6-retry loop
hit the same dead token 6× before DLQing — the token was never refreshed
between retries.

Three-layer fix:
  Layer 1 — _glific_post_with_401_retry: centralised 401 → invalidate →
             re-authenticate → retry-once helper, applied to every API POST.
  Layer 2 — exponential backoff in _sync_contact_fields_job (via
             rq Queue.enqueue_in), with immediate frappe.enqueue fallback.
  Layer 3 — probe_token_health cron (hourly); replay_glific_sync_dlq operator
             tool.

All tests mock _GLIFIC_SESSION, get_queue, and frappe DB primitives.
No real Glific calls. No frappe.db.commit() in tests (L-017).
No setUpClass fixtures needed — all tests run against mocked objects only.
"""

import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

# ── constants imported from canonical location (never hardcode strings) ────
from tap_lms.summer_program.constants import (
    GLIFIC_SYNC_MAX_RETRIES,
    GLIFIC_SYNC_RETRY_LOG_TITLE,
    GLIFIC_SYNC_DLQ_LOG_TITLE,
)

# ── helpers under test ─────────────────────────────────────────────────────
from tap_lms.glific_integration import (
    _glific_post_with_401_retry,
    _invalidate_stored_token,
    probe_token_health,
    get_glific_auth_headers,
)
from tap_lms.summer_program.state_machine import _sync_contact_fields_job

# ── test fixtures ──────────────────────────────────────────────────────────
GLIFIC_ID = "42001"
PE_NAME = "PE-CR025-001"
STUDENT_ID = "ST00099001"
GLIFIC_URL = "https://api.glific.example.com/api"
FIELDS = {
    "resolved_flow_state": "normal_content_delivery",
    "current_week": "1",
    "program_status": "active",
}


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1 tests — _glific_post_with_401_retry helper
# ═══════════════════════════════════════════════════════════════════════════

class TestGlificPostWith401Retry(unittest.TestCase):
    """Tests for the centralised 401-retry helper."""

    # ── 1. 401 → invalidate → retry succeeds ───────────────────────────
    @patch("tap_lms.glific_integration._invalidate_stored_token")
    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_401_invalidates_token_and_retries(self, mock_session, mock_headers, mock_invalidate):
        """A 401 on the first attempt must:
          1. Call _invalidate_stored_token() exactly once.
          2. Re-fetch fresh headers.
          3. Retry the POST once.
          4. Return the 200 response on retry.
        """
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.ok = False

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.ok = True

        mock_session.post.side_effect = [resp_401, resp_200]
        mock_headers.return_value = {"authorization": "new-token", "Content-Type": "application/json"}

        result = _glific_post_with_401_retry(GLIFIC_URL, {"query": "{ }"})

        self.assertEqual(result, resp_200)
        mock_invalidate.assert_called_once()
        self.assertEqual(mock_session.post.call_count, 2,
                         "Must POST exactly twice: once (401) + once (retry)")

    # ── 2. Persistent 401 raises HTTPError ─────────────────────────────
    @patch("tap_lms.glific_integration._invalidate_stored_token")
    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_persistent_401_raises_http_error(self, mock_session, mock_headers, mock_invalidate):
        """If BOTH attempts return 401, raise_for_status() must be called."""
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.ok = False
        import requests
        resp_401.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")

        mock_session.post.return_value = resp_401
        mock_headers.return_value = {"authorization": "stale-token"}

        with self.assertRaises(requests.HTTPError):
            _glific_post_with_401_retry(GLIFIC_URL, {"query": "{ }"})

        # Invalidate fires on EVERY 401, including the terminal one — clearing
        # the confirmed-dead token so the next operation re-authenticates rather
        # than reusing it. With max_attempts=2 and both 401, that's 2 calls.
        self.assertEqual(mock_invalidate.call_count, 2)

    # ── 3. 200 on first attempt — no invalidation ──────────────────────
    @patch("tap_lms.glific_integration._invalidate_stored_token")
    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_200_no_invalidation(self, mock_session, mock_headers, mock_invalidate):
        """A clean 200 must NOT call _invalidate_stored_token."""
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.ok = True
        mock_session.post.return_value = resp_200
        mock_headers.return_value = {"authorization": "good-token"}

        result = _glific_post_with_401_retry(GLIFIC_URL, {"query": "{ }"})

        self.assertEqual(result, resp_200)
        mock_invalidate.assert_not_called()
        mock_session.post.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1 integration — update_contact_fields recovers from 401
# ═══════════════════════════════════════════════════════════════════════════

class TestUpdateContactFieldsRecovery(unittest.TestCase):
    """update_contact_fields must recover from a 401 without requiring its
    caller (_sync_contact_fields_job) to handle the 401 itself."""

    @patch("tap_lms.glific_integration._invalidate_stored_token")
    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_update_contact_fields_recovers_from_401(
        self, mock_session, mock_headers, mock_invalidate
    ):
        """Simulate 401 on the fetch step then 200 on retry. The function
        should return True (success) because _glific_post_with_401_retry
        transparently handles the token refresh."""
        # First call (fetch): 401 → retry with fresh token → 200 with valid data
        fetch_401 = MagicMock(); fetch_401.status_code = 401; fetch_401.ok = False
        fetch_200 = MagicMock(); fetch_200.status_code = 200; fetch_200.ok = True
        fetch_200.json.return_value = {
            "data": {
                "contact": {
                    "contact": {
                        "id": GLIFIC_ID, "name": "Test Student",
                        "fields": json.dumps({}),
                    }
                }
            }
        }
        # Second call pair (update): 200 immediately
        update_200 = MagicMock(); update_200.status_code = 200; update_200.ok = True
        update_200.json.return_value = {
            "data": {
                "updateContact": {
                    "contact": {"id": GLIFIC_ID, "fields": "{}"},
                    "errors": [],
                }
            }
        }
        mock_session.post.side_effect = [fetch_401, fetch_200, update_200]
        mock_headers.return_value = {"authorization": "token"}

        from tap_lms.glific_integration import update_contact_fields, get_glific_settings
        with patch("tap_lms.glific_integration.get_glific_settings") as mock_settings:
            mock_settings.return_value.api_url = "https://api.glific.example.com"
            result = update_contact_fields(GLIFIC_ID, FIELDS)

        self.assertTrue(result)
        mock_invalidate.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1 unit — _invalidate_stored_token
# ═══════════════════════════════════════════════════════════════════════════

class TestInvalidateStoredToken(unittest.TestCase):
    """_invalidate_stored_token must clear token fields and commit."""

    @patch("tap_lms.glific_integration.frappe.db.commit")
    @patch("tap_lms.glific_integration.frappe.db.set_value")
    @patch("tap_lms.glific_integration.get_glific_settings")
    def test_invalidate_clears_token_fields(self, mock_settings, mock_set_value, mock_commit):
        """After invalidation, access_token and token_expiry_time must be None."""
        settings_mock = MagicMock()
        settings_mock.name = "Glific Settings"
        mock_settings.return_value = settings_mock

        _invalidate_stored_token()

        mock_set_value.assert_called_once_with(
            "Glific Settings",
            "Glific Settings",
            {"access_token": None, "token_expiry_time": None},
            update_modified=False,
        )
        mock_commit.assert_called_once()

    @patch("tap_lms.glific_integration.frappe.db.commit")
    @patch("tap_lms.glific_integration.frappe.db.set_value")
    @patch("tap_lms.glific_integration.get_glific_settings")
    def test_get_glific_auth_headers_fetches_fresh_token_after_invalidation(
        self, mock_settings, mock_set_value, mock_commit
    ):
        """After _invalidate_stored_token() clears the token, the next call
        to get_glific_auth_headers() must trigger a fresh /api/v1/session POST
        (because access_token is now None → stale-expiry condition fires)."""
        settings_mock = MagicMock()
        settings_mock.name = "Glific Settings"
        settings_mock.access_token = None  # post-invalidation state
        settings_mock.token_expiry_time = None
        settings_mock.api_url = "https://api.glific.example.com"
        settings_mock.phone_number = "+91999999999"
        settings_mock.password = "testpass"
        mock_settings.return_value = settings_mock

        auth_resp = MagicMock()
        auth_resp.status_code = 200
        auth_resp.json.return_value = {
            "data": {
                "access_token": "fresh-token-abc",
                "renewal_token": "renewal-xyz",
                "token_expiry_time": "2099-01-01T00:00:00Z",
            }
        }

        with patch("tap_lms.glific_integration._GLIFIC_SESSION") as mock_session:
            mock_session.post.return_value = auth_resp
            headers = get_glific_auth_headers()

        self.assertEqual(headers["authorization"], "fresh-token-abc")
        # The auth POST must have gone to /api/v1/session
        call_url = mock_session.post.call_args[0][0]
        self.assertIn("/api/v1/session", call_url)


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2 tests — exponential backoff in _sync_contact_fields_job
# ═══════════════════════════════════════════════════════════════════════════

class TestSyncContactFieldsJobBackoff(unittest.TestCase):
    """_sync_contact_fields_job must use delayed retry (enqueue_in) not
    immediate frappe.enqueue, and the delay must grow exponentially."""

    def _make_queue_mock(self):
        q_mock = MagicMock()
        q_mock.enqueue_in = MagicMock()
        return q_mock

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_backoff_delay_grows_exponentially(self, mock_log_error, mock_update):
        """retry_count=0 → increments to 1 → delay = min(60, 2**1) = 2s.
        We verify that when enqueue_in IS available, it's called with a
        positive timedelta capped at 60s."""
        mock_update.side_effect = Exception("Glific 401")
        q_mock = self._make_queue_mock()

        # Patch get_queue at the module path the code uses via the inline import
        with patch("frappe.utils.background_jobs.get_queue", return_value=q_mock):
            _sync_contact_fields_job(
                GLIFIC_ID, FIELDS, PE_NAME, retry_count=0, student_id=STUDENT_ID
            )

        # enqueue_in must have been called with a positive timedelta <= 60s
        self.assertTrue(
            q_mock.enqueue_in.called,
            "enqueue_in must be called when get_queue succeeds",
        )
        delay_arg = q_mock.enqueue_in.call_args[0][0]
        self.assertIsInstance(delay_arg, timedelta)
        self.assertGreater(delay_arg.total_seconds(), 0)
        self.assertLessEqual(delay_arg.total_seconds(), 60,
                             "delay must be capped at 60s")

    @patch("tap_lms.summer_program.state_machine.update_contact_fields")
    @patch("tap_lms.summer_program.state_machine.frappe.enqueue")
    @patch("tap_lms.summer_program.state_machine.frappe.log_error")
    def test_backoff_falls_back_to_immediate_when_enqueue_in_unavailable(
        self, mock_log_error, mock_enqueue, mock_update
    ):
        """If get_queue().enqueue_in raises (rq-scheduler not running), the
        code must fall back to frappe.enqueue (immediate) rather than DLQ-ing."""
        mock_update.side_effect = Exception("Glific 503")
        mock_enqueue.return_value = None

        q_mock = MagicMock()
        q_mock.enqueue_in.side_effect = Exception("rq-scheduler not available")

        with patch("frappe.utils.background_jobs.get_queue", return_value=q_mock):
            _sync_contact_fields_job(
                GLIFIC_ID, FIELDS, PE_NAME, retry_count=0, student_id=STUDENT_ID
            )

        # Must have fallen back to immediate frappe.enqueue
        mock_enqueue.assert_called_once()
        enqueue_kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(enqueue_kwargs["retry_count"], 1)
        self.assertEqual(enqueue_kwargs["glific_id"], GLIFIC_ID)
        # Must NOT have written a DLQ entry (retry was successfully re-enqueued)
        dlq_calls = [
            c for c in mock_log_error.call_args_list
            if c.kwargs.get("title") == GLIFIC_SYNC_DLQ_LOG_TITLE
        ]
        self.assertEqual(len(dlq_calls), 0,
                         "Fallback to immediate enqueue must not write DLQ")


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3a tests — probe_token_health
# ═══════════════════════════════════════════════════════════════════════════

class TestProbeTokenHealth(unittest.TestCase):
    """probe_token_health must invalidate on 401 and no-op on 200."""

    @patch("tap_lms.glific_integration._invalidate_stored_token")
    @patch("tap_lms.glific_integration.frappe.log_error")
    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    @patch("tap_lms.glific_integration.get_glific_settings")
    def test_probe_invalidates_on_401(
        self, mock_settings, mock_session, mock_headers, mock_log_error, mock_invalidate
    ):
        """probe_token_health must call _invalidate_stored_token and log an
        error when the Glific probe returns 401."""
        mock_settings.return_value.api_url = "https://api.glific.example.com"
        mock_headers.return_value = {"authorization": "stale-token"}
        resp_401 = MagicMock(); resp_401.status_code = 401; resp_401.ok = False
        mock_session.post.return_value = resp_401

        probe_token_health()

        mock_invalidate.assert_called_once()
        mock_log_error.assert_called_once()

    @patch("tap_lms.glific_integration._invalidate_stored_token")
    @patch("tap_lms.glific_integration.frappe.log_error")
    @patch("tap_lms.glific_integration.get_glific_auth_headers")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    @patch("tap_lms.glific_integration.get_glific_settings")
    def test_probe_noop_on_200(
        self, mock_settings, mock_session, mock_headers, mock_log_error, mock_invalidate
    ):
        """probe_token_health must NOT invalidate or log an error on HTTP 200."""
        mock_settings.return_value.api_url = "https://api.glific.example.com"
        mock_headers.return_value = {"authorization": "good-token"}
        resp_200 = MagicMock(); resp_200.status_code = 200; resp_200.ok = True
        mock_session.post.return_value = resp_200

        probe_token_health()

        mock_invalidate.assert_not_called()
        mock_log_error.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3b tests — replay_glific_sync_dlq
# ═══════════════════════════════════════════════════════════════════════════

class TestReplayGlificSyncDlq(unittest.TestCase):
    """replay_glific_sync_dlq must deduplicate by glific_id and use the
    recompute-current approach (reconcile_pe_to_glific)."""

    def _make_dlq_row(self, glific_id, pe_name, student_id=None):
        """Build a mock DLQ tabError Log row as returned by frappe.db.sql."""
        payload = {
            "student_id": student_id or STUDENT_ID,
            "pe_name": pe_name,
            "glific_id": glific_id,
            "fields": FIELDS,
            "final_error": "update_contact_fields returned False",
            "retries_attempted": 6,
        }
        return {"name": f"ERR-{glific_id}", "error_text": json.dumps(payload),
                "creation": "2026-06-09 10:00:00"}

    # ── dry_run count ───────────────────────────────────────────────────
    @patch("tap_lms.summer_program.dev_tools.frappe.db.sql")
    @patch("tap_lms.summer_program.dev_tools.frappe.db.exists")
    def test_dry_run_returns_correct_count(self, mock_exists, mock_sql):
        """dry_run=True must count all unique glific_ids without pushing."""
        mock_sql.return_value = [
            self._make_dlq_row("g1", "PE-001"),
            self._make_dlq_row("g2", "PE-002"),
            self._make_dlq_row("g3", "PE-003"),
        ]
        mock_exists.return_value = True  # all pe_names "exist"

        from tap_lms.summer_program.dev_tools import replay_glific_sync_dlq
        stats = replay_glific_sync_dlq(dry_run=True, verbose=False)

        self.assertEqual(stats["unique"], 3)
        self.assertEqual(stats["replayed"], 3)
        self.assertEqual(stats["failed"], 0)

    # ── deduplicate by glific_id ────────────────────────────────────────
    @patch("tap_lms.summer_program.dev_tools.frappe.db.sql")
    @patch("tap_lms.summer_program.dev_tools.frappe.db.exists")
    def test_deduplicates_by_glific_id(self, mock_exists, mock_sql):
        """Multiple DLQ rows for the same glific_id must result in only ONE
        reconcile call (latest entry wins, earlier duplicates skipped)."""
        mock_sql.return_value = [
            self._make_dlq_row("g1", "PE-001"),  # newest (first = DESC order)
            self._make_dlq_row("g1", "PE-001"),  # duplicate — must be skipped
        ]
        mock_exists.return_value = True

        from tap_lms.summer_program.dev_tools import replay_glific_sync_dlq
        stats = replay_glific_sync_dlq(dry_run=True, verbose=False)

        # 2 DLQ rows but only 1 unique glific_id
        self.assertEqual(stats["total_dlq"], 2)
        self.assertEqual(stats["unique"], 1)
        self.assertEqual(stats["replayed"], 1)

    # ── recompute-current: calls reconcile_pe_to_glific ────────────────
    @patch("tap_lms.summer_program.dev_tools.frappe.log_error")
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    @patch("tap_lms.summer_program.dev_tools.frappe.db.sql")
    @patch("tap_lms.summer_program.dev_tools.frappe.db.exists")
    def test_replay_uses_current_state_not_stored_fields(
        self, mock_exists, mock_sql, mock_reconcile, mock_log_error
    ):
        """Live replay must call reconcile_pe_to_glific (recomputes current
        state) rather than pushing the stored fields dict from the DLQ entry."""
        mock_sql.return_value = [self._make_dlq_row(GLIFIC_ID, PE_NAME)]
        mock_exists.return_value = True
        mock_reconcile.return_value = {"pe": PE_NAME, "diff": [], "pushed": False}

        from tap_lms.summer_program.dev_tools import replay_glific_sync_dlq
        stats = replay_glific_sync_dlq(dry_run=False, verbose=False)

        mock_reconcile.assert_called_once_with(PE_NAME, dry_run=False, verbose=False)
        self.assertEqual(stats["replayed"], 1)
        self.assertEqual(stats["failed"], 0)

    # ── latest state per student (synthetic pe_name) ────────────────────
    @patch("tap_lms.summer_program.dev_tools.reconcile_pe_to_glific")
    @patch("tap_lms.summer_program.dev_tools.frappe.db.get_value")
    @patch("tap_lms.summer_program.dev_tools.frappe.db.sql")
    @patch("tap_lms.summer_program.dev_tools.frappe.db.exists")
    def test_synthetic_pe_name_resolves_current_pe(
        self, mock_exists, mock_sql, mock_get_value, mock_reconcile
    ):
        """When pe_name is 'pre-pe:STU-...' (pre-PE enrollment path), the
        replay tool must look up the student's current PE and reconcile that."""
        synthetic = f"pre-pe:{STUDENT_ID}"
        row = self._make_dlq_row(GLIFIC_ID, synthetic, student_id=STUDENT_ID)
        mock_sql.return_value = [row]
        # pe_name does NOT exist as a ProgramEnrollment
        mock_exists.return_value = False
        # db.get_value returns the student's current PE
        mock_get_value.return_value = PE_NAME
        mock_reconcile.return_value = {"pe": PE_NAME, "diff": [], "pushed": True}

        from tap_lms.summer_program.dev_tools import replay_glific_sync_dlq
        stats = replay_glific_sync_dlq(dry_run=False, verbose=False)

        # Must reconcile against the current PE, not the synthetic name
        mock_reconcile.assert_called_once_with(PE_NAME, dry_run=False, verbose=False)
        self.assertEqual(stats["replayed"], 1)


if __name__ == "__main__":
    unittest.main()
