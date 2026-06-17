import frappe
import requests
from frappe.utils.background_jobs import enqueue
from .glific_integration import (
    optin_contact,
    start_contact_flow,
    create_or_get_teacher_group_for_batch,
    add_contact_to_group
)
# Remove the import: from .api import get_active_batch_for_school

def process_glific_actions(teacher_id, phone, first_name, school, school_name, language, model_name, batch_name, batch_id):
    try:
        # Optin the contact
        try:
            optin_success = optin_contact(phone, first_name)
        except requests.exceptions.RequestException as e:
            frappe.log_error(
                title="Glific timeout (degraded)",
                message=f"process_glific_actions: optin_contact timed out for {phone}: {e}",
            )
            optin_success = False
        if not optin_success:
            frappe.logger().error(f"Failed to opt in contact for teacher {teacher_id}")
            return

        # Get the Glific ID
        glific_id = frappe.db.get_value("Teacher", teacher_id, "glific_id")
        if not glific_id:
            frappe.logger().error(f"Glific ID not found for teacher {teacher_id}")
            return

        # Create or get the teacher group for this batch
        # Now we use the passed batch_name and batch_id directly
        if batch_id and batch_id != "no_active_batch_id" and batch_name:
            try:
                teacher_group = create_or_get_teacher_group_for_batch(batch_name, batch_id)
                
                if teacher_group and teacher_group.get("group_id"):
                    # Add the teacher to the group
                    try:
                        group_added = add_contact_to_group(glific_id, teacher_group["group_id"])
                    except requests.exceptions.RequestException as e:
                        frappe.log_error(
                            title="Glific timeout (degraded)",
                            message=f"process_glific_actions: add_contact_to_group timed out for teacher {teacher_id}: {e}",
                        )
                        group_added = False
                    if group_added:
                        frappe.logger().info(f"Teacher {teacher_id} added to group {teacher_group['label']}")
                    else:
                        frappe.logger().warning(f"Failed to add teacher {teacher_id} to group {teacher_group['label']}")
                else:
                    frappe.logger().warning(f"Could not create/get teacher group for batch {batch_id}")
                    
            except Exception as e:
                # Log error but don't stop the flow
                frappe.logger().error(f"Error managing teacher group for teacher {teacher_id}: {str(e)}")
        else:
            frappe.logger().info(f"No valid batch_id for teacher {teacher_id}, skipping group assignment")

        # Start the "Teacher Web Onboarding Flow" in Glific
        flow = frappe.db.get_value("Glific Flow", {"label": "Teacher Web Onboarding Flow"}, "flow_id")
        if flow:
            default_results = {
                "teacher_id": teacher_id,
                "school_id": school,
                "school_name": school_name,
                "language": language,
                "model": model_name
            }
            flow_started = start_contact_flow(flow, glific_id, default_results)
            if flow_started:
                frappe.logger().info(f"Onboarding flow started for teacher {teacher_id}")
            else:
                frappe.logger().error(f"Failed to start onboarding flow for teacher {teacher_id}")
        else:
            frappe.logger().error("Glific flow not found")

    except Exception as e:
        frappe.logger().error(f"Error in process_glific_actions for teacher {teacher_id}: {str(e)}")

def enqueue_glific_actions(teacher_id, phone, first_name, school, school_name, language, model_name, batch_name, batch_id):
    enqueue(
        process_glific_actions,
        queue="short",
        timeout=300,
        teacher_id=teacher_id,
        phone=phone,
        first_name=first_name,
        school=school,
        school_name=school_name,
        language=language,
        model_name=model_name,
        batch_name=batch_name,
        batch_id=batch_id
    )
