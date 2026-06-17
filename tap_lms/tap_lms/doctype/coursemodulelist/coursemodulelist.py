# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

# Stub child DocType referenced by ProjectChallenge.related_modules (Table
# field). Previously this DocType was missing from the codebase — the
# reference was orphaned at some point during early development. Restored
# 2026-05-23 as a minimal istable=1 placeholder so Frappe's test runner can
# walk the dependency tree without crashing. The field carries a single
# `course_module` text column; extend if the related-modules feature is
# fleshed out later.

from frappe.model.document import Document


class CourseModuleList(Document):
	pass
