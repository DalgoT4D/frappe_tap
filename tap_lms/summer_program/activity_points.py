"""
Activity-points handler (CR-002 v2).
tap_lms/summer_program/activity_points.py

Awards `VideoClass.points` to a ProgramEnrollment whenever a StudentContentLog
row is inserted that records a VideoClass completion. Wired via
`doc_events["StudentContentLog"]["after_insert"]` in hooks.py.

Design notes:

  - Per-row idempotency: `scl.points_awarded > 0` is the write-once anchor
    (P-005). Re-saves of the same SCL row are no-ops.
  - Race-tolerant weekly counter bump: COALESCE-update SQL (P-002 / L-011) so
    the handler is immune to T19's weekly reset of `weekly_activity_points`.
    Cumulative activity/total points roll up at week advance.
  - First-video-of-week flag: `weekly_video_done = 1` is set idempotently
    (writing 1 to 1 is harmless). This flag is the gating signal for T19's
    streak/gem penalty branch.
  - Edge E11 (CR-002 v2) — RETIRED 2026-05-23: zero-point VideoClasses
    used to early-return WITHOUT flipping `weekly_video_done` or arming
    grace/escalation. That broke CR-009: students who watched a 0-point
    intro video that requires a submission would never get escalation
    nudges because the engagement signal never fired. Treating all videos
    the same is the simpler and correct model — engagement is about content
    watched, point value is a separate reward dimension.
  - Glific sync: re-uses `_enqueue_contact_field_sync` (the same retry+DLQ
    machinery that protects every other PE→Glific write — pattern P-007).

CR-003 follow-up (2026-05-13) — grace clock arming:
  - The same UPDATE that flips `weekly_video_done` 0→1 also arms the
    per-week grace clock (`grace_window_start`, `grace_window_end_at`,
    `in_grace_window`) via atomic Postgres `CASE WHEN` clauses. Postgres
    evaluates `CASE WHEN weekly_video_done = 0 ...` against the OLD row
    state within a single UPDATE statement (standard SQL behaviour), so
    the arm only fires when this is genuinely the week's first VideoClass.
    A `grace_window_entered` ProgramEventLog row is written before the
    UPDATE, gated on the pre-UPDATE Python read of `pe.weekly_video_done`.
  - Re-arming on the next week is automatic: T19's weekly reset sets
    `weekly_video_done = 0`, so the next VideoClass completion re-trips
    the CASE WHEN and writes a fresh `grace_window_end_at`.

Test plan: see app/tap_lms/summer_program/tests/test_activity_points.py
and app/tap_lms/summer_program/tests/test_grace_logic.py.
"""
import frappe
import json
from types import SimpleNamespace
from frappe.utils import add_to_date, now_datetime

from tap_lms.summer_program.constants import (
    ACTION_ESCALATION,
    DEFAULT_GRACE_WINDOW_DAYS,
    PATH_CORE,
)
from tap_lms.summer_program.state_machine import (
    _enqueue_contact_field_sync,
    get_active_pe,
)
from tap_lms.summer_program.event_log import log_event


# ════════════════════════════════════════════════════════════
# DocType-event entry point
# ════════════════════════════════════════════════════════════

def handle_content_log(doc, method=None):
    """`after_insert` hook for StudentContentLog.

    Filters to VideoClass completions only; everything else returns
    immediately. Idempotent via `doc.points_awarded > 0`.
    """
    # Filter at entry — only VideoClass completions trigger an award.
    if (doc.content_type or "") != "VideoClass":
        return
    if (doc.action or "") != "completed":
        return
    if _activity_points_handled_upstream(doc):
        return

    award_activity_points(doc)


# ════════════════════════════════════════════════════════════
# Core award logic
# ════════════════════════════════════════════════════════════

