# glific_batch_id_update.py
# New file to handle updating existing Glific contacts with batch_id field for specific Backend Student Onboarding sets

import frappe
import requests
import json
from datetime import datetime, timezone
from .glific_integration import get_glific_settings, _glific_post_with_401_retry
import time
from frappe.utils.background_jobs import enqueue


def get_student_batch_id(student_name, backend_student_batch):
    """
    Get the batch_id for a student from their Backend Students record
    Returns the batch value from Backend Students record
    """
    try:
        # Check if student exists first to avoid transaction errors
        if not frappe.db.exists("Student", student_name):
            frappe.logger().error(f"Student document not found for batch_id check: {student_name}")
            return None
        
        # Return the batch from backend student record (passed as parameter)
        if backend_student_batch:
            return backend_student_batch
        else:
            return None
            
    except Exception as e:
        frappe.logger().error(f"Error getting batch_id for student {student_name}: {str(e)}")
        return None


def update_specific_set_contacts_with_batch_id(onboarding_set_name, batch_size=50):
    """
    Update existing Glific contacts to add batch_id field for a specific Backend Student Onboarding set
    
    Args:
        onboarding_set_name: Specific Backend Student Onboarding set name to process (required)
        batch_size: Number of students to process at once (default: 50)
    """
    
    if not onboarding_set_name:
        return {"error": "Backend Student Onboarding set name is required"}
    
    # Get the specific Backend Student Onboarding set
    try:
        onboarding_set = frappe.get_doc("Backend Student Onboarding", onboarding_set_name)
    except frappe.DoesNotExistError:
        return {"error": f"Backend Student Onboarding set '{onboarding_set_name}' not found"}
    
    if onboarding_set.status != "Processed":
        return {"error": f"Set '{onboarding_set_name}' status is '{onboarding_set.status}', not 'Processed'"}
    
    frappe.logger().info(f"Processing Backend Student Onboarding set for batch_id: {onboarding_set.set_name}")
    
    # Get all Backend Students from this set that were successfully processed
    backend_students = frappe.get_all(
        "Backend Students",
        filters={
            "parent": onboarding_set_name,
            "processing_status": "Success",
            "student_id": ["not in", ["", None]]  # Only students with valid student_id
        },
        fields=["name", "student_name", "phone", "student_id", "batch", "batch_skeyword"],
        limit=batch_size  # Add batch size limit
    )
    
    if not backend_students:
        return {"message": f"No successfully processed students found in set {onboarding_set.set_name}"}
    
    total_updated = 0
    total_skipped = 0
    total_errors = 0
    total_processed = 0
    
    # Process each backend student
    for backend_student_entry in backend_students:
        try:
            # Get the full Backend Students document
            backend_student = frappe.get_doc("Backend Students", backend_student_entry.name)
            
            student_id = backend_student.student_id
            student_name = backend_student.student_name
            phone = backend_student.phone
            batch_id = backend_student.batch  # Get batch from backend student
            
            # Get the actual Student document to get glific_id
            try:
                # Check if student exists first to avoid transaction errors
                if not frappe.db.exists("Student", student_id):
                    frappe.logger().error(f"Student document not found: {student_id}")
                    total_errors += 1
                    total_processed += 1
                    continue
                
                student_doc = frappe.get_doc("Student", student_id)
                glific_id = student_doc.glific_id
            except Exception as e:
                frappe.logger().error(f"Error getting student document {student_id}: {str(e)}")
                total_errors += 1
                total_processed += 1
                continue
            
            if not glific_id:
                frappe.logger().warning(f"No Glific ID found for student {student_name} ({student_id})")
                total_errors += 1
                total_processed += 1
                continue
            
            frappe.logger().info(f"Processing student for batch_id: {student_name} (Glific ID: {glific_id})")
            
            # Get current contact from Glific
            settings = get_glific_settings()
            url = f"{settings.api_url}/api"

            fetch_payload = {
                "query": """
                query contact($id: ID!) {
                  contact(id: $id) {
                    contact {
                      id
                      name
                      phone
                      fields
                    }
                  }
                }
                """,
                "variables": {
                    "id": glific_id
                }
            }

            # Fetch current contact — CR-025: use 401-retry helper
            try:
                response = _glific_post_with_401_retry(url, fetch_payload)
            except Exception as req_e:
                frappe.logger().error(f"Failed to fetch contact {glific_id} for student {student_name}: {req_e}")
                total_errors += 1
                total_processed += 1
                continue
            if response.status_code != 200:
                frappe.logger().error(f"Failed to fetch contact {glific_id} for student {student_name}")
                total_errors += 1
                total_processed += 1
                continue
                
            data = response.json()
            if "errors" in data:
                frappe.logger().error(f"Glific API Error fetching contact {glific_id}: {data['errors']}")
                total_errors += 1
                total_processed += 1
                continue
                
            contact_data = data.get("data", {}).get("contact", {}).get("contact")
            if not contact_data:
                frappe.logger().error(f"Contact not found for ID: {glific_id}")
                total_errors += 1
                total_processed += 1
                continue
            
            # Parse existing fields
            existing_fields = {}
            if contact_data.get("fields"):
                try:
                    existing_fields = json.loads(contact_data.get("fields", "{}"))
                except json.JSONDecodeError:
                    frappe.logger().error(f"Failed to parse fields JSON for contact {glific_id}")
                    existing_fields = {}
            
            # Check if batch_id field already exists
            batch_id_exists = "batch_id" in existing_fields
            if batch_id_exists:
                old_value = existing_fields["batch_id"]["value"]
                frappe.logger().info(f"batch_id exists for contact {glific_id} with value: {old_value}")
                # We'll update it instead of skipping
            
            # Get batch_id value for this student
            batch_id_value = get_student_batch_id(student_id, batch_id)
            
            # Skip if no batch_id found
            if not batch_id_value:
                frappe.logger().warning(f"No batch_id found for student {student_name}")
                total_skipped += 1
                total_processed += 1
                continue
            
            # Log the batch_id check result
            frappe.logger().info(f"Student {student_name} batch_id value: {batch_id_value}")
            
            # Add or update batch_id field
            existing_fields["batch_id"] = {
                "value": batch_id_value,
                "type": "string",
                "inserted_at": datetime.now(timezone.utc).isoformat()
            }
            
            # Log what we're doing
            if batch_id_exists:
                if old_value != batch_id_value:
                    frappe.logger().info(f"Updating batch_id for {student_name}: {old_value} → {batch_id_value}")
                else:
                    frappe.logger().info(f"Refreshing batch_id for {student_name}: {batch_id_value} (no change)")
            else:
                frappe.logger().info(f"Adding batch_id for {student_name}: {batch_id_value}")
            
            # Update contact with new fields
            update_payload = {
                "query": """
                mutation updateContact($id: ID!, $input:ContactInput!) {
                  updateContact(id: $id, input: $input) {
                    contact {
                      id
                      name
                      fields
                    }
                    errors {
                      key
                      message
                    }
                  }
                }
                """,
                "variables": {
                    "id": glific_id,
                    "input": {
                        "name": contact_data.get("name", student_name),
                        "fields": json.dumps(existing_fields)
                    }
                }
            }
            
            # Execute update — CR-025: use 401-retry helper
            try:
                update_response = _glific_post_with_401_retry(url, update_payload)
            except Exception as req_e:
                frappe.logger().error(f"Failed to update contact {glific_id}: {req_e}")
                total_errors += 1
                total_processed += 1
                continue
            if update_response.status_code == 200:
                update_data = update_response.json()
                if "errors" not in update_data and update_data.get("data", {}).get("updateContact", {}).get("contact"):
                    contact_updated = update_data.get("data", {}).get("updateContact", {}).get("contact")
                    
                    # Check if we actually updated vs refreshed the same value
                    if batch_id_exists and old_value == batch_id_value:
                        frappe.logger().info(f"Refreshed contact {glific_id} - batch_id value unchanged: {batch_id_value}")
                        # Count as updated since we processed it
                        total_updated += 1
                    elif batch_id_exists and old_value != batch_id_value:
                        frappe.logger().info(f"Updated contact {glific_id} - batch_id value changed: {old_value} → {batch_id_value}")
                        total_updated += 1
                    else:
                        frappe.logger().info(f"Added batch_id to contact {glific_id}: {batch_id_value}")
                        total_updated += 1
                else:
                    frappe.logger().error(f"Failed to update contact {glific_id}: {update_data}")
                    total_errors += 1
            else:
                frappe.logger().error(f"Failed to update contact {glific_id}. Status: {update_response.status_code}")
                total_errors += 1
            
            total_processed += 1
                
        except Exception as e:
            frappe.logger().error(f"Exception processing backend student {backend_student_entry.name}: {str(e)}")
            total_errors += 1
            total_processed += 1
            continue
    
    result = {
        "set_name": onboarding_set.set_name,
        "updated": total_updated,
        "skipped": total_skipped,
        "errors": total_errors,
        "total_processed": total_processed
    }
    
    frappe.logger().info(f"Batch ID update completed for set {onboarding_set.set_name}. Updated: {total_updated}, Skipped: {total_skipped}, Errors: {total_errors}, Total Processed: {total_processed}")
    return result


