import frappe


GLIFIC_FEEDBACK_FLOW_ID = "34108"


def execute():
    frappe.reload_doc("tap_lms", "doctype", "submission")

    if not frappe.db.table_exists("Submission"):
        return

    if not frappe.db.has_column("Submission", "feedback_flow_id"):
        return

    if not frappe.db.has_column("Submission", "send_feedback"):
        return

    frappe.db.sql(
        """
        UPDATE `tabSubmission`
           SET feedback_flow_id = %s
         WHERE send_feedback = 'yes'
           AND COALESCE(feedback_flow_id, '') = ''
        """,
        (GLIFIC_FEEDBACK_FLOW_ID,),
    )
    frappe.db.commit()
