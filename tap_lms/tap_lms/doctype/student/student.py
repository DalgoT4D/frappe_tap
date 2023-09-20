# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document

class Student(Document):
	pass


@frappe.whitelist()
def register_student():
	payload = json.loads(frappe.request.data)
	doc = frappe.new_doc('Student')
	doc.name1 = payload.get('name1')
	doc.phone = payload.get('phone')
	doc.section = payload.get('section')
	doc.grade = payload.get('grade')
	doc.enrollment = [{"course": payload.get('course'), "batch": payload.get("batch")}]
	doc.insert()
	return {'status_code':200,  'message': 'Student registered succesfully'}