"""
Summer Program — dev / test reset helpers.

Tools for re-running a student (or all students in a batch) through the SP
journey from state 0. Used during integration testing where the same dev
contacts need to be cycled through many times.

**USE FOR DEV/TEST ONLY.** Every function in this module DESTROYS journey
history (StudentQuizAttempt / StudentContentLog / StudentStageProgress /
StudentReflection / TransitionHistory / ProgramEventLog rows). A safety
guard refuses to run on sites whose name suggests a production environment
unless the caller explicitly opts in.

**Submission rows are deliberately PRESERVED across resets** so the team
can analyse AI-feedback quality and submission-pipeline behaviour across
many test cycles for the same student. If you need a truly clean slate
(e.g. to test the save_submission claim logic), delete the rows manually
via console.

Usage from `bench execute`:

    bench --site tap_lms.dev execute \\
        tap_lms.summer_program.dev_tools.list_pes_for_batch \\
        --kwargs '{"batch_name": "palv2-test-BT52231"}'

    bench --site tap_lms.dev execute \\
        tap_lms.summer_program.dev_tools.reset_pe_to_state_0 \\
        --kwargs '{"student_id": "STU-0001", "dry_run": true}'

    bench --site tap_lms.dev execute \\
        tap_lms.summer_program.dev_tools.reset_pe_to_state_0 \\
        --kwargs '{"student_id": "STU-0001"}'

    bench --site tap_lms.dev execute \\
        tap_lms.summer_program.dev_tools.reset_pes_for_batch \\
        --kwargs '{"batch_name": "palv2-test-BT52231"}'

Usage from `bench console`:

    >>> from tap_lms.summer_program.dev_tools import reset_pe_to_state_0
    >>> reset_pe_to_state_0("STU-0001", dry_run=True)
    >>> reset_pe_to_state_0("STU-0001")
"""
import frappe

from tap_lms.summer_program.constants import (
    STATE_NORMAL_CONTENT,
    LABEL_ENROLLED,
    PROGRAM_ACTIVE,
    PROGRAM_PAUSED,
    PATH_CORE,
    # Task #84: pull the canonical lists from constants instead of
    # hardcoding so the dev_tools validation stays in sync with the
    # enum source-of-truth used elsewhere (state machine, doctype
    # Select options, Glific field contracts).
    ALL_ARCHETYPES,
    ALL_ARMS,
    # Task #87: TIER_BY_WEEK gives the week-1 default tier (Basic)
    # for create_test_student_with_pe — mirrors _process_pe_chunk.
    TIER_BY_WEEK,
)
# Hoisted at module level so tests can patch at the canonical location
# `tap_lms.summer_program.dev_tools.{maintain_collections,_enqueue_contact_field_sync}`
# without depending on Python's late-binding behaviour for inline imports.
from tap_lms.summer_program.collection_membership import maintain_collections
from tap_lms.summer_program.state_machine import _enqueue_contact_field_sync
# Task #82/#84: dev_tools reset must recompute current_expected_submission_type
# from the archetype's WeekRule, and update_student_state must do the same
# when week/path/archetype/arm change. The canonical helpers live in the
# enrollment API module; importing them here keeps a single source of truth.
from tap_lms.summer_program.program_enrollment_api import (
    _get_week1_submission_type,
    _get_expected_submission_type_for_week,
)


# ════════════════════════════════════════════════════════════
# Safety guard
# ════════════════════════════════════════════════════════════

_PRODUCTION_SITE_MARKERS = ("prod", "live", "production")


def _assert_dev_site(i_know_this_is_destructive=False):
    """Refuse to run on sites whose name suggests a production environment.

    The check is heuristic — it pattern-matches the site name against a
    small denylist (`prod`, `live`, `production`). False positives are
    unlocked by passing `i_know_this_is_destructive=True`, which is the
    operator's signed acknowledgement.

    Raises:
        frappe.PermissionError on a suspected-prod site without override.
    """
    if i_know_this_is_destructive:
        return

    site = (frappe.local.site or "").lower()
    for marker in _PRODUCTION_SITE_MARKERS:
        if marker in site:
            raise frappe.PermissionError(
                f"dev_tools refuses to run on site '{site}' (matched "
                f"production marker '{marker}'). If you really mean to "
                f"do this, pass i_know_this_is_destructive=True."
            )


# ════════════════════════════════════════════════════════════
# Listing helper
# ════════════════════════════════════════════════════════════

def list_pes_for_batch(batch_name):
    """Print all active/paused PEs for a batch with key state fields.

    Read-only — no safety guard needed.

    Returns the list of dicts so callers (or tests) can introspect.
    """
    frappe.db.rollback()  # PG txn hygiene

    rows = frappe.db.sql(
        """
        SELECT
            pe.name                   AS pe,
            pe.student,
            s.name1                   AS student_name,
            pe.resolved_flow_state,
            pe.current_week,
            pe.current_path,
            pe.journey_label,
            pe.submission_count,
            pe.current_escalation_step,
            pe.delivery_failure_count,
            pe.glific_id,
            pe.program_status
          FROM "tabProgramEnrollment" pe
          LEFT JOIN "tabStudent" s ON s.name = pe.student
         WHERE pe.batch = %s
           AND pe.program_status IN ('active', 'paused')
         ORDER BY pe.name
        """,
        (batch_name,),
        as_dict=True,
    )

    for r in rows:
        name = r.get("student_name") or "<no name>"
        print(
            f"{r['pe']}  {name:25}  "
            f"w{r['current_week']} {r['current_path'] or '-':8} "
            f"state={r['resolved_flow_state']:30} "
            f"step={r['current_escalation_step']} "
            f"subs={r['submission_count']} "
            f"status={r['program_status']}"
        )
    print(f"\n{len(rows)} PEs total in batch {batch_name}.")
    return rows


# ════════════════════════════════════════════════════════════
# Single-PE reset
# ════════════════════════════════════════════════════════════

# Fields zeroed by the reset. Listed explicitly so adding a new PE column
# without thinking about reset behaviour is a visible omission (rather than
# silently retaining stale data).
_FIELDS_TO_ZERO = (
    # Counters
    "submission_count",
    "current_escalation_step",
    "last_escalation_step",
    "delivery_failure_count",
    # Grace
    "in_grace_window",
    # CR-002 v2 gamification — per-stream totals AND the rollup.
    # Task #85: total_points was previously missing here, so per-stream
    # totals got zeroed but total_points stayed at its old value, breaking
    # the invariant `total_activity + total_quiz + total_submission ==
    # total_points`. This was the exact pattern of ST00051295's drift in
    # the 2026-05-24 audit (total_points=1053, all per-stream=0). Zeroing
    # total_points alongside the streams keeps the invariant intact and
    # makes the Glific reconcile push a coherent state-0 bundle.
    "total_points",
    "total_activity_points",
    "weekly_activity_points",
    "total_quiz_points",
    "weekly_quiz_points",
    "bonus_quiz_points",
    "total_submission_points",
    "weekly_submission_points",
    "special_gems",
    "current_streak",
    # CR-002 v2 + CR-003 follow-up 2 sticky flags
    "weekly_video_done",
    "weekly_submission_done",
)

# Fields nulled by the reset (vs. zeroed).
_FIELDS_TO_NULL = (
    "next_action_at",
    "grace_window_start",
    "grace_window_end_at",
)

# Fields cleared to empty string.
# Task #85: current_escalation_type and pause_reason were previously
# missing. current_escalation_step zeros to 0 but the type string ('parent_call',
# 'help_note_b') stayed at its old value — Glific contact then ended up with
# step=0 + type='parent_call' (mismatch). pause_reason similarly carried over
# 'binge_limit' even though program_status was switched back to active.
_FIELDS_TO_CLEAR = (
    "next_action_type",
    "drop_reason",
    "current_escalation_type",
    "pause_reason",
)

# Historical/audit rows to delete when delete_history=True.
# Tuple shape: (doctype, FK field name ON that doctype, source-key — either
# 'student' to fill with pe.student, or 'pe_name' to fill with the PE name).
#
# FK names verified against the doctype JSONs (2026-05-16):
#   - StudentQuizAttempt.student / StudentContentLog.student are Link to Student
#   - ProgramEventLog.enrollment is the PE link (NOT 'program_enrollment')
#   - StudentStageProgress.student is a Link to Student. CRITICAL: this is
#     the doctype `get_next_content` reads to track current_content_index /
#     is_on_remedial / active_content_type / current_question_index — without
#     clearing it, the next-content API stays stuck on the old position even
#     after the PE row is reset.
#   - StudentReflection.student / TransitionHistory.student are Link to Student.
#
# Submission is DELIBERATELY OMITTED from this list (2026-05-18 product
# decision). The team analyses AI-feedback quality across many test cycles
# for the same student, so historical Submission rows must persist. Trade-
# offs accepted: (a) PE.submission_count is reset to 0 while old Submission
# rows persist — counter is intentionally inconsistent with row count, and
# (b) re-submitting the same week may hit save_submission's claim logic
# depending on uniqueness constraints. Both are dev-only nuisances; the
# benefit of feedback-quality analysis outweighs them.
_HISTORY_DOCTYPES = (
    ("StudentQuizAttempt",    "student",            "student"),
    ("StudentContentLog",     "student",            "student"),
    ("StudentStageProgress",  "student",            "student"),
    ("StudentReflection",     "student",            "student"),
    ("TransitionHistory",     "student",            "student"),
    ("ProgramEventLog",       "enrollment",         "pe_name"),
)


