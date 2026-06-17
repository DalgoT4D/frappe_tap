"""
Summer Program API Endpoints
tap_lms/summer_program/api.py

Whitelisted API endpoints for the Summer Program.
"""
import frappe
import json
from frappe import _
from frappe.utils import now_datetime, today, getdate, date_diff

from tap_lms.summer_program.constants import (
    BPR_ACTIVE,
    BPR_COLLECTIONS_READY,
    ALL_ARCHETYPES,
    ALL_ARMS,
    ACTION_FLOW_FIELD_MAP,
    PROGRAM_ACTIVE,
    PROGRAM_PAUSED,
)


@frappe.whitelist()
def get_bpr_status(bpr_name):
    """
    Get current status and stats for a BatchProgramRun.
    Used by the BatchProgramRun page to show progress.
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)
    batch = frappe.get_doc("Batch", bpr.batch)

    return {
        "name": bpr.name,
        "batch": bpr.batch,
        "batch_id": batch.batch_id,
        "status": bpr.status,
        "validation_status": bpr.validation_status,
        "total_imported": bpr.total_imported or 0,
        "total_enrolled": bpr.total_enrolled or 0,
        "enrollment_progress": (
            round((bpr.total_enrolled or 0) / max(bpr.total_imported or 1, 1) * 100, 1)
        ),
        "counts": {
            "dormant": bpr.dormant_count or 0,
            "fence_sitter": bpr.fence_sitter_count or 0,
            "irregular_submitter": bpr.irregular_submitter_count or 0,
            "submitter": bpr.submitter_count or 0,
            "arm_a": bpr.arm_a_count or 0,
            "arm_b": bpr.arm_b_count or 0,
            "arm_default": bpr.arm_default_count or 0,
        },
        "collections": len(bpr.pg_collections),
        "onboarding_sets": len(bpr.pg_onboarding_sets),
        "flows": {
            action: getattr(bpr, field, None)
            for action, field in ACTION_FLOW_FIELD_MAP.items()
        },
        "timestamps": {
            "enrollment_started": str(bpr.enrollment_started_at) if bpr.enrollment_started_at else None,
            "enrollment_completed": str(bpr.enrollment_completed_at) if bpr.enrollment_completed_at else None,
            "collections_created": str(bpr.collections_created_at) if bpr.collections_created_at else None,
            "activated": str(bpr.activated_at) if bpr.activated_at else None,
        },
    }


@frappe.whitelist()
def list_bprs(batch_name=None):
    """
    List all BatchProgramRuns, optionally filtered by batch.
    """
    filters = {}
    if batch_name:
        filters["batch"] = batch_name

    bprs = frappe.get_all(
        "BatchProgramRun",
        filters=filters,
        fields=[
            "name", "batch", "status", "validation_status",
            "total_imported", "total_enrolled",
            "activated_at", "modified",
        ],
        order_by="modified desc",
    )

    return bprs


@frappe.whitelist()
def get_collection_details(bpr_name):
    """
    Get detailed collection information for a BPR.
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)

    collections = []
    for c in bpr.pg_collections:
        collections.append({
            "label": c.collection_label,
            "glific_group_id": c.glific_group_id,
            "archetype": c.archetype,
            "experiment_arm": c.experiment_arm,
            "student_count": c.student_count or 0,
        })

    onboarding_sets = []
    for s in bpr.pg_onboarding_sets:
        onboarding_sets.append({
            "onboarding_set": s.onboarding_set,
            "glific_contact_group": s.glific_contact_group,
            "student_count": s.student_count or 0,
            "processing_status": s.processing_status,
        })

    return {
        "collections": collections,
        "onboarding_sets": onboarding_sets,
    }


@frappe.whitelist()
def get_batch_progress(bpr_name):
    """
    Get weekly progress overview for an active BPR.
    Shows current week, archetype distribution, and activity.
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)
    batch = frappe.get_doc("Batch", bpr.batch)

    if not batch.start_date:
        return {"error": "Batch has no start_date"}

    days = date_diff(today(), batch.start_date)
    current_week = max(0, (days // 7) + 1) if days >= 0 else 0
    total_weeks = batch.total_weeks or 0

    # Get activity summary for enrolled students
    from tap_lms.summer_program.enrollment import _get_students_for_bpr
    student_ids = _get_students_for_bpr(bpr)

    active_today = 0
    if student_ids:
        active_today = frappe.db.count(
            "EngagementState",
            filters={
                "student": ["in", student_ids],
                "last_activity_date": today(),
            },
        )

    return {
        "current_week": current_week,
        "total_weeks": total_weeks,
        "weeks_remaining": max(0, total_weeks - current_week),
        "batch_started": days >= 0,
        "active_today": active_today,
        "total_enrolled": bpr.total_enrolled or 0,
        "activity_rate": (
            round(active_today / max(bpr.total_enrolled or 1, 1) * 100, 1)
        ),
    }


@frappe.whitelist()
def update_flow_ids(bpr_name, flows):
    """
    Update Glific flow IDs on a BatchProgramRun.

    Args:
        bpr_name: BatchProgramRun name
        flows: dict mapping action_type → flow_id
               e.g. {"content_delivery": 123, "escalation": 456}
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)

    if isinstance(flows, str):
        flows = json.loads(flows)

    updated = []
    for action_type, flow_id in flows.items():
        field_name = ACTION_FLOW_FIELD_MAP.get(action_type)
        if field_name:
            setattr(bpr, field_name, int(flow_id) if flow_id else None)
            updated.append(action_type)

    bpr.save(ignore_permissions=True)
    frappe.db.commit()

    return {"updated": updated}


@frappe.whitelist()
def get_student_sp_status(student_id):
    """
    Get a student's Summer Program status — which BPR they belong to,
    their archetype, arm, and current activity.
    """
    student = frappe.get_doc("Student", student_id)

    # Find BPRs the student is currently enrolled in via ProgramEnrollment
    # (canonical SP source). The legacy Student.enrollment child table is
    # populated only by backend onboarding and misses students enrolled
    # through start_program_enrollment for a new SP batch (root cause of
    # 2026-05-19 "no_active_batch" incident).
    bpr_info = None
    pe_batches = frappe.get_all(
        "ProgramEnrollment",
        filters={
            "student": student.name,
            "program_status": ["in", [PROGRAM_ACTIVE, PROGRAM_PAUSED]],
        },
        fields=["batch"],
        order_by="creation desc",
    )
    for pe in pe_batches:
        if not pe.batch:
            continue
        bprs = frappe.get_all(
            "BatchProgramRun",
            filters={"batch": pe.batch},
            fields=["name", "status"],
        )
        if bprs:
            bpr_info = bprs[0]
            break

    # Get engagement state
    engagement = frappe.get_all(
        "EngagementState",
        filters={"student": student_id},
        fields=["last_activity_date", "current_streak", "completion_rate"],
        limit=1,
    )

    return {
        "student_id": student.name,
        "student_name": student.name1,
        "archetype": student.archetype,
        "experiment_arm": student.experiment_arm,
        "glific_id": student.glific_id,
        "bpr": bpr_info,
        "engagement": engagement[0] if engagement else None,
    }
