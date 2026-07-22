"""
Didi Voice Agent — Initial seed migration.

What this patch does
--------------------
1. Inserts 8 VoiceNudgeConfig records (one per nudge type) with default
   condition values. The admin reviews and adjusts after deployment.
2. Backfills ProgramEnrollment.total_call_count and weekly_call_count
   to 0 for all existing PEs where these fields are NULL.
3. Verifies the patch ran correctly.

Schema changes (doctype JSON, done before this patch runs)
----------------------------------------------------------
Phase 1 (new doctypes):
  - VoiceNudgeConfig + child tables VoiceNudgeEscalationType, VoiceNudgeSubmissionType
  - StudentGlificContext

Phase 2 (extended doctypes):
  - ParentCallConfig: nudge_type, language, variables_reference
  - VoiceAgentSettings: call timing + BigQuery sync fields
  - EscalationStep: enable_voice_call, nudge_config_override
  - ProgramEnrollment: last_call_at, last_call_outcome, total_call_count, weekly_call_count
  - ProgramEventLog event_type: voice_call_queued, voice_call_outcome

Idempotency
-----------
Frappe PatchLog gates re-execution. Within-run guards:
  - VoiceNudgeConfig insert: skip if record with that nudge_type already exists.
  - PE backfill: WHERE total_call_count IS NULL (only touches unset rows).

Postgres notes
--------------
- All table names quoted ("tabX") per Frappe-Postgres convention.
- frappe.get_doc().insert() used for VoiceNudgeConfig so controllers run.
- frappe.db.set_value used for PE backfill for speed (bypasses hooks).
- frappe.db.commit() called once at end.
"""

import frappe


# ── Nudge type seed data ──────────────────────────────────────────────────
# Each dict is one VoiceNudgeConfig record. Fields not listed use doctype
# defaults. Child table rows (applicable_escalation_types, applicable_submission_types)
# are listed as lists of dicts matching the child doctype fields.

