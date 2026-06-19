"""
CR-2026-06-19 M2 + M3 — authorization + concurrency guard on process_batch.

M2: process_batch is whitelisted and enqueues a 2-hour long-queue job over an
    entire onboarding set.  It must require the TAP Admin role and validate the
    set exists before doing anything.

M3: the pre-fix concurrency guards (is_rq_job_running / is_batch_job_running)
    were never wired to any caller (and is_batch_job_running crashed on
    get_jobs()'s list-of-strings).  process_batch must refuse to (re-)trigger a
    set whose job is already queued/running — the L-073 re-run path.  The new
    _onboarding_job_is_active reads the RQ Job doctype and filters to ACTIVE
    statuses (a finished/failed RQ Job row must NOT block a legitimate retry).

Pattern: patch bop.frappe wholesale (matches the rest of the onboarding suite).
"""
import unittest
from unittest.mock import patch, MagicMock

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


class _ThrowErr(Exception):
    """Stand-in for frappe.throw's ValidationError under a mocked frappe."""


def _row(name, **attrs):
    m = MagicMock()
    m.name = name
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class TestProcessBatchAuth(unittest.TestCase):
    """M2 — authorization + existence validation."""

    def test_permission_required_blocks_non_admin(self):
        """frappe.only_for('TAP Admin') gating: when it raises, process_batch
        must propagate and never enqueue or flip status."""
        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "_", lambda s: s):
            mock_frappe.only_for = MagicMock(side_effect=PermissionError("not TAP Admin"))
            mock_frappe.enqueue = MagicMock()
            mock_frappe.get_doc = MagicMock()

            with self.assertRaises(PermissionError):
                bop.process_batch("SET-001", use_background_job=True)

            mock_frappe.enqueue.assert_not_called()
            mock_frappe.get_doc.assert_not_called()

    def test_unknown_set_rejected_before_enqueue(self):
        """A non-existent set must be rejected (frappe.throw) before any work."""
        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "_", lambda s: s):
            mock_frappe.only_for = MagicMock()
            mock_frappe.db = MagicMock()
            mock_frappe.db.exists = MagicMock(return_value=False)
            mock_frappe.throw = MagicMock(side_effect=_ThrowErr)
            mock_frappe.enqueue = MagicMock()

            with self.assertRaises(_ThrowErr):
                bop.process_batch("SET-MISSING", use_background_job=True)

            mock_frappe.enqueue.assert_not_called()

    def test_happy_path_enqueues_long_queue_job(self):
        """TAP Admin, existing set, no in-flight job → enqueue once on 'long'."""
        batch_doc = MagicMock(status="Draft", save=MagicMock())
        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "_", lambda s: s), \
             patch.object(bop, "_onboarding_job_is_active", return_value=False):
            mock_frappe.only_for = MagicMock()
            mock_frappe.db = MagicMock()
            mock_frappe.db.exists = MagicMock(return_value=True)
            mock_frappe.get_doc = MagicMock(return_value=batch_doc)
            mock_frappe.enqueue = MagicMock(return_value=MagicMock(id="job-123"))

            result = bop.process_batch("SET-001", use_background_job=True)

            self.assertEqual(result, {"job_id": "job-123"})
            self.assertEqual(mock_frappe.enqueue.call_count, 1)
            _, kwargs = mock_frappe.enqueue.call_args
            self.assertEqual(kwargs.get("queue"), "long")
            self.assertEqual(kwargs.get("set_id"), "SET-001")
            self.assertEqual(batch_doc.status, "Processing")
            self.assertTrue(batch_doc.save.called)


class TestConcurrencyGuard(unittest.TestCase):
    """M3 — _onboarding_job_is_active + the process_batch refusal."""

    def test_second_trigger_refused_when_job_active(self):
        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "_", lambda s: s), \
             patch.object(bop, "_onboarding_job_is_active", return_value=True):
            mock_frappe.only_for = MagicMock()
            mock_frappe.db = MagicMock()
            mock_frappe.db.exists = MagicMock(return_value=True)
            mock_frappe.throw = MagicMock(side_effect=_ThrowErr)
            mock_frappe.enqueue = MagicMock()

            with self.assertRaises(_ThrowErr):
                bop.process_batch("SET-001", use_background_job=True)

            mock_frappe.enqueue.assert_not_called()

    def test_guard_true_for_active_status(self):
        with patch.object(bop, "frappe") as mock_frappe:
            mock_frappe.db = MagicMock()
            mock_frappe.db.table_exists = MagicMock(return_value=True)
            mock_frappe.get_all = MagicMock(return_value=[_row("rq1", status="started")])
            self.assertTrue(bop._onboarding_job_is_active("BT00000001"))

    def test_guard_false_for_finished_or_failed_only(self):
        """A finished/failed RQ Job row persists (result_ttl/failure_ttl) and
        must NOT block a legitimate re-trigger."""
        with patch.object(bop, "frappe") as mock_frappe:
            mock_frappe.db = MagicMock()
            mock_frappe.db.table_exists = MagicMock(return_value=True)
            mock_frappe.get_all = MagicMock(return_value=[
                _row("rq1", status="finished"), _row("rq2", status="failed"),
            ])
            self.assertFalse(bop._onboarding_job_is_active("BT00000001"))

    def test_guard_false_when_no_rq_job_doctype(self):
        with patch.object(bop, "frappe") as mock_frappe:
            mock_frappe.db = MagicMock()
            mock_frappe.db.table_exists = MagicMock(return_value=False)
            mock_frappe.get_all = MagicMock()
            self.assertFalse(bop._onboarding_job_is_active("BT00000001"))
            mock_frappe.get_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
