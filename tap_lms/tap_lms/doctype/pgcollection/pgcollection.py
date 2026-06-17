# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PGCollection(Document):
	def validate(self):
		"""CR-005 follow-up (2026-05-16): enforce uniqueness of (parent, kind)
		for active kind-keyed rows.

		Each Batch Program Run owns exactly one active row per kind
		(main / escalation / binge_paused / program_dropped / program_completed).
		Two concurrent activate_bpr calls or a hand-edit could otherwise leave
		duplicate rows that the dispatcher and weekly cron read non-deterministically.

		Legacy pre-CR-005 archetype-keyed rows have kind IS NULL / '' and are
		exempt — they were deactivated en masse by patches/v0_2/backfill_pg_collection_kinds
		and remain in place as historical record.

		Deactivated rows (is_active = 0) are also exempt — they can duplicate
		freely; only active siblings are forbidden.
		"""
		if not self.kind:
			return  # legacy row, no uniqueness rule

		if not self.is_active:
			return  # deactivated row, no uniqueness rule

		sibling = frappe.db.sql(
			"""
			SELECT name
			  FROM "tabPGCollection"
			 WHERE parent = %s
			   AND kind = %s
			   AND COALESCE(is_active, 0) = 1
			   AND name != %s
			 LIMIT 1
			""",
			(self.parent, self.kind, self.name or ""),
		)
		if sibling:
			frappe.throw(
				_(
					"PGCollection (parent={0}, kind={1}) already exists as "
					"active row {2}. Each BPR may have at most one active "
					"row per kind."
				).format(self.parent, self.kind, sibling[0][0]),
				frappe.DuplicateEntryError,
			)
