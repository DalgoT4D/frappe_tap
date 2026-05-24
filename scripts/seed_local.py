"""
Seed script for local development.
Creates the minimal fixtures needed to exercise both submission flows:
  - Flow 1: save_submission (Summer Program)
  - Flow 2: submit_artwork (imgana, legacy)

Run from inside the bench environment:
    cd /path/to/frappe-bench
    python apps/frappe_tap/scripts/seed_local.py

Safe to re-run — all creates are idempotent (skip if already exists).
Prints the IDs you'll need for the test script.
"""

import json

import frappe
from frappe.utils import add_days, today

frappe.init("tap_lms.localhost")
frappe.connect()
frappe.set_user("Administrator")

# ── 1. Batch ────────────────────────────────────────────────
BATCH_NAME1 = "LocalDev"
BATCH_ID = "LOCAL_DEV_001"

if not frappe.db.exists("Batch", {"batch_id": BATCH_ID}):
    batch = frappe.new_doc("Batch")
    batch.name1 = BATCH_NAME1
    batch.batch_id = BATCH_ID
    batch.start_date = today()
    batch.end_date = add_days(today(), 90)
    batch.regist_end_date = add_days(today(), 7)
    batch.active = 1
    batch.program_type = "Summer"
    batch.total_weeks = 8
    batch.grace_window_days = 14
    batch.current_calendar_week = 1
    batch.insert(ignore_permissions=True)
    frappe.db.commit()
    batch_name = batch.name
    print(f"✓ Batch created: {batch_name}")
else:
    batch_name = frappe.db.get_value("Batch", {"batch_id": BATCH_ID}, "name")
    print(f"  Batch already exists: {batch_name}")

# ── 2. Course Vertical ─────────────────────────────────────────────
VERTICAL_LABEL = "Arts"
VERTICAL_NAME = "Visual Arts"
VERTICAL_ID = "ARTS_001"

if not frappe.db.exists("Course Verticals", VERTICAL_LABEL):
    cv = frappe.new_doc("Course Verticals")
    cv.name1 = VERTICAL_NAME
    cv.name2 = VERTICAL_LABEL
    cv.vertical_id = VERTICAL_ID
    cv.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"✓ Course Vertical created: {VERTICAL_LABEL}")

# ── 3. Assignment ───────────────────────────────────────────
ASSIGNMENT_ID = "MockAssign-Basic"

if not frappe.db.exists("Assignment", ASSIGNMENT_ID):
    assignment = frappe.new_doc("Assignment")
    assignment.assignment_name = "MockAssign"
    assignment.difficulty_tier = "Basic"
    assignment.assignment_type = "Practical"
    assignment.activity_type = "Regular"
    assignment.subject = VERTICAL_LABEL
    assignment.description = "Local dev mock assignment. Create a beautiful painting."
    assignment.max_score = "100"

    # Add mock rubrics
    assignment.append(
        "rubric_grades",
        {"grade_value": 4, "grade_description": "Excellent work with great detail."},
    )
    assignment.append(
        "rubric_grades",
        {"grade_value": 2, "grade_description": "Good effort but needs more detail."},
    )

    assignment.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"✓ Assignment created: {assignment.name}")
else:
    # Update existing assignment to ensure fields needed for RAG are present
    assignment = frappe.get_doc("Assignment", ASSIGNMENT_ID)
    assignment.activity_type = "Regular"
    assignment.subject = VERTICAL_LABEL
    assignment.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"  Assignment updated: {ASSIGNMENT_ID}")

# ── 4. Student ──────────────────────────────────────────────
STUDENT_PHONE = "9999900001"
STUDENT_GLIFIC = "LOCAL_GLIFIC_001"
STUDENT_NAME1 = "LocalDevStudent"

existing_student = frappe.db.get_value("Student", {"phone": STUDENT_PHONE}, "name")
if not existing_student:
    student = frappe.new_doc("Student")
    student.name1 = STUDENT_NAME1
    student.phone = STUDENT_PHONE
    student.glific_id = STUDENT_GLIFIC
    student.status = "active"
    student.grade = "8"
    student.archetype = "submitter"
    student.experiment_arm = "default"
    student.insert(ignore_permissions=True)
    frappe.db.commit()
    student_id = student.name
    print(f"✓ Student created: {student_id}")
else:
    student_id = existing_student
    print(f"  Student already exists: {student_id}")

# ── 5. ProgramEnrollment ────────────────────────────────────
PE_STATES = [
    ("normal_content_delivery", "Core", "content_delivered"),
    ("normal_escalation", "Core", "content_delivered"),
    ("remedial_content_delivery", "Remedial", "content_delivered"),
    ("remedial_escalation", "Remedial", "content_delivered"),
    ("grace_waiting", "Core", "grace_window"),
    ("submitted_awaiting_feedback", "Core", "submitted"),
]