@frappe.whitelist(allow_guest=False)
def reset_pe_to_state_0(
    student_id,
    dry_run=False,
    delete_history=True,
    push_to_glific=True,
    verbose=True,
    i_know_this_is_destructive=False,
):
    """Reset a student's latest ProgramEnrollment to its initial state.

    Resets:
      - PE state machine fields → state 0 (normal_content_delivery, week 1,
        Core path, enrolled label, active status)
      - All counters → 0
      - Grace window → cleared
      - CR-002 v2 gamification points (weekly + total) → 0
      - Sticky weekly flags → 0
      - Scheduler pointers (next_action_at, next_action_type) → cleared

    Optionally:
      - Deletes journey history when `delete_history=True` (default):
        StudentQuizAttempt, StudentContentLog, StudentStageProgress,
        StudentReflection, TransitionHistory, ProgramEventLog.
        NOTE: Submission rows are DELIBERATELY PRESERVED so the team can
        analyse AI-feedback quality across many reset cycles for the same
        student. See the comment on `_HISTORY_DOCTYPES` for the rationale.
      - Pushes fresh contact fields to Glific when `push_to_glific=True`
        (default), so Glific flows see the reset state immediately.
      - Calls `maintain_collections(pe, from_state, to_state=normal)` so the
        CR-005 Glific group membership is corrected (PE removed from any
        escalation/audit group, ensured in `main`).

    Does NOT touch:
      - Student.archetype / Student.experiment_arm / Student.glific_id
        (upstream-supplied data per CLAUDE.md)
      - BPR aggregate counters
      - Batch / ArchetypeConfig / WeekRule (configuration)

    Args:
        student_id: Student.name whose latest ProgramEnrollment is reset
        dry_run: if True, print intended changes without writing
        delete_history: if True, delete journey audit rows for this student
        push_to_glific: if True, enqueue a contact-field sync job
        verbose: if True, print before/after snapshot
        i_know_this_is_destructive: bypass the production-site safety guard

    Returns:
        dict with `before` and `after` snapshots, plus `history_deleted` counts.

    Raises:
        frappe.PermissionError if site name suggests production.
        frappe.DoesNotExistError if student_id is invalid.
        frappe.ValidationError if student has no PE.
    """
    dry_run = _coerce_bool(dry_run)
    delete_history = _coerce_bool(delete_history)
    push_to_glific = _coerce_bool(push_to_glific)
    verbose = _coerce_bool(verbose)
    i_know_this_is_destructive = _coerce_bool(i_know_this_is_destructive)

    _assert_dev_site(i_know_this_is_destructive=i_know_this_is_destructive)

    frappe.db.rollback()  # PG txn hygiene

    if not frappe.db.exists("Student", student_id):
        frappe.throw(f"Student not found: {student_id}", frappe.DoesNotExistError)

    pe = _get_latest_pe_for_student(student_id)
    if not pe:
        frappe.throw(
            f"No ProgramEnrollment found for student {student_id}",
            frappe.ValidationError,
        )

    return _reset_pe_doc_to_state_0(
        pe,
        dry_run=dry_run,
        delete_history=delete_history,
        push_to_glific=push_to_glific,
        verbose=verbose,
    )


def _reset_pe_doc_to_state_0(
    pe,
    dry_run=False,
    delete_history=True,
    push_to_glific=True,
    verbose=True,
):
    """Reset an already-resolved PE doc. Keep public API resolution separate."""
    pe_name = pe.name
    student_id = pe.student

    # ── Snapshot history counts ──────────────────────────
    history_counts = {}
    for dt, field, key_source in _HISTORY_DOCTYPES:
        key = student_id if key_source == "student" else pe_name
        try:
            history_counts[dt] = frappe.db.count(dt, {field: key})
        except Exception as e:
            history_counts[dt] = f"<error: {e}>"

    before = _snapshot(pe)
    before["history_rows"] = history_counts

    if verbose:
        print(f"\nPE {pe_name} (student={student_id}) BEFORE:")
        for k, v in before.items():
            print(f"  {k:30} = {v}")

    if dry_run:
        if verbose:
            print("\nDRY RUN — nothing written.")
        return {"before": before, "after": None, "history_deleted": {}}

    from_state = pe.resolved_flow_state
    history_deleted = {}

    # ── 1. Delete journey history (if requested) ─────────
    if delete_history:
        for dt, field, key_source in _HISTORY_DOCTYPES:
            key = student_id if key_source == "student" else pe_name
            try:
                frappe.db.sql(
                    f'DELETE FROM "tab{dt}" WHERE "{field}" = %s',
                    (key,),
                )
                history_deleted[dt] = history_counts.get(dt, 0)
                if verbose:
                    print(f"  deleted {history_deleted[dt]} rows from {dt}")
            except Exception as e:
                history_deleted[dt] = f"<error: {e}>"
                if verbose:
                    print(f"  could not delete from {dt}: {e}")

    # ── 2. Reset PE state-machine fields ─────────────────
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.journey_label = LABEL_ENROLLED
    pe.program_status = PROGRAM_ACTIVE
    pe.current_week = 1
    pe.current_path = PATH_CORE
    if hasattr(pe, "current_tier"):
        pe.current_tier = "Basic"  # week-1 default per TIER_BY_WEEK

    for f in _FIELDS_TO_ZERO:
        if hasattr(pe, f):
            setattr(pe, f, 0)
    for f in _FIELDS_TO_NULL:
        if hasattr(pe, f):
            setattr(pe, f, None)
    for f in _FIELDS_TO_CLEAR:
        if hasattr(pe, f):
            setattr(pe, f, "")

    # Task #82 (bug fix): recompute current_expected_submission_type so it
    # matches the archetype's week-1 WeekRule, not whatever stale value the
    # PE carried from a later week. Mirrors the normal enrollment flow at
    # program_enrollment_api._process_pe_chunk:268. Without this, Glific
    # flows fail validation (expecting word_text_voice when the week-1
    # rule actually expects image, etc.).
    if hasattr(pe, "current_expected_submission_type"):
        try:
            batch_doc = frappe.get_doc("Batch", pe.batch)
            expected = _get_week1_submission_type(
                batch_doc, pe.archetype, pe.experiment_arm,
            )
            pe.current_expected_submission_type = expected or ""
            if verbose:
                print(
                    f"  recomputed current_expected_submission_type for "
                    f"archetype={pe.archetype}, arm={pe.experiment_arm}, "
                    f"path=Core, week=1 → {expected!r}"
                )
        except Exception as e:
            # Don't fail the reset if the lookup goes sideways — leave the
            # field empty and let the operator notice via the reconcile diff.
            if verbose:
                print(
                    f"  could not recompute current_expected_submission_type: "
                    f"{e}. Setting to empty string."
                )
            pe.current_expected_submission_type = ""

    pe.save(ignore_permissions=True)

    # ── 3. CR-005: fix Glific group membership ───────────
    maintain_collections(pe, from_state=from_state, to_state=STATE_NORMAL_CONTENT)

    # ── 4. Push fresh contact fields to Glific ───────────
    # Task #83: synchronous reconcile (was: async _enqueue_contact_field_sync).
    # Matches update_student_state + reset_and_update_student behavior so the
    # test team gets immediate visibility into what landed on Glific. Returns
    # a diff so the operator can audit any drift inline.
    reconcile_result = None
    if push_to_glific and pe.glific_id:
        try:
            reconcile_result = reconcile_pe_to_glific(
                pe.name, dry_run=False, verbose=verbose,
            )
        except Exception as e:
            if verbose:
                print(f"  reconcile_pe_to_glific failed: {e}")

    # Skip commit under the test runner — FrappeTestCase relies on
    # transaction rollback for test isolation, and an explicit commit
    # here would leak this PE's reset state into the next test case.
    # In real bench/console usage, the commit is required so workers
    # see the saved state.
    if not getattr(frappe.flags, "in_test", False):
        frappe.db.commit()

    after = _snapshot(pe)
    if verbose:
        print(f"\nPE {pe_name} AFTER:")
        for k, v in after.items():
            print(f"  {k:30} = {v}")
        print(
            f"\nCR-005 group-write jobs enqueued "
            f"(from={from_state} → {STATE_NORMAL_CONTENT}). "
            f"Background workers will sync to Glific within seconds."
        )

    return {
        "before": before,
        "after": after,
        "history_deleted": history_deleted,
        # Task #83: synchronous reconcile result, so callers see the diff
        # that was pushed to Glific (or None if push was skipped).
        "reconcile": reconcile_result,
    }


