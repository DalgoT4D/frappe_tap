"""Binge-resume threshold consistency (regression for the resume asymmetry).

THE BUG (pre-fix): three thresholds disagreed —
  PAUSE        (handle_week_advancement) : max_allowed = calendar_week + 1
  DISPATCHER   (handle_pause_check)       : max_allowed = calendar_week      ← outlier
  REACTIVATION (reactivation.py)          : max_allowed = calendar_week + 1
So a student paused at the week-W→(W+1) boundary, whom the pause policy allows
to be 1 week ahead, was held paused by the DISPATCHER one extra week — yet the
inbound reactivation path resumed them. Same student, different resume timing
depending on which path ran.

THE FIX: handle_pause_check now uses calendar_week + 1, matching pause +
reactivation. These tests assert the dispatcher resumes on policy (and does not
over-resume a student more than 1 week ahead).

Glific side-effects of the resume transition are stubbed in setUp (no network).
"""
import frappe
from unittest.mock import patch
from frappe.tests.utils import FrappeTestCase

SM = "tap_lms.summer_program.state_machine"

from tap_lms.summer_program.tests.factories import make_batch
from tap_lms.summer_program.constants import (
    STATE_PAUSED_BINGE,
    STATE_NORMAL_CONTENT,
    PROGRAM_PAUSED,
    PATH_CORE,
    LABEL_PAUSED,
)

_SEQ = [0]


def _mk_paused_binge_pe(batch, current_week, max_allowed_week):
    _SEQ[0] += 1
    suf = f"{_SEQ[0]:05d}"
    s = frappe.new_doc("Student")
    s.name1 = f"BingeStu{suf}"
    s.phone = f"+9198000{suf}"
    s.archetype = "fence_sitter"
    s.experiment_arm = "arm_a"
    s.language = "English"
    s.glific_id = f"binge-stu-glific-{suf}"
    s.insert(ignore_permissions=True)

    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"{s.name}-{batch}-binge-{suf}"
    pe.student = s.name
    pe.batch = batch
    pe.program_type = "Summer"
    pe.glific_id = f"binge-pe-glific-{suf}"
    pe.archetype = "fence_sitter"
    pe.experiment_arm = "arm_a"
    pe.current_path = PATH_CORE
    pe.current_tier = "Basic"
    pe.journey_label = LABEL_PAUSED
    pe.program_status = PROGRAM_PAUSED
    pe.resolved_flow_state = STATE_PAUSED_BINGE
    pe.current_week = current_week
    pe.max_allowed_week = max_allowed_week
    pe.insert(ignore_permissions=True)
    return pe.name, s.name


class TestBingeResumeAsymmetry(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # calendar_week = 3; auto_advance sets max_allowed_week = calendar+1 = 4.
        cls.batch = make_batch("BingeAsymBatch", "BNGA1", current_calendar_week=3)
        # make_batch is idempotent-return: if a prior run left this batch with a
        # different week, force it to 3 so tests 1/2 don't false-fail (L-062).
        frappe.db.set_value("Batch", cls.batch, "current_calendar_week", 3)

    def _state(self, pe):
        return frappe.db.get_value("ProgramEnrollment", pe, "resolved_flow_state")

    def setUp(self):
        # The resume path (t21 → transition, no skip_glific) would enqueue
        # Glific sync + collection maintenance; stub both (no network).
        for tgt in ("_enqueue_contact_field_sync", "maintain_collections"):
            p = patch(f"{SM}.{tgt}")
            p.start()
            self.addCleanup(p.stop)

    def test_dispatcher_resumes_wk3_paused_at_calendar3(self):
        """FIXED: a student who completed week 3 (next_week=4) is within the
        pause ceiling (calendar+1 = 4) at calendar=3, so the dispatcher now
        resumes them — matching the pause policy and the reactivation path.
        (Pre-fix this stayed paused because the check used bare calendar=3.)"""
        from tap_lms.summer_program import pe_dispatcher
        pe, _stu = _mk_paused_binge_pe(self.batch, current_week=3, max_allowed_week=4)
        pe_dispatcher.handle_pause_check(frappe._dict(name=pe))
        self.assertEqual(self._state(pe), STATE_NORMAL_CONTENT)

    def test_dispatcher_does_not_over_resume_two_weeks_ahead(self):
        """Guard: the fix must not resume a student MORE than 1 week ahead.
        current_week=4 (next_week=5) at calendar=3 → 5 > calendar+1(4) → stays
        paused."""
        from tap_lms.summer_program import pe_dispatcher
        pe, _stu = _mk_paused_binge_pe(self.batch, current_week=4, max_allowed_week=4)
        pe_dispatcher.handle_pause_check(frappe._dict(name=pe))
        self.assertEqual(self._state(pe), STATE_PAUSED_BINGE)

    def test_reactivation_DOES_resume_wk3_paused_at_calendar3(self):
        """Same student, inbound path: reactivation uses max_allowed =
        calendar+1 = 4, so next_week(4) <= 4 → resumes. This is the
        inconsistency — resume timing depends on which path runs."""
        from tap_lms.summer_program import reactivation
        pe, stu = _mk_paused_binge_pe(self.batch, current_week=3, max_allowed_week=4)
        frappe.local.response = frappe._dict()
        reactivation.reactivate_student(stu)
        self.assertEqual(self._state(pe), STATE_NORMAL_CONTENT)

    def test_dispatcher_resumes_once_calendar_reaches_next_week(self):
        """Sanity: bump calendar to 4 and the dispatcher resumes (next_week=4
        <= calendar=4). Confirms the threshold is the only thing holding it."""
        from tap_lms.summer_program import pe_dispatcher
        pe, _stu = _mk_paused_binge_pe(self.batch, current_week=3, max_allowed_week=4)
        frappe.db.set_value("Batch", self.batch, "current_calendar_week", 4)
        try:
            pe_dispatcher.handle_pause_check(frappe._dict(name=pe))
            self.assertEqual(self._state(pe), STATE_NORMAL_CONTENT)
        finally:
            frappe.db.set_value("Batch", self.batch, "current_calendar_week", 3)