@frappe.whitelist()
def run_batch_id_update_for_specific_set(onboarding_set_name, batch_size=10):
    """
    Whitelist function to run the batch_id update process for a specific Backend Student Onboarding set
    
    Args:
        onboarding_set_name: Backend Student Onboarding set name to process (required)
        batch_size: Number of students to process at once (default: 10)
    """
    if not onboarding_set_name:
        return "Error: Backend Student Onboarding set name is required"
    
    try:
        # Start a new transaction to handle any database errors
        frappe.db.begin()
        result = update_specific_set_contacts_with_batch_id(onboarding_set_name, int(batch_size))
        frappe.db.commit()
        
        if "error" in result:
            return f"Error: {result['error']}"
        elif "message" in result:
            return result["message"]
        else:
            return f"Process completed for set '{result['set_name']}'. Updated: {result['updated']}, Skipped: {result['skipped']}, Errors: {result['errors']}, Total Processed: {result['total_processed']}"
    except Exception as e:
        frappe.db.rollback()
        frappe.logger().error(f"Error in run_batch_id_update_for_specific_set: {str(e)}")
        return f"Error occurred: {str(e)}"


def process_multiple_sets_batch_id(set_names, batch_size=50):
    """
    Simple background processor for multiple sets (batch_id update)
    """
    results = []

    for i, set_name in enumerate(set_names, 1):
        frappe.logger().info(f"Processing set {i}/{len(set_names)} for batch_id: {set_name}")

        try:
            # Process all students in this set
            total_updated = 0
            total_errors = 0
            total_skipped = 0
            batch_count = 0

            while True:
                batch_count += 1
                result = update_specific_set_contacts_with_batch_id(set_name, batch_size)

                if "error" in result:
                    frappe.logger().error(f"Error in {set_name}: {result['error']}")
                    break
                elif "message" in result:
                    frappe.logger().info(f"Set {set_name}: {result['message']}")
                    break
                else:
                    total_updated += result['updated']
                    total_errors += result['errors']
                    total_skipped += result['skipped']

                    # If no students processed, we're done
                    if result['total_processed'] == 0:
                        break

                time.sleep(1)  # Brief pause between batches

                if batch_count > 20:  # Safety limit
                    frappe.logger().warning(f"Reached batch limit for {set_name}")
                    break

            results.append({
                "set_name": set_name,
                "updated": total_updated,
                "skipped": total_skipped,
                "errors": total_errors,
                "status": "completed"
            })

            frappe.logger().info(f"Completed {set_name}: {total_updated} updated, {total_skipped} skipped, {total_errors} errors")

        except Exception as e:
            frappe.logger().error(f"Exception in {set_name}: {str(e)}")
            results.append({
                "set_name": set_name,
                "updated": 0,
                "skipped": 0,
                "errors": 1,
                "status": "error",
                "error": str(e)
            })

    # Log final summary
    total_updated = sum(r['updated'] for r in results)
    total_skipped = sum(r['skipped'] for r in results)
    total_errors = sum(r['errors'] for r in results)
    frappe.logger().info(f"All sets completed for batch_id: {total_updated} updated, {total_skipped} skipped, {total_errors} errors")

    return results


@frappe.whitelist()
def process_multiple_sets_batch_id_background(set_names):
    """
    Function to process multiple sets for batch_id update in background

    Args:
        set_names: List of set names (can be list or comma-separated string)
    """

    # Handle string input
    if isinstance(set_names, str):
        set_names = [name.strip() for name in set_names.split(',')]

    # Start background job
    job = enqueue(
        process_multiple_sets_batch_id,
        queue='long',
        timeout=7200,  # 2 hours
        set_names=set_names,
        batch_size=50
    )

    return f"Started processing {len(set_names)} sets for batch_id update in background. Job ID: {job.id}"


@frappe.whitelist()
def get_backend_onboarding_sets_for_batch_id():
    """
    Get list of all processed Backend Student Onboarding sets for batch_id update
    """
    sets = frappe.get_all(
        "Backend Student Onboarding",
        filters={"status": "Processed"},
        fields=["name", "set_name", "processed_student_count", "upload_date"],
        order_by="upload_date desc"
    )
    return sets