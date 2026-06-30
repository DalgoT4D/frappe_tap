"""
Seed script for local development — rag_service site.

rag_service runs as its own Frappe site with its own DB (mirroring dev/prod),
so it needs its own fixtures separate from tap_lms's. This creates the
Prompt Segment/Prompt Template rag_service needs to find an active template
for the "MockAssign-Basic" assignment seeded by scripts/seed_local.py
(course_vertical="Arts", activity_type="Regular", media_type="image",
prompt_type="both"), plus RAG Settings pointing back at the tap_lms site.

Run scripts/seed_local.py FIRST (against the tap_lms site, to create the
Administrator API key this script reuses), then run this one against the
rag_service site:

    cd /path/to/frappe-bench
    RAG_SITE_NAME=rag.localhost python apps/frappe_tap/scripts/seed_local_rag.py

Safe to re-run — all creates are idempotent (skip if already exists).
"""

import json
import os

import frappe
import frappe.utils.password as frappe_crypt

RAG_SITE_NAME = os.environ.get("RAG_SITE_NAME", "rag.localhost")
TAP_LMS_SITE_NAME = os.environ.get("SITE_NAME", "tap_lms.localhost")
TAP_LMS_WEB_PORT = os.environ.get("WEB_PORT", "8000")

frappe.init(RAG_SITE_NAME)
frappe.connect()
frappe.set_user("Administrator")

# Must match the Administrator Authorization-header credential seeded by
# seed_local.py on the tap_lms site — this is what rag_service sends as the
# Authorization header when it calls tap_lms's get_assignment_context /
# get_student_details. (Distinct from LOCAL_API_KEY, which is the separate
# "api_key" body field checked by the submission endpoint.)
API_KEY_VALUE = os.environ.get("AUTH_KEY", os.environ.get("LOCAL_API_KEY", "local-dev-api-key-001"))
API_SECRET_VALUE = os.environ.get("AUTH_SECRET", os.environ.get("LOCAL_API_SECRET", "local-secret-key"))

# Must match the values used in scripts/seed_local.py's Assignment fixture.
VERTICAL_LABEL = "Arts"

# ── Prompt Segments + Prompt Template ────────────────────────


def create_segment(name, seg_type, content):
    if not frappe.db.exists("Prompt Segment", name):
        doc = frappe.new_doc("Prompt Segment")
        doc.segment_name = name
        doc.segment_type = seg_type
        doc.content = content
        doc.is_active = 1
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        print(f"✓ Prompt Segment created: {name}")
        return name
    print(f"  Prompt Segment already exists: {name}")
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

# ── RAG Settings → points back at the tap_lms site over HTTP ─────────────────
rag_settings = frappe.get_doc("RAG Settings", "RAG Settings")
rag_settings.base_url = f"http://{TAP_LMS_SITE_NAME}:{TAP_LMS_WEB_PORT}"
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
print("\n=== rag_service seed complete ===")
print(f"RAG_SITE_NAME   = '{RAG_SITE_NAME}'")
print(f"TEMPLATE_NAME   = '{TEMPLATE_NAME}'")
print(f"RAG base_url    = '{rag_settings.base_url}'")
