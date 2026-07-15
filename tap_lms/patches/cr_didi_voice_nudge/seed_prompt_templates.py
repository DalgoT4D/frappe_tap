"""
Didi Voice Agent — ParentCallConfig prompt templates seed.

Seeds one Hindi ParentCallConfig per nudge type, with complete
status_template text using all available variables.

The templates are starting points. Adjust tone and wording after
reviewing with Manu. The Vocallabs agent_prompt on the dashboard
is the other half — it reads the {situation} variable and switches
scripts. These templates fill the {status} variable in the data_block.

Run via: bench --site tap_lms.localhost migrate
Or directly: bench --site tap_lms.localhost execute
    tap_lms.patches.cr_didi_voice_nudge.seed_prompt_templates.execute
"""

import frappe


_TEMPLATES = [
    {
        "title": "Didi-Hindi-Continuation",
        "nudge_type": "continuation",
        "language": "Hindi",
        "is_active": 1,
        "status_template": (
            "Student: {student_name} | Week: {week} | Course: {course} | "
            "Situation: {situation} | Submissions so far: {submission_count} | "
            "This week: {submission_ask} | Call number: {call_attempt} | "
            "Grade group: {grade_group}"
        ),
        "_script_note": """
DIDI SCRIPT — Continuation (returning_not_submitted)
=====================================================
Opening (grade_group = parent_facilitated, grade 4-6):
  "Namaste! Main Didi hoon TAP se. Kya main [student_name] ke
   mummy/papa se baat kar sakti hoon? [student_name] ne pichle
   [submission_count] week [course] mein bahut achha kiya hai.
   Is hafte ka kaam aa gaya hai — bas [submission_ask] bhejna hai.
   Kya aap unhe ek baar help kar sakte hain?"

Opening (grade_group = student_direct, grade 7-9):
  "Namaste [student_name]! Main Didi hoon TAP se. Tumne pichle
   [submission_count] week bahut achha kiya — [course] mein week
   [week] ka kaam ab ready hai. Bas [submission_ask] bhejna hai.
   Kya aaj kar sakte ho?"

Common responses to handle:
  "Baad mein karenge" → "Bilkul, kab? Kal subah ya aaj sham?"
  "Busy hain" → "Sirf 5 minute ka kaam hai. Kya aaj sham mein time milega?"
  "Kya karna hai?" → Explain [submission_ask] specifically
        """
    },
    {
        "title": "Didi-Hindi-Streak-Protection",
        "nudge_type": "streak_protection",
        "language": "Hindi",
        "is_active": 1,
        "status_template": (
            "Student: {student_name} | Week: {week} | Streak: {streak} hafton ki | "
            "Situation: {situation} | Ask: {submission_ask} | "
            "Course: {course} | Grade: {grade_group}"
        ),
        "_script_note": """
DIDI SCRIPT — Streak Protection (streak_at_risk)
================================================
Opening:
  "Namaste! Main Didi hoon TAP se. [student_name] ki [streak] hafton
   ki streak chal rahi hai — aaj submit karo toh streak safe rahegi!
   Sirf [submission_ask] bhejna hai WhatsApp pe. 5 minute ka kaam."

If parent answers:
  "[student_name] ki [streak] week ki streak chal rahi hai. Aaj
   WhatsApp pe [submission_ask] bhejne se streak save hogi."

Common responses:
  "Kya hoti hai streak?" → "Lagaataar weeks mein kaam karne se points
   milte hain aur special reward bhi."
  "Aaj nahi hoga" → "Kal ho sakta hai? Streak kal tak valid hai."
        """
    },
    {
        "title": "Didi-Hindi-Real-Blocker",
        "nudge_type": "real_blocker",
        "language": "Hindi",
        "is_active": 1,
        "status_template": (
            "Student: {student_name} | Week: {week} | Course: {course} | "
            "Situation: {situation} | Ask: {submission_ask} | "
            "Last problem: {last_problem_reported} | Last message: {last_message} | "
            "Grade: {grade_group}"
        ),
        "_script_note": """
DIDI SCRIPT — Real Blocker (complex_submission_stuck)
=====================================================
If last_problem_reported is not empty:
  "Namaste! [student_name] ne bataya tha ki '[last_problem_reported]'.
   Main seedha call karke jaanna chahti thi — kya problem solve hui?
   Week [week] ki [course] activity mein [submission_ask] bhejna hai."

If last_problem_reported is empty:
  "Namaste! [student_name] is hafte [course] ki activity mein kuch
   problem aa rahi hai kya? Kai students keh rahe hain link nahi khul
   raha, ya experiment mein kuch hua, ya format samajh nahi aaya.
   Kya aap bata sakte hain kya problem hai?"

Common blockers to handle:
  "Link nahi khul raha" → WhatsApp ko band karke dubara kholo. Ya
    pehle video dekho, phir submit karo.
  "Internet nahi hai" → Kab milega? Main tab call karta/karti.
  "Samajh nahi aaya" → [submission_ask] ka matlab explain karo.
  "Experiment nahi hua" → Koi baat nahi, jo hua woh photo bhejo.
        """
    },
    {
        "title": "Didi-Hindi-First-Submission",
        "nudge_type": "first_submission",
        "language": "Hindi",
        "is_active": 1,
        "status_template": (
            "Student: {student_name} | Week: {week} | Course: {course} | "
            "Situation: {situation} | Ask: {submission_ask} | Grade: {grade_group}"
        ),
        "_script_note": """
DIDI SCRIPT — First Submission (first_timer)
============================================
DO NOT say "aapne submit nahi kiya". Frame as getting started.

For grade_group = parent_facilitated (grade 4-6):
  "Namaste! Main Didi hoon TAP se. [student_name] hamare [course]
   program mein join kiya hua hai. Shuru karna bahut easy hai —
   sirf WhatsApp pe [submission_ask]. 10 second ka kaam hai.
   Kya aap unhe ek baar help karenge?"

For grade_group = student_direct (grade 7-9):
  "Namaste! [student_name] ke liye TAP ka message hai. Bas
   WhatsApp pe [submission_ask] — bilkul easy hai. Kya aap phone
   [student_name] ko de sakte hain ek minute ke liye?"

Common responses:
  "Kya program hai yeh?" → Short explanation: "TAP ka free learning
   program hai jisme [student_name] join kiya. Abhi sirf ek [submission_ask]."
  "Hum try karenge" → "Abhi 2 minute mein ho sakta hai. Main wait karti hoon."
        """
    },
    {
        "title": "Didi-Hindi-Grace-Deadline",
        "nudge_type": "grace_deadline",
        "language": "Hindi",
        "is_active": 1,
        "status_template": (
            "Student: {student_name} | Week: {week} | Course: {course} | "
            "Situation: {situation} | Deadline: {grace_deadline} | "
            "Ask: {submission_ask} | Grade: {grade_group}"
        ),
        "_script_note": """
DIDI SCRIPT — Grace Deadline (deadline_live)
============================================
URGENT tone. Specific date is critical.

  "Namaste! [student_name] ke program ka aakhri mauka [grace_deadline]
   tak hai. Uske baad program ruk jayega. Abhi sirf [submission_ask]
   bhejna hai WhatsApp pe — itna hi kaafi hai program jaari rakhne ke
   liye. Kya aaj ho sakta hai?"

If < 24 hours to deadline:
  "[grace_deadline] matlab kal tak sirf! Abhi bhi time hai —
   [submission_ask] karo aur program safe ho jayega."

Common responses:
  "Kal karenge" → "[grace_deadline] ke baad program band ho jayega.
   Aaj hi karna zaroori hai."
  "Nahi hoga" → "Sirf [submission_ask] bhejna hai, 2 minute ka kaam.
   Kya main wait kar sakti hoon?"
        """
    },
    {
        "title": "Didi-Hindi-Reinitiation",
        "nudge_type": "reinitiation",
        "language": "Hindi",
        "is_active": 1,
        "status_template": (
            "Student: {student_name} | Course: {course} | "
            "Situation: {situation} | Grade: {grade_group}"
        ),
        "_script_note": """
DIDI SCRIPT — Reinitiation (program_paused)
===========================================
Hopeful tone. The pause is NOT permanent.

NOTE: Set reinitiation_keyword in VoiceNudgeConfig before using.
Replace [KEYWORD] below with the actual keyword.

  "Namaste! [student_name] ka TAP [course] program thodi der ke liye
   ruka tha. Par koi baat nahi — abhi bhi wapas aa sakte hain!
   Bas WhatsApp pe [KEYWORD] type karo aur program wahan se shuru
   hoga jahan choda tha."

If parent asks why it paused:
  "Kuch weeks mein koi response nahi aaya tha. Par aaj bhi
   restart ho sakta hai — [KEYWORD] type karo."

IMPORTANT: Confirm [KEYWORD] with Manu before activating this template.
        """
    },
    {
        "title": "Didi-Hindi-Celebration",
        "nudge_type": "celebration",
        "language": "Hindi",
        "is_active": 1,
        "status_template": (
            "Student: {student_name} | Week: {week} | Course: {course} | "
            "Points: {total_points} | Situation: {situation}"
        ),
        "_script_note": """
DIDI SCRIPT — Celebration (celebration)
========================================
NO ASK. Pure celebration. The call IS the reward.

  "Namaste! Main Didi hoon TAP se. [student_name] ne week [week]
   complete kiya — TAP ki taraf se personally badhai dene ke liye
   call kiya! Bahut badiya kiya. [total_points] points aa gaye hain.
   [course] mein aage bhi aise hi karte raho!"

Optional preview of next week (encourage continuation):
  "Week [week+1] thoda interesting hoga — aage ka content aa
   jayega. Main janti hoon tum kar loge."

DO NOT: Ask for anything. Do not mention missing tasks. This call
is a reward and should feel completely positive.
        """
    },
]


def execute():
    inserted = []
    skipped = []

    for tmpl in _TEMPLATES:
        title = tmpl["title"]
        if frappe.db.exists("ParentCallConfig", title):
            skipped.append(title)
            continue

        script_note = tmpl.pop("_script_note", "")

        doc = frappe.new_doc("ParentCallConfig")
        doc.update(tmpl)
        doc.insert(ignore_permissions=True)
        inserted.append(title)

        # restore for next loop
        tmpl["_script_note"] = script_note

    # Link each config to its VoiceNudgeConfig.default_template_config
    for tmpl in _TEMPLATES:
        nudge_name = tmpl["nudge_type"]
        config_title = tmpl["title"]
        if frappe.db.exists("VoiceNudgeConfig", nudge_name):
            current = frappe.db.get_value("VoiceNudgeConfig", nudge_name, "default_template_config")
            if not current:
                frappe.db.set_value("VoiceNudgeConfig", nudge_name, "default_template_config", config_title)

    frappe.db.commit()

    frappe.log_error(
        title="Didi prompt templates seeded",
        message=(
            f"ParentCallConfig inserted: {len(inserted)} ({inserted}). "
            f"Skipped (already existed): {len(skipped)}."
        ),
    )
