import frappe
from frappe.model.document import Document


class BigQuerySettings(Document):
    def validate(self):
        if self.enabled and self.service_account_json:
            import json
            try:
                json.loads(self.service_account_json)
            except Exception:
                frappe.throw("Service Account JSON is not valid JSON. Paste the full credentials file content.")
