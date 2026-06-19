import frappe
from frappe import _
import json
import requests as _requests  # shared alias used by CR-004 retry helpers
from frappe.utils import nowdate, nowtime, now
from tap_lms.glific_integration import create_or_get_glific_group_for_batch, add_student_to_glific_for_onboarding, get_contact_by_phone
from tap_lms.api import get_course_level
import time
import random  # jitter for Phase-1 serialization-retry backoff
import psycopg2.errors as _pg_errors  # Postgres serialization / deadlock classification (L-071)
from rq.job import Job  # used by get_job_status (RQ status lookup)
from frappe.utils.background_jobs import get_redis_conn  # used by get_job_status


def normalize_phone_number(phone):
    """
    Normalize phone number to handle both 10-digit and 12-digit formats
    Returns both normalized 12-digit format and 10-digit format for comparison
    """
    if not phone:
        return None, None
    
    phone = phone.strip().replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    
    # Remove any non-digit characters
    phone = ''.join(filter(str.isdigit, phone))
    
    if len(phone) == 10:
        # 10-digit number, add country code
        phone_12 = f"91{phone}"
        phone_10 = phone
    elif len(phone) == 12 and phone.startswith('91'):
        # 12-digit number with country code
        phone_12 = phone
        phone_10 = phone[2:] # Remove 91 prefix
    elif len(phone) == 11 and phone.startswith('1'):
        # Sometimes numbers come as 1XXXXXXXXXX, treat as 91XXXXXXXXXX
        phone_12 = f"9{phone}"
        phone_10 = phone[1:]
    else:
        # Invalid format
        return None, None
    
    return phone_12, phone_10

def find_existing_student_by_phone_and_name(phone, name):
    """
    Find existing student by phone and name, handling both 10-digit and 12-digit phone formats
    """
    if not phone or not name:
        return None
    
    phone_12, phone_10 = normalize_phone_number(phone)
    
    if not phone_12 or not phone_10:
        return None
    
    # Search for existing students with either phone format
    existing_students = frappe.db.sql("""
        SELECT name, phone, name1
        FROM `tabStudent`
        WHERE name1 = %s 
        AND (phone = %s OR phone = %s)
        LIMIT 1
    """, (name, phone_10, phone_12), as_dict=True)
    
    if existing_students:
        return existing_students[0]
    
    return None

@frappe.whitelist()
def get_onboarding_batches():
    print("get_onboarding_batches called")
    # Return all draft backend onboarding batches
    return frappe.get_all("Backend Student Onboarding", 
                         filters={"status": ["in", ["Draft", "Processing", "Failed"]]},
                         fields=["name", "set_name", "upload_date", "uploaded_by", 
                                "student_count", "processed_student_count"])

@frappe.whitelist()
def get_batch_details(batch_id):
    # Get the details of a specific batch
    # batch = frappe.get_doc("Backend Student Onboarding", batch_id)
    # Only request fields that exist in the database
    students = frappe.get_all("Backend Students", 
                             filters={"parent": batch_id,"processing_status": ["in", ["Pending", "Failed"]]},
                             fields=["name", "student_name", "phone", "gender", 
                                    "batch", "course_vertical", "grade", "school",
                                    "language", "processing_status", "student_id"])
    

    already_on_bground = False
    # Add validation flags
    # for student in students:
    #     student["validation"] = validate_student(student)
    
    # # Get Glific group for this batch if exists
    # glific_group = frappe.get_all("GlificContactGroup", 
    #                              filters={"backend_onboarding_set": batch_id},
    #                              fields=["group_id", "label"])
    
    return {
        "batch": batch_id,
        "students": len(students),
        "on_queue": already_on_bground,
        #"glific_group": glific_group[0] if glific_group else None
    }
    #return {"students_"(students),bac}


@frappe.whitelist()
def is_job_name_exist(batch_id):
    job_name = f"student_onboarding_{batch_id}"
    job_data = None

    try:
        # check if RQ Job doctype exists (v14+)
        if frappe.db.table_exists("RQ Job"):
            job_data = frappe.db.get_value(
                "RQ Job",
                {"job_name": job_name},
                ["name", "status"],
                as_dict=True
            )
        else:
            # fallback to Redis RQ directly
            from frappe.utils.background_jobs import get_job
            job = get_job(job_name)
            if job:
                job_data = {
                    "name": job.id,
                    "status": job.get_status()
                }

    except Exception:
        pass
        #frappe.log_error(frappe.get_traceback(), "is_job_name_exist")

    return True if job_data else False


def validate_student(student):
    validation = {}
    
    # Check for empty required fields
    required_fields = ["student_name", "phone", "school", "grade", "language", "batch"]
    for field in required_fields:
        if not student.get(field):
            validation[field] = "missing"
    
    # Check for duplicate phone numbers with normalized phone comparison
    if student.get("phone"):
        existing = find_existing_student_by_phone_and_name(student.get("phone"), student.get("student_name"))
        if existing:
            validation["duplicate"] = {
                "student_id": existing.name,
                "student_name": existing.name1
            }
    
    return validation

@frappe.whitelist()
def get_onboarding_stages():
    try:
        # Check if the DocType exists
        if not frappe.db.table_exists("OnboardingStage"):
            return []
        
        # Get all onboarding stages ordered by the order field
        return frappe.get_all("OnboardingStage", 
                             fields=["name", "description", "order"],
                             order_by="`order`") # Using backticks to escape the reserved keyword
    except Exception as e:
        #frappe.log_error(f"Error fetching OnboardingStage: {str(e)}")
        
        return []

def get_initial_stage():
    """Get the initial onboarding stage (with order=0)"""
    try:
        stages = frappe.get_all("OnboardingStage", 
                               filters={"order": 0},
                               fields=["name"])
        if stages:
            return stages[0].name
        else:
            # If no stage with order 0, get the stage with minimum order
            stages = frappe.get_all("OnboardingStage", 
                                   fields=["name", "order"],
                                   order_by="order ASC",
                                   limit=1)
            if stages:
                return stages[0].name
    except Exception as e:
        #frappe.log_error(f"Error getting initial stage: {str(e)}")
        pass
    
    return None

def _onboarding_job_is_active(batch_id):
    """Return True if a student_onboarding job for this set is queued or running.

    M3 (CR-2026-06-19) concurrency guard.  Filters to ACTIVE RQ statuses only —
    a finished/failed RQ Job row persists (result_ttl / failure_ttl) and must
    NOT block a legitimate re-trigger.  Reads the RQ Job doctype (Frappe v15);
    on any error it fails OPEN (returns False) and logs the reason — the
    set-existence + status flow in process_batch is the second line of defense.

    Replaces the dead is_rq_job_running / is_batch_job_running pair (neither was
    wired to any caller; is_batch_job_running also crashed on get_jobs()'s
    list-of-strings).  See docs/code-reviews/CR-2026-06-19-backend-onboarding-deep-review.md §4.
    """
    job_name = f"student_onboarding_{batch_id}"
    active_statuses = {"queued", "started", "deferred", "scheduled"}
    try:
        if not frappe.db.table_exists("RQ Job"):
            return False
        rows = frappe.get_all("RQ Job", filters={"job_name": job_name}, fields=["status"])
        return any((r.status or "").lower() in active_statuses for r in rows)
    except Exception:
        frappe.log_error(
            title="Backend onboarding concurrency-guard check failed",
            message=frappe.get_traceback(),
        )
        return False