# ════════════════════════════════════════════════════════════
# Bulk reset
# ════════════════════════════════════════════════════════════

def reset_pes_for_batch(
    batch_name,
    dry_run=False,
    delete_history=True,
    push_to_glific=True,
    i_know_this_is_destructive=False,
):
    """Reset every active/paused PE in a batch to state 0.

    Calls `reset_pe_to_state_0` per PE with `verbose=False` to keep output
    sane on a batch of 9+ students. Prints a one-line summary per PE and
    a totals line.

    Returns the per-PE result dicts keyed by PE name.
    """
    dry_run = _coerce_bool(dry_run)
    delete_history = _coerce_bool(delete_history)
    push_to_glific = _coerce_bool(push_to_glific)
    i_know_this_is_destructive = _coerce_bool(i_know_this_is_destructive)

    _assert_dev_site(i_know_this_is_destructive=i_know_this_is_destructive)

    frappe.db.rollback()

    rows = frappe.db.sql(
        """
            SELECT name, student FROM "tabProgramEnrollment"
             WHERE batch = %s AND program_status IN ('active', 'paused')
             ORDER BY name
        """,
        (batch_name,),
        as_dict=True,
    )

    if not rows:
        print(f"No active/paused PEs in batch {batch_name}.")
        return {}

    if dry_run:
        print(f"DRY RUN — would reset {len(rows)} PEs in batch {batch_name}:")
        for row in rows:
            print(f"  {row.name}  student={row.student}")
        return {}

    print(f"Resetting {len(rows)} PEs in batch {batch_name}…")
    results = {}
    for row in rows:
        pe_name = row.name
        try:
            pe = frappe.get_doc("ProgramEnrollment", pe_name)
            result = _reset_pe_doc_to_state_0(
                pe,
                dry_run=False,
                delete_history=delete_history,
                push_to_glific=push_to_glific,
                verbose=False,
            )
            results[pe_name] = result
            print(f"  {pe_name}  reset OK")
        except Exception as e:
            results[pe_name] = {"error": str(e)}
            print(f"  {pe_name}  FAILED: {e}")

    ok = sum(1 for r in results.values() if "error" not in r)
    print(f"\nDone — {ok}/{len(rows)} succeeded.")
    return results


# ════════════════════════════════════════════════════════════
# Student state mutation — archetype / experiment_arm / PE state
# ════════════════════════════════════════════════════════════
#
# For testing only. Lets operators update a student's archetype /
# experiment_arm (Student fields) and optionally key PE-state fields
# (program_status, current_week, current_path) in one call, then
# reconciles to Glific so contact fields land.
#
# Why this exists:
#   - Student.archetype + Student.experiment_arm are upstream-supplied
#     (CLAUDE.md). Operationally we still need to flip them during testing.
#   - Three places hold the same data: Student, ProgramEnrollment
#     (denormalized at enrollment), Glific contact fields. All three must
#     agree or the dispatcher/state-machine + Glific flows disagree on
#     what archetype the student "is".


# Task #84: source these from constants so the validation can never drift
# out of sync with the canonical enum. Previous hardcoded list had 'lurker'
# (invalid — never in ALL_ARCHETYPES) and was missing 'irregular_submitter'
# (canonical) — operators trying to flip to irregular_submitter got a
# confusing validation error.
_VALID_ARCHETYPES = tuple(ALL_ARCHETYPES)
_VALID_ARMS = tuple(ALL_ARMS)
_VALID_PROGRAM_STATUSES = ("active", "paused", "completed", "dropped")
_VALID_PATHS = ("Core", "Remedial")


@frappe.whitelist(allow_guest=False)
def update_student_state(
    student_id,
    archetype=None,
    experiment_arm=None,
    program_status=None,
    current_week=None,
    current_path=None,
    push_to_glific=True,
    dry_run=False,
    i_know_this_is_destructive=False,
):
    """Update a student's archetype / experiment_arm + optional PE state.

    Writes to:
      - Student.archetype, Student.experiment_arm
      - ProgramEnrollment.archetype, .experiment_arm (denormalized copies)
      - Optional: ProgramEnrollment.program_status, .current_week, .current_path

    After Frappe writes complete, calls reconcile_pe_to_glific to push the
    canonical state to Glific contact fields (synchronous).

    Args (all positional-or-keyword via Frappe whitelist):
        student_id: Student.name (required)
        archetype: one of fence_sitter / dormant / lurker / submitter (or None to skip)
        experiment_arm: one of default / arm_a / arm_b (or None)
        program_status: one of active / paused / completed / dropped (or None)
        current_week: int (or None)
        current_path: one of Core / Remedial (or None)
        push_to_glific: bool, default True
        dry_run: bool, default False — compute the change set without writing
        i_know_this_is_destructive: bypass production-site safety guard

    Returns:
        {
            "student_id": ..., "pe_name": ...,
            "before": {<snapshot>},
            "after": {<snapshot>},
            "applied": {<field: new_value>},
            "reconcile": <reconcile result dict>,
            "dry_run": <bool>,
        }

    Raises:
        frappe.PermissionError on suspected production site without override.
        frappe.ValidationError on invalid enum value or missing student/PE.
    """
    dry_run = _coerce_bool(dry_run)
    push_to_glific = _coerce_bool(push_to_glific)
    i_know_this_is_destructive = _coerce_bool(i_know_this_is_destructive)

    _assert_dev_site(i_know_this_is_destructive=i_know_this_is_destructive)
    frappe.db.rollback()  # PG txn hygiene

    if not frappe.db.exists("Student", student_id):
        frappe.throw(
            f"Student not found: {student_id}", frappe.ValidationError
        )

    # ── Validate enum inputs ────────────────────────────────
    if archetype is not None and archetype not in _VALID_ARCHETYPES:
        frappe.throw(
            f"Invalid archetype {archetype!r}; must be one of {_VALID_ARCHETYPES}",
            frappe.ValidationError,
        )
    if experiment_arm is not None and experiment_arm not in _VALID_ARMS:
        frappe.throw(
            f"Invalid experiment_arm {experiment_arm!r}; must be one of {_VALID_ARMS}",
            frappe.ValidationError,
        )
    if program_status is not None and program_status not in _VALID_PROGRAM_STATUSES:
        frappe.throw(
            f"Invalid program_status {program_status!r}; must be one of {_VALID_PROGRAM_STATUSES}",
            frappe.ValidationError,
        )
    if current_path is not None and current_path not in _VALID_PATHS:
        frappe.throw(
            f"Invalid current_path {current_path!r}; must be one of {_VALID_PATHS}",
            frappe.ValidationError,
        )
    if current_week is not None:
        try:
            current_week = int(current_week)
        except (TypeError, ValueError):
            frappe.throw(
                f"current_week must be an integer; got {current_week!r}",
                frappe.ValidationError,
            )

    # ── Snapshot before ─────────────────────────────────────
    student = frappe.get_doc("Student", student_id)
    pe = _get_active_pe_for_student(student_id)
    pe_name = pe.name if pe else None

    before = {
        "student.archetype": student.archetype,
        "student.experiment_arm": student.experiment_arm,
        "pe.archetype": pe.archetype if pe else None,
        "pe.experiment_arm": pe.experiment_arm if pe else None,
        "pe.program_status": pe.program_status if pe else None,
        "pe.current_week": pe.current_week if pe else None,
        "pe.current_path": pe.current_path if pe else None,
        # Task #84: surface this in the snapshot so callers can verify the
        # recompute happened (or didn't, if no week/path/archetype/arm change).
        "pe.current_expected_submission_type": (
            pe.current_expected_submission_type if pe else None
        ),
    }

    # ── Compute applied diff (only fields the caller passed) ────
    applied = {}
    student_updates = {}
    pe_updates = {}

    if archetype is not None:
        if student.archetype != archetype:
            student_updates["archetype"] = archetype
            applied["student.archetype"] = archetype
        if pe and pe.archetype != archetype:
            pe_updates["archetype"] = archetype
            applied["pe.archetype"] = archetype
    if experiment_arm is not None:
        if student.experiment_arm != experiment_arm:
            student_updates["experiment_arm"] = experiment_arm
            applied["student.experiment_arm"] = experiment_arm
        if pe and pe.experiment_arm != experiment_arm:
            pe_updates["experiment_arm"] = experiment_arm
            applied["pe.experiment_arm"] = experiment_arm
    if pe and program_status is not None and pe.program_status != program_status:
        pe_updates["program_status"] = program_status
        applied["pe.program_status"] = program_status
    if pe and current_week is not None and pe.current_week != current_week:
        pe_updates["current_week"] = current_week
        applied["pe.current_week"] = current_week
    if pe and current_path is not None and pe.current_path != current_path:
        pe_updates["current_path"] = current_path
        applied["pe.current_path"] = current_path

    # Task #84: if ANY of (current_week, current_path, archetype, experiment_arm)
    # changed, recompute current_expected_submission_type from the WeekRule
    # for the NEW combination. Without this, the field stays at whatever
    # value it had pre-update — e.g., caller fast-forwards to week=2 but
    # current_expected_submission_type is still week-1's value. Glific flows
    # would then prompt for the wrong submission type and validation fails.
    # Uses the same _get_expected_submission_type_for_week helper as the
    # enrollment flow + the reset, so all three paths agree on the rule.
    if pe and any(k in pe_updates for k in (
        "current_week", "current_path", "archetype", "experiment_arm",
    )):
        # Resolve the post-update values (use update if present, else current PE value).
        final_week = pe_updates.get("current_week", pe.current_week or 1)
        final_path = pe_updates.get("current_path", pe.current_path or PATH_CORE)
        final_archetype = pe_updates.get("archetype", pe.archetype)
        final_arm = pe_updates.get("experiment_arm", pe.experiment_arm)
        try:
            batch_doc = frappe.get_doc("Batch", pe.batch)
            new_expected = _get_expected_submission_type_for_week(
                batch_doc, final_archetype, final_arm, final_path, final_week,
            )
            new_value = new_expected or ""
            if pe.current_expected_submission_type != new_value:
                pe_updates["current_expected_submission_type"] = new_value
                applied["pe.current_expected_submission_type"] = new_value
        except Exception as e:
            # Don't fail the whole update if the lookup errors — fall back
            # to empty string so the operator notices via the reconcile diff
            # rather than seeing a stale value persist.
            pe_updates["current_expected_submission_type"] = ""
            applied["pe.current_expected_submission_type"] = (
                f"<recompute failed: {e}> — cleared to empty"
            )

    if dry_run:
        return {
            "student_id": student_id,
            "pe_name": pe_name,
            "before": before,
            "after": None,
            "applied": applied,
            "reconcile": None,
            "dry_run": True,
        }

    # ── Write ────────────────────────────────────────────────
    if student_updates:
        frappe.db.set_value("Student", student_id, student_updates)
    if pe and pe_updates:
        frappe.db.set_value("ProgramEnrollment", pe.name, pe_updates)
    frappe.db.commit()

    # ── Snapshot after ──────────────────────────────────────
    student.reload()
    if pe:
        pe.reload()
    after = {
        "student.archetype": student.archetype,
        "student.experiment_arm": student.experiment_arm,
        "pe.archetype": pe.archetype if pe else None,
        "pe.experiment_arm": pe.experiment_arm if pe else None,
        "pe.program_status": pe.program_status if pe else None,
        "pe.current_week": pe.current_week if pe else None,
        "pe.current_path": pe.current_path if pe else None,
        "pe.current_expected_submission_type": (
            pe.current_expected_submission_type if pe else None
        ),
    }

    # ── Push to Glific via reconciler ───────────────────────
    reconcile_result = None
    if push_to_glific and pe and pe.glific_id:
        reconcile_result = reconcile_pe_to_glific(
            pe.name, dry_run=False, verbose=False
        )

    return {
        "student_id": student_id,
        "pe_name": pe_name,
        "before": before,
        "after": after,
        "applied": applied,
        "reconcile": reconcile_result,
        "dry_run": False,
    }


