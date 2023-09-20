# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document

frappe.utils.logger.set_log_level("DEBUG")
logger = frappe.logger("api", allow_site=True, file_count=50)

class Student(Document):
	pass


@frappe.whitelist()
def register_student():
	"""Method to create/register a new student"""
	payload = json.loads(frappe.request.data)
	doc = frappe.new_doc('Student')
	doc.name1 = payload.get('name1')
	doc.phone = payload.get('phone')
	doc.section = payload.get('section')
	doc.grade = payload.get('grade')
	doc.level = ''
	doc.rigour = ''
	doc.append("enrollment", {
		"course": payload.get("course"),
		"batch": payload.get("batch")
	})
	doc.insert()
	return {'status_code': 200,  'message': 'Student registered succesfully'}


@frappe.whitelist()
def update_student_profile():
	"""Method to update the profile id of a student"""
	logger.info("inside the update profile hook")
	payload = json.loads(frappe.request.data)
	query = {
		"phone": payload.get('phone'),
		"name1": payload.get('name1'),
		"profile_id": ""
	}
	student = None
	logger.info("loaded the request data")
	try:
		doc = frappe.get_last_doc('Student', filters=query)
		student = doc
		logger.info("found the student with the filters")
	except Exception:
		logger.info("did not find the student")
		pass

	if student:
		logger.info("saving the student")
		student.profile_id = payload.get('profile_id')
		student.save()
	return {'status_code': 200,  'message': 'Profile updated successfully'}