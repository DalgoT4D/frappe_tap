"""
Shared test factories for Summer Program tests.

Every SP test that needs a Batch / Student / ProgramEnrollment should import
the relevant factory from this module rather than inlining its own. The
recurring bug we're trying to prevent: each test file inventing its own
`_ensure_batch()` and forgetting to populate a mandatory field that the
doctype gained AFTER the test was written. Latest occurrence: 2026-05-19
test_sibling_skip.py missing `regist_start_date` / `regist_end_date` while
two sister test files had them.

When the Batch / Student / ProgramEnrollment doctype gains a new mandatory
field, update the factory here once and every test inherits the fix.

Usage in a test file:

    from tap_lms.summer_program.tests.factories import (
        make_batch, make_student, make_active_pe,
    )

    class TestX(FrappeTestCase):
        @classmethod
        def setUpClass(cls):
            super().setUpClass()
            cls.batch = make_batch("XTestBatch", batch_id="XT01")
            cls.student = make_student(
                suffix="X01",
                phone_prefix="+9999900",
                glific_id="glific-x-01",
            )

Each factory is idempotent — re-running with the same inputs returns the
existing doc. That's important under FrappeTestCase, which rolls back
the txn at end of each test; data created in `setUpClass` survives across
the class but is rolled back after the class finishes.
"""
import frappe


def make_batch(
    label,
    batch_id,
    start_date="2026-01-01",
    end_date="2026-04-30",
    regist_start_date="2025-12-01",
    regist_end_date="2025-12-31",
    total_weeks=12,
    current_calendar_week=1,
    grace_window_days=14,
    program_type="Summer",
    program="TestProgram",
):
    """Idempotent: create or return a Batch with name1=label.

    Populates every field currently required by the Batch doctype. If the
    doctype gains a new mandatory field, add it here once and all tests
    that import this factory inherit the fix.

    Args:
        label: unique-per-test display name (used as Batch.name1 lookup key)
        batch_id: short friendly identifier (Batch.batch_id, the Glific-facing
                  value distinct from Batch.name which is the doc name)
    """
    existing = frappe.get_value("Batch", {"name1": label}, "name")
    if existing:
        return existing
    # Batch.program became mandatory in commit bf24daf (Link to Program).
    # Ensure a Program exists so the factory keeps populating EVERY required
    # Batch field (L-037).
    if not frappe.db.exists("Program", program):
        prog = frappe.new_doc("Program")
        prog.program = program
        prog.insert(ignore_permissions=True)
    batch = frappe.new_doc("Batch")
    batch.name1 = label
    batch.start_date = start_date
    batch.end_date = end_date
    # CRITICAL — these became mandatory mid-2026; missing them throws
    # MandatoryError at setUpClass time. Three test files broke before
    # this factory was extracted (2026-05-19).
    batch.regist_start_date = regist_start_date
    batch.regist_end_date = regist_end_date
    batch.batch_id = batch_id
    batch.program = program
    batch.program_type = program_type
    batch.total_weeks = total_weeks
    batch.current_calendar_week = current_calendar_week
    batch.grace_window_days = grace_window_days
    batch.insert(ignore_permissions=True)
    return batch.name


def make_student(
    suffix,
    phone_prefix="+9999000",
    glific_id="",
    name1=None,
    archetype="fence_sitter",
    experiment_arm="arm_a",
    language="English",
):
    """Idempotent: create or return a Student with phone = phone_prefix+suffix.

    Re-asserts `glific_id` on every call so cross-test contamination doesn't
    leave a stale value pointing at the wrong Glific contact. Suffix must
    be unique per (phone_prefix, test class) to avoid lookup collisions.

    Args:
        suffix: short string appended to phone_prefix to form the unique phone
        phone_prefix: pass a unique prefix per test file to keep students
                      across test files in disjoint phone spaces (avoid
                      Glific cross-wiring during dev). Convention so far:
                      test_dev_tools="+9999500", test_sibling_skip="+9999600",
                      test_enrollment_contact_field_push="+9999400".
        glific_id: pass "" if the test doesn't need a Glific contact.
        archetype: defaults to "fence_sitter" because the SP enrollment chunk
                   rejects Students without an archetype — every active test
                   needs one.
    """
    phone = f"{phone_prefix}{suffix}"
    existing = frappe.get_value("Student", {"phone": phone}, "name")
    if existing:
        # Always re-set glific_id so a previous test's value doesn't leak.
        frappe.db.set_value("Student", existing, {"glific_id": glific_id})
        return existing
    s = frappe.new_doc("Student")
    s.name1 = name1 or f"TestStudent{suffix}"
    s.phone = phone
    s.glific_id = glific_id
    s.archetype = archetype
    s.experiment_arm = experiment_arm
    s.language = language
    s.insert(ignore_permissions=True)
    return s.name


def make_active_pe(student_id, batch_name, glific_id="", enrollment_suffix="test"):
    """Idempotent: create or return an active ProgramEnrollment for
    (student_id, batch_name). Used by tests that need a pre-existing PE in
    a sibling-cluster setup, dev-tools reset target, etc.

    Skips the full _process_pe_chunk logic — directly inserts a minimal
    active PE so the test stays focused on what it's actually exercising.

    Returns the PE doc name.
    """
    from tap_lms.summer_program.constants import (
        STATE_NORMAL_CONTENT, LABEL_ENROLLED, PROGRAM_ACTIVE, PATH_CORE,
    )
    existing = frappe.db.get_value(
        "ProgramEnrollment",
        {
            "student": student_id,
            "batch": batch_name,
            "program_status": ["in", ["active", "paused"]],
        },
        "name",
    )
    if existing:
        if glific_id:
            frappe.db.set_value("ProgramEnrollment", existing,
                                {"glific_id": glific_id})
        return existing
    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = f"{student_id}-{batch_name}-{enrollment_suffix}"
    pe.student = student_id
    pe.batch = batch_name
    pe.program_type = "Summer"
    pe.glific_id = glific_id or ""
    pe.archetype = "fence_sitter"
    pe.experiment_arm = "arm_a"
    pe.current_path = PATH_CORE
    pe.current_tier = "Basic"
    pe.journey_label = LABEL_ENROLLED
    pe.program_status = PROGRAM_ACTIVE
    pe.resolved_flow_state = STATE_NORMAL_CONTENT
    pe.current_week = 1
    pe.insert(ignore_permissions=True)
    return pe.name
