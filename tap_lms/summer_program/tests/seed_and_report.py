"""
Didi Voice Agent — Comprehensive Test Suite
============================================
Paste this entire file into the bench console.

Creates every student type, then shows a full report per student:
  - Who they are
  - Their current PE state
  - Which situation Didi computes
  - The rendered prompt that would be sent to Vocallabs
  - Whether a call would actually fire (window + rate limit checks)

All students use phone 8950507072 (Dashpreet's test number).
Names are Dash_<type> so they are easy to find in the desk.

Usage:
    # Paste entire file, then:
    seed_all()       # creates all students in Frappe
    show_report()    # prints full state table
    seed_all(); show_report()   # do both at once
"""

import frappe
import random
import string
from frappe.utils import now_datetime, add_to_date

TEST_PHONE = "8950507072"
BATCH = "Test Batch 2026-BT0003"

# ── Student definitions ────────────────────────────────────────────────────
# Each entry: display_name, school_key, language, grade, PE state dict, glific_context dict

STUDENTS = [

    # N1 — Continuation, Hindi, grade 7 (student_direct), has submitted before
    {
        "name":     "Dash_N1_Hindi",
        "school":   "School A Delhi",
        "lang":     "Hindi",
        "grade":    "7",
        "desc":     "N1 Continuation — submitted 2x, no streak, slipping week 2",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 2,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "help_note_b",
            "resolved_flow_state": "normal_escalation",
            "current_week": 2,
            "current_expected_submission_type": "word_text_voice",
            "program_status": "active",
            "total_points": 40,
            "special_gems": 0,
        },
        "glific": {
            "sp_submission_link": "",
            "last_inbound_message": "haan karta hoon",
        },
    },

    # N1 — Continuation, English, grade 8
    {
        "name":     "Dash_N1_English",
        "school":   "School B Mumbai",
        "lang":     "English",
        "grade":    "8",
        "desc":     "N1 Continuation — English language, grade 8, help_note_a stage",
        "pe": {
            "archetype": "irregular_submitter",
            "submission_count": 1,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "help_note_a",
            "resolved_flow_state": "normal_escalation",
            "current_week": 2,
            "current_expected_submission_type": "word_text_voice",
            "program_status": "active",
            "total_points": 20,
            "special_gems": 0,
        },
        "glific": {
            "sp_submission_link": "",
            "last_inbound_message": "",
        },
    },

    # N2 — Streak Protection, Hindi, active 3-week streak
    {
        "name":     "Dash_N2_Streak",
        "school":   "School A Delhi",
        "lang":     "Hindi",
        "grade":    "9",
        "desc":     "N2 Streak Protection — 3 week streak at risk, voice_note stage",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 3,
            "current_streak": 3,
            "weekly_submission_done": 0,
            "current_escalation_type": "voice_note",
            "resolved_flow_state": "normal_escalation",
            "current_week": 3,
            "current_expected_submission_type": "summary_text_voice",
            "program_status": "active",
            "total_points": 60,
            "special_gems": 0,
        },
        "glific": {
            "sp_submission_link": "",
            "last_inbound_message": "kal karta hoon",
        },
    },

    # N2 — Streak Protection, Punjabi, long streak
    {
        "name":     "Dash_N2_Punjabi_LongStreak",
        "school":   "School C Moga",
        "lang":     "Punjabi",
        "grade":    "8",
        "desc":     "N2 Streak — Punjabi, 5-week streak, voice_note",
        "pe": {
            "archetype": "irregular_submitter",
            "submission_count": 5,
            "current_streak": 5,
            "weekly_submission_done": 0,
            "current_escalation_type": "voice_note",
            "resolved_flow_state": "normal_escalation",
            "current_week": 4,
            "current_expected_submission_type": "photo_video_artefact",
            "program_status": "active",
            "total_points": 100,
            "special_gems": 1,
        },
        "glific": {
            "sp_submission_link": "ਸਮਝ ਨਹੀਂ ਆਇਆ",
            "last_inbound_message": "ਹਾਂ ਕਰਦਾ ਹਾਂ",
        },
    },

    # N4 — Real Blocker, week 3, image submission, Hindi
    {
        "name":     "Dash_N4_ImageBlocker",
        "school":   "School A Delhi",
        "lang":     "Hindi",
        "grade":    "7",
        "desc":     "N4 Real Blocker — week 3, image submission, reported problem",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 2,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "help_note_a",
            "resolved_flow_state": "normal_escalation",
            "current_week": 3,
            "current_expected_submission_type": "image",
            "program_status": "active",
            "total_points": 40,
            "special_gems": 0,
        },
        "glific": {
            "sp_submission_link": "photo upload nahi ho rahi",
            "last_inbound_message": "try kiya par nahi hua",
            "last_assignment_name": "Make a Homemade Lava Lamp",
        },
    },

    # N4 — Real Blocker, week 4, summary, Marathi
    {
        "name":     "Dash_N4_Marathi_Summary",
        "school":   "School D Pune",
        "lang":     "Marathi",
        "grade":    "8",
        "desc":     "N4 Real Blocker — Marathi, week 4, summary_text_voice, link issue",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 3,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "help_note_b",
            "resolved_flow_state": "normal_escalation",
            "current_week": 4,
            "current_expected_submission_type": "summary_text_voice",
            "program_status": "active",
            "total_points": 60,
            "special_gems": 0,
        },
        "glific": {
            "sp_submission_link": "link uघdत nahi",
            "last_inbound_message": "internet nahi ahe",
        },
    },

    # N5 — First Submission, dormant, grade 5 (parent_facilitated), emoji
    {
        "name":     "Dash_N5_Dormant_Young",
        "school":   "School A Delhi",
        "lang":     "Hindi",
        "grade":    "5",
        "desc":     "N5 First Submission — dormant, grade 5, parent_facilitated, never engaged",
        "pe": {
            "archetype": "dormant",
            "submission_count": 0,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "parent_call",
            "resolved_flow_state": "normal_escalation",
            "current_week": 1,
            "current_expected_submission_type": "emoji",
            "program_status": "active",
            "total_points": 0,
            "special_gems": 0,
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },

    # N5 — First Submission, fence_sitter, grade 9 (student_direct)
    {
        "name":     "Dash_N5_FenceSitter_Teen",
        "school":   "School B Mumbai",
        "lang":     "Hindi",
        "grade":    "9",
        "desc":     "N5 First Submission — fence_sitter, grade 9, student_direct",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 0,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "parent_call",
            "resolved_flow_state": "normal_escalation",
            "current_week": 1,
            "current_expected_submission_type": "emoji",
            "program_status": "active",
            "total_points": 0,
            "special_gems": 0,
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },

    # N5 — No language set (tests fallback handling)
    {
        "name":     "Dash_N5_NoLanguage",
        "school":   "School A Delhi",
        "lang":     None,
        "grade":    "6",
        "desc":     "EDGE: No language set — tests fallback when language is null",
        "pe": {
            "archetype": "dormant",
            "submission_count": 0,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "parent_call",
            "resolved_flow_state": "normal_escalation",
            "current_week": 1,
            "current_expected_submission_type": "emoji",
            "program_status": "active",
            "total_points": 0,
            "special_gems": 0,
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },

    # N6 — Grace Deadline, urgent (< 48 hours), Hindi
    {
        "name":     "Dash_N6_Grace_Urgent",
        "school":   "School A Delhi",
        "lang":     "Hindi",
        "grade":    "6",
        "desc":     "N6 Grace Deadline — deadline in 20 hours, most urgent",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 0,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "parent_call",
            "resolved_flow_state": "grace_waiting",
            "current_week": 1,
            "current_expected_submission_type": "emoji",
            "program_status": "active",
            "total_points": 0,
            "special_gems": 0,
            "grace_window_end_at": str(add_to_date(now_datetime(), hours=20)),
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },

    # N6 — Grace Deadline, 7 days, Punjabi
    {
        "name":     "Dash_N6_Grace_Week",
        "school":   "School C Moga",
        "lang":     "Punjabi",
        "grade":    "8",
        "desc":     "N6 Grace Deadline — Punjabi, deadline in 7 days",
        "pe": {
            "archetype": "dormant",
            "submission_count": 0,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "parent_call",
            "resolved_flow_state": "grace_waiting",
            "current_week": 1,
            "current_expected_submission_type": "emoji",
            "program_status": "active",
            "total_points": 0,
            "special_gems": 0,
            "grace_window_end_at": str(add_to_date(now_datetime(), days=7)),
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },

    # N7 — Reinitiation, program paused
    {
        "name":     "Dash_N7_Paused",
        "school":   "School A Delhi",
        "lang":     "Hindi",
        "grade":    "7",
        "desc":     "N7 Reinitiation — program paused, needs keyword to restart",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 1,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "parent_call",
            "resolved_flow_state": "normal_escalation",
            "current_week": 2,
            "current_expected_submission_type": "word_text_voice",
            "program_status": "paused",
            "total_points": 20,
            "special_gems": 0,
        },
        "glific": {
            "sp_submission_link": "",
            "last_inbound_message": "kab shuru hoga",
        },
    },

    # N8 — Celebration, submitter, special_gems=2
    {
        "name":     "Dash_N8_Celebration",
        "school":   "School B Mumbai",
        "lang":     "Hindi",
        "grade":    "9",
        "desc":     "N8 Celebration — submitter, 4 weeks done, special_gems=2",
        "pe": {
            "archetype": "submitter",
            "submission_count": 4,
            "current_streak": 4,
            "weekly_submission_done": 1,
            "current_escalation_type": "parent_call",
            "resolved_flow_state": "normal_escalation",
            "current_week": 4,
            "current_expected_submission_type": "photo_video_artefact",
            "program_status": "active",
            "total_points": 120,
            "special_gems": 2,
        },
        "glific": {
            "sp_submission_link": "",
            "last_inbound_message": "bahut maza aaya",
        },
    },

    # EDGE — Already submitted this week (unknown, call should not fire)
    {
        "name":     "Dash_Edge_AlreadyDone",
        "school":   "School A Delhi",
        "lang":     "Hindi",
        "grade":    "7",
        "desc":     "EDGE: Already submitted this week — should get 'unknown' situation",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 3,
            "current_streak": 2,
            "weekly_submission_done": 1,
            "current_escalation_type": "parent_call",
            "resolved_flow_state": "normal_escalation",
            "current_week": 2,
            "current_expected_submission_type": "summary_text_voice",
            "program_status": "active",
            "total_points": 60,
            "special_gems": 0,
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },

    # EDGE — Program dropped (should still compute context but note the status)
    {
        "name":     "Dash_Edge_Dropped",
        "school":   "School A Delhi",
        "lang":     "Hindi",
        "grade":    "6",
        "desc":     "EDGE: Program dropped — context computed but real call would be blocked",
        "pe": {
            "archetype": "dormant",
            "submission_count": 0,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "program_pause",
            "resolved_flow_state": "normal_escalation",
            "current_week": 1,
            "current_expected_submission_type": "emoji",
            "program_status": "dropped",
            "total_points": 0,
            "special_gems": 0,
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },

    # EDGE — Remedial path, fence_sitter, continuation nudge
    {
        "name":     "Dash_Edge_Remedial",
        "school":   "School D Pune",
        "lang":     "Hindi",
        "grade":    "8",
        "desc":     "EDGE: Remedial path — should still compute continuation situation",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 1,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "help_note_b",
            "resolved_flow_state": "remedial_escalation",
            "current_week": 2,
            "current_expected_submission_type": "word_text_voice",
            "program_status": "active",
            "current_path": "Remedial",
            "total_points": 10,
            "special_gems": 0,
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },

    # EDGE — No school linked (missing field)
    {
        "name":     "Dash_Edge_NoSchool",
        "school":   None,
        "lang":     "Hindi",
        "grade":    "7",
        "desc":     "EDGE: No school — tests missing field graceful handling",
        "pe": {
            "archetype": "fence_sitter",
            "submission_count": 1,
            "current_streak": 0,
            "weekly_submission_done": 0,
            "current_escalation_type": "help_note_a",
            "resolved_flow_state": "normal_escalation",
            "current_week": 2,
            "current_expected_submission_type": "word_text_voice",
            "program_status": "active",
            "total_points": 20,
            "special_gems": 0,
        },
        "glific": {"sp_submission_link": "", "last_inbound_message": ""},
    },
]


# ── School master data ─────────────────────────────────────────────────────
SCHOOLS = {
    "School A Delhi":  {"type": "GOVT",   "district": "Delhi"},
    "School B Mumbai": {"type": "PMC",    "district": "Mumbai"},
    "School C Moga":   {"type": "GOVT",   "district": "Moga"},
    "School D Pune":   {"type": "GOVT. Aided", "district": "Pune"},
}

DISTRICTS = ["Delhi", "Mumbai", "Moga", "Pune"]


# ── Seed helpers ───────────────────────────────────────────────────────────

def _ensure_language(lang_name):
    if not lang_name:
        return
    if not frappe.db.exists("TAP Language", lang_name):
        d = frappe.new_doc("TAP Language")
        d.language_name = lang_name
        d.language_code = lang_name[:2].lower()
        d.insert(ignore_permissions=True)


def _ensure_district(name):
    if not frappe.db.exists("District", name):
        d = frappe.new_doc("District")
        d.district_name = name
        d.insert(ignore_permissions=True, ignore_links=True)


def _ensure_school(name, stype, district):
    if not frappe.db.get_value("School", {"name1": name}, "name"):
        frappe.db.sql("""
            INSERT INTO "tabSchool" (name, name1, type, district, keyword, creation, modified, modified_by, owner, docstatus)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW(), 'Administrator', 'Administrator', 0)
            ON CONFLICT (name) DO NOTHING
        """, (name.replace(" ", "-")[:20], name, stype, district, name[:6].upper()))
    return frappe.db.get_value("School", {"name1": name}, "name") or name.replace(" ", "-")[:20]


def _ensure_program():
    if not frappe.db.exists("Program", "Summer 2026"):
        p = frappe.new_doc("Program")
        p.program = "Summer 2026"
        p.insert(ignore_permissions=True)


def _make_student(s):
    existing = frappe.db.get_value("Student", {"name1": s["name"]}, "name")
    if existing:
        return existing

    st = frappe.new_doc("Student")
    st.name1 = s["name"]
    st.phone = TEST_PHONE
    st.grade = s["grade"]
    if s.get("lang"):
        st.language = s["lang"]
    if s.get("school"):
        st.school_id = _ensure_school(s["school"], SCHOOLS[s["school"]]["type"], SCHOOLS[s["school"]]["district"])
    st.status = "active"
    st.insert(ignore_permissions=True)
    return st.name


def _make_pe(st_name, pe_data):
    existing = frappe.db.get_value("ProgramEnrollment", {"student": st_name}, "name")
    if existing:
        return existing

    pe = frappe.new_doc("ProgramEnrollment")
    pe.enrollment = "ER-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    pe.student = st_name
    pe.batch = BATCH
    pe.program_type = "Summer"
    pe.glific_id = "DASH-TEST-" + st_name[-6:]
    pe.language = pe_data.get("language", "Hindi")
    pe.current_path = pe_data.get("current_path", "Core")
    pe.current_tier = "Basic"
    pe.experiment_arm = "arm_a"
    pe.journey_label = "content_delivered"
    pe.total_call_count = 0
    pe.weekly_call_count = 0

    for k, v in pe_data.items():
        if k not in ("language", "current_path"):
            setattr(pe, k, v)

    pe.insert(ignore_permissions=True)
    return pe.name


def _make_glific(st_name, g):
    if frappe.db.get_value("StudentGlificContext", {"student": st_name}, "name"):
        return
    doc = frappe.new_doc("StudentGlificContext")
    doc.student = st_name
    doc.last_synced_at = now_datetime()
    doc.sp_submission_link = g.get("sp_submission_link", "")
    doc.last_inbound_message = g.get("last_inbound_message", "")
    doc.last_assignment_name = g.get("last_assignment_name", "")
    doc.last_flow_incomplete = g.get("last_flow_incomplete", 0)
    doc.insert(ignore_permissions=True)


def seed_all():
    print("Seeding prerequisites...")

    for d in DISTRICTS:
        _ensure_district(d)
    for lang in ["Hindi", "English", "Punjabi", "Marathi"]:
        _ensure_language(lang)
    _ensure_program()
    frappe.db.commit()

    print(f"Creating {len(STUDENTS)} test students (phone: {TEST_PHONE})...\n")
    created = []

    for s in STUDENTS:
        pe_data = dict(s["pe"])
        if s.get("lang"):
            pe_data["language"] = s["lang"]

        st_name = _make_student(s)
        pe_name = _make_pe(st_name, pe_data)
        _make_glific(st_name, s.get("glific", {}))
        created.append((s["name"], st_name, pe_name))
        print(f"  {s['name']:<35} → {st_name} / {pe_name}")

    frappe.db.commit()
    print(f"\nDone. {len(created)} students ready.\n")
    print("Run show_report() to see full state + prompts.")


# ── Report ─────────────────────────────────────────────────────────────────

def show_report():
    from tap_lms.summer_program.voice_context import build_student_context, check_call_window, check_rate_limit
    from tap_lms.summer_program.vocallabs import (
        _get_voice_agent_settings, _resolve_parent_call_config,
        _resolve_agent_id, _render_status_template, _resolve_welcome_greeting,
    )

    settings = _get_voice_agent_settings()

    HDR = (
        f"\n{'='*130}\n"
        f"  DIDI VOICE AGENT — FULL STATE REPORT\n"
        f"  Settings: enabled={settings.enabled} | "
        f"window={settings.call_window_start or 'any'}-{settings.call_window_end or 'any'} | "
        f"max/week={settings.max_calls_per_student_per_week or 'unlimited'} | "
        f"cooldown={settings.call_cooldown_hours or 0}h\n"
        f"{'='*130}"
    )
    print(HDR)

    for s in STUDENTS:
        st_name = frappe.db.get_value("Student", {"name1": s["name"]}, "name")
        if not st_name:
            print(f"\n  SKIP {s['name']} — not in DB. Run seed_all() first.")
            continue

        pe_name = frappe.db.get_value("ProgramEnrollment", {"student": st_name}, "name")
        if not pe_name:
            print(f"\n  SKIP {s['name']} — no enrollment found.")
            continue

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        student = frappe.get_doc("Student", st_name)

        # Context
        ctx = build_student_context(pe, student)

        # Config + prompt
        config = _resolve_parent_call_config(pe, pe.current_week or 1, settings)
        agent_id = _resolve_agent_id(settings, pe.language or "")
        greeting = _resolve_welcome_greeting(pe)

        if config and config.status_template:
            step = {"escalation_order": pe.current_escalation_type, "escalation_type": pe.current_escalation_type,
                    "hours_after_previous": 48, "points_awarded": 0}
            rendered = _render_status_template(config.status_template, pe, student, step)
        else:
            rendered = "(no ParentCallConfig found — check VoiceNudgeConfig.default_template_config)"

        # Would call fire?
        call_blocked_reason = []
        if not settings.enabled:
            call_blocked_reason.append("settings.enabled=0")
        if greeting == "None":
            call_blocked_reason.append("arm_b dormant skip")
        if not check_call_window(settings):
            call_blocked_reason.append("outside call window")
        if not check_rate_limit(pe, settings):
            call_blocked_reason.append("rate limited")
        if not config:
            call_blocked_reason.append("no ParentCallConfig")
        if not agent_id:
            call_blocked_reason.append("no Vocallabs agent for language")
        if pe.program_status == "dropped":
            call_blocked_reason.append("program dropped")

        call_fires = len(call_blocked_reason) == 0

        print(f"\n{'─'*130}")
        print(f"  STUDENT    : {s['name']} ({st_name})")
        print(f"  DESC       : {s['desc']}")
        print(f"  SCHOOL     : {student.school_id or '(none)'} | LANG: {student.language or '(none)'} | GRADE: {student.grade} → {ctx['grade_group']}")
        print(f"")
        print(f"  PE STATE   : archetype={pe.archetype} | week={pe.current_week} | path={pe.current_path}")
        print(f"               submission_count={pe.submission_count} | streak={pe.current_streak} | weekly_done={pe.weekly_submission_done}")
        print(f"               escalation={pe.current_escalation_type} | flow_state={pe.resolved_flow_state} | program={pe.program_status}")
        print(f"               submission_type={pe.current_expected_submission_type} | points={pe.total_points} | gems={pe.special_gems}")
        if pe.grace_window_end_at:
            print(f"               grace_deadline={ctx['grace_deadline']}")
        print(f"")
        print(f"  GLIFIC     : problem_reported='{ctx['last_problem_reported']}' | last_message='{ctx['last_message']}'")
        print(f"")
        print(f"  SITUATION  : ▶  {ctx['situation'].upper()}")
        print(f"  AGENT      : {agent_id or '(no agent — language not mapped)'}")
        print(f"  GREETING   : {greeting}")
        print(f"")
        print(f"  CONTEXT    :")
        for k, v in ctx.items():
            if v:
                print(f"    {k:<25} = {v}")
        print(f"")
        print(f"  PROMPT     : {rendered[:200]}{'...' if len(rendered) > 200 else ''}")
        print(f"")
        if call_fires:
            print(f"  CALL FIRES : ✅  YES — would call {TEST_PHONE}")
        else:
            print(f"  CALL FIRES : ❌  NO  — blocked: {', '.join(call_blocked_reason)}")

    print(f"\n{'='*130}")
    print(f"  END OF REPORT — {len(STUDENTS)} students shown")
    print(f"{'='*130}\n")
    print("To place a test call for one student:")
    print("  from tap_lms.summer_program.vocallabs import initiate_parent_call")
    print("  pe_name = frappe.db.get_value('ProgramEnrollment', {'student': frappe.db.get_value('Student', {'name1': 'Dash_N2_Streak'}, 'name')}, 'name')")
    print("  initiate_parent_call(pe_name, {'escalation_order': 2, 'escalation_type': 'voice_note', 'hours_after_previous': 48, 'points_awarded': 0})")


print("Loaded. Run:  seed_all()        — creates all test students")
print("             show_report()      — shows full state + prompts per student")
print("             seed_all(); show_report()  — do both")
