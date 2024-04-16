# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

import frappe
import json
import re
from frappe.model.document import Document

logger = frappe.logger("custom_student_webhook", with_more_info=True)
logger.setLevel("INFO")


class Student(Document):
    pass


@frappe.whitelist()
def register_student():
    """Method to create/register a new student"""
    try:
        logger.info(
            "Entered tap's registration webhook with payload %s", frappe.request.data
        )
        payload = json.loads(frappe.request.data)
        doc = frappe.new_doc("Student")
        doc.name1 = payload.get("name1")
        doc.phone = re.sub("^91", "", payload.get("phone"), count=0, flags=0)
        doc.section = payload.get("section")
        doc.grade = payload.get("grade")
        doc.gender = payload.get("gender")
        doc.level = ""
        doc.rigour = ""
        doc.append(
            "enrollment",
            {"course": payload.get("course"), "batch": payload.get("batch")},
        )
        if payload.get("keyword") and payload.get("keyword") != "":
            try:
                school = frappe.get_last_doc(
                    "School", filters={"keyword": payload.get("keyword")}
                )
                doc.school_id = school.name
            except Exception:
                pass
        doc.insert()
        logger.info("Student with phone %s registered successfully", doc.phone)
        return {"status_code": 200, "message": "Student registered succesfully"}
    except Exception as err:
        raise Exception("Registration webhook : " + str(err))


@frappe.whitelist()
def update_student_profile():
    """Method to update the profile id of a student"""
    try:
        # will have name, phone and profile_id
        payload = json.loads(frappe.request.data)

        logger.info(
            "Entered tap's profile update webhook for profile_id %s",
            payload.get("profile_id"),
        )

        # phone number should be 10 digit
        payload_phone = re.sub("^91", "", payload.get("phone"), count=0, flags=0)
        payload_name = payload.get("name1")
        payload_profile_id = payload.get("profile_id")
        payload_course = payload.get("course")
        payload_batch = payload.get("batch")

        query = {"phone": payload_phone, "profile_id": ""}
        student = None
        try:
            doc = frappe.get_last_doc("Student", filters=query)
            student = doc
        except Exception:
            pass

        if student:
            # update the profile id
            student.profile_id = payload_profile_id
            student.name1 = payload_name
            student.save()
        else:
            # create a new student with the profile, name, phone number and enrollment
            doc = frappe.new_doc("Student")
            doc.name1 = payload_name
            doc.phone = payload_phone
            doc.profile_id = payload_profile_id
            doc.level = ""
            doc.rigour = ""
            doc.append("enrollment", {"course": payload_course, "batch": payload_batch})
            doc.insert()
        logger.info("Updated profile for student with phone %s ", payload_phone)

        return {"status_code": 200, "message": "Profile updated successfully"}
    except Exception as err:
        raise Exception("Profile webhook : " + str(err))
