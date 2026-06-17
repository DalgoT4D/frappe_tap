# -*- coding: utf-8 -*-
# Copyright (c) 2026, TAP LMS
# File: tap_lms/journey/student_progression_api.py
#
# Student Progression API v3 - Split Endpoints with Background Jobs
#
# ENDPOINTS:
# - get_next_content: Get next content item for student
# - get_content_details: Get specific content details
# - complete_content: Mark non-quiz content complete
# - start_quiz: Begin or resume quiz
# - submit_answer: Submit quiz answer
# - get_quiz_status: Check for active quiz
# - get_student_progress: Get progress overview
# - get_student_history: Get completion history
#
# BACKGROUND JOBS (in background_jobs.py):
# - job_log_content_completion
# - job_update_statistics
# - job_finalize_quiz

import frappe
from frappe import _
from frappe.utils import now_datetime, cint, flt, time_diff_in_seconds
import json

# Task #1 (2026-05-28): journey APIs deprecated pre-launch. All 8
# whitelisted endpoints in this file return a deprecation envelope and
# do NOT execute the legacy progression logic. The SP replacement lives
# in summer_program/student_progression_sp.py with the same function
# names. Old endpoint bodies are preserved as `_DEPRECATED_*_original`
# helpers for one release cycle so the code is still readable.
from tap_lms.journey._deprecation import journey_deprecated_response

# ============================================================
# CONSTANTS
# ============================================================

TIER_BY_WEEK = {
    1: "Basic",
    2: "Intermediate",
}
DEFAULT_TIER = "Advanced"
REMEDIAL_TIER = "Remedial"

VALID_CONTENT_TYPES = ["VideoClass", "Quiz", "Assignment", "NoteContent", "CourseProject"]
OPTION_LETTERS = ['A', 'B', 'C', 'D']


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def get_tier_for_week(week_no: int) -> str:
    """Get default tier for a week number."""
    return TIER_BY_WEEK.get(cint(week_no), DEFAULT_TIER)


def resolve_student_id(student_identifier: str) -> str:
    """Resolve various student identifiers to actual Student ID."""
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
    student = frappe.db.get_value("Student", {"phone": student_identifier}, "name")
    return student


def get_student_progress(student_id: str, course_level: str) -> dict:
    """Get or create student progress record."""
    progress = frappe.db.get_value(
        "StudentStageProgress",
        {"student": student_id, "course_context": course_level, "stage_type": "LearningUnit"},
        ["name", "student", "stage", "status", "current_week", "current_tier",
         "current_content_index", "is_on_remedial", "remedial_attempts",
         "active_content_type", "active_content_id", "content_started_at",
         "active_quiz_attempt", "question_started_at",
         "total_content_completed", "total_quizzes_passed", "total_quizzes_failed",
         "total_time_spent_seconds", "start_timestamp", "last_activity_timestamp"],
        as_dict=True
    )
    
    if progress:
        return progress
    
    # Create new progress
    first_week = 1
    first_tier = get_tier_for_week(first_week)
    first_lu = get_first_learning_unit(course_level, first_week, first_tier)
    
    if not first_lu:
        frappe.throw(f"No Learning Unit found for Week {first_week} {first_tier} tier")
    
    doc = frappe.get_doc({
        "doctype": "StudentStageProgress",
        "student": student_id,
        "stage_type": "LearningUnit",
        "stage": first_lu,
        "course_context": course_level,
        "status": "assigned",
        "current_week": first_week,
        "current_tier": first_tier,
        "current_content_index": 0,
        "is_on_remedial": 0,
        "remedial_attempts": 0,
        "start_timestamp": now_datetime(),
        "total_content_completed": 0,
        "total_quizzes_passed": 0,
        "total_quizzes_failed": 0,
        "total_time_spent_seconds": 0
    })
    doc.insert(ignore_permissions=True)
    # Removed mid-handler commit per L-017 — Frappe commits at request-end.

    return frappe.db.get_value(
        "StudentStageProgress", doc.name,
        ["name", "student", "stage", "status", "current_week", "current_tier",
         "current_content_index", "is_on_remedial", "remedial_attempts",
         "active_content_type", "active_content_id", "content_started_at",
         "active_quiz_attempt", "question_started_at",
         "total_content_completed", "total_quizzes_passed", "total_quizzes_failed",
         "total_time_spent_seconds", "start_timestamp", "last_activity_timestamp"],
        as_dict=True
    )


def update_progress(progress_name: str, updates: dict):
    """Update progress record."""
    updates["last_activity_timestamp"] = now_datetime()
    frappe.db.set_value("StudentStageProgress", progress_name, updates)
    # Removed mid-handler commit per L-017 — Frappe commits at request-end.


