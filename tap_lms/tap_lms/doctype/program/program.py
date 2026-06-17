import frappe
from frappe.model.document import Document


class Program(Document):

    def after_insert(self):
        frappe.msgprint(
            frappe._("Program {0} created successfully.").format(self.name),
            indicator="green",
            alert=True,
        )


@frappe.whitelist()
def get_batches(program_name):
    return frappe.get_all(
        "Batch",
        filters={"program": program_name},
        fields=[
            "name",
            "name1",
            "batch_id",
            "start_date",
            "end_date",
            "active",
            "regist_end_date",
            "program_type",
            "total_weeks",
            "current_calendar_week",
        ],
        order_by="start_date asc",
    )


@frappe.whitelist()
def get_course_levels(program_name):
    return frappe.get_all(
        "Course Level",
        filters={"program": program_name},
        fields=[
            "name",
            "name1",
            "vertical",
            "stage",
            "kit_less",
        ],
        order_by="name1 asc",
    )


@frappe.whitelist()
def get_program_summary(program_name):
    doc = frappe.get_doc("Program", program_name)
    batches = get_batches(program_name)
    course_levels = get_course_levels(program_name)
    return {
        "program": doc.program,
        "total_batches": len(batches),
        "active_batches": len([b for b in batches if b.active]),
        "total_course_levels": len(course_levels),
        "batches": batches,
        "course_levels": course_levels,
    }