pe_ids = {}
for state, path, label in PE_STATES:
    enrollment_key = f"LOCAL-{state[:20]}"
    existing_pe = frappe.db.get_value(
        "ProgramEnrollment",
        {
            "student": student_id,
            "resolved_flow_state": state,
            "program_status": "active",
        },
        "name",
    )
    if not existing_pe:
        pe = frappe.new_doc("ProgramEnrollment")
        pe.enrollment = enrollment_key
        pe.student = student_id
        pe.batch = batch_name
        pe.program_type = "Summer"
        pe.glific_id = STUDENT_GLIFIC
        pe.program_status = "active"
        pe.resolved_flow_state = state
        pe.current_path = path
        pe.current_tier = "Basic"
        pe.archetype = "submitter"
        pe.journey_label = label
        pe.current_week = 1
        pe.submission_count = 0
        pe.insert(ignore_permissions=True)
        frappe.db.commit()
        pe_ids[state] = pe.name
        print(f"✓ ProgramEnrollment created: {pe.name}  [{state}]")
    else:
        pe_ids[state] = existing_pe
        print(f"  ProgramEnrollment already exists: {existing_pe}  [{state}]")

# ── 6. Prompt Templates (rag_service) ───────────────────────
print("\nSeeding Prompt Templates...")


def create_segment(name, seg_type, content):
    if not frappe.db.exists("Prompt Segment", name):
        doc = frappe.new_doc("Prompt Segment")
        doc.segment_name = name
        doc.segment_type = seg_type
        doc.content = content
        doc.is_active = 1
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        return name
    return name


sys_seg = create_segment(
    "Local System",
    "system",
    "You are an expert art teacher. Provide feedback on {assignment_name}.",
)
grad_seg = create_segment(
    "Local Grading", "grading", "Evaluate the following rubrics: {rubric_criteria}"
)
subj_seg = create_segment(
    "Local Subject", "subject", "Assignment Description: {assignment_description}"
)
out_seg = create_segment("Local Output", "output", "Format your response as JSON.")

TEMPLATE_NAME = "Local Image Template"
if not frappe.db.exists("Prompt Template", TEMPLATE_NAME):
    template = frappe.new_doc("Prompt Template")
    template.template_name = TEMPLATE_NAME
    template.assignment_type = "Practical"
    template.course_vertical = VERTICAL_LABEL
    template.media_type = "image"
    template.prompt_type = "both"
    template.activity_type = "Regular"
    template.system_segment = sys_seg
    template.grading_segment = grad_seg
    template.subject_segment = subj_seg
    template.output_segment = out_seg
    template.response_format = json.dumps(
        {
            "rubric_evaluations": [],
            "strengths": [],
            "areas_for_improvement": [],
            "encouragement": "",
            "overall_feedback": "",
            "overall_feedback_translated": "",
            "learning_objectives_feedback": [],
            "final_grade": 0,
        }
    )
    template.is_active = 1
    template.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"✓ Prompt Template created: {TEMPLATE_NAME}")
else:
    print(f"  Prompt Template already exists: {TEMPLATE_NAME}")

# ── 7. API Key (for submit_artwork flow) ────────────────────
API_KEY_VALUE = "local-dev-api-key-001"
API_SECRET_VALUE = "local-secret-key"

# 1. Update the User Profile directly with the public API key identifier
user_doc = frappe.get_doc("User", "Administrator")
if user_doc.api_key != API_KEY_VALUE:
    user_doc.api_key = API_KEY_VALUE
    user_doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"✓ Public API Key bound to User Profile: {API_KEY_VALUE}")
else:
    print(f"  Public API Key already set on User Profile: {API_KEY_VALUE}")

# 2. Force-inject the crypted Secret password block into Frappe's security vault
import frappe.utils.password as frappe_crypt

current_secret = frappe_crypt.get_decrypted_password(
    "User", "Administrator", "api_secret", raise_exception=False
)

if current_secret != API_SECRET_VALUE:
    frappe_crypt.set_encrypted_password(
        "User", "Administrator", API_SECRET_VALUE, "api_secret"
    )
    frappe.db.commit()
    print(f"✓ API Secret encrypted and vaulted securely: {API_SECRET_VALUE}")
else:
    print(f"  API Secret already validated in vault.")

rag_settings = frappe.get_doc("RAG Settings", "RAG Settings")
rag_settings.base_url = "http://tap_lms.localhost:8000"
rag_settings.assignment_context_endpoint = (
    "api/method/tap_lms.imgana.submission.get_assignment_context"
)
rag_settings.student_context_endpoint = (
    "api/method/tap_lms.imgana.submission.get_student_details"
)
rag_settings.enable_caching = 0
rag_settings.api_key = API_KEY_VALUE
rag_settings.save(ignore_permissions=True)

# Securely vault the secret key onto RAG Settings too
frappe_crypt.set_encrypted_password(
    "RAG Settings", "RAG Settings", API_SECRET_VALUE, "api_secret"
)

frappe.db.commit()
frappe.clear_cache()
print("✓ RAG Settings configured")

# ── Summary ─────────────────────────────────────────────────
print("\n=== Seed complete. Use these values in test_submissions.py ===")
print(f"STUDENT_ID      = '{student_id}'")
print(f"STUDENT_PHONE   = '{STUDENT_PHONE}'")
print(f"STUDENT_GLIFIC  = '{STUDENT_GLIFIC}'")
print(f"BATCH_NAME      = '{batch_name}'")
print(f"ASSIGNMENT_ID   = '{ASSIGNMENT_ID}'")
print(f"API_KEY         = '{API_KEY_VALUE}'")
print(f"PE_IDS          = {pe_ids}")
