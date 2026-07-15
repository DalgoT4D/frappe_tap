"""
Summer Program Student Progression
tap_lms/summer_program/student_progression_sp.py

Submission-driven progression engine for the Summer Program.
Replaces the old quiz-pass/fail branching with:
  - Assignment submission tracking
  - ArchetypeConfig-driven Core/Remedial path resolution
  - Escalation step processing (time-based nudges)
  - Weekly content serving (Core or Remedial LearningUnit)

Called by Glific flows via whitelisted API endpoints.
"""
import frappe
import json
from frappe import _
from frappe.utils import (
    now_datetime, today, getdate, cint, flt,
    date_diff, get_datetime,
    add_days, add_to_date,
)

from tap_lms.summer_program.custom_messages import EXPECTED_SUBMISSION_LABELS
from tap_lms.summer_program.state_machine import get_active_pe
from tap_lms.summer_program.utils import (
    glific_response,
    normalize_unicode_surrogates,
    resolve_student,
    safe_sp_api_error_response,
    sp_safe_endpoint,
    _insert_with_serialization_retry,
    check_glific_placeholders,
)
# CR-024 Phase 2: in-process read cache for immutable master data (ADR-006).
from tap_lms.summer_program.master_data_lookup import (
    cached_question_details,
    cached_content_details_payload,
)


def _time_diff_in_seconds(dt1, dt2):
    """Return the difference (dt1 - dt2) in seconds."""
    return (get_datetime(dt1) - get_datetime(dt2)).total_seconds()

from tap_lms.summer_program.constants import (
    ALL_ARCHETYPES,
    ALL_ARMS,
    BPR_ACTIVE,
    PATH_CORE,
    PATH_REMEDIAL,
    ACTION_WEEK_ADVANCEMENT,
    STATE_WEEK_COMPLETED,
    TIER_BY_WEEK,
    DEFAULT_TIER,
    REMEDIAL_TIER,
    PROGRAM_ACTIVE,
    PROGRAM_PAUSED,
)


# ============================================================
# CONSTANTS (local to this module)
# ============================================================

VALID_CONTENT_TYPES = ["VideoClass", "Quiz", "Assignment", "NoteContent", "CourseProject",
                       "TextMessageContent", "VoiceNoteContent", "ParentCallConfig"]
OPTION_LETTERS = ['A', 'B', 'C', 'D']

# Grace time: hours into a new week during which a student can still submit
# for the previous week without being blocked. Default 24 hours.
SUBMISSION_GRACE_HOURS = 24
DEFAULT_LANGUAGE = "English"


def _option_fields(question_details):
    """Return flattened option fields present in question details."""
    return {
        f"option_{letter.lower()}": question_details[f"option_{letter.lower()}"]
        for letter in OPTION_LETTERS
        if f"option_{letter.lower()}" in question_details
    }


def _is_week_advancement_pending(pe):
    """Return True while T13 has scheduled T14 but dispatcher has not run it."""
    return (
        getattr(pe, "resolved_flow_state", None) == STATE_WEEK_COMPLETED
        and getattr(pe, "next_action_type", None) == ACTION_WEEK_ADVANCEMENT
    )


# ============================================================
# API 1: GET WEEKLY CONTENT
# ============================================================

@frappe.whitelist(allow_guest=False)
@sp_safe_endpoint("get_weekly_content")
@glific_response
def get_weekly_content(student_id, course_level=None, **_glific_kwargs):
    """
    Get the current week's content for a Summer Program student.

    `**_glific_kwargs` absorbs Glific-injected fields (organization_id,
    etc.) per task #89 — ignored at this layer.
    Returns the appropriate LearningUnit (Core or Remedial)
    based on the student's archetype, submission history,
    and ArchetypeConfig rules.

    Called by: Glific content_delivery flow

    Args:
        student_id: Student ID, Glific ID, or phone
        course_level: Optional. If omitted, resolved from enrollment.

    Returns:
        dict with week info, content items, path (Core/Remedial),
        expected submission type, and LearningUnit details.
    """
    # CR-024 Layer 3: reject Glific placeholder strings before any lookup.
    # Checks identifier params only (student_id, course_level) — free-text
    # params like answer or submission are intentionally NOT checked here.
    placeholder_hit = check_glific_placeholders(
        [("student_id", student_id), ("course_level", course_level)],
        api_name="get_weekly_content",
        student_id=student_id,
    )
    if placeholder_hit:
        return placeholder_hit

    course_level = normalize_unicode_surrogates(course_level)
    student_id = _resolve_student_id(student_id)
    if not student_id:
        return {"success": False, "status": "not_found",
                "error_detail": "Student not found"}

    student = frappe.get_doc("Student", student_id)
    batch, bpr = _get_active_bpr_for_student(student)
    if not batch or not bpr:
        return {"success": False, "status": "no_active_batch",
                "error_detail": "No active Summer Program batch found"}

    if not course_level:
        course_level = _get_course_level_for_student(student, batch)
    if not course_level:
        return {"success": False, "status": "no_course_level",
                "error_detail": "No course level found for student"}

    calendar_week = _get_current_week(batch)
    if calendar_week <= 0:
        return {"success": False, "status": "batch_not_started",
                "error_detail": "Batch has not started yet"}

    # PE-canonical SP read (task #71, 2026-05-23): the state machine owns
    # current_week, current_path, and current_tier — these are written by
    # T6b (failed-feedback → Remedial), T14 (week_advance → Core+tier), T8
    # (start_remedial), etc. The legacy `_resolve_path(...)` helper recomputed
    # path from prior-week submission validity, which could disagree with
    # the state machine's recorded value (e.g., a T6b-routed student would
    # have pe.current_path=Remedial while _resolve_path could return Core if
    # the prior-week submission was technically "valid" but the AI flagged it).
    # Same divergence risk for current_tier.
    # Diagnostic against palv2-test-BT52231 (45 PEs) confirmed 0 divergent —
    # this is a preemptive fix with no current-cohort behavior change.
    pe = get_active_pe(student.name, batch.name)
    if not pe:
        return {"success": False, "status": "no_active_pe",
                "error_detail": "No active ProgramEnrollment for this student"}

    current_week = pe.current_week or _get_effective_week(student, batch, calendar_week)
    path = pe.current_path or PATH_CORE
    tier = pe.current_tier or (
        REMEDIAL_TIER if path == PATH_REMEDIAL
        else TIER_BY_WEEK.get(current_week, DEFAULT_TIER)
    )

    if current_week > (batch.total_weeks or 0):
        return {"success": True, "status": "program_completed", "week": current_week}

    learning_unit = _get_learning_unit(course_level, current_week, tier)
    if not learning_unit:
        # Fallback: if no Remedial LU exists, serve Core
        if path == PATH_REMEDIAL:
            tier = TIER_BY_WEEK.get(current_week, DEFAULT_TIER)
            learning_unit = _get_learning_unit(course_level, current_week, tier)
            path = PATH_CORE

    if not learning_unit:
        return {"success": False, "status": "no_content_for_week",
                "error_detail": f"No content found for week {current_week}"}

    # Get content items
    content_items = _get_content_items(learning_unit)

    # Get WeekRule for expected submission type
    week_rule = _get_week_rule(student, batch, current_week)

    # Update/create StudentStageProgress (side-effect, return value unused here)
    _get_or_create_sp_progress(student_id, course_level, current_week, tier, learning_unit)

    # Flatten content_items[] to numeric-suffix scalars per
    # docs/api-standard-glific.md Rule 3. Cap at 10 — log + truncate if exceeded.
    CONTENT_CAP = 10
    if len(content_items) > CONTENT_CAP:
        frappe.log_error(
            f"get_weekly_content: learning_unit {learning_unit} has "
            f"{len(content_items)} content items; truncating to {CONTENT_CAP}. "
            f"Increase the cap or split the LU.",
            "SP API contract",
        )
        content_items = content_items[:CONTENT_CAP]

    response = {
        "success": True,
        "status": "content_available",
        "student_id": student_id,
        "week": current_week,
        "path": path,
        "tier": tier,
        "learning_unit": learning_unit,
        "learning_unit_name": frappe.db.get_value("LearningUnit", learning_unit, "unit_name"),
        "expected_submission_type": (week_rule.get("expected_submission_type") if week_rule else None),
        "submission_validation_enabled": (week_rule.get("submission_validation_enabled", 0) if week_rule else 0),
        "total_weeks": batch.total_weeks,
        "content_count": len(content_items),
    }
    for i, item in enumerate(content_items, start=1):
        response[f"content_{i}_type"] = item.get("content_type")
        response[f"content_{i}_id"] = item.get("content_id")
        response[f"content_{i}_name"] = item.get("content_name")
        response[f"content_{i}_is_optional"] = bool(item.get("is_optional"))
    return response


# ════════════════════════════════════════════════════════════
# REMOVED: record_submission, get_escalation_action, get_student_sp_overview
# ════════════════════════════════════════════════════════════
# Three legacy whitelisted endpoints removed on 2026-05-11:
#
#   - record_submission        → superseded by `save_submission` (does strictly
#                                more: state machine T3/T7/T22, atomic primary
#                                claim, idempotency, AI feedback pipeline,
#                                GCS upload, state-aware terminal/paused guard)
#   - get_escalation_action    → superseded by the per-PE dispatcher (#15) +
#                                state_machine T1/T2/T4 + escalation_runner cron
#   - get_student_sp_overview  → superseded by `get_student_state` (in
#                                program_enrollment_api.py) + admin dashboard
#                                APIs in summer_program.api
#
# Verified zero callers across the codebase before deletion (grep against
# app/tap_lms turned up only the function definitions themselves). All three
# were missing from the Glific reference doc (`docs/glific-api-reference-v1.md`),
# and the live SP enrollment + submission flow does not depend on them.
#
# If any external caller (Glific flow, dashboard, ad-hoc API consumer) still
# hits these paths, they will now get a 404. That's intentional — we'd rather
# fail loud than silently accept submissions that don't transition state.
#
# Orphan-helper audit completed in task #72 (2026-05-15): the following
# private helpers — only ever called by the three removed endpoints — were
# deleted: _log_submission, _validate_submission_type, _reset_escalation,
# _get_next_escalation_step, _record_escalation_step, _has_submitted_this_week,
# _get_submission_history, _calculate_submission_points, _update_engagement_state,
# _mark_week_submitted. Each was confirmed to have zero callers via grep across
# app/tap_lms + tests before deletion. Removed helpers also accounted for
# 4 stray frappe.db.commit() calls (L-017 cleanup, task #87).


# ============================================================
# API 5: GET NEXT CONTENT (Content Stepping)
# ============================================================

