# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

import frappe
import json
import re
from frappe.model.document import Document


class Student(Document):
	pass


@frappe.whitelist()
def register_student():
	"""Method to create/register a new student"""
	payload = json.loads(frappe.request.data)
	doc = frappe.new_doc('Student')
	doc.name1 = payload.get('name1')
	doc.phone = re.sub('^91', '', payload.get('phone'), count=0, flags=0)
	doc.section = payload.get('section')
	doc.grade = payload.get('grade')
	doc.level = ''
	doc.rigour = ''
	doc.append("enrollment", {
		"course": payload.get("course"),
		"batch": payload.get("batch")
	})

	if payload.get('school_id') and payload.get('school_id') != '':
		try:
			school = frappe.get_doc('School', payload.get('school_id'))
			doc.school_id = school.name
		except Exception:
			pass

	doc.insert()
	return {'status_code': 200,  'message': 'Student registered succesfully'}


@frappe.whitelist()
def update_student_profile():
	"""Method to update the profile id of a student"""

	# will have name, phone and profile_id
	payload = json.loads(frappe.request.data)

	# phone number should be 10 digit
	payload_phone = re.sub('^91', '', payload.get('phone'), count=0, flags=0)
	payload_name = payload.get('name1')
	payload_profile_id = payload.get('profile_id')

	query = {
		"phone": payload_phone,
		"profile_id": ""
	}
	student = None
	try:
		doc = frappe.get_last_doc('Student', filters=query)
		student = doc
	except Exception:
		pass

	if student:
		# update the profile id
		student.profile_id = payload_profile_id
		student.name1 = payload_name
		student.save()
	else:
		# create a new student with the profile, name and phone number
		doc = frappe.new_doc('Student')
		doc.name1 = payload_name
		doc.phone = payload_phone
		doc.profile_id = payload_profile_id
		doc.insert()

	return {'status_code': 200,  'message': 'Profile updated successfully'}