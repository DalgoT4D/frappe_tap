"""
Save Submission API
tap_lms/summer_program/save_submission.py

API A3: save_submission — the core submission handler.

Atomic idempotent submission with:
  - Atomic UPDATE WHERE journey_label check (prevents race conditions)
  - is_primary logic (first submission = primary, rest = duplicates)
  - Submission record creation
  - ProgramEnrollment state transition
  - Points calculation from EscalationStep
  - EngagementState update

Called by:
  - SP_Content_Delivery wait node (Path A — inline submission)
  - SP_Escalation wait node (Path A — inline during escalation)
  - SP_Submission sub-flow (Path B — via SP_Incoming_Router)

CR-003 follow-up (2026-05-13): SP_Grace_Entry / SP_Grace_Reminder
references retired — both flows are deleted. Submissions during the grace
window arrive via SP_Submission (Path B) or via inline wait nodes on
SP_Content_Delivery / SP_Escalation if the student happens to be in those
states when grace expires. Either way the primary-submission transitions
(T7/T9/T17/T3) clear the grace clock.
"""
import frappe
import json
import os
import time
from frappe.utils import now_datetime, today, getdate, cint
from urllib.parse import urlparse

from tap_lms.summer_program.constants import (
    TERMINAL_STATES,
    FEEDBACK_PIPELINE_MAX_RETRIES,
    FEEDBACK_PIPELINE_RETRY_LOG_TITLE,
    FEEDBACK_PIPELINE_DLQ_LOG_TITLE,
)
from tap_lms.summer_program.state_machine import (
    get_active_pe,
    apply_submission_transition,
)
from tap_lms.summer_program.event_log import log_event
from tap_lms.summer_program.utils import (
    normalize_unicode_surrogates,
    safe_sp_api_error_response,
    check_glific_placeholders,
)
URL_SUBMISSION_TYPES = {"audio", "image", "video"}
SAVE_SUBMISSION_DB_RETRY_ATTEMPTS = 3
SAVE_SUBMISSION_DB_RETRY_DELAY_SECONDS = 0.15


def _is_serialization_failure(error):
    """Return True for Postgres retryable serialization/concurrent-update errors."""
    pgcode = getattr(error, "pgcode", None)
    if pgcode == "40001":
        return True

    cause = getattr(error, "__cause__", None)
    if cause is not None and _is_serialization_failure(cause):
        return True

    error_text = str(error).lower()
    return (
        "could not serialize access due to concurrent update" in error_text
        or "serialization failure" in error_text
    )