@frappe.whitelist(allow_guest=False)
@glific_response
def get_next_content(student_id, course_level=None, **_glific_kwargs):
    """
    `**_glific_kwargs` absorbs Glific-injected fields per task #89 — ignored.

    Get next content item for a Summer Program student.
    Steps through LearningUnit items one at a time using
    current_content_index in StudentStageProgress.

    Handles:
      - Active quiz detection (resume)
      - Content stepping within a LearningUnit
      - LU completion → next LU in same week/tier
      - Week completion → next week (via _resolve_path for Core/Remedial)
      - Course completion

    Called by: Glific content delivery sub-flow (replaces flat get_weekly_content
    for step-by-step delivery).

    Args:
        student_id: Student ID, Glific ID, or phone
        course_level: Course Level name (resolved from enrollment if omitted)

    Returns:
        dict with content_available / quiz_in_progress / stage_complete /
        course_complete status plus content details.
    """
    # ASSESSMENT_CAP — must match get_content_details for consistency.
    # `assessment_<i>_id` is CRITICAL — it's the assignment_id Glific passes
    # to save_submission. Do NOT drop these.
    ASSESSMENT_CAP = 5

    def _flat_content_response(item, content_index, total_in_unit, position_kwargs,
                                new_learning_unit=False):
        """Build the flat content_available response (helper to avoid
        duplicating the same flat-shape logic across the two LU variants)."""
        assessments = _get_video_assessments(item["content_type"], item["content_id"]) or []
        if len(assessments) > ASSESSMENT_CAP:
            frappe.log_error(
                f"{item['content_type']} {item['content_id']} has "
                f"{len(assessments)} assessments; truncating to {ASSESSMENT_CAP}.",
                "SP API contract",
            )
            assessments = assessments[:ASSESSMENT_CAP]

        resp = {
            "success": True,
            "status": "content_available",
            "student_id": student_id,
            "has_active_quiz": False,
            "new_learning_unit": new_learning_unit,
            "course_level": course_level,
            # position.* flattened
            "position_week": position_kwargs["week"],
            "position_tier": position_kwargs["tier"],
            "position_learning_unit": position_kwargs["learning_unit"],
            "position_learning_unit_name": position_kwargs.get("learning_unit_name"),
            "position_content_index": content_index,
            "position_is_remedial": position_kwargs["is_remedial"],
            "position_path": position_kwargs["path"],
            # content.* flattened
            "content_type": item["content_type"],
            "content_id": item["content_id"],
            "content_name": item["content_name"],
            "content_order": content_index + 1,
            "content_total_in_unit": total_in_unit,
            "content_is_optional": bool(item.get("is_optional")),
            # assessments[] flattened — assessment_<i>_id is the assignment_id
            # input to save_submission. Critical path.
            "assessment_count": len(assessments),
        }
        for i, a in enumerate(assessments, start=1):
            resp[f"assessment_{i}_type"] = a.get("assessment_type")
            resp[f"assessment_{i}_id"] = a.get("assessment_id")
        return resp

    try:
        # CR-024 Layer 3: reject Glific placeholder strings before any lookup.
        placeholder_hit = check_glific_placeholders(
            [("student_id", student_id), ("course_level", course_level)],
            api_name="get_next_content",
            student_id=student_id,
        )
        if placeholder_hit:
            return placeholder_hit

        course_level = normalize_unicode_surrogates(course_level)
        student_id = _resolve_student_id(student_id)
        if not student_id:
            return {"success": False, "status": "not_found",
                    "error_detail": "Student not found"}

        student = frappe.get_doc("Student", student_id)
        batch, bpr = _get_active_bpr_for_student(student)
        if not batch or not bpr:
            return {"success": False, "status": "no_active_batch",
                    "error_detail": "No active Summer Program batch found"}

        if not course_level:
            course_level = _get_course_level_for_student(student, batch)
        if not course_level:
            return {"success": False, "status": "no_course_level",
                    "error_detail": "No course level found for student"}

        calendar_week = _get_current_week(batch)
        if calendar_week <= 0:
            return {"success": False, "status": "batch_not_started",
                    "error_detail": "Batch has not started yet"}

        # PE-canonical SP read (task #71, 2026-05-23): the state machine
        # owns current_week, current_path, and current_tier — these are
        # written by T6b (failed-feedback → Remedial), T14 (week_advance →
        # Core+tier), T8 (start_remedial), etc. The legacy `_resolve_path(...)`
        # helper recomputed path from prior-week submission validity, which
        # could disagree with the state machine's recorded value (e.g., a
        # T6b-routed student would have pe.current_path=Remedial while
        # _resolve_path could return Core if the prior-week submission was
        # technically "valid" but the AI flagged it). Same divergence risk
        # for current_tier (Manu's set-by-T14 vs locally-recomputed by week).
        # Diagnostic against palv2-test-BT52231 (45 PEs) confirmed 0 divergent —
        # this is a preemptive fix with no current-cohort behavior change.
        pe = get_active_pe(student.name, batch.name)
        if not pe:
            return {"success": False, "status": "no_active_pe",
                    "error_detail": "No active ProgramEnrollment for this student"}

        if _is_week_advancement_pending(pe):
            return {
                "success": False,
                "status": "week advancement pending",
                "current_week": cint(pe.current_week or 0),
            }

        current_week = pe.current_week or _get_effective_week(student, batch, calendar_week)
        path = pe.current_path or PATH_CORE
        tier = pe.current_tier or (
            REMEDIAL_TIER if path == PATH_REMEDIAL
            else TIER_BY_WEEK.get(current_week, DEFAULT_TIER)
        )

        # Check content blocking: if student didn't submit previous week,
        # they can't access current week content (escalation handles follow-up).
        if current_week > 1:
            prev_submission = _get_submission_validity(student.name, current_week - 1)
            if not prev_submission["submitted"]:
                if not _is_within_grace_period(batch, current_week):
                    return {
                        "success": True,
                        "status": "content_blocked",
                        "user_message": f"Please complete your Week {current_week - 1} submission first.",
                        "student_id": student_id,
                        "blocked_reason": "missing_previous_submission",
                        "pending_week": current_week - 1,
                        "calendar_week": calendar_week,
                    }

        learning_unit = _get_learning_unit(course_level, current_week, tier)
        if not learning_unit and path == PATH_REMEDIAL:
            # Defensive fallback: missing Remedial LU → serve Core for this week.
            tier = TIER_BY_WEEK.get(current_week, DEFAULT_TIER)
            learning_unit = _get_learning_unit(course_level, current_week, tier)
            path = PATH_CORE

        if not learning_unit:
            return {"success": False, "status": "no_content_for_week",
                    "error_detail": f"No content found for week {current_week}"}

        progress = _get_or_create_sp_progress(student_id, course_level, current_week, tier, learning_unit)
        progress_data = frappe.db.get_value(
            "StudentStageProgress", progress,
            ["name", "student", "stage", "status", "current_week", "current_tier",
             "current_content_index", "is_on_remedial", "remedial_attempts",
             "active_content_type", "active_content_id", "content_started_at",
             "active_quiz_attempt", "question_started_at", "course_context"],
            as_dict=True,
        )

        # Check if course complete
        if progress_data.get("status") == "completed":
            return {
                "success": True,
                "status": "course_complete",
                "user_message": "You have completed the program.",
                "student_id": student_id,
            }

        # Check for active quiz — flat shape per Rules 2 + 3.
        if progress_data.get("active_quiz_attempt"):
            return {
                "success": True,
                "status": "quiz_in_progress",
                "student_id": student_id,
                "has_active_quiz": True,
                "quiz_attempt_id": progress_data["active_quiz_attempt"],
                "position_week": cint(progress_data["current_week"]),
                "position_tier": progress_data["current_tier"],
                "position_learning_unit": progress_data["stage"],
                "position_is_remedial": bool(progress_data.get("is_on_remedial")),
                "position_path": path,
                "content_type": "Quiz",
                "content_id": progress_data.get("active_content_id"),
            }

        # ── SSP-canonical auto-correct (task #71 + CR-008 extension 2026-05-23) ──
        # The legacy trigger only caught LU drift ("stage mismatch"). It missed
        # two real divergence scenarios:
        #
        #   1. SSP.current_week is behind PE.current_week. complete_content
        #      bumps SSP.current_week in its advance branch, but the legacy
        #      cursor can lag if PE advanced via T14 without an immediate
        #      complete_content call.
        #
        #   2. New week just started (pe.weekly_video_done = 0, set by T14
        #      as the lazy-reset trigger signal) AND SSP.current_content_index
        #      has advanced past 0 from a previous flow. The student needs the
        #      FIRST content item of the new week (typically the VideoClass).
        #      Without this reset, the API serves item N — silently skipping
        #      items 0..N-1. Observed in ST00051359's case where SSP had
        #      content_index=1 (Quiz) while pe.weekly_video_done=0 (no W2
        #      video watched yet).
        #
        # All three conditions converge on the same fix: align SSP to PE +
        # reset content_index to 0. Atomic via a single set_value.
        ssp_week = cint(progress_data.get("current_week") or 0)
        ssp_content_index = cint(progress_data.get("current_content_index") or 0)
        new_week_no_video_yet = (
            not bool(pe.weekly_video_done) and ssp_content_index > 0
        )
        needs_reset = (
            progress_data["stage"] != learning_unit
            or ssp_week != current_week
            or new_week_no_video_yet
        )

        if needs_reset:
            frappe.db.set_value("StudentStageProgress", progress_data["name"], {
                "stage": learning_unit,
                "current_week": current_week,                # NEW: align to PE
                "current_tier": tier,
                "is_on_remedial": 1 if tier == REMEDIAL_TIER else 0,
                "current_content_index": 0,
                "last_activity_timestamp": now_datetime(),
            })
            # Removed mid-handler commit per L-017 — Frappe commits at request-end.
            progress_data["stage"] = learning_unit
            progress_data["current_week"] = current_week
            progress_data["current_tier"] = tier
            progress_data["current_content_index"] = 0

        # Get content items for current LU
        content_items = _get_content_items(progress_data["stage"])
        current_index = cint(progress_data["current_content_index"])

        if current_index < len(content_items):
            item = content_items[current_index]
            lu_info = _get_learning_unit_info(progress_data["stage"])

            # Mark content as started
            frappe.db.set_value("StudentStageProgress", progress_data["name"], {
                "active_content_type": item["content_type"],
                "active_content_id": item["content_id"],
                "content_started_at": now_datetime(),
                "status": "in_progress",
                "last_activity_timestamp": now_datetime(),
            })
            # Removed mid-handler commit per L-017 — Frappe commits at request-end.

            return _flat_content_response(
                item=item,
                content_index=current_index,
                total_in_unit=len(content_items),
                position_kwargs={
                    "week": cint(progress_data["current_week"]),
                    "tier": progress_data["current_tier"],
                    "learning_unit": progress_data["stage"],
                    "learning_unit_name": lu_info["name"] if lu_info else None,
                    "is_remedial": bool(progress_data.get("is_on_remedial")),
                    "path": path,
                },
            )

        # Current LU exhausted — try next LU in same week/tier
        next_lu = _get_next_learning_unit(course_level, current_week, tier, progress_data["stage"])
        if next_lu:
            frappe.db.set_value("StudentStageProgress", progress_data["name"], {
                "stage": next_lu,
                "current_content_index": 0,
                "status": "in_progress",
                "active_content_type": None,
                "active_content_id": None,
                "content_started_at": None,
                "last_activity_timestamp": now_datetime(),
            })
            # Removed mid-handler commit per L-017 — Frappe commits at request-end.

            content_items = _get_content_items(next_lu)
            if content_items:
                item = content_items[0]
                lu_info = _get_learning_unit_info(next_lu)
                return _flat_content_response(
                    item=item,
                    content_index=0,
                    total_in_unit=len(content_items),
                    position_kwargs={
                        "week": current_week,
                        "tier": tier,
                        "learning_unit": next_lu,
                        "learning_unit_name": lu_info["name"] if lu_info else None,
                        "is_remedial": tier == REMEDIAL_TIER,
                        "path": path,
                    },
                    new_learning_unit=True,
                )

        # Week complete — check if programme finished
        total_weeks = batch.total_weeks or 0
        if current_week >= total_weeks:
            frappe.db.set_value("StudentStageProgress", progress_data["name"], {
                "status": "completed",
                "last_activity_timestamp": now_datetime(),
            })
            # Removed mid-handler commit per L-017 — Frappe commits at request-end.
            return {
                "success": True,
                "status": "course_complete",
                "user_message": "Congratulations! You have completed the program.",
                "student_id": student_id,
                "completed_week": current_week,
            }

        # Week complete but more weeks remain — see comments inline.
        this_week_submitted = _has_submitted_week(student.name, current_week)
        next_week = current_week + 1
        max_allowed_week = min(calendar_week + 1, total_weeks)

        if this_week_submitted and next_week <= max_allowed_week:
            frappe.db.set_value("StudentStageProgress", progress_data["name"], {
                "current_week": next_week,
                "current_content_index": 0,
                "active_content_type": None,
                "active_content_id": None,
                "content_started_at": None,
                "last_activity_timestamp": now_datetime(),
            })
            # Removed mid-handler commit per L-017 — Frappe commits at request-end.
            return {
                "success": True,
                "status": "stage_complete",
                "user_message": f"Week {current_week} complete! Moving to Week {next_week}.",
                "student_id": student_id,
                "completed_week": current_week,
                "next_week": next_week,
                "can_advance": True,
                "total_weeks": total_weeks,
                "course_level": course_level,
            }

        if this_week_submitted and next_week > max_allowed_week:
            return {
                "success": True,
                "status": "stage_complete",
                "user_message": f"Week {current_week} complete! New content will be available in Week {next_week}.",
                "student_id": student_id,
                "completed_week": current_week,
                "can_advance": False,
                "paused": True,
                "paused_reason": "ahead_of_calendar",
                "next_week": next_week,
                "calendar_week": calendar_week,
                "total_weeks": total_weeks,
                "course_level": course_level,
            }

        # Student hasn't submitted yet
        return {
            "success": True,
            "status": "stage_complete",
            "user_message": f"Week {current_week} complete! Submit your assignment to continue.",
            "student_id": student_id,
            "completed_week": current_week,
            "can_advance": False,
            "needs_submission": True,
            "total_weeks": total_weeks,
            "course_level": course_level,
        }

    except Exception as e:
        # BR-003: rollback-first + flat error (L-030 cascade fix). Was:
        # log_error on the poisoned txn → InFailedSqlTransaction → Glific 400.
        return safe_sp_api_error_response(e, "get_next_content", student_id=student_id)


