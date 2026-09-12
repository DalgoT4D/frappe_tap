import frappe
from frappe.model.document import Document


class ElevenLabsSettings(Document):
    def validate(self):
        if self.enabled and not self.api_key:
            frappe.throw("API Key is required when ElevenLabs is enabled.")
        if self.enabled and not self.phone_number_id:
            frappe.throw("Phone Number ID is required when ElevenLabs is enabled.")