@frappe.whitelist()
def process_batch(batch_id, use_background_job=False):
    """
    Process the batch by creating students and Glific contacts

    Args:
        batch_id: ID of the Backend Student Onboarding document
        use_background_job: Whether to process in the background

    Returns:
        If background job is used, returns the job ID
        Otherwise, returns processing results
    """
    # M2 (CR-2026-06-19): restrict to TAP Admin — this enqueues a 2-hour
    # long-queue job that creates/updates Student + Enrollment rows for an
    # entire onboarding set.  Previously any authenticated user could trigger it.
    frappe.only_for("TAP Admin")

    use_background_job = json.loads(use_background_job) if isinstance(use_background_job, str) else use_background_job

    # M2: validate the set exists before flipping any status.
    if not frappe.db.exists("Backend Student Onboarding", batch_id):
        frappe.throw(_("Backend Student Onboarding {0} not found").format(batch_id))

    # M3: refuse to enqueue/run if a job for this set is already in flight.
    # Double-triggering the same set is the L-073 re-run path (duplicate
    # processing / silent re-enrollment); trigger each set once.
    if _onboarding_job_is_active(batch_id):
        frappe.throw(
            _("An onboarding job for set {0} is already queued or running. "
              "Wait for it to finish before re-triggering.").format(batch_id)
        )

    # Update batch status to Processing
    batch = frappe.get_doc("Backend Student Onboarding", batch_id)
    batch.status = "Processing"
    batch.save()

    if use_background_job:
        # Enqueue the processing job
        job = frappe.enqueue(
            process_batch_job,
            queue='long',
            timeout=7200,  # 2 hours
            job_name=f"student_onboarding_{batch_id}",
            set_id=batch_id
        )
        return {"job_id": job.id}
    else:
        # Process immediately
        return process_batch_job(batch_id)


# ── CR-2026-06-19 §10: Phase-1 serialization-retry tuning ───────────────────
# Backend onboarding runs at background_workers=4 in prod, so concurrent
# new-Student inserts contend on the shared tabSeries 'ST' counter
# (SELECT … FOR UPDATE), which can raise SerializationFailure (L-071/L-075).
# Enrollment's 'ER' counter was removed (→ hash, cr_2026_06_19); Student keeps
# its ST counter (L-031), so we retry the per-student work on serialization
# conflict instead of dead-lettering it to a re-run.
_PHASE1_MAX_SER_RETRIES = 3
_PHASE1_SER_BACKOFFS = (0.05, 0.10, 0.20)  # seconds; per-attempt jitter added


def _record_phase1_failure(student_entry, error, actual_index, set_id, results):
    """Mark a Phase-1 student Failed durably + visibly (M1 / CR-2026-06-19).

    The caller has already rolled this student's work back to its per-student
    savepoint, so the txn is healthy.  The Failed-status write is isolated under
    its OWN savepoint (bsf_<idx>) so a double-fault (the status write itself
    failing) can't poison the txn or wipe prior slice successes — the row simply
    stays Pending and is picked up on a re-run.  A trailing commit persists the
    log + status alongside the prior slice successes (L-080).

    The CALLER owns `failure_count += 1`; this helper only records the failed
    row + logs (do not add the increment here, or it will double-count).
    """
    # M1: durable structured failure log, under its OWN savepoint so a
    # log_error INSERT failure can't poison the txn and break the (un-guarded)
    # bsf_ savepoint below — a scoped rollback keeps prior slice successes
    # intact (L-030/L-080; code-review CR-2026-06-19 §A-HIGH).
    sp_log = f"bsl_{actual_index}"
    frappe.db.savepoint(sp_log)
    try:
        frappe.log_error(
            title="Backend onboarding Phase-1 student failure",
            message=json.dumps({
                "backend_student": student_entry.name,
                "set": set_id,
                "error": str(error),
            }),
        )
        frappe.db.release_savepoint(sp_log)
    except Exception:
        try:
            frappe.db.rollback(save_point=sp_log)
        except Exception:
            pass

    sp_fail = f"bsf_{actual_index}"
    frappe.db.savepoint(sp_fail)
    try:
        student = frappe.get_doc("Backend Students", student_entry.name)
        update_backend_student_status(student, "Failed", error=str(error))
        frappe.db.release_savepoint(sp_fail)
        results["failed"].append({
            "backend_id": student.name,
            "student_name": student.student_name,
            "error": str(error),
        })
    except Exception as inner_e:
        # Double-fault: the Failed-status write itself failed.  Roll back ONLY
        # that write (keep the txn + prior successes alive), then log loudly.
        frappe.db.rollback(save_point=sp_fail)
        try:
            frappe.log_error(
                title="Backend onboarding Phase-1 double-fault",
                message=json.dumps({
                    "backend_student": student_entry.name,
                    "set": set_id,
                    "original_error": str(error),
                    "status_write_error": str(inner_e),
                }),
            )
        except Exception:
            pass
        results["failed"].append({
            "backend_id": student_entry.name,
            "student_name": "Unknown",
            "error": f"Original error: {str(error)}. Status update error: {str(inner_e)}",
        })
    frappe.db.commit()


