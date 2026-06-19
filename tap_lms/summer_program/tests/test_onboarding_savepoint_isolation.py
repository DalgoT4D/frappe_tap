"""
CR-2026-06-19 H1 + M1 — per-student SAVEPOINT isolation in process_batch_job.

H1 (the bug being fixed):
  The pre-fix per-student `except` called a blanket `frappe.db.rollback()`,
  which reverted EVERY uncommitted success since the last slice-end commit
  (up to 49 students) while their in-memory `success_count` stayed bumped —
  silently deferring real students to a re-run (the L-073/L-065 incident
  trigger).

The fix wraps each student in a Postgres SAVEPOINT so a single failure rolls
back ONLY that student.  These tests assert the behavioural delta with the
established full-mock pattern (a true Postgres "prior rows survive" test is
precluded by process_batch_job's internal frappe.db.commit() calls breaking
FrappeTestCase rollback isolation — L-017 — which is why the whole suite mocks
frappe):

  - each student is wrapped in savepoint() / released on success
  - on failure, frappe.db.rollback(save_point=...) is called — SCOPED, not the
    argless blanket rollback
  - the argless frappe.db.rollback() is NEVER called inside the slice loop
  - success_count / failure_count are accurate after a mid-slice failure
  - the prior + later successes are still marked Success (not reverted)

M1: every Phase-1 failure writes a durable structured frappe.log_error.
"""
import json
import unittest
from unittest.mock import patch, MagicMock

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


def _bs(name, batch_skeyword="coding_1"):
    """A MagicMock behaving like a Backend Students doc with a real .name."""
    m = MagicMock()
    m.name = name
    m.student_name = f"Student {name}"
    m.phone = "919876543210"
    m.course_vertical = "Coding"
    m.grade = "5"
    m.batch = "BT00000001"
    m.batch_skeyword = batch_skeyword
    m.parent = "SET-SP-001"
    m.glific_sync_status = "pending"
    return m


def _row(name, **attrs):
    m = MagicMock()
    m.name = name
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _student_doc(name):
    d = MagicMock()
    d.name = name
    d.name1 = name
    d.glific_id = None
    return d