# ============================================================
# API 6: GET CONTENT DETAILS
# ============================================================

@frappe.whitelist(allow_guest=False)
@glific_response
def get_content_details(content_type, content_id, language=None,
                        student_id=None, **_glific_kwargs):
    """
    `**_glific_kwargs` absorbs Glific-injected fields per task #89 — ignored.

    Get detailed information about a specific content item.
    Returns type-specific payload:
      - VideoClass: youtube_url, plio_url, video_file + translations
      - Quiz: question_count, passing_score, time_limit
      - NoteContent: content text
      - Assignment: description, assignment_type
      - CourseProject: description

    Called by: Glific after get_next_content returns a content item,
    to fetch the actual media URL or quiz config.

    Args:
        content_type: DocType name (VideoClass, Quiz, etc.)
        content_id: Document name
        language: Optional language code for translation lookup
        student_id: Optional student identifier. When provided for VideoClass,
            includes unguided submission text fields for the student's active
            enrollment.

    Returns:
        dict with type-specific content details
    """
    try:
        if not content_type or not content_id:
            return {"success": False, "status": "invalid_input",
                    "error_detail": "content_type and content_id are required"}

        # CR-024 Layer 3: reject Glific placeholder strings on identifier params.
        # content_type and content_id are identifier params (DB keys).
        # student_id and language are NOT checked — student_id is optional and
        # language is free-text, not an identifier.
        placeholder_hit = check_glific_placeholders(
            [("content_type", content_type), ("content_id", content_id)],
            api_name="get_content_details",
            student_id=student_id,
        )
        if placeholder_hit:
            return placeholder_hit

        if content_type not in VALID_CONTENT_TYPES:
            return {"success": False, "status": "invalid_content_type",
                    "error_detail": f"Invalid content_type: {content_type}"}

        if content_type in ("Assignment", "Quiz"):
            content_id = normalize_unicode_surrogates(content_id)

        if not frappe.db.exists(content_type, content_id):
            return {"success": False, "status": "not_found",
                    "error_detail": f"{content_type} not found: {content_id}"}

        # CR-024 Phase 2: replace frappe.get_doc with in-process read cache for
        # immutable master-data fields (ADR-006 Revision). Per-student/per-language
        # bits (translation selection, unguided_text) are still resolved LIVE below.
        #
        # Task #45 (2026-05-22): _resolve_content_language is called only in the
        # VideoClass branch — other branches don't use language, so skipping it
        # there saves 1 DB hit per non-Video call.
        payload = cached_content_details_payload(content_type, content_id)
        if "error" in payload:
            # Cache raised on load (e.g. doc not found); surface as not_found.
            return {"success": False, "status": "not_found",
                    "error_detail": f"{content_type} not found: {content_id}"}

        if content_type == "VideoClass":
            language = _resolve_content_language(language, student_id)
            # Assessments from cached payload. Cap at 5 (ADR-006: immutable).
            assessments = payload.get("assessments") or []
            ASSESSMENT_CAP = 5
            if len(assessments) > ASSESSMENT_CAP:
                frappe.log_error(
                    f"VideoClass {content_id} has {len(assessments)} assessments; "
                    f"truncating to {ASSESSMENT_CAP}.",
                    "SP API contract",
                )
                assessments = assessments[:ASSESSMENT_CAP]

            result = {
                "success": True,
                "status": "video_class",
                "content_type": "VideoClass",
                "content_id": content_id,
                "name": payload.get("name"),
                "youtube_url": payload.get("youtube_url"),
                "plio_url": payload.get("plio_url"),
                "video_file": payload.get("video_file"),
                "url": (payload.get("youtube_url") or payload.get("plio_url")
                        or payload.get("video_file")),
                "duration": payload.get("duration"),
                "description": payload.get("description"),
                "translated": False,
                "language": "",
                "assessment_count": len(assessments),
            }
            for i, a in enumerate(assessments, start=1):
                result[f"assessment_{i}_type"] = a.get("assessment_type")
                result[f"assessment_{i}_id"] = a.get("assessment_id")
            # Overlay per-language translation LIVE from cached translations list.
            for trans in payload.get("translations") or []:
                if trans.get("language") == language:
                    if trans.get("translated_name"):
                        result["name"] = trans["translated_name"]
                    if trans.get("video_youtube_url"):
                        result["youtube_url"] = trans["video_youtube_url"]
                        result["url"] = trans["video_youtube_url"]
                    result["translated"] = True
                    result["language"] = language
                    break
            # Overlay per-student unguided_text LIVE (never cached).
            if student_id:
                unguided = _get_video_unguided_submission_message(
                    student_id, assessments, language
                )
                result["unguided_text"] = unguided.get("unguided_text")
                result["unguided_text_url"] = unguided.get("unguided_text_url")
            return result

        elif content_type == "Quiz":
            return {
                "success": True,
                "status": "quiz",
                "content_type": "Quiz",
                "content_id": content_id,
                "name": payload.get("name"),
                "total_questions": payload.get("total_questions", 0),
                "passing_score": payload.get("passing_score", 60.0),
                "time_limit": payload.get("time_limit"),
            }

        elif content_type == "NoteContent":
            return {
                "success": True,
                "status": "note_content",
                "content_type": "NoteContent",
                "content_id": content_id,
                "name": payload.get("name"),
                "content": payload.get("content"),
            }

        elif content_type == "Assignment":
            return {
                "success": True,
                "status": "assignment",
                "content_type": "Assignment",
                "content_id": content_id,
                "name": payload.get("name"),
                "description": payload.get("description"),
                "assignment_type": payload.get("assignment_type"),
            }

        elif content_type == "CourseProject":
            return {
                "success": True,
                "status": "course_project",
                "content_type": "CourseProject",
                "content_id": content_id,
                "name": payload.get("name"),
                "description": payload.get("description"),
            }

        # TextMessageContent, VoiceNoteContent, ParentCallConfig — minimal
        return {
            "success": True,
            "status": "generic_content",
            "content_type": content_type,
            "content_id": content_id,
            "name": _get_content_display_name(content_type, content_id),
        }

    except Exception as e:
        return safe_sp_api_error_response(e, "get_content_details", student_id=student_id)


# ============================================================
# API 7: COMPLETE CONTENT (Non-Quiz)
# ============================================================