def process_batch_job(set_id):
    """Background job function to process the batch.

    CR-004 T-04-04: two-phase split.

    Phase 1 — DB only, zero Glific HTTP calls.
      Resolves course level and creates/updates Student + Enrollment +
      LearningState/EngagementState/StudentStageProgress.  Sets
      glific_sync_status='pending' on success.  Success/Fail accounting
      is based purely on DB outcome.

    Phase 2 — Glific sync, enqueued, retryable.
      After the Phase-1 loop commits, enqueues sync_student_to_glific
      for every Backend Students row with glific_sync_status IN
      ('pending','failed').  Retries and DLQ are handled inside
      sync_student_to_glific (Slice 0 / T-04-03c).
    """
    try:
        frappe.db.commit() # Commit any pending changes before starting job

        batch = frappe.get_doc("Backend Student Onboarding", set_id)

        # Get students to process (only pending or failed)
        students = frappe.get_all("Backend Students",
                                 filters={"parent": set_id, "processing_status": ["in", ["Pending", "Failed"]]},
                                 fields=["name","batch_skeyword"])

        success_count = 0
        failure_count = 0
        results = {
            "success": [],
            "failed": []
        }

        # Phase 1: NO Glific group creation here — Glific work is Phase 2.
        # get_initial_stage is still needed for StudentStageProgress.
        initial_stage = get_initial_stage()

        # Process students in batches for better performance
        total_students = len(students)
        batch_size = 50  # Process 50 students at a time
        commit_interval = 200  # Commit every 200 students

        for batch_start in range(0, total_students, batch_size):
            batch_end = min(batch_start + batch_size, total_students)
            batch_students = students[batch_start:batch_end]

            # Pre-fetch batch onboarding data for this batch
            batch_keywords = list(set([
                s.get('batch_skeyword') for s in batch_students
                if hasattr(s, 'batch_skeyword') and s.batch_skeyword
            ]))

            batch_onboarding_cache = {}
            if batch_keywords:
                batch_onboardings = frappe.get_all(
                    "Batch onboarding",
                    filters={"batch_skeyword": ["in", batch_keywords]},
                    fields=["batch_skeyword", "name", "kit_less"]
                )
                batch_onboarding_cache = {b.batch_skeyword: b for b in batch_onboardings}

            for index, student_entry in enumerate(batch_students):
                actual_index = batch_start + index
                update_job_progress(actual_index, total_students)

                # H1 fix (CR-2026-06-19): wrap each student in a Postgres
                # SAVEPOINT so a single failure rolls back ONLY that student —
                # never the up-to-49 already-processed successes in this slice.
                # The pre-fix blanket frappe.db.rollback() reverted every
                # uncommitted success since the last slice-end commit, silently
                # deferring real students to a re-run (the L-073/L-065 incident
                # trigger). Mirrors the CR-2026-06-15 B-2 pattern in
                # event_log.log_event.  A commit releases ALL savepoints, so the
                # name is per-iteration (bs_<actual_index>); nothing references a
                # savepoint across the 200-boundary or slice-end commit.
                # CR-2026-06-19 §10: create the per-student savepoint ONCE, then
                # RETRY the body on PG serialization/deadlock (e.g. the tabSeries
                # 'ST' counter under background_workers=4, L-071/L-075) by rolling
                # back to THIS savepoint and re-running.  We deliberately do NOT
                # use _insert_with_serialization_retry — its blanket
                # frappe.db.rollback() would wipe the whole slice (re-introducing
                # H1).  ROLLBACK TO SAVEPOINT keeps sp valid for the next attempt.
                sp = f"bs_{actual_index}"
                frappe.db.savepoint(sp)
                ser_attempt = 0
                while True:
                    try:
                        student = frappe.get_doc("Backend Students", student_entry.name)

                        # ── Phase 1: resolve course level (DB only) ──────────
                        # AC-1: NO Glific calls here.  process_glific_contact is
                        # intentionally absent from the Phase-1 path.
                        course_level_for_glific = None
                        if (hasattr(student, 'batch_skeyword') and student.batch_skeyword
                                and student.course_vertical and student.grade):
                            batch_onboarding = batch_onboarding_cache.get(student.batch_skeyword)
                            if batch_onboarding:
                                kitless = batch_onboarding.kit_less
                                course_level_for_glific = get_course_level_with_validation_backend(
                                    student.course_vertical,
                                    student.grade,
                                    student.phone,
                                    student.student_name,
                                    kitless,
                                )

                        # ── Phase 1: create/update Student + Enrollment + states
                        # glific_contact=None — process_student_record already
                        # guards `if glific_contact and 'id' in glific_contact`.
                        student_doc = process_student_record(
                            student, None, set_id, initial_stage, course_level_for_glific
                        )

                        # ── Phase 1: mark DB success; Glific is pending ──────
                        # glific_sync_status set 'pending' so Phase 2 picks it up.
                        student.glific_sync_status = "pending"
                        update_backend_student_status(student, "Success", student_doc)

                        # Student fully processed — release its savepoint so the
                        # row joins the slice transaction (committed at slice end).
                        frappe.db.release_savepoint(sp)

                        success_count += 1
                        results["success"].append({
                            "backend_id": student.name,
                            "student_id": student_doc.name,
                            "student_name": student_doc.name1,
                            "phone": student.phone,
                        })

                        # Commit every commit_interval students
                        if (actual_index + 1) % commit_interval == 0:
                            frappe.db.commit()
                            time.sleep(0.1)
                        break

                    except (_pg_errors.SerializationFailure,
                            _pg_errors.DeadlockDetected) as ser_e:
                        # Transient PG write contention (tabSeries 'ST' counter
                        # under parallel workers, L-071/L-075).  Roll back ONLY
                        # this attempt and retry with bounded backoff + jitter.
                        frappe.db.rollback(save_point=sp)
                        ser_attempt += 1
                        if ser_attempt <= _PHASE1_MAX_SER_RETRIES:
                            backoff = _PHASE1_SER_BACKOFFS[
                                min(ser_attempt - 1, len(_PHASE1_SER_BACKOFFS) - 1)
                            ]
                            time.sleep(backoff + random.uniform(0, 0.02))
                            continue
                        # Retries exhausted — record as a Phase-1 failure (M1).
                        failure_count += 1
                        _record_phase1_failure(student_entry, ser_e, actual_index, set_id, results)
                        break

                    except Exception as e:
                        # H1: roll back ONLY this student's partial work.  Prior
                        # successes in the slice stay intact and are persisted by
                        # the commit inside _record_phase1_failure (and slice end).
                        frappe.db.rollback(save_point=sp)
                        failure_count += 1
                        _record_phase1_failure(student_entry, e, actual_index, set_id, results)
                        break

            # Commit at end of each slice
            frappe.db.commit()

        # ── Phase 1 complete: update set status based on DB outcome ──────────
        try:
            batch = frappe.get_doc("Backend Student Onboarding", set_id)
            if failure_count == 0:
                batch.status = "Processed"
            elif success_count == 0:
                batch.status = "Failed"
            else:
                batch.status = "Processing"  # Partially processed

            processed_count = frappe.db.count("Backend Students",
                                              filters={"parent": set_id, "processing_status": "Success"})
            if hasattr(batch, 'processed_student_count'):
                batch.processed_student_count = processed_count

            batch.save()
            frappe.db.commit()
        except Exception as e:
            frappe.log_error(title="process_batch_job: batch status update failed", message=str(e))

        # ── Phase 2: enqueue Glific sync for pending/failed rows ─────────────
        # AC-1 enforcement: all Glific HTTP calls happen inside
        # sync_student_to_glific (a separate RQ job), never here.
        # enqueue_after_commit=True ensures Phase-1 commits are visible to the
        # worker before it reads the Backend Students row.
        # No `retry=` kwarg — retries are self-managed via _attempt inside
        # sync_student_to_glific to avoid double-retry.
        pending_rows = frappe.get_all(
            "Backend Students",
            filters={
                "parent": set_id,
                "glific_sync_status": ["in", ["pending", "failed"]],
            },
            fields=["name"],
        )
        for row in pending_rows:
            frappe.enqueue(
                "tap_lms.tap_lms.page.backend_onboarding_process"
                ".backend_onboarding_process.sync_student_to_glific",
                backend_student_name=row.name,
                queue="long",
                enqueue_after_commit=True,
            )

        frappe.logger().info(
            f"process_batch_job [{set_id}]: Phase 1 done — "
            f"{success_count} success, {failure_count} failed; "
            f"Phase 2: {len(pending_rows)} Glific-sync jobs enqueued."
        )

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "results": results,
            "glific_sync_enqueued": len(pending_rows),
        }
    except Exception as e:
        frappe.db.rollback()
        try:
            # Update batch status to Failed
            batch = frappe.get_doc("Backend Student Onboarding", set_id)
            batch.status = "Failed"
            # Add processing_notes if the field exists
            if hasattr(batch, 'processing_notes'):
                # Get the field's max length
                meta = frappe.get_meta("Backend Student Onboarding")
                field = meta.get_field("processing_notes")
                max_length = field.length if field and hasattr(field, 'length') else 140

                batch.processing_notes = str(e)[:max_length]
            batch.save()
            frappe.db.commit()
        except:
            pass # If this fails too, just continue

        # M1 / L-035 / L-056: the outer job-level failure is the path with the
        # MOST context (it may fail before any per-student loop runs), so it must
        # be durably visible.  rollback first to clear any poison from the
        # status-write attempt above (L-030/L-077), log, then commit so the
        # record survives (L-080); fall back to the DB-independent file logger.
        # Finally re-raise so RQ marks the job FAILED, not finished.
        try:
            frappe.db.rollback()
            frappe.log_error(
                title="Backend onboarding job failure",
                message=json.dumps({
                    "set": set_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }),
            )
            frappe.db.commit()
        except Exception:
            frappe.logger().error(f"process_batch_job [{set_id}] failed: {e}")
        raise

def update_job_progress(current, total):
    """Update the background job progress"""
    if total > 0:
        try:
            # Try without user parameter first (for older Frappe versions)
            frappe.publish_progress(
                percent=(current+1) * 100 / total,
                title=_("Processing Students"),
                description=_("Processing student {0} of {1}").format(current + 1, total)
            )
        except Exception:
            # Fall back to basic approach if publish_progress fails
            if (current+1) % 10 == 0 or (current+1) == total: # Update every 10 items
              #  frappe.db.commit()
                print(f"Processed {current+1} of {total} students")


def _ref(cache, doctype, name, fieldname):
    """Per-job reference cache helper (T-04-05 / AC-7).

    Lazily populates a per-job dict so repeated lookups for the same
    (doctype, name, fieldname) within one job hit the DB only once.

    Args:
        cache:     dict passed through from the job (created once per
                   process_batch_job run; never a module-level global).
        doctype:   Frappe DocType name, e.g. "School".
        name:      Document name (the 'name' field / primary key).
        fieldname: Single field to fetch, e.g. "name1".

    Returns:
        The cached (or freshly fetched) field value, which may be None.
    """
    # Key: (doctype, fieldname) → inner dict {name: value}
    # This lets us cache multiple fields for the same doctype without
    # collisions across different fieldname requests.
    inner = cache.setdefault((doctype, fieldname), {})
    if name not in inner:
        inner[name] = frappe.get_value(doctype, name, fieldname)
    return inner[name]


