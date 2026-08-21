import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class VoiceCallBlocklist(Document):
    def before_insert(self):
        self.added_at = now_datetime()
        # Normalize phone — strip leading 91 if present
        phone = (self.phone or "").strip()
        if phone.startswith("91") and len(phone) > 10:
            phone = phone[2:]
        self.phone = phone


def is_blocked(phone):
    """Return True if this phone is in the no-call blocklist.

    Normalizes the phone before checking — strips leading 91.
    """
    if not phone:
        return False
    clean = str(phone).strip()
    if clean.startswith("91") and len(clean) > 10:
        clean = clean[2:]
    return bool(frappe.db.exists("VoiceCallBlocklist", {"phone": clean}))