# ════════════════════════════════════════════════════════════
# Create a fresh test Student + ProgramEnrollment in one call
# ════════════════════════════════════════════════════════════
#
# Task #87 — completes the dev_tools quartet (reset, update_student_state,
# reset_and_update_student, create_test_student_with_pe). The QA team
# needed a single API to spin up a new student against an existing batch
# without going through the full Backend Student Onboarding flow (CSV
# upload → page handler → manual Glific contact creation → BPR pipeline).
#
# This is a TEST-ONLY shortcut. It bypasses BPR counters, the legacy
# LearningState/EngagementState seeding, and (by default) the Glific
# contact-creation roundtrip. Use Backend Student Onboarding for
# production onboarding.
#
# Minimum input set (3 required + 1 highly recommended):
#   name, phone, batch        — must pass
#   glific_id                 — optional but skip_glific_sync forces False
#                               when this is empty
#
# Everything else has sensible defaults that mirror _process_pe_chunk's
# enrollment-time logic. See CLAUDE.md "Conventions for this app" for
# the L-029 note that archetype/experiment_arm are NORMALLY upstream-
# supplied; the default here is for test convenience only.


@frappe.whitelist(allow_guest=False)
def create_test_student_with_pe(
    name,
    phone,
    batch,
    glific_id="",
    archetype="submitter",
    experiment_arm="default",
    language=None,
    course_level=None,
    grade=None,
    school_id=None,
    gender="Not Available",
    skip_glific_sync=True,
    glific_group_id=None,
    dry_run=False,
    i_know_this_is_destructive=False,
):
    """Create a Student + ProgramEnrollment in one call for testing.

    Mirrors _process_pe_chunk's field-set on the PE side, so the resulting
    PE matches what a real BPR-driven enrollment produces. Differences
    from production onboarding:
      - Bypasses BPR.total_enrolled counter update (no BPR involved).
      - Skips LearningState / EngagementState / StudentStageProgress
        initialization (legacy onboarding state, not needed for SP testing).
      - Default skip_glific_sync=True — does NOT enqueue the 28-field
        contact-field push or add the contact to any Glific group. Tests
        usually don't want real Glific HTTP calls.
      - Skips the sibling-PE check by design (test student normally has
        a unique glific_id so won't collide; if you reuse a glific_id,
        the partial unique index in Postgres still catches the collision
        and raises DuplicateEntryError).

    Idempotency:
      - On (phone, name1) for Student: reuses existing Student doc.
      - On (student, batch) for active/paused PE: returns the existing PE.

    Args:
        name: Student.name1 (display name). Required.
        phone: Phone number; normalized to 12-digit (`91XXXXXXXXXX`). Required.
        batch: Batch doc name (e.g. 'palv2-test-BT52231'). Must exist. Required.
        glific_id: Student.glific_id + PE.glific_id. Empty string is allowed
            (Glific sync auto-skips); pass a real ID if testing Glific flows.
        archetype: PE.archetype + Student.archetype. Default 'submitter'.
            Must be in constants.ALL_ARCHETYPES.
        experiment_arm: PE.experiment_arm + Student.experiment_arm. Default
            'default'. Must be in constants.ALL_ARMS.
        language: Optional TAP Language link.
        course_level: Optional Course Level link. If None, PE.course_level=None
            (content-delivery APIs that depend on course_level will fail —
            pass a real Course Level if testing those paths).
        grade: Optional Student.grade.
        school_id: Optional School link.
        gender: Default 'Not Available'.
        skip_glific_sync: If True (default), don't enqueue the 28-field push.
            Set False to exercise the real Glific sync end-to-end.
        glific_group_id: Optional Glific group ID. When skip_glific_sync=False
            AND glific_id is set, calls add_contact_to_group as a follow-up.
        dry_run: Validate inputs + return the would-be payload without writing.
        i_know_this_is_destructive: Bypass the production-site safety guard.

    Returns:
        {
            "student_id": <Student.name>,
            "pe_name": <ProgramEnrollment.name>,
            "created_student": <bool>,    # False if reused existing
            "created_pe": <bool>,         # False if reused existing
            "glific_synced": <bool>,
            "expected_submission_type": <str | None>,
            "dry_run": <bool>,
        }

    Raises:
        frappe.PermissionError on suspected production site without override.
        frappe.ValidationError on invalid enum value, missing Batch, or
            unparseable phone.
    """
    import frappe.utils
    dry_run = _coerce_bool(dry_run)
    skip_glific_sync = _coerce_bool(skip_glific_sync)
    i_know_this_is_destructive = _coerce_bool(i_know_this_is_destructive)

    _assert_dev_site(i_know_this_is_destructive=i_know_this_is_destructive)
    frappe.db.rollback()  # PG txn hygiene

    # ── Input validation ─────────────────────────────────────
    if not name or not str(name).strip():
        frappe.throw("name is required", frappe.ValidationError)
    if not phone or not str(phone).strip():
        frappe.throw("phone is required", frappe.ValidationError)
    if not batch or not str(batch).strip():
        frappe.throw("batch is required", frappe.ValidationError)
    if not frappe.db.exists("Batch", batch):
        frappe.throw(f"Batch not found: {batch}", frappe.ValidationError)
    if archetype not in _VALID_ARCHETYPES:
        frappe.throw(
            f"Invalid archetype {archetype!r}; must be one of {_VALID_ARCHETYPES}",
            frappe.ValidationError,
        )
    if experiment_arm not in _VALID_ARMS:
        frappe.throw(
            f"Invalid experiment_arm {experiment_arm!r}; must be one of {_VALID_ARMS}",
            frappe.ValidationError,
        )

    # Normalize phone the same way Backend Student Onboarding does
    # (12-digit Indian format with country code).
    from tap_lms.tap_lms.page.backend_onboarding_process.backend_onboarding_process import (
        normalize_phone_number, find_existing_student_by_phone_and_name,
    )
    phone_12, phone_10 = normalize_phone_number(phone)
    if not phone_12:
        frappe.throw(
            f"Could not normalize phone {phone!r}; expected 10-digit "
            f"or 12-digit with 91 prefix.",
            frappe.ValidationError,
        )
    normalized_phone = phone_12   # Backend onboarding stores 12-digit form

    batch_doc = frappe.get_doc("Batch", batch)

    # ── Compute the expected_submission_type now so dry-run shows it ──
    expected_submission = _get_week1_submission_type(
        batch_doc, archetype, experiment_arm,
    )

    if dry_run:
        return {
            "student_id": None,
            "pe_name": None,
            "created_student": None,
            "created_pe": None,
            "glific_synced": False,
            "expected_submission_type": expected_submission,
            "dry_run": True,
            "would_use": {
                "name1": name, "phone": normalized_phone,
                "glific_id": glific_id, "archetype": archetype,
                "experiment_arm": experiment_arm, "language": language,
                "course_level": course_level, "grade": grade,
                "school_id": school_id, "gender": gender,
                "batch": batch,
            },
        }

    # ── Resolve or create Student ────────────────────────────
    existing = find_existing_student_by_phone_and_name(normalized_phone, name)
    if existing:
        student_id = existing["name"]
        created_student = False
        # Update key fields in case the test re-runs with different params
        # (archetype/arm flips are the common test pattern).
        student_doc = frappe.get_doc("Student", student_id)
        student_doc.archetype = archetype
        student_doc.experiment_arm = experiment_arm
        if glific_id:
            student_doc.glific_id = glific_id
        if language:
            student_doc.language = language
        if grade:
            student_doc.grade = grade
        if school_id:
            student_doc.school_id = school_id
        student_doc.save(ignore_permissions=True)
    else:
        student_doc = frappe.new_doc("Student")
        student_doc.name1 = name
        student_doc.phone = normalized_phone
        student_doc.gender = gender
        student_doc.status = "active"
        student_doc.joined_on = frappe.utils.nowdate()
        student_doc.archetype = archetype
        student_doc.experiment_arm = experiment_arm
        if glific_id:
            student_doc.glific_id = glific_id
        if language:
            student_doc.language = language
        if grade:
            student_doc.grade = grade
        if school_id:
            student_doc.school_id = school_id
        student_doc.insert(ignore_permissions=True)
        student_id = student_doc.name
        created_student = True

    # ── Idempotency: return existing active/paused PE if one exists ──
    existing_pe = frappe.db.get_value(
        "ProgramEnrollment",
        {
            "student": student_id,
            "batch": batch,
            "program_status": ["in", [PROGRAM_ACTIVE, PROGRAM_PAUSED]],
        },
        "name",
        order_by="creation desc",
    )
    if existing_pe:
        return {
            "student_id": student_id,
            "pe_name": existing_pe,
            "created_student": created_student,
            "created_pe": False,
            "glific_synced": False,
            "expected_submission_type": expected_submission,
            "dry_run": False,
            "note": "PE already exists for (student, batch); returning it idempotently.",
        }

    # ── Create the PE (mirrors _process_pe_chunk lines 251-283) ──
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"{student_id}-{batch}"
    pe.student = student_id
    pe.batch = batch
    pe.program_type = batch_doc.program_type or "Summer"
    pe.glific_id = glific_id or ""
    pe.course_level = course_level
    pe.language = language
    pe.experiment_arm = experiment_arm
    pe.archetype = archetype
    pe.current_path = PATH_CORE
    pe.current_tier = TIER_BY_WEEK.get(1, "Basic")
    pe.journey_label = LABEL_ENROLLED
    pe.last_label_change_at = frappe.utils.now_datetime()
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.current_expected_submission_type = expected_submission
    pe.current_week = 1
    pe.max_allowed_week = (batch_doc.current_calendar_week or 1) + 1
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

    try:
        pe.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError as e:
        # Sibling-glific_id collision caught by the partial unique index.
        # Re-query and return the existing PE rather than erroring.
        frappe.db.rollback()
        existing_pe = frappe.db.get_value(
            "ProgramEnrollment",
            {
                "batch": batch, "glific_id": glific_id,
                "program_status": ["in", [PROGRAM_ACTIVE, PROGRAM_PAUSED]],
            },
            "name",
        )
        return {
            "student_id": student_id,
            "pe_name": existing_pe,
            "created_student": created_student,
            "created_pe": False,
            "glific_synced": False,
            "expected_submission_type": expected_submission,
            "dry_run": False,
            "note": f"Sibling PE collision (glific_id={glific_id}); returned existing PE.",
        }

    # ── Optional Glific sync ────────────────────────────────
    glific_synced = False
    if not skip_glific_sync and pe.glific_id:
        try:
            reconcile_pe_to_glific(pe.name, dry_run=False, verbose=False)
            glific_synced = True
        except Exception as e:
            frappe.log_error(
                f"create_test_student_with_pe: reconcile failed for "
                f"pe={pe.name}: {e}",
                "SP Dev Tools Create",
            )

        # Optional group add
        if glific_group_id:
            try:
                from tap_lms.glific_integration import add_contact_to_group
                add_contact_to_group(pe.glific_id, glific_group_id)
            except Exception as e:
                frappe.log_error(
                    f"create_test_student_with_pe: add_contact_to_group "
                    f"failed for glific_id={pe.glific_id} group={glific_group_id}: {e}",
                    "SP Dev Tools Create",
                )

    if not getattr(frappe.flags, "in_test", False):
        frappe.db.commit()

    return {
        "student_id": student_id,
        "pe_name": pe.name,
        "created_student": created_student,
        "created_pe": True,
        "glific_synced": glific_synced,
        "expected_submission_type": expected_submission,
        "dry_run": False,
    }