def process_glific_contact(student, glific_group, course_level=None, ref_cache=None):
    """
    Process Glific contact creation or retrieval
    FIXED: Shorter log messages to avoid 140-char limit

    Args:
        student: Backend Students document
        glific_group: Glific group information
        course_level: Optional course level name for Glific
        ref_cache: Optional per-job dict for caching reference-doctype
                   lookups (School, TAP Language, Course Verticals,
                   Course Level).  If None a local dict is created so
                   existing direct callers still work.  Do NOT pass a
                   process-global — cache must be scoped to one job.

    Returns:
        Glific contact information if successful, None otherwise
    """
    if ref_cache is None:
        ref_cache = {}

    # Format phone number
    phone = format_phone_number(student.phone)
    if not phone:
        raise ValueError(f"Invalid phone number format: {student.phone}")

    # Get school name for Glific — cached per-job (T-04-05)
    school_name = ""
    if student.school:
        school_name = _ref(ref_cache, "School", student.school, "name1") or ""

    # Get batch id for Glific.  Initialised to "" so the new-contact path below
    # (which passes batch_id to add_student_to_glific_for_onboarding) can't raise
    # a NameError when the row has no batch.
    batch_id = ""
    if student.batch:
        batch_id = frappe.get_value("Batch", student.batch, "name") or ""

    # Get language ID for Glific from TAP Language — cached per-job (T-04-05)
    language_id = None
    if student.language:
        try:
            language_id = _ref(ref_cache, "TAP Language", student.language, "glific_language_id")
            if not language_id:
                value = []
                #frappe.logger().warning(f"No glific_language_id found for language {student.language}, will use default")
        except Exception as e:
            value = []
            #frappe.logger().warning(f"Error getting glific_language_id: {str(e)}")

    # Get course level name for Glific — cached per-job (T-04-05)
    course_level_name = ""
    if course_level:
        try:
            course_level_name = _ref(ref_cache, "Course Level", course_level, "name1") or ""
            # SHORTENED LOG
            print(f"Course level: {course_level} -> '{course_level_name}'")
        except Exception as e:
            print(f"Course level error: {str(e)}")
            course_level_name = ""
    else:
        print(f"No course level provided for {student.student_name}")

    # Get course vertical name for Glific — cached per-job (T-04-05)
    course_vertical_name = ""
    if student.course_vertical:
        course_vertical_name = _ref(ref_cache, "Course Verticals", student.course_vertical, "name2") or ""
    
    # Check if contact already exists in Glific
    existing_contact = get_contact_by_phone(phone)
    
    if existing_contact and 'id' in existing_contact:
        # Contact exists, add to group if needed
        if glific_group and glific_group.get("group_id"):
            from tap_lms.glific_integration import add_contact_to_group
            add_contact_to_group(existing_contact['id'], glific_group.get("group_id"))
        
        # Update fields to ensure they're current
        fields_to_update = {
            "buddy_name": student.student_name,
            "batch_id": student.batch
        }
        
        if school_name:
            fields_to_update["school"] = school_name
        if course_level_name:
            fields_to_update["course_level"] = course_level_name
            print(f"Adding course_level: '{course_level_name}'")
        if course_vertical_name:
            fields_to_update["course"] = course_vertical_name
        if student.grade:
            fields_to_update["grade"] = student.grade
        
        # Update the contact fields — and CORE language at the same time.
        # 2026-05-19 fix: previously the existing-contact path didn't update
        # Glific's CORE `language` field at all (it was only set at
        # create_contact time). Pass language_id to update_contact_fields so
        # it's pushed in the same updateContact mutation — no extra network
        # call. New-contact path below already handles language correctly via
        # create_contact's languageId arg.
        from tap_lms.glific_integration import update_contact_fields
        update_result = update_contact_fields(
            existing_contact['id'],
            fields_to_update,
            language_id=language_id,
        )

        # SHORTENED LOG - just print, don't use #frappe.log_error
        print(
            f"Updated {student.student_name}: {len(fields_to_update)} fields"
            f"{' + core language' if language_id else ''}"
        )
        
        return existing_contact
    else:
        # Create new contact and add to group
        contact = add_student_to_glific_for_onboarding(
            student.student_name,
            phone,
            school_name,
            batch_id,
            glific_group.get("group_id") if glific_group else None,
            language_id,
            course_level_name,
            course_vertical_name,
            student.grade
        )
        
        if not contact or 'id' not in contact:
            #frappe.log_error(
            #     f"Failed to create Glific contact for {student.student_name}",
            #     "Glific Contact Error"
            # )
            value = []
        else:
            print(f"Created contact: {student.student_name}")
        
        return contact


def determine_student_type_backend(phone_number, student_name, course_vertical):
    """
    Determine if student is New or Old based on comprehensive enrollment analysis
    
    Logic:
    - IF student has enrollments in SAME vertical (valid links) → OLD
    - ELSE IF student has enrollments with BROKEN course links → OLD  
    - ELSE IF student has enrollments in DIFFERENT verticals → NEW
    - ELSE IF student has enrollments with NULL course → OLD
    - ELSE IF student has ANY enrollments but can't determine vertical → OLD
    - ELSE → NEW
    
    Args:
        phone_number: Student's phone number (can be 10 or 12 digits)
        student_name: Student's name (name1 field)
        course_vertical: Course vertical name/ID for comparison
    
    Returns:
        "Old" or "New" based on enrollment analysis
    """
    try:
        phone_12, phone_10 = normalize_phone_number(phone_number)
        
        if not phone_12 or not phone_10:
            #frappe.log_error(f"Invalid phone format for student type check: {phone_number}", "Backend Student Type Error")
            return "New"
        
        # Find existing student
        existing_students = frappe.db.sql("""
            SELECT name, phone, name1
            FROM `tabStudent`
            WHERE name1 = %s 
            AND (phone = %s OR phone = %s)
            LIMIT 1
        """, (student_name, phone_10, phone_12), as_dict=True)
        
        if not existing_students:
            #frappe.log_error(
            #     f"Backend: No existing student found: phone={phone_number}, name={student_name} → NEW",
            #     "Backend Student Type Classification"
            # )
            return "New"
        
        student_id = existing_students[0].name
        
        # Get all enrollments for this student
        enrollments = frappe.db.sql("""
            SELECT name, course, batch, grade, school
            FROM `tabEnrollment` 
            WHERE parent = %s
        """, (student_id,), as_dict=True)
        
        if not enrollments:
            #frappe.log_error(
            #     f"Backend: Student exists but no enrollments: {student_name} → NEW",
            #     "Backend Student Type Classification"
            # )
            return "New"
        
        # Analyze each enrollment
        same_vertical_count = 0
        different_vertical_count = 0
        broken_course_count = 0
        null_course_count = 0
        undetermined_count = 0
        
        enrollment_details = []
        
        for enrollment in enrollments:
            detail = {
                "enrollment": enrollment.name,
                "course": enrollment.course,
                "status": "",
                "vertical": ""
            }
            
            if not enrollment.course:
                # NULL course
                null_course_count += 1
                detail["status"] = "NULL_COURSE"
                detail["vertical"] = "N/A"
            else:
                # Check if course exists
                course_exists = frappe.db.exists("Course Level", enrollment.course)
                if not course_exists:
                    # BROKEN course link
                    broken_course_count += 1
                    detail["status"] = "BROKEN_COURSE"
                    detail["vertical"] = "BROKEN"
                else:
                    # Valid course - check vertical
                    course_vertical_data = frappe.db.sql("""
                        SELECT cv.name as vertical_name
                        FROM `tabCourse Level` cl
                        INNER JOIN `tabCourse Verticals` cv ON cv.name = cl.vertical
                        WHERE cl.name = %s
                    """, (enrollment.course,), as_dict=True)
                    
                    if course_vertical_data:
                        enrollment_vertical = course_vertical_data[0].vertical_name
                        detail["vertical"] = enrollment_vertical
                        
                        if enrollment_vertical == course_vertical:
                            same_vertical_count += 1
                            detail["status"] = "SAME_VERTICAL"
                        else:
                            different_vertical_count += 1
                            detail["status"] = "DIFFERENT_VERTICAL"
                    else:
                        # Course exists but can't determine vertical
                        undetermined_count += 1
                        detail["status"] = "UNDETERMINED_VERTICAL"
                        detail["vertical"] = "UNKNOWN"
            
            enrollment_details.append(detail)
        
        # Apply decision logic in priority order
        student_type = "New"  # Default
        reason = ""
        
        if same_vertical_count > 0:
            # Rule 1: Has enrollments in SAME vertical (valid links) → OLD
            student_type = "Old"
            reason = f"Has {same_vertical_count} enrollments in same vertical '{course_vertical}'"
        elif broken_course_count > 0:
            # Rule 2: Has enrollments with BROKEN course links → OLD
            student_type = "Old"
            reason = f"Has {broken_course_count} enrollments with broken course links"
        elif different_vertical_count > 0 and null_course_count == 0 and undetermined_count == 0:
            # Rule 3: Has enrollments ONLY in DIFFERENT verticals → NEW
            student_type = "New"
            reason = f"Has {different_vertical_count} enrollments only in different verticals"
        elif null_course_count > 0:
            # Rule 4: Has enrollments with NULL course → OLD
            student_type = "Old"
            reason = f"Has {null_course_count} enrollments with NULL course"
        elif undetermined_count > 0:
            # Rule 5: Has enrollments but can't determine vertical → OLD
            student_type = "Old"
            reason = f"Has {undetermined_count} enrollments with undetermined vertical"
        else:
            # Rule 6: Fallback → NEW (shouldn't reach here if logic is correct)
            student_type = "New"
            reason = "Fallback case - no clear enrollment pattern"
        
        # Detailed logging for debugging
        #frappe.log_error(
        #     f"Backend: Student type analysis for {student_name} (phone={phone_10}/{phone_12}):\n"
        #     f"Target vertical: {course_vertical}\n"
        #     f"Total enrollments: {len(enrollments)}\n"
        #     f"Same vertical: {same_vertical_count}\n"
        #     f"Different vertical: {different_vertical_count}\n"
        #     f"Broken courses: {broken_course_count}\n"
        #     f"NULL courses: {null_course_count}\n"
        #     f"Undetermined: {undetermined_count}\n"
        #     f"Decision: {student_type} - {reason}\n"
        #     f"Enrollment details: {enrollment_details}",
        #     "Backend Student Type Classification"
        # )
        
        return student_type
        
    except Exception as e:
        #frappe.log_error(f"Backend: Error determining student type: {str(e)}", "Backend Student Type Error")
        return "New"  # Default to New on error