def get_first_learning_unit(course_level: str, week_no: int, tier: str) -> str:
    """Get first LU for a week/tier."""
    # Join with LearningUnit to filter by difficulty_tier
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
    """, (course_level, week_no, tier), as_dict=True)
    return result[0].learning_unit if result else None


def get_next_learning_unit(course_level: str, week_no: int, tier: str, after_lu: str) -> str:
    """Get next LU after current one in same week/tier."""
    current_idx = frappe.db.get_value(
        "LearningUnitList",
        {"parent": course_level, "parenttype": "Course Level", "learning_unit": after_lu},
        "idx"
    )
    if not current_idx:
        return None
    
    # Join with LearningUnit to filter by difficulty_tier
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


def check_week_exists(course_level: str, week_no: int) -> bool:
    """Check if a week exists in course level."""
    return frappe.db.exists("LearningUnitList", {
        "parent": course_level, "parenttype": "Course Level", "week_no": week_no
    })


def get_content_items(learning_unit: str) -> list:
    """Get content items for a learning unit."""
    return frappe.get_all(
        "UnitContentItem",
        filters={"parent": learning_unit, "parenttype": "LearningUnit"},
        fields=["idx", "content_type", "content", "is_optional"],
        order_by="idx asc"
    )


def get_learning_unit_info(learning_unit: str) -> dict:
    """Get LU display info."""
    if not learning_unit:
        return None
    try:
        lu = frappe.get_doc("LearningUnit", learning_unit)
        return {"id": learning_unit, "name": getattr(lu, 'unit_name', learning_unit)}
    except:
        return {"id": learning_unit, "name": learning_unit}


def get_content_name(content_type: str, content_id: str) -> str:
    """Get display name for content."""
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
# API 1: GET NEXT CONTENT
# ============================================================

@frappe.whitelist(allow_guest=False)
def get_next_content(**_kw):
    """[DEPRECATED 2026-05-28] Use summer_program.student_progression_sp.get_next_content."""
    return journey_deprecated_response("get_next_content", _kw)


def _DEPRECATED_get_next_content_original(student_id: str, course_level: str):
    """
    Get next content item for student.

    Returns:
    - Content info if available
    - Quiz in progress status if active quiz exists
    - Course complete status if finished
    """
    try:
        if not student_id or not course_level:
            return {"success": False, "error": "student_id and course_level are required"}
        
        resolved_student = resolve_student_id(student_id)
        if not resolved_student:
            return {"success": False, "error": f"Student not found: {student_id}"}
        
        if not frappe.db.exists("Course Level", course_level):
            return {"success": False, "error": f"Course Level not found: {course_level}"}
        
        progress = get_student_progress(resolved_student, course_level)
        
        # Check if course complete
        if progress.get("status") == "completed":
            return {
                "success": True,
                "status": "course_complete",
                "message": "You have already completed this course.",
                "student_id": resolved_student
            }
        
        # Check for active quiz
        if progress.get("active_quiz_attempt"):
            return {
                "success": True,
                "status": "quiz_in_progress",
                "student_id": resolved_student,
                "position": {
                    "week": cint(progress["current_week"]),
                    "tier": progress["current_tier"],
                    "learning_unit": progress["stage"],
                    "is_remedial": bool(progress.get("is_on_remedial"))
                },
                "content": {
                    "type": "Quiz",
                    "id": progress.get("active_content_id")
                },
                "has_active_quiz": True,
                "quiz_attempt_id": progress["active_quiz_attempt"]
            }
        
        # Get content items
        content_items = get_content_items(progress["stage"])
        current_index = cint(progress["current_content_index"])
        
        # Return current content if available
        if current_index < len(content_items):
            item = content_items[current_index]
            lu_info = get_learning_unit_info(progress["stage"])
            
            return {
                "success": True,
                "status": "content_available",
                "student_id": resolved_student,
                "position": {
                    "week": cint(progress["current_week"]),
                    "tier": progress["current_tier"],
                    "learning_unit": progress["stage"],
                    "learning_unit_name": lu_info["name"] if lu_info else None,
                    "content_index": current_index,
                    "is_remedial": bool(progress.get("is_on_remedial"))
                },
                "content": {
                    "type": item["content_type"],
                    "id": item["content"],
                    "name": get_content_name(item["content_type"], item["content"]),
                    "order": current_index + 1,
                    "total_in_unit": len(content_items),
                    "is_optional": bool(item.get("is_optional"))
                },
                "has_active_quiz": False
            }
        
        # Current LU complete - check for next LU
        next_lu = get_next_learning_unit(
            course_level, progress["current_week"], progress["current_tier"], progress["stage"]
        )
        
        if next_lu:
            update_progress(progress["name"], {
                "stage": next_lu,
                "current_content_index": 0,
                "status": "in_progress"
            })
            content_items = get_content_items(next_lu)
            if content_items:
                item = content_items[0]
                lu_info = get_learning_unit_info(next_lu)
                return {
                    "success": True,
                    "status": "content_available",
                    "student_id": resolved_student,
                    "position": {
                        "week": cint(progress["current_week"]),
                        "tier": progress["current_tier"],
                        "learning_unit": next_lu,
                        "learning_unit_name": lu_info["name"] if lu_info else None,
                        "content_index": 0,
                        "is_remedial": bool(progress.get("is_on_remedial"))
                    },
                    "content": {
                        "type": item["content_type"],
                        "id": item["content"],
                        "name": get_content_name(item["content_type"], item["content"]),
                        "order": 1,
                        "total_in_unit": len(content_items),
                        "is_optional": bool(item.get("is_optional"))
                    },
                    "has_active_quiz": False,
                    "new_learning_unit": True
                }
        
        # Week complete - check for next week
        next_week = cint(progress["current_week"]) + 1
        next_tier = get_tier_for_week(next_week)
        
        if not check_week_exists(course_level, next_week):
            # Course complete
            update_progress(progress["name"], {
                "status": "completed",
                "completion_timestamp": now_datetime()
            })
            return {
                "success": True,
                "status": "course_complete",
                "message": "Congratulations! You have completed the course.",
                "student_id": resolved_student,
                "completed_week": progress["current_week"]
            }
        
        # Move to next week
        first_lu = get_first_learning_unit(course_level, next_week, next_tier)
        if not first_lu:
            return {"success": False, "error": f"No LU found for Week {next_week} {next_tier}"}
        
        update_progress(progress["name"], {
            "stage": first_lu,
            "current_week": next_week,
            "current_tier": next_tier,
            "current_content_index": 0,
            "is_on_remedial": 0,
            "status": "in_progress"
        })
        
        content_items = get_content_items(first_lu)
        lu_info = get_learning_unit_info(first_lu)
        
        return {
            "success": True,
            "status": "stage_complete",
            "message": f"Stage {progress['current_week']} complete! Moving to Stage {next_week}.",
            "student_id": resolved_student,
            "completed_week": progress["current_week"],
            "position": {
                "week": next_week,
                "tier": next_tier,
                "learning_unit": first_lu,
                "learning_unit_name": lu_info["name"] if lu_info else None,
                "is_remedial": False
            },
            "content": {
                "type": content_items[0]["content_type"],
                "id": content_items[0]["content"],
                "name": get_content_name(content_items[0]["content_type"], content_items[0]["content"]),
                "order": 1,
                "total_in_unit": len(content_items)
            } if content_items else None,
            "has_active_quiz": False
        }
        
    except Exception as e:
        frappe.log_error(f"get_next_content error: {str(e)}", "Student Progression API")
        return {"success": False, "error": str(e)}


# ============================================================
# API 2: GET CONTENT DETAILS
# ============================================================

@frappe.whitelist(allow_guest=False)
def get_content_details(**_kw):
    """[DEPRECATED 2026-05-28] Use summer_program.student_progression_sp.get_content_details."""
    return journey_deprecated_response("get_content_details", _kw)


def _DEPRECATED_get_content_details_original(content_type: str, content_id: str, language: str = None):
    """
    Get detailed information about a specific content item.
    Includes translations if available.
    """
    try:
        if not content_type or not content_id:
            return {"success": False, "error": "content_type and content_id are required"}
        
        if content_type not in VALID_CONTENT_TYPES:
            return {"success": False, "error": f"Invalid content_type: {content_type}"}
        
        if not frappe.db.exists(content_type, content_id):
            return {"success": False, "error": f"{content_type} not found: {content_id}"}
        
        doc = frappe.get_doc(content_type, content_id)
        
        if content_type == "VideoClass":
            result = {
                "success": True,
                "content_type": "VideoClass",
                "content_id": content_id,
                "name": doc.video_name,
                "youtube_url": doc.video_youtube_url,
                "plio_url": doc.video_plio_url,
                "video_file": doc.video_file,
                "url": doc.video_youtube_url or doc.video_plio_url or doc.video_file,
                "duration": str(doc.duration) if doc.duration else None,
                "description": doc.description,
                "translated": False
            }
            
            # Check for translation
            if language and hasattr(doc, 'video_translations'):
                for trans in doc.video_translations:
                    if trans.language == language:
                        if trans.translated_name:
                            result["name"] = trans.translated_name
                        if trans.video_youtube_url:
                            result["youtube_url"] = trans.video_youtube_url
                            result["url"] = trans.video_youtube_url
                        result["translated"] = True
                        result["language"] = language
                        break
            
            return result
        
        elif content_type == "Quiz":
            question_count = len(doc.questions) if hasattr(doc, 'questions') else 0
            return {
                "success": True,
                "content_type": "Quiz",
                "content_id": content_id,
                "name": getattr(doc, 'quiz_name', content_id),
                "total_questions": question_count,
                "passing_score": flt(getattr(doc, 'passing_score', 60)),
                "time_limit": getattr(doc, 'time_limit', None)
            }
        
        elif content_type == "NoteContent":
            return {
                "success": True,
                "content_type": "NoteContent",
                "content_id": content_id,
                "name": getattr(doc, 'note_name', content_id),
                "content": getattr(doc, 'content', None)
            }
        
        elif content_type == "Assignment":
            return {
                "success": True,
                "content_type": "Assignment",
                "content_id": content_id,
                "name": getattr(doc, 'assignment_name', content_id),
                "description": getattr(doc, 'description', None)
            }
        
        elif content_type == "CourseProject":
            return {
                "success": True,
                "content_type": "CourseProject",
                "content_id": content_id,
                "name": getattr(doc, 'project_name', content_id),
                "description": getattr(doc, 'description', None)
            }
        
        return {"success": True, "content_type": content_type, "content_id": content_id}
        
    except Exception as e:
        frappe.log_error(f"get_content_details error: {str(e)}", "Student Progression API")
        return {"success": False, "error": str(e)}


# ============================================================
# API 3: COMPLETE CONTENT (Non-Quiz)
# ============================================================

@frappe.whitelist(allow_guest=False)
def complete_content(**_kw):
    """[DEPRECATED 2026-05-28] Use summer_program.student_progression_sp.complete_content."""
    return journey_deprecated_response("complete_content", _kw)


def _DEPRECATED_complete_content_original(student_id: str, course_level: str, content_type: str, content_id: str):
    """
    Mark non-quiz content as complete.
    For Quiz, use start_quiz and submit_answer instead.
    """
    try:
        if not all([student_id, course_level, content_type, content_id]):
            return {"success": False, "error": "All parameters required"}
        
        if content_type == "Quiz":
            return {"success": False, "error": "Use start_quiz and submit_answer for Quiz content"}
        
        if content_type not in VALID_CONTENT_TYPES:
            return {"success": False, "error": f"Invalid content_type: {content_type}"}
        
        resolved_student = resolve_student_id(student_id)
        if not resolved_student:
            return {"success": False, "error": f"Student not found: {student_id}"}
        
        progress = get_student_progress(resolved_student, course_level)
        
        # Validate content matches current position
        content_items = get_content_items(progress["stage"])
        current_index = cint(progress["current_content_index"])
        
        if current_index >= len(content_items):
            return {"success": False, "error": "No content at current position"}
        
        current_item = content_items[current_index]
        if current_item["content"] != content_id:
            return {
                "success": False,
                "error": f"Content mismatch. Expected: {current_item['content']}, Got: {content_id}"
            }
        
        # Calculate time spent
        time_spent = 0
        if progress.get("content_started_at"):
            time_spent = cint(time_diff_in_seconds(now_datetime(), progress["content_started_at"]))
        
        # Enqueue background job for logging
        frappe.enqueue(
            "tap_lms.journey.background_jobs.job_log_content_completion",
            queue="short",
            timeout=60,
            student_id=resolved_student,
            course_level=course_level,
            progress_name=progress["name"],
            content_type=content_type,
            content_id=content_id,
            action="completed",
            time_spent_seconds=time_spent,
            stage_no=progress["current_week"],
            tier=progress["current_tier"],
            learning_unit=progress["stage"]
        )
        
        # Enqueue stats update
        frappe.enqueue(
            "tap_lms.journey.background_jobs.job_update_statistics",
            queue="short",
            timeout=30,
            progress_name=progress["name"],
            content_completed=1,
            time_spent=time_spent
        )
        
        # Advance to next content
        return advance_to_next_content(progress, course_level)
        
    except Exception as e:
        frappe.log_error(f"complete_content error: {str(e)}", "Student Progression API")
        return {"success": False, "error": str(e)}


def advance_to_next_content(progress: dict, course_level: str) -> dict:
    """Move to next content item."""
    current_index = cint(progress["current_content_index"])
    new_index = current_index + 1
    content_items = get_content_items(progress["stage"])
    
    if new_index < len(content_items):
        # More content in current LU
        update_progress(progress["name"], {
            "current_content_index": new_index,
            "status": "in_progress",
            "active_content_type": None,
            "active_content_id": None,
            "content_started_at": None
        })
        
        next_item = content_items[new_index]
        return {
            "success": True,
            "action": "next_content",
            "message": "Content completed!",
            "next_content": {
                "type": next_item["content_type"],
                "id": next_item["content"],
                "name": get_content_name(next_item["content_type"], next_item["content"]),
                "order": new_index + 1
            },
            "progress": {
                "completed": new_index,
                "total": len(content_items),
                "percentage": round((new_index / len(content_items)) * 100, 1)
            }
        }
    
    # Current LU complete - find next LU
    next_lu = get_next_learning_unit(
        course_level, progress["current_week"], progress["current_tier"], progress["stage"]
    )
    
    if next_lu:
        update_progress(progress["name"], {
            "stage": next_lu,
            "current_content_index": 0,
            "active_content_type": None,
            "active_content_id": None,
            "content_started_at": None
        })
        
        content_items = get_content_items(next_lu)
        first_content = content_items[0] if content_items else None
        lu_info = get_learning_unit_info(next_lu)
        
        return {
            "success": True,
            "action": "next_learning_unit",
            "message": "Learning Unit completed!",
            "new_learning_unit": next_lu,
            "new_learning_unit_name": lu_info["name"] if lu_info else None,
            "next_content": {
                "type": first_content["content_type"],
                "id": first_content["content"],
                "name": get_content_name(first_content["content_type"], first_content["content"]),
                "order": 1
            } if first_content else None
        }
    
    # Week complete - move to next week
    current_week = cint(progress["current_week"])
    next_week = current_week + 1
    next_tier = get_tier_for_week(next_week)
    
    if not check_week_exists(course_level, next_week):
        # Course complete
        update_progress(progress["name"], {
            "status": "completed",
            "completion_timestamp": now_datetime(),
            "active_content_type": None,
            "active_content_id": None
        })
        return {
            "success": True,
            "action": "course_complete",
            "message": "Congratulations! Course completed!",
            "completed_week": current_week
        }
    
    # Move to next week
    first_lu = get_first_learning_unit(course_level, next_week, next_tier)
    if not first_lu:
        return {"success": False, "error": f"No LU found for Week {next_week}"}
    
    update_progress(progress["name"], {
        "stage": first_lu,
        "current_week": next_week,
        "current_tier": next_tier,
        "current_content_index": 0,
        "is_on_remedial": 0,
        "active_content_type": None,
        "active_content_id": None,
        "content_started_at": None
    })
    
    content_items = get_content_items(first_lu)
    first_content = content_items[0] if content_items else None
    lu_info = get_learning_unit_info(first_lu)
    
    return {
        "success": True,
        "action": "week_complete",
        "message": f"Stage {current_week} complete! Moving to Stage {next_week}.",
        "completed_week": current_week,
        "new_week": next_week,
        "new_tier": next_tier,
        "new_learning_unit": first_lu,
        "new_learning_unit_name": lu_info["name"] if lu_info else None,
        "next_content": {
            "type": first_content["content_type"],
            "id": first_content["content"],
            "name": get_content_name(first_content["content_type"], first_content["content"]),
            "order": 1
        } if first_content else None
    }


# ============================================================
# API 4: START QUIZ
# ============================================================

@frappe.whitelist(allow_guest=False)
def start_quiz(**_kw):
    """[DEPRECATED 2026-05-28] Use summer_program.student_progression_sp.start_quiz."""
    return journey_deprecated_response("start_quiz", _kw)


def _DEPRECATED_start_quiz_original(student_id: str, course_level: str, quiz_id: str, language: str = None):
    """
    Begin a quiz attempt or resume existing one.
    Returns first question (or current question if resuming).
    """
    try:
        if not all([student_id, course_level, quiz_id]):
            return {"success": False, "error": "student_id, course_level, and quiz_id required"}
        
        resolved_student = resolve_student_id(student_id)
        if not resolved_student:
            return {"success": False, "error": f"Student not found: {student_id}"}
        
        if not frappe.db.exists("Quiz", quiz_id):
            return {"success": False, "error": f"Quiz not found: {quiz_id}"}
        
        progress = get_student_progress(resolved_student, course_level)
        
        # Check for existing in-progress attempt
        if progress.get("active_quiz_attempt"):
            attempt = frappe.get_doc("StudentQuizAttempt", progress["active_quiz_attempt"])
            if attempt.quiz == quiz_id and attempt.status == "in_progress":
                # Resume existing attempt
                return resume_quiz(attempt, progress, language)
        
        # Create new attempt
        quiz_doc = frappe.get_doc("Quiz", quiz_id)
        questions = get_quiz_questions(quiz_doc)
        
        if not questions:
            return {"success": False, "error": "Quiz has no questions"}
        
        # Count previous attempts
        prev_attempts = frappe.db.count("StudentQuizAttempt", {
            "student": resolved_student,
            "quiz": quiz_id,
            "course_level": course_level
        })
        
        # Create attempt
        attempt = frappe.get_doc({
            "doctype": "StudentQuizAttempt",
            "student": resolved_student,
            "course_level": course_level,
            "student_progress": progress["name"],
            "quiz": quiz_id,
            "quiz_name": getattr(quiz_doc, 'quiz_name', quiz_id),
            "stage_no": progress["current_week"],
            "tier": progress["current_tier"],
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
            "answers": []
        })
        attempt.insert(ignore_permissions=True)
        # L-017: commit kept — legacy journey/ endpoint. The new StudentQuizAttempt
        # row needs to be visible before update_progress() writes
        # `active_quiz_attempt = attempt.name` onto StudentStageProgress, because
        # the teacher dashboard (other consumer of this endpoint) cross-reads
        # both tables and historically relied on the pointer being resolvable
        # after start_quiz returns. Removing this commit changes that assumption
        # without coordinating with the dashboard. Revisit when journey/ is
        # consolidated with summer_program/.
        frappe.db.commit()

        # Update progress
        update_progress(progress["name"], {
            "active_quiz_attempt": attempt.name,
            "active_content_type": "Quiz",
            "active_content_id": quiz_id,
            "content_started_at": now_datetime(),
            "question_started_at": now_datetime()
        })
        
        # Get first question
        first_question = get_question_details(questions[0].question, language)
        
        return {
            "success": True,
            "status": "quiz_started",
            "message": "Quiz started! Good luck!",
            "quiz_attempt_id": attempt.name,
            "quiz_name": attempt.quiz_name,
            "total_questions": len(questions),
            "passing_score": attempt.passing_score,
            "question": {
                "index": 1,
                "id": questions[0].question,
                "text": first_question.get("question"),
                "type": first_question.get("question_type", "Multiple Choice"),
                "options": {
                    "A": first_question.get("option_a"),
                    "B": first_question.get("option_b"),
                    "C": first_question.get("option_c"),
                    "D": first_question.get("option_d")
                },
                "correct_option": first_question.get("correct_option")
            }
        }
        
    except Exception as e:
        frappe.log_error(f"start_quiz error: {str(e)}", "Student Progression API")
        return {"success": False, "error": str(e)}


def resume_quiz(attempt, progress: dict, language: str = None) -> dict:
    """Resume an in-progress quiz attempt."""
    quiz_doc = frappe.get_doc("Quiz", attempt.quiz)
    questions = get_quiz_questions(quiz_doc)
    
    # Find next unanswered question
    answered_indices = {cint(a.question_index) for a in attempt.answers}
    next_index = 1
    for i in range(1, len(questions) + 1):
        if i not in answered_indices:
            next_index = i
            break
    
    # Update question started time
    update_progress(progress["name"], {"question_started_at": now_datetime()})
    attempt.question_started_at = now_datetime()
    attempt.save(ignore_permissions=True)
    # L-017: commit kept — legacy journey/ resume_quiz. This is a mid-handler
    # safety commit on a refresh-time write (`question_started_at`); strictly,
    # removing it would only mean a Frappe request-end commit a few ms later.
    # Conservative: kept because other consumers of resume_quiz (teacher
    # dashboard) may observe `question_started_at` from a parallel read; the
    # explicit commit makes that read-after-write boundary unambiguous. Cheap
    # to remove once journey/ has its own test coverage.
    frappe.db.commit()

    # Get question details
    question_row = questions[next_index - 1]
    question_details = get_question_details(question_row.question, language)
    
    correct_so_far = sum(1 for a in attempt.answers if a.is_correct)
    
    return {
        "success": True,
        "status": "quiz_resumed",
        "message": f"Welcome back! Continuing from question {next_index}.",
        "quiz_attempt_id": attempt.name,
        "quiz_name": attempt.quiz_name,
        "total_questions": attempt.total_questions,
        "questions_answered": len(attempt.answers),
        "correct_so_far": correct_so_far,
        "question": {
            "index": next_index,
            "id": question_row.question,
            "text": question_details.get("question"),
            "type": question_details.get("question_type", "Multiple Choice"),
            "options": {
                "A": question_details.get("option_a"),
                "B": question_details.get("option_b"),
                "C": question_details.get("option_c"),
                "D": question_details.get("option_d")
            },
            "correct_option": question_details.get("correct_option")
        }
    }


def get_quiz_questions(quiz_doc) -> list:
    """Get ordered list of quiz questions."""
    if not hasattr(quiz_doc, 'questions') or not quiz_doc.questions:
        return []
    questions = list(quiz_doc.questions)
    questions.sort(key=lambda q: getattr(q, 'question_number', q.idx))
    return questions


def get_question_details(question_id: str, language: str = None) -> dict:
    """Get question details with translation support."""
    try:
        q = frappe.get_doc("QuizQuestion", question_id)
        
        # Get question text
        question_text = q.question or getattr(q, 'question_name', '') or ""
        
        # Check for translation
        if language and hasattr(q, 'question_translations') and q.question_translations:
            for trans in q.question_translations:
                if trans.language == language and trans.translated_question:
                    question_text = trans.translated_question
                    break
        
        # Strip HTML if present
        from frappe.utils import strip_html_tags
        question_text = strip_html_tags(question_text) if question_text else ""
        
        # Get options - options child table (QuizOptionList) contains links to QuizOption
        options = {"option_a": "", "option_b": "", "option_c": "", "option_d": ""}
        if hasattr(q, 'options') and q.options:
            for i, opt_row in enumerate(q.options[:4]):
                letter = OPTION_LETTERS[i].lower()  # a, b, c, d
                
                # opt_row.options contains the QuizOption ID (like "77", "78")
                option_id = opt_row.options
                if option_id:
                    # Fetch actual option text from QuizOption DocType
                    option_text = frappe.db.get_value("QuizOption", option_id, "option_text") or ""
                    options[f"option_{letter}"] = strip_html_tags(option_text) if option_text else ""
        
        # Get correct option letter (correct_option is 1-based index: 1=A, 2=B, 3=C, 4=D)
        correct_num = cint(q.correct_option)
        correct_letter = OPTION_LETTERS[correct_num - 1] if 1 <= correct_num <= 4 else "A"
        
        return {
            "question_id": question_id,
            "question": question_text,
            "question_type": getattr(q, 'question_type', 'Multiple Choice'),
            "option_a": options["option_a"],
            "option_b": options["option_b"],
            "option_c": options["option_c"],
            "option_d": options["option_d"],
            "correct_option": correct_letter
        }
    except Exception as e:
        frappe.log_error(f"get_question_details error for {question_id}: {str(e)}")
        return {"question_id": question_id, "error": str(e)}


# ============================================================
# API 5: SUBMIT ANSWER
# ============================================================

@frappe.whitelist(allow_guest=False)
def submit_answer(**_kw):
    """[DEPRECATED 2026-05-28] Use summer_program.student_progression_sp.submit_answer."""
    return journey_deprecated_response("submit_answer", _kw)


def _DEPRECATED_submit_answer_original(student_id: str, quiz_attempt_id: str, question_index: int, answer: str, language: str = None):
    """
    Submit an answer for current question.
    Time tracking handled server-side.
    """
    try:
        if not all([student_id, quiz_attempt_id, question_index, answer]):
            return {"success": False, "error": "All parameters required"}
        
        question_index = cint(question_index)
        answer = answer.strip().upper()
        
        if answer not in OPTION_LETTERS:
            return {"success": False, "error": f"Invalid answer. Must be A, B, C, or D"}
        
        resolved_student = resolve_student_id(student_id)
        if not resolved_student:
            return {"success": False, "error": f"Student not found: {student_id}"}
        
        # Get attempt
        if not frappe.db.exists("StudentQuizAttempt", quiz_attempt_id):
            return {"success": False, "error": f"Quiz attempt not found: {quiz_attempt_id}"}
        
        attempt = frappe.get_doc("StudentQuizAttempt", quiz_attempt_id)
        
        if attempt.student != resolved_student:
            return {"success": False, "error": "Attempt does not belong to this student"}
        
        if attempt.status != "in_progress":
            return {"success": False, "error": "Quiz attempt is not in progress"}
        
        if question_index < 1 or question_index > attempt.total_questions:
            return {"success": False, "error": f"Invalid question_index. Must be 1-{attempt.total_questions}"}
        
        # Get quiz and questions
        quiz_doc = frappe.get_doc("Quiz", attempt.quiz)
        questions = get_quiz_questions(quiz_doc)
        question_row = questions[question_index - 1]
        
        # Get correct answer
        question_details = get_question_details(question_row.question)
        correct_option = question_details.get("correct_option", "A")
        is_correct = (answer == correct_option)
        
        # Calculate time spent
        started_at = attempt.question_started_at or attempt.started_at
        answered_at = now_datetime()
        time_spent = cint(time_diff_in_seconds(answered_at, started_at))
        
        # Save answer
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
                "question": question_row.question,
                "selected_option": answer,
                "correct_option": correct_option,
                "is_correct": 1 if is_correct else 0,
                "started_at": started_at,
                "answered_at": answered_at,
                "time_spent_seconds": time_spent
            })
        
        attempt.current_question_index = question_index
        attempt.correct_answers = sum(1 for a in attempt.answers if a.is_correct)
        
        # Check if last question
        if question_index >= attempt.total_questions:
            return complete_quiz(attempt, quiz_doc, questions, language)
        
        # Update for next question
        attempt.question_started_at = now_datetime()
        attempt.save(ignore_permissions=True)
        # L-017: commit kept — legacy journey/ submit_answer mid-quiz path.
        # The commit is between saving the answer row and the subsequent
        # update_progress() write on StudentStageProgress. Removing it would
        # rely on Frappe's end-of-request commit, which is fine in the happy
        # path but loses durability of the answer if a downstream call later
        # raises. The teacher dashboard reads answers + progress together;
        # keeping the commit preserves the historical "answers are durable
        # the moment submit_answer returns" guarantee for that consumer.
        frappe.db.commit()

        # Update progress
        progress = frappe.db.get_value(
            "StudentStageProgress", attempt.student_progress,
            ["name"], as_dict=True
        )
        if progress:
            update_progress(progress["name"], {"question_started_at": now_datetime()})
        
        # Get next question
        next_question_row = questions[question_index]  # 0-based, so current index = next question
        next_question = get_question_details(next_question_row.question, language)
        
        return {
            "success": True,
            "action": "next_question",
            "answer_result": {
                "question_index": question_index,
                "selected_answer": answer,
                "correct_answer": correct_option,
                "was_correct": is_correct,
                "time_spent_seconds": time_spent
            },
            "progress": {
                "answered": question_index,
                "total": attempt.total_questions,
                "correct": attempt.correct_answers,
                "percentage": round((question_index / attempt.total_questions) * 100, 1)
            },
            "question": {
                "index": question_index + 1,
                "id": next_question_row.question,
                "text": next_question.get("question"),
                "type": next_question.get("question_type", "Multiple Choice"),
                "options": {
                    "A": next_question.get("option_a"),
                    "B": next_question.get("option_b"),
                    "C": next_question.get("option_c"),
                    "D": next_question.get("option_d")
                },
                "correct_option": next_question.get("correct_option")
            }
        }
        
    except Exception as e:
        frappe.log_error(f"submit_answer error: {str(e)}", "Student Progression API")
        return {"success": False, "error": str(e)}


def complete_quiz(attempt, quiz_doc, questions, language: str = None) -> dict:
    """Complete quiz attempt and determine next action."""
    # Calculate final score
    correct_count = sum(1 for a in attempt.answers if a.is_correct)
    total = attempt.total_questions
    score = (correct_count / total * 100) if total > 0 else 0
    passed = score >= flt(attempt.passing_score)
    
    # Calculate total time
    total_time = cint(time_diff_in_seconds(now_datetime(), attempt.started_at))
    
    # Update attempt
    attempt.status = "completed"
    attempt.completed_at = now_datetime()
    attempt.score = score
    attempt.correct_answers = correct_count
    attempt.passed = 1 if passed else 0
    attempt.time_spent_seconds = total_time
    attempt.save(ignore_permissions=True)
    # L-017: commit kept — legacy journey/ complete_quiz. The terminal write
    # marks the attempt completed and stores final score. Downstream work in
    # complete_quiz reads back StudentStageProgress (different table) and may
    # invoke remediation logic that the teacher dashboard expects to be
    # idempotent against the committed attempt row. Removing this commit
    # would couple attempt-completion durability to whatever happens after
    # this return, including any caller that might raise. Conservative: keep.
    frappe.db.commit()

    # Get progress
    progress = frappe.db.get_value(
        "StudentStageProgress", attempt.student_progress,
        ["name", "student", "stage", "current_week", "current_tier", "is_on_remedial",
         "remedial_attempts", "current_content_index", "course_context"],
        as_dict=True
    )
    
    course_level = progress["course_context"]
    
    # Clear quiz state
    update_progress(progress["name"], {
        "active_quiz_attempt": None,
        "active_content_type": None,
        "active_content_id": None,
        "content_started_at": None,
        "question_started_at": None
    })
    
    # Enqueue background jobs
    frappe.enqueue(
        "tap_lms.journey.background_jobs.job_log_content_completion",
        queue="short",
        timeout=60,
        student_id=progress["student"],
        course_level=course_level,
        progress_name=progress["name"],
        content_type="Quiz",
        content_id=attempt.quiz,
        action="completed" if passed else "failed",
        score=score,
        max_score=100,
        passed=passed,
        time_spent_seconds=total_time,
        quiz_attempt=attempt.name,
        stage_no=progress["current_week"],
        tier=progress["current_tier"],
        learning_unit=progress["stage"]
    )
    
    frappe.enqueue(
        "tap_lms.journey.background_jobs.job_update_statistics",
        queue="short",
        timeout=30,
        progress_name=progress["name"],
        content_completed=1,
        quiz_passed=1 if passed else 0,
        quiz_failed=0 if passed else 1,
        time_spent=total_time
    )
    
    # Build base response
    last_answer = attempt.answers[-1] if attempt.answers else None
    response = {
        "success": True,
        "action": "quiz_passed" if passed else "quiz_failed",
        "answer_result": {
            "question_index": attempt.total_questions,
            "selected_answer": last_answer.selected_option if last_answer else None,
            "correct_answer": last_answer.correct_option if last_answer else None,
            "was_correct": bool(last_answer.is_correct) if last_answer else False,
            "time_spent_seconds": last_answer.time_spent_seconds if last_answer else 0
        },
        "quiz_result": {
            "score": round(score, 1),
            "correct": correct_count,
            "total": total,
            "passed": passed,
            "passing_score": attempt.passing_score,
            "time_spent_seconds": total_time
        }
    }
    
    # Handle progression based on pass/fail
    if passed:
        next_action = handle_quiz_passed(progress, course_level)
    else:
        next_action = handle_quiz_failed(progress, course_level)
    
    response.update(next_action)
    return response


def handle_quiz_passed(progress: dict, course_level: str) -> dict:
    """Handle quiz pass - advance or exit remedial."""
    is_remedial = bool(progress.get("is_on_remedial"))
    
    if is_remedial:
        # Exit remedial to next week
        current_week = cint(progress["current_week"])
        next_week = current_week + 1
        next_tier = get_tier_for_week(next_week)
        
        if not check_week_exists(course_level, next_week):
            update_progress(progress["name"], {
                "status": "completed",
                "is_on_remedial": 0,
                "completion_timestamp": now_datetime()
            })
            return {
                "next_action": "course_complete",
                "message": "Congratulations! Course completed!"
            }
        
        first_lu = get_first_learning_unit(course_level, next_week, next_tier)
        if not first_lu:
            return {"next_action": "error", "error": f"No LU for Week {next_week}"}
        
        update_progress(progress["name"], {
            "stage": first_lu,
            "current_week": next_week,
            "current_tier": next_tier,
            "current_content_index": 0,
            "is_on_remedial": 0
        })
        
        content_items = get_content_items(first_lu)
        first_content = content_items[0] if content_items else None
        
        return {
            "next_action": "exit_remedial",
            "message": f"Great job! Moving to Stage {next_week}!",
            "new_week": next_week,
            "new_tier": next_tier,
            "next_content": {
                "type": first_content["content_type"],
                "id": first_content["content"],
                "name": get_content_name(first_content["content_type"], first_content["content"])
            } if first_content else None
        }
    
    else:
        # Normal advancement
        result = advance_to_next_content(progress, course_level)
        result["next_action"] = result.pop("action", "next_content")
        return result


def handle_quiz_failed(progress: dict, course_level: str) -> dict:
    """Handle quiz fail - switch to remedial or restart."""
    is_remedial = bool(progress.get("is_on_remedial"))
    current_week = cint(progress["current_week"])
    
    if is_remedial:
        # Check if more content after quiz
        content_items = get_content_items(progress["stage"])
        current_index = cint(progress["current_content_index"])
        
        if current_index < len(content_items) - 1:
            # More content - continue
            new_index = current_index + 1
            update_progress(progress["name"], {"current_content_index": new_index})
            next_content = content_items[new_index]
            
            return {
                "next_action": "continue_remedial",
                "message": "Keep practicing! Continue with next content.",
                "next_content": {
                    "type": next_content["content_type"],
                    "id": next_content["content"],
                    "name": get_content_name(next_content["content_type"], next_content["content"])
                }
            }
        else:
            # Restart remedial
            remedial_attempts = cint(progress.get("remedial_attempts", 0)) + 1
            update_progress(progress["name"], {
                "current_content_index": 0,
                "remedial_attempts": remedial_attempts
            })
            
            first_content = content_items[0] if content_items else None
            return {
                "next_action": "restart_remedial",
                "message": "Let's review from the beginning.",
                "remedial_attempt": remedial_attempts,
                "next_content": {
                    "type": first_content["content_type"],
                    "id": first_content["content"],
                    "name": get_content_name(first_content["content_type"], first_content["content"])
                } if first_content else None
            }
    
    else:
        # Switch to remedial
        remedial_lu = get_first_learning_unit(course_level, current_week, REMEDIAL_TIER)
        
        if not remedial_lu:
            return {
                "next_action": "error",
                "error": f"No remedial content for Week {current_week}"
            }
        
        update_progress(progress["name"], {
            "stage": remedial_lu,
            "current_tier": REMEDIAL_TIER,
            "current_content_index": 0,
            "is_on_remedial": 1,
            "remedial_attempts": 1
        })
        
        content_items = get_content_items(remedial_lu)
        first_content = content_items[0] if content_items else None
        lu_info = get_learning_unit_info(remedial_lu)
        
        return {
            "next_action": "switched_to_remedial",
            "message": "Let's review with some extra content.",
            "new_tier": REMEDIAL_TIER,
            "new_learning_unit": remedial_lu,
            "new_learning_unit_name": lu_info["name"] if lu_info else None,
            "next_content": {
                "type": first_content["content_type"],
                "id": first_content["content"],
                "name": get_content_name(first_content["content_type"], first_content["content"])
            } if first_content else None
        }


# ============================================================
# API 6: GET QUIZ STATUS
# ============================================================

@frappe.whitelist(allow_guest=False)
def get_quiz_status(**_kw):
    """[DEPRECATED 2026-05-28] Quiz status surfaces in summer_program/program_enrollment_api.get_student_state (quiz fields are flattened into the PE state response)."""
    return journey_deprecated_response("get_quiz_status", _kw)


def _DEPRECATED_get_quiz_status_original(student_id: str, course_level: str):
    """
    Check if student has an active quiz attempt.
    Used for resume detection.
    """
    try:
        if not student_id or not course_level:
            return {"success": False, "error": "student_id and course_level required"}
        
        resolved_student = resolve_student_id(student_id)
        if not resolved_student:
            return {"success": False, "error": f"Student not found: {student_id}"}
        
        progress = frappe.db.get_value(
            "StudentStageProgress",
            {"student": resolved_student, "course_context": course_level, "stage_type": "LearningUnit"},
            ["active_quiz_attempt"],
            as_dict=True
        )
        
        if not progress or not progress.get("active_quiz_attempt"):
            return {"success": True, "has_active_quiz": False}
        
        attempt = frappe.db.get_value(
            "StudentQuizAttempt",
            progress["active_quiz_attempt"],
            ["name", "quiz", "quiz_name", "started_at", "total_questions", "current_question_index", "status"],
            as_dict=True
        )
        
        if not attempt or attempt.get("status") != "in_progress":
            return {"success": True, "has_active_quiz": False}
        
        # Count answered questions
        answered = frappe.db.count("StudentQuizAnswer", {"parent": attempt["name"]})
        
        return {
            "success": True,
            "has_active_quiz": True,
            "quiz_attempt": {
                "id": attempt["name"],
                "quiz_id": attempt["quiz"],
                "quiz_name": attempt["quiz_name"],
                "started_at": str(attempt["started_at"]) if attempt["started_at"] else None,
                "questions_answered": answered,
                "total_questions": attempt["total_questions"],
                "current_question": answered + 1
            }
        }
        
    except Exception as e:
        frappe.log_error(f"get_quiz_status error: {str(e)}", "Student Progression API")
        return {"success": False, "error": str(e)}


# ============================================================
# API 7: GET STUDENT PROGRESS
# ============================================================

@frappe.whitelist(allow_guest=False)
def get_student_progress_overview(**_kw):
    """[DEPRECATED 2026-05-28] Use summer_program/program_enrollment_api.get_student_state — flat-map response with all SP state fields."""
    return journey_deprecated_response("get_student_progress_overview", _kw)


def _DEPRECATED_get_student_progress_overview_original(student_id: str, course_level: str):
    """
    Get comprehensive progress overview for dashboard.
    """
    try:
        if not student_id or not course_level:
            return {"success": False, "error": "student_id and course_level required"}
        
        resolved_student = resolve_student_id(student_id)
        if not resolved_student:
            return {"success": False, "error": f"Student not found: {student_id}"}
        
        # Get student info
        student = frappe.db.get_value(
            "Student", resolved_student,
            ["name", "name1", "phone", "glific_id"],
            as_dict=True
        )
        
        # Get course info
        course = frappe.db.get_value("Course Level", course_level, ["name", "name1"], as_dict=True)
        if not course:
            return {"success": False, "error": f"Course not found: {course_level}"}
        
        # Get progress
        progress = get_student_progress(resolved_student, course_level)
        
        # Get total weeks
        total_weeks = frappe.db.sql("""
            SELECT MAX(week_no) as max_week FROM `tabCourseLevelLU`
            WHERE parent = %s AND parenttype = 'Course Level'
        """, course_level, as_dict=True)
        total_weeks = total_weeks[0].max_week if total_weeks and total_weeks[0].max_week else 1
        
        # Get current content
        content_items = get_content_items(progress["stage"])
        current_index = cint(progress["current_content_index"])
        current_content = None
        if current_index < len(content_items):
            item = content_items[current_index]
            current_content = {
                "type": item["content_type"],
                "id": item["content"],
                "order": current_index + 1
            }
        
        # Check for active quiz
        active_quiz = None
        if progress.get("active_quiz_attempt"):
            attempt = frappe.db.get_value(
                "StudentQuizAttempt", progress["active_quiz_attempt"],
                ["name", "quiz", "total_questions", "current_question_index"],
                as_dict=True
            )
            if attempt:
                answered = frappe.db.count("StudentQuizAnswer", {"parent": attempt["name"]})
                active_quiz = {
                    "quiz_attempt_id": attempt["name"],
                    "quiz_id": attempt["quiz"],
                    "questions_answered": answered,
                    "total_questions": attempt["total_questions"]
                }
        
        lu_info = get_learning_unit_info(progress["stage"])
        
        return {
            "success": True,
            "student": {
                "id": student["name"],
                "name": student.get("name1"),
                "phone": student.get("phone"),
                "glific_id": student.get("glific_id")
            },
            "course": {
                "id": course["name"],
                "name": course.get("name1"),
                "total_weeks": cint(total_weeks)
            },
            "current_position": {
                "week": cint(progress["current_week"]),
                "tier": progress["current_tier"],
                "learning_unit": progress["stage"],
                "learning_unit_name": lu_info["name"] if lu_info else None,
                "content_index": current_index,
                "is_remedial": bool(progress.get("is_on_remedial")),
                "remedial_attempts": cint(progress.get("remedial_attempts", 0))
            },
            "current_content": current_content,
            "active_quiz": active_quiz,
            "progress": {
                "weeks_completed": cint(progress["current_week"]) - 1,
                "current_week_progress": round((current_index / len(content_items)) * 100, 1) if content_items else 0,
                "overall_progress": round(((cint(progress["current_week"]) - 1) / cint(total_weeks)) * 100, 1)
            },
            "statistics": {
                "content_completed": cint(progress.get("total_content_completed", 0)),
                "quizzes_passed": cint(progress.get("total_quizzes_passed", 0)),
                "quizzes_failed": cint(progress.get("total_quizzes_failed", 0)),
                "total_time_minutes": round(cint(progress.get("total_time_spent_seconds", 0)) / 60, 1)
            },
            "status": progress.get("status", "assigned"),
            "started_at": str(progress.get("start_timestamp")) if progress.get("start_timestamp") else None,
            "last_activity": str(progress.get("last_activity_timestamp")) if progress.get("last_activity_timestamp") else None
        }
        
    except Exception as e:
        frappe.log_error(f"get_student_progress_overview error: {str(e)}", "Student Progression API")
        return {"success": False, "error": str(e)}


# ============================================================
# API 8: GET STUDENT HISTORY
# ============================================================

@frappe.whitelist(allow_guest=False)
def get_student_history(**_kw):
    """[DEPRECATED 2026-05-28] No direct SP equivalent. If you need completion history, query StudentContentLog / ProgramEventLog directly via the Frappe REST API."""
    return journey_deprecated_response("get_student_history", _kw)


def _DEPRECATED_get_student_history_original(student_id: str, course_level: str, limit: int = 50, offset: int = 0):
    """
    Get detailed content completion history.
    """
    try:
        if not student_id or not course_level:
            return {"success": False, "error": "student_id and course_level required"}
        
        resolved_student = resolve_student_id(student_id)
        if not resolved_student:
            return {"success": False, "error": f"Student not found: {student_id}"}
        
        limit = min(cint(limit) or 50, 100)
        offset = cint(offset) or 0
        
        # Get total count
        total = frappe.db.count("StudentContentLog", {
            "student": resolved_student,
            "course_level": course_level
        })
        
        # Get history
        history = frappe.db.sql("""
            SELECT 
                completed_at as timestamp,
                stage_no as week,
                tier,
                content_type,
                content_name,
                action,
                score,
                passed,
                time_spent_seconds
            FROM `tabStudentContentLog`
            WHERE student = %s AND course_level = %s
            ORDER BY completed_at DESC
            LIMIT %s OFFSET %s
        """, (resolved_student, course_level, limit, offset), as_dict=True)
        
        return {
            "success": True,
            "total_records": total,
            "returned": len(history),
            "limit": limit,
            "offset": offset,
            "history": [
                {
                    "timestamp": str(h["timestamp"]) if h["timestamp"] else None,
                    "week": h["week"],
                    "tier": h["tier"],
                    "content_type": h["content_type"],
                    "content_name": h["content_name"],
                    "action": h["action"],
                    "score": h["score"],
                    "passed": bool(h["passed"]) if h["passed"] is not None else None,
                    "time_spent_seconds": h["time_spent_seconds"]
                }
                for h in history
            ]
        }
        
    except Exception as e:
        frappe.log_error(f"get_student_history error: {str(e)}", "Student Progression API")
        return {"success": False, "error": str(e)}