@frappe.whitelist(allow_guest=True)
def save_submission(student_id, assignment_id=None, submission=None,
                    week=None, content_id=None, **_glific_kwargs):
    """
    API A3: save_submission

    Atomic idempotent submission handler.

    Args:
        student_id: Student name, glific_id, or phone
        assignment_id: Assignment ID from get_content_details API
                       (e.g. "B2_FL_L1_RA12-Basic")
        submission: URL, text, or emoji submitted by the student
        week: Override week number (defaults to PE.current_week)
        content_id: DEPRECATED alias for assignment_id. Older Glific flows
                    use this name; pattern P-006 keeps it around for one
                    cycle with a deprecation log. New flows should use
                    assignment_id. Restored 2026-05-15 (task #78 / audit).
        **_glific_kwargs: absorbs any extra fields Glific injects into its
                    outbound webhook payload (e.g. `organization_id`, the
                    multi-tenant tag added 2026-05-25 — discord report:
                    Himani re Mayank ST00052222). Per task #89 / future
                    L-043, every Glific-consumed endpoint accepts these
                    silently so a Glific-side payload expansion can never
                    TypeError us into a raw HTML 500. Ignored at this layer.

    Returns:
        dict with: status (accepted|duplicate|rejected), is_primary,
                   points_awarded, submission_count, submission_id
    """
    # Task #93 (2026-05-25): pre-validate empty submission and short-circuit
    # with structured response. Prevents the downstream
    # `_normalize_submission_payload` from raising
    # `frappe.ValidationError("Submission is required")`, which would
    # escape the retry loop and surface as raw HTTP 500 HTML — violating
    # api-standard-glific.md Rule 7. Surfaces as a clean `status="submission_empty"`
    # branch for Glific flows. Discord report 2026-05-25: Mayank (ST00052222)
    # blocked here because his emoji-flow webhook sent submission="".
    if submission is None or not str(submission).strip():
        frappe.local.response.update({
            "success": False,
            "status": "submission_empty",
            "user_message": "Please submit your response.",
            "error_detail": "submission parameter is empty or whitespace-only",
        })
        return

    # CR-024 Layer 3: reject Glific placeholder strings on identifier params.
    # student_id and assignment_id (or legacy content_id alias) are identifier
    # params. submission is free text (student answer) — intentionally NOT
    # checked. Do NOT touch _try_claim_primary / P-001 / L-010 atomicity.
    _effective_assignment_id = assignment_id if assignment_id is not None else content_id
    placeholder_hit = check_glific_placeholders(
        [
            ("student_id", student_id),
            ("assignment_id", _effective_assignment_id),
        ],
        api_name="save_submission",
        student_id=student_id,
    )
    if placeholder_hit:
        frappe.local.response.update(placeholder_hit)
        return

    last_error = None
    for attempt in range(1, SAVE_SUBMISSION_DB_RETRY_ATTEMPTS + 1):
        try:
            return _save_submission_once(
                student_id=student_id,
                assignment_id=assignment_id,
                submission=submission,
                week=week,
                content_id=content_id,
            )
        except frappe.ValidationError as e:
            # Task #93: convert validation errors to structured response per
            # api-standard-glific.md Rule 7. Any `frappe.throw` call inside
            # `_save_submission_once` raises ValidationError; without this
            # catch, those escape to Frappe's HTTP layer and surface as raw
            # HTML 500. Glific flows expect the flat envelope so they can
            # branch on `status`.
            frappe.db.rollback()
            frappe.local.response.update({
                "success": False,
                "status": "validation_error",
                "user_message": str(e) or "Validation error.",
                "error_detail": str(e),
            })
            return
        except frappe.DoesNotExistError as e:
            # Same pattern for missing-record errors (student, assignment,
            # PE not found). Structured envelope, not HTML 500.
            frappe.db.rollback()
            frappe.local.response.update({
                "success": False,
                "status": "not_found",
                "user_message": "Required record not found.",
                "error_detail": str(e),
            })
            return
        except Exception as e:
            if not _is_serialization_failure(e):
                # Unknown exception — log + structured response. Per Rule 7
                # an unhandled error must NEVER surface as raw HTML 500.
                # Logged under "SP Save Submission" so ops can investigate
                # the root cause while Glific gets a parseable answer.
                frappe.db.rollback()
                frappe.log_error(
                    f"save_submission unhandled exception for "
                    f"student_id={student_id}, assignment_id={assignment_id}: "
                    f"{type(e).__name__}: {e}",
                    "SP Save Submission",
                )
                frappe.local.response.update({
                    "success": False,
                    "status": "internal_error",
                    "user_message": "Could not process your submission. "
                                    "Please try again later.",
                    "error_detail": f"{type(e).__name__}: {e}",
                })
                return

            last_error = e
            frappe.db.rollback()
            if attempt < SAVE_SUBMISSION_DB_RETRY_ATTEMPTS:
                time.sleep(SAVE_SUBMISSION_DB_RETRY_DELAY_SECONDS * attempt)
                continue

    frappe.log_error(
        f"save_submission exhausted serialization retries for "
        f"student_id={student_id}, assignment_id={assignment_id}: {last_error}",
        "SP Save Submission",
    )
    frappe.local.response.update({
        "success": False,
        "status": "retryable_conflict",
        "error_detail": "Submission is being updated concurrently. Please retry.",
    })
    return