def get_current_academic_year_backend():
    """
    Get current academic year based on current date
    Academic year runs from April to March
    (Same logic as API version)
    
    Returns:
        Academic year string in format "YYYY-YY" (e.g., "2025-26")
    """
    try:
        current_date = frappe.utils.getdate()
        
        if current_date.month >= 4: # April onwards = new academic year
            academic_year = f"{current_date.year}-{str(current_date.year + 1)[-2:]}"
        else:
            academic_year = f"{current_date.year - 1}-{str(current_date.year)[-2:]}"
        
        #frappe.log_error(f"Backend: Current academic year determined: {academic_year}", "Backend Academic Year Calculation")
        
        return academic_year
        
    except Exception as e:
        #frappe.log_error(f"Backend: Error calculating academic year: {str(e)}", "Backend Academic Year Error")
        return None

def validate_enrollment_data(student_name, phone_number):
    """
    Validate enrollment data WITHOUT making any repairs - only detection
    
    Args:
        student_name: Student's name
        phone_number: Student's phone number
        
    Returns:
        dict: Summary of validation results
    """
    try:
        phone_12, phone_10 = normalize_phone_number(phone_number)
        
        if not phone_12 or not phone_10:
            return {"error": "Invalid phone number format"}
        
        # Find all enrollments for this student
        enrollments = frappe.db.sql("""
            SELECT s.name as student_id, e.name as enrollment_id, e.course, e.batch, e.grade
            FROM `tabStudent` s
            INNER JOIN `tabEnrollment` e ON e.parent = s.name 
            WHERE (s.phone = %s OR s.phone = %s) AND s.name1 = %s
        """, (phone_10, phone_12, student_name), as_dict=True)
        
        validation_results = {
            "total_enrollments": len(enrollments),
            "valid_enrollments": 0,
            "broken_enrollments": 0,
            "broken_details": []
        }
        
        for enrollment in enrollments:
            if enrollment.course:
                # Check if course_level exists
                course_level_exists = frappe.db.exists("Course Level", enrollment.course)
                
                if course_level_exists:
                    validation_results["valid_enrollments"] += 1
                else:
                    validation_results["broken_enrollments"] += 1
                    validation_results["broken_details"].append({
                        "enrollment_id": enrollment.enrollment_id,
                        "invalid_course": enrollment.course,
                        "batch": enrollment.batch,
                        "grade": enrollment.grade
                    })
                    
                    # Only log the broken data, don't fix it
                    #frappe.log_error(
                    #     f"Detected broken course_level link: enrollment={enrollment.enrollment_id}, invalid_course={enrollment.course}, student={student_name}",
                    #     "Backend Broken Enrollment Data"
                    # )
        
        return validation_results
        
    except Exception as e:
        #frappe.log_error(f"Error validating enrollment data: {str(e)}", "Backend Enrollment Validation Error")
        return {"error": str(e)}

def get_course_level_with_mapping_backend(course_vertical, grade, phone_number, student_name, kitless):
    """
    Get course level using Grade Course Level Mapping with fallback to Stage Grades logic
    UPDATED: Now handles both 10-digit and 12-digit phone number formats
    
    Args:
        course_vertical: Course vertical name/ID
        grade: Student grade
        phone_number: Student phone number (can be 10 or 12 digits)
        student_name: Student name (for unique identification with phone)
        kitless: School's kit capability (for fallback logic)
    
    Returns:
        Course level name or raises exception
    """
    try:
        # Step 1: Normalize phone number and determine student type
        student_type = determine_student_type_backend(phone_number, student_name, course_vertical)
        
        # Step 2: Get current academic year
        academic_year = get_current_academic_year_backend()
        
        phone_12, phone_10 = normalize_phone_number(phone_number)
        
        #frappe.log_error(
        #     f"Backend: Course level mapping lookup: vertical={course_vertical}, grade={grade}, type={student_type}, year={academic_year}, phone={phone_10}/{phone_12}",
        #     "Backend Course Level Mapping Lookup"
        # )
        
        # Step 3: Try manual mapping with current academic year
        if academic_year:
            mapping = frappe.get_all(
                "Grade Course Level Mapping",
                filters={
                    "academic_year": academic_year,
                    "course_vertical": course_vertical,
                    "grade": grade,
                    "student_type": student_type,
                    "is_active": 1
                },
                fields=["assigned_course_level", "mapping_name"],
                order_by="modified desc", # Last modified takes priority
                limit=1
            )
            
            if mapping:
                #frappe.log_error(
                #     f"Backend: Found mapping: {mapping[0].mapping_name} -> {mapping[0].assigned_course_level}",
                #     "Backend Course Level Mapping Found"
                # )
                return mapping[0].assigned_course_level
        
        # Step 4: Try mapping with academic_year = null (flexible mappings)
        mapping_null = frappe.get_all(
            "Grade Course Level Mapping",
            filters={
                "academic_year": ["is", "not set"], # Null academic year
                "course_vertical": course_vertical,
                "grade": grade,
                "student_type": student_type,
                "is_active": 1
            },
            fields=["assigned_course_level", "mapping_name"],
            order_by="modified desc",
            limit=1
        )
        
        if mapping_null:
            #frappe.log_error(
            #     f"Backend: Found flexible mapping: {mapping_null[0].mapping_name} -> {mapping_null[0].assigned_course_level}",
            #     "Backend Course Level Flexible Mapping Found"
            # )
            return mapping_null[0].assigned_course_level
        
        # Step 5: Log that no mapping was found, falling back
        #frappe.log_error(
        #     f"Backend: No mapping found for vertical={course_vertical}, grade={grade}, type={student_type}, year={academic_year}. Using Stage Grades fallback.",
        #     "Backend Course Level Mapping Fallback"
        # )
        
        # Step 6: Fallback to current Stage Grades logic
        return get_course_level(course_vertical, grade, kitless)
        
    except Exception as e:
        #frappe.log_error(f"Backend: Error in course level mapping: {str(e)}", "Backend Course Level Mapping Error")
        # On any error, fallback to original logic
        return get_course_level(course_vertical, grade, kitless)