@frappe.whitelist(allow_guest=False)
@glific_response
def complete_content(student_id, course_level, content_type, content_id,
                     **_glific_kwargs):
    """
    Mark non-quiz content as complete and advance to next item.

    `**_glific_kwargs` absorbs Glific-injected fields per task #89 — ignored.
    For Quiz content, use start_quiz / submit_answer instead.

    Called by: Glific after student views a video, reads a note, etc.

    Args:
        student_id: Student ID, Glific ID, or phone
        course_level: Course Level document name
        content_type: Content DocType (VideoClass, NoteContent, etc.)
        content_id: Content document name

    Returns:
        dict with next_content / next_learning_unit / week_complete /
        course_complete action and next content details.
    """
    try:
        if not all([student_id, course_level, content_type, content_id]):
            return {"success": False, "status": "invalid_input",
                    "error_detail": "All parameters required"}

        # CR-024 Layer 3: reject Glific placeholder strings on identifier params.
        # student_id, course_level, content_type, content_id are identifier params.
        # ENTRY GUARD ONLY — do NOT modify SSP alignment, dedup, points, or
        # _advance_to_next_content logic (P-001 / L-010 atomicity preserved).
        placeholder_hit = check_glific_placeholders(
            [
                ("student_id", student_id),
                ("course_level", course_level),
                ("content_type", content_type),
                ("content_id", content_id),
            ],
            api_name="complete_content",
            student_id=student_id,
        )
        if placeholder_hit:
            return placeholder_hit

        course_level = normalize_unicode_surrogates(course_level)
        if content_type in ("Assignment", "Quiz"):
            content_id = normalize_unicode_surrogates(content_id)

        if content_type == "Quiz":
            return {"success": False, "status": "wrong_endpoint",
                    "error_detail": "Use start_quiz and submit_answer for Quiz content"}

        student_id = _resolve_student_id(student_id)
        if not student_id:
            return {"success": False, "status": "not_found",
                    "error_detail": "Student not found"}

        progress_data = frappe.db.get_value(
            "StudentStageProgress",
            {"student": student_id, "course_context": course_level, "stage_type": "LearningUnit"},
            ["name", "student", "stage", "current_week", "current_tier",
             "current_content_index", "is_on_remedial", "remedial_attempts",
             "content_started_at", "course_context"],
            as_dict=True,
        )

        if not progress_data:
            return {"success": False, "status": "no_progress",
                    "error_detail": "No progress record found. Call get_next_content first."}

        # ── PE-canonical SSP auto-correct (CR-023, mirrors get_next_content) ──
        # The state machine owns the truth (PE.current_week / current_path /
        # current_tier); SSP is DERIVED and can lag a week boundary — e.g. PE
        # advanced via T14 but no get_next_content call has realigned SSP yet.
        # Without this, complete_content validates the incoming content_id
        # against a STALE SSP.stage and fails with content_mismatch even though
        # the student is legitimately on the new week; worse, the stale stage
        # would flow into _advance_to_next_content + the SCL enqueue below,
        # writing cross-week StudentContentLog rows. Align SSP to PE BEFORE
        # reading content_items. (Loud content_mismatch for a genuinely wrong
        # content_id is preferred over silently passing stale SSP through.)
        #
        # MUST stay in sync with the get_next_content SSP-canonical block (L-074):
        # both endpoints read SSP.stage and both require PE alignment first.
        #
        # Two checks get_next_content has are DELIBERATELY omitted here (L-074:
        # enumerate the intentional differences, don't silently let the blocks
        # diverge):
        #   - _is_week_advancement_pending: not needed. If T14 is queued but
        #     unrun, pe.current_week is still the OLD week, so we align SSP to
        #     the old week and the completion proceeds normally; T14 later sets
        #     weekly_video_done=0 which re-triggers the reset on the next call.
        #   - content-blocking (prior-week submission gate): complete_content
        #     MARKS content done, it does not GATE access. A blocked student
        #     never got a valid content_id for the blocked week (get_next_content
        #     returns content_blocked), so a stale id here fails content_mismatch.
        student = frappe.get_doc("Student", student_id)
        batch, bpr = _get_active_bpr_for_student(student)
        if not batch or not bpr:
            return {"success": False, "status": "no_active_batch",
                    "error_detail": "No active Summer Program batch found"}

        pe = get_active_pe(student.name, batch.name)
        if not pe:
            return {"success": False, "status": "no_active_pe",
                    "error_detail": "No active ProgramEnrollment for this student"}

        current_week = pe.current_week or _get_effective_week(
            student, batch, _get_current_week(batch))
        path = pe.current_path or PATH_CORE
        tier = pe.current_tier or (
            REMEDIAL_TIER if path == PATH_REMEDIAL
            else TIER_BY_WEEK.get(current_week, DEFAULT_TIER)
        )

        learning_unit = _get_learning_unit(course_level, current_week, tier)
        if not learning_unit and path == PATH_REMEDIAL:
            # Defensive fallback: missing Remedial LU → serve Core for this week.
            tier = TIER_BY_WEEK.get(current_week, DEFAULT_TIER)
            learning_unit = _get_learning_unit(course_level, current_week, tier)
            path = PATH_CORE
        if not learning_unit:
            return {"success": False, "status": "no_content_for_week",
                    "error_detail": f"No content found for week {current_week}"}

        # Align SSP to PE + reset content_index to 0 (identical conditions to
        # the get_next_content auto-correct: LU drift, week lag, or new-week-
        # no-video-yet). Atomic via a single set_value, then mirror into the
        # in-memory progress_data so content_items + the SCL enqueue use the
        # aligned values.
        ssp_week = cint(progress_data.get("current_week") or 0)
        ssp_content_index = cint(progress_data.get("current_content_index") or 0)
        new_week_no_video_yet = (
            not bool(pe.weekly_video_done) and ssp_content_index > 0
        )
        needs_reset = (
            progress_data["stage"] != learning_unit
            or ssp_week != current_week
            or new_week_no_video_yet
        )
        if needs_reset:
            frappe.db.set_value("StudentStageProgress", progress_data["name"], {
                "stage": learning_unit,
                "current_week": current_week,
                "current_tier": tier,
                "is_on_remedial": 1 if tier == REMEDIAL_TIER else 0,
                "current_content_index": 0,
                "last_activity_timestamp": now_datetime(),
            })
            progress_data["stage"] = learning_unit
            progress_data["current_week"] = current_week
            progress_data["current_tier"] = tier
            progress_data["current_content_index"] = 0

        # Validate content matches current position
        content_items = _get_content_items(progress_data["stage"])
        current_index = cint(progress_data["current_content_index"])

        if current_index >= len(content_items):
            return {"success": False, "status": "no_content_at_position",
                    "error_detail": "No content at current position"}

        current_item = content_items[current_index]

        # Task #18 (2026-05-28, Content R3): SCL duplicate-suppression.
        # Webhook retries / double-taps from Glific can fire complete_content
        # twice for the same (student, content_id). Two dedup layers:
        #
        #   (a) Have we already advanced past this content? If so, the prior
        #       call's _advance_to_next_content has already updated progress
        #       and bumped points — return idempotent success without
        #       recreating the SCL insert or the activity_points handler.
        #       This catches sequential retries (seconds apart).
        #
        #   (b) Has an SCL row for this (student, content_id, completed) been
        #       inserted in the last 5 minutes? Catches the case where the
        #       prior worker has inserted the SCL but progress hasn't yet
        #       reflected the advance (cross-transaction visibility window).
        #
        # Neither check is bulletproof against truly simultaneous concurrent
        # calls (both pre-checks pass on stale reads, both enqueue). A unique
        # index on (student, content_id, action) would close that final gap;
        # deferred per Content R3 because it requires schema migration and
        # would also reject legitimate re-completes of optional repeatable
        # content. The two-layer check above is the MVP-acceptable mitigation.

        # Layer (a): position-passed check.
        already_completed_ids = {
            item["content_id"] for item in content_items[:current_index]
        }
        if content_id in already_completed_ids:
            frappe.logger().info(
                f"complete_content dedup (a): {content_id} already advanced "
                f"past for student={student_id} "
                f"(current_index={current_index}); idempotent return."
            )
            return {
                "success": True,
                "status": "already_completed",
                "user_message": "Content already marked complete.",
                "content_type": content_type,
                "content_id": content_id,
            }

        if current_item["content_id"] != content_id:
            return {
                "success": False,
                "status": "content_mismatch",
                "error_detail": f"Content mismatch. Expected: {current_item['content_id']}, Got: {content_id}",
            }

        # Layer (b): recent-SCL check.
        recent_dup = frappe.db.sql(
            """
            SELECT name FROM "tabStudentContentLog"
             WHERE student = %s
               AND content_id = %s
               AND content_type = %s
               AND action = 'completed'
               AND creation > NOW() AT TIME ZONE 'UTC' - INTERVAL '5 minutes'
             LIMIT 1
            """,
            (student_id, content_id, content_type),
            as_dict=True,
        )
        if recent_dup:
            frappe.logger().info(
                f"complete_content dedup (b): recent SCL "
                f"{recent_dup[0]['name']} for student={student_id} "
                f"content={content_id} within 5min; idempotent return."
            )
            return {
                "success": True,
                "status": "already_completed",
                "user_message": "Content already marked complete.",
                "content_type": content_type,
                "content_id": content_id,
            }

        # Calculate time spent
        time_spent = 0
        if progress_data.get("content_started_at"):
            time_spent = cint(_time_diff_in_seconds(now_datetime(), progress_data["content_started_at"]))

        points_awarded = None
        log_metadata = None
        if content_type == "VideoClass":
            # Video completion owns ProgramEnrollment activity fields here.
            # The background log job still creates StudentContentLog, but it
            # gets a marker so the SCL after_insert hook does not double-award.
            from tap_lms.summer_program.activity_points import award_video_completion_points
            points_awarded = award_video_completion_points(
                student_id=student_id,
                video_id=content_id,
                trigger_source="complete_content",
            )
            if points_awarded is not None:
                log_metadata = {
                    "activity_points_awarded_by": "complete_content",
                }

        frappe.enqueue(
            "tap_lms.summer_program.background_jobs.job_log_content_completion",
            queue="short",
            timeout=60,
            enqueue_after_commit=True,
            student_id=student_id,
            course_level=course_level,
            progress_name=progress_data["name"],
            content_type=content_type,
            content_id=content_id,
            action="completed",
            time_spent_seconds=time_spent,
            stage_no=progress_data["current_week"],
            tier=progress_data["current_tier"],
            learning_unit=progress_data["stage"],
            points_awarded=points_awarded,
            metadata=log_metadata,
        )

        # Update statistics
        frappe.enqueue(
            "tap_lms.summer_program.background_jobs.job_update_statistics",
            queue="short",
            timeout=30,
            enqueue_after_commit=True,
            progress_name=progress_data["name"],
            content_completed=1,
            time_spent=time_spent,
        )

        # Advance to next content
        return _advance_to_next_content(progress_data, course_level)

    except Exception as e:
        return safe_sp_api_error_response(e, "complete_content", student_id=student_id)


def _advance_to_next_content(progress_data, course_level):
    """Move to next content item within LU, or next LU, or signal week complete.

    Returns a flat dict per docs/api-standard-glific.md (Rules 2 + 3):
      - status: "next_content" | "next_learning_unit" | "week_complete"
      - next-content fields flattened to next_content_type / next_content_id /
        next_content_name / next_content_order (no nested object)
      - progress fields flattened to progress_completed / progress_total /
        progress_percentage
    """
    course_level = normalize_unicode_surrogates(course_level)
    current_index = cint(progress_data["current_content_index"])
    new_index = current_index + 1
    content_items = _get_content_items(progress_data["stage"])

    if new_index < len(content_items):
        # More content in current LU
        frappe.db.set_value("StudentStageProgress", progress_data["name"], {
            "current_content_index": new_index,
            "status": "in_progress",
            "active_content_type": None,
            "active_content_id": None,
            "content_started_at": None,
            "last_activity_timestamp": now_datetime(),
        })
        # Removed mid-handler commit per L-017 — Frappe commits at request-end.

        next_item = content_items[new_index]
        return {
            "success": True,
            "status": "next_content",
            "user_message": "Content completed!",
            "next_content_type": next_item["content_type"],
            "next_content_id": next_item["content_id"],
            "next_content_name": next_item["content_name"],
            "next_content_order": new_index + 1,
            "progress_completed": new_index,
            "progress_total": len(content_items),
            "progress_percentage": round((new_index / len(content_items)) * 100, 1),
        }

    # Current LU exhausted — try next LU in same week/tier
    current_week = cint(progress_data["current_week"])
    tier = progress_data["current_tier"]

    next_lu = _get_next_learning_unit(course_level, current_week, tier, progress_data["stage"])
    if next_lu:
        frappe.db.set_value("StudentStageProgress", progress_data["name"], {
            "stage": next_lu,
            "current_content_index": 0,
            "active_content_type": None,
            "active_content_id": None,
            "content_started_at": None,
            "last_activity_timestamp": now_datetime(),
        })
        # Removed mid-handler commit per L-017 — Frappe commits at request-end.

        content_items = _get_content_items(next_lu)
        first_content = content_items[0] if content_items else None
        lu_info = _get_learning_unit_info(next_lu)

        return {
            "success": True,
            "status": "next_learning_unit",
            "user_message": "Learning Unit completed!",
            "new_learning_unit": next_lu,
            "new_learning_unit_name": lu_info["name"] if lu_info else None,
            "next_content_type": first_content["content_type"] if first_content else None,
            "next_content_id": first_content["content_id"] if first_content else None,
            "next_content_name": first_content["content_name"] if first_content else None,
            "next_content_order": 1 if first_content else 0,
        }

    # Week complete
    return {
        "success": True,
        "status": "week_complete",
        "user_message": f"Week {current_week} content complete!",
        "completed_week": current_week,
    }


# ============================================================
# API 8: START QUIZ
# ============================================================

