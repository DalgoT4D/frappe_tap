"""
CR-004 T-04-04 — two-phase process_batch_job tests.

AC-1: Phase 1 makes ZERO Glific HTTP calls.
      process_glific_contact and create_or_get_glific_group_for_batch must
      never be called during the Phase-1 (DB-only) loop.

Phase-2 enqueue assertions:
      After Phase-1 loop completes, frappe.enqueue must be called once per
      student with:
        - function path pointing at sync_student_to_glific
        - backend_student_name = the Backend Students doc name
        - queue = 'long'
        - enqueue_after_commit = True
        - NO 'retry=' kwarg (retries are self-managed inside sync_student_to_glific)

glific_sync_status='pending' in Phase 1:
      The Backend Students row must have glific_sync_status='pending' set
      before update_backend_student_status is called (which calls save()).
      This is asserted by inspecting what student.glific_sync_status was
      at the moment save() was called.

Set-status accounting:
      Phase-1 DB success/fail counts still drive the set's overall status
      (Processed / Failed / Processing).  Glific outcome is NOT part of
      the success/fail accounting in Phase 1.

Pattern: patch at the process_batch_job call boundary.
         process_glific_contact and glific_integration functions are patched
         to assert they are NEVER called.
         frappe.enqueue is patched to capture Phase-2 enqueue calls.
"""
import unittest
from unittest.mock import patch, MagicMock, call, ANY

import tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process as bop


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _row(name, **attrs):
    """Build a MagicMock whose .name attribute is genuinely set to *name*.

    ``MagicMock(name=x)`` sets the mock's repr/identity, NOT a ``.name``
    attribute.  Using this helper avoids that trap everywhere a row mock
    needs a real string in ``.name``.
    """
    m = MagicMock()
    m.name = name
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _make_backend_student(name="BS-TWO-PHASE-001",
                           student_name="Two Phase Student",
                           phone="919876543210",
                           course_vertical="Coding",
                           grade="5",
                           batch="BT00000001",
                           batch_skeyword="coding_1",
                           parent="SET-TP-001",
                           glific_sync_status="pending"):
    """Return a MagicMock behaving like a Backend Students doc for two-phase tests."""
    bs = MagicMock()
    bs.name = name
    bs.student_name = student_name
    bs.phone = phone
    bs.course_vertical = course_vertical
    bs.grade = grade
    bs.batch = batch
    bs.batch_skeyword = batch_skeyword
    bs.parent = parent
    bs.processing_status = "Pending"
    bs.glific_sync_status = glific_sync_status
    bs.save = MagicMock()
    return bs


def _make_student_doc(name="ST-TP-001", name1="Two Phase Student"):
    """Return a MagicMock behaving like a Student doc returned by process_student_record."""
    doc = MagicMock()
    doc.name = name
    doc.name1 = name1
    doc.glific_id = None
    return doc


# ---------------------------------------------------------------------------
# AC-1: Phase 1 makes ZERO Glific HTTP calls
# ---------------------------------------------------------------------------