def get_course_level_with_validation_backend(course_vertical, grade, phone_number, student_name, kitless):
    """
    Wrapper that delegates to get_course_level_with_mapping_backend.

    CR-004 T-04-07 (AC-5): the dead validate_enrollment_data() call has been
    removed.  Its only consumer was a commented-out log; the return value was
    never used.  The course-level resolution chain is unchanged:
      get_course_level_with_mapping_backend → determine_student_type_backend
      → Grade Course Level Mapping → flexible mapping → Stage-Grades fallback.

    Args:
        course_vertical: Course vertical name/ID
        grade: Student grade
        phone_number: Student phone number
        student_name: Student name
        kitless: School's kit capability

    Returns:
        Course level name or None if not found
    """
    try:
        return get_course_level_with_mapping_backend(course_vertical, grade, phone_number, student_name, kitless)
    except Exception as e:
        #frappe.log_error(f"Backend: Error in course level selection: {str(e)}", "Backend Course Level Validation Error")
        # Fallback to basic course level selection
        try:
            return get_course_level(course_vertical, grade, kitless)
        except Exception as fallback_error:
            #frappe.log_error(f"Backend: Fallback course level selection also failed: {str(fallback_error)}", "Backend Course Level Fallback Error")
            return None


def process_student_record(student, glific_contact, batch_id, initial_stage, course_level=None):
    """
    Create or update student record based on duplicate handling logic
    UPDATED: Enhanced error handling for broken enrollment data
    
    Args:
        student: Backend Students document
        glific_contact: Glific contact information
        batch_id: Backend onboarding batch ID
        initial_stage: Initial onboarding stage
        course_level: Pre-determined course level (optional)
    """
    try:
        # Check for duplicate using normalized phone number comparison
        existing_student_data = find_existing_student_by_phone_and_name(student.phone, student.student_name)
        
        if existing_student_data:
            # Phone and name match - update existing student
            existing_student = frappe.get_doc("Student", existing_student_data.name)
            
            # Update phone number to normalized 12-digit format if needed
            phone_12, phone_10 = normalize_phone_number(student.phone)
            if phone_12 and existing_student.phone != phone_12:
                # Update to the 12-digit format for consistency
                existing_student.phone = phone_12
                #frappe.log_error(
                #     f"Updated phone format for existing student {existing_student.name}: {existing_student_data.phone} -> {phone_12}",
                #     "Backend Phone Format Update"
                # )
            
            # SHORTENED LOG MESSAGE
            #frappe.log_error(
            #     f"Existing: {student.student_name} | Grade: {existing_student.grade}→{student.grade}",
            #     "Backend Student Found"
            # )
            
            # Update student fields including grade
            updated_fields = []
            
            # Update grade (allow both upgrade and downgrade)
            if student.grade and str(student.grade) != str(existing_student.grade):
                #frappe.log_error(
                #     f"Grade update: {student.student_name} | {existing_student.grade}→{student.grade}",
                #     "Backend Grade Update"
                # )
                existing_student.grade = student.grade
                updated_fields.append(f"grade: {existing_student.grade}→{student.grade}")
            
            # Update school if changed
            if student.school and student.school != existing_student.school_id:
                #frappe.log_error(
                #     f"School update: {student.student_name} | {existing_student.school_id}→{student.school}",
                #     "Backend School Update"
                # )
                existing_student.school_id = student.school
                updated_fields.append(f"school: {existing_student.school_id}→{student.school}")
            
            # Update language if changed
            if student.language and student.language != existing_student.language:
                #frappe.log_error(
                #     f"Language update: {student.student_name} | {existing_student.language}→{student.language}",
                #     "Backend Language Update"
                # )
                existing_student.language = student.language
                updated_fields.append(f"language: {existing_student.language}→{student.language}")
            
            # Update gender ONLY when the existing student has none (fill-only).
            # Bug fix 2026-05-31: the prior logic OVERWROTE an existing gender
            # whenever the incoming import row differed. Import gender data is
            # unreliable (blank/other genders arrive as "Male"), so this flipped
            # real Female students to Male on re-import. We now only populate a
            # blank gender and never change an already-set one. Gender is treated
            # as immutable-once-set here, unlike grade/school/language above which
            # are intentionally mutable across terms.
            if student.gender and not existing_student.gender:
                existing_student.gender = student.gender
                updated_fields.append(f"gender: (blank)→{student.gender}")

            if student.archetype:
                existing_student.archetype = student.archetype

            if student.experiment_arm:
                existing_student.experiment_arm = student.experiment_arm
            
            # Log all updates with shortened message
            if updated_fields:
                update_msg = f"Updated fields: {student.student_name} | {', '.join(updated_fields)}"
                #frappe.log_error(
                #     update_msg[:140], # Truncate to 140 chars
                #     "Backend Fields Updated"
                # )
            
            # ALWAYS ADD NEW ENROLLMENT (regardless of existing enrollments)
            if student.batch:
                # Use pre-determined course level if available, otherwise determine it
                if course_level is None:
                    try:
                        if hasattr(student, 'batch_skeyword') and student.batch_skeyword and student.course_vertical and student.grade:
                            # Get batch onboarding details using batch_skeyword
                            batch_onboarding = frappe.get_all(
                                "Batch onboarding",
                                filters={"batch_skeyword": student.batch_skeyword},
                                fields=["name", "kit_less"]
                            )
                            
                            if batch_onboarding:
                                kitless = batch_onboarding[0].kit_less
                                
                                # Use enhanced course level selection that handles broken data
                                course_level = get_course_level_with_validation_backend(
                                    student.course_vertical,
                                    student.grade,
                                    phone_12 or student.phone, # Use normalized phone
                                    student.student_name, # Student name for unique identification
                                    kitless # For fallback logic
                                )
                                
                                # SHORTENED LOG MESSAGE
                                #frappe.log_error(
                                #     f"Course selected: {student.student_name} | {course_level or 'None'}",
                                #     "Backend Course Selection"
                                # )
                        
                        # If course_level is still None, try basic fallback
                        if not course_level and student.course_vertical and student.grade:
                            try:
                                # Direct fallback to get_course_level without mapping
                                course_level = get_course_level(student.course_vertical, student.grade, False)
                                #frappe.log_error(
                                #     f"Fallback course selected: {student.student_name} | {course_level or 'None'}",
                                #     "Backend Course Fallback"
                                # )
                            except Exception as fallback_error:
                                #frappe.log_error(f"Fallback course selection failed: {str(fallback_error)}", "Backend Course Fallback Error")
                                course_level = None
                                
                    except Exception as e:
                        #frappe.log_error(f"Course selection error: {str(e)}", "Backend Course Error")
                        course_level = None
                else:
                    value = []
                    # Use the pre-determined course level
                    #frappe.log_error(
                    #     f"Using pre-determined course level: {student.student_name} | {course_level}",
                    #     "Backend Course Reuse"
                    # )
                
                # Idempotency guard (L-073): skip if the student already has
                # an enrollment for this exact batch.  Different batches are
                # allowed — a student legitimately enrolled in a prior term
                # AND the current term must keep both rows.
                # Key is batch only; sibling-safety is naturally preserved
                # because siblings are different Student docs (different
                # student_id / doc name) each with their own child table.
                existing_batches = {
                    e.batch for e in (existing_student.enrollment or [])
                }
                if student.batch in existing_batches:
                    # duplicate on re-run — skip silently
                    frappe.logger().info(
                        f"process_student_record: skipping duplicate enrollment "
                        f"for {student.student_name} in batch {student.batch} "
                        f"(idempotency guard L-073)"
                    )
                else:
                    # Create new enrollment - with enhanced error handling
                    try:
                        enrollment = {
                            "batch": student.batch,
                            "grade": student.grade, # Use the updated grade
                            "date_joining": nowdate(),
                            "school": student.school
                        }

                        # Add course level if we found one (can be None)
                        if course_level:
                            enrollment["course"] = course_level

                        existing_student.append("enrollment", enrollment)

                        # SHORTENED LOG MESSAGE
                        enrollment_msg = f"Enrollment added: {student.student_name} | Batch: {student.batch} | Grade: {student.grade} | Course: {course_level or 'None'}"
                        #frappe.log_error(
                        #     enrollment_msg[:140], # Truncate to 140 chars
                        #     "Backend Enrollment Added"
                        # )

                    except Exception as enrollment_error:
                        value = []
                        #frappe.log_error(f"Error creating enrollment: {str(enrollment_error)}", "Backend Enrollment Error")
                        # Continue without enrollment if there's an error
            
            # Update Glific ID if we have it and student doesn't
            if glific_contact and 'id' in glific_contact and not existing_student.glific_id:
                existing_student.glific_id = glific_contact['id']
                #frappe.log_error(
                #     f"Glific ID added: {student.student_name} | ID: {glific_contact['id']}",
                #     "Backend Glific Added"
                # )
            
            # Update backend onboarding reference
            existing_student.backend_onboarding = batch_id
            
            # Save the existing student with all updates - with error handling
            try:
                existing_student.save()
                
                # SHORTENED LOG MESSAGE
                #frappe.log_error(
                #     f"Student updated: {student.student_name} (ID: {existing_student.name})",
                #     "Backend Update Complete"
                # )
                
            except Exception as save_error:
                #frappe.log_error(f"Error saving existing student: {str(save_error)}", "Backend Save Error")
                raise save_error
            
            student_doc = existing_student
            
        else:
            # Create new student with normalized phone number
            phone_12, phone_10 = normalize_phone_number(student.phone)
            
            #frappe.log_error(
            #     f"Creating new: {student.student_name} | Grade: {student.grade}",
            #     "Backend New Student"
            # )
            
            student_doc = frappe.new_doc("Student")
            student_doc.name1 = student.student_name
            student_doc.phone = phone_12 or student.phone # Use normalized 12-digit format
            student_doc.gender = student.gender
            student_doc.school_id = student.school
            student_doc.grade = student.grade
            student_doc.language = student.language
            student_doc.backend_onboarding = batch_id
            student_doc.joined_on = nowdate()
            student_doc.status = "active"
            student_doc.archetype = student.archetype
            student_doc.experiment_arm = student.experiment_arm
            
            # Add Glific ID if available
            if glific_contact and 'id' in glific_contact:
                student_doc.glific_id = glific_contact['id']
            
            # Add enrollment with course level for new student
            if student.batch:
                # Use pre-determined course level if available, otherwise determine it
                if course_level is None:
                    try:
                        if hasattr(student, 'batch_skeyword') and student.batch_skeyword and student.course_vertical and student.grade:
                            # Get batch onboarding details using batch_skeyword
                            batch_onboarding = frappe.get_all(
                                "Batch onboarding",
                                filters={"batch_skeyword": student.batch_skeyword},
                                fields=["name", "kit_less"]
                            )
                            
                            if batch_onboarding:
                                kitless = batch_onboarding[0].kit_less
                                
                                # Use enhanced course level selection
                                course_level = get_course_level_with_validation_backend(
                                    student.course_vertical,
                                    student.grade,
                                    phone_12 or student.phone, # Use normalized phone
                                    student.student_name, # Student name for unique identification
                                    kitless # For fallback logic
                                )
                                
                                # SHORTENED LOG MESSAGE
                                #frappe.log_error(
                                #     f"Course selected: {student.student_name} | {course_level or 'None'}",
                                #     "Backend Course Selection"
                                # )
                        
                        # If course_level is still None, try basic fallback
                        if not course_level and student.course_vertical and student.grade:
                            try:
                                course_level = get_course_level(student.course_vertical, student.grade, False)
                                #frappe.log_error(
                                #     f"Fallback course selected: {student.student_name} | {course_level or 'None'}",
                                #     "Backend Course Fallback"
                                # )
                            except Exception as fallback_error:
                                #frappe.log_error(f"Fallback course selection failed: {str(fallback_error)}", "Backend Course Fallback Error")
                                course_level = None
                                
                    except Exception as e:
                        #frappe.log_error(f"Course selection error: {str(e)}", "Backend Course Error")
                        course_level = None
                else:
                    # Use the pre-determined course level
                    #frappe.log_error(
                    #     f"Using pre-determined course level for new student: {student.student_name} | {course_level}",
                    #     "Backend Course Reuse"
                    # )
                    value = []
                
                # Create enrollment with enhanced error handling
                try:
                    enrollment = {
                        "batch": student.batch,
                        "grade": student.grade,
                        "date_joining": nowdate(),
                        "school": student.school
                    }
                    
                    # Add course level if we found one (can be None)
                    if course_level:
                        enrollment["course"] = course_level
                    
                    student_doc.append("enrollment", enrollment)
                    
                    # SHORTENED LOG MESSAGE
                    enrollment_msg = f"New enrollment: {student.student_name} | Batch: {student.batch} | Grade: {student.grade} | Course: {course_level or 'None'}"
                    #frappe.log_error(
                    #     enrollment_msg[:140], # Truncate to 140 chars
                    #     "Backend New Enrollment"
                    # )
                    
                except Exception as enrollment_error:
                    value = []
                    #frappe.log_error(f"Error creating new enrollment: {str(enrollment_error)}", "Backend New Enrollment Error")
                    # Continue without enrollment if there's an error
            
            # Insert new student with error handling
            try:
                student_doc.insert()
                
                # SHORTENED LOG MESSAGE
                #frappe.log_error(
                #     f"Created: {student.student_name} (ID: {student_doc.name})",
                #     "Backend Creation Complete"
                # )
                
            except Exception as insert_error:
                #frappe.log_error(f"Error inserting new student: {str(insert_error)}", "Backend Insert Error")
                raise insert_error
            
            # Initialize LearningState if it doesn't exist
            if not frappe.db.exists("LearningState", {"student": student_doc.name}):
                try:
                    learning_state = frappe.new_doc("LearningState")
                    learning_state.student = student_doc.name
                    learning_state.insert()
                except Exception as e:
                    value = []
                    #frappe.log_error(f"Error creating LearningState for student {student_doc.name}: {str(e)}", 
                                #    "Backend Student Onboarding")
                    # Continue without creating LearningState if there's an error
            
            # Initialize EngagementState if it doesn't exist
            if not frappe.db.exists("EngagementState", {"student": student_doc.name}):
                try:
                    engagement_state = frappe.new_doc("EngagementState")
                    engagement_state.student = student_doc.name
                    
                    # Set default values for required fields
                    engagement_state.average_response_time = "0" # Based on error, this is a required field
                    engagement_state.completion_rate = "0"
                    engagement_state.session_frequency = 0
                    engagement_state.current_streak = 0
                    engagement_state.last_activity_date = nowdate()
                    engagement_state.engagement_trend = "Stable"
                    engagement_state.re_engagement_attempts = "0"
                    engagement_state.sentiment_analysis = "Neutral"
                    
                    engagement_state.insert()
                except Exception as e:
                    value = []
                    #frappe.log_error(f"Error creating EngagementState for student {student_doc.name}: {str(e)}", 
                                #    "Backend Student Onboarding")
                    # Continue without creating EngagementState if there's an error
            
            # Create first StudentStageProgress for onboarding if it doesn't exist
            if initial_stage and not frappe.db.exists("StudentStageProgress", 
                                                     {"student": student_doc.name, "stage_type": "OnboardingStage"}):
                try:
                    stage_progress = frappe.new_doc("StudentStageProgress")
                    stage_progress.student = student_doc.name
                    stage_progress.stage_type = "OnboardingStage"
                    stage_progress.stage = initial_stage
                    stage_progress.status = "not_started"
                    stage_progress.start_timestamp = now()
                    stage_progress.insert()
                except Exception as e:
                    value = []
                    #frappe.log_error(f"Error creating StudentStageProgress for student {student_doc.name}: {str(e)}", 
                                #    "Backend Student Onboarding")
                    # Continue without creating StudentStageProgress if there's an error
        
        return student_doc
        
    except Exception as main_error:
        #frappe.log_error(f"Critical error in process_student_record for {student.student_name}: {str(main_error)}", "Backend Student Processing Critical Error")
        raise main_error

