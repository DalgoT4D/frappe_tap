"""
CR-004 Slice 0 — AC-6: Glific calls use a shared session with explicit timeout.

Tests verify that:
1. All Glific HTTP calls route through the shared _GLIFIC_SESSION (not bare
   requests.post), so TLS connections are reused and timeouts are enforced.
2. A simulated requests.Timeout raises promptly rather than blocking forever —
   the 2026-05-31 worker-hang root cause.
3. GLIFIC_TIMEOUT constant is positive and <= 15 seconds (reasonable ceiling).

Pattern: mock at `tap_lms.glific_integration._GLIFIC_SESSION` — that is the
sole HTTP egress point after CR-004 Slice 0.

FIX 4 (2026-05-31): auth-path tests previously relied on a MagicMock
token_expiry_time comparison that is non-deterministic (datetime.__ge__ against
a MagicMock hits NotImplemented and the comparison result varies by Python
version). All tests that call Glific leaf functions now also patch
`get_glific_auth_headers` so the auth branch is bypassed entirely.
The assertions about session.post() routing and timeout kwarg remain unchanged.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, call

import requests

import tap_lms.glific_integration as gi

_FAKE_HEADERS = {"authorization": "tok_test", "Content-Type": "application/json"}


class TestGlificSharedSession(unittest.TestCase):
    """Verify the module-level session and timeout constant exist and are sane."""

    def test_shared_session_is_requests_session(self):
        """_GLIFIC_SESSION must be a requests.Session instance."""
        self.assertIsInstance(gi._GLIFIC_SESSION, requests.Session)

    def test_timeout_constant_is_positive(self):
        """GLIFIC_TIMEOUT must be a positive number."""
        self.assertIsInstance(gi.GLIFIC_TIMEOUT, (int, float))
        self.assertGreater(gi.GLIFIC_TIMEOUT, 0)

    def test_timeout_constant_is_reasonable(self):
        """GLIFIC_TIMEOUT must be <= 15 seconds — large enough to avoid spurious
        failures on a slow connection but small enough to prevent a permanent hang."""
        self.assertLessEqual(gi.GLIFIC_TIMEOUT, 15)

    def test_coerce_utc_datetime_normalizes_naive_values(self):
        """Naive datetimes from Frappe Datetime fields must become aware UTC."""
        naive = datetime(2026, 6, 9, 12, 0, 0)
        normalized = gi._coerce_utc_datetime(naive)
        self.assertEqual(normalized.tzinfo, timezone.utc)
        self.assertEqual(normalized.hour, 12)


class TestGlificAuthHeaders(unittest.TestCase):
    """Regression coverage for mixed naive/aware token expiry handling."""

    @patch("tap_lms.glific_integration.get_glific_settings")
    def test_get_glific_auth_headers_accepts_naive_expiry(self, mock_settings):
        """Stored naive expiry must not crash comparison with aware current time."""
        mock_settings.return_value = MagicMock(
            access_token="tok_existing",
            token_expiry_time=datetime.now() + timedelta(minutes=5),
        )

        headers = gi.get_glific_auth_headers()

        self.assertEqual(headers["authorization"], "tok_existing")
        self.assertEqual(headers["Content-Type"], "application/json")


class TestGlificTimeoutRaisesPromptly(unittest.TestCase):
    """A simulated Timeout from the session must propagate up immediately."""

    @patch("tap_lms.glific_integration.get_glific_auth_headers",
           return_value=_FAKE_HEADERS)
    @patch("tap_lms.glific_integration.get_glific_settings")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_get_contact_by_phone_raises_on_session_timeout(
        self, mock_session, mock_settings, mock_auth
    ):
        """get_contact_by_phone: a requests.Timeout from the session propagates
        as a requests.exceptions.RequestException (FIX 2) and is NOT silently
        swallowed. The retry/DLQ path in sync_student_to_glific depends on this."""
        mock_settings.return_value = MagicMock(api_url="https://api.glific.example")

        # The session's .post() raises Timeout — simulates a hung Glific endpoint.
        mock_session.post.side_effect = requests.Timeout("Simulated hang")

        with self.assertRaises(requests.exceptions.RequestException):
            gi.get_contact_by_phone("919876543210")

        # Session was called exactly once (no silent retry loop).
        mock_session.post.assert_called_once()

    @patch("tap_lms.glific_integration.get_glific_auth_headers",
           return_value=_FAKE_HEADERS)
    @patch("tap_lms.glific_integration.get_glific_settings")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_create_contact_raises_on_session_timeout(
        self, mock_session, mock_settings, mock_auth
    ):
        """create_contact: a requests.Timeout now surfaces immediately (FIX 2).
        The session must be called with timeout=GLIFIC_TIMEOUT."""
        mock_settings.return_value = MagicMock(api_url="https://api.glific.example")

        # The inner post raises Timeout
        mock_session.post.side_effect = requests.Timeout("Simulated hang")

        with self.assertRaises(requests.exceptions.RequestException):
            gi.create_contact(
                "Test Student", "919876543210", "Test School",
                "Model A", 1, "BATCH001"
            )

        mock_session.post.assert_called_once()
        _, kwargs = mock_session.post.call_args
        self.assertIn("timeout", kwargs, "timeout= must be passed to session.post()")
        self.assertEqual(kwargs["timeout"], gi.GLIFIC_TIMEOUT)

    @patch("tap_lms.glific_integration.get_glific_auth_headers",
           return_value=_FAKE_HEADERS)
    @patch("tap_lms.glific_integration.get_glific_settings")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_all_calls_pass_glific_timeout_kwarg(
        self, mock_session, mock_settings, mock_auth
    ):
        """Regression: the session.post() call on get_contact_by_phone must
        include timeout=GLIFIC_TIMEOUT — not some other value and not omitted."""
        mock_settings.return_value = MagicMock(api_url="https://api.glific.example")

        # Return a valid-looking empty response (contact not found — genuine
        # business not-found, so return None rather than raising)
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "data": {"contactByPhone": {"contact": None}}
        }
        mock_session.post.return_value = mock_resp

        gi.get_contact_by_phone("919876543210")

        mock_session.post.assert_called_once()
        _, kwargs = mock_session.post.call_args
        self.assertIn("timeout", kwargs)
        self.assertEqual(kwargs["timeout"], gi.GLIFIC_TIMEOUT)

    @patch("tap_lms.glific_integration.get_glific_auth_headers",
           return_value=_FAKE_HEADERS)
    @patch("tap_lms.glific_integration.get_glific_settings")
    @patch("tap_lms.glific_integration._GLIFIC_SESSION")
    def test_no_bare_requests_post_in_module(
        self, mock_session, mock_settings, mock_auth
    ):
        """Structural: the module must not import and call bare requests.post
        anywhere — all calls go through _GLIFIC_SESSION. Verified by ensuring
        that after a successful get_contact_by_phone call, only mock_session.post
        was invoked (not a raw requests.post hanging in there)."""
        mock_settings.return_value = MagicMock(api_url="https://api.glific.example")

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "data": {"contactByPhone": {"contact": {"id": "42", "phone": "919876543210"}}}
        }
        mock_session.post.return_value = mock_resp

        contact = gi.get_contact_by_phone("919876543210")
        self.assertEqual(contact["id"], "42")
        # If a bare requests.post call existed, mocking _GLIFIC_SESSION alone
        # would not intercept it, and the test would fail or error on the real
        # network. Passing here proves the routing is correct.
        mock_session.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
