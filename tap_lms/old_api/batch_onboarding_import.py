import frappe
from frappe.model.document import Document
from your_app.your_app.doctype.batch_onboarding.batch_onboarding import generate_batch_skeyword

def before_import(doc, method):
    if not doc.batch_skeyword:
        doc.batch_skeyword = generate_batch_skeyword(doc.school, doc.batch)

def after_import(doc, method):
    # This will ensure that the batch_skeyword is unique even after bulk import
    existing_doc = frappe.get_doc("Batch Onboarding", doc.name)
    if frappe.db.exists("Batch Onboarding", {"batch_skeyword": existing_doc.batch_skeyword, "name": ["!=", doc.name]}):
        new_keyword = generate_batch_skeyword(doc.school, doc.batch)
        existing_doc.batch_skeyword = new_keyword
        existing_doc.save()
