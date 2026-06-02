"""
Summer Program Enrollment
tap_lms/summer_program/enrollment.py

Step 3 of the pipeline:
  3a  SP Enrollment (local DB) — bulk create program records, update Glific contact fields
  3b  Collection Setup — create 8 archetype collections, bulk-add contacts

Uses frappe.enqueue (queue="long") for heavy work.
"""
import frappe
import json
import time
import random
import psycopg2.errors as pg_errors
from frappe.utils import now_datetime
from datetime import datetime, timezone

from tap_lms.glific_integration import (
    update_contact_fields,
    add_contact_to_group,
)
from tap_lms.summer_program.constants import (
    ALL_ARCHETYPES,
    ARM_A,
    ARM_B,
    COLLECTION_BATCH_SIZE,
    ENROLLMENT_CHUNK_SIZE,
    ENROLLMENT_QUEUE,
    BPR_ENROLLING,
    BPR_COLLECTIONS_READY,
    collection_label,
)
from tap_lms.summer_program.glific_extensions import (
    add_contacts_to_group_bulk,
    create_or_get_collection,
)


# ── Public API (called from the BatchProgramRun page) ────────


@frappe.whitelist()
def start_enrollment(bpr_name):
    """
    Kick off SP enrollment for a BatchProgramRun.
    Splits students into chunks and enqueues each chunk.

    Args:
        bpr_name: name of the BatchProgramRun document
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)
    batch = frappe.get_doc("Batch", bpr.batch)

    # Gather all students linked to this batch via onboarding sets
    student_ids = _get_students_for_bpr(bpr)
    if not student_ids:
        frappe.throw("No students found for this batch.")

    # Mark BPR as enrolling
    bpr.status = BPR_ENROLLING
    bpr.enrollment_started_at = now_datetime()
    bpr.total_imported = len(student_ids)
    bpr.save(ignore_permissions=True)
    frappe.db.commit()

    # Chunk and enqueue
    for i in range(0, len(student_ids), ENROLLMENT_CHUNK_SIZE):
        chunk = student_ids[i : i + ENROLLMENT_CHUNK_SIZE]
        frappe.enqueue(
            "tap_lms.summer_program.enrollment._process_enrollment_chunk",
            queue=ENROLLMENT_QUEUE,
            timeout=600,
            bpr_name=bpr_name,
            batch_name=bpr.batch,
            student_ids=chunk,
            chunk_index=i // ENROLLMENT_CHUNK_SIZE,
        )

    frappe.msgprint(
        f"Enrollment started for {len(student_ids)} students "
        f"in {(len(student_ids) - 1) // ENROLLMENT_CHUNK_SIZE + 1} chunks.",
        alert=True,
    )


@frappe.whitelist()
def setup_collections(bpr_name):
    """
    Step 3b: Create the 8 archetype×arm collections in Glific
    and bulk-add students to each.

    Call this AFTER enrollment is complete.
    """
    bpr = frappe.get_doc("BatchProgramRun", bpr_name)
    batch = frappe.get_doc("Batch", bpr.batch)
    batch_id = batch.batch_id

    collections_created = []

    for archetype in ALL_ARCHETYPES:
        for arm in [ARM_A, ARM_B]:
            label = collection_label(batch_id, archetype, arm)
            group = create_or_get_collection(
                label, f"Summer Program {batch_id} — {archetype} / {arm}"
            )
            if not group:
                frappe.log_error(
                    f"Failed to create collection: {label}",
                    "SP Collection Setup",
                )
                continue

            # Fetch glific_ids of students matching this archetype + arm
            glific_ids = frappe.get_all(
                "Student",
                filters={
                    "archetype": archetype,
                    "experiment_arm": arm,
                    "name": ["in", _get_enrolled_student_ids(bpr)],
                },
                pluck="glific_id",
            )
            # Filter out empty glific_ids
            glific_ids = [gid for gid in glific_ids if gid]

            # Bulk add in batches of COLLECTION_BATCH_SIZE
            for j in range(0, len(glific_ids), COLLECTION_BATCH_SIZE):
                batch_ids = glific_ids[j : j + COLLECTION_BATCH_SIZE]
                add_contacts_to_group_bulk(batch_ids, group["id"])

            # Record in BPR child table
            bpr.append("pg_collections", {
                "collection_label": label,
                "glific_group_id": group["id"],
                "archetype": archetype,
                "experiment_arm": arm,
                "student_count": len(glific_ids),
            })

            collections_created.append({
                "label": label,
                "group_id": group["id"],
                "students": len(glific_ids),
            })

    # Update counts on BPR
    _update_bpr_counts(bpr)
    bpr.status = BPR_COLLECTIONS_READY
    bpr.collections_created_at = now_datetime()
    bpr.save(ignore_permissions=True)
    frappe.db.commit()

    frappe.msgprint(
        f"Created {len(collections_created)} collections.", alert=True
    )
    return collections_created


# ── Background job (enqueued) ────────────────────────────────


def _process_enrollment_chunk(bpr_name, batch_name, student_ids, chunk_index):
    """
    Process a chunk of students:
      1. Update Glific contact fields (archetype, experiment_arm, program_type)
      2. Track enrollment count on BPR
    """
    batch = frappe.get_doc("Batch", batch_name)
    enrolled = 0

    for sid in student_ids:
        try:
            student = frappe.get_doc("Student", sid)
            glific_id = student.glific_id

            if not glific_id:
                frappe.logger().warning(
                    f"Student {sid} has no glific_id, skipping Glific field update"
                )
                continue

            # Update Glific contact fields for Summer Program — async via
            # retry-aware background job (pattern P-007). Failures get re-queued
            # up to GLIFIC_SYNC_MAX_RETRIES, then land in the DLQ Error Log.
            from tap_lms.summer_program.utils import get_student_display_name
            fields = {
                "archetype": student.archetype or "",
                "experiment_arm": student.experiment_arm or "",
                "program_type": "Summer",
                "batch_id": batch.batch_id or "",
                "course_level": getattr(student, "course_level", "") or "",
                "student_name": get_student_display_name(student),
            }
            frappe.enqueue(
                "tap_lms.summer_program.state_machine._sync_contact_fields_job",
                queue="short",
                timeout=30,
                enqueue_after_commit=True,
                glific_id=str(glific_id),
                fields=fields,
                pe_name=f"pre-pe:{sid}",
                retry_count=0,
                student_id=sid,
            )
            enrolled += 1

        except Exception as e:
            frappe.log_error(
                f"SP enrollment error for student {sid}: {str(e)}",
                "SP Enrollment Chunk",
            )

    # Flush the per-student enqueue_after_commit Glific sync jobs in their OWN
    # transaction BEFORE touching the contended BPR counter row. Previously the
    # sync enqueues shared a transaction with the counter UPDATE below, so a
    # SerializationFailure on the counter rolled the sync work back too,
    # silently dropping it (BR-001 / L-071). Committing here dispatches those
    # jobs independently of whether the counter UPDATE later conflicts.
    frappe.db.commit()

    # Counter UPDATE in its own retry-wrapped transaction. On exhaustion this
    # raises so the RQ job is recorded as failed (L-056) — the lost increment
    # surfaces in the DLQ rather than vanishing into a permanent undercount.
    _update_bpr_counter_with_retry(bpr_name, enrolled)

    # Stamp completion atomically. Multiple chunk workers can cross the
    # total_enrolled >= total_imported threshold near-simultaneously; a
    # reload()+save() race lets the last writer clobber concurrent counter
    # increments and re-stamp enrollment_completed_at. The guarded UPDATE +
    # RETURNING is the project idempotency primitive (L-010): only the worker
    # whose UPDATE actually flips the still-NULL stamp gets a row back; the
    # rest no-op. The WHERE re-reads total_enrolled/total_imported in the same
    # statement, so there is no stale-snapshot window.
    stamped = frappe.db.sql(
        """
        UPDATE `tabBatchProgramRun`
        SET enrollment_completed_at = %s
        WHERE name = %s
          AND enrollment_completed_at IS NULL
          AND total_enrolled >= total_imported
        RETURNING name
        """,
        (now_datetime(), bpr_name),
    )
    frappe.db.commit()
    if stamped:
        frappe.logger().info(f"SP enrollment complete for BPR {bpr_name}")


# ── Serialization-retry primitives (BR-001 / L-071) ─────────


def _commit_with_serialization_retry(do_write, *, context, max_retries=3):
    """Run a DB write + commit, retrying on transient Postgres write conflicts.

    `do_write` is a zero-arg callable that issues the UPDATE (but NOT the
    commit — this helper owns commit/rollback so each attempt is its own
    transaction). On `psycopg2.errors.SerializationFailure` /
    `DeadlockDetected` (the transient PG write-contention errors, L-071) we
    rollback and retry with exponential backoff + jitter: 100ms, 300ms, 900ms
    (+0-50ms). Any other exception is non-transient and propagates immediately
    after a rollback.

    On retry exhaustion we `log_error` with `context` then re-raise the last
    serialization error — the RQ job MUST see the failure (L-056), never a
    silent swallow.
    """
    backoffs = (0.1, 0.3, 0.9)
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            do_write()
            frappe.db.commit()
            return
        except (pg_errors.SerializationFailure, pg_errors.DeadlockDetected) as e:
            last_exc = e
            frappe.db.rollback()
            if attempt < max_retries:
                delay = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                time.sleep(delay + random.uniform(0, 0.05))
                continue
        except Exception:
            # Non-transient — do not retry; surface immediately.
            frappe.db.rollback()
            raise

    # Retries exhausted — log loudly with context, then re-raise so the RQ job
    # is recorded as failed (L-056) rather than silently undercounting.
    try:
        frappe.log_error(
            message=(
                f"BPR counter UPDATE: SerializationFailure exhausted after "
                f"{max_retries + 1} attempts. context={context} "
                f"last_exception={last_exc!r}"
            ),
            title="SP counter SerializationFailure exhausted",
        )
    except Exception:
        pass
    raise last_exc


def _update_bpr_counter_with_retry(bpr_name, increment, max_retries=3):
    """Increment BatchProgramRun.total_enrolled by `increment`, retry-safe.

    The shared BPR row is contended by every enrollment chunk worker; under
    concurrency Postgres raises SerializationFailure on overlapping UPDATEs
    (L-071). Each attempt commits in its own transaction, so a conflict only
    costs the increment — never the already-committed per-student Glific sync
    enqueues, which the caller flushed with its own commit before calling this.
    """
    def _do_update():
        # COALESCE: total_enrolled is a nullable Int (no JSON default); NULL + n
        # is NULL in Postgres and would permanently poison the counter (L-011).
        frappe.db.sql(
            """
            UPDATE `tabBatchProgramRun`
            SET total_enrolled = COALESCE(total_enrolled, 0) + %s
            WHERE name = %s
            """,
            (increment, bpr_name),
        )

    _commit_with_serialization_retry(
        _do_update,
        context=f"bpr={bpr_name} increment={increment}",
        max_retries=max_retries,
    )


# ── Helpers ──────────────────────────────────────────────────


def _get_students_for_bpr(bpr):
    """
    Get all student IDs linked to a BPR through its onboarding sets.
    Each PGOnboardingSet points to a Backend Student Onboarding set
    which was processed into Students.
    """
    student_ids = []
    for row in bpr.pg_onboarding_sets:
        # Get students from this onboarding set
        backend_students = frappe.get_all(
            "Backend Students",
            filters={
                "parent": row.onboarding_set,
                "processing_status": ["in", ["Processed", "Success"]],
                "student_id": ["is", "set"],
            },
            pluck="student_id",
        )
        student_ids.extend(backend_students)

    return list(set(student_ids))  # deduplicate


def _get_enrolled_student_ids(bpr):
    """Get student IDs that are enrolled for this BPR."""
    return _get_students_for_bpr(bpr)


def _update_bpr_counts(bpr):
    """Recalculate archetype and arm counts on the BPR."""
    student_ids = _get_students_for_bpr(bpr)
    if not student_ids:
        return

    counts = frappe.db.sql(
        """
        SELECT
            archetype,
            experiment_arm,
            COUNT(*) as cnt
        FROM `tabStudent`
        WHERE name IN %s
        GROUP BY archetype, experiment_arm
        """,
        (student_ids,),
        as_dict=True,
    )

    dormant = fence = irregular = submitter = 0
    arm_a = arm_b = arm_default = 0

    for row in counts:
        arch = row.get("archetype", "")
        arm = row.get("experiment_arm", "")
        cnt = row.get("cnt", 0)

        if arch == "dormant":
            dormant += cnt
        elif arch == "fence_sitter":
            fence += cnt
        elif arch == "irregular_submitter":
            irregular += cnt
        elif arch == "submitter":
            submitter += cnt

        if arm == "arm_a":
            arm_a += cnt
        elif arm == "arm_b":
            arm_b += cnt
        else:
            arm_default += cnt

    bpr.dormant_count = dormant
    bpr.fence_sitter_count = fence
    bpr.irregular_submitter_count = irregular
    bpr.submitter_count = submitter
    bpr.arm_a_count = arm_a
    bpr.arm_b_count = arm_b
    bpr.arm_default_count = arm_default