def _save_submission_once(student_id, assignment_id=None, submission=None, week=None, content_id=None):
    """
    Single transactional attempt for save_submission.

    Postgres serialization failures must be handled by retrying the whole
    transaction, so the whitelisted wrapper owns the retry loop.
    """
    # P-006 deprecation alias (L-009): older Glific flows pass `content_id`.
    # Map it to `assignment_id` and log so we can track call-sites that still
    # use the legacy name before removing the alias.
    if assignment_id is None and content_id is not None:
        frappe.log_error(
            f"save_submission called with legacy 'content_id' param "
            f"(student_id={student_id}). Update Glific flow to use 'assignment_id'.",
            "SP API Deprecation",
        )
        assignment_id = content_id
    assignment_id = normalize_unicode_surrogates(assignment_id)

    if not assignment_id:
        frappe.local.response.update({
            "success": False,
            "status": "missing_param",
            "error_detail": "assignment_id (or legacy content_id) is required",
        })
        return

    student_id = _resolve_student(student_id)
    if not student_id:
        frappe.local.response.update({
            "success": False, "status": "not_found",
            "error_detail": "Student not found",
        })
        return

    pe = get_active_pe(student_id)
    if not pe:
        frappe.local.response.update({
            "success": False, "status": "no_active_enrollment",
            "error_detail": "No active ProgramEnrollment",
        })
        return

    current_week = cint(week) or pe.current_week or 1

    # Check if student is in a terminal or paused state
    if pe.resolved_flow_state in TERMINAL_STATES:
        frappe.local.response.update({
            "success": False,
            "status": "terminal_state",
            "error_detail": "Student in terminal state",
            "resolved_flow_state": pe.resolved_flow_state,
        })
        return

    # ── Normalize submission payload ────────────────────────
    # Store only the raw value for async classification. Submission type,
    # text/url placement, media probing, and GCS upload all happen after the
    # API response has been returned.
    payload = _normalize_submission_payload(
        submission,
        pe=pe,
    )

    # ── Create Submission record FIRST (task #81 / audit 2026-05-15) ─
    # Insert the Submission inside a savepoint BEFORE claiming primary.
    # Previous order (claim → insert) could leave the PE with
    # journey_label='submitted' + submission_count bumped but no Submission
    # row if the insert failed — retries would then see "duplicate" and
    # silently drop the real submission. With this order, an insert failure
    # rolls back the savepoint and leaves PE state untouched.
    #
    # is_primary=0 is a placeholder; we flip it after the atomic claim
    # succeeds via a targeted set_value (no full reload needed).
    sp_name = f"sub_create_{frappe.utils.random_string(8)}"
    submission_doc = None
    try:
        frappe.db.savepoint(sp_name)
        submission_doc = _create_submission(
            pe=pe,
            student_id=student_id,
            week=current_week,
            payload=payload,
            assignment_id=assignment_id,
            is_primary=False,  # provisional — flipped after claim succeeds
        )
        frappe.db.release_savepoint(sp_name)
    except Exception as e:
        frappe.db.rollback(save_point=sp_name)
        if _is_serialization_failure(e):
            raise
        frappe.log_error(
            f"Submission insert failed for student {student_id}, "
            f"week {current_week}: {e}",
            "SP Save Submission",
        )
        frappe.local.response.update({
            "success": False,
            "status": "insert_failed",
            "error_detail": "Could not record submission",
        })
        return

    # ── Atomic is_primary claim ─────────────────────────────
    # Atomic UPDATE on PE.journey_label decides who is primary. Race window
    # is shorter now that the Submission row is already on disk: even if a
    # parallel call wins the claim, the loser's Submission still exists as
    # a duplicate record.
    is_primary = _try_claim_primary(pe, current_week)

    # Flip the Submission.is_primary flag now that we know the truth.
    # status="Pending" for primary (feedback pipeline picks it up),
    # "Completed" for duplicates (no further processing).
    if is_primary:
        frappe.db.set_value(
            "Submission", submission_doc.name,
            {"is_primary": 1, "status": "Pending"},
            update_modified=False,
        )
        submission_doc.is_primary = 1
        submission_doc.status = "Pending"

    # ── Calculate points ────────────────────────────────────
    # CR-007 (2026-05-19): submission points are no longer awarded here.
    # AI validation runs asynchronously after save_submission; the actual
    # award (Assignment.points_per_item for on-time submissions, or
    # EscalationStep.points_awarded for late ones) is computed by
    # `feedback_consumer_hook.on_feedback_ready` once result_status is known.
    # The transition below still fires with points=0 so streak / gems /
    # weekly_submission_done bump on every submission (user spec:
    # "every submission regardless of validity"). See CR-007.
    points = 0

    # ── Apply state transition ──────────────────────────────
    if is_primary:
        transition_id, success = apply_submission_transition(
            pe, points=points, trigger_source="flow_callback"
        )
        _log_student_content_submission(
            pe=pe,
            student_id=student_id,
            week=current_week,
            payload=payload,
            assignment_id=assignment_id,
            points=points,
            submission_doc=submission_doc,
        )
    else:
        # Duplicate — no state change (T22)
        from tap_lms.summer_program.state_machine import t22_duplicate_submission
        t22_duplicate_submission(pe, "flow_callback")
        transition_id = "T22"
        if submission_doc:
            _apply_duplicate_submission_feedback(submission_doc, getattr(pe, "language", None))

    # ── Update EngagementState ──────────────────────────────
    _update_engagement(student_id)

    # ── Log the submission event ────────────────────────────
    log_event(pe, "submission_received", trigger_source="flow_callback",
              details={
                  "is_primary": is_primary,
                  "submission_type": payload["submission_type"],
                  "points_awarded": points,
                  "week": current_week,
                  "escalation_step_at_submit": pe.current_escalation_step or 0,
                  "transition": transition_id,
                  "submission_id": submission_doc.name if submission_doc else None,
              })

    # ── Upload to GCS + enqueue to RabbitMQ (background) ────
    if is_primary and submission_doc:
        _queue_submission_processing(
            submission_doc,
            pe_context=_build_pe_context(pe),
        )

    # Removed mid-handler commit per L-017 — Frappe commits at request-end.

    return _build_submission_response(
        pe=pe,
        student_id=student_id,
        submission_doc=submission_doc,
        is_primary=is_primary,
        points=points,
        week=current_week,
    )


@frappe.whitelist(allow_guest=True)
def get_submission_feedback(submission_id, **_glific_kwargs):
    """
    Get feedback for a summer-program submission.

    `**_glific_kwargs` absorbs Glific-injected fields (organization_id, etc.)
    per task #89. Ignored at this layer.

    Args:
        submission_id: Submission document ID

    Returns:
        dict with submission status. Completed submissions include feedback fields.
    """
    try:
        submission = frappe.get_doc("Submission", submission_id)

        if submission.status == "Completed":
            return {
                "status": submission.status,
                "overall_feedback": submission.overall_feedback,
                "overall_feedback_translated": submission.overall_feedback_translated,
                "audio_feedback_url": submission.audio_feedback_url,
            }

        return {"status": submission.status}

    except frappe.DoesNotExistError:
        return {"error": "Submission not found"}

    except Exception as e:
        # BR-003: rollback-first + flat error (L-030 cascade fix).
        return safe_sp_api_error_response(e, "get_submission_feedback",
                                          extras={"submission_id": submission_id})