def award_activity_points(scl):
    """Award VideoClass.points to the student's active PE.

    Steps (per CR-002 v2 §"New module — activity_points.py" + CR-003
    follow-up 2026-05-13):
      1. Filter (handled by caller `handle_content_log`).
      2. Idempotency: skip if scl.points_awarded > 0.
      3. Resolve award: VideoClass.points → pts. Return on 0/null (E11).
      4. Resolve active PE for scl.student. Return if none.
      5a. If this is the week's first VideoClass (pre-UPDATE
          `pe.weekly_video_done == 0`), log a `grace_window_entered`
          ProgramEventLog row BEFORE the UPDATE so the audit trail captures
          the arm intent even if the UPDATE races. Resolve
          `Batch.grace_window_days` once so the CASE WHEN can use it.
      5b. Atomic UPDATE bumping weekly_activity_points,
          setting weekly_video_done = 1,
          AND arming the grace clock via CASE WHEN clauses that fire iff
          weekly_video_done = 0 at UPDATE time. Postgres evaluates CASE
          against OLD row values within the same UPDATE, so the arm only
          fires for the genuine 0→1 flip.
      6. Write `scl.points_awarded` (audit-after-PE so retries are safe).
      7. Sync contact fields + log activity_points_awarded ProgramEventLog
         event.
    """
    # ── 2. Idempotency anchor (P-005) ───────────────────────
    if (scl.points_awarded or 0) > 0:
        return

    return award_video_completion_points(
        student_id=scl.student,
        video_id=scl.content_id,
        audit_scl_name=scl.name,
        trigger_source="content_log",
    )


