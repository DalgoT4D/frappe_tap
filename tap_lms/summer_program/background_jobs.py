# -*- coding: utf-8 -*-
# Copyright (c) 2026, TAP LMS
# File: tap_lms/summer_program/background_jobs.py
#
# Background Jobs for Student Progression
#
# Moved from tap_lms/journey/background_jobs.py on 2026-05-28 as part
# of the journey/ → summer_program/ migration. journey/background_jobs.py
# now exists as a thin shim that re-exports from this module so any
# in-flight jobs enqueued with the old path (`tap_lms.journey.background_jobs.X`)
# still resolve correctly after the deploy.
#
# JOBS:
# - job_log_content_completion: Log content completion to StudentContentLog
# - job_update_statistics: Update aggregate statistics in StudentStageProgress
# - job_finalize_quiz: Finalize quiz attempt (future use)
#
# These are NOT @frappe.whitelist() endpoints — Glific cannot call them
# directly. They are invoked via frappe.enqueue from the SP API layer
# (summer_program/student_progression_sp.py).

import frappe
from frappe.utils import now_datetime, cint, flt

from tap_lms.summer_program.utils import normalize_unicode_surrogates


def create_content_completion_log(
    student_id: str,
    course_level: str,
    progress_name: str,
    content_type: str,
    content_id: str,
    action: str,
    time_spent_seconds: int = 0,
    score: float = None,
    max_score: float = None,
    passed: bool = None,
    quiz_attempt: str = None,
    stage_no: int = None,
    tier: str = None,
    learning_unit: str = None,
    started_at=None,
    points_awarded=None,
    metadata=None,
):
    """Insert StudentContentLog and let DocType hooks run.

    This intentionally does not commit. Callers decide transaction boundaries:
    API paths commit at request end, while background jobs commit after insert.
    VideoClass points are awarded by complete_content before this log is
    enqueued. For those rows, pass points_awarded + metadata so the
    StudentContentLog.after_insert hook can skip and avoid double-awarding.
    """
    course_level = normalize_unicode_surrogates(course_level)
    learning_unit = normalize_unicode_surrogates(learning_unit)
    if content_type in ("Assignment", "Quiz"):
        content_id = normalize_unicode_surrogates(content_id)

    content_name = get_content_name(content_type, content_id)
    attempt_number = frappe.db.count("StudentContentLog", {
        "student": student_id,
        "content_type": content_type,
        "content_id": content_id,
    }) + 1

    log = frappe.get_doc({
        "doctype": "StudentContentLog",
        "student": student_id,
        "course_level": course_level,
        "student_progress": progress_name,
        "stage_no": stage_no,
        "tier": tier,
        "learning_unit": learning_unit,
        "content_type": content_type,
        "content_id": content_id,
        "content_name": content_name,
        "action": action,
        "started_at": started_at,
        "completed_at": now_datetime(),
        "time_spent_seconds": cint(time_spent_seconds),
        "score": flt(score) if score is not None else None,
        "max_score": flt(max_score) if max_score is not None else None,
        "passed": 1 if passed else 0 if passed is not None else None,
        "attempt_number": attempt_number,
        "quiz_attempt": quiz_attempt,
        "points_awarded": cint(points_awarded) if points_awarded is not None else 0,
        "metadata": metadata,
    })
    log.insert(ignore_permissions=True)
    return log


