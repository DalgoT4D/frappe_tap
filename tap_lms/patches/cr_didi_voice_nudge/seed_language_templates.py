"""
Seed 24 ParentCallConfig records (8 nudges x 3 languages: English, Marathi, Punjabi)
and link all 4 languages (including Hindi) in VoiceNudgeLanguageTemplate child table
on each VoiceNudgeConfig.

Hindi templates are seeded by seed_prompt_templates.py.
This patch handles English, Marathi, Punjabi and links all 4 on each nudge config.
"""

import frappe


TEMPLATES = {
    "continuation": {
        "English": ("Didi-English-Continuation", "Student: {student_name} | Week: {week} | Course: {course} | Situation: {situation} | Submissions: {submission_count} | Ask: {submission_ask} | Grade: {grade_group} | Last message: {last_message}"),
        "Marathi": ("Didi-Marathi-Continuation", "विद्यार्थी: {student_name} | आठवडा: {week} | अभ्यासक्रम: {course} | परिस्थिती: {situation} | submissions: {submission_count} | या आठवड्यात: {submission_ask} | शेवटचा संदेश: {last_message}"),
        "Punjabi": ("Didi-Punjabi-Continuation", "ਵਿਦਿਆਰਥੀ: {student_name} | ਹਫ਼ਤਾ: {week} | ਕੋਰਸ: {course} | ਸਥਿਤੀ: {situation} | submissions: {submission_count} | ਇਸ ਹਫ਼ਤੇ: {submission_ask} | ਆਖਰੀ ਸੁਨੇਹਾ: {last_message}"),
    },
    "streak_protection": {
        "English": ("Didi-English-Streak-Protection", "Student: {student_name} | Week: {week} | Streak: {streak} weeks | Situation: {situation} | Ask: {submission_ask} | Grade: {grade_group} | Last message: {last_message}"),
        "Marathi": ("Didi-Marathi-Streak-Protection", "विद्यार्थी: {student_name} | आठवडा: {week} | streak: {streak} आठवडे | परिस्थिती: {situation} | या आठवड्यात: {submission_ask} | शेवटचा संदेश: {last_message}"),
        "Punjabi": ("Didi-Punjabi-Streak-Protection", "ਵਿਦਿਆਰਥੀ: {student_name} | ਹਫ਼ਤਾ: {week} | streak: {streak} ਹਫ਼ਤੇ | ਸਥਿਤੀ: {situation} | ਇਸ ਹਫ਼ਤੇ: {submission_ask} | ਆਖਰੀ ਸੁਨੇਹਾ: {last_message}"),
    },
    "real_blocker": {
        "English": ("Didi-English-Real-Blocker", "Student: {student_name} | Week: {week} | Course: {course} | Situation: {situation} | Ask: {submission_ask} | Problem: {last_problem_reported} | Last message: {last_message} | Grade: {grade_group}"),
        "Marathi": ("Didi-Marathi-Real-Blocker", "विद्यार्थी: {student_name} | आठवडा: {week} | अभ्यासक्रम: {course} | परिस्थिती: {situation} | या आठवड्यात: {submission_ask} | समस्या: {last_problem_reported} | शेवटचा संदेश: {last_message}"),
        "Punjabi": ("Didi-Punjabi-Real-Blocker", "ਵਿਦਿਆਰਥੀ: {student_name} | ਹਫ਼ਤਾ: {week} | ਕੋਰਸ: {course} | ਸਥਿਤੀ: {situation} | ਇਸ ਹਫ਼ਤੇ: {submission_ask} | ਸਮੱਸਿਆ: {last_problem_reported} | ਆਖਰੀ ਸੁਨੇਹਾ: {last_message}"),
    },
    "first_submission": {
        "English": ("Didi-English-First-Submission", "Student: {student_name} | Week: {week} | Course: {course} | Situation: {situation} | Ask: {submission_ask} | Grade: {grade_group}"),
        "Marathi": ("Didi-Marathi-First-Submission", "विद्यार्थी: {student_name} | आठवडा: {week} | अभ्यासक्रम: {course} | परिस्थिती: {situation} | या आठवड्यात: {submission_ask} | ग्रेड गट: {grade_group}"),
        "Punjabi": ("Didi-Punjabi-First-Submission", "ਵਿਦਿਆਰਥੀ: {student_name} | ਹਫ਼ਤਾ: {week} | ਕੋਰਸ: {course} | ਸਥਿਤੀ: {situation} | ਇਸ ਹਫ਼ਤੇ: {submission_ask} | ਗ੍ਰੇਡ ਗਰੁੱਪ: {grade_group}"),
    },
    "grace_deadline": {
        "English": ("Didi-English-Grace-Deadline", "Student: {student_name} | Week: {week} | Course: {course} | Situation: {situation} | Deadline: {grace_deadline} | Ask: {submission_ask} | Grade: {grade_group}"),
        "Marathi": ("Didi-Marathi-Grace-Deadline", "विद्यार्थी: {student_name} | आठवडा: {week} | अभ्यासक्रम: {course} | परिस्थिती: {situation} | अंतिम तारीख: {grace_deadline} | या आठवड्यात: {submission_ask}"),
        "Punjabi": ("Didi-Punjabi-Grace-Deadline", "ਵਿਦਿਆਰਥੀ: {student_name} | ਹਫ਼ਤਾ: {week} | ਕੋਰਸ: {course} | ਸਥਿਤੀ: {situation} | ਆਖਰੀ ਤਾਰੀਖ: {grace_deadline} | ਇਸ ਹਫ਼ਤੇ: {submission_ask}"),
    },
    "reinitiation": {
        "English": ("Didi-English-Reinitiation", "Student: {student_name} | Course: {course} | Situation: {situation} | Grade: {grade_group}"),
        "Marathi": ("Didi-Marathi-Reinitiation", "विद्यार्थी: {student_name} | अभ्यासक्रम: {course} | परिस्थिती: {situation} | ग्रेड गट: {grade_group}"),
        "Punjabi": ("Didi-Punjabi-Reinitiation", "ਵਿਦਿਆਰਥੀ: {student_name} | ਕੋਰਸ: {course} | ਸਥਿਤੀ: {situation} | ਗ੍ਰੇਡ ਗਰੁੱਪ: {grade_group}"),
    },
    "celebration": {
        "English": ("Didi-English-Celebration", "Student: {student_name} | Week: {week} | Course: {course} | Situation: {situation} | Last message: {last_message}"),
        "Marathi": ("Didi-Marathi-Celebration", "विद्यार्थी: {student_name} | आठवडा: {week} | अभ्यासक्रम: {course} | परिस्थिती: {situation} | शेवटचा संदेश: {last_message}"),
        "Punjabi": ("Didi-Punjabi-Celebration", "ਵਿਦਿਆਰਥੀ: {student_name} | ਹਫ਼ਤਾ: {week} | ਕੋਰਸ: {course} | ਸਥਿਤੀ: {situation} | ਆਖਰੀ ਸੁਨੇਹਾ: {last_message}"),
    },
    "so_close": {
        "English": ("Didi-English-So-Close", "Student: {student_name} | Week: {week} | Course: {course} | Situation: {situation} | Submissions: {submission_count} | Ask: {submission_ask} | Last message: {last_message}"),
        "Marathi": ("Didi-Marathi-So-Close", "विद्यार्थी: {student_name} | आठवडा: {week} | अभ्यासक्रम: {course} | परिस्थिती: {situation} | submissions: {submission_count} | या आठवड्यात: {submission_ask} | शेवटचा संदेश: {last_message}"),
        "Punjabi": ("Didi-Punjabi-So-Close", "ਵਿਦਿਆਰਥੀ: {student_name} | ਹਫ਼ਤਾ: {week} | ਕੋਰਸ: {course} | ਸਥਿਤੀ: {situation} | submissions: {submission_count} | ਇਸ ਹਫ਼ਤੇ: {submission_ask} | ਆਖਰੀ ਸੁਨੇਹਾ: {last_message}"),
    },
}

