"""
CR-026 — tests for the stuck-feedback replay utility.

These exercise the REPLAY ORCHESTRATION (dry-run, guard rails, per-row failure
isolation, idempotency, outcome tallying) in isolation. `on_feedback_ready`
itself is mocked — its correctness is pinned by
summer_program/tests/test_validation_gate.py. Following the consumer tests'
pattern, `frappe` is fully mocked so no real commits leak into the test
transaction.
"""

import unittest
from unittest.mock import patch, MagicMock

from tap_lms.summer_program.recovery import replay_stuck_feedback as R


def _rows(n):
    return [
        {"pe_name": f"PE{i}", "student": f"ST{i}", "submission_name": f"SUB{i}"}
        for i in range(n)
    ]


class _Base(unittest.TestCase):
    def _mock_frappe(self, mf, batch_exists=True):
        mf.db.exists.return_value = batch_exists
        mf.db.rollback.return_value = None
        mf.db.commit.return_value = None
        mf.utils.cint = lambda v: int(v) if v else 0
        mf.utils.sbool = lambda v: str(v).lower() in ("true", "1", "yes")
        return mf


class TestReplayDryRun(_Base):
    def test_dry_run_reports_count_and_never_calls_hook(self):
        hook = MagicMock()
        with patch.object(R, "frappe") as mf, \
                patch.object(R, "_find_stuck", return_value=_rows(3)), \
                patch("tap_lms.summer_program.feedback_consumer_hook.on_feedback_ready", hook):
            self._mock_frappe(mf)
            out = R.replay_stuck_submitted_awaiting_feedback("BATCH-1", dry_run=True)

        self.assertTrue(out["dry_run"])
        self.assertEqual(out["would_replay"], 3)
        self.assertEqual(out["sample"], ["PE0", "PE1", "PE2"])
        hook.assert_not_called()

    def test_dry_run_is_the_default(self):
        hook = MagicMock()
        with patch.object(R, "frappe") as mf, \
                patch.object(R, "_find_stuck", return_value=_rows(1)), \
                patch("tap_lms.summer_program.feedback_consumer_hook.on_feedback_ready", hook):
            self._mock_frappe(mf)
            out = R.replay_stuck_submitted_awaiting_feedback("BATCH-1")

        self.assertTrue(out["dry_run"])
        hook.assert_not_called()


class TestReplayGuards(_Base):
    def test_missing_batch_name_errors(self):
        with patch.object(R, "frappe") as mf:
            self._mock_frappe(mf)
            out = R.replay_stuck_submitted_awaiting_feedback("", dry_run=True)
        self.assertIn("required", out["error"])

    def test_unknown_batch_errors(self):
        with patch.object(R, "frappe") as mf:
            self._mock_frappe(mf, batch_exists=False)
            out = R.replay_stuck_submitted_awaiting_feedback("NOPE", dry_run=True)
        self.assertIn("not found", out["error"])

    def test_real_run_blocked_without_destructive_flag(self):
        hook = MagicMock()
        with patch.object(R, "frappe") as mf, \
                patch.object(R, "_find_stuck", return_value=_rows(5)), \
                patch("tap_lms.summer_program.feedback_consumer_hook.on_feedback_ready", hook):
            self._mock_frappe(mf)
            out = R.replay_stuck_submitted_awaiting_feedback(
                "BATCH-1", dry_run=False, i_know_this_is_destructive=False
            )
        self.assertIn("error", out)
        hook.assert_not_called()


class TestReplayRealRun(_Base):
    def test_transitions_are_tallied_by_branch(self):
        hook = MagicMock(side_effect=[
            {"status": "transitioned", "branch": "feedback_ready", "points_awarded": 25},
            {"status": "transitioned", "branch": "remedial", "points_awarded": 25},
        ])
        with patch.object(R, "frappe") as mf, \
                patch.object(R, "_find_stuck", return_value=_rows(2)), \
                patch("tap_lms.summer_program.feedback_consumer_hook.on_feedback_ready", hook):
            self._mock_frappe(mf)
            out = R.replay_stuck_submitted_awaiting_feedback(
                "BATCH-1", dry_run=False, i_know_this_is_destructive=True
            )

        self.assertEqual(out["processed"], 2)
        self.assertEqual(out["failed"], 0)
        self.assertEqual(out["transitions"], {"feedback_ready": 1, "remedial": 1})
        self.assertEqual(hook.call_count, 2)
        # One commit per successful row.
        self.assertEqual(mf.db.commit.call_count, 2)

    def test_one_failing_row_does_not_abort_the_batch(self):
        def _side(submission_name, student_id=None):
            if submission_name == "SUB1":
                raise RuntimeError("boom")
            return {"status": "transitioned", "branch": "feedback_ready"}

        hook = MagicMock(side_effect=_side)
        with patch.object(R, "frappe") as mf, \
                patch.object(R, "_find_stuck", return_value=_rows(3)), \
                patch("tap_lms.summer_program.feedback_consumer_hook.on_feedback_ready", hook):
            self._mock_frappe(mf)
            out = R.replay_stuck_submitted_awaiting_feedback(
                "BATCH-1", dry_run=False, i_know_this_is_destructive=True
            )

        self.assertEqual(out["processed"], 2)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(hook.call_count, 3)  # all rows attempted
        self.assertEqual(out["failures_sample"][0]["pe"], "PE1")
        # The failing row must roll back before the next row's work so partial
        # state never leaks into a subsequent commit.
        self.assertGreater(mf.db.rollback.call_count, 0)
        # Failure is persisted durably (log_error + commit), so even a dropped
        # console session leaves a record.
        mf.log_error.assert_called()

    def test_already_fixed_rows_count_as_no_pe_idempotent(self):
        # Re-running over already-transitioned PEs: on_feedback_ready returns
        # no_pe and nothing double-processes.
        hook = MagicMock(return_value={"status": "no_pe"})
        with patch.object(R, "frappe") as mf, \
                patch.object(R, "_find_stuck", return_value=_rows(2)), \
                patch("tap_lms.summer_program.feedback_consumer_hook.on_feedback_ready", hook):
            self._mock_frappe(mf)
            out = R.replay_stuck_submitted_awaiting_feedback(
                "BATCH-1", dry_run=False, i_know_this_is_destructive=True
            )

        self.assertEqual(out["processed"], 2)
        self.assertEqual(out["failed"], 0)
        self.assertEqual(out["transitions"], {"no_pe": 2})


if __name__ == "__main__":
    unittest.main()