def award_video_completion_points(
    student_id,
    video_id,
    audit_scl_name=None,
    trigger_source="complete_content",
):
    """Award VideoClass completion points directly to ProgramEnrollment.

    `complete_content` calls this synchronously so weekly_activity_points,
    total_activity_points, total_points, weekly_video_done, and grace/escalation
    side effects are updated before the API advances progress. The legacy
    StudentContentLog hook calls the same helper for non-API-created video logs.
    """
    if not video_id:
        return 0

    # CR-009 (2026-05-23): zero-point videos still trigger the engagement
    # pipeline. The point bump is a no-op, but weekly_video_done/grace/escalation
    # must still update.
    pts = _resolve_video_points(video_id) or 10

    pe = get_active_pe(student_id)
    if not pe:
        frappe.log_error(
            f"activity_points: no active ProgramEnrollment for student "
            f"{student_id}, video {video_id}; cannot award activity points",
            "SP Activity Points",
        )
        return None

    # ── 5a. First-VideoClass-of-week detection (CR-003 follow-up) ──
    # Pre-UPDATE Python read; the SQL CASE WHEN in step 5b is the source
    # of truth for the atomic arm, but the Python read is a cheap and
    # consistent gate for the event-log row. If the read here is racing a
    # concurrent first-VideoClass write, both paths converge on the same
    # arm semantics — the CASE WHEN preserves the existing clock and the
    # event log may have one extra row that says "grace_window_entered"
    # with no actual arm; that's harmless and observable.
    is_first_video_of_week = not bool(pe.weekly_video_done)
    grace_window_days = _resolve_grace_window_days(pe)

    if is_first_video_of_week:
        # Log BEFORE the UPDATE so the audit row exists even if the SQL
        # raises after the log write. `grace_window_entered` is one of the
        # accepted event_type values on ProgramEventLog.
        from frappe.utils import now_datetime, add_to_date
        log_event(
            pe, "grace_window_entered",
            trigger_source=trigger_source,
            details={
                "video": video_id,
                "grace_days": grace_window_days,
                # Expected end timestamp the CASE WHEN should write — useful
                # for reconstructing the timeline from logs alone if the
                # PE row's grace_window_end_at gets clobbered downstream.
                "grace_window_end_at_expected": str(
                    add_to_date(now_datetime(), days=grace_window_days)
                ),
            },
        )

    before_total_activity_points = int(pe.total_activity_points or 0)
    before_total_points = int(pe.total_points or 0)

    # ── 5b. Atomic UPDATE on PE (P-002 / L-011 + CR-003 grace arm + CR-008 lazy reset + CR-011 eager totals) ──
    # COALESCE-update is race-tolerant against T19's reset of
    # weekly_activity_points (E5).
    #
    # The CASE WHEN clauses fire IFF weekly_video_done = 0 at UPDATE time.
    # Postgres reads OLD row values in CASE WHEN within the same UPDATE
    # (standard SQL behaviour), so the arm only happens when this is
    # genuinely the week's first VideoClass. Second-video-same-week is a
    # no-op for the grace fields (existing values preserved).
    #
    # CR-008 lazy reset (2026-05-23): the same `weekly_video_done = 0` gate
    # also drives the per-week reset of weekly_*_points / weekly_submission_done /
    # quiz_completed / bonus_quiz_points. T14 no longer zeros these (so the
    # student's W1 stats stay visible on Glific through the inter-week gap).
    # Instead, the first VideoClass of the new week wipes them and bumps the
    # video's points — atomically, in this single UPDATE.
    #
    # CR-011 eager totals (2026-05-25): total_activity_points and total_points
    # are now bumped here on every VideoClass completion (ungated — they always
    # fire when pts > 0), so the cumulative totals stay coherent with weekly_*
    # at all times. T14 no longer rolls weekly→total. Note these lines are
    # OUTSIDE the lazy-reset CASE WHEN: the lazy reset only zeros the OTHER
    # weekly_* buckets at the 0→1 flip; total_* are independent of that gate.
    #
    # Assumption (confirmed with the team 2026-05-23, managed in UI + Glific):
    # every week has at least one VideoClass; VideoClass is always the first
    # content item; students cannot skip the video. Known limitations if
    # those assumptions break: weeks without VideoClass freeze weekly_* at
    # previous-week values until a future video; quiz/submission taken
    # before video lose their points when the lazy reset fires.
    frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET weekly_activity_points   = CASE WHEN weekly_video_done = 0
                                               THEN %s
                                               ELSE COALESCE(weekly_activity_points, 0) + %s END,
               weekly_quiz_points       = CASE WHEN weekly_video_done = 0
                                               THEN 0
                                               ELSE weekly_quiz_points END,
               weekly_submission_points = CASE WHEN weekly_video_done = 0
                                               THEN 0
                                               ELSE weekly_submission_points END,
               bonus_quiz_points        = CASE WHEN weekly_video_done = 0
                                               THEN 0
                                               ELSE bonus_quiz_points END,
               weekly_submission_done   = CASE WHEN weekly_video_done = 0
                                               THEN 0
                                               ELSE weekly_submission_done END,
               quiz_completed           = CASE WHEN weekly_video_done = 0
                                               THEN 0
                                               ELSE quiz_completed END,
               weekly_video_done        = 1,
               grace_window_start       = CASE WHEN weekly_video_done = 0
                                               THEN NOW()
                                               ELSE grace_window_start END,
               grace_window_end_at      = CASE WHEN weekly_video_done = 0
                                               THEN NOW() + (%s || ' days')::interval
                                               ELSE grace_window_end_at END,
               in_grace_window          = CASE WHEN weekly_video_done = 0
                                               THEN 1
                                               ELSE in_grace_window END,
               total_activity_points    = COALESCE(total_activity_points, 0) + %s,
               total_points             = COALESCE(total_points,          0) + %s
         WHERE name = %s
        """,
        (pts, pts, grace_window_days, pts, pts, pe.name),
    )
    frappe.db.commit()

    # Reload before any audit marker is written. If this verification fails,
    # complete_content must NOT mark the future StudentContentLog as handled,
    # otherwise the after_insert hook would skip the only remaining chance to
    # award the video points.
    pe.reload()
    update_verified = bool(pe.weekly_video_done)
    if pts > 0:
        update_verified = (
            update_verified
            and int(pe.total_activity_points or 0) >= before_total_activity_points + pts
            and int(pe.total_points or 0) >= before_total_points + pts
        )
    if not update_verified:
        frappe.log_error(
            (
                "activity_points: ProgramEnrollment update did not verify; "
                f"student={student_id}, video={video_id}, pe={pe.name}, "
                f"points={pts}, before_total_activity={before_total_activity_points}, "
                f"after_total_activity={int(pe.total_activity_points or 0)}, "
                f"before_total_points={before_total_points}, "
                f"after_total_points={int(pe.total_points or 0)}, "
                f"weekly_video_done={int(pe.weekly_video_done or 0)}"
            ),
            "SP Activity Points",
        )
        return None

    # Audit-field anchor for the StudentContentLog hook path. The complete_content
    # path has no SCL row yet; it passes points_awarded into the background log
    # job so that later insert is marked as already handled.
    if audit_scl_name:
        frappe.db.set_value(
            "StudentContentLog", audit_scl_name, "points_awarded", pts,
            update_modified=False,
        )

    # ── 7a. Push contact fields ────────────────────────────
    # The PE was reloaded during update verification, so the Glific sync
    # reflects the post-UPDATE values.
    if pe.glific_id:
        _enqueue_contact_field_sync(pe)

    # ── 7c. CR-009 (2026-05-23): backend-driven escalation arming ──
    # Pre-fix, the escalation chain was ARMED only when Glific's
    # update_flow_status callback fired with status='no_response' / 'timeout'
    # for SP_Content_Delivery. Observed in palv2-test-BT52231: 4 PEs in the
    # 'watched-but-no-submission' gap (weekly_video_done=1, weekly_submission_done=0,
    # next_action_at=None) — the Glific callback either fires with 'completed'
    # (which doesn't arm escalation) or doesn't fire at all (webhook miss).
    # Backend-driven arming closes that gap: when a student watches the
    # week's first VideoClass, schedule the first escalation step here.
    #
    # Idempotency gates:
    #   (a) is_first_video_of_week (the pre-UPDATE Python read from step 5a)
    #       — subsequent video watches in the same week must NOT re-arm.
    #   (b) pe.current_escalation_step == 0 — don't reset an in-flight chain.
    #   (c) pe.next_action_type != 'escalation' — defensive; same intent.
    #
    # Submission transitions (T7/T9/T17/T3) clear next_action_at when a
    # submission lands, so the dispatcher won't fire the escalation step
    # once the student responds.
    scl_ref = SimpleNamespace(
        name=audit_scl_name or f"complete_content:{student_id}:{video_id}",
        student=student_id,
        content_id=video_id,
    )
    _maybe_arm_escalation(pe, scl_ref, is_first_video_of_week) 

    # ── 7b. ProgramEventLog ────────────────────────────────
    # Gate on pts > 0 (CR-009 follow-on 2026-05-23): with the E11 early-
    # return removed, zero-point videos now flow through the whole pipeline.
    # The "activity_points_awarded" event with new_value="0" is misleading
    # in audit trails — skip it. The grace_window_entered (step 5a, first
    # video only) and escalation_scheduled (from _maybe_arm_escalation)
    # events already capture the engagement signal for zero-point videos.
    if pts > 0:
        log_event(
            pe, "activity_points_awarded",
            new_value=str(pts),
            trigger_source=trigger_source,
            details={
                "scl": audit_scl_name or "",
                "video": video_id,
                "points": pts,
                "source": trigger_source,
            },
        )
    return pts


# ════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════

def _resolve_video_points(video_id):
    """Return VideoClass.points for the given id, or 0 if missing/zero.

    Reads via `frappe.db.get_value` (single SELECT, no full doc hydration)
    because the handler runs on every video completion across the cohort —
    keep it cheap.
    """
    try:
        pts = frappe.db.get_value("VideoClass", video_id, "points")
    except Exception:
        # Defensive: a VideoClass row missing should not crash the SCL insert.
        # Log and skip; the SCL is still recorded.
        frappe.log_error(
            f"activity_points: VideoClass {video_id} lookup failed",
            "SP Activity Points",
        )
        return 0
    return int(pts or 0)


def _activity_points_handled_upstream(scl):
    """Return True when complete_content already updated PE activity points."""
    metadata = getattr(scl, "metadata", None)
    if not metadata:
        return False
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            return False
    if not isinstance(metadata, dict):
        return False
    return metadata.get("activity_points_awarded_by") == "complete_content"


def _resolve_grace_window_days(pe):
    """Return Batch.grace_window_days for the PE, falling back to
    DEFAULT_GRACE_WINDOW_DAYS if unset.

    Mirrors `state_machine._batch_grace_window_days` but is duplicated here
    to keep activity_points self-contained — the CASE WHEN UPDATE needs the
    integer value bound as a parameter, so we resolve it before the SQL call.
    """
    if not pe.batch:
        return DEFAULT_GRACE_WINDOW_DAYS
    try:
        days = frappe.db.get_value("Batch", pe.batch, "grace_window_days")
    except Exception:
        days = None
    if not days:
        frappe.log_error(
            f"activity_points: Batch {pe.batch} has no grace_window_days; "
            f"falling back to default ({DEFAULT_GRACE_WINDOW_DAYS}d) for PE {pe.name}",
            "SP Activity Points Grace Config",
        )
        return DEFAULT_GRACE_WINDOW_DAYS
    return int(days)


# ════════════════════════════════════════════════════════════
# CR-009 (2026-05-23) — backend-driven escalation arming
# ════════════════════════════════════════════════════════════

def _maybe_arm_escalation(pe, scl, is_first_video_of_week):
    """Arm `next_action_at + next_action_type = 'escalation'` when the
    student watches the week's first VideoClass — independent of whether
    Glific's update_flow_status callback for SP_Content_Delivery ever
    arrives.

    Pre-CR-009 path: escalation was armed by
    flow_callback._handle_content_delivery only when Glific reported
    status in ('no_response', 'timeout'). For students who DID tap the
    content message (status='completed'), or for cases where the Glific
    callback was missing entirely, the escalation chain never started even
    though the grace clock did. Net result: students stuck watching the
    grace countdown with zero nudges in between.

    Args:
        pe: ProgramEnrollment doc (post-UPDATE, already reloaded by caller).
        scl: StudentContentLog row (used for the event-log audit field).
        is_first_video_of_week: pre-UPDATE Python read captured before the
            atomic SQL flipped weekly_video_done 0→1. We need the
            pre-UPDATE value, not the post-reload value (which is always 1).

    Guarded by three idempotency gates:
        (a) is_first_video_of_week == True
        (b) pe.current_escalation_step == 0 (no chain in flight)
        (c) pe.next_action_type != 'escalation' (defensive — same intent)

    Failure mode: any exception is logged and swallowed. Escalation arming
    is a follow-on to the points bump; we never want to bubble a failure
    here back into the StudentContentLog insert path.
    """
    if not is_first_video_of_week:
        return
    if (pe.current_escalation_step or 0) != 0:
        return
    if pe.next_action_type == ACTION_ESCALATION:
        return

    try:
        # Lazy import — avoid pulling student_progression_sp at module load
        # (it's a heavier module with broader dependencies).
        from tap_lms.summer_program.student_progression_sp import _get_escalation_steps

        student = frappe.get_doc("Student", scl.student)
        batch_doc = frappe.get_doc("Batch", pe.batch)
        steps = _get_escalation_steps(
            student, batch_doc, path=pe.current_path or PATH_CORE,
        )
        if not steps:
            # No escalation config for this archetype/arm/path. Not an
            # error — could be by design (e.g., a path with no escalation).
            # Log at info level for operator visibility without polluting
            # the Error Log doctype.
            frappe.logger("activity_points").info(
                f"_maybe_arm_escalation: PE {pe.name} (archetype={student.archetype}, "
                f"arm={student.experiment_arm}, path={pe.current_path}) has no "
                f"escalation_steps configured — skipping arm."
            )
            return

        first_step_hours = float(steps[0].get("hours_after_previous") or 24)
        fire_at = add_to_date(now_datetime(), hours=first_step_hours)
        frappe.db.set_value("ProgramEnrollment", pe.name, {
            "next_action_at": fire_at,
            "next_action_type": ACTION_ESCALATION,
        })

        log_event(
            pe, "escalation_scheduled",
            trigger_source="activity_points",
            details={
                "scheduled_at": str(fire_at),
                "hours_from_now": first_step_hours,
                "first_step_type": steps[0].get("escalation_type"),
                "trigger": "first_video_of_week",
                "scl": scl.name,
            },
        )
    except Exception as e:
        try:
            frappe.db.rollback()
        except Exception:
            pass
        frappe.logger("activity_points").error(
            f"_maybe_arm_escalation: failed to arm escalation for PE "
            f"{pe.name}: {e}"
        )
        # Do NOT re-raise — escalation arming is a non-critical follow-on
        # to the points bump. The SCL is already on disk; the next time
        # this PE's first-video-of-week handler fires (e.g., next week's
        # T19+video cycle), arming will retry.