@frappe.whitelist(allow_guest=False)
@glific_response
def start_quiz(student_id, course_level, quiz_id, language=None,
               **_glific_kwargs):
    """
    Begin a quiz attempt or resume an existing in-progress attempt.
    Returns the first (or current) question with options A/B/C/D.

    `**_glific_kwargs` absorbs Glific-injected fields per task #89 — ignored.

    Called by: Glific quiz sub-flow after get_next_content returns
    a Quiz content item.

    Args:
        student_id: Student ID, Glific ID, or phone
        course_level: Course Level document name
        quiz_id: Quiz document name
        language: Ignored. Quiz language is resolved from ProgramEnrollment
                  first, then Student.language.

    Returns:
        dict with quiz_started / quiz_resumed status, quiz_attempt_id,
        and first question details.
    """
    try:

        if not all([student_id, course_level, quiz_id]):
            return {"success": False, "status": "invalid_input",
                    "error_detail": "student_id, course_level, and quiz_id required"}

        # CR-024 Layer 3: reject Glific placeholder strings on identifier params.
        placeholder_hit = check_glific_placeholders(
            [
                ("student_id", student_id),
                ("course_level", course_level),
                ("quiz_id", quiz_id),
            ],
            api_name="start_quiz",
            student_id=student_id,
        )
        if placeholder_hit:
            return placeholder_hit

        course_level = normalize_unicode_surrogates(course_level)
        quiz_id = normalize_unicode_surrogates(quiz_id)

        student_id = _resolve_student_id(student_id)
        if not student_id:
            return {"success": False, "status": "not_found",
                    "error_detail": "Student not found"}
        language = _get_language_for_student(student_id, course_level)

        if not frappe.db.exists("Quiz", quiz_id):
            return {"success": False, "status": "quiz_not_found",
                    "error_detail": f"Quiz not found: {quiz_id}"}

        progress_data = frappe.db.get_value(
            "StudentStageProgress",
            {"student": student_id, "course_context": course_level, "stage_type": "LearningUnit"},
            ["name", "stage", "current_week", "current_tier", "current_content_index",
             "is_on_remedial", "active_quiz_attempt"],
            as_dict=True,
        )

        if not progress_data:
            return {"success": False, "status": "no_progress",
                    "error_detail": "No progress record. Call get_next_content first."}

        # Resume existing in-progress attempt
        if progress_data.get("active_quiz_attempt"):
            attempt = frappe.get_doc("StudentQuizAttempt", progress_data["active_quiz_attempt"])
            if attempt.quiz == quiz_id and attempt.status == "in_progress":
                return _resume_quiz(attempt, progress_data, language)

        # Create new attempt
        quiz_doc = frappe.get_doc("Quiz", quiz_id)
        questions = _get_quiz_questions(quiz_doc)
        if not questions:
            return {"success": False, "status": "empty_quiz",
                    "error_detail": "Quiz has no questions"}

        prev_attempts = frappe.db.count("StudentQuizAttempt", {
            "student": student_id, "quiz": quiz_id, "course_level": course_level,
        })

        attempt = frappe.get_doc({
            "doctype": "StudentQuizAttempt",
            "student": student_id,
            "course_level": course_level,
            "student_progress": progress_data["name"],
            "quiz": quiz_id,
            "quizname": getattr(quiz_doc, 'quiz_name', quiz_id),
            "stage_no": progress_data["current_week"],
            "tier": progress_data["current_tier"],
            "attempt_number": prev_attempts + 1,
            "status": "in_progress",
            "total_questions": len(questions),
            "current_question_index": 0,
            "question_started_at": now_datetime(),
            "started_at": now_datetime(),
            "passing_score": flt(getattr(quiz_doc, 'passing_score', 60)),
            "score": 0,
            "correct_answers": 0,
            "passed": 0,
            "answers": [],
        })
        attempt.insert(ignore_permissions=True)
        # Removed mid-handler commit per L-017 — Frappe commits at request-end.

        # Update progress
        frappe.db.set_value("StudentStageProgress", progress_data["name"], {
            "active_quiz_attempt": attempt.name,
            "active_content_type": "Quiz",
            "active_content_id": quiz_id,
            "content_started_at": now_datetime(),
            "question_started_at": now_datetime(),
            "last_activity_timestamp": now_datetime(),
        })
        # Removed mid-handler commit per L-017 — Frappe commits at request-end.

        # CR-024 Phase 2: use in-process cache for immutable question payload.
        first_q = cached_question_details(questions[0].question, language)
        # Flat shape per docs/api-standard-glific.md (Rules 2 + 3): no nested
        # question/options objects. Glific reads `question_text`, `option_a`,
        # etc. directly. Question is at index 1 (1-based).
        response = {
            "success": True,
            "status": "quiz_started",
            "user_message": "Quiz started! Good luck!",
            "quiz_attempt_id": attempt.name,
            "quiz_name": attempt.quizname,
            "total_questions": len(questions),
            "passing_score": attempt.passing_score,
            "question_index": 1,
            "question_id": questions[0].question,
            "question_text": first_q.get("question"),
            "question_type": first_q.get("question_type", "Multiple Choice"),
            "correct_option": first_q.get("correct_option"),
        }
        response.update(_option_fields(first_q))
        return response

    except Exception as e:
        return safe_sp_api_error_response(e, "start_quiz", student_id=student_id)


def _resume_quiz(attempt, progress_data, language=None):
    """Resume an in-progress quiz attempt."""
    quiz_id = normalize_unicode_surrogates(attempt.quiz)
    quiz_doc = frappe.get_doc("Quiz", quiz_id)
    questions = _get_quiz_questions(quiz_doc)

    answered_indices = {cint(a.question_index) for a in attempt.answers}
    next_index = None
    for i in range(1, len(questions) + 1):
        if i not in answered_indices:
            next_index = i
            break

    # All questions already answered — complete the quiz instead of re-serving
    if next_index is None:
        return _complete_quiz_sp(attempt, quiz_doc, questions, language)

    frappe.db.set_value("StudentStageProgress", progress_data["name"], {
        "question_started_at": now_datetime(),
        "last_activity_timestamp": now_datetime(),
    })
    attempt.question_started_at = now_datetime()
    attempt.save(ignore_permissions=True)
    # Removed mid-handler commit per L-017 — Frappe commits at request-end.

    q_row = questions[next_index - 1]
    # CR-024 Phase 2: use in-process cache for immutable question payload.
    q_details = cached_question_details(q_row.question, language)
    correct_so_far = sum(1 for a in attempt.answers if a.is_correct)

    # Flat shape per docs/api-standard-glific.md (Rules 2 + 3).
    response = {
        "success": True,
        "status": "quiz_resumed",
        "user_message": f"Welcome back! Continuing from question {next_index}.",
        "quiz_attempt_id": attempt.name,
        "quiz_name": attempt.quizname,
        "total_questions": attempt.total_questions,
        "questions_answered": len(attempt.answers),
        "correct_so_far": correct_so_far,
        "question_index": next_index,
        "question_id": q_row.question,
        "question_text": q_details.get("question"),
        "question_type": q_details.get("question_type", "Multiple Choice"),
        "correct_option": q_details.get("correct_option"),
    }
    response.update(_option_fields(q_details))
    return response


# ============================================================
# API 9: SUBMIT ANSWER
# ============================================================

@frappe.whitelist(allow_guest=False)
@glific_response
def submit_answer(student_id, quiz_attempt_id, question_index, answer,
                  language=None, **_glific_kwargs):
    """
    Submit an answer for the current quiz question.

    `**_glific_kwargs` absorbs Glific-injected fields per task #89 — ignored.

    On the last question, automatically completes the quiz and returns
    pass/fail result. Server-side time tracking per question.

    KEY DESIGN CHANGE vs old system:
      - Core quiz FAIL → advance to next content (no remedial switch)
      - Remedial quiz FAIL → restart or continue remedial LU
      - Remedial quiz PASS → advance (exit remedial for next week)
    Remedial path is driven by assignment submission, not quiz score.

    Args:
        student_id: Student ID, Glific ID, or phone
        quiz_attempt_id: StudentQuizAttempt document name
        question_index: 1-based question number
        answer: Selected option letter (A, B, C, or D)
        language: Ignored. Quiz language is resolved from ProgramEnrollment
                  first, then Student.language.

    Returns:
        dict with answer_result + next_question or quiz_passed/quiz_failed
    """
    try:
        if not all([student_id, quiz_attempt_id, question_index, answer]):
            return {"success": False, "status": "invalid_input",
                    "error_detail": "All parameters required"}

        # CR-024 Layer 3: reject Glific placeholder strings on identifier params.
        # student_id and quiz_attempt_id are identifier params (DB keys).
        # answer is free text (A/B/C/D from student) — intentionally NOT checked.
        placeholder_hit = check_glific_placeholders(
            [("student_id", student_id), ("quiz_attempt_id", quiz_attempt_id)],
            api_name="submit_answer",
            student_id=student_id,
        )
        if placeholder_hit:
            return placeholder_hit

        question_index = cint(question_index)
        answer = answer.strip().upper()
        if answer not in OPTION_LETTERS:
            return {"success": False, "status": "invalid_answer",
                    "error_detail": "Invalid answer. Must be A, B, C, or D"}

        student_id = _resolve_student_id(student_id)
        if not student_id:
            return {"success": False, "status": "not_found",
                    "error_detail": "Student not found"}

        if not frappe.db.exists("StudentQuizAttempt", quiz_attempt_id):
            return {"success": False, "status": "attempt_not_found",
                    "error_detail": f"Quiz attempt not found: {quiz_attempt_id}"}

        attempt = frappe.get_doc("StudentQuizAttempt", quiz_attempt_id)
        if attempt.student != student_id:
            return {"success": False, "status": "wrong_student",
                    "error_detail": "Attempt does not belong to this student"}
        language = _get_language_for_student(student_id, attempt.course_level)
        if attempt.status != "in_progress":
            return {"success": False, "status": "attempt_not_in_progress",
                    "error_detail": "Quiz attempt is not in progress"}
        if question_index < 1 or question_index > attempt.total_questions:
            return {"success": False, "status": "invalid_question_index",
                    "error_detail": f"Invalid question_index. Must be 1-{attempt.total_questions}"}

        quiz_id = normalize_unicode_surrogates(attempt.quiz)
        quiz_doc = frappe.get_doc("Quiz", quiz_id)
        questions = _get_quiz_questions(quiz_doc)
        q_row = questions[question_index - 1]

        # CR-024 Phase 2: use in-process cache for immutable question payload.
        # language is resolved from PE above; passing it here ensures the cached
        # entry is keyed consistently for this student's language.
        q_details = cached_question_details(q_row.question, language)
        correct_option = q_details.get("correct_option", "A")
        is_correct = (answer == correct_option)

        started_at = attempt.question_started_at or attempt.started_at
        answered_at = now_datetime()
        time_spent = cint(_time_diff_in_seconds(answered_at, started_at))

        # Save answer (update or append)
        existing_answer = None
        for ans in attempt.answers:
            if cint(ans.question_index) == question_index:
                existing_answer = ans
                break

        if existing_answer:
            existing_answer.selected_option = answer
            existing_answer.correct_option = correct_option
            existing_answer.is_correct = 1 if is_correct else 0
            existing_answer.started_at = started_at
            existing_answer.answered_at = answered_at
            existing_answer.time_spent_seconds = time_spent
        else:
            attempt.append("answers", {
                "question_index": question_index,
                "question": q_row.question,
                "selected_option": answer,
                "correct_option": correct_option,
                "is_correct": 1 if is_correct else 0,
                "started_at": started_at,
                "answered_at": answered_at,
                "time_spent_seconds": time_spent,
            })

        attempt.current_question_index = question_index
        attempt.correct_answers = sum(1 for a in attempt.answers if a.is_correct)

        # Last question → complete quiz
        if question_index >= attempt.total_questions:
            return _complete_quiz_sp(attempt, quiz_doc, questions, language)

        # More questions — save and return next
        attempt.question_started_at = now_datetime()
        attempt.save(ignore_permissions=True)
        # Removed mid-handler commit per L-017 — Frappe commits at request-end.

        progress_name = attempt.student_progress
        if progress_name:
            frappe.db.set_value("StudentStageProgress", progress_name, {
                "question_started_at": now_datetime(),
                "last_activity_timestamp": now_datetime(),
            })
            # Removed mid-handler commit per L-017 — Frappe commits at request-end.

        next_q_row = questions[question_index]  # 0-based → next question
        # CR-024 Phase 2: use in-process cache for immutable question payload.
        next_q = cached_question_details(next_q_row.question, language)

        # Flat shape per docs/api-standard-glific.md (Rules 2 + 3):
        #   - answer_result.* → answered_question_*, was_correct, time_spent_seconds
        #   - progress.* → progress_answered, progress_total, progress_correct, progress_percentage
        #   - question.* + nested options.* → question_*, option_a..option_d
        response = {
            "success": True,
            "status": "next_question",
            "answered_question_index": question_index,
            "selected_answer": answer,
            "correct_answer": correct_option,
            "was_correct": is_correct,
            "time_spent_seconds": time_spent,
            "progress_answered": question_index,
            "progress_total": attempt.total_questions,
            "progress_correct": attempt.correct_answers,
            "progress_percentage": round((question_index / attempt.total_questions) * 100, 1),
            "question_index": question_index + 1,
            "question_id": next_q_row.question,
            "question_text": next_q.get("question"),
            "question_type": next_q.get("question_type", "Multiple Choice"),
            "correct_option": next_q.get("correct_option"),
        }
        response.update(_option_fields(next_q))
        return response

    except Exception as e:
        return safe_sp_api_error_response(e, "submit_answer", student_id=student_id)


