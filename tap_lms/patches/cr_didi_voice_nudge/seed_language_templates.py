"""
Seed per-language ParentCallConfig mappings into VoiceNudgeConfig.language_templates.

This is a no-op stub — the actual linking is done manually by the admin
in the Frappe desk after reviewing and creating ParentCallConfig records
per language. This patch just registers itself so the migration does not
error on missing module.
"""

import frappe


def execute():
    frappe.log_error(
        title="Didi seed_language_templates patch",
        message=(
            "Language template seeding is a manual step. "
            "Open each VoiceNudgeConfig record in the Frappe desk and add "
            "rows to the Language Templates child table, linking each language "
            "to the correct ParentCallConfig. Hindi, Marathi, Punjabi templates "
            "were seeded by seed_prompt_templates.py."
        ),
    )