_NUDGE_SEED = [
    {
        "nudge_type": "reinitiation",
        "display_name": "Reinitiation",
        "priority": 1,
        "is_active": 1,
        "situation_label": "program_paused",
        "description": (
            "Student's program is already paused. They need to know how to restart. "
            "9,636 students at this stage. The call tells them to type the reinitiation "
            "keyword on WhatsApp. Confirm reinitiation_keyword from Manu or Fencesitter flow JSON."
        ),
        "require_program_paused": 1,
        "require_prior_submission": 0,
        "require_no_weekly_submission": 0,
        "require_active_streak": 0,
        "require_grace_window": 0,
        "min_week": 0,
        "min_submission_count": 0,
        "call_script_note": (
            "Tone: hopeful. The pause is not permanent. Open with reassurance "
            "then give the keyword. Example: '[Name] ka program thodi der ke liye "
            "ruka tha. Par restart ho sakta hai — bas WhatsApp pe [KEYWORD] type karo.'"
        ),
        "reinitiation_keyword": "",
        "applicable_escalation_types": [],
        "applicable_submission_types": [],
    },
    {
        "nudge_type": "streak_protection",
        "display_name": "Streak Protection",
        "priority": 2,
        "is_active": 1,
        "situation_label": "streak_at_risk",
        "description": (
            "Student has an active week streak and is at risk of breaking it this week. "
            "Highest-value retention signal. The streak number is the anchor of the call."
        ),
        "require_prior_submission": 1,
        "require_no_weekly_submission": 1,
        "require_active_streak": 1,
        "require_program_paused": 0,
        "require_grace_window": 0,
        "min_week": 0,
        "min_submission_count": 1,
        "call_script_note": (
            "Tone: urgent but warm. Reference streak count explicitly. "
            "Example: '[Name], tumhari [X] hafton ki streak chal rahi hai. "
            "Aaj sirf [submission_ask] — streak safe rahegi.' "
            "Use {streak} and {submission_ask} variables."
        ),
        "applicable_escalation_types": [
            {"escalation_type": "help_note_a"},
            {"escalation_type": "help_note_b"},
            {"escalation_type": "voice_note"},
        ],
        "applicable_submission_types": [],
    },
    {
        "nudge_type": "real_blocker",
        "display_name": "Real Blocker Acknowledgment",
        "priority": 3,
        "is_active": 1,
        "situation_label": "complex_submission_stuck",
        "description": (
            "Week 3+ student with complex submission type (image, summary) who has submitted before "
            "but is not submitting this week. Real blockers from data: link not opening, internet "
            "ran out, experiment did not work, do not understand the format. Do NOT just re-ask to "
            "submit. Ask what is blocking, then address it."
        ),
        "require_prior_submission": 1,
        "require_no_weekly_submission": 1,
        "require_active_streak": 0,
        "require_program_paused": 0,
        "require_grace_window": 0,
        "min_week": 3,
        "min_submission_count": 1,
        "call_script_note": (
            "Tone: problem-solving. Open with acknowledgment of common blockers. "
            "Example: '[Name], week [N] ki [course] activity mein kuch problem aa rahi hai kya? "
            "Kai log keh rahe hain link nahi khul raha ya experiment mein kuch hua. "
            "Kya tum bata sakte ho kya problem hai?' "
            "If {last_problem_reported} is non-empty, reference it directly."
        ),
        "applicable_escalation_types": [
            {"escalation_type": "help_note_a"},
            {"escalation_type": "help_note_b"},
            {"escalation_type": "voice_note"},
        ],
        "applicable_submission_types": [
            {"submission_type": "summary_text_voice"},
            {"submission_type": "image"},
        ],
    },
    {
        "nudge_type": "continuation",
        "display_name": "Continuation",
        "priority": 4,
        "is_active": 1,
        "situation_label": "returning_not_submitted",
        "description": (
            "Student has submitted before (proven they can) but has not submitted this week. "
            "Currently receive zero Vocallabs calls — the biggest gap in the existing system. "
            "Requires enable_continuation_calls = 1 in VoiceAgentSettings AND "
            "enable_voice_call = 1 on the relevant EscalationStep."
        ),
        "require_prior_submission": 1,
        "require_no_weekly_submission": 1,
        "require_active_streak": 0,
        "require_program_paused": 0,
        "require_grace_window": 0,
        "min_week": 0,
        "min_submission_count": 1,
        "call_script_note": (
            "Tone: forward-looking, celebratory. They already proved they can do it. "
            "Example: '[Name], tumne pichle [submission_count] week bahut achha kiya. "
            "Is hafte ka kaam aa gaya hai — bas [submission_ask] bhejna hai.' "
            "Use {submission_count}, {course}, {week}, {submission_ask}."
        ),
        "applicable_escalation_types": [
            {"escalation_type": "help_note_a"},
            {"escalation_type": "help_note_b"},
            {"escalation_type": "voice_note"},
        ],
        "applicable_submission_types": [],
    },
    {
        "nudge_type": "so_close",
        "display_name": "You Were So Close",
        "priority": 5,
        "is_active": 1,
        "situation_label": "dropped_mid_flow",
        "description": (
            "Student started the WhatsApp flow or quiz this week but dropped off mid-way. "
            "Detected at runtime via ProgramEventLog: flow_triggered event for this week "
            "without a subsequent submission_received event."
        ),
        "require_prior_submission": 1,
        "require_no_weekly_submission": 0,
        "require_active_streak": 0,
        "require_program_paused": 0,
        "require_grace_window": 0,
        "min_week": 0,
        "min_submission_count": 1,
        "call_script_note": (
            "Tone: encouraging, specific. They almost finished — one more step. "
            "Example: '[Name], tumne [course] ka content dekha tha is hafte. "
            "Bas submission baki hai. WhatsApp kholo, wahan se shuru hoga jahan choda tha.' "
            "NOTE: _compute_situation() also checks ProgramEventLog at runtime for this nudge."
        ),
        "applicable_escalation_types": [
            {"escalation_type": "voice_note"},
        ],
        "applicable_submission_types": [],
    },
    {
        "nudge_type": "grace_deadline",
        "display_name": "Grace Deadline",
        "priority": 6,
        "is_active": 1,
        "situation_label": "deadline_live",
        "description": (
            "Student is in grace_waiting state with a live deadline. 34,893 students. "
            "Most urgent group. Deadlines concentrated July 2-9. The specific date must "
            "be mentioned. After the deadline, the program pauses."
        ),
        "require_prior_submission": 0,
        "require_no_weekly_submission": 0,
        "require_active_streak": 0,
        "require_program_paused": 0,
        "require_grace_window": 1,
        "min_week": 0,
        "min_submission_count": 0,
        "call_script_note": (
            "Tone: urgent but not threatening. The specific date makes it real. "
            "Example: '[Name] ke program ka aakhri mauka hai — [grace_deadline] tak submit "
            "karna hoga warna program ruk jayega. Abhi sirf [submission_ask] bhejna hai.' "
            "Use {grace_deadline} and {submission_ask}. "
            "If deadline is < 48 hours away, use more urgent framing."
        ),
        "applicable_escalation_types": [
            {"escalation_type": "parent_call"},
        ],
        "applicable_submission_types": [],
    },
    {
        "nudge_type": "celebration",
        "display_name": "Celebration",
        "priority": 7,
        "is_active": 1,
        "situation_label": "celebration",
        "description": (
            "Student hit a milestone: completed a week, earned a special gem, "
            "or crossed a points threshold. Low volume but high value for long-term retention. "
            "Triggered by ProgramEventLog events (week_completed, special_gems change), "
            "not by standard escalation. No submission ask."
        ),
        "require_prior_submission": 0,
        "require_no_weekly_submission": 0,
        "require_active_streak": 0,
        "require_program_paused": 0,
        "require_grace_window": 0,
        "min_week": 0,
        "min_submission_count": 0,
        "call_script_note": (
            "Tone: pure celebration. No ask at all. The call IS the reward. "
            "Optionally preview next week at the end: plants next action without pressure. "
            "Example: '[Name]! Tumne week [week] complete kiya — TAP ki taraf se Didi "
            "khud bulana chahti thi. Bahut badiya kiya!' "
            "Use {week}, {total_points}, {course}."
        ),
        "applicable_escalation_types": [],
        "applicable_submission_types": [],
    },
    {
        "nudge_type": "first_submission",
        "display_name": "First Submission Ever",
        "priority": 8,
        "is_active": 1,
        "situation_label": "first_timer",
        "description": (
            "Student has never submitted anything. submission_count = 0. "
            "At parent_call escalation stage in normal_escalation flow state. "
            "current_expected_submission_type is emoji for ~100% of these students. "
            "The ask is already at absolute minimum bar."
        ),
        "require_prior_submission": 0,
        "require_no_weekly_submission": 0,
        "require_active_streak": 0,
        "require_program_paused": 0,
        "require_grace_window": 0,
        "min_week": 0,
        "min_submission_count": 0,
        "call_script_note": (
            "Tone: zero pressure. Frame as getting started, NOT as failure. "
            "Do not say 'aapne submit nahi kiya'. "
            "Grade 4-6 ({grade_group}=parent_facilitated): address parent, ask them to help. "
            "Grade 7-9 ({grade_group}=student_direct): ask parent to hand phone or pass message. "
            "Example: '[Name] [course] program mein hain. Abhi sirf ek emoji bhejna hai "
            "WhatsApp pe — 10 second ka kaam hai.'"
        ),
        "applicable_escalation_types": [
            {"escalation_type": "parent_call"},
        ],
        "applicable_submission_types": [],
    },
]


