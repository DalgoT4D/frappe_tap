"""
P4 — Postgres serialization / deadlock errors in _is_transient_glific_error.

Tests verify (L-071):
1. psycopg2.errors.SerializationFailure is classified as transient.
2. psycopg2.errors.DeadlockDetected is classified as transient.
3. A plain Exception with "could not serialize access" in the message is
   classified as transient (Frappe wraps the raw PG error).
4. A plain Exception with "deadlock detected" in the message is classified
   as transient.
5. A plain ValueError (unrelated) is classified as non-transient.
6. The existing transient classes (Timeout, ConnectionError, HTTP 429/5xx)
   are still classified as transient — regression guard.

These are unit tests on the classifier function only (no Frappe DB required).
"""
import unittest
from unittest.mock import MagicMock, patch

import psycopg2.errors as pg_errors
import requests

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


class TestIsTransientGlificErrorSerializationClassifier(unittest.TestCase):
    """Classifier-level tests for L-071 additions to _is_transient_glific_error."""

    # ── Postgres serialization errors ────────────────────────────────────────

    def test_serialization_failure_by_type_is_transient(self):
        """psycopg2.errors.SerializationFailure must be classified transient.

        This is the exact exception type raised by Postgres (and surfaced by
        psycopg2) when two transactions conflict in REPEATABLE READ or
        SERIALIZABLE isolation.  Under 4-worker contention this occurred for
        ~9 Phase-2 sync jobs on 2026-05-31 (L-071).
        """
        exc = pg_errors.SerializationFailure(
            "could not serialize access due to concurrent update"
        )
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "psycopg2.errors.SerializationFailure must be classified transient (L-071)"
        )

    def test_deadlock_detected_by_type_is_transient(self):
        """psycopg2.errors.DeadlockDetected must be classified transient.

        A deadlock is broken by Postgres by aborting one of the conflicting
        transactions; retrying that transaction usually succeeds.
        """
        exc = pg_errors.DeadlockDetected("deadlock detected")
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "psycopg2.errors.DeadlockDetected must be classified transient (L-071)"
        )

    # ── Message-substring fallback ────────────────────────────────────────────

    def test_serialize_access_message_is_transient(self):
        """A generic Exception whose message contains 'could not serialize access'
        must be classified transient.

        Frappe sometimes wraps psycopg2 exceptions in its own DatabaseError or
        a plain Exception, preserving the original message but losing the
        psycopg2 type hierarchy.  The message-substring check is the belt-and-
        braces fallback for that case.
        """
        exc = Exception("could not serialize access due to concurrent update")
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "Message containing 'could not serialize access' must be transient (L-071)"
        )

    def test_serialize_access_message_case_insensitive(self):
        """The substring check must be case-insensitive (the actual Postgres
        message uses lower-case; Frappe may present it differently)."""
        exc = Exception("ERROR: Could Not Serialize Access Due To Concurrent Update")
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "Case-insensitive substring 'could not serialize access' must be transient"
        )

    def test_deadlock_detected_message_is_transient(self):
        """A generic Exception whose message contains 'deadlock detected' must
        be classified transient (belt-and-braces alongside the type check)."""
        exc = Exception("deadlock detected")
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "Message containing 'deadlock detected' must be transient (L-071)"
        )

    def test_deadlock_detected_message_case_insensitive(self):
        """The deadlock substring check must also be case-insensitive."""
        exc = Exception("ERROR: Deadlock Detected")
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "Case-insensitive 'deadlock detected' substring must be transient"
        )

    # ── Non-transient (must NOT classify as transient) ────────────────────────

    def test_plain_value_error_is_not_transient(self):
        """A plain ValueError (e.g., bad input data) must NOT be classified
        as transient — it would never succeed on retry."""
        exc = ValueError("student phone is invalid")
        self.assertFalse(
            bop._is_transient_glific_error(exc),
            "ValueError must NOT be classified as transient"
        )

    def test_unrelated_exception_message_is_not_transient(self):
        """A generic Exception whose message is unrelated to serialization /
        deadlock must NOT be classified as transient."""
        exc = Exception("contact not found in Glific")
        self.assertFalse(
            bop._is_transient_glific_error(exc),
            "Unrelated exception message must NOT be classified as transient"
        )

    def test_key_error_is_not_transient(self):
        """KeyError (from a missing dict key in the Glific response) must
        NOT be transient."""
        exc = KeyError("id")
        self.assertFalse(
            bop._is_transient_glific_error(exc),
            "KeyError must NOT be classified as transient"
        )

    # ── Regression guard: existing transient classes still work ──────────────

    def test_requests_timeout_still_transient(self):
        """requests.Timeout must still be transient — regression guard."""
        exc = _requests_Timeout()
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "requests.Timeout must still be classified transient (regression guard)"
        )

    def test_requests_connection_error_still_transient(self):
        """requests.ConnectionError must still be transient — regression guard."""
        exc = _requests_ConnectionError()
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "requests.ConnectionError must still be classified transient (regression guard)"
        )

    def test_http_429_still_transient(self):
        """HTTP 429 (rate-limit) via requests.HTTPError must still be transient —
        regression guard."""
        exc = _make_http_error(429)
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "HTTP 429 HTTPError must still be classified transient (regression guard)"
        )

    def test_http_500_still_transient(self):
        """HTTP 500 (server error) via requests.HTTPError must still be transient —
        regression guard."""
        exc = _make_http_error(500)
        self.assertTrue(
            bop._is_transient_glific_error(exc),
            "HTTP 500 HTTPError must still be classified transient (regression guard)"
        )

    def test_http_400_still_not_transient(self):
        """HTTP 400 (bad request — client error, non-transient) must NOT be
        classified as transient — regression guard."""
        exc = _make_http_error(400)
        self.assertFalse(
            bop._is_transient_glific_error(exc),
            "HTTP 400 HTTPError must NOT be classified as transient (regression guard)"
        )


# ── Small helpers to build exception instances without importing from requests ──

def _requests_Timeout():
    return requests.Timeout("connection timed out")


def _requests_ConnectionError():
    return requests.ConnectionError("DNS resolution failed")


def _make_http_error(status_code):
    """Build a requests.HTTPError with the given HTTP status code."""
    resp = MagicMock()
    resp.status_code = status_code
    exc = requests.HTTPError(response=resp)
    exc.response = resp
    return exc


if __name__ == "__main__":
    unittest.main()