# ════════════════════════════════════════════════════════════
# Combined: reset PE + update Student identity in one call
# ════════════════════════════════════════════════════════════


@frappe.whitelist(allow_guest=False)
def reset_and_update_student(
    student_id,
    archetype=None,
    experiment_arm=None,
    program_status=None,
    current_week=None,
    current_path=None,
    delete_history=True,
    dry_run=False,
    i_know_this_is_destructive=False,
):
    """One-shot: reset_pe_to_state_0 + update_student_state, single Glific push.

    Atomic-ish wrapper around the two existing endpoints. Use when a test
    cycle needs to restart a student from week 1 AND change their archetype
    or experiment_arm at the same time.

    Sequence:
      1. reset_pe_to_state_0(push_to_glific=False) — clears PE state-machine
         fields, counters, history; maintains Glific group memberships;
         skips contact-field push (we'll do one combined push at the end).
      2. update_student_state(push_to_glific=True) — writes new Student
         archetype/arm + optional PE state fields, then reconciles ALL
         fields to Glific in a single round-trip.

    Args mirror the two underlying functions:
        student_id: required
        archetype, experiment_arm: optional new identity (None to skip)
        program_status, current_week, current_path: optional PE-state overrides
            applied AFTER the reset (e.g., if you want the resulting PE
            to be `paused` instead of `active`, or already at week 2)
        delete_history: passed to reset_pe_to_state_0
        dry_run: bool
        i_know_this_is_destructive: production-site safety override

    Returns:
        {
            "student_id": ..., "pe_name": ...,
            "phase1_reset": <result of reset_pe_to_state_0>,
            "phase2_update": <result of update_student_state>,
            "dry_run": <bool>,
        }

    Raises:
        Same as the two underlying functions.
    """
    dry_run = _coerce_bool(dry_run)
    delete_history = _coerce_bool(delete_history)
    i_know_this_is_destructive = _coerce_bool(i_know_this_is_destructive)

    _assert_dev_site(i_know_this_is_destructive=i_know_this_is_destructive)
    frappe.db.rollback()

    # ── Phase 1: reset PE to state 0 ────────────────────────
    # push_to_glific=False — defer Glific push to phase 2 so the team only
    # pays one Glific round-trip per call.
    phase1 = reset_pe_to_state_0(
        student_id,
        dry_run=dry_run,
        delete_history=delete_history,
        push_to_glific=False,
        verbose=False,
        i_know_this_is_destructive=True,  # outer guard already passed
    )

    # ── Phase 2: update Student identity + optional PE state ────
    # push_to_glific=True — reconciles the FULL 28-field bundle to Glific
    # in one shot, picking up both the reset-cleared values AND the new
    # archetype/arm.
    phase2 = update_student_state(
        student_id,
        archetype=archetype,
        experiment_arm=experiment_arm,
        program_status=program_status,
        current_week=current_week,
        current_path=current_path,
        push_to_glific=True,
        dry_run=dry_run,
        i_know_this_is_destructive=True,  # outer guard already passed
    )

    pe_name = phase2.get("pe_name") or (phase1 or {}).get("pe_name")

    return {
        "student_id": student_id,
        "pe_name": pe_name,
        "phase1_reset": phase1,
        "phase2_update": phase2,
        "dry_run": dry_run,
    }