def _complete_quiz_sp(attempt, quiz_doc, questions, language=None):
    """
    Complete quiz attempt and determine next action.

    Quiz behavior is the SAME on both Core and Remedial paths:
      - Quiz pass or fail → advance to next content item
      - If no more content → week_complete
    Quiz results do NOT affect path routing. Only assignment submission
    (valid vs invalid type) determines Core/Remedial for the next week.
    """
    correct_count = sum(1 for a in attempt.answers if a.is_correct)
    total = attempt.total_questions
    score = (correct_count / total * 100) if total > 0 else 0
    passed = score >= flt(attempt.passing_score)

    total_time = cint(_time_diff_in_seconds(now_datetime(), attempt.started_at))

    # Finalize attempt
    attempt.status = "completed"
    attempt.completed_at = now_datetime()
    attempt.score = score
    attempt.correct_answers = correct_count
    attempt.passed = 1 if passed else 0
    attempt.time_spent_seconds = total_time
    attempt.save(ignore_permissions=True)
    # Removed mid-handler commit per L-017 — Frappe commits at request-end.

    # The on_update hook (quiz_points.handle_attempt_update) just fired inside
    # attempt.save() and wrote points_earned via frappe.db.set_value. That
    # bypasses the in-memory doc, so reload to pick up the new value for the
    # response below (where quiz_score now carries GAMIFICATION POINTS earned,
    # not the percentage — see CR 2026-05-22).
    attempt.reload()

    # Get progress
    progress_data = frappe.db.get_value(
        "StudentStageProgress", attempt.student_progress,
        ["name", "student", "stage", "current_week", "current_tier",
         "is_on_remedial", "remedial_attempts", "current_content_index", "course_context"],
        as_dict=True,
    )

    course_level = normalize_unicode_surrogates(progress_data["course_context"])
    quiz_id = normalize_unicode_surrogates(attempt.quiz)

    # Clear quiz state
    frappe.db.set_value("StudentStageProgress", progress_data["name"], {
        "active_quiz_attempt": None,
        "active_content_type": None,
        "active_content_id": None,
        "content_started_at": None,
        "question_started_at": None,
        "last_activity_timestamp": now_datetime(),
    })
    # Removed mid-handler commit per L-017 — Frappe commits at request-end.

    # Background jobs
    frappe.enqueue(
        "tap_lms.summer_program.background_jobs.job_log_content_completion",
        queue="short", timeout=60,
        student_id=progress_data["student"],
        course_level=course_level,
        progress_name=progress_data["name"],
        content_type="Quiz",
        content_id=quiz_id,
        action="completed" if passed else "failed",
        score=score, max_score=100, passed=passed,
        time_spent_seconds=total_time,
        quiz_attempt=attempt.name,
        stage_no=progress_data["current_week"],
        tier=progress_data["current_tier"],
        learning_unit=progress_data["stage"],
    )
    frappe.enqueue(
        "tap_lms.summer_program.background_jobs.job_update_statistics",
        queue="short", timeout=30,
        progress_name=progress_data["name"],
        content_completed=1,
        quiz_passed=1 if passed else 0,
        quiz_failed=0 if passed else 1,
        time_spent=total_time,
    )

    # Build base response — flat per docs/api-standard-glific.md (Rules 2 + 3).
    # Note: removed boolean `quiz_passed` field that previously collided with
    # the `status` enum value ("quiz_passed" / "quiz_failed"). The status enum
    # already conveys pass/fail unambiguously; flows read it via Split-by-Expression.
    last_ans = attempt.answers[-1] if attempt.answers else None
    response = {
        "success": True,
        "status": "quiz_passed" if passed else "quiz_failed",
        "user_message": "Great job! Quiz passed!" if passed
                        else "Quiz complete. Let's continue with the next content.",
        # answer_result.* flattened
        "answered_question_index": attempt.total_questions,
        "selected_answer": last_ans.selected_option if last_ans else None,
        "correct_answer": last_ans.correct_option if last_ans else None,
        "was_correct": bool(last_ans.is_correct) if last_ans else False,
        "time_spent_seconds": last_ans.time_spent_seconds if last_ans else 0,
        # quiz_result.* flattened with quiz_ prefix to avoid colliding with
        # next-content fields injected below.
        #
        # CR (2026-05-22): `quiz_score` now carries the GAMIFICATION POINTS
        # earned (StudentQuizAttempt.points_earned), NOT the percentage.
        # Glific flows display this as the user-facing quiz reward. The
        # percentage is still available as `quiz_score_percentage`.
        "quiz_score": int(attempt.points_earned or 0),
        "quiz_score_percentage": round(score, 1),
        "quiz_correct": correct_count,
        "quiz_total": total,
        "quiz_passing_score": attempt.passing_score,
        "quiz_time_spent_seconds": total_time,
    }

    # Progression: same for both Core and Remedial paths.
    # Quiz pass/fail → advance to next content or week_complete.
    # Path routing (Core vs Remedial) is determined solely by assignment
    # submission validity, not quiz results.
    #
    # _advance_to_next_content returns a flat dict with `status` and
    # next-content fields. Merge into our response, but our outer `status`
    # ("quiz_passed" / "quiz_failed") and `user_message` win — Glific reads
    # those for the quiz outcome and reads `next_action_status` for what to
    # do next (the merged child's status enum). The renamed key avoids the
    # confusion of a "next_action" key holding what is actually a status string.
    next_action = _advance_to_next_content(progress_data, course_level)
    next_action.pop("success", None)
    response["next_action_status"] = next_action.pop("status", "next_content")
    next_action.pop("user_message", None)  # quiz outcome msg wins over next-content msg
    response.update(next_action)

    return response


# ============================================================
# CORE LOGIC: PATH RESOLUTION
# ============================================================

def _resolve_path(student, batch, bpr, current_week):
    """
    Determine whether a student should be on Core or Remedial path
    for the current week.

    Path resolution is based on SUBMISSION TYPE VALIDITY, not presence:
      - No submission at all → Core path (escalation flow handles follow-up,
        content is blocked via get_next_content until they submit)
      - Submitted with VALID type → Core path
      - Submitted with WRONG/INVALID type → Remedial path

    For week 1, everyone starts on Core (no prior history to evaluate).
    """
    if current_week <= 1:
        return PATH_CORE

    archetype = student.archetype or "submitter"
    arm = student.experiment_arm or "default"

    # Check previous week's submission status AND validity
    prev_week = current_week - 1
    submission = _get_submission_validity(student.name, prev_week)

    if not submission["submitted"]:
        # No submission → stay on Core (escalation flow + content blocking
        # handle follow-up; Remedial is NOT triggered by missing submission)
        return PATH_CORE

    if submission["is_valid"]:
        # Valid submission type → Core
        return PATH_CORE

    # Invalid submission type (wrong type) → check if Remedial config exists
    config = _get_archetype_config(batch.name, arm, archetype, PATH_REMEDIAL)
    if config:
        return PATH_REMEDIAL

    # No Remedial config for this archetype → stay on Core
    return PATH_CORE


# ============================================================
# ARCHETYPE CONFIG HELPERS
# ============================================================

def _get_archetype_config(batch_name, arm, archetype, path):
    """
    Fetch ArchetypeConfig for a specific combination.
    Uses Redis cache to avoid repeated DB lookups.

    Args:
        batch_name: Batch document name
        arm: experiment arm (default, arm_a, arm_b)
        archetype: student archetype
        path: Core or Remedial

    Returns:
        ArchetypeConfig document or None
    """
    cache_key = f"archetype_config:{batch_name}:{arm}:{archetype}:{path}"
    # `expires=True` is load-bearing (2026-06-15 fix). set_value below uses
    # `expires_in_sec=3600` which writes to Redis only, not `frappe.local.cache`.
    # Without `expires=True` here, a prior cache-miss get_value pollutes local.cache
    # with `None`, and every subsequent get_value reads that stale `None` instead
    # of falling through to Redis — so the "1 hour cache" comment at line 1844 was
    # silently doing 0 caching, and the dispatcher hit the DB every tick per PE.
    # See frappe/utils/redis_wrapper.py:79-85 for the local.cache write logic.
    cached = frappe.cache().get_value(cache_key, expires=True)
    if cached:
        return cached

    config_name = frappe.db.get_value(
        "ArchetypeConfig",
        {
            "batch": batch_name,
            "experiment_arm": arm,
            "archetype": archetype,
            "path": path,
            "is_active": 1,
        },
        "name",
    )

    if not config_name:
        # Try with 'default' arm as fallback
        if arm != "default":
            config_name = frappe.db.get_value(
                "ArchetypeConfig",
                {
                    "batch": batch_name,
                    "experiment_arm": "default",
                    "archetype": archetype,
                    "path": path,
                    "is_active": 1,
                },
                "name",
            )

    if not config_name:
        return None

    config = frappe.get_doc("ArchetypeConfig", config_name)
    # Cache for 1 hour. ArchetypeConfig is essentially static admin-controlled
    # configuration (escalation timings, archetype rules per batch/arm/path).
    # At 100K students × per-tick dispatcher reads, the 5-minute TTL that was
    # here originally caused ~12x more DB hits than necessary for data that
    # rarely changes. Trade-off: after an admin edits an ArchetypeConfig row,
    # workers may use stale config for up to 1 hour. If that becomes a problem,
    # add an on_update hook on ArchetypeConfig that invalidates the cache key
    # for the affected (batch, arm, archetype, path) combination.
    frappe.cache().set_value(cache_key, config, expires_in_sec=3600)
    return config


def _get_week_rule(student, batch, week):
    """
    Get the WeekRule for a student's archetype/arm/week.
    Determines expected submission type and whether validation is on.
    """
    archetype = student.archetype or "submitter"
    arm = student.experiment_arm or "default"

    # Try Core config first (Core has the week rules for content delivery)
    config = _get_archetype_config(batch.name, arm, archetype, PATH_CORE)
    if not config:
        return None

    for rule in config.week_rules:
        if cint(rule.week) == cint(week):
            return {
                "week": rule.week,
                "expected_submission_type": rule.expected_submission_type,
                "submission_validation_enabled": rule.submission_validation_enabled,
            }

    return None


