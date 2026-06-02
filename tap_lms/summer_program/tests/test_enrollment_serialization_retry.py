"""
BR-001 / L-071 — _process_enrollment_chunk SerializationFailure retry.

The 71K prod cohort enrollment (BPR a51af73ec6, 2026-06-01) undercounted and
silently dropped Glific sync work because the per-chunk counter UPDATE shared a
transaction with the per-student `enqueue_after_commit=True` sync enqueues.
Under 4 long workers contending on the shared BatchProgramRun row, Postgres
raised `SerializationFailure`; the rollback took the sync enqueues down with it.

Two fixes are under test:
  Fix 1 — the per-student sync enqueues commit BEFORE the counter UPDATE, so a
          counter conflict can never roll the sync work back.
  Fix 2 — the counter UPDATE runs in its own transaction with a bounded
          serialization-retry loop (100/300/900ms backoff + jitter); on
          exhaustion it log_errors with chunk metadata and re-raises (L-056).

These are pure unit tests — every DB / queue call is mocked, no bench DB needed.
"""
import unittest
from unittest.mock import patch, MagicMock, call

import psycopg2.errors as pg_errors

import tap_lms.summer_program.enrollment as enrollment


_SYNC_JOB = "tap_lms.summer_program.state_machine._sync_contact_fields_job"


class TestCounterRetry(unittest.TestCase):
    """Fix 2 — _update_bpr_counter_with_retry serialization-retry behavior."""

    def test_counter_retry_succeeds_on_transient_serialization_failure(self):
        """Two transient SerializationFailures then success → counter lands,
        no exception propagates, exactly one commit (on the winning attempt)."""
        sql = MagicMock(side_effect=[
            pg_errors.SerializationFailure("could not serialize access due to concurrent update"),
            pg_errors.SerializationFailure("could not serialize access due to concurrent update"),
            None,  # third attempt succeeds
        ])
        with patch.object(enrollment.frappe.db, "sql", sql), \
             patch.object(enrollment.frappe.db, "commit") as commit, \
             patch.object(enrollment.frappe.db, "rollback") as rollback, \
             patch.object(enrollment.frappe, "log_error") as log_error, \
             patch.object(enrollment.time, "sleep") as sleep:

            enrollment._update_bpr_counter_with_retry("BT00000019", 100)

        self.assertEqual(sql.call_count, 3, "should retry until the 3rd attempt succeeds")
        self.assertEqual(commit.call_count, 1, "commit only once, on the successful attempt")
        self.assertEqual(rollback.call_count, 2, "rollback once per failed attempt")
        self.assertEqual(sleep.call_count, 2, "backoff slept before each of the 2 retries")
        log_error.assert_not_called()

    def test_counter_retry_exhaustion_raises(self):
        """SerializationFailure on every attempt → after 4 tries (initial + 3
        retries) the function log_errors with chunk metadata AND re-raises."""
        sql = MagicMock(side_effect=pg_errors.SerializationFailure(
            "could not serialize access due to concurrent update"
        ))
        with patch.object(enrollment.frappe.db, "sql", sql), \
             patch.object(enrollment.frappe.db, "commit") as commit, \
             patch.object(enrollment.frappe.db, "rollback") as rollback, \
             patch.object(enrollment.frappe, "log_error") as log_error, \
             patch.object(enrollment.time, "sleep"):

            with self.assertRaises(pg_errors.SerializationFailure):
                enrollment._update_bpr_counter_with_retry("BT00000019", 100, max_retries=3)

        self.assertEqual(sql.call_count, 4, "initial attempt + 3 retries = 4 UPDATE attempts")
        self.assertEqual(rollback.call_count, 4, "rollback after every failed attempt")
        commit.assert_not_called()  # never reached a successful commit
        # Logged loudly with chunk metadata before re-raising (L-056).
        log_error.assert_called_once()
        logged = " ".join(str(a) for a in log_error.call_args.args) + \
            " ".join(f"{k}={v}" for k, v in log_error.call_args.kwargs.items())
        self.assertIn("BT00000019", logged, "log must name the contended BPR")
        self.assertIn("100", logged, "log must name the lost increment")

    def test_non_transient_exception_propagates_without_retry(self):
        """A non-serialization error (ValueError) propagates immediately — no
        retries, no log_error swallow, a single rollback."""
        sql = MagicMock(side_effect=ValueError("programming error, not transient"))
        with patch.object(enrollment.frappe.db, "sql", sql), \
             patch.object(enrollment.frappe.db, "commit") as commit, \
             patch.object(enrollment.frappe.db, "rollback") as rollback, \
             patch.object(enrollment.frappe, "log_error") as log_error, \
             patch.object(enrollment.time, "sleep") as sleep:

            with self.assertRaises(ValueError):
                enrollment._update_bpr_counter_with_retry("BT00000019", 100)

        self.assertEqual(sql.call_count, 1, "non-transient error must NOT be retried")
        self.assertEqual(rollback.call_count, 1, "rollback once before propagating")
        sleep.assert_not_called()
        commit.assert_not_called()
        log_error.assert_not_called()


