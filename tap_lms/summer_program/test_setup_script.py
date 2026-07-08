"""
Manual test-scenario setup for Summer Program testing.

Drop this file into the tap_lms app, e.g.:
    frappe_tap/tap_lms/summer_program/test_setup_script.py
(no migrate needed — it's a plain module, no doctype changes)

USAGE
-----
Option A -- bench execute (no interactive console needed):

    bench --site <site> execute \\
        tap_lms.summer_program.test_setup_script.run_test_setup \\
        --kwargs '{
            "assignment_name": "MockAssign",
            "difficulty_tier": "Basic",
            "student_name": "Test Student",
            "student_phone": "9876543210",
            "batch": "palv2-test-BT52231",
            "bpr_name": "hsugtupp28",
            "reset_existing": true,
            "trigger_content_delivery": false
        }'

Option B -- bench console:

    bench --site <site> console
    >>> from tap_lms.summer_program.test_setup_script import run_test_setup
    >>> run_test_setup(
    ...     assignment_name="MockAssign",
    ...     difficulty_tier="Basic",
    ...     student_name="Test Student",
    ...     student_phone="9876543210",
    ...     batch="palv2-test-BT52231",
    ...     bpr_name="hsugtupp28",
    ...     reset_existing=True,
    ... )

WHAT IT DOES (in order)
------------------------
1. Creates the Assignment if it doesn't already exist (no-op if it does).
2. Ensures the BatchProgramRun is 'active' -- calls activate_bpr() if it
   isn't, but refuses to blindly re-validate a BPR that's already moved
   past collections_ready (e.g. active -> completed), since validate_bpr()
   itself requires status == collections_ready and would just error out.
   If validation_status isn't 'passed' here, it stops and asks you to look.
3. Creates/reuses the Student + ProgramEnrollment via dev_tools'
   create_test_student_with_pe (idempotent on phone+name and student+batch).
4. Optionally resets that student's PE to state 0 and deletes their prior
   Submission rows for this specific assignment (reset_existing=True).
5. Checks the BPR's 'main' Glific collection and adds the student's contact
   to it if they have a glific_id and aren't already synced.
6. Optionally fires content delivery for THIS BATCH ONLY, via a direct
   start_group_flow call -- NOT the site-wide weekly_content_delivery_trigger,
   which would fire for every active BPR on the site.
"""

import frappe

from tap_lms.summer_program.dev_tools import (
    create_test_student_with_pe,
    reset_pe_to_state_0,
)
from tap_lms.summer_program.batch_activation import activate_bpr
from tap_lms.summer_program.glific_extensions import start_group_flow
from tap_lms.glific_integration import add_contact_to_group


def _ensure_assignment(assignment_name, difficulty_tier, assignment_type="Written", max_score="10"):
    assign_id = f"{assignment_name}-{difficulty_tier}"
    if frappe.db.exists("Assignment", assign_id):
        print(f"[assignment] already exists: {assign_id}")
        return assign_id

    a = frappe.new_doc("Assignment")
    a.assignment_name = assignment_name
    a.difficulty_tier = difficulty_tier
    a.assignment_type = assignment_type
    a.max_score = max_score
    a.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"[assignment] created: {a.name}")
    return a.name


def _ensure_bpr_active(bpr_name):
    status = frappe.db.get_value("BatchProgramRun", bpr_name, "status")
    print(f"[bpr] {bpr_name} status = {status}")
    if status == "active":
        return status

    validation_status = frappe.db.get_value("BatchProgramRun", bpr_name, "validation_status")
    if validation_status != "passed":
        # Deliberately NOT auto-calling validate_bpr() here: it requires
        # status == 'collections_ready', which won't be true for a BPR
        # that's already been active/completed once. Auto-revalidating
        # would just fail with a confusing status error. Surface it
        # instead and let the operator decide.
        raise RuntimeError(
            f"BPR {bpr_name} validation_status={validation_status!r} (not 'passed'). "
            f"activate_bpr() requires validation_status == 'passed'. Inspect the BPR "
            f"manually (validation_report field) before proceeding -- this script "
            f"won't auto-run validate_bpr() since that call itself requires "
            f"status == 'collections_ready', which a previously-active BPR won't have."
        )

    result = activate_bpr(bpr_name)
    print(f"[bpr] activate_bpr result: {result}")
    if not result.get("success"):
        raise RuntimeError(f"activate_bpr failed for {bpr_name}: {result}")
    return "active"


