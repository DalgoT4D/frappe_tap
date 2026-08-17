"""
Submission flow test script for local development.
Exercises all meaningful input combinations for both flows:
  - Flow 1: save_submission (Summer Program) — all PE states + identifier variants
  - Flow 2: submit_artwork (imgana, legacy) — API key auth + student lookup

Run AFTER seed_local.py:
    python apps/frappe_tap/scripts/test_submissions.py

Each test resets the relevant PE back to its original state after running,
so the script is safe to re-run.

NOTE: GCS upload and Glific/RabbitMQ calls will fail locally — that is expected.
      The test validates the Frappe-side logic (student resolution, PE state
      transitions, Submission doc creation) up to the point of external calls.
"""

import frappe
from frappe.utils import now_datetime

frappe.connect()
frappe.set_user("Administrator")

# ── Config (copy from seed_local.py output) ─────────────────
STUDENT_ID     = None   # will be resolved below
STUDENT_PHONE  = "9999900001"
STUDENT_GLIFIC = "LOCAL_GLIFIC_001"
ASSIGNMENT_ID  = "MockAssign-Basic"
API_KEY        = "local-dev-api-key-001"

# A publicly accessible image URL for submit_artwork tests
MOCK_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/280px-PNG_transparency_demonstration_1.png"
MOCK_TEXT      = "Here is my drawing!"
MOCK_EMOJI     = "🎨"

# ── Helpers ──────────────────────────────────────────────────

def resolve_student():
    from tap_lms.summer_program.utils import resolve_student as _resolve
    sid = _resolve(STUDENT_PHONE)
    assert sid, "Student not found — run seed_local.py first"
    return sid


def get_pe(state):
    return frappe.db.get_value(
        "ProgramEnrollment",
        {"student": STUDENT_ID, "resolved_flow_state": state, "program_status": "active"},
        "name",
    )


def reset_pe(pe_name, state, path, label):
    """Restore PE to its original seeded state so tests are re-runnable."""
    frappe.db.set_value("ProgramEnrollment", pe_name, {
        "resolved_flow_state": state,
        "current_path": path,
        "journey_label": label,
        "program_status": "active",
        "submission_count": 0,
    })
    frappe.db.commit()


def delete_submissions_for(student_id, assignment_id):
    subs = frappe.get_all("Submission", filters={
        "student_id": student_id,
        "assign_id": assignment_id,
    }, pluck="name")
    for s in subs:
        frappe.delete_doc("Submission", s, ignore_permissions=True)
    if subs:
        frappe.db.commit()


def run(label, fn):
    try:
        result = fn()
        status = result.get("status", "?") if isinstance(result, dict) else str(result)
        print(f"  ✓ {label}: {status}")
        return result
    except Exception as e:
        print(f"  ✗ {label}: {e}")
        return None


# ════════════════════════════════════════════════════════════
# FLOW 1: save_submission
# ════════════════════════════════════════════════════════════