class TestSyncEnqueueSurvivesCounterFailure(unittest.TestCase):
    """Fix 1 — sync enqueues commit before (and survive) a counter failure."""

    def _student(self):
        s = MagicMock()
        s.glific_id = "111222"
        s.archetype = "submitter"
        s.experiment_arm = "arm_a"
        s.course_level = "ReadingFluency-Literacy-C0001"
        return s

    def test_sync_enqueue_survives_counter_failure(self):
        """When the counter UPDATE fails permanently, the per-student sync jobs
        were already enqueued and the pre-counter commit already dispatched
        them. The chunk still raises (so RQ records the counter failure, L-056),
        but the Glific sync work is NOT lost."""
        student_ids = ["ST00000001", "ST00000002", "ST00000003"]

        bpr = MagicMock(name="bpr")
        batch = MagicMock(name="batch")
        batch.batch_id = "summer-2026"

        def _get_doc(doctype, name):
            if doctype == "Student":
                return self._student()
            if doctype == "Batch":
                return batch
            return bpr  # BatchProgramRun

        # Counter UPDATE permanently fails → exhausts retries → raises.
        sql = MagicMock(side_effect=pg_errors.SerializationFailure(
            "could not serialize access due to concurrent update"
        ))

        with patch.object(enrollment.frappe, "get_doc", side_effect=_get_doc), \
             patch.object(enrollment.frappe, "enqueue") as enqueue, \
             patch.object(enrollment.frappe.db, "sql", sql), \
             patch.object(enrollment.frappe.db, "commit") as commit, \
             patch.object(enrollment.frappe.db, "rollback"), \
             patch.object(enrollment.frappe, "log_error"), \
             patch.object(enrollment.time, "sleep"), \
             patch("tap_lms.summer_program.utils.get_student_display_name",
                   return_value="Test Student"):

            with self.assertRaises(pg_errors.SerializationFailure):
                enrollment._process_enrollment_chunk(
                    bpr_name="BT00000019",
                    batch_name="summer-2026",
                    student_ids=student_ids,
                    chunk_index=0,
                )

        # All three sync jobs were enqueued (enqueue_after_commit=True) ...
        sync_calls = [c for c in enqueue.call_args_list if c.args and c.args[0] == _SYNC_JOB]
        self.assertEqual(len(sync_calls), 3, "one Glific sync job enqueued per student")
        for c in sync_calls:
            self.assertTrue(
                c.kwargs.get("enqueue_after_commit"),
                "sync jobs must use enqueue_after_commit=True",
            )

        # ... and the pre-counter commit (Fix 1) dispatched them BEFORE the
        # counter UPDATE was ever attempted. The counter retry never commits
        # (every attempt raises), so exactly one commit — the decoupling flush.
        self.assertEqual(
            commit.call_count, 1,
            "exactly the pre-counter flush commit; the counter never commits on failure",
        )

    def test_sync_enqueue_dispatched_before_counter_on_happy_path(self):
        """Happy path: the decoupling commit (Fix 1) fires AFTER all per-student
        enqueues but BEFORE the counter UPDATE, and completion stamps on the
        first try. Ordering is asserted, not just counts."""
        student_ids = ["ST00000001", "ST00000002", "ST00000003"]
        batch = MagicMock(name="batch")
        batch.batch_id = "summer-2026"

        def _get_doc(doctype, name=None):
            if doctype == "Student":
                return self._student()
            return batch  # Batch

        # Counter UPDATE succeeds (None); completion-stamp UPDATE returns a row.
        sql = MagicMock(side_effect=[None, [["BT00000019"]]])

        manager = MagicMock()
        # now_datetime() internally resolves System Settings via frappe.get_doc;
        # patch it to a fixed value so the global get_doc patch isn't reached.
        with patch.object(enrollment.frappe, "get_doc", side_effect=_get_doc), \
             patch.object(enrollment, "now_datetime", return_value="2026-06-02 12:00:00"), \
             patch.object(enrollment.frappe, "enqueue") as enqueue, \
             patch.object(enrollment.frappe.db, "sql", sql), \
             patch.object(enrollment.frappe.db, "commit") as commit, \
             patch.object(enrollment.frappe.db, "rollback"), \
             patch.object(enrollment.frappe, "log_error"), \
             patch.object(enrollment.time, "sleep"), \
             patch("tap_lms.summer_program.utils.get_student_display_name",
                   return_value="Test Student"):

            manager.attach_mock(enqueue, "enqueue")
            manager.attach_mock(commit, "commit")
            manager.attach_mock(sql, "sql")

            enrollment._process_enrollment_chunk(
                bpr_name="BT00000019",
                batch_name="summer-2026",
                student_ids=student_ids,
                chunk_index=0,
            )

        # Three commits: Fix-1 flush, counter, completion stamp.
        self.assertEqual(commit.call_count, 3)
        self.assertEqual(sql.call_count, 2, "counter UPDATE + completion-stamp UPDATE")

        # Ordering: all 3 enqueues precede the first commit (the flush), and the
        # flush precedes the first sql (the counter UPDATE).
        names = [c[0] for c in manager.mock_calls]
        first_commit = names.index("commit")
        first_sql = names.index("sql")
        enqueue_positions = [i for i, n in enumerate(names) if n == "enqueue"]
        self.assertEqual(len(enqueue_positions), 3)
        self.assertTrue(
            all(p < first_commit for p in enqueue_positions),
            "every sync enqueue must be registered before the decoupling commit",
        )
        self.assertLess(
            first_commit, first_sql,
            "the decoupling commit (Fix 1) must precede the counter UPDATE",
        )


if __name__ == "__main__":
    unittest.main()
