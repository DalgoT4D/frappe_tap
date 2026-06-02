"""
Program Enrollment APIs
tap_lms/summer_program/program_enrollment_api.py

Bulk:  start_program_enrollment — bulk-create PEs for all students in a BPR (pipeline step 3)
API A6: create_program_enrollment — creates single PE + sets 14 Glific contact fields
API A8: get_enrollment_summary — aggregated stats for admin dashboard
API A1: get_student_state — fallback when contact fields may be stale
"""
import frappe
import json
from frappe import _
from frappe.utils import now_datetime, cint

from tap_lms.glific_integration import update_contact_fields, add_contact_to_group
from tap_lms.summer_program.constants import (
    STATE_NORMAL_CONTENT,
    LABEL_ENROLLED, PROGRAM_ACTIVE,
    PATH_CORE, TIER_BY_WEEK, DEFAULT_TIER,
    CF_STUDENT_ID, CF_BATCH_ID, CF_ARCHETYPE, CF_LANGUAGE_ID,
    CF_RESOLVED_FLOW_STATE, CF_CURRENT_WEEK, CF_CURRENT_PATH,
    CF_CURRENT_TIER, CF_PROGRAM_STATUS, CF_TOTAL_POINTS,
    CF_CURRENT_STREAK, CF_GRACE_WINDOW_END, CF_EXPECTED_SUBMISSION,
    CF_EXPERIMENT_ARM,
    CF_COURSE_LEVEL, CF_STUDENT_NAME,
    CF_LAST_ESCALATION_STEP, CF_SUBMISSION_COUNT,
    # CR-002 v2 gamification (8 fields, all initialized to 0 at enrollment)
    CF_TOTAL_ACTIVITY_POINTS, CF_WEEKLY_ACTIVITY_POINTS,
    CF_TOTAL_QUIZ_POINTS, CF_WEEKLY_QUIZ_POINTS,
    CF_TOTAL_SUBMISSION_POINTS, CF_WEEKLY_SUBMISSION_POINTS,
    CF_SPECIAL_GEMS, CF_WEEKLY_SUBMISSION_DONE,
    # Task #98 (2026-05-25): bonus_quiz_points added to enrollment-time push
    # so the contact has the field from day one (and the Glific gamification
    # card never renders the literal @contact.fields.bonus_quiz_points text).
    CF_BONUS_QUIZ_POINTS,
    # Task #7 (2026-05-26): weekly_engagement_points is COMPUTED at every
    # push site as weekly_submission_points + weekly_activity_points. Seeded
    # at "0" here so the Glific contact has the field from day one.
    CF_WEEKLY_ENGAGEMENT_POINTS,
    # CR-003 escalation routing (2 fields, initialized to empty/0 — no
    # escalation at enrollment time)
    CF_ESCALATION_ORDER, CF_ESCALATION_TYPE,
    ALL_ARCHETYPES, TERMINAL_STATES,
    ENROLLMENT_CHUNK_SIZE, ENROLLMENT_QUEUE,
    BPR_COLLECTIONS_READY,
)
from tap_lms.summer_program.event_log import log_event
from tap_lms.summer_program.enrollment import _commit_with_serialization_retry
from tap_lms.summer_program.utils import (
    get_student_display_name,
    normalize_unicode_surrogates,
)


# ════════════════════════════════════════════════════════════
# BULK PROGRAM ENROLLMENT (Pipeline Step — after collections_ready)
# ════════════════════════════════════════════════════════════