# ════════════════════════════════════════════════════════════════════════════
# CR-004 Slice 0 — T-04-03c: retryable Phase-2 Glific sync entrypoint
# ════════════════════════════════════════════════════════════════════════════
# This function is the unit of retry: on a transient Glific error it
# re-enqueues itself (up to 3 times) and on exhaustion or non-transient
# error it dead-letters to Error Log (DLQ) per L-015 / P-007.
#
# It is IDEMPOTENT: the `get_contact_by_phone` lookup inside
# `process_glific_contact` short-circuits to an update if the contact
# already exists — re-runs never double-create a Glific contact.
#
# Wiring into process_batch_job Phase 2 is T-04-04 (Slice 2).
# ════════════════════════════════════════════════════════════════════════════

def _dlq_glific(backend_student_name, error):
    """Dead-letter a failed Glific sync to Error Log (L-030 rollback-before-log).

    Sets glific_sync_status='failed' on the Backend Students row and writes a
    structured JSON payload so operators can replay the sync manually.
    Does NOT mark the student's processing_status as Failed — Phase-1 DB
    records (Student + Enrollment) are left intact.
    """
    try:
        frappe.db.rollback()  # L-030: clear any poisoned transaction first
        bs = frappe.get_doc("Backend Students", backend_student_name)
        bs.glific_sync_status = "failed"
        bs.save(ignore_permissions=True)
        payload = {
            "backend_student": backend_student_name,
            "student_id": bs.student_id if bs.student_id else None,
            "phone": bs.phone,
            "set": bs.parent,
            "error": str(error),
        }
        frappe.log_error(
            title=f"DLQ: Glific sync {backend_student_name}",
            message=json.dumps(payload),
        )
        frappe.db.commit()
    except Exception as inner_e:
        # Double-fault: DLQ write itself failed. Log and re-raise so the
        # worker surfaces the failure rather than swallowing it (L-056).
        frappe.log_error(
            title=f"DLQ double-fault: Glific sync {backend_student_name}",
            message=f"Original: {error}; DLQ error: {inner_e}",
        )
        raise