# ════════════════════════════════════════════════════════════
# Glific reconciliation (task #51, 2026-05-21)
# ════════════════════════════════════════════════════════════
#
# When Frappe PE state and Glific contact fields drift apart (failed sync
# job, manual data edit, parallel writer bypassing the state machine), these
# helpers rebuild the canonical 28-field bundle from PE state and push it
# synchronously to Glific. Frappe PE is the source of truth.
#
# Use cases:
#   - Post-incident cleanup after _enqueue_contact_field_sync DLQ'd (task #5).
#   - After dev_tools.reset_pe_to_state_0 if Glific has stale weekly_* values
#     the async sync hasn't drained yet.
#   - Pre-launch audit to confirm every cohort PE matches Glific.
#
# These DO NOT mutate Frappe PE state. Strictly one-way Frappe → Glific.


def reconcile_pe_to_glific(pe_name, dry_run=False, verbose=True):
    """Re-push the canonical 28-field bundle for one PE to its Glific contact.

    Synchronous (does NOT go through frappe.enqueue). Returns a dict with the
    fields that differed pre-push, so the operator can audit.

    Args:
        pe_name: ProgramEnrollment doc name.
        dry_run: If True, compute the diff but do NOT push to Glific.
        verbose: Print field-by-field diff.

    Returns: {
        "pe": <pe_name>, "glific_id": <id>,
        "diff": [{"field": ..., "frappe": ..., "glific": ...}, ...],
        "pushed": <bool>,
    }
    """
    import json
    import requests
    from tap_lms.summer_program.utils import get_student_display_name
    from tap_lms.glific_integration import (
        update_contact_fields,
        get_glific_settings,
        get_glific_auth_headers,
    )
    from tap_lms.summer_program.constants import (
        CF_STUDENT_ID, CF_BATCH_ID, CF_ARCHETYPE, CF_LANGUAGE_ID,
        CF_EXPERIMENT_ARM, CF_COURSE_LEVEL, CF_STUDENT_NAME,
        CF_RESOLVED_FLOW_STATE, CF_CURRENT_WEEK, CF_CURRENT_PATH,
        CF_CURRENT_TIER, CF_PROGRAM_STATUS, CF_TOTAL_POINTS,
        CF_CURRENT_STREAK, CF_GRACE_WINDOW_END, CF_EXPECTED_SUBMISSION,
        CF_LAST_ESCALATION_STEP, CF_SUBMISSION_COUNT,
        CF_TOTAL_ACTIVITY_POINTS, CF_WEEKLY_ACTIVITY_POINTS,
        CF_TOTAL_QUIZ_POINTS, CF_WEEKLY_QUIZ_POINTS,
        CF_TOTAL_SUBMISSION_POINTS, CF_WEEKLY_SUBMISSION_POINTS,
        CF_SPECIAL_GEMS, CF_WEEKLY_SUBMISSION_DONE,
        CF_BONUS_QUIZ_POINTS,
        CF_WEEKLY_ENGAGEMENT_POINTS,
        CF_ESCALATION_ORDER, CF_ESCALATION_TYPE,
    )

    dry_run = _coerce_bool(dry_run)
    pe = frappe.get_doc("ProgramEnrollment", pe_name)
    if not pe.glific_id:
        if verbose:
            print(f"PE {pe_name}: no glific_id — skip")
        return {"pe": pe_name, "glific_id": None, "diff": [], "pushed": False}

    student = frappe.get_doc("Student", pe.student)
    batch = frappe.get_doc("Batch", pe.batch)

    # Mirror _process_pe_chunk's enrollment-time bundle so the reconciler
    # stays in sync with the canonical writer. If a field is added to that
    # writer, mirror it here too.
    glific_language_id = ""
    if student.language:
        glific_language_id = str(
            frappe.db.get_value("TAP Language", student.language, "glific_language_id") or ""
        )

    expected = {
        CF_STUDENT_ID: pe.student,
        CF_BATCH_ID: batch.batch_id or batch.name,
        CF_ARCHETYPE: pe.archetype or "",
        CF_LANGUAGE_ID: glific_language_id,
        CF_EXPERIMENT_ARM: pe.experiment_arm or "",
        CF_COURSE_LEVEL: pe.course_level or "",
        CF_STUDENT_NAME: get_student_display_name(student),
        CF_RESOLVED_FLOW_STATE: pe.resolved_flow_state or "",
        CF_CURRENT_WEEK: str(pe.current_week or 1),
        CF_CURRENT_PATH: pe.current_path or "Core",
        CF_CURRENT_TIER: pe.current_tier or "Basic",
        CF_PROGRAM_STATUS: pe.program_status or "active",
        CF_TOTAL_POINTS: str(pe.total_points or 0),
        CF_CURRENT_STREAK: str(pe.current_streak or 0),
        CF_GRACE_WINDOW_END: str(pe.grace_window_end_at or ""),
        CF_EXPECTED_SUBMISSION: pe.current_expected_submission_type or "",
        CF_LAST_ESCALATION_STEP: str(pe.current_escalation_step or 0),
        CF_SUBMISSION_COUNT: str(pe.submission_count or 0),
        CF_TOTAL_ACTIVITY_POINTS: str(pe.total_activity_points or 0),
        CF_WEEKLY_ACTIVITY_POINTS: str(pe.weekly_activity_points or 0),
        CF_TOTAL_QUIZ_POINTS: str(pe.total_quiz_points or 0),
        CF_WEEKLY_QUIZ_POINTS: str(pe.weekly_quiz_points or 0),
        CF_TOTAL_SUBMISSION_POINTS: str(pe.total_submission_points or 0),
        CF_WEEKLY_SUBMISSION_POINTS: str(pe.weekly_submission_points or 0),
        CF_SPECIAL_GEMS: str(pe.special_gems or 0),
        CF_WEEKLY_SUBMISSION_DONE: str(int(pe.weekly_submission_done or 0)),
        # Task #98 (2026-05-25): reconcile bonus_quiz_points too — required
        # for the periodic_glific_reconcile cron to push the field on PEs
        # that were enrolled before task #98 landed.
        CF_BONUS_QUIZ_POINTS: str(pe.bonus_quiz_points or 0),
        # Task #7 (2026-05-26): weekly_engagement_points is COMPUTED, not
        # stored. Reconciled here so the cron / manual reconcile pushes the
        # sum-of-source-columns value to Glific when the contact's stored
        # value drifts (e.g. after a manual db.set_value on either addend).
        CF_WEEKLY_ENGAGEMENT_POINTS: str(
            (pe.weekly_submission_points or 0)
            + (pe.weekly_activity_points or 0)
        ),
        CF_ESCALATION_ORDER: str(pe.current_escalation_step or 0),
        CF_ESCALATION_TYPE: getattr(pe, "current_escalation_type", "") or "",
    }

    # Fetch Glific contact to diff
    settings = get_glific_settings()
    payload = {
        "query": "query contact($id: ID!) { contact(id: $id) { contact { id fields } } }",
        "variables": {"id": str(pe.glific_id)},
    }
    r = requests.post(f"{settings.api_url}/api", json=payload,
                      headers=get_glific_auth_headers(), timeout=15).json()
    contact = (r.get("data") or {}).get("contact", {}).get("contact") or {}
    raw_fields = contact.get("fields")
    glific_fields = json.loads(raw_fields) if isinstance(raw_fields, str) else (raw_fields or {})

    diff = []
    for k, v_expected in expected.items():
        raw = glific_fields.get(k)
        v_glific = raw.get("value") if isinstance(raw, dict) else raw
        if str(v_glific or "") != str(v_expected or ""):
            diff.append({"field": k, "frappe": v_expected, "glific": v_glific})

    if verbose:
        print(f"PE {pe_name} → Glific {pe.glific_id}")
        if not diff:
            print(f"  ✓ no mismatches")
        else:
            print(f"  ✗ {len(diff)} mismatches:")
            for d in diff:
                print(f"    {d['field']:30s} frappe={d['frappe']!r:25s} glific={d['glific']!r}")

    if not diff:
        return {"pe": pe_name, "glific_id": pe.glific_id, "diff": [], "pushed": False}

    if dry_run:
        if verbose:
            print(f"  DRY RUN — would push {len(diff)} fields")
        return {"pe": pe_name, "glific_id": pe.glific_id, "diff": diff, "pushed": False}

    # Push only the fields that differ, to minimize payload churn.
    fields_to_push = {d["field"]: d["frappe"] for d in diff}

    # Also push the CORE Glific language if the language_id field is in the diff
    # (single round-trip via the language_id kwarg).
    core_lang = glific_language_id if any(d["field"] == CF_LANGUAGE_ID for d in diff) else None
    ok = update_contact_fields(
        contact_id=pe.glific_id,
        fields_to_update=fields_to_push,
        language_id=core_lang,
    )
    if verbose:
        print(f"  pushed: {ok}")
    return {"pe": pe_name, "glific_id": pe.glific_id, "diff": diff, "pushed": bool(ok)}


