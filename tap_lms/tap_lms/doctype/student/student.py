# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

class Student(Document):
	pass


@frappe.whitelist()
def register_student():
	return {'status_code':200,  'message': 'Student registered succesfully'}