def _get_escalation_steps(student, batch, path=PATH_CORE):
    """
    Get escalation steps from ArchetypeConfig for this student.

    CR-003: step dicts now carry `escalation_type` (Select:
    help_note_a/help_note_b/voice_note/parent_call) instead of the previous
    `message_type` Data field. Glific reads `escalation_type` via the
    contact field pushed by the dispatcher to pick its per-channel branch
    in SP_Escalation. `parent_call` steps are routed by the dispatcher
    through `summer_program/vocallabs.py` and skip Glific entirely.

    Fix 2026-05-22 (task #68): added explicit `path` parameter. Previously
    this function hardcoded PATH_CORE, so dispatcher T8/T10 Remedial
    transitions and feedback_consumer_hook._escalation_points on Remedial
    submissions silently used Core's escalation cadence + point rewards.
    Diagnostic against palv2-test-BT52231 showed every Remedial-path
    ArchetypeConfig has divergent step counts vs Core (e.g.
    arm_b/fence_sitter/Remedial has 5 steps vs Core's 4), confirming the
    operator team intended path-aware lookup. Default remains PATH_CORE
    for back-compat with callers that pre-date Remedial-aware routing.

    Args:
        student: Student doc (or _dict with archetype + experiment_arm).
        batch:   Batch doc.
        path:    PATH_CORE | PATH_REMEDIAL. Defaults to PATH_CORE.

    Callers that handle Remedial states (pe_dispatcher's
    _get_escalation_steps_for_pe, feedback_consumer_hook._escalation_points)
    pass `pe.current_path` and apply a Core fallback themselves if the
    Remedial config has no steps configured.
    """
    archetype = student.archetype or "submitter"
    arm = student.experiment_arm or "default"

    config = _get_archetype_config(batch.name, arm, archetype, path)
    if not config or not config.escalation_steps:
        return []

    steps = []
    for step in config.escalation_steps:
        if step.is_active:
            steps.append({
                "escalation_order": step.escalation_order,
                "escalation_type": step.escalation_type or "help_note_a",
                "points_awarded": step.points_awarded or 0,
                "hours_after_previous": step.hours_after_previous or 24,
                "enable_voice_call": int(getattr(step, "enable_voice_call", 0) or 0),
                "nudge_config_override": getattr(step, "nudge_config_override", None) or None,
            })

    return sorted(steps, key=lambda s: s["escalation_order"])


# ============================================================
# SUBMISSION TRACKING
# ============================================================

def _has_submitted_week(student_id, week):
    """Check if student has a submission logged for a specific week."""
    return frappe.db.exists("StudentContentLog", {
        "student": student_id,
        "stage_no": week,
        "content_type": "Assignment",
        "action": "completed",
    })


def _get_submission_validity(student_id, week):
    """
    Check a student's submission status and validity for a specific week.

    Returns:
        dict with:
          - submitted (bool): whether any submission exists
          - is_valid (bool or None): True if valid, False if invalid, None if no submission
    """
    log = frappe.db.get_value(
        "StudentContentLog",
        {
            "student": student_id,
            "stage_no": week,
            "content_type": "Assignment",
            "action": "completed",
        },
        ["metadata"],
        as_dict=True,
    )

    if not log:
        return {"submitted": False, "is_valid": None}

    # Parse metadata to get is_valid flag
    is_valid = True  # default if metadata missing
    if log.metadata:
        try:
            meta = json.loads(log.metadata)
            is_valid = meta.get("is_valid", True)
        except (json.JSONDecodeError, TypeError):
            pass

    return {"submitted": True, "is_valid": is_valid}


# ============================================================
# CONTENT RESOLUTION
# ============================================================

def _get_learning_unit(course_level, week, tier):
    """
    Get the LearningUnit for a specific week and tier from Course Level.
    """
    course_level = normalize_unicode_surrogates(course_level)
    result = frappe.db.sql("""
        SELECT lul.learning_unit
        FROM `tabLearningUnitList` lul
        INNER JOIN `tabLearningUnit` lu ON lu.name = lul.learning_unit
        WHERE lul.parent = %s
          AND lul.parenttype = 'Course Level'
          AND lul.week_no = %s
          AND lu.difficulty_tier = %s
        ORDER BY lul.idx ASC
        LIMIT 1
    """, (course_level, week, tier), as_dict=True)

    return result[0].learning_unit if result else None


def _get_content_items(learning_unit):
    """Get content items for a learning unit."""
    learning_unit = normalize_unicode_surrogates(learning_unit)
    items = frappe.get_all(
        "UnitContentItem",
        filters={"parent": learning_unit, "parenttype": "LearningUnit"},
        fields=["idx", "content_type", "content", "is_optional"],
        order_by="idx asc",
    )

    result = []
    for item in items:
        name = _get_content_display_name(item.content_type, item.content)
        result.append({
            "index": item.idx,
            "content_type": item.content_type,
            "content_id": item.content,
            "content_name": name,
            "is_optional": item.is_optional,
        })

    return result


def _get_content_display_name(content_type, content_id):
    """Get display name for content."""
    if content_type in ("Assignment", "Quiz"):
        content_id = normalize_unicode_surrogates(content_id)
    field_map = {
        "VideoClass": "video_name",
        "Quiz": "quiz_name",
        "Assignment": "assignment_name",
        "NoteContent": "note_name",
        "CourseProject": "project_name",
    }
    field = field_map.get(content_type, "name")
    try:
        return frappe.db.get_value(content_type, content_id, field) or content_id
    except Exception:
        return content_id


def _resolve_content_language(language=None, student_id=None):
    """Resolve content language from input, active PE, then English fallback."""
    if language:
        return language

    if student_id:
        resolved_student_id = resolve_student(student_id)
        if resolved_student_id:
            pe = get_active_pe(resolved_student_id)
            pe_language = getattr(pe, "language", None)
            if pe_language:
                return pe_language

    return DEFAULT_LANGUAGE


def _get_video_assessments(content_type, content_id):
    """If content is a VideoClass, return its linked assessments (type + id)."""
    if content_type != "VideoClass":
        return None
    rows = frappe.get_all(
        "AssessmentList",
        filters={"parent": content_id, "parenttype": "VideoClass"},
        fields=["assessment_type", "assessment"],
        order_by="idx asc",
    )
    if not rows:
        return None
    return [
        {
            "assessment_type": r.assessment_type,
            "assessment_id": (
                normalize_unicode_surrogates(r.assessment)
                if r.assessment_type == "Assignment"
                else r.assessment
            ),
        }
        for r in rows if r.assessment
    ]


def _get_video_unguided_submission_message(student_id, assessments, language=None):
    """Return unguided submission copy for a video's assignment, when resolvable."""
    response = {"unguided_text": "Not Found", "unguided_text_url": "Not Found"}
    input_student_id = student_id

    student_id = resolve_student(student_id)
    if not student_id:
        _log_unguided_submission(
            "SP Unguided: student unresolved",
            f"input_student_id={input_student_id}, language={language}",
        )
        return response

    pe = get_active_pe(student_id)
    if not pe:
        _log_unguided_submission(
            "SP Unguided: no active PE",
            f"student_id={student_id}, input_student_id={input_student_id}, "
            f"language={language}",
        )
        return response

    pe_name = getattr(pe, "name", None)
    expected_submission_type = pe.current_expected_submission_type
    submission_labels = EXPECTED_SUBMISSION_LABELS.get(
        expected_submission_type
    )
    if not submission_labels:
        _log_unguided_submission(
            "SP Unguided: unsupported type",
            f"student_id={student_id}, pe={pe_name}, "
            f"expected_submission_type={expected_submission_type}, "
            f"language={language}",
        )
        return response

    if not language:
        _log_unguided_submission(
            "SP Unguided: missing language",
            f"student_id={student_id}, pe={pe_name}, "
            f"expected_submission_type={expected_submission_type}, "
            f"submission_labels={submission_labels}",
        )
        return response

    assignment_id = None
    for assessment in assessments or []:
        if assessment.get("assessment_type") == "Assignment":
            assignment_id = assessment.get("assessment_id")
            break
    assignment_id = normalize_unicode_surrogates(assignment_id)

    if not assignment_id:
        _log_unguided_submission(
            "SP Unguided: no assignment",
            f"student_id={student_id}, pe={pe_name}, "
            f"expected_submission_type={expected_submission_type}, "
            f"language={language}, assessments={assessments}",
        )
        return response

    _log_unguided_submission(
        "SP Unguided: lookup",
        f"student_id={student_id}, pe={pe_name}, assignment_id={assignment_id}, "
        f"expected_submission_type={expected_submission_type}, "
        f"submission_labels={submission_labels}, language={language}",
    )
    rows = frappe.get_all(
        "Assignment Submission Rule",
        filters={
            "parent": assignment_id,
            "parenttype": "Assignment",
            "submission_label": ["in", submission_labels],
            "language": language,
        },
        fields=["unguided_text", "unguided_text_audio"],
        order_by="display_order asc, idx asc",
        limit_page_length=1,
    )
    if not rows:
        _log_unguided_submission(
            "SP Unguided: no rule match",
            f"student_id={student_id}, pe={pe_name}, assignment_id={assignment_id}, "
            f"expected_submission_type={expected_submission_type}, "
            f"submission_labels={submission_labels}, language={language}",
        )
        return response

    _log_unguided_submission(
        "SP Unguided: found",
        f"student_id={student_id}, pe={pe_name}, assignment_id={assignment_id}, "
        f"language={language}",
    )
    return {
        "unguided_text": _strip_html_text(rows[0].unguided_text),
        "unguided_text_url": rows[0].unguided_text_audio,
    }


def _log_unguided_submission(title, message):
    """Log unguided submission diagnostics.

    Task #42 (2026-05-22): switched from `frappe.log_error` to
    `frappe.logger().info`. Most call sites here are HAPPY-PATH diagnostics
    (e.g. "looked up unguided text", "found rule match") that should never
    have been polluting the Error Log doctype — they were drowning real
    errors in operator triage. The only call site that's actually an error
    condition ("no rule match" / "no assignment") is left as info too,
    since the API still returns a valid `"Not Found"` payload and Glific
    routes around it; if operators want a stronger signal those callers
    can be promoted to warning() individually.

    Title is preserved as a structured prefix for grep-ability in the log
    stream. We don't truncate here because logger().info has no doctype
    length constraint.
    """
    frappe.logger("unguided_submission").info(f"{title}: {message}")


def _strip_html_text(value):
    if not value:
        return value
    from frappe.utils import strip_html_tags
    return strip_html_tags(value).strip()


def _get_next_learning_unit(course_level, week_no, tier, after_lu):
    """Get next LU after current one in same week/tier."""
    course_level = normalize_unicode_surrogates(course_level)
    after_lu = normalize_unicode_surrogates(after_lu)
    current_idx = frappe.db.get_value(
        "LearningUnitList",
        {"parent": course_level, "parenttype": "Course Level", "learning_unit": after_lu},
        "idx"
    )
    if not current_idx:
        return None

    result = frappe.db.sql("""
        SELECT lul.learning_unit
        FROM `tabLearningUnitList` lul
        INNER JOIN `tabLearningUnit` lu ON lu.name = lul.learning_unit
        WHERE lul.parent = %s
          AND lul.parenttype = 'Course Level'
          AND lul.week_no = %s
          AND lu.difficulty_tier = %s
          AND lul.idx > %s
        ORDER BY lul.idx ASC
        LIMIT 1
    """, (course_level, week_no, tier, current_idx), as_dict=True)
    return result[0].learning_unit if result else None


def _check_week_exists(course_level, week_no):
    """Check if a week exists in course level."""
    course_level = normalize_unicode_surrogates(course_level)
    return frappe.db.exists("LearningUnitList", {
        "parent": course_level, "parenttype": "Course Level", "week_no": week_no
    })


