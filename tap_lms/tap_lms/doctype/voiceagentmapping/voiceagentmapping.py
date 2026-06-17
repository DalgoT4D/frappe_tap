# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

"""
VoiceAgentMapping — per-language Vocallabs agent assignment.

Child table of VoiceAgentSettings (istable=1). One row per language
maps that language's students to a specific Vocallabs agent UUID. The
parent's `agent_id` field remains the fallback for any student whose
language has no enabled mapping here.

Uniqueness: at most ONE enabled row per language within the parent.
The standard Frappe `unique=1` flag at column level only enforces
table-wide uniqueness, which is wrong here (we want uniqueness within
the parent doc). Enforced via the validate hook below.

Backend resolver (separate file, not in this controller):
  `summer_program/vocallabs._resolve_voice_agent(pe, settings)` reads
  this child table to pick the right agent_id for a student's language,
  falling back to `settings.agent_id` if no enabled row matches.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class VoiceAgentMapping(Document):
    """Per-language voice-agent assignment row.

    Validate-time rule: enforce uniqueness of (parent, language) for
    ENABLED rows. Disabled rows are allowed to duplicate a language so
    operators can stage a new agent_id before flipping the switch.
    """

    def validate(self):
        self._enforce_unique_language_within_parent()

    def _enforce_unique_language_within_parent(self):
        """Reject saves where another ENABLED row in the same parent
        already claims this language. Disabled rows are exempt — that's
        how operators stage a new agent_id before cutover.
        """
        if not self.enabled:
            return  # disabled rows don't need to be unique

        if not (self.parent and self.language):
            return  # incomplete row; required-field validation handles the error

        # Look for another enabled row in the same parent with the same
        # language. Exclude this row's own `name` (which is None on a
        # fresh insert — `frappe.db.get_all` handles that case).
        siblings = frappe.db.get_all(
            "VoiceAgentMapping",
            filters={
                "parent": self.parent,
                "parenttype": self.parenttype,
                "language": self.language,
                "enabled": 1,
                "name": ["!=", self.name or ""],
            },
            pluck="name",
        )
        if siblings:
            frappe.throw(
                _("Another enabled VoiceAgentMapping row in this "
                  "VoiceAgentSettings already serves language "
                  "{0}: {1}. Disable that row first, or remove this "
                  "duplicate.").format(self.language, siblings[0]),
                title=_("Duplicate enabled language mapping"),
            )