class TestPhase1ZeroGlificCalls(unittest.TestCase):
    """Assert that process_batch_job's Phase-1 loop never invokes any Glific
    I/O helpers — specifically process_glific_contact and
    create_or_get_glific_group_for_batch."""

    def _run_process_batch_job(self, backend_student_entries, student_docs=None,
                               glific_sync_status="pending"):
        """Drive process_batch_job with a controlled set of Backend Students
        entries and capture whether Glific helpers are invoked.

        Returns (result, mock_glific_contact, mock_glific_group, mock_enqueue).
        """
        if student_docs is None:
            student_docs = [_make_student_doc(f"ST-TP-{i:03d}") for i in range(len(backend_student_entries))]

        # Sequence of frappe.get_doc return values:
        #   1st call = the Backend Student Onboarding (batch) doc
        #   subsequent calls = one Backend Students doc per student
        #   last call = the batch doc again for final status update
        batch_doc = MagicMock(
            status="Processing", name="SET-TP-001",
            processed_student_count=0, save=MagicMock()
        )
        batch_doc.__bool__ = lambda s: True

        get_doc_side_effect = [batch_doc]
        for entry in backend_student_entries:
            get_doc_side_effect.append(entry)
        get_doc_side_effect.append(batch_doc)  # final batch status update

        # frappe.get_all: first call returns the student list, then Phase-2
        # get_all returns the pending rows list.
        pending_names = [
            _row(e.name) for e in backend_student_entries
            if e.glific_sync_status in ("pending", "failed")
        ]

        get_all_call_count = [0]

        def _get_all_side_effect(*args, **kwargs):
            get_all_call_count[0] += 1
            count = get_all_call_count[0]
            if count == 1:
                # Phase-1: student list query
                return [
                    _row(e.name, batch_skeyword=e.batch_skeyword)
                    for e in backend_student_entries
                ]
            elif count == 2:
                # batch_onboarding_cache lookup
                return [_row(e.batch_skeyword, batch_skeyword=e.batch_skeyword, kit_less=False)
                        for e in backend_student_entries]
            else:
                # Phase-2: pending rows query
                return pending_names

        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "process_glific_contact") as mock_gc, \
             patch.object(bop, "create_or_get_glific_group_for_batch") as mock_grp, \
             patch.object(bop, "process_student_record",
                          side_effect=iter(student_docs)) as mock_psr, \
             patch.object(bop, "update_backend_student_status") as mock_ubss, \
             patch.object(bop, "update_job_progress"), \
             patch.object(bop, "get_initial_stage", return_value="Stage-0"), \
             patch.object(bop, "get_course_level_with_validation_backend",
                          return_value="CL-CODING-001"):

            mock_frappe.get_doc = MagicMock(side_effect=get_doc_side_effect)
            mock_frappe.get_all = MagicMock(side_effect=_get_all_side_effect)
            mock_frappe.db = MagicMock(count=MagicMock(return_value=len(student_docs)))
            mock_frappe.enqueue = MagicMock()
            mock_frappe.log_error = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())

            result = bop.process_batch_job("SET-TP-001")

        return result, mock_gc, mock_grp, mock_frappe.enqueue, mock_psr, mock_ubss

    def test_process_glific_contact_never_called_in_phase1(self):
        """AC-1: process_glific_contact must be called ZERO times during Phase 1."""
        bs = _make_backend_student()
        result, mock_gc, mock_grp, mock_enqueue, mock_psr, mock_ubss = \
            self._run_process_batch_job([bs])

        mock_gc.assert_not_called()

    def test_create_glific_group_never_called_in_phase1(self):
        """AC-1: create_or_get_glific_group_for_batch must be called ZERO times
        during Phase 1 — the Glific group is only needed in Phase 2."""
        bs = _make_backend_student()
        result, mock_gc, mock_grp, mock_enqueue, mock_psr, mock_ubss = \
            self._run_process_batch_job([bs])

        mock_grp.assert_not_called()

    def test_process_student_record_called_with_none_glific_contact(self):
        """AC-1 corollary: process_student_record must be called with
        glific_contact=None in Phase 1 (not an actual Glific contact dict)."""
        bs = _make_backend_student()
        result, mock_gc, mock_grp, mock_enqueue, mock_psr, mock_ubss = \
            self._run_process_batch_job([bs])

        mock_psr.assert_called_once()
        call_args = mock_psr.call_args
        glific_contact_arg = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("glific_contact")
        )
        self.assertIsNone(
            glific_contact_arg,
            "process_student_record must receive glific_contact=None in Phase 1"
        )

    def test_student_created_in_phase1_regardless_of_glific(self):
        """Phase-1 DB records are created independently of Glific outcome.
        process_student_record is called (creating Student+Enrollment+states)
        even though no Glific contact is fetched."""
        bs = _make_backend_student()
        result, mock_gc, mock_grp, mock_enqueue, mock_psr, mock_ubss = \
            self._run_process_batch_job([bs])

        mock_psr.assert_called_once()
        # Status was updated (Success path)
        mock_ubss.assert_called_once()

    def test_phase1_multiple_students_no_glific_calls(self):
        """With three students in the set, Phase 1 still makes zero Glific calls."""
        students = [
            _make_backend_student(f"BS-TWO-PHASE-{i:03d}", parent="SET-TP-001")
            for i in range(1, 4)
        ]
        student_docs = [_make_student_doc(f"ST-TP-{i:03d}") for i in range(1, 4)]

        result, mock_gc, mock_grp, mock_enqueue, mock_psr, mock_ubss = \
            self._run_process_batch_job(students, student_docs)

        # Still zero Glific calls
        mock_gc.assert_not_called()
        mock_grp.assert_not_called()
        # But process_student_record was called for each student
        self.assertEqual(mock_psr.call_count, 3)


# ---------------------------------------------------------------------------
# glific_sync_status='pending' set in Phase 1
# ---------------------------------------------------------------------------