@frappe.whitelist(allow_guest=True)
def ready_to_receive_feedback(submission_id, **_glific_kwargs):
    """
    Mark a submission as ready for student feedback delivery.

    If AI feedback has already landed, this triggers the feedback flow
    immediately. Otherwise the RabbitMQ feedback consumer will trigger it
    after processing finishes.
    """
    try:
        submission = frappe.get_doc("Submission", submission_id)
        requested_at = now_datetime()

        if not _mark_feedback_requested(submission_id, requested_at):
            return {
                "success": True,
                "status": "success",
                "message": "Feedback flow already triggered.",
            }

        submission.send_feedback = "yes"
        submission.feedback_requested_at = requested_at

        if submission.status not in ("Completed", "Failed"):
            return {
                "success": True,
                "status": "success",
                "message": "Feedback will be given once ready.",
            }

        from tap_lms.feedback_handler.feedback_consumer import FeedbackConsumer

        consumer = FeedbackConsumer.__new__(FeedbackConsumer)
        message_data = _build_feedback_flow_message(submission)

        consumer.process_feedback_ready(submission_id, message_data)

        if consumer._claim_feedback_flow(submission_id):
            frappe.db.commit()
            consumer.trigger_feedback_flow(submission_id, message_data)

        return {
            "success": True,
            "status": "success",
            "message": "Feedback flow triggered.",
        }

    except frappe.DoesNotExistError:
        return {
            "success": False,
            "status": "not_found",
            "message": "Submission not found.",
        }

    except Exception as e:
        # M-2 (2026-06-15): unify with safe_sp_api_error_response so this
        # Glific-facing whitelisted endpoint follows the same L-077 pattern
        # as every other SP endpoint (rollback-first, length-capped log,
        # flat-map response, double-fault defense around log_error itself).
        return safe_sp_api_error_response(
            e, "ready_to_receive_feedback",
            extras={"submission_id": submission_id},
        )


def _build_feedback_flow_message(submission):
    return {
        "submission_id": submission.name,
        "student_id": submission.student_id,
        "feedback": {
            "overall_feedback": submission.overall_feedback or "",
        },
    }


def _mark_feedback_requested(submission_id, requested_at):
    result = frappe.db.sql(
        """
        UPDATE `tabSubmission`
        SET send_feedback = 'yes',
            feedback_requested_at = COALESCE(feedback_requested_at, %s)
        WHERE name = %s
          AND feedback_flow_triggered_at IS NULL
        RETURNING name
        """,
        (requested_at, submission_id),
    )
    return bool(result)


# ════════════════════════════════════════════════════════════
# ATOMIC PRIMARY CLAIM
# ════════════════════════════════════════════════════════════

def _try_claim_primary(pe, week):
    """
    Atomically claim primary submission for this week.
    Uses UPDATE WHERE to prevent race conditions.

    Returns True if this is the primary (first) submission, False if duplicate.

    Postgres-only — relies on UPDATE ... RETURNING. The previous MariaDB
    fallback branch was removed (task #79 / audit 2026-05-15): on Postgres the
    primary UPDATE's failure poisons the transaction (L-030), so the fallback
    would always read stale state. The fallback also reproduced the same
    `IN %s` bug fixed below — see L-005.

    L-005 fix (task #77 / audit 2026-05-15): the previous `journey_label IN %s`
    binding mangles tuple-of-strings on Postgres (Frappe's `modify_values`
    flattens the inner sequence). Use flat `IN (%s, %s, ...)` with scalar
    params — same shape as validators.py:197 and pe_dispatcher.py:108.
    """
    # Atomic UPDATE: only succeeds if journey_label is still pre-submission.
    result = frappe.db.sql("""
        UPDATE `tabProgramEnrollment`
        SET journey_label = 'submitted',
            last_label_change_at = NOW(),
            submission_count = COALESCE(submission_count, 0) + 1,
            last_submission_at = NOW()
        WHERE name = %s
          AND journey_label IN (%s, %s, %s, %s, %s)
        RETURNING name
    """, (pe.name, "enrolled", "content_delivered", "grace_window", "resumed", "week_advanced"))

    if result:
        pe.reload()
        return True

    # Already submitted — this is a duplicate
    pe.reload()
    return False


