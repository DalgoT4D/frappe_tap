# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class VoiceNudgeConfig(Document):
    def validate(self):
        self._validate_unique_priority()
        self._validate_required_situation_label()

    def _validate_unique_priority(self):
        """Warn if another active VoiceNudgeConfig has the same priority.

        Two records at the same priority produce undefined evaluation order.
        We warn rather than error so existing data is not blocked on save.
        """
        duplicate = frappe.db.get_value(
            "VoiceNudgeConfig",
            {
                "priority": self.priority,
                "is_active": 1,
                "name": ("!=", self.name),
            },
            "nudge_type",
        )
        if duplicate:
            frappe.msgprint(
                f"Warning: VoiceNudgeConfig '{duplicate}' also has priority "
                f"{self.priority}. Evaluation order between them is undefined. "
                f"Assign unique priorities to guarantee deterministic selection.",
                indicator="orange",
                alert=True,
            )

    def _validate_required_situation_label(self):
        if not (self.situation_label or "").strip():
            frappe.throw("Situation Label is required. It is passed in data_block['situation'] to Vocallabs.")