def _sync_student_to_main_collection(student_id, bpr_name):
    main_col = frappe.db.get_value(
        "PGCollection",
        {"parent": bpr_name, "kind": "main"},
        ["name", "glific_group_id", "member_count"],
        as_dict=True,
    )
    if not main_col:
        raise RuntimeError(f"No 'main' PGCollection found under BPR {bpr_name}")
    print(f"[glific] main collection: {main_col}")

    glific_id = frappe.db.get_value("Student", student_id, "glific_id")
    if not glific_id:
        print(
            f"[glific] student {student_id} has no glific_id -- skipping group add "
            f"(likely enrolled with skip_glific_sync=True)"
        )
        return main_col

    added = add_contact_to_group(contact_id=glific_id, group_id=main_col["glific_group_id"])
    print(f"[glific] add_contact_to_group -> {added}")
    return main_col


def run_test_setup(
    assignment_name,
    difficulty_tier,
    student_name,
    student_phone,
    batch,
    bpr_name,
    archetype="submitter",
    experiment_arm="default",
    reset_existing=False,
    trigger_content_delivery=False,
    skip_glific_sync=True,
    i_know_this_is_destructive=False,
):
    """
    Full test-scenario setup: assignment + enrolled student + active batch.

    Args:
        assignment_name: Assignment.assignment_name, e.g. "MockAssign"
        difficulty_tier: Remedial | Basic | Intermediate | Advanced
        student_name: Student.name1 (display name), e.g. "Test Student"
        student_phone: 10-digit phone, e.g. "9876543210"
        batch: Batch doc name to enroll into, e.g. "palv2-test-BT52231"
        bpr_name: BatchProgramRun name for that batch, e.g. "hsugtupp28"
        archetype / experiment_arm: passed through to create_test_student_with_pe
        reset_existing: if True, reset the student's PE to state 0 and delete
            their prior Submission rows for this specific assignment
        trigger_content_delivery: if True, fires content delivery for THIS
            batch only (direct start_group_flow call). Leave False unless
            you specifically need delivery outside the Tuesday 09:00 IST cron.
        skip_glific_sync: passed through to create_test_student_with_pe; keep
            True to avoid real Glific HTTP calls during enrollment itself
        i_know_this_is_destructive: bypasses dev_tools' production-site name
            guard (only relevant if your site name contains prod/live/production)

    Returns:
        dict summary with assign_id, student_id, batch, bpr_name
    """
    print("=" * 60)
    print("STEP 1: Assignment")
    print("=" * 60)
    assign_id = _ensure_assignment(assignment_name, difficulty_tier)

    print("=" * 60)
    print("STEP 2: BatchProgramRun status")
    print("=" * 60)
    _ensure_bpr_active(bpr_name)

    print("=" * 60)
    print("STEP 3: Student + ProgramEnrollment")
    print("=" * 60)
    pe_result = create_test_student_with_pe(
        name=student_name,
        phone=student_phone,
        batch=batch,
        archetype=archetype,
        experiment_arm=experiment_arm,
        skip_glific_sync=skip_glific_sync,
        i_know_this_is_destructive=i_know_this_is_destructive,
    )
    print(f"[student] {pe_result}")
    student_id = pe_result["student_id"]

    print("=" * 60)
    print("STEP 4: Reset (optional)")
    print("=" * 60)
    if reset_existing:
        reset_result = reset_pe_to_state_0(
            student_id,
            push_to_glific=not skip_glific_sync,
            i_know_this_is_destructive=i_know_this_is_destructive,
        )
        print(f"[reset] {reset_result}")
        deleted = frappe.db.delete("Submission", {"student_id": student_id, "assign_id": assign_id})
        frappe.db.commit()
        print(f"[reset] deleted {deleted} prior submission(s) for {student_id} / {assign_id}")
    else:
        print("[reset] skipped (reset_existing=False)")

    print("=" * 60)
    print("STEP 5: Glific main-collection membership")
    print("=" * 60)
    main_col = _sync_student_to_main_collection(student_id, bpr_name)

    if trigger_content_delivery:
        print("=" * 60)
        print("STEP 6: Trigger content delivery (this batch only)")
        print("=" * 60)
        flow_id = frappe.db.get_value("BatchProgramRun", bpr_name, "content_delivery_flow")
        fired = start_group_flow(flow_id=str(flow_id), group_id=str(main_col["glific_group_id"]))
        print(f"[content_delivery] start_group_flow -> {fired}")

    print("=" * 60)
    print("DONE")
    print("=" * 60)
    summary = {
        "assign_id": assign_id,
        "student_id": student_id,
        "batch": batch,
        "bpr_name": bpr_name,
    }
    print(summary)
    return summary