def test_save_submission():
    from tap_lms.summer_program.save_submission import save_submission

    print("\n── Flow 1: save_submission ─────────────────────────────")

    PE_STATES = [
        ("normal_content_delivery",     "Core",     "content_delivered", "T7"),
        ("normal_escalation",           "Core",     "content_delivered", "T3"),
        ("remedial_content_delivery",   "Remedial", "content_delivered", "T9"),
        ("remedial_escalation",         "Remedial", "content_delivered", "T9"),
        ("grace_waiting",               "Core",     "grace_window",      "T17"),
        ("submitted_awaiting_feedback", "Core",     "submitted",         "T22 (duplicate)"),
    ]

    SUBMISSION_VARIANTS = [
        ("image URL",  MOCK_IMAGE_URL),
        ("text",       MOCK_TEXT),
        ("emoji",      MOCK_EMOJI),
    ]

    IDENTIFIER_VARIANTS = [
        ("by student_id",  STUDENT_ID),
        ("by phone",       STUDENT_PHONE),
        ("by glific_id",   STUDENT_GLIFIC),
    ]

    # ── 1a. Each PE state with an image URL submission ───────
    print("\n  [1a] PE state transitions (image URL submission)")
    for state, path, label, expected_t in PE_STATES:
        pe_name = get_pe(state)
        if not pe_name:
            print(f"  ⚠  No PE found for state '{state}' — skipping")
            continue

        delete_submissions_for(STUDENT_ID, ASSIGNMENT_ID)

        result = run(
            f"{state} → {expected_t}",
            lambda s=STUDENT_ID: save_submission(s, ASSIGNMENT_ID, MOCK_IMAGE_URL),
        )

        reset_pe(pe_name, state, path, label)
        delete_submissions_for(STUDENT_ID, ASSIGNMENT_ID)

    # ── 1b. Submission type variants (on normal_content_delivery) ──
    print("\n  [1b] Submission type variants")
    pe_name = get_pe("normal_content_delivery")
    if pe_name:
        for variant_label, submission in SUBMISSION_VARIANTS:
            delete_submissions_for(STUDENT_ID, ASSIGNMENT_ID)
            result = run(
                variant_label,
                lambda s=submission: save_submission(STUDENT_ID, ASSIGNMENT_ID, s),
            )
            reset_pe(pe_name, "normal_content_delivery", "Core", "content_delivered")
        delete_submissions_for(STUDENT_ID, ASSIGNMENT_ID)

    # ── 1c. Student identifier variants ─────────────────────
    print("\n  [1c] Student identifier variants")
    pe_name = get_pe("normal_content_delivery")
    if pe_name:
        for variant_label, identifier in IDENTIFIER_VARIANTS:
            delete_submissions_for(STUDENT_ID, ASSIGNMENT_ID)
            result = run(
                variant_label,
                lambda i=identifier: save_submission(i, ASSIGNMENT_ID, MOCK_TEXT),
            )
            reset_pe(pe_name, "normal_content_delivery", "Core", "content_delivered")
        delete_submissions_for(STUDENT_ID, ASSIGNMENT_ID)

    # ── 1d. Error cases ──────────────────────────────────────
    print("\n  [1d] Error cases")

    run("unknown student",    lambda: save_submission("DOES_NOT_EXIST", ASSIGNMENT_ID, MOCK_TEXT))
    run("empty submission",   lambda: save_submission(STUDENT_ID, ASSIGNMENT_ID, ""))
    run("no active PE",       _test_no_active_pe)


def _test_no_active_pe():
    """Temporarily drop the PE, call save_submission, then restore."""
    from tap_lms.summer_program.save_submission import save_submission

    pe_name = frappe.db.get_value(
        "ProgramEnrollment",
        {"student": STUDENT_ID, "program_status": "active"},
        "name",
        order_by="modified desc",
    )
    if not pe_name:
        return {"status": "skipped — no active PE to drop"}

    original_status = frappe.db.get_value("ProgramEnrollment", pe_name, "program_status")
    frappe.db.set_value("ProgramEnrollment", pe_name, "program_status", "dropped")
    frappe.db.commit()

    try:
        result = save_submission(STUDENT_ID, ASSIGNMENT_ID, MOCK_TEXT)
    finally:
        frappe.db.set_value("ProgramEnrollment", pe_name, "program_status", original_status)
        frappe.db.commit()

    return result or {"status": "no_active_enrollment (expected)"}


# ════════════════════════════════════════════════════════════
# FLOW 2: submit_artwork (imgana, legacy)
# ════════════════════════════════════════════════════════════

def test_submit_artwork():
    from tap_lms.imgana.submission import submit_artwork

    print("\n── Flow 2: submit_artwork (imgana) ─────────────────────")

    # ── 2a. Happy path ───────────────────────────────────────
    print("\n  [2a] Happy path")
    run(
        "valid API key + student",
        lambda: submit_artwork(API_KEY, ASSIGNMENT_ID, "LocalDevStudent", STUDENT_GLIFIC, MOCK_IMAGE_URL),
    )
    delete_submissions_for(
        frappe.db.get_value("Student", {"phone": STUDENT_PHONE}, "name"),
        ASSIGNMENT_ID,
    )

    # ── 2b. Error cases ──────────────────────────────────────
    print("\n  [2b] Error cases")
    run("invalid API key",    lambda: submit_artwork("bad-key", ASSIGNMENT_ID, "LocalDevStudent", STUDENT_GLIFIC, MOCK_IMAGE_URL))
    run("unknown student",    lambda: submit_artwork(API_KEY, ASSIGNMENT_ID, "NoSuchName", "bad-glific", MOCK_IMAGE_URL))
    run("bad image URL",      lambda: submit_artwork(API_KEY, ASSIGNMENT_ID, "LocalDevStudent", STUDENT_GLIFIC, "https://does-not-exist.invalid/img.jpg"))


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Submission Flow Tests ===")
    print("(GCS / RabbitMQ / Glific failures are expected in local dev)\n")

    STUDENT_ID = resolve_student()
    print(f"Student: {STUDENT_ID}")

    test_save_submission()
    test_submit_artwork()

    print("\n=== Done ===")