class TestSavepointIsolation(unittest.TestCase):

    def _run(self, names, fail_name, fail_status_write_for=None):
        """Drive process_batch_job over `names`; process_student_record raises
        for `fail_name`.  If `fail_status_write_for` is set,
        update_backend_student_status raises when marking THAT row Failed
        (the double-fault path).  Returns (result, captured)."""
        bs_by_name = {n: _bs(n) for n in names}
        batch_doc = MagicMock(status="Processing", name="SET-SP-001",
                              processed_student_count=0, save=MagicMock())
        batch_doc.__bool__ = lambda s: True

        def get_doc_side_effect(doctype, name):
            if doctype == "Backend Student Onboarding":
                return batch_doc
            if doctype == "Backend Students":
                return bs_by_name[name]
            return MagicMock()

        get_all_calls = [0]

        def get_all_side_effect(*args, **kwargs):
            get_all_calls[0] += 1
            n = get_all_calls[0]
            if n == 1:                      # Phase-1 student list
                return [_row(nm, batch_skeyword="coding_1") for nm in names]
            if n == 2:                      # batch_onboarding cache
                return [_row("coding_1", batch_skeyword="coding_1", kit_less=False)]
            return [_row(nm) for nm in names]   # Phase-2 pending rows

        def psr_side_effect(student, glific_contact, set_id, initial_stage, course_level=None):
            if student.name == fail_name:
                raise ValueError(f"boom in process_student_record for {student.name}")
            return _student_doc(f"ST-{student.name}")

        ubss_calls = []

        def ubss_side_effect(student, status, student_doc=None, error=None):
            if (fail_status_write_for and student.name == fail_status_write_for
                    and status == "Failed"):
                raise RuntimeError(f"status write failed for {student.name}")
            ubss_calls.append((student.name, status))

        log_error_titles = []

        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "process_glific_contact"), \
             patch.object(bop, "create_or_get_glific_group_for_batch"), \
             patch.object(bop, "process_student_record", side_effect=psr_side_effect), \
             patch.object(bop, "update_backend_student_status", side_effect=ubss_side_effect), \
             patch.object(bop, "update_job_progress"), \
             patch.object(bop, "get_initial_stage", return_value="Stage-0"), \
             patch.object(bop, "get_course_level_with_validation_backend",
                          return_value="CL-CODING-001"):

            mock_frappe.get_doc = MagicMock(side_effect=get_doc_side_effect)
            mock_frappe.get_all = MagicMock(side_effect=get_all_side_effect)
            mock_frappe.db = MagicMock()
            mock_frappe.db.count = MagicMock(return_value=len(names) - (1 if fail_name in names else 0))
            mock_frappe.enqueue = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())

            def _log_error(title=None, message=None):
                log_error_titles.append(title)
            mock_frappe.log_error = MagicMock(side_effect=_log_error)

            result = bop.process_batch_job("SET-SP-001")

        return result, {
            "db": mock_frappe.db,
            "ubss": ubss_calls,
            "log_error_titles": log_error_titles,
        }

    # ── H1 ────────────────────────────────────────────────────────────────
    def test_counts_accurate_after_midslice_failure(self):
        result, _ = self._run(["BS-1", "BS-FAIL", "BS-3"], fail_name="BS-FAIL")
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failure_count"], 1)

    def test_failing_student_rolled_back_scoped_not_blanket(self):
        """The failure path must call rollback(save_point=...) — never the
        argless blanket rollback that wiped prior successes."""
        _, cap = self._run(["BS-1", "BS-FAIL", "BS-3"], fail_name="BS-FAIL")
        rollback = cap["db"].rollback
        scoped = [c for c in rollback.call_args_list if "save_point" in c.kwargs]
        argless = [c for c in rollback.call_args_list if not c.args and not c.kwargs]
        self.assertEqual(len(scoped), 1,
                         "exactly one savepoint-scoped rollback (the failing student)")
        self.assertEqual(len(argless), 0,
                         "the argless blanket rollback must NOT be used in the slice loop")

    def test_savepoint_created_per_student_released_on_success(self):
        _, cap = self._run(["BS-1", "BS-FAIL", "BS-3"], fail_name="BS-FAIL")
        # Count the per-student savepoints (bs_*) distinctly from the
        # status-write savepoints (bsf_*) so the assertion is robust to the
        # failure-path internals.
        created = [c.args[0] for c in cap["db"].savepoint.call_args_list if c.args]
        released = [c.args[0] for c in cap["db"].release_savepoint.call_args_list if c.args]
        student_created = [n for n in created if n.startswith("bs_")]
        student_released = [n for n in released if n.startswith("bs_")]
        # one student savepoint per student; only the 2 successes release theirs.
        self.assertEqual(len(student_created), 3)
        self.assertEqual(len(student_released), 2)

    def test_prior_and_later_successes_marked_success(self):
        """The student BEFORE and AFTER the failure are both marked Success —
        proving they were not reverted by the failure handling."""
        _, cap = self._run(["BS-1", "BS-FAIL", "BS-3"], fail_name="BS-FAIL")
        statuses = dict(cap["ubss"])
        self.assertEqual(statuses.get("BS-1"), "Success")
        self.assertEqual(statuses.get("BS-3"), "Success")
        self.assertEqual(statuses.get("BS-FAIL"), "Failed")

    def test_all_success_when_no_failure(self):
        result, cap = self._run(["BS-1", "BS-2", "BS-3"], fail_name="NONE")
        self.assertEqual(result["success_count"], 3)
        self.assertEqual(result["failure_count"], 0)
        # No failure → no rollback at all.
        self.assertEqual(cap["db"].rollback.call_count, 0)
        self.assertEqual(cap["db"].release_savepoint.call_count, 3)

    # ── M1 ────────────────────────────────────────────────────────────────
    def test_phase1_failure_writes_structured_error_log(self):
        _, cap = self._run(["BS-1", "BS-FAIL", "BS-3"], fail_name="BS-FAIL")
        self.assertTrue(
            any(t and "Phase-1 student failure" in t for t in cap["log_error_titles"]),
            f"expected a durable Phase-1 failure Error Log; got titles={cap['log_error_titles']}"
        )

    # ── Double-fault (status-write also fails) ────────────────────────────
    def test_double_fault_preserves_successes_and_logs_loudly(self):
        """When the Failed-status write ALSO fails, the row's savepoint (sp_fail)
        keeps the txn healthy so prior + later successes survive, the job does
        not abort, and the double-fault is operator-visible — never a blanket
        rollback."""
        result, cap = self._run(["BS-1", "BS-FAIL", "BS-3"],
                                 fail_name="BS-FAIL", fail_status_write_for="BS-FAIL")
        # job completed, counts intact
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failure_count"], 1)
        # prior + later successes preserved despite the double-fault
        statuses = dict(cap["ubss"])
        self.assertEqual(statuses.get("BS-1"), "Success")
        self.assertEqual(statuses.get("BS-3"), "Success")
        # double-fault is durably logged
        self.assertTrue(
            any(t and "double-fault" in t for t in cap["log_error_titles"]),
            f"expected a double-fault Error Log; got titles={cap['log_error_titles']}"
        )
        # still NO blanket rollback — both rollbacks are savepoint-scoped
        rollback = cap["db"].rollback
        argless = [c for c in rollback.call_args_list if not c.args and not c.kwargs]
        scoped = [c for c in rollback.call_args_list if "save_point" in c.kwargs]
        self.assertEqual(len(argless), 0)
        self.assertGreaterEqual(len(scoped), 2,
                                "the student savepoint AND the status-write savepoint both roll back scoped")

    # ── Serialization retry (CR-2026-06-19 §10, background_workers=4) ──────
    def _run_serialization(self, fail_times):
        """Drive process_batch_job for one student whose process_student_record
        raises SerializationFailure `fail_times` times before succeeding."""
        import psycopg2.errors as pg_errors

        bs = _bs("BS-SER")
        batch_doc = MagicMock(status="Processing", name="SET-SP-001",
                              processed_student_count=0, save=MagicMock())
        batch_doc.__bool__ = lambda s: True

        def get_doc_side_effect(doctype, name):
            if doctype == "Backend Student Onboarding":
                return batch_doc
            if doctype == "Backend Students":
                return bs
            return MagicMock()

        get_all_calls = [0]

        def get_all_side_effect(*a, **k):
            get_all_calls[0] += 1
            n = get_all_calls[0]
            if n == 1:
                return [_row("BS-SER", batch_skeyword="coding_1")]
            if n == 2:
                return [_row("coding_1", batch_skeyword="coding_1", kit_less=False)]
            return [_row("BS-SER")]

        psr_calls = [0]

        def psr_side_effect(student, glific_contact, set_id, initial_stage, course_level=None):
            psr_calls[0] += 1
            if psr_calls[0] <= fail_times:
                raise pg_errors.SerializationFailure("simulated tabSeries contention")
            return _student_doc("ST-BS-SER")

        ubss_calls = []

        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "process_glific_contact"), \
             patch.object(bop, "create_or_get_glific_group_for_batch"), \
             patch.object(bop, "process_student_record",
                          side_effect=psr_side_effect) as mock_psr, \
             patch.object(bop, "update_backend_student_status",
                          side_effect=lambda student, status, student_doc=None, error=None:
                              ubss_calls.append((student.name, status))), \
             patch.object(bop, "update_job_progress"), \
             patch.object(bop, "get_initial_stage", return_value="Stage-0"), \
             patch.object(bop, "get_course_level_with_validation_backend",
                          return_value="CL-CODING-001"), \
             patch.object(bop.time, "sleep"):
            mock_frappe.get_doc = MagicMock(side_effect=get_doc_side_effect)
            mock_frappe.get_all = MagicMock(side_effect=get_all_side_effect)
            mock_frappe.db = MagicMock()
            mock_frappe.db.count = MagicMock(return_value=1)
            mock_frappe.enqueue = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())
            mock_frappe.log_error = MagicMock()
            result = bop.process_batch_job("SET-SP-001")

        return result, mock_psr, ubss_calls, mock_frappe.db.rollback

    def test_serialization_failure_retries_then_succeeds(self):
        result, mock_psr, ubss_calls, rollback = self._run_serialization(fail_times=2)
        # Retried twice then succeeded → 3 PSR calls, counted Success.
        self.assertEqual(mock_psr.call_count, 3)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        self.assertIn(("BS-SER", "Success"), ubss_calls)
        # Each retry rolled back to the per-student savepoint (scoped), never blanket.
        scoped = [c for c in rollback.call_args_list if "save_point" in c.kwargs]
        argless = [c for c in rollback.call_args_list if not c.args and not c.kwargs]
        self.assertEqual(len(scoped), 2, "two retries → two savepoint-scoped rollbacks")
        self.assertEqual(len(argless), 0)

    def test_serialization_failure_exhausts_then_marked_failed(self):
        # Never clears → 1 initial + _PHASE1_MAX_SER_RETRIES attempts, then Failed.
        result, mock_psr, ubss_calls, rollback = self._run_serialization(fail_times=999)
        self.assertEqual(mock_psr.call_count, 1 + bop._PHASE1_MAX_SER_RETRIES)
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 1)
        self.assertIn(("BS-SER", "Failed"), ubss_calls)
        argless = [c for c in rollback.call_args_list if not c.args and not c.kwargs]
        self.assertEqual(len(argless), 0, "exhaustion must not trigger a blanket rollback")


if __name__ == "__main__":
    unittest.main()
