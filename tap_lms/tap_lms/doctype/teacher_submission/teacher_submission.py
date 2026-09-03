# Copyright (c) 2026, Techt4dev and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TeacherSubmission(Document):
    def before_save(self):
        # image_count is derived, never set by hand — so it stays correct
        # whoever wrote the URLs: the API, the matching job, or a person
        # editing the field in the Desk.
        urls = [line.strip() for line in (self.image_urls or "").splitlines() if line.strip()]
        self.image_count = len(urls)
