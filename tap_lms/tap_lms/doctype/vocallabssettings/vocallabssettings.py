import frappe
from frappe.model.document import Document


class VocalLabsSettings(Document):
    def validate(self):
        if not self.service_url:
            frappe.throw("Service URL is required.")
        if not self.client_id:
            frappe.throw("Client ID is required.")
        if not self.client_secret:
            frappe.throw("Client Secret is required.")