def _is_transient_glific_error(exc):
    """Return True if the exception is a transient error that should be retried.

    Transient classes:
    - requests network errors: Timeout, ConnectionError
    - HTTP rate-limit / server errors: 429 or 5xx (via raise_for_status())
    - Postgres serialization / deadlock errors (L-071): these arise under
      concurrent workers and succeed on retry without any application change.
      Matched both by psycopg2 exception class (when the raw PG exception
      bubbles up) AND by message substring (Frappe sometimes wraps the PG
      exception in its own exception, preserving the message but changing
      the type).
    """
    if isinstance(exc, (_requests.Timeout, _requests.ConnectionError)):
        return True
    # HTTP 429 / 5xx surfaces as requests.HTTPError after raise_for_status()
    if isinstance(exc, _requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None and (status == 429 or status >= 500):
            return True
    # Postgres serialization failure / deadlock — both are transient under
    # concurrent worker contention (L-071).  Match by class first (raw psycopg2
    # exception), then by message substring (Frappe-wrapped exception).
    if isinstance(exc, (_pg_errors.SerializationFailure, _pg_errors.DeadlockDetected)):
        return True
    exc_msg = str(exc).lower()
    if "could not serialize access" in exc_msg or "deadlock detected" in exc_msg:
        return True
    return False


def sync_student_to_glific(backend_student_name, _attempt=0):
    """Phase-2 Glific sync entrypoint — retryable, idempotent, dead-lettering.

    Fetches the Backend Students row, runs the existing process_glific_contact
    logic (contact lookup/create/optin/add-to-group OR update for existing
    student), writes glific_id back onto both Student and Backend Students,
    and sets glific_sync_status='synced'.

    On transient error (Timeout / ConnectionError / HTTP 429 or 5xx):
      - if _attempt < 3, re-enqueues with _attempt+1 and returns.
    On exhaustion or non-transient error: DLQ via _dlq_glific.

    Wired into process_batch_job Phase 2 by T-04-04 (Slice 2).
    """
    try:
        bs = frappe.get_doc("Backend Students", backend_student_name)

        # Idempotency guard: skip if already synced (e.g., duplicate enqueue).
        if bs.glific_sync_status == "synced":
            frappe.logger().info(
                f"sync_student_to_glific: {backend_student_name} already synced, skipping."
            )
            return

        # Resolve course level for Glific (same logic as process_batch_job Phase 1).
        course_level_for_glific = None
        if (hasattr(bs, "batch_skeyword") and bs.batch_skeyword
                and bs.course_vertical and bs.grade):
            batch_onboarding = frappe.get_all(
                "Batch onboarding",
                filters={"batch_skeyword": bs.batch_skeyword},
                fields=["kit_less"],
                limit=1,
            )
            if batch_onboarding:
                course_level_for_glific = get_course_level_with_validation_backend(
                    bs.course_vertical,
                    bs.grade,
                    bs.phone,
                    bs.student_name,
                    batch_onboarding[0].kit_less,
                )

        # Get the Glific group for this set (may be None — handled gracefully).
        try:
            glific_group = create_or_get_glific_group_for_batch(bs.parent)
        except Exception as _grp_exc:
            frappe.logger().warning(
                f"sync_student_to_glific: could not resolve Glific group for "
                f"set {bs.parent!r} (student {backend_student_name}): {_grp_exc}"
            )
            glific_group = None

        # Perform the Glific sync (lookup / create / optin / add-to-group).
        # Create a local ref_cache per-invocation — not shared across students
        # (each sync_student_to_glific is a separate job), not process-global.
        local_ref_cache = {}
        glific_contact = process_glific_contact(
            bs, glific_group, course_level_for_glific, local_ref_cache
        )

        # Write glific_id back to Student and Backend Students.
        glific_id = None
        if glific_contact and "id" in glific_contact:
            glific_id = glific_contact["id"]

        if glific_id and bs.student_id:
            frappe.db.set_value("Student", bs.student_id, "glific_id", glific_id,
                                update_modified=False)

        bs.reload()
        bs.glific_sync_status = "synced"
        if glific_id:
            bs.glific_id = glific_id
        bs.save(ignore_permissions=True)

        frappe.logger().info(
            f"sync_student_to_glific: {backend_student_name} synced "
            f"(glific_id={glific_id}, attempt={_attempt})"
        )
        frappe.db.commit()

    except Exception as exc:
        if _is_transient_glific_error(exc) and _attempt < 3:
            frappe.logger().warning(
                f"sync_student_to_glific: transient error for "
                f"{backend_student_name} (attempt {_attempt}): {exc}; re-enqueueing."
            )
            frappe.enqueue(
                sync_student_to_glific,
                backend_student_name=backend_student_name,
                _attempt=_attempt + 1,
                queue="long",
                enqueue_after_commit=True,
            )
            return
        # Non-transient error OR retry budget exhausted — DLQ.
        _dlq_glific(backend_student_name, exc)
        raise  # L-056: RQ job must surface as failed, not finished


# ════════════════════════════════════════════════════════════════════════════

def update_backend_student_status(student, status, student_doc=None, error=None):
    """
    Update the status of a Backend Students record
    
    Args:
        student: Backend Students document
        status: New status ("Success" or "Failed")
        student_doc: Optional Student document (for Success status)
        error: Optional error message (for Failed status)
    """
    student.processing_status = status
    
    if status == "Success" and student_doc:
        student.student_id = student_doc.name
        # If we have a glific_id field, update it
        if hasattr(student, 'glific_id') and student_doc.glific_id:
            student.glific_id = student_doc.glific_id
    
    # Handle processing_notes with proper truncation for not-null constraint
    if error and hasattr(student, 'processing_notes'):
        # Get the field's max length from metadata or default to 140
        try:
            meta = frappe.get_meta("Backend Students")
            field = meta.get_field("processing_notes")
            max_length = field.length if field and hasattr(field, 'length') else 140
        except:
            max_length = 140 # Fallback if metadata can't be accessed
        
        # Truncate error message to max length
        student.processing_notes = str(error)[:max_length]
    
    student.save()

def format_phone_number(phone):
    """Format phone number for Glific (must be 12 digits with 91 prefix for India)"""
    phone_12, phone_10 = normalize_phone_number(phone)
    return phone_12


@frappe.whitelist()
def get_job_status(job_id):
    """Check job status using Redis RQ (works even if no RQ Job table)"""
    try:
        conn = get_redis_conn()
        job = Job.fetch(job_id, connection=conn)
                # Map RQ's "finished" to "Completed"
        status = job.get_status()
        if status == "finished":
            status = "Completed"
        return {
            "status": status,
            "result": job.result,
            "progress": job.meta.get("progress") if hasattr(job, "meta") else None
        }
    except Exception as e:
        #frappe.logger().error(f"[get_job_status] {e}")
        return {"status": "Not Found"}