def execute():
    # ── Step 1. Insert VoiceNudgeConfig records ──────────────────────────
    inserted = []
    skipped = []

    for seed in _NUDGE_SEED:
        nudge_type = seed["nudge_type"]

        if frappe.db.exists("VoiceNudgeConfig", nudge_type):
            skipped.append(nudge_type)
            continue

        doc = frappe.new_doc("VoiceNudgeConfig")
        escalation_types = seed.pop("applicable_escalation_types", [])
        submission_types = seed.pop("applicable_submission_types", [])

        doc.update(seed)

        for et in escalation_types:
            doc.append("applicable_escalation_types", et)

        for st in submission_types:
            doc.append("applicable_submission_types", st)

        doc.insert(ignore_permissions=True)
        inserted.append(nudge_type)

    # ── Step 2. Backfill ProgramEnrollment call tracking fields ──────────
    pe_backfill = frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET total_call_count  = COALESCE(total_call_count, 0),
               weekly_call_count = COALESCE(weekly_call_count, 0)
         WHERE total_call_count IS NULL
            OR weekly_call_count IS NULL
        RETURNING name
        """
    )
    pe_backfill_count = len(pe_backfill or [])

    frappe.db.commit()

    # ── Step 3. Verification ─────────────────────────────────────────────
    total_configs = frappe.db.count("VoiceNudgeConfig")
    active_configs = frappe.db.count("VoiceNudgeConfig", {"is_active": 1})

    null_call_count = frappe.db.sql(
        """
        SELECT COUNT(*) FROM "tabProgramEnrollment"
         WHERE total_call_count IS NULL OR weekly_call_count IS NULL
        """
    )[0][0]

    if null_call_count > 0:
        frappe.log_error(
            title="Didi Voice Nudge migration — PE backfill incomplete",
            message=f"{null_call_count} ProgramEnrollment rows still have null call count fields.",
        )

    frappe.log_error(
        title="[INFO] Didi Voice Nudge migration complete",
        message=(
            f"VoiceNudgeConfig inserted: {len(inserted)} ({inserted}). "
            f"Skipped (already existed): {len(skipped)} ({skipped}). "
            f"Total configs: {total_configs}, active: {active_configs}. "
            f"ProgramEnrollment call tracking backfilled: {pe_backfill_count} rows. "
            f"Remaining null PE rows: {null_call_count} (target: 0)."
        ),
    )
