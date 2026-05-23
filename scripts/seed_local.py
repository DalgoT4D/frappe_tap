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

import frappe
from frappe.utils import today, add_days

frappe.init("tap_lms.localhost") 
frappe.connect()
frappe.set_user("Administrator")

# ── 1. Batch ────────────────────────────────────────────────
BATCH_NAME1 = "LocalDev"
BATCH_ID    = "LOCAL_DEV_001"

if not frappe.db.exists("Batch", {"batch_id": BATCH_ID}):
    batch = frappe.new_doc("Batch")
    batch.name1             = BATCH_NAME1
    batch.batch_id          = BATCH_ID
    batch.start_date        = today()
    batch.end_date          = add_days(today(), 90)
    batch.regist_end_date   = add_days(today(), 7)
    batch.active            = 1
    batch.program_type      = "Summer"
    batch.total_weeks       = 8
    batch.grace_window_days = 14
    batch.current_calendar_week = 1
    batch.insert(ignore_permissions=True)
    frappe.db.commit()
    batch_name = batch.name
    print(f"✓ Batch created: {batch_name}")
else:
    batch_name = frappe.db.get_value("Batch", {"batch_id": BATCH_ID}, "name")
    print(f"  Batch already exists: {batch_name}")

# ── 2. Assignment ───────────────────────────────────────────
ASSIGNMENT_ID = "MockAssign-Basic"

if not frappe.db.exists("Assignment", ASSIGNMENT_ID):
    assignment = frappe.new_doc("Assignment")
    assignment.assignment_name = "MockAssign"
    assignment.difficulty_tier = "Basic"
    assignment.assignment_type = "Practical"
    assignment.description     = "Local dev mock assignment"
    assignment.max_score       = "100"
    assignment.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"✓ Assignment created: {assignment.name}")
else:
    print(f"  Assignment already exists: {ASSIGNMENT_ID}")

# ── 3. Student ──────────────────────────────────────────────
STUDENT_PHONE   = "9999900001"
STUDENT_GLIFIC  = "LOCAL_GLIFIC_001"
STUDENT_NAME1   = "LocalDevStudent"

existing_student = frappe.db.get_value("Student", {"phone": STUDENT_PHONE}, "name")
if not existing_student:
    student = frappe.new_doc("Student")
    student.name1    = STUDENT_NAME1
    student.phone    = STUDENT_PHONE
    student.glific_id = STUDENT_GLIFIC
    student.status   = "active"
    student.grade    = "8"
    student.archetype = "submitter"
    student.experiment_arm = "default"
    student.insert(ignore_permissions=True)
    frappe.db.commit()
    student_id = student.name
    print(f"✓ Student created: {student_id}")
else:
    student_id = existing_student
    print(f"  Student already exists: {student_id}")

# ── 4. ProgramEnrollment ────────────────────────────────────
# One PE per state that save_submission handles, so we can test each path.
# States: normal_content_delivery, normal_escalation, remedial_content_delivery,
#         remedial_escalation, grace_waiting, submitted_awaiting_feedback (→ duplicate)

PE_STATES = [
    ("normal_content_delivery",   "Core",     "content_delivered"),
    ("normal_escalation",         "Core",     "content_delivered"),
    ("remedial_content_delivery", "Remedial", "content_delivered"),
    ("remedial_escalation",       "Remedial", "content_delivered"),
    ("grace_waiting",             "Core",     "grace_window"),
    ("submitted_awaiting_feedback", "Core",   "submitted"),
]

pe_ids = {}
for state, path, label in PE_STATES:
    enrollment_key = f"LOCAL-{state[:20]}"
    existing_pe = frappe.db.get_value(
        "ProgramEnrollment",
        {"student": student_id, "resolved_flow_state": state, "program_status": "active"},
        "name"
    )
    if not existing_pe:
        pe = frappe.new_doc("ProgramEnrollment")
        pe.enrollment           = enrollment_key
        pe.student              = student_id
        pe.batch                = batch_name
        pe.program_type         = "Summer"
        pe.glific_id            = STUDENT_GLIFIC
        pe.program_status       = "active"
        pe.resolved_flow_state  = state
        pe.current_path         = path
        pe.current_tier         = "Basic"
        pe.archetype            = "submitter"
        pe.journey_label        = label
        pe.current_week         = 1
        pe.submission_count     = 0
        pe.insert(ignore_permissions=True)
        frappe.db.commit()
        pe_ids[state] = pe.name
        print(f"✓ ProgramEnrollment created: {pe.name}  [{state}]")
    else:
        pe_ids[state] = existing_pe
        print(f"  ProgramEnrollment already exists: {existing_pe}  [{state}]")

# ── 5. API Key (for submit_artwork flow) ────────────────────
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
current_secret = frappe_crypt.get_decrypted_password("User", "Administrator", "api_secret", raise_exception=False)

if current_secret != API_SECRET_VALUE:
    frappe_crypt.set_encrypted_password("User", "Administrator", API_SECRET_VALUE, "api_secret")
    frappe.db.commit()
    print(f"✓ API Secret encrypted and vaulted securely: {API_SECRET_VALUE}")
else:
    print(f"  API Secret already validated in vault.")
    
rag_settings = frappe.get_doc("RAG Settings", "RAG Settings")
rag_settings.base_url = "http://tap_lms.localhost:8000"
rag_settings.assignment_context_endpoint = "api/method/tap_lms.imgana.submission.get_assignment_context"
rag_settings.student_context_endpoint = "api/method/tap_lms.imgana.submission.get_student_details"
rag_settings.enable_caching = 0
rag_settings.api_key = API_KEY_VALUE
rag_settings.save(ignore_permissions=True)

# Securely vault the secret key onto RAG Settings too
frappe_crypt.set_encrypted_password("RAG Settings", "RAG Settings", API_SECRET_VALUE, "api_secret")

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