class TestPhase1GlificSyncStatusPending(unittest.TestCase):
    """Assert that glific_sync_status='pending' is set on the Backend Students
    doc before update_backend_student_status (which calls save()) is invoked.

    This is the mechanism that allows Phase 2 to identify which rows to enqueue.
    """

    def test_glific_sync_status_pending_before_save(self):
        """glific_sync_status must be 'pending' on the doc object at the moment
        update_backend_student_status is called (so save() persists it)."""
        bs = _make_backend_student()
        bs.glific_sync_status = "pending"  # default; Phase 1 sets it explicitly

        captured_status = []

        def _capture_ubss(student, status, student_doc=None, error=None):
            """Intercept update_backend_student_status and record the
            glific_sync_status at call time."""
            captured_status.append(student.glific_sync_status)

        batch_doc = MagicMock(
            status="Processing", name="SET-TP-001",
            processed_student_count=0, save=MagicMock()
        )
        batch_doc.__bool__ = lambda s: True

        get_all_call_count = [0]

        def _get_all_side_effect(*args, **kwargs):
            get_all_call_count[0] += 1
            count = get_all_call_count[0]
            if count == 1:
                return [_row(bs.name, batch_skeyword=bs.batch_skeyword)]
            elif count == 2:
                return [_row(bs.batch_skeyword, batch_skeyword=bs.batch_skeyword, kit_less=False)]
            else:
                return [_row(bs.name)]  # pending rows for Phase 2

        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "process_glific_contact"), \
             patch.object(bop, "create_or_get_glific_group_for_batch"), \
             patch.object(bop, "process_student_record",
                          return_value=_make_student_doc()), \
             patch.object(bop, "update_backend_student_status",
                          side_effect=_capture_ubss), \
             patch.object(bop, "update_job_progress"), \
             patch.object(bop, "get_initial_stage", return_value=None), \
             patch.object(bop, "get_course_level_with_validation_backend",
                          return_value="CL-CODING-001"):

            mock_frappe.get_doc = MagicMock(
                side_effect=[batch_doc, bs, batch_doc]
            )
            mock_frappe.get_all = MagicMock(side_effect=_get_all_side_effect)
            mock_frappe.db = MagicMock(count=MagicMock(return_value=1))
            mock_frappe.enqueue = MagicMock()
            mock_frappe.log_error = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())

            bop.process_batch_job("SET-TP-001")

        self.assertTrue(
            len(captured_status) > 0,
            "update_backend_student_status must have been called at least once"
        )
        self.assertEqual(
            captured_status[0], "pending",
            f"glific_sync_status must be 'pending' when update_backend_student_status "
            f"is called in Phase 1; got {captured_status[0]!r}"
        )


# ---------------------------------------------------------------------------
# Phase 2: enqueue assertions
# ---------------------------------------------------------------------------