# ════════════════════════════════════════════════════════════
# POINTS CALCULATION
# ════════════════════════════════════════════════════════════
#
# CR-007 (2026-05-19): the legacy `_calculate_points(pe)` function was
# removed. It always read from EscalationStep — even for on-time
# submissions (sent_count==0 → steps[0].points_awarded) — and never
# consulted Assignment.points_per_item or the submission_validation_enabled
# flag. Points are now awarded by feedback_consumer_hook.on_feedback_ready
# after AI validation lands. See:
#   - feedback_consumer_hook._compute_submission_points  (the new logic)
#   - WeekRule.submission_validation_enabled              (the gate)
#   - Assignment.points_per_item                          (on-time award)
#   - EscalationStep.points_awarded                       (late award)


# ════════════════════════════════════════════════════════════
# SUBMISSION RECORD
# ════════════════════════════════════════════════════════════

def _create_submission(pe, student_id, week, payload, assignment_id, is_primary):
    """Create assessment-style Submission with summer-program context."""
    assignment_id = normalize_unicode_surrogates(assignment_id)
    doc = frappe.new_doc("Submission")
    doc.assign_id = assignment_id
    doc.student_id = student_id
    doc.submission_type = payload.get("submission_type")
    doc.submission_text = payload.get("submission_text")
    doc.submission_url = payload.get("submission_url")
    doc.status = "Pending" if is_primary else "Completed"
    doc.program_enrollment = pe.name
    doc.week = week
    doc.escalation_step_at_submit = pe.current_escalation_step or 0
    doc.is_primary = 1 if is_primary else 0
    doc.created_at = now_datetime()
    doc.insert(ignore_permissions=True)
    doc._raw_submission = payload.get("raw_submission")
    return doc


def _apply_duplicate_submission_feedback(submission_doc, language):
    """Mark duplicate submissions with stock feedback, without queueing AI review."""
    english_feedback = _get_stock_feedback("English", "double_submission") or {}
    translated_feedback = _get_stock_feedback(language, "double_submission") or english_feedback

    overall_feedback = english_feedback.get("translated_feedback")
    overall_feedback_translated = translated_feedback.get("translated_feedback") or overall_feedback
    audio_feedback_url = translated_feedback.get("audio_feedback_url") or english_feedback.get("audio_feedback_url")

    updates = {
        "result_status": "Success - Flagged",
        "overall_feedback": overall_feedback,
        "overall_feedback_translated": overall_feedback_translated,
        "audio_feedback_url": audio_feedback_url,
    }
    frappe.db.set_value(
        "Submission",
        submission_doc.name,
        updates,
        update_modified=False,
    )
    for field, value in updates.items():
        setattr(submission_doc, field, value)


def _get_stock_feedback(language, message_type):
    stock_data = _load_stock_feedback_data()
    language_entries = _find_stock_feedback_language_entries(stock_data, language)
    if not language_entries:
        language_entries = stock_data.get("English", [])

    for entry in language_entries:
        if entry.get("message_type") == message_type:
            return entry
    return None


def _find_stock_feedback_language_entries(stock_data, language):
    language_name = _normalize_stock_feedback_language(language)
    for stock_language, entries in stock_data.items():
        if stock_language.lower() == language_name.lower():
            return entries
    return None


def _normalize_stock_feedback_language(language):
    if not language:
        return "English"

    normalized = str(language).strip()
    language_aliases = {
        "en": "English",
        "hi": "Hindi",
        "pa": "Punjabi",
        "mr": "Marathi",
        "ka": "Kannada",
        "kn": "Kannada",
    }
    return language_aliases.get(normalized.lower(), normalized)