def job_log_content_completion(
    student_id: str,
    course_level: str,
    progress_name: str,
    content_type: str,
    content_id: str,
    action: str,
    time_spent_seconds: int = 0,
    score: float = None,
    max_score: float = None,
    passed: bool = None,
    quiz_attempt: str = None,
    stage_no: int = None,
    tier: str = None,
    learning_unit: str = None,
    started_at=None,
    points_awarded=None,
    metadata=None,
):
    """
    Log content completion to StudentContentLog.

    This job runs asynchronously to avoid blocking the main API response.

    Args:
        student_id: Student document name
        course_level: Course Level document name
        progress_name: StudentStageProgress document name
        content_type: Type of content (VideoClass, Quiz, etc.)
        content_id: Content document ID
        action: Action type (started, completed, failed, skipped, abandoned)
        time_spent_seconds: Time spent on content
        score: Score achieved (for quizzes)
        max_score: Maximum possible score
        passed: Whether passed (for quizzes)
        quiz_attempt: StudentQuizAttempt name (for quizzes)
        stage_no: Week number
        tier: Difficulty tier
        learning_unit: LearningUnit document name
        started_at: When content was started
        points_awarded: Optional pre-awarded points for audit/log rows
        metadata: Optional JSON metadata for the StudentContentLog row
    """
    try:
        log = create_content_completion_log(
            student_id=student_id,
            course_level=course_level,
            progress_name=progress_name,
            content_type=content_type,
            content_id=content_id,
            action=action,
            time_spent_seconds=time_spent_seconds,
            score=score,
            max_score=max_score,
            passed=passed,
            quiz_attempt=quiz_attempt,
            stage_no=stage_no,
            tier=tier,
            learning_unit=learning_unit,
            started_at=started_at,
            points_awarded=points_awarded,
            metadata=metadata,
        )
        frappe.db.commit()

        frappe.logger().info(f"Content log created: {log.name} for {student_id} - {content_type}:{content_id}")

    except Exception as e:
        # Fail loudly: this job is the durable bridge that inserts
        # StudentContentLog. If we swallow the exception, RQ marks the job
        # successful and the student silently loses the completion audit row.
        try:
            frappe.db.rollback()
        except Exception:
            pass

        context = (
            f"job_log_content_completion failed: {str(e)}\n"
            f"student: {student_id}\n"
            f"course_level: {course_level}\n"
            f"progress_name: {progress_name}\n"
            f"content: {content_type}:{content_id}\n"
            f"action: {action}\n"
            f"stage_no: {stage_no}\n"
            f"tier: {tier}\n"
            f"learning_unit: {learning_unit}\n"
            f"quiz_attempt: {quiz_attempt}"
        )
        try:
            frappe.log_error(context, "Background Job Error")
        except Exception:
            frappe.logger().error(context)
        raise


def job_update_statistics(
    progress_name: str,
    content_completed: int = 0,
    quiz_passed: int = 0,
    quiz_failed: int = 0,
    time_spent: int = 0
):
    """
    Update aggregate statistics in StudentStageProgress.

    This job runs asynchronously to avoid blocking the main API response.
    Handles concurrent updates safely using SQL increment.

    Args:
        progress_name: StudentStageProgress document name
        content_completed: Number of content items completed (to add)
        quiz_passed: Number of quizzes passed (to add)
        quiz_failed: Number of quizzes failed (to add)
        time_spent: Time spent in seconds (to add)
    """
    try:
        # Use SQL for atomic increment to handle concurrent updates
        if content_completed:
            frappe.db.sql("""
                UPDATE `tabStudentStageProgress`
                SET total_content_completed = COALESCE(total_content_completed, 0) + %s
                WHERE name = %s
            """, (content_completed, progress_name))

        if quiz_passed:
            frappe.db.sql("""
                UPDATE `tabStudentStageProgress`
                SET total_quizzes_passed = COALESCE(total_quizzes_passed, 0) + %s
                WHERE name = %s
            """, (quiz_passed, progress_name))

        if quiz_failed:
            frappe.db.sql("""
                UPDATE `tabStudentStageProgress`
                SET total_quizzes_failed = COALESCE(total_quizzes_failed, 0) + %s
                WHERE name = %s
            """, (quiz_failed, progress_name))

        if time_spent:
            frappe.db.sql("""
                UPDATE `tabStudentStageProgress`
                SET total_time_spent_seconds = COALESCE(total_time_spent_seconds, 0) + %s
                WHERE name = %s
            """, (time_spent, progress_name))

        frappe.db.commit()

        frappe.logger().info(
            f"Statistics updated for {progress_name}: "
            f"content+{content_completed}, passed+{quiz_passed}, failed+{quiz_failed}, time+{time_spent}s"
        )

    except Exception as e:
        frappe.log_error(
            f"job_update_statistics failed: {str(e)}\n"
            f"progress: {progress_name}",
            "Background Job Error"
        )