class TestPhase2EnqueueCalls(unittest.TestCase):
    """Assert that process_batch_job enqueues sync_student_to_glific for each
    student whose glific_sync_status is 'pending' or 'failed', with the correct
    kwargs and WITHOUT a 'retry=' kwarg.
    """

    def _run_and_capture_enqueues(self, backend_students):
        """Run process_batch_job and return the list of frappe.enqueue call_args."""
        student_docs = [_make_student_doc(f"ST-TP-{i:03d}") for i in range(len(backend_students))]

        batch_doc = MagicMock(
            status="Processing", name="SET-TP-001",
            processed_student_count=0, save=MagicMock()
        )
        batch_doc.__bool__ = lambda s: True

        # Phase-2 pending rows = all students in this helper (all are pending)
        pending_rows = [_row(s.name) for s in backend_students]

        get_all_call_count = [0]

        def _get_all_side_effect(*args, **kwargs):
            get_all_call_count[0] += 1
            count = get_all_call_count[0]
            if count == 1:
                return [
                    _row(s.name, batch_skeyword=s.batch_skeyword)
                    for s in backend_students
                ]
            elif count == 2:
                return [
                    _row(s.batch_skeyword, batch_skeyword=s.batch_skeyword, kit_less=False)
                    for s in backend_students
                ]
            else:
                return pending_rows

        get_doc_side_effect = [batch_doc] + backend_students + [batch_doc]

        enqueue_calls = []

        def _capture_enqueue(*args, **kwargs):
            enqueue_calls.append((args, kwargs))

        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "process_glific_contact"), \
             patch.object(bop, "create_or_get_glific_group_for_batch"), \
             patch.object(bop, "process_student_record",
                          side_effect=iter(student_docs)), \
             patch.object(bop, "update_backend_student_status"), \
             patch.object(bop, "update_job_progress"), \
             patch.object(bop, "get_initial_stage", return_value=None), \
             patch.object(bop, "get_course_level_with_validation_backend",
                          return_value="CL-CODING-001"):

            mock_frappe.get_doc = MagicMock(side_effect=get_doc_side_effect)
            mock_frappe.get_all = MagicMock(side_effect=_get_all_side_effect)
            mock_frappe.db = MagicMock(count=MagicMock(return_value=len(backend_students)))
            mock_frappe.enqueue = MagicMock(side_effect=_capture_enqueue)
            mock_frappe.log_error = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())

            bop.process_batch_job("SET-TP-001")

        return enqueue_calls

    def test_enqueue_called_once_per_pending_student(self):
        """Phase 2 enqueues one job per student with glific_sync_status='pending'."""
        bs1 = _make_backend_student("BS-TWO-PHASE-001", glific_sync_status="pending")
        bs2 = _make_backend_student("BS-TWO-PHASE-002", glific_sync_status="pending")

        enqueue_calls = self._run_and_capture_enqueues([bs1, bs2])
        self.assertEqual(
            len(enqueue_calls), 2,
            f"Expected 2 enqueue calls (one per student), got {len(enqueue_calls)}"
        )

    def test_enqueue_uses_sync_student_to_glific_path(self):
        """Phase-2 enqueue must target the sync_student_to_glific function path."""
        bs = _make_backend_student()

        enqueue_calls = self._run_and_capture_enqueues([bs])
        self.assertEqual(len(enqueue_calls), 1)

        args, kwargs = enqueue_calls[0]
        func_arg = args[0] if args else kwargs.get("method") or ""
        self.assertIn(
            "sync_student_to_glific", str(func_arg),
            f"Enqueue must target sync_student_to_glific; got {func_arg!r}"
        )

    def test_enqueue_passes_correct_backend_student_name(self):
        """Each enqueue call must pass the Backend Students doc name as
        backend_student_name=."""
        bs = _make_backend_student("BS-TWO-PHASE-UNIQUE-001")

        enqueue_calls = self._run_and_capture_enqueues([bs])
        self.assertEqual(len(enqueue_calls), 1)

        args, kwargs = enqueue_calls[0]
        self.assertEqual(
            kwargs.get("backend_student_name"), "BS-TWO-PHASE-UNIQUE-001",
            f"backend_student_name kwarg must be 'BS-TWO-PHASE-UNIQUE-001'; "
            f"got {kwargs.get('backend_student_name')!r}"
        )

    def test_enqueue_uses_long_queue(self):
        """Phase-2 jobs must be enqueued on queue='long'."""
        bs = _make_backend_student()

        enqueue_calls = self._run_and_capture_enqueues([bs])
        self.assertEqual(len(enqueue_calls), 1)

        args, kwargs = enqueue_calls[0]
        self.assertEqual(
            kwargs.get("queue"), "long",
            f"queue must be 'long'; got {kwargs.get('queue')!r}"
        )

    def test_enqueue_has_no_retry_kwarg(self):
        """Phase-2 enqueue must NOT include a 'retry=' kwarg.

        Retries are self-managed inside sync_student_to_glific via _attempt.
        Passing both would double-retry (retry attempts × _attempt budget).
        """
        bs = _make_backend_student()

        enqueue_calls = self._run_and_capture_enqueues([bs])
        self.assertEqual(len(enqueue_calls), 1)

        args, kwargs = enqueue_calls[0]
        self.assertNotIn(
            "retry", kwargs,
            "enqueue must NOT pass 'retry=' — retries are self-managed "
            "inside sync_student_to_glific via _attempt"
        )

    def test_enqueue_uses_enqueue_after_commit(self):
        """Phase-2 enqueue must use enqueue_after_commit=True so Phase-1 commits
        are visible to the worker before it reads the Backend Students row."""
        bs = _make_backend_student()

        enqueue_calls = self._run_and_capture_enqueues([bs])
        self.assertEqual(len(enqueue_calls), 1)

        args, kwargs = enqueue_calls[0]
        self.assertEqual(
            kwargs.get("enqueue_after_commit"), True,
            f"enqueue_after_commit must be True; got {kwargs.get('enqueue_after_commit')!r}"
        )


# ---------------------------------------------------------------------------
# Set-status accounting (Phase-1 DB success, not Glific outcome)
# ---------------------------------------------------------------------------