def _load_stock_feedback_data():
    data_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "feedback_handler",
            "stock_feedback_and_audio.json",
        )
    )
    try:
        with open(data_path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        frappe.logger("submission").error(
            f"Could not load stock feedback for duplicate submission: {exc}"
        )
        return {}


def _log_student_content_submission(
    pe, student_id, week, payload, assignment_id, points, submission_doc
):
    """Write the legacy completion log used by StudentProgression helpers."""
    try:
        assignment_id = normalize_unicode_surrogates(assignment_id)
        filters = {
            "student": student_id,
            "stage_no": week,
            "content_type": "Assignment",
            "action": "completed",
        }
        if frappe.db.exists("StudentContentLog", filters):
            return

        submission_type = payload.get("submission_type")
        is_valid = _is_expected_submission_type(
            submission_type,
            getattr(pe, "current_expected_submission_type", None),
        )

        log = frappe.new_doc("StudentContentLog")
        log.student = student_id
        log.course_level = getattr(pe, "course_level", None)
        log.stage_no = week
        log.content_type = "Assignment"
        log.content_id = assignment_id or f"sp_week_{week}_submission"
        log.content_name = f"Week {week} Submission"
        log.action = "completed"
        log.started_at = now_datetime()
        log.completed_at = today()
        log.tier = getattr(pe, "current_tier", None) or "Core"
        log.metadata = json.dumps({
            "submission_type": submission_type,
            "expected_submission_type": getattr(
                pe, "current_expected_submission_type", ""
            ),
            "is_valid": is_valid,
            "points_awarded": points,
            "source": "save_submission",
            "submission_id": submission_doc.name if submission_doc else None,
            "program_enrollment": getattr(pe, "name", None),
        })
        # Wrap the bridge-log insert in a savepoint so a failure here
        # (e.g. duplicate-name from a malformed autoname pattern) doesn't
        # poison the outer transaction. Without this, L-030 fires: the failed
        # insert aborts the txn, then frappe.log_error() in the except block
        # fails with InFailedSqlTransaction, which re-raises and rolls back
        # the whole save_submission call — losing Submission + T7 transition.
        # With a savepoint, only the StudentContentLog insert rolls back.
        sp_name = f"scl_{frappe.utils.random_string(8)}"
        try:
            frappe.db.savepoint(sp_name)
            log.insert(ignore_permissions=True)
            frappe.db.release_savepoint(sp_name)
        except Exception as e:
            frappe.db.rollback(save_point=sp_name)
            frappe.log_error(
                f"StudentContentLog submission bridge error: {str(e)}",
                "SP Save Submission",
            )
    except Exception as e:
        # Catches anything outside the savepoint block (build errors etc.)
        # These are bugs, not transient state — just log without further txn changes.
        frappe.log_error(
            f"StudentContentLog submission bridge build error: {str(e)}",
            "SP Save Submission",
        )


def _build_submission_response(pe, student_id, submission_doc, is_primary, points, week):
    return {
        "success": True,
        "status": "accepted" if is_primary else "duplicate",
        "is_primary": is_primary,
        "points_awarded": points,
        "submission_count": pe.submission_count or 1,
        "week": week,
        "resolved_flow_state": pe.resolved_flow_state,
        "next_action_type": pe.next_action_type or "",
        "next_action_at": str(pe.next_action_at) if pe.next_action_at else "",
        "program_status": pe.program_status or "",
        "current_path": pe.current_path or "",
        "student_id": student_id,
        "submission_id": submission_doc.name if submission_doc else None,
    }


# ════════════════════════════════════════════════════════════
# ENGAGEMENT STATE
# ════════════════════════════════════════════════════════════

def _update_engagement(student_id):
    """Update EngagementState on submission.

    Task #94 hardening (2026-05-25):
      - On INSERT (new EngagementState), explicitly set known-NOT-NULL-drift
        fields (`average_response_time` etc.) to empty string. The doctype
        JSON declares them as plain Data fields (no `reqd: 1`), but the
        production DB has a NOT NULL constraint on `average_response_time`
        from a manual `ALTER TABLE ... SET NOT NULL` that was never
        backported to the JSON. Without the default, the INSERT raises
        `NotNullViolation`, which poisons the outer Postgres txn (L-030),
        which then breaks the subsequent `frappe.log_error` call with
        `InFailedSqlTransaction`, which cascades up to the @frappe.whitelist
        boundary as raw HTTP 500 HTML — breaking Glific's flow.
      - Wrap the EngagementState mutation in a SAVEPOINT so any future
        schema drift in this doctype fails CONTAINED (rolled back to the
        savepoint) instead of poisoning save_submission's outer txn.
      - On exception: rollback to the savepoint FIRST so the txn is clean
        before we attempt `frappe.log_error` (L-030).

    The submission itself is independent of EngagementState — losing the
    EngagementState write is acceptable; losing the submission is not.
    """
    savepoint = f"engagement_{frappe.utils.random_string(8)}"
    try:
        frappe.db.sql(f"SAVEPOINT {savepoint}")

        es = frappe.db.get_value(
            "EngagementState", {"student": student_id},
            ["name", "last_activity_date", "current_streak"], as_dict=True,
        )
        today_date = getdate(today())

        if es:
            updates = {"last_activity_date": today_date, "last_updated": now_datetime()}
            last = es.last_activity_date
            if last:
                if isinstance(last, str):
                    last = getdate(last)
                days_diff = (today_date - last).days
                if days_diff == 1:
                    updates["current_streak"] = (es.current_streak or 0) + 1
                elif days_diff > 1:
                    updates["current_streak"] = 1
            else:
                updates["current_streak"] = 1
            frappe.db.set_value("EngagementState", es.name, updates)
        else:
            new_es = frappe.new_doc("EngagementState")
            new_es.student = student_id
            new_es.last_activity_date = today_date
            new_es.current_streak = 1
            new_es.last_updated = now_datetime()
            # Task #94: defaults for fields that have DB-level NOT NULL
            # constraints even though the doctype JSON says they're nullable.
            # Schema drift — production DB was manually ALTERed at some
            # point. Setting empty string satisfies NOT NULL.
            new_es.average_response_time = ""
            new_es.completion_rate = ""
            new_es.re_engagement_attempts = ""
            new_es.insert(ignore_permissions=True)

        frappe.db.sql(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as e:
        # Rollback to savepoint clears the poisoned-txn state from the
        # failed EngagementState write. WITHOUT this, the subsequent
        # log_error fails with InFailedSqlTransaction and escapes up to
        # the whitelisted entry-point as HTML 500.
        try:
            frappe.db.sql(f"ROLLBACK TO SAVEPOINT {savepoint}")
        except Exception:
            # Savepoint may not exist if SAVEPOINT itself failed earlier.
            # Last-resort rollback (loses the whole txn — better than
            # propagating the unhandled exception).
            frappe.db.rollback()
        frappe.log_error(f"EngagementState error: {str(e)}", "SP Engagement")


# ════════════════════════════════════════════════════════════
# SUBMISSION NORMALIZATION
# ════════════════════════════════════════════════════════════

def _normalize_submission_payload(submission, pe=None):
    """
    Normalize only enough to persist a placeholder Submission.

    No text-vs-URL or media inference happens here; the raw value is passed to
    the async processing job after the API response is returned.
    """
    if not isinstance(submission, str) or not submission.strip():
        frappe.throw("Submission is required")

    return {
        "raw_submission": submission.strip(),
        "submission_type": None,
        "submission_text": None,
        "submission_url": None,
    }


def _contains_only_emoji(submission):
    text = submission.strip()
    if not text:
        return False

    return not any(char.isalnum() for char in text)


def _looks_like_url(submission):
    parsed = urlparse(submission.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _to_assessment_submission_type(submission_type):
    mapping = {
        "text_word": "text",
        "voice_note": "audio",
        "photo": "image",
        "photo_video_artefact": "image",
        "voice_note_text_summary": "audio",
    }
    return mapping.get(submission_type or "", submission_type or "")


def _is_expected_submission_type(actual_type, expected_type):
    if not actual_type or not expected_type:
        return True

    actual = _to_assessment_submission_type(actual_type.lower().strip())
    expected = expected_type.lower().strip()

    compatible = {
        "photo_video_artefact": {"image", "video"},
        "voice_note_text_summary": {"audio", "text"},
    }
    if expected in compatible:
        return actual in compatible[expected]

    return actual == _to_assessment_submission_type(expected)


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════

def _resolve_student(identifier):
    """Delegate to shared utility."""
    from tap_lms.summer_program.utils import resolve_student
    return resolve_student(identifier)


def _build_pe_context(pe):
    return {
        "program_enrollment": pe.name,
        "archetype": pe.archetype,
        "experiment_arm": pe.experiment_arm,
        "expected_submission_type": pe.current_expected_submission_type,
        "language": getattr(pe, "language", ""),
        "batch": pe.batch,
        "current_week": pe.current_week,
        "current_path": pe.current_path,
        "current_tier": pe.current_tier,
        "course_level": pe.course_level,
        "current_escalation_step": pe.current_escalation_step,
    }


def _queue_submission_processing(submission_doc, pe_context):
    frappe.enqueue(
        "tap_lms.summer_program.save_submission.process_submission_async",
        queue="long",
        timeout=600,
        enqueue_after_commit=True,
        submission_id=submission_doc.name,
        raw_submission=getattr(submission_doc, "_raw_submission", None),
        pe_context=pe_context,
    )


def process_submission_async(
    submission_id,
    raw_submission=None,
    submission_url=None,
    pe_context=None,
):
    """
    Upload URL submissions to GCS, mark the record Processing, and enqueue
    feedback processing. Text and emoji submissions skip GCS upload.
    """
    pe_context = pe_context or {}
    try:
        submission = frappe.get_doc("Submission", submission_id)

        raw_submission = (raw_submission or submission_url or "").strip()

        if raw_submission and _looks_like_url(raw_submission):
            from tap_lms.imgana.media_detection import detect_url_media_type
            from tap_lms.imgana.gcs_client import upload_to_gcs

            media_type = detect_url_media_type(raw_submission, default="image")
            uploaded_url = upload_to_gcs(
                raw_submission,
                submission.name,
                media_type=media_type,
            )
            submission.submission_type = media_type
            submission.submission_url = uploaded_url
            submission.submission_text = None
        elif raw_submission:
            submission.submission_type = (
                "emoji" if _contains_only_emoji(raw_submission) else "text"
            )
            submission.submission_text = raw_submission
            submission.submission_url = None

        submission.status = "Processing"
        submission.upload_error_log = None
        submission.save(ignore_permissions=True)
        frappe.db.commit()

        enqueue_submission(submission.name, pe_context=pe_context)

    except Exception as e:
        frappe.db.rollback()
        # H-4 (2026-06-15): upgrade from file-logger to frappe.log_error so
        # the failure is operator-visible in tabError Log (L-056). The
        # original pattern logged ONLY to the file logger, marked the
        # submission status=Failed, and returned normally — RQ recorded the
        # job as `finished`, no DLQ entry, no queue-depth alarm; students
        # silently stuck. CR-026 (2026-06-09) was the most recent incident
        # caused by this pattern.
        frappe.log_error(
            f"Error in background processing for submission {submission_id}: {str(e)}",
            "SP process_submission_async",
        )

        try:
            submission = frappe.get_doc("Submission", submission_id)
            submission.status = "Failed"
            submission.upload_error_log = frappe.get_traceback()[:5000]
            submission.save(ignore_permissions=True)
            frappe.db.commit()
        except Exception as log_error:
            frappe.log_error(
                f"Failed to update submission {submission_id} after background error: {str(log_error)}",
                "SP process_submission_async",
            )

        # H-4: re-raise so RQ marks the job as failed and the DLQ logic
        # engages. Without this, RQ thinks the job succeeded.
        raise


def enqueue_submission(submission_id, pe_context=None, retry_count=0):
    try:
        import pika
        from tap_lms.imgana.submission import get_rabbitmq_settings

        pe_context = pe_context or {}
        submission = frappe.get_doc("Submission", submission_id)

        payload = {
            "submission_id": submission.name,
            "assign_id": submission.assign_id,
            "student_id": submission.student_id,
            "submission_type": submission.submission_type,
            "submission_text": submission.submission_text,
            "submission_url": submission.submission_url,
            "program_enrollment": getattr(
                submission,
                "program_enrollment",
                pe_context.get("program_enrollment", ""),
            ),
            "week": getattr(submission, "week", pe_context.get("current_week", 1)),
            "is_primary": getattr(submission, "is_primary", 1),
            "escalation_step_at_submit": getattr(
                submission,
                "escalation_step_at_submit",
                pe_context.get("current_escalation_step", 0),
            ),
            "archetype": pe_context.get("archetype", ""),
            "experiment_arm": pe_context.get("experiment_arm", ""),
            "expected_submission_type": pe_context.get("expected_submission_type", ""),
            "language": pe_context.get("language", ""),
            "batch": pe_context.get("batch", ""),
            "current_week": pe_context.get("current_week", 1),
            "current_path": pe_context.get("current_path", ""),
            "current_tier": pe_context.get("current_tier", ""),
            "course_level": pe_context.get("course_level", ""),
            "created_at": str(getattr(submission, "created_at", submission.creation)),
        }

        rabbitmq_config = get_rabbitmq_settings()
        credentials = pika.PlainCredentials(
            rabbitmq_config["username"],
            rabbitmq_config["password"],
        )
        parameters = pika.ConnectionParameters(
            rabbitmq_config["host"],
            int(rabbitmq_config["port"]),
            rabbitmq_config["virtual_host"],
            credentials,
        )

        connection = None
        publish_succeeded = False
        try:
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            channel.confirm_delivery()
            channel.queue_declare(queue=rabbitmq_config["queue"], durable=True)
            channel.basic_publish(
                exchange="",
                routing_key=rabbitmq_config["queue"],
                body=json.dumps(payload, default=str),
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                ),
                mandatory=True,
            )
            publish_succeeded = True
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as close_err:
                    frappe.logger("submission").warning(
                        f"RabbitMQ close failed for {submission_id} "
                        f"(publish_succeeded={publish_succeeded}): {close_err}"
                    )

        if publish_succeeded:
            frappe.logger("submission").info(
                f"Enqueued submission {submission_id} with type {submission.submission_type}"
            )
    except Exception as e:
        frappe.logger("submission").error(
            f"Failed to enqueue submission {submission_id}: {str(e)}"
        )
        retry_count = (retry_count or 0) + 1
        student_id = ""

        try:
            student_id = frappe.db.get_value("Submission", submission_id, "student_id") or ""
        except Exception:
            student_id = ""

        if retry_count <= FEEDBACK_PIPELINE_MAX_RETRIES:
            frappe.log_error(
                title=FEEDBACK_PIPELINE_RETRY_LOG_TITLE,
                message=(
                    f"Feedback pipeline transient failure "
                    f"(attempt {retry_count}/{FEEDBACK_PIPELINE_MAX_RETRIES + 1}) "
                    f"for submission {submission_id} "
                    f"(student={student_id or 'unknown'}): {e}"
                ),
            )
            try:
                frappe.enqueue(
                    "tap_lms.summer_program.save_submission.enqueue_submission",
                    queue="default",
                    timeout=120,
                    submission_id=submission_id,
                    pe_context=pe_context,
                    retry_count=retry_count,
                )
            except Exception as enqueue_err:
                frappe.log_error(
                    title=FEEDBACK_PIPELINE_DLQ_LOG_TITLE,
                    message=json.dumps(
                        {
                            "reason": "double_fault_enqueue_failed",
                            "submission_id": submission_id,
                            "student_id": student_id,
                            "pe_context": pe_context,
                            "final_error": str(e),
                            "enqueue_error": str(enqueue_err),
                            "retries_attempted": retry_count,
                        },
                        indent=2,
                        default=str,
                    ),
                )
        else:
            frappe.log_error(
                title=FEEDBACK_PIPELINE_DLQ_LOG_TITLE,
                message=json.dumps(
                    {
                        "submission_id": submission_id,
                        "student_id": student_id,
                        "pe_context": pe_context,
                        "final_error": str(e),
                        "retries_attempted": retry_count,
                    },
                    indent=2,
                    default=str,
                ),
            )