HINDI_MAP = {
    "continuation": "Didi-Hindi-Continuation",
    "streak_protection": "Didi-Hindi-Streak-Protection",
    "real_blocker": "Didi-Hindi-Real-Blocker",
    "first_submission": "Didi-Hindi-First-Submission",
    "grace_deadline": "Didi-Hindi-Grace-Deadline",
    "reinitiation": "Didi-Hindi-Reinitiation",
    "celebration": "Didi-Hindi-Celebration",
    "so_close": "Didi-Hindi-So-Close",
}


def execute():
    frappe.reload_doc("tap_lms", "doctype", "parentcallconfig")
    frappe.reload_doc("tap_lms", "doctype", "voicenudgeconfig")
    frappe.reload_doc("tap_lms", "doctype", "voicenudgelanguagetemplate")
    frappe.db.commit()

    for nudge_type, languages in TEMPLATES.items():
        if not frappe.db.exists("VoiceNudgeConfig", nudge_type):
            continue

        nudge_doc = frappe.get_doc("VoiceNudgeConfig", nudge_type)

        for language, (title, template) in languages.items():
            # Create ParentCallConfig if not exists
            if not frappe.db.exists("ParentCallConfig", title):
                pcc = frappe.new_doc("ParentCallConfig")
                pcc.title = title
                pcc.nudge_type = nudge_type
                pcc.language = language
                pcc.is_active = 1
                pcc.status_template = template
                pcc.insert(ignore_permissions=True)

            # Link in language_templates if not already linked
            already = any(
                r.language == language
                for r in nudge_doc.get("language_templates", [])
            )
            if not already:
                nudge_doc.append("language_templates", {
                    "language": language,
                    "parent_call_config": title,
                    "is_active": 1,
                })

        # Also link Hindi if not already linked
        hindi_config = HINDI_MAP.get(nudge_type)
        if hindi_config and frappe.db.exists("ParentCallConfig", hindi_config):
            already = any(
                r.language == "Hindi"
                for r in nudge_doc.get("language_templates", [])
            )
            if not already:
                nudge_doc.append("language_templates", {
                    "language": "Hindi",
                    "parent_call_config": hindi_config,
                    "is_active": 1,
                })

        nudge_doc.save(ignore_permissions=True)

    frappe.db.commit()