def reconcile_batch_to_glific(batch_name, dry_run=False, verbose=True):
    """Reconcile every active/paused PE in a batch to Glific.

    Loops PEs and calls reconcile_pe_to_glific per row. When `verbose=True`
    (default, suits bench-console callers) prints a one-line summary per
    PE, a per-field roll-up so drift patterns are visible (added 2026-05-25
    — "which fields are drifting across the cohort?"), plus totals at the
    end. When `verbose=False` (used by `periodic_glific_reconcile` cron)
    stays silent — the caller logs its own per-batch summary so we don't
    spam stdout every 10 minutes.

    Returns: {pe_name: <per-pe result dict>, ...}
    """
    from collections import Counter, defaultdict

    dry_run = _coerce_bool(dry_run)

    rows = frappe.db.sql(
        """
            SELECT name, student FROM "tabProgramEnrollment"
             WHERE batch = %s AND program_status IN ('active', 'paused')
             ORDER BY name
        """,
        (batch_name,),
        as_dict=True,
    )
    if not rows:
        if verbose:
            print(f"No active/paused PEs in batch {batch_name}.")
        return {}

    if verbose:
        print(f"Reconciling {len(rows)} PEs in batch {batch_name} "
              f"({'DRY RUN' if dry_run else 'LIVE'})…")
    results = {}
    total_mismatches = 0
    total_pushed = 0
    # Aggregations for the per-field roll-up.
    field_drift_counts = Counter()      # field -> num PEs drifting on that field
    field_drift_examples = defaultdict(list)  # field -> [(pe, student, frappe, glific), ...]
    pes_with_drift = []                  # PEs that had ≥1 mismatch
    for row in rows:
        try:
            result = reconcile_pe_to_glific(row.name, dry_run=dry_run, verbose=False)
            results[row.name] = result
            n = len(result["diff"])
            total_mismatches += n
            if result.get("pushed"):
                total_pushed += 1
            if n:
                pes_with_drift.append((row.name, row.student, n))
                for d in result["diff"]:
                    field_drift_counts[d["field"]] += 1
                    # Keep up to 3 examples per field so the roll-up stays terse.
                    # Use .get() defensively — callers / tests may pass diff
                    # entries that only carry a `field` key (e.g. mocks).
                    if len(field_drift_examples[d["field"]]) < 3:
                        field_drift_examples[d["field"]].append(
                            (row.name, row.student,
                             d.get("frappe"), d.get("glific"))
                        )
            if verbose:
                print(f"  {row.name}  student={row.student}  mismatches={n}  "
                      f"{'pushed' if result.get('pushed') else 'no-push'}")
        except Exception as e:
            results[row.name] = {"error": str(e)}
            if verbose:
                print(f"  {row.name}  student={row.student}  FAILED: {e}")

    if verbose:
        print(f"\nDone — total mismatches across {len(rows)} PEs: "
              f"{total_mismatches}; PEs pushed: {total_pushed}.")

    # ── Per-field roll-up ───────────────────────────────────────────
    # Shows which fields drift the most across the cohort. Useful for
    # spotting systemic bugs (e.g. "total_points drifts on 12 PEs" =
    # silent sync failure in award handler; "current_streak drifts on
    # 1 PE" = isolated incident). Silent in cron mode (verbose=False).
    if verbose and field_drift_counts:
        print(f"\nPer-field drift roll-up ({len(field_drift_counts)} fields, "
              f"{len(pes_with_drift)}/{len(rows)} PEs affected):")
        # Sort by count desc, then field name.
        for field, count in sorted(field_drift_counts.items(),
                                   key=lambda kv: (-kv[1], kv[0])):
            print(f"  {field:30s}  {count:>3d} PE(s)")
            for pe_name, student, frappe_val, glific_val in field_drift_examples[field]:
                print(f"      e.g. {pe_name}  student={student}  "
                      f"frappe={frappe_val!r}  glific={glific_val!r}")
        # Highlight critical fields (silent bugs vs cosmetic drift).
        CRITICAL = {
            "total_points",
            "total_activity_points",
            "weekly_activity_points",
            "total_quiz_points",
            "weekly_quiz_points",
            "total_submission_points",
            "weekly_submission_points",
            "special_gems",
            "current_streak",
            "resolved_flow_state",
            "program_status",
            "current_week",
        }
        critical_drift = {f: n for f, n in field_drift_counts.items() if f in CRITICAL}
        if critical_drift:
            print(f"\n  ⚠ CRITICAL field drift (likely a real bug — "
                  f"check award handlers / state-machine sync):")
            for field, count in sorted(critical_drift.items(),
                                       key=lambda kv: (-kv[1], kv[0])):
                print(f"      {field:30s}  {count:>3d} PE(s)")

    return results


# ════════════════════════════════════════════════════════════
# Bootstrap: register SP contact field DEFINITIONS on Glific
# Added 2026-05-26 — Glific support flagged that updateContact only writes
# field VALUES; flow dropdowns and @contact.fields.X resolution need a
# separate createContactsField call per field DEFINITION. Without this,
# template tokens render as literal text (e.g. Himani's "Submission Missing!"
# card showing "@co" instead of bonus_quiz_points).
# ════════════════════════════════════════════════════════════

# (shortcode, display_name) tuples — shortcode MUST match the CF_* constants
# in constants.py so @contact.fields.<shortcode> matches what we push. Display
# names are human-readable for the Glific UI dropdown.
#
# When you add a new contact field to the SP, add a row here AND re-run
# `bootstrap_sp_contact_fields()` to register the definition on every Glific
# org. Without that step the value will be set but flows can't read it.
SP_CONTACT_FIELD_DEFINITIONS = [
    # Identity (7) — pushed once at enrollment, immutable thereafter.
    ("student_id",                       "Student ID"),
    ("student_name",                     "Student Name"),
    ("batch_id",                         "Batch ID"),
    ("archetype",                        "Archetype"),
    ("language_id",                      "Language ID"),
    ("experiment_arm",                   "Experiment Arm"),
    ("course_level",                     "Course Level"),
    # Base state (11) — re-pushed on every state-machine transition.
    ("resolved_flow_state",              "Resolved Flow State"),
    ("current_week",                     "Current Week"),
    ("current_path",                     "Current Path"),
    ("current_tier",                     "Current Tier"),
    ("program_status",                   "Program Status"),
    ("total_points",                     "Total Points"),
    ("current_streak",                   "Current Streak"),
    ("grace_window_end_at",              "Grace Window End At"),
    ("current_expected_submission_type", "Expected Submission Type"),
    ("last_escalation_step",             "Last Escalation Step"),
    ("submission_count",                 "Submission Count"),
    # CR-002 v2 gamification (9 — includes bonus_quiz_points added 2026-05-25).
    ("total_activity_points",            "Total Activity Points"),
    ("weekly_activity_points",           "Weekly Activity Points"),
    ("total_quiz_points",                "Total Quiz Points"),
    ("weekly_quiz_points",               "Weekly Quiz Points"),
    ("total_submission_points",          "Total Submission Points"),
    ("weekly_submission_points",         "Weekly Submission Points"),
    ("special_gems",                     "Special Gems"),
    ("weekly_submission_done",           "Weekly Submission Done"),
    ("bonus_quiz_points",                "Bonus Quiz Points"),
    # Task #7 (2026-05-26) — computed, not stored on PE. Sum of
    # weekly_submission_points + weekly_activity_points. Pushed alongside
    # the other weekly_* fields.
    ("weekly_engagement_points",         "Weekly Engagement Points"),
    # CR-003 escalation routing (2).
    ("escalation_order",                 "Escalation Order"),
    ("escalation_type",                  "Escalation Type"),
]