def _get_learning_unit_info(learning_unit):
    """Get LU display info."""
    if not learning_unit:
        return None
    learning_unit = normalize_unicode_surrogates(learning_unit)
    try:
        lu = frappe.get_doc("LearningUnit", learning_unit)
        return {"id": learning_unit, "name": getattr(lu, 'unit_name', learning_unit)}
    except Exception:
        return {"id": learning_unit, "name": learning_unit}


def _get_quiz_questions(quiz_doc):
    """Get ordered list of quiz questions."""
    if not hasattr(quiz_doc, 'questions') or not quiz_doc.questions:
        return []
    questions = list(quiz_doc.questions)
    questions.sort(key=lambda q: getattr(q, 'question_number', q.idx))
    return questions


def _get_question_details(question_id, language=None):
    """Get question details with translation support.

    # console-use only (L-050, 2026-06-08): production endpoints now call
    # cached_question_details() from master_data_lookup, which is a strict
    # superset (surrogate normalization + lru_cache). This function is kept
    # for interactive bench-console diagnostic use and as a reference for the
    # TestQuestionDetailsTranslations unit tests. Do not delete.
    """
    try:
        from frappe.utils import strip_html_tags

        question_id = normalize_unicode_surrogates(question_id)
        q = frappe.get_doc("QuizQuestion", question_id)
        question_text = q.question or getattr(q, 'question_name', '') or ""

        if language and hasattr(q, 'question_translations') and q.question_translations:
            for trans in q.question_translations:
                if trans.language == language and trans.translated_question:
                    question_text = trans.translated_question
                    break

        question_text = strip_html_tags(question_text) if question_text else ""

        options = {}
        if hasattr(q, 'options') and q.options:
            for i, opt_row in enumerate(q.options[:4]):
                letter = OPTION_LETTERS[i].lower()
                option_id = opt_row.options
                if option_id:
                    option_doc = frappe.get_doc("QuizOption", option_id)
                    option_text = option_doc.option_text or ""
                    if language and hasattr(option_doc, 'option_translations') and option_doc.option_translations:
                        for trans in option_doc.option_translations:
                            if trans.language == language and trans.translated_option:
                                option_text = trans.translated_option
                                break
                    options[f"option_{letter}"] = strip_html_tags(option_text) if option_text else ""

        correct_num = cint(q.correct_option)
        correct_letter = OPTION_LETTERS[correct_num - 1] if 1 <= correct_num <= 4 else "A"

        response = {
            "question_id": question_id,
            "question": question_text,
            "question_type": getattr(q, 'question_type', 'Multiple Choice'),
            "correct_option": correct_letter,
        }
        response.update(options)
        return response
    except Exception as e:
        frappe.log_error(f"_get_question_details error for {question_id}: {str(e)}")
        return {"question_id": question_id, "error": str(e)}


# ============================================================
# STUDENT & BATCH RESOLUTION
# ============================================================

def _resolve_student_id(student_identifier):
    """Resolve various student identifiers to Student name."""
    if not student_identifier:
        return None

    # Direct match
    if frappe.db.exists("Student", student_identifier):
        return student_identifier

    # Try Glific ID
    student = frappe.db.get_value("Student", {"glific_id": student_identifier}, "name")
    if student:
        return student

    # Try phone number
    phone = str(student_identifier).strip().replace(" ", "")
    student = frappe.db.get_value("Student", {"phone": phone}, "name")
    return student


def _get_active_bpr_for_student(student):
    """
    Find the active BatchProgramRun and Batch for a student in the Summer Program.

    Reads from ProgramEnrollment (the canonical SP enrollment record), NOT
    from Student.enrollment (the legacy child table populated by
    backend onboarding). Students enrolled via start_program_enrollment for
    a new SP batch may have a ProgramEnrollment but no matching
    Student.enrollment row for that batch, so reading the child table
    misses them (root cause of 2026-05-19 "no_active_batch" production
    incident for Shivansh / palv2-test-BT52231).

    A student may have multiple PEs (legacy or completed); iterate them so we
    return the first one whose Batch is a Summer program AND has an active BPR.
    """
    pe_rows = frappe.get_all(
        "ProgramEnrollment",
        filters={
            "student": student.name,
            "program_status": ["in", [PROGRAM_ACTIVE, PROGRAM_PAUSED]],
        },
        fields=["name", "batch"],
        order_by="creation desc",
    )

    for pe in pe_rows:
        if not pe.batch:
            continue

        batch = frappe.get_doc("Batch", pe.batch)
        if batch.program_type != "Summer":
            continue

        bpr_name = frappe.db.get_value(
            "BatchProgramRun",
            {"batch": pe.batch, "status": BPR_ACTIVE},
            "name",
        )
        if bpr_name:
            return batch, frappe.get_doc("BatchProgramRun", bpr_name)

    return None, None


def _get_course_level_for_student(student, batch):
    """
    Get the course level for a student's enrollment in a batch.

    Reads ProgramEnrollment.course_level — the canonical source for SP
    enrollments. Both callers are SP-only paths that have already resolved
    the batch via _get_active_bpr_for_student, so if the PE row was used
    to find the batch, the PE row is also the right source for course_level.
    """
    course_level = frappe.db.get_value(
        "ProgramEnrollment",
        {
            "student": student.name,
            "batch": batch.name,
            "program_status": ["in", [PROGRAM_ACTIVE, PROGRAM_PAUSED]],
        },
        "course_level",
    )
    return normalize_unicode_surrogates(course_level) or None


def _get_language_for_student(student_id, course_level=None):
    """
    Resolve quiz translation language from canonical enrollment data.

    ProgramEnrollment.language is preferred because it is the Summer Program
    snapshot used by Glific flows. Student.language is the fallback for legacy
    or incomplete enrollment rows. API-provided language is intentionally not
    considered.
    """
    course_level = normalize_unicode_surrogates(course_level)
    filters = {
        "student": student_id,
        "program_status": ["in", [PROGRAM_ACTIVE, PROGRAM_PAUSED]],
    }
    if course_level:
        filters["course_level"] = course_level

    pe_rows = frappe.get_all(
        "ProgramEnrollment",
        filters=filters,
        fields=["language"],
        order_by="creation desc",
        limit_page_length=1,
    )
    if pe_rows and pe_rows[0].language:
        return pe_rows[0].language

    return frappe.db.get_value("Student", student_id, "language") or None


def _is_within_grace_period(batch, current_week):
    """
    Check if we're still within the grace period at the start of a new week.

    Grace period = SUBMISSION_GRACE_HOURS after the week boundary.
    During this time, students who haven't submitted for the previous week
    are NOT blocked yet — they still have time to submit.

    Returns True if within grace period, False if grace has expired.
    """
    if not batch.start_date:
        return False

    # Calculate when the current week started
    week_start_days = (current_week - 1) * 7
    week_start_date = add_days(batch.start_date, week_start_days)

    # Grace deadline = week_start + SUBMISSION_GRACE_HOURS
    grace_deadline = add_to_date(
        get_datetime(week_start_date),
        hours=SUBMISSION_GRACE_HOURS,
    )

    return now_datetime() <= grace_deadline


def _get_current_week(batch):
    """Calculate current calendar week from batch start_date."""
    if not batch.start_date:
        return 0
    days = date_diff(today(), batch.start_date)
    if days < 0:
        return 0
    return (days // 7) + 1


def _get_effective_week(student, batch, calendar_week):
    """
    Determine the student's effective week based on their progress.

    Students can advance AT MOST one week ahead of the calendar week.
    Example: during calendar week 1, a student can complete week 1 and
    advance to week 2 content. But they CANNOT start week 3 — they are
    paused until calendar week 3 arrives.

    Rule: effective_week <= calendar_week + 1

    Returns the week the student should be working on.

    Source of truth (2026-05-22): ProgramEnrollment.current_week is the
    canonical SP-side week pointer post-CR-003 — same architecture as the
    other read helpers (_get_active_bpr_for_student, _get_course_level_for_student,
    get_student_sp_status). The legacy StudentStageProgress.current_week is
    only used as a fallback for non-SP code paths.

    Critical: `dev_tools.reset_pe_to_state_0` deletes StudentStageProgress
    rows (they're in `_HISTORY_DOCTYPES`). Before this fix, the function
    fell back to calendar_week when SSP was missing — which made every
    reset student look like they were already at the calendar's pace,
    triggering the content_blocked check for previous-week submission.
    Reading PE.current_week first sidesteps that completely.
    """
    # Canonical: ProgramEnrollment.current_week (SP-aware, reset-safe).
    pe_week = frappe.db.get_value(
        "ProgramEnrollment",
        {
            "student": student.name,
            "batch": batch.name,
            "program_status": ["in", ["active", "paused"]],
        },
        "current_week",
    )
    if pe_week:
        student_week = cint(pe_week)
    else:
        # Legacy fallback: StudentStageProgress (non-SP code paths).
        progress = frappe.db.get_value(
            "StudentStageProgress",
            {
                "student": student.name,
                "course_context": batch.name,
                "stage_type": "LearningUnit",
            },
            ["current_week"],
            as_dict=True,
        )
        if not progress or not progress.current_week:
            return calendar_week
        student_week = cint(progress.current_week)

    total_weeks = cint(batch.total_weeks) or calendar_week

    # Cap: student can be at most 1 week ahead of calendar
    max_allowed = min(calendar_week + 1, total_weeks)

    # Effective week is bounded by [1, max_allowed]
    effective = min(max(student_week, 1), max_allowed)

    return effective


# ============================================================
# PROGRESS TRACKING
# ============================================================

def _get_or_create_sp_progress(student_id, course_level, week, tier, learning_unit):
    """
    Get or create a StudentStageProgress record for Summer Program.
    """
    course_level = normalize_unicode_surrogates(course_level)
    learning_unit = normalize_unicode_surrogates(learning_unit)
    progress = frappe.db.get_value(
        "StudentStageProgress",
        {
            "student": student_id,
            "course_context": course_level,
            "stage_type": "LearningUnit",
        },
        ["name", "current_week", "current_tier", "is_on_remedial"],
        as_dict=True,
    )

    if progress:
        # Update to current week if needed
        if cint(progress.current_week) != cint(week):
            frappe.db.set_value("StudentStageProgress", progress.name, {
                "current_week": week,
                "current_tier": tier,
                "stage": learning_unit,
                "is_on_remedial": 1 if tier == REMEDIAL_TIER else 0,
                "last_activity_timestamp": now_datetime(),
            })
            # Removed mid-handler commit per L-017 — Frappe commits at request-end.
        return progress.name

    # Create new
    doc = frappe.get_doc({
        "doctype": "StudentStageProgress",
        "student": student_id,
        "stage_type": "LearningUnit",
        "stage": learning_unit,
        "course_context": course_level,
        "status": "assigned",
        "current_week": week,
        "current_tier": tier,
        "current_content_index": 0,
        "is_on_remedial": 1 if tier == REMEDIAL_TIER else 0,
        "remedial_attempts": 0,
        "start_timestamp": now_datetime(),
        "total_content_completed": 0,
        "total_quizzes_passed": 0,
        "total_quizzes_failed": 0,
        "total_time_spent_seconds": 0,
    })
    # BR-003: retry on transient PG SerializationFailure. StudentStageProgress
    # is now hash-autoname (no tabSeries lock), but the retry also covers the
    # rollout window (workers still on the old format-autoname until recycled,
    # L-065) and any other transient write conflict.
    _insert_with_serialization_retry(doc)
    # Removed mid-handler commit per L-017 — Frappe commits at request-end.
    return doc.name