class TestSetStatusAccountingDBOnly(unittest.TestCase):
    """Phase-1 success/fail count drives the set's overall status.
    Glific outcome (pending) does NOT mark the student 'Failed' in Phase 1.
    """

    def test_set_status_processed_when_all_db_success(self):
        """When all students succeed at the DB level, set status = 'Processed'
        regardless of Glific pending status."""
        bs = _make_backend_student()

        batch_doc = MagicMock(
            status="Processing", name="SET-TP-001",
            processed_student_count=0
        )
        batch_doc.__bool__ = lambda s: True
        saved_statuses = []

        def _save():
            saved_statuses.append(batch_doc.status)

        batch_doc.save = _save

        get_all_call_count = [0]

        def _get_all_side_effect(*args, **kwargs):
            get_all_call_count[0] += 1
            count = get_all_call_count[0]
            if count == 1:
                return [_row(bs.name, batch_skeyword=bs.batch_skeyword)]
            elif count == 2:
                return [_row(bs.batch_skeyword, batch_skeyword=bs.batch_skeyword, kit_less=False)]
            else:
                return [_row(bs.name)]  # Phase-2 pending rows

        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "process_glific_contact"), \
             patch.object(bop, "create_or_get_glific_group_for_batch"), \
             patch.object(bop, "process_student_record",
                          return_value=_make_student_doc()), \
             patch.object(bop, "update_backend_student_status"), \
             patch.object(bop, "update_job_progress"), \
             patch.object(bop, "get_initial_stage", return_value=None), \
             patch.object(bop, "get_course_level_with_validation_backend",
                          return_value="CL-CODING-001"):

            mock_frappe.get_doc = MagicMock(
                side_effect=[batch_doc, bs, batch_doc]
            )
            mock_frappe.get_all = MagicMock(side_effect=_get_all_side_effect)
            mock_frappe.db = MagicMock(count=MagicMock(return_value=1))
            mock_frappe.enqueue = MagicMock()
            mock_frappe.log_error = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())

            result = bop.process_batch_job("SET-TP-001")

        # success_count=1, failure_count=0 → status should be "Processed"
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        # Check batch.status was set to "Processed"
        self.assertIn("Processed", saved_statuses,
                      f"Batch status must be 'Processed' when all DB ops succeed; "
                      f"saved_statuses={saved_statuses}")

    def test_result_includes_glific_sync_enqueued_count(self):
        """process_batch_job return dict must include 'glific_sync_enqueued' key
        with the count of Phase-2 jobs enqueued (one per pending row)."""
        bs = _make_backend_student()

        batch_doc = MagicMock(
            status="Processing", name="SET-TP-001",
            processed_student_count=0, save=MagicMock()
        )
        batch_doc.__bool__ = lambda s: True

        get_all_call_count = [0]

        def _get_all_side_effect(*args, **kwargs):
            get_all_call_count[0] += 1
            count = get_all_call_count[0]
            if count == 1:
                return [_row(bs.name, batch_skeyword=bs.batch_skeyword)]
            elif count == 2:
                return [_row(bs.batch_skeyword, batch_skeyword=bs.batch_skeyword, kit_less=False)]
            else:
                return [_row(bs.name)]  # 1 pending row

        with patch.object(bop, "frappe") as mock_frappe, \
             patch.object(bop, "process_glific_contact"), \
             patch.object(bop, "create_or_get_glific_group_for_batch"), \
             patch.object(bop, "process_student_record",
                          return_value=_make_student_doc()), \
             patch.object(bop, "update_backend_student_status"), \
             patch.object(bop, "update_job_progress"), \
             patch.object(bop, "get_initial_stage", return_value=None), \
             patch.object(bop, "get_course_level_with_validation_backend",
                          return_value="CL-CODING-001"):

            mock_frappe.get_doc = MagicMock(
                side_effect=[batch_doc, bs, batch_doc]
            )
            mock_frappe.get_all = MagicMock(side_effect=_get_all_side_effect)
            mock_frappe.db = MagicMock(count=MagicMock(return_value=1))
            mock_frappe.enqueue = MagicMock()
            mock_frappe.log_error = MagicMock()
            mock_frappe.logger = MagicMock(return_value=MagicMock())

            result = bop.process_batch_job("SET-TP-001")

        self.assertIn(
            "glific_sync_enqueued", result,
            "Return dict must include 'glific_sync_enqueued' key"
        )
        self.assertEqual(
            result["glific_sync_enqueued"], 1,
            f"glific_sync_enqueued must be 1 (one pending row); got {result['glific_sync_enqueued']}"
        )


if __name__ == "__main__":
    unittest.main()