def bootstrap_sp_contact_fields(verbose=True):
    """Register every SP contact field DEFINITION on the connected Glific org.

    Idempotent — fields already registered return "shortcode already taken"
    from Glific, which the `register_contact_field` helper treats as success.

    Run this ONCE per Glific organization (dev, prod) right after the app
    is installed / connected to that org. Re-run whenever a new field is
    added to `SP_CONTACT_FIELD_DEFINITIONS` (cheap and safe to re-run any
    time — already-registered fields are no-ops).

    Why this exists: Glific's updateContact mutation writes the VALUE into
    the contacts.fields JSON column, but the field is invisible to the Flow
    Editor (no dropdown entry, @contact.fields.X renders as literal text)
    until a separate createContactsField mutation registers the DEFINITION.
    Glific support confirmed this 2026-05-26 in response to the "Submission
    Missing!" card rendering @contact.fields.bonus_quiz_points as garbled
    text on Himani's contact (ST00051295).

    Returns:
        Dict {shortcode: bool} where bool=True means the field is now
        registered (either freshly created or already existed).
    """
    from tap_lms.glific_integration import register_contact_field

    results = {}
    ok_count = 0
    fail_count = 0
    for shortcode, display_name in SP_CONTACT_FIELD_DEFINITIONS:
        ok = register_contact_field(shortcode, display_name)
        results[shortcode] = ok
        if ok:
            ok_count += 1
        else:
            fail_count += 1
        if verbose:
            status = "✓" if ok else "✗"
            print(f"  {status}  {shortcode:32s}  ({display_name})")

    if verbose:
        print(
            f"\nbootstrap_sp_contact_fields DONE: "
            f"{ok_count}/{len(SP_CONTACT_FIELD_DEFINITIONS)} registered "
            f"({fail_count} failed)."
        )
        if fail_count:
            print(
                "Failures listed above — check the Glific Error Log for "
                "the underlying GraphQL response. Common causes: API key "
                "rotated, network blip, or a field name with an invalid "
                "character that Glific rejected."
            )

    return results


# ════════════════════════════════════════════════════════════
# Pre-launch sanity: CR-008 video-first invariant validator
# Added 2026-05-28 (task #15 / Content R2)
# ════════════════════════════════════════════════════════════

def validate_video_first_invariant(batch_name=None, verbose=True):
    """Audit every LearningUnit used by active enrollments and confirm its
    first non-optional content item is a VideoClass.

    Why this matters (CR-008 lazy reset):
      `activity_points.handle_content_log` flips `weekly_video_done = 0→1`
      AND zeroes weekly_*_points + grace fields ONLY on a VideoClass
      completion. The CR-008 invariant ("every LU's first content is a
      VideoClass") is what guarantees the gate trips at the start of each
      week. If a LU's first content is a Quiz / NoteContent / Assignment
      etc., a student progressing through that LU never trips the gate —
      weekly_* values accumulate across weeks and grace clock never arms.
      See task #6 for the runtime symptom.

      Today (2026-05-28) nothing in the doctype layer enforces this. LU
      authors can save any content order, and the bug only surfaces in
      production when a real student hits the affected LU.

    What it does:
      Scans LearningUnits used by active ProgramEnrollments (filtered to
      batch_name if given, all batches otherwise). For each LU, fetches
      UnitContentItems ordered by idx and checks the first non-optional
      item's content_type is `VideoClass`. Reports any violations with
      enough detail to fix in the Frappe UI.

    Returns:
        Dict {"violations": [...], "checked": <int>, "ok": <int>}.
        verbose=True (default) also prints a human-readable report.

    Run pre-launch and after any content authoring session that touched
    LU ordering. Cheap and idempotent — pure read.
    """
    # Scope: which course levels are in play
    if batch_name:
        course_level_filter_sql = """
            SELECT DISTINCT course_level
              FROM "tabProgramEnrollment"
             WHERE batch = %s
               AND program_status IN ('active', 'paused')
               AND course_level IS NOT NULL
        """
        params = (batch_name,)
    else:
        course_level_filter_sql = """
            SELECT DISTINCT course_level
              FROM "tabProgramEnrollment"
             WHERE program_status IN ('active', 'paused')
               AND course_level IS NOT NULL
        """
        params = ()

    course_levels = [r[0] for r in frappe.db.sql(course_level_filter_sql, params)]

    if not course_levels:
        if verbose:
            print(f"No active course_levels found"
                  f"{' for batch ' + batch_name if batch_name else ''}.")
        return {"violations": [], "checked": 0, "ok": 0}

    # All LearningUnits across those course levels
    placeholders = ",".join(["%s"] * len(course_levels))
    lus = frappe.db.sql(f"""
        SELECT DISTINCT lul.learning_unit, lul.parent AS course_level,
               lul.week_no, lu.unit_name, lu.difficulty_tier
          FROM "tabLearningUnitList" lul
          JOIN "tabLearningUnit" lu ON lu.name = lul.learning_unit
         WHERE lul.parent IN ({placeholders})
           AND lul.parenttype = 'Course Level'
         ORDER BY lul.parent, lul.week_no, lu.difficulty_tier
    """, tuple(course_levels), as_dict=True)

    violations = []
    ok_count = 0
    for lu in lus:
        # First NON-OPTIONAL content item (optional intros are allowed).
        # Per CR-008 the first item the student actually has to consume
        # must be a VideoClass — optional items at the top don't break
        # the lazy-reset trigger because the gate only flips when the
        # VideoClass SCL fires.
        items = frappe.db.sql("""
            SELECT idx, content_type, content, is_optional
              FROM "tabUnitContentItem"
             WHERE parent = %s AND parenttype = 'LearningUnit'
             ORDER BY idx ASC
        """, (lu.learning_unit,), as_dict=True)

        # First non-optional item — that's what kicks the engagement gate.
        first_required = next((i for i in items if not (i.is_optional or 0)),
                              None)

        if first_required is None:
            violations.append({
                "learning_unit": lu.learning_unit,
                "unit_name": lu.unit_name,
                "course_level": lu.course_level,
                "week_no": lu.week_no,
                "tier": lu.difficulty_tier,
                "violation": "no non-optional content items",
                "first_required_type": None,
            })
        elif first_required.content_type != "VideoClass":
            violations.append({
                "learning_unit": lu.learning_unit,
                "unit_name": lu.unit_name,
                "course_level": lu.course_level,
                "week_no": lu.week_no,
                "tier": lu.difficulty_tier,
                "violation": "first non-optional content is not VideoClass",
                "first_required_type": first_required.content_type,
                "first_required_content": first_required.content,
            })
        else:
            ok_count += 1

    if verbose:
        scope = f"batch={batch_name}" if batch_name else "all active batches"
        print(f"\nvalidate_video_first_invariant ({scope}):")
        print(f"  LearningUnits scanned: {len(lus)}")
        print(f"  ✓ OK (first non-optional is VideoClass): {ok_count}")
        print(f"  ✗ Violations: {len(violations)}")
        for v in violations:
            print(f"    LU={v['learning_unit']} ({v['unit_name']}) "
                  f"course_level={v['course_level']}, week={v['week_no']}, "
                  f"tier={v['tier']}")
            print(f"      → {v['violation']}: first_required_type="
                  f"{v.get('first_required_type')!r}")
        if not violations:
            print("\n  Invariant holds across all active LearningUnits.")

    return {"violations": violations, "checked": len(lus), "ok": ok_count}


# ════════════════════════════════════════════════════════════
# Internal helpers
# ════════════════════════════════════════════════════════════

def _get_active_pe_for_student(student_id):
    """Return the most recently modified active/paused PE for a student."""
    pe_name = frappe.db.get_value(
        "ProgramEnrollment",
        {
            "student": student_id,
            "program_status": ["in", [PROGRAM_ACTIVE, PROGRAM_PAUSED]],
        },
        "name",
        order_by="modified desc",
    )
    if pe_name:
        return frappe.get_doc("ProgramEnrollment", pe_name)
    return None


def _get_latest_pe_for_student(student_id):
    """Return the most recently modified PE for a student, regardless of status."""
    pe_name = frappe.db.get_value(
        "ProgramEnrollment",
        {"student": student_id},
        "name",
        order_by="modified desc",
    )
    if pe_name:
        return frappe.get_doc("ProgramEnrollment", pe_name)
    return None


def _coerce_bool(value):
    """Coerce API form-string booleans while preserving existing bool callers."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _snapshot(pe):
    """Return the small subset of PE fields we care about for before/after."""
    return {
        "resolved_flow_state":      pe.resolved_flow_state,
        "journey_label":            pe.journey_label,
        "program_status":           pe.program_status,
        "current_week":             pe.current_week,
        "current_path":             pe.current_path,
        "submission_count":         pe.submission_count,
        "current_escalation_step":  pe.current_escalation_step,
        "delivery_failure_count":   pe.delivery_failure_count,
        "in_grace_window":          pe.in_grace_window,
        "next_action_at":           pe.next_action_at,
        "next_action_type":         pe.next_action_type,
    }