def job_finalize_quiz(
    quiz_attempt_name: str,
    score: float,
    passed: bool
):
    """
    Finalize quiz attempt with additional processing.

    This is a placeholder for future enhancements like:
    - Sending completion notifications
    - Updating leaderboards
    - Triggering analytics events
    - Syncing with external systems

    Args:
        quiz_attempt_name: StudentQuizAttempt document name
        score: Final score
        passed: Whether quiz was passed
    """
    try:
        # Future: Add notification logic
        # Future: Update leaderboard
        # Future: Trigger analytics event

        frappe.logger().info(
            f"Quiz finalized: {quiz_attempt_name}, score: {score}, passed: {passed}"
        )

    except Exception as e:
        frappe.log_error(
            f"job_finalize_quiz failed: {str(e)}\n"
            f"attempt: {quiz_attempt_name}",
            "Background Job Error"
        )


def get_content_name(content_type: str, content_id: str) -> str:
    """Get display name for content item."""
    if content_type in ("Assignment", "Quiz"):
        content_id = normalize_unicode_surrogates(content_id)
    field_map = {
        "VideoClass": "video_name",
        "Quiz": "quiz_name",
        "Assignment": "assignment_name",
        "NoteContent": "note_name",
        "CourseProject": "project_name"
    }
    field = field_map.get(content_type, "name")
    try:
        return frappe.db.get_value(content_type, content_id, field) or content_id
    except:
        return content_id


# ============================================================
# UTILITY: Retry Failed Jobs
# ============================================================

def retry_failed_content_logs():
    """
    Utility function to retry failed content log jobs.
    Can be called manually or scheduled.
    """
    # Get failed jobs from error log
    failed_jobs = frappe.db.sql("""
        SELECT name, error, creation
        FROM `tabError Log`
        WHERE method LIKE '%job_log_content_completion%'
          AND creation > NOW() - INTERVAL '24 hours'
        ORDER BY creation DESC
        LIMIT 100
    """, as_dict=True)

    frappe.logger().info(f"Found {len(failed_jobs)} failed content log jobs to review")

    return {"failed_jobs": len(failed_jobs), "details": failed_jobs}


# ============================================================
# SCHEDULED: Daily Statistics Reconciliation
# ============================================================

def reconcile_statistics():
    """
    Scheduled job to reconcile statistics from ContentLog.
    Ensures statistics in StudentStageProgress match actual logs.

    Run daily via scheduler.
    """
    try:
        # Get all progress records
        progress_records = frappe.get_all(
            "StudentStageProgress",
            filters={"stage_type": "LearningUnit"},
            fields=["name", "student", "course_context"]
        )

        updated = 0
        for progress in progress_records:
            # Calculate actual stats from ContentLog
            stats = frappe.db.sql("""
                SELECT
                    COUNT(*) as total_content,
                    SUM(CASE WHEN content_type = 'Quiz' AND passed = 1 THEN 1 ELSE 0 END) as quizzes_passed,
                    SUM(CASE WHEN content_type = 'Quiz' AND passed = 0 THEN 1 ELSE 0 END) as quizzes_failed,
                    SUM(COALESCE(time_spent_seconds, 0)) as total_time
                FROM `tabStudentContentLog`
                WHERE student = %s AND course_level = %s AND action IN ('completed', 'failed')
            """, (progress["student"], progress["course_context"]), as_dict=True)

            if stats and stats[0]:
                s = stats[0]
                frappe.db.set_value("StudentStageProgress", progress["name"], {
                    "total_content_completed": cint(s.get("total_content", 0)),
                    "total_quizzes_passed": cint(s.get("quizzes_passed", 0)),
                    "total_quizzes_failed": cint(s.get("quizzes_failed", 0)),
                    "total_time_spent_seconds": cint(s.get("total_time", 0))
                }, update_modified=False)
                updated += 1

        frappe.db.commit()
        frappe.logger().info(f"Statistics reconciliation complete: {updated} records updated")

        return {"updated": updated}

    except Exception as e:
        frappe.log_error(f"reconcile_statistics failed: {str(e)}", "Scheduled Job Error")
        return {"error": str(e)}