@frappe.whitelist(allow_guest=False)
def start_program_enrollment(bpr_name):
    """
    Bulk-create ProgramEnrollment records for all students in a BPR.

    This is a SEPARATE pipeline step from enrollment.py (which handles
    Glific contact field updates and collection setup).

    Pipeline order:
      1. start_enrollment      → Glific field updates (enrolling)
      2. setup_collections     → Glific collections (collections_ready)
      3. start_program_enrollment → PE records (this function)
      4. validate_bpr          → Readiness check
      5. activate_bpr          → Go live

    Runs in background chunks via frappe.enqueue.

    Args:
        bpr_name: BatchProgramRun document name

    Returns:
        dict with total students queued
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)
    batch = frappe.get_doc("Batch", bpr.batch)

    if bpr.status not in (BPR_COLLECTIONS_READY, "active"):
        return {
            "success": False,
            "error": f"BPR status must be '{BPR_COLLECTIONS_READY}' or 'active', "
                     f"currently '{bpr.status}'",
        }

    # Gather student IDs from the BPR's onboarding sets
    from tap_lms.summer_program.enrollment import _get_students_for_bpr
    student_ids = _get_students_for_bpr(bpr)

    if not student_ids:
        return {"success": False, "error": "No students found for this BPR"}

    # Filter out students who already have a PE for this batch
    existing_pes = set(frappe.get_all(
        "ProgramEnrollment",
        filters={"batch": bpr.batch, "student": ["in", student_ids]},
        pluck="student",
    ))
    new_students = [sid for sid in student_ids if sid not in existing_pes]

    # ── Sibling-skip preview (interim, pre-sibling-PRD) ─────────────
    # Pre-compute which candidates will hit the sibling-skip in
    # _process_pe_chunk so we can surface the count up-front rather than
    # silently dropping them in the chunk workers. The chunk workers
    # still do their own re-check (race-safe with the partial unique
    # index), but the preview gives operators visibility into the
    # expected drop count before the chunks fire.
    #
    # KNOWN UNDERCOUNT: this preview only catches siblings of ALREADY-
    # enrolled PEs. If two candidates in the same `new_students` list
    # share a glific_id (intra-batch sibling cluster, neither yet
    # enrolled), neither shows up here — the count understates by 1 per
    # such cluster. The chunk worker still skips correctly (first wins,
    # second hits the sibling check OR the DB unique index), so this is
    # a display-only undercount, not a correctness bug.
    siblings_to_skip = 0
    if new_students:
        # Glific IDs already in use by active/paused PEs in this batch
        existing_glific_ids = set(frappe.db.sql_list("""
            SELECT DISTINCT glific_id FROM "tabProgramEnrollment"
             WHERE batch = %s
               AND program_status IN ('active', 'paused')
               AND glific_id IS NOT NULL
               AND glific_id != ''
        """, (bpr.batch,)))
        if existing_glific_ids:
            # Look up each candidate's glific_id and count collisions
            candidate_gids = frappe.db.sql("""
                SELECT name, glific_id FROM "tabStudent"
                 WHERE name IN %s
                   AND glific_id IS NOT NULL
                   AND glific_id != ''
            """, (tuple(new_students),), as_dict=True)
            for row in candidate_gids:
                if row["glific_id"] in existing_glific_ids:
                    siblings_to_skip += 1

    if not new_students:
        return {
            "success": True,
            "message": "All students already have ProgramEnrollment records",
            "total": len(student_ids),
            "already_enrolled": len(existing_pes),
            "new": 0,
            "siblings_to_skip": 0,
        }

    # Enqueue in chunks
    total_chunks = (len(new_students) - 1) // ENROLLMENT_CHUNK_SIZE + 1
    for i in range(0, len(new_students), ENROLLMENT_CHUNK_SIZE):
        chunk = new_students[i : i + ENROLLMENT_CHUNK_SIZE]
        frappe.enqueue(
            "tap_lms.summer_program.program_enrollment_api._process_pe_chunk",
            queue=ENROLLMENT_QUEUE,
            timeout=600,
            bpr_name=bpr_name,
            batch_name=bpr.batch,
            student_ids=chunk,
            chunk_index=i // ENROLLMENT_CHUNK_SIZE,
        )

    expected_to_create = len(new_students) - siblings_to_skip
    sibling_note = (
        f" ({siblings_to_skip} will be skipped as siblings of existing PEs.)"
        if siblings_to_skip else ""
    )
    frappe.msgprint(
        f"Program Enrollment started: {len(new_students)} candidates in "
        f"{total_chunks} chunks; ~{expected_to_create} new PEs expected. "
        f"({len(existing_pes)} already enrolled, skipped.)" + sibling_note,
        alert=True,
    )

    return {
        "success": True,
        "total": len(student_ids),
        "already_enrolled": len(existing_pes),
        "new": len(new_students),
        "siblings_to_skip": siblings_to_skip,
        "chunks": total_chunks,
    }


def _process_pe_chunk(bpr_name, batch_name, student_ids, chunk_index):
    """
    Background job: create ProgramEnrollment for a chunk of students.

    For each student:
      1. Create PE record (same logic as create_program_enrollment)
      2. Set 14 Glific contact fields
      3. Log enrollment event
    """
    batch = frappe.get_doc("Batch", batch_name)
    created = 0
    skipped = 0
    errors = []

    for sid in student_ids:
        try:
            # Skip if PE already exists (idempotency)
            existing = frappe.db.get_value(
                "ProgramEnrollment",
                {"student": sid, "batch": batch_name,
                 "program_status": ["not in", ["dropped"]]},
                "name",
            )
            if existing:
                skipped += 1
                continue

            student = frappe.get_doc("Student", sid)
            archetype = student.archetype
            experiment_arm = student.experiment_arm or "default"
            language = getattr(student, "language", None)
            glific_id = student.glific_id

            if not archetype:
                errors.append(f"{sid}: no archetype")
                continue

            # ── Sibling skip (interim, pre-sibling-PRD) ──────────────
            # If another active/paused PE in this batch already uses the
            # same glific_id, this candidate is a sibling on a shared
            # household WhatsApp number. Per the pending sibling PRD,
            # the interim policy is "one PE per Glific contact per
            # batch" — skip the duplicate at enrollment time.
            #
            # The partial unique index on (batch, glific_id) WHERE
            # glific_id != '' AND program_status IN ('active','paused')
            # — installed by patches/v0_2/add_pe_glific_id_unique_index.py
            # — is the race-safe DB guarantee. This app-level check is
            # the friendly UX layer that logs structured context so ops
            # can find skipped siblings via grep.
            if glific_id:
                sibling_pe = frappe.db.get_value(
                    "ProgramEnrollment",
                    {
                        "batch": batch_name,
                        "glific_id": glific_id,
                        "program_status": ["in", ["active", "paused"]],
                    },
                    "name",
                )
                if sibling_pe:
                    frappe.logger().info(
                        f"sp_enrollment_skipped_sibling: "
                        f"student={sid}, glific_id={glific_id}, "
                        f"existing_pe={sibling_pe}, batch={batch_name}"
                    )
                    skipped += 1
                    continue

            course_level = _resolve_course_level(student, batch)
            expected_submission = _get_week1_submission_type(batch, archetype, experiment_arm)

            # Create PE
            pe = frappe.new_doc("ProgramEnrollment")
            # Use batch.name (unique doc ID), not batch.batch_id (user-editable field — can collide)
            pe.enrollment = f"{sid}-{batch.name}"
            pe.student = sid
            pe.batch = batch_name
            pe.program_type = batch.program_type or "Summer"
            pe.glific_id = glific_id or ""
            pe.course_level = course_level
            pe.language = language
            pe.experiment_arm = experiment_arm
            pe.archetype = archetype
            pe.current_path = PATH_CORE
            pe.current_tier = TIER_BY_WEEK.get(1, "Basic")
            pe.journey_label = LABEL_ENROLLED
            pe.last_label_change_at = now_datetime()
            pe.program_status = PROGRAM_ACTIVE
            pe.resolved_flow_state = STATE_NORMAL_CONTENT
            pe.current_expected_submission_type = expected_submission
            pe.current_week = 1
            pe.max_allowed_week = (batch.current_calendar_week or 1) + 1
            pe.total_points = 0
            pe.current_streak = 0
            pe.pause_count = 0
            pe.submission_count = 0
            pe.quiz_completed = 0
            pe.in_grace_window = 0
            pe.current_escalation_step = 0
            pe.current_escalation_type = ""
            pe.delivery_failure_count = 0
            pe.re_engagement_count = 0
            pe.next_action_at = None
            pe.next_action_type = ""

            # Race-safety: even with the pre-flight sibling check above, two
            # parallel chunk workers could both see "no sibling" and both
            # attempt to insert. The partial unique index on (batch,
            # glific_id) WHERE active/paused — installed by
            # patches/v0_2/add_pe_glific_id_unique_index.py — guarantees only
            # one insert wins. The loser raises DuplicateEntryError, which we
            # treat as another sibling-skip (a sibling chunk worker got there
            # first). rollback() is required to clear the failed transaction
            # before we continue with the next student in this chunk.
            try:
                pe.insert(ignore_permissions=True)
            except frappe.DuplicateEntryError:
                frappe.db.rollback()
                frappe.logger().info(
                    f"sp_enrollment_skipped_sibling_race: "
                    f"student={sid}, glific_id={glific_id}, batch={batch_name} "
                    f"— DB unique index rejected the insert; a parallel worker "
                    f"won the race for this glific_id."
                )
                skipped += 1
                continue

            # Set 28 Glific contact fields — async via retry-aware background job
            # so transient Glific outages don't lose the enrollment-time push.
            #
            # Cache size = 28 fields = 7 identity + 21 state. The 8 CR-002 v2
            # gamification fields and 2 CR-003 escalation fields are all
            # initialized to "0" (or "" for escalation_type) so Glific flows
            # see consistent values from day-one, not just after the first
            # state transition fires _enqueue_contact_field_sync. See the
            # field-provenance docstring on _enqueue_contact_field_sync for
            # the complete list of fields and their sources.
            if glific_id:
                # Resolve TAP Language → Glific integer language ID for the
                # custom `language_id` contact field. The CORE Glific
                # language is set separately on the contact record (via
                # create_contact's languageId arg in the chunk worker's
                # downstream path / process_glific_contact for existing).
                glific_language_id = ""
                if language:
                    glific_language_id = str(
                        frappe.db.get_value(
                            "TAP Language", language, "glific_language_id"
                        ) or ""
                    )
                fields = {
                    # ── 7 IDENTITY fields (immutable post-enrollment) ──
                    CF_STUDENT_ID: sid,
                    CF_BATCH_ID: batch.batch_id or batch_name,
                    CF_ARCHETYPE: archetype,
                    CF_LANGUAGE_ID: glific_language_id,
                    CF_EXPERIMENT_ARM: experiment_arm or "",
                    CF_COURSE_LEVEL: course_level or "",
                    CF_STUDENT_NAME: get_student_display_name(student),
                    # ── 11 base STATE fields (re-synced on transitions) ──
                    CF_RESOLVED_FLOW_STATE: STATE_NORMAL_CONTENT,
                    CF_CURRENT_WEEK: "1",
                    CF_CURRENT_PATH: PATH_CORE,
                    CF_CURRENT_TIER: TIER_BY_WEEK.get(1, "Basic"),
                    CF_PROGRAM_STATUS: PROGRAM_ACTIVE,
                    CF_TOTAL_POINTS: "0",
                    CF_CURRENT_STREAK: "0",
                    CF_GRACE_WINDOW_END: "",
                    CF_EXPECTED_SUBMISSION: expected_submission or "",
                    CF_LAST_ESCALATION_STEP: "0",
                    CF_SUBMISSION_COUNT: "0",
                    # ── 9 CR-002 v2 gamification (all 0 at enrollment) ──
                    # Includes bonus_quiz_points (added 2026-05-25 task #98)
                    # so the Glific gamification card's
                    # @contact.fields.bonus_quiz_points resolves from day one.
                    CF_TOTAL_ACTIVITY_POINTS: "0",
                    CF_WEEKLY_ACTIVITY_POINTS: "0",
                    CF_TOTAL_QUIZ_POINTS: "0",
                    CF_WEEKLY_QUIZ_POINTS: "0",
                    CF_TOTAL_SUBMISSION_POINTS: "0",
                    CF_WEEKLY_SUBMISSION_POINTS: "0",
                    CF_SPECIAL_GEMS: "0",
                    CF_WEEKLY_SUBMISSION_DONE: "0",
                    # Task #98 (2026-05-25): bonus_quiz_points seeded at 0
                    # so the Glific gamification card finds the field on
                    # day one and doesn't render the literal template text.
                    CF_BONUS_QUIZ_POINTS: "0",
                    # Task #7 (2026-05-26): weekly_engagement_points seeded
                    # at "0" — both addends are 0 at enrollment.
                    CF_WEEKLY_ENGAGEMENT_POINTS: "0",
                    # ── 2 CR-003 escalation routing (empty at enrollment) ──
                    CF_ESCALATION_ORDER: "0",
                    CF_ESCALATION_TYPE: "",
                }
                frappe.enqueue(
                    "tap_lms.summer_program.state_machine._sync_contact_fields_job",
                    queue="short",
                    timeout=30,
                    enqueue_after_commit=True,
                    glific_id=str(glific_id),
                    fields=fields,
                    pe_name=pe.name,
                    retry_count=0,
                    student_id=sid,
                )

            # Log
            log_event(pe, "archetype_assigned", new_value=archetype,
                      trigger_source="admin",
                      details={"experiment_arm": experiment_arm, "batch": batch_name})

            created += 1

        except Exception as e:
            frappe.log_error(
                f"PE creation error for {sid}: {str(e)}",
                "SP Program Enrollment Chunk",
            )
            errors.append(f"{sid}: {str(e)}")

    # This commit flushes the per-student enqueue_after_commit Glific sync jobs
    # in their own transaction — independent of the contended BPR counter
    # UPDATE below (Fix 1 / BR-001). The counter conflict can never roll the
    # sync work back because it already committed here.
    frappe.db.commit()

    # Recompute BPR.total_enrolled from the authoritative PE row count, in its
    # own retry-wrapped transaction. The shared BPR row is contended by all
    # ~72 Phase-3 chunk workers (1000 students/chunk — MORE contention than
    # Phase 1); without retry a SerializationFailure here leaves the count
    # stale until a later chunk happens to recompute it, and the final chunk's
    # conflict would strand it permanently. L-071.
    def _recompute_counter():
        frappe.db.sql("""
            UPDATE `tabBatchProgramRun`
            SET total_enrolled = (
                SELECT COUNT(*) FROM `tabProgramEnrollment`
                WHERE batch = (SELECT batch FROM `tabBatchProgramRun` WHERE name = %s)
                  AND program_status != 'dropped'
            )
            WHERE name = %s
        """, (bpr_name, bpr_name))

    _commit_with_serialization_retry(
        _recompute_counter,
        context=f"bpr={bpr_name} phase=3 recompute",
    )

    frappe.logger().info(
        f"SP PE chunk {chunk_index}: created={created}, skipped={skipped}, "
        f"errors={len(errors)} for BPR {bpr_name}"
    )


@frappe.whitelist(allow_guest=False)
def create_program_enrollment(student_id, batch_id, archetype=None,
                               experiment_arm=None, language=None,
                               course_level=None, glific_group_id=None):
    """
    API A6: create_program_enrollment

    Creates a ProgramEnrollment record, sets 14 Glific contact fields,
    and optionally adds student to a Glific collection.

    Called during onboarding Step 3a for each student.

    Args:
        student_id: Student document name
        batch_id: Batch document name
        archetype: Student archetype (defaults to Student.archetype)
        experiment_arm: Experiment arm (defaults to Student.experiment_arm)
        language: Language (defaults to Student.language)
        course_level: Course Level name (resolved if not provided)
        glific_group_id: Glific collection group ID to add student to

    Returns:
        dict with PE name, enrollment details
    """
    if not frappe.db.exists("Student", student_id):
        return {"success": False, "error": f"Student {student_id} not found"}

    if not frappe.db.exists("Batch", batch_id):
        return {"success": False, "error": f"Batch {batch_id} not found"}

    student = frappe.get_doc("Student", student_id)
    batch = frappe.get_doc("Batch", batch_id)

    # Resolve defaults from Student record
    archetype = archetype or student.archetype
    experiment_arm = experiment_arm or student.experiment_arm or "default"
    language = language or getattr(student, "language", None)
    glific_id = student.glific_id

    if not archetype:
        return {"success": False, "error": "Student has no archetype assigned"}

    course_level = normalize_unicode_surrogates(course_level)

    # Resolve course level if not provided
    if not course_level:
        course_level = _resolve_course_level(student, batch)

    # Get initial WeekRule for expected submission type
    expected_submission = _get_week1_submission_type(batch, archetype, experiment_arm)

    # Check for existing active PE
    existing = frappe.db.get_value(
        "ProgramEnrollment",
        {"student": student_id, "batch": batch_id,
         "program_status": ["not in", ["dropped"]]},
        "name",
    )
    if existing:
        return {
            "success": False,
            "error": "Student already enrolled in this batch",
            "enrollment": existing,
        }

    # ── Sibling skip (interim, pre-sibling-PRD) ──────────────
    # If another active/paused PE in this batch already uses the same
    # glific_id, this candidate is a sibling sharing a household WhatsApp
    # number. Skip the duplicate at enrollment time. See _process_pe_chunk
    # for the matching logic in the batch path, and patches/v0_2/
    # add_pe_glific_id_unique_index.py for the race-safe DB constraint.
    if glific_id:
        sibling_pe = frappe.db.get_value(
            "ProgramEnrollment",
            {
                "batch": batch_id,
                "glific_id": glific_id,
                "program_status": ["in", ["active", "paused"]],
            },
            "name",
        )
        if sibling_pe:
            frappe.logger().info(
                f"sp_enrollment_skipped_sibling: "
                f"student={student_id}, glific_id={glific_id}, "
                f"existing_pe={sibling_pe}, batch={batch_id}"
            )
            return {
                "success": False,
                "skipped": True,
                "reason": "sibling_enrolled",
                "error": f"Glific contact already enrolled in this batch (PE {sibling_pe})",
                "existing_pe": sibling_pe,
            }

    # ── Create ProgramEnrollment ────────────────────────────
    pe = frappe.new_doc("ProgramEnrollment")
    # Use batch.name (unique doc ID), not batch.batch_id (user-editable field — can collide)
    pe.enrollment = f"{student_id}-{batch.name}"
    pe.student = student_id
    pe.batch = batch_id
    pe.program_type = batch.program_type or "Summer"
    pe.glific_id = glific_id or ""
    pe.course_level = course_level
    pe.language = language
    pe.experiment_arm = experiment_arm
    pe.archetype = archetype
    pe.current_path = PATH_CORE
    pe.current_tier = TIER_BY_WEEK.get(1, "Basic")
    pe.journey_label = LABEL_ENROLLED
    pe.last_label_change_at = now_datetime()
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.current_expected_submission_type = expected_submission
    pe.current_week = 1
    pe.max_allowed_week = (batch.current_calendar_week or 1) + 1
    pe.total_points = 0
    pe.current_streak = 0
    pe.pause_count = 0
    pe.submission_count = 0
    pe.quiz_completed = 0
    pe.in_grace_window = 0
    pe.current_escalation_step = 0
    pe.current_escalation_type = ""
    pe.delivery_failure_count = 0
    pe.re_engagement_count = 0

    # next_action_at = NULL for batch start (collection trigger handles it)
    pe.next_action_at = None
    pe.next_action_type = ""

    pe.insert(ignore_permissions=True)

    # ── Set 28 Glific Contact Fields ────────────────────────
    # Async via retry-aware background job (pattern P-007) so transient Glific
    # outages don't lose the enrollment-time push.
    # Mirror of _process_pe_chunk's enrollment push — keep these two in sync.
    # See _enqueue_contact_field_sync for the field-provenance docstring.
    if glific_id:
        # Resolve TAP Language → Glific integer language ID for the custom
        # `language_id` contact field. CORE Glific language is set separately
        # on the contact record by create_contact / process_glific_contact.
        glific_language_id = ""
        if language:
            glific_language_id = str(
                frappe.db.get_value(
                    "TAP Language", language, "glific_language_id"
                ) or ""
            )
        fields = {
            # ── 7 IDENTITY fields ──
            CF_STUDENT_ID: student_id,
            CF_BATCH_ID: batch.batch_id or batch_id,
            CF_ARCHETYPE: archetype,
            CF_LANGUAGE_ID: glific_language_id,
            CF_EXPERIMENT_ARM: experiment_arm or "",
            CF_COURSE_LEVEL: course_level or "",
            CF_STUDENT_NAME: get_student_display_name(student),
            # ── 11 base STATE fields ──
            CF_RESOLVED_FLOW_STATE: STATE_NORMAL_CONTENT,
            CF_CURRENT_WEEK: "1",
            CF_CURRENT_PATH: PATH_CORE,
            CF_CURRENT_TIER: TIER_BY_WEEK.get(1, "Basic"),
            CF_PROGRAM_STATUS: PROGRAM_ACTIVE,
            CF_TOTAL_POINTS: "0",
            CF_CURRENT_STREAK: "0",
            CF_GRACE_WINDOW_END: "",
            CF_EXPECTED_SUBMISSION: expected_submission or "",
            CF_LAST_ESCALATION_STEP: "0",
            CF_SUBMISSION_COUNT: "0",
            # ── 8 CR-002 v2 gamification (all 0 at enrollment) ──
            CF_TOTAL_ACTIVITY_POINTS: "0",
            CF_WEEKLY_ACTIVITY_POINTS: "0",
            CF_TOTAL_QUIZ_POINTS: "0",
            CF_WEEKLY_QUIZ_POINTS: "0",
            CF_TOTAL_SUBMISSION_POINTS: "0",
            CF_WEEKLY_SUBMISSION_POINTS: "0",
            CF_SPECIAL_GEMS: "0",
            CF_WEEKLY_SUBMISSION_DONE: "0",
            # Task #98 (2026-05-25): bonus_quiz_points seeded at 0 — see
            # _process_pe_chunk for full rationale.
            CF_BONUS_QUIZ_POINTS: "0",
            # Task #7 (2026-05-26): weekly_engagement_points seeded at "0".
            CF_WEEKLY_ENGAGEMENT_POINTS: "0",
            # ── 2 CR-003 escalation routing (empty at enrollment) ──
            CF_ESCALATION_ORDER: "0",
            CF_ESCALATION_TYPE: "",
        }
        frappe.enqueue(
            "tap_lms.summer_program.state_machine._sync_contact_fields_job",
            queue="short",
            timeout=30,
            enqueue_after_commit=True,
            glific_id=str(glific_id),
            fields=fields,
            pe_name=pe.name,
            retry_count=0,
            student_id=student_id,
        )

        # Add to collection if group_id provided
        if glific_group_id:
            try:
                add_contact_to_group(str(glific_id), str(glific_group_id))
            except Exception as e:
                frappe.log_error(
                    f"Glific group add error: {str(e)}", "SP Enrollment Collection"
                )

    # ── Log event ──────────────────────────────────────────
    log_event(pe, "archetype_assigned", new_value=archetype,
              trigger_source="admin",
              details={
                  "experiment_arm": experiment_arm,
                  "archetype": archetype,
                  "batch": batch_id,
              })

    # Removed mid-handler commit per L-017 — Frappe commits at request-end.

    return {
        "success": True,
        "enrollment": pe.name,
        "student_id": student_id,
        "batch": batch_id,
        "archetype": archetype,
        "experiment_arm": experiment_arm,
        "resolved_flow_state": pe.resolved_flow_state,
        "current_week": pe.current_week,
        "current_path": pe.current_path,
    }


@frappe.whitelist(allow_guest=False)
def get_enrollment_summary(batch_id):
    """
    API A8: get_enrollment_summary

    Aggregated stats for admin dashboard. Breaks down by:
      - archetype × resolved_flow_state
      - archetype × experiment_arm
      - per-week submission rates

    Args:
        batch_id: Batch document name

    Returns:
        dict with aggregated enrollment statistics
    """
    if not frappe.db.exists("Batch", batch_id):
        return {"success": False, "error": "Batch not found"}

    batch = frappe.get_doc("Batch", batch_id)

    # Total enrollments
    total = frappe.db.count("ProgramEnrollment", {"batch": batch_id})

    # By resolved_flow_state
    state_counts = frappe.db.sql("""
        SELECT resolved_flow_state, COUNT(*) as count
        FROM `tabProgramEnrollment`
        WHERE batch = %s
        GROUP BY resolved_flow_state
    """, batch_id, as_dict=True)

    # By archetype × program_status
    archetype_status = frappe.db.sql("""
        SELECT archetype, program_status, COUNT(*) as count
        FROM `tabProgramEnrollment`
        WHERE batch = %s
        GROUP BY archetype, program_status
    """, batch_id, as_dict=True)

    # By archetype × experiment_arm
    archetype_arm = frappe.db.sql("""
        SELECT archetype, experiment_arm, COUNT(*) as count
        FROM `tabProgramEnrollment`
        WHERE batch = %s
        GROUP BY archetype, experiment_arm
    """, batch_id, as_dict=True)

    # Per-week submission counts
    week_submissions = frappe.db.sql("""
        SELECT current_week,
               COUNT(*) as total,
               SUM(CASE WHEN submission_count > 0 THEN 1 ELSE 0 END) as submitted,
               SUM(CASE WHEN in_grace_window = 1 THEN 1 ELSE 0 END) as in_grace,
               SUM(CASE WHEN program_status = 'paused' THEN 1 ELSE 0 END) as paused
        FROM `tabProgramEnrollment`
        WHERE batch = %s AND program_status != 'dropped'
        GROUP BY current_week
        ORDER BY current_week
    """, batch_id, as_dict=True)

    # Active vs completed vs paused vs dropped
    status_summary = frappe.db.sql("""
        SELECT program_status, COUNT(*) as count
        FROM `tabProgramEnrollment`
        WHERE batch = %s
        GROUP BY program_status
    """, batch_id, as_dict=True)

    return {
        "success": True,
        "batch": batch_id,
        "total_enrolled": total,
        "total_weeks": batch.total_weeks or 0,
        "current_calendar_week": batch.current_calendar_week or 0,
        "by_state": {r.resolved_flow_state: r["count"] for r in state_counts},
        "by_archetype_status": archetype_status,
        "by_archetype_arm": archetype_arm,
        "by_week": week_submissions,
        "by_program_status": {r.program_status: r["count"] for r in status_summary},
    }


@frappe.whitelist(allow_guest=False)
def get_student_state(student_id):
    """
    API A1: get_student_state

    Fallback API when Glific contact fields may be stale OR missing.
    Returns full Glific-parity PE state as a single flat-map response.

    Used by Glific flows that:
      (a) hit a stale contact.fields snapshot (flow execution captured the
          contact state before the latest sync landed), or
      (b) need a key that's not pushed to contact.fields (none currently —
          bonus_quiz_points was added 2026-05-25 — but a defensive single
          source of truth saves debugging time when this kind of drift
          appears in the future).

    Field parity with the Glific contact-field sync payload:
      Identity (7): student_id, student_name, batch_id, archetype,
                    language_id, experiment_arm, course_level
      State (21):   resolved_flow_state, current_week, current_path,
                    current_tier, program_status, total_points,
                    current_streak, grace_window_end_at,
                    current_expected_submission_type, last_escalation_step,
                    submission_count, total_activity_points,
                    weekly_activity_points, total_quiz_points,
                    weekly_quiz_points, total_submission_points,
                    weekly_submission_points, special_gems,
                    weekly_submission_done, bonus_quiz_points,
                    escalation_order, escalation_type
      Bonus (debug): name (PE doc name), batch (PE.batch doc name),
                    journey_label, in_grace_window, program_type, language,
                    current_escalation_type, glific_id

    Response (via frappe.local.response per docs/api-standard-glific.md Rule 1):
        Flat dict with `success` + `status` + PE fields. Does NOT return.

    Last expanded 2026-05-25 (task #99) for full Glific parity in
    response to the ST00051295 "Submission Missing!" template-render bug.
    """
    student_id = _resolve_student(student_id)
    if not student_id:
        frappe.local.response.update({
            "success": False, "status": "not_found",
            "error_detail": "Student not found",
        })
        return

    # A student can have multiple non-dropped PEs over time (re-enrollments
    # into the same or different batches). Return the most recently MODIFIED
    # one — that's the row whose state was last advanced, i.e. the "live"
    # enrollment. `creation desc` would prefer the newest row even if a
    # paused-but-stale newer row outranks an actively-progressing older row.
    pe_data = frappe.db.get_value(
        "ProgramEnrollment",
        {"student": student_id, "program_status": ["not in", ["dropped"]]},
        [
            "name", "batch", "program_type", "archetype", "experiment_arm",
            "resolved_flow_state", "journey_label", "program_status",
            "current_week", "current_path", "current_tier",
            "total_points", "current_streak", "in_grace_window",
            "grace_window_end_at", "current_expected_submission_type",
            "submission_count", "current_escalation_step",
            "current_escalation_type", "course_level",
            "language", "glific_id",
            # CR-002 v2 — 9 gamification fields. Flat-map per
            # docs/api-standard-glific.md Rule 1 (no nesting, no arrays).
            # `weekly_video_done` is internal-only and intentionally omitted.
            # `bonus_quiz_points` added 2026-05-25 (task #99) for full
            # Glific-parity — Glific flow template references it.
            "total_activity_points", "weekly_activity_points",
            "total_quiz_points", "weekly_quiz_points",
            "total_submission_points", "weekly_submission_points",
            "special_gems", "weekly_submission_done",
            "bonus_quiz_points",
        ],
        as_dict=True,
        order_by="modified desc",
    )

    if not pe_data:
        frappe.local.response.update({
            "success": False, "status": "no_enrollment_found",
            "error_detail": "No ProgramEnrollment found",
        })
        return

    # Public scoreboard total is the canonical stream sum. Do not trust a
    # drifted stored total_points value here; get_student_state is the Glific
    # fallback used specifically when cached/displayed state may be stale.
    pe_data["total_points"] = _canonical_total_points(pe_data)

    # L-008 (Glific public contract): the Glific contact field + webhook
    # response key is `last_escalation_step`, despite the PE column being
    # renamed to `current_escalation_step` in this CR. Re-key the dict so
    # SP_Incoming_Router and any other flow reading
    # `@results.webhook.last_escalation_step` continues to work.
    # `current_escalation_type` is genuinely new — it uses its natural name.
    if "current_escalation_step" in pe_data:
        pe_data["last_escalation_step"] = pe_data.pop("current_escalation_step")

    # Task #99 (2026-05-25): also expose `escalation_order` for symmetry
    # with the Glific contact-field sync payload (`last_escalation_step`
    # and `escalation_order` share the same source — pe.current_escalation_step
    # — and Glific has both keys, so we return both here too).
    pe_data["escalation_order"] = pe_data.get("last_escalation_step", 0)

    # Task #13 (2026-05-28): mirror the escalation_order pattern for
    # `escalation_type` — the Glific contact field is `escalation_type`
    # (not `current_escalation_type`), and bootstrap_sp_contact_fields
    # registers it under that name. Flows that call get_student_state as a
    # webhook and read `@results.webhook.escalation_type` previously got
    # nothing (only `current_escalation_type` was in the response). Expose
    # both keys for symmetry; downstream consumers can use either.
    pe_data["escalation_type"] = pe_data.get("current_escalation_type") or ""

    # Task #7 (2026-05-26): weekly_engagement_points is COMPUTED, not stored
    # — defined as weekly_submission_points + weekly_activity_points. Returned
    # here so flows calling get_student_state as a webhook (instead of reading
    # @contact.fields.weekly_engagement_points) get the same value without an
    # extra round-trip.
    pe_data["weekly_engagement_points"] = (
        (pe_data.get("weekly_submission_points") or 0)
        + (pe_data.get("weekly_activity_points") or 0)
    )

    # Identity fields (7) — fetch from joined Student / Batch / TAP Language
    # rows. These are CHEAP single-row lookups via db.get_value, NOT full
    # doc hydrations (which would pull child tables on every API call).
    student_name = frappe.db.get_value("Student", student_id, "name1") or ""

    batch_id = ""
    if pe_data.get("batch"):
        batch_id = frappe.db.get_value("Batch", pe_data["batch"], "batch_id") or ""

    language_id = ""
    if pe_data.get("language"):
        language_id = str(
            frappe.db.get_value("TAP Language", pe_data["language"], "glific_language_id") or ""
        )

    frappe.local.response.update({
        "success": True,
        "status": "ok",
        "student_id": student_id,
        "student_name": student_name,
        "batch_id": batch_id,
        "language_id": language_id,
        **pe_data,
    })


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════

def _resolve_student(identifier):
    """Delegate to shared utility."""
    from tap_lms.summer_program.utils import resolve_student
    return resolve_student(identifier)


def _canonical_total_points(pe_data):
    """Return the public total score from persisted point streams."""
    return (
        cint(pe_data.get("total_activity_points") or 0)
        + cint(pe_data.get("total_quiz_points") or 0)
        + cint(pe_data.get("total_submission_points") or 0)
        + cint(pe_data.get("bonus_quiz_points") or 0)
    )


def _resolve_course_level(student, batch):
    """Get course level from student's enrollment in this batch.

    enrollment.course is a Link field pointing directly to a Course Level
    document name, so no extra lookup is needed.
    """
    if not student.enrollment:
        return None
    for enrollment in student.enrollment:
        if enrollment.batch == batch.name and enrollment.course:
            return normalize_unicode_surrogates(enrollment.course)
    return None


def _get_expected_submission_type_for_week(batch, archetype, experiment_arm,
                                            path, week):
    """Resolve expected_submission_type from ArchetypeConfig + WeekRule.

    Matches the ArchetypeConfig by (batch, archetype, experiment_arm, path)
    with `is_active=1`. Falls back to `experiment_arm='default'` if the
    requested arm has no config — same fallback as the enrollment-time
    lookup, so resets and updates stay consistent with how enrollment
    originally seeded the PE.

    Returns the `expected_submission_type` string from the matching
    WeekRule child row, or None if no matching config / week rule exists.

    Single source of truth for "what submission type does THIS PE expect
    THIS week" — used by enrollment (week=1, Core), dev_tools reset
    (week=1, Core), and dev_tools.update_student_state (any week/path/
    archetype/arm change — task #84). All three callers stay in sync.
    """
    config = frappe.db.get_value(
        "ArchetypeConfig",
        {
            "batch": batch.name,
            "experiment_arm": experiment_arm,
            "archetype": archetype,
            "path": path,
            "is_active": 1,
        },
        "name",
    )
    if not config:
        # Fallback to default arm — same fallback as the enrollment lookup.
        config = frappe.db.get_value(
            "ArchetypeConfig",
            {
                "batch": batch.name,
                "experiment_arm": "default",
                "archetype": archetype,
                "path": path,
                "is_active": 1,
            },
            "name",
        )
    if not config:
        return None

    return frappe.db.get_value(
        "WeekRule",
        {"parent": config, "parenttype": "ArchetypeConfig", "week": week},
        "expected_submission_type",
    )


def _get_week1_submission_type(batch, archetype, experiment_arm):
    """Backwards-compatible thin wrapper — week 1, Core path.

    Kept for callers that don't care about other weeks (enrollment flow,
    reset_pe_to_state_0). New code should call the general helper directly.
    """
    return _get_expected_submission_type_for_week(
        batch, archetype, experiment_arm, PATH_CORE, 1,
    )
