import frappe
import json
import traceback
from frappe import _
from frappe.utils import now_datetime, add_to_date, get_datetime
import requests
import uuid
from tap_lms.glific_integration import (
    get_contact_by_phone,
    create_or_get_glific_group_for_batch,
    add_contact_to_group,
    start_contact_flow,
    _glific_post_with_401_retry,
)



@frappe.whitelist()
def trigger_onboarding_flow(onboarding_set, onboarding_stage, student_status=None):
    """
    Trigger Glific flows for students in a Backend Student Onboarding set for a specific OnboardingStage
    
    Args:
        onboarding_set (str): DocName of Backend Student Onboarding
        onboarding_stage (str): DocName of OnboardingStage
        student_status (str, optional): Filter students by their stage status
        
    Returns:
        dict: Results of the flow trigger operation
    """
    try:
        frappe.logger().info(f"Triggering onboarding flow - Set: {onboarding_set}, Stage: {onboarding_stage}, Status Filter: {student_status}")
        
        # Validate inputs
        if not onboarding_set or not onboarding_stage:
            frappe.throw(_("Both Backend Student Onboarding Set and Onboarding Stage are required"))
            
        if not student_status:
            frappe.throw(_("Student status is required"))
            
        # Get the onboarding stage details
        stage = frappe.get_doc("OnboardingStage", onboarding_stage)
        
        # Check if stage is active
        if not stage.is_active:
            frappe.throw(_("Selected Onboarding Stage is not active"))
            
        # Get the onboarding set details
        onboarding = frappe.get_doc("Backend Student Onboarding", onboarding_set)
        if onboarding.status != "Processed":
            frappe.throw(_("Selected Backend Student Onboarding Set is not in Processed status"))
        
        # Find the appropriate flow based on status
        flow_id = None
        flow_type = None
        
        # For backward compatibility, check if we need to use the old fields or new child table
        if hasattr(stage, 'stage_flows') and stage.stage_flows:
            # Use the new child table approach
            matching_flows = [flow for flow in stage.stage_flows if flow.student_status == student_status]
            if not matching_flows:
                frappe.throw(_("No flow configured for stage '{0}' with status '{1}'").format(
                    stage.name, student_status
                ))
            
            flow_id = matching_flows[0].glific_flow_id
            flow_type = matching_flows[0].flow_type
        else:
            # Legacy support - use the old fields
            if hasattr(stage, 'glific_flow_id') and stage.glific_flow_id:
                flow_id = stage.glific_flow_id
                flow_type = getattr(stage, 'glific_flow_type', 'Group')  # Default to Group if not specified
                
                frappe.logger().warning(
                    f"Using deprecated flow fields for stage {stage.name}. Please migrate to the new stage_flows structure."
                )
            else:
                frappe.throw(_("No flows configured for stage '{0}'").format(stage.name))
        
        if not flow_id:
            frappe.throw(_("Flow ID is missing for stage '{0}' with status '{1}'").format(
                stage.name, student_status
            ))
            
        # Create a background job for processing
        job_id = frappe.enqueue(
            _trigger_onboarding_flow_job,
            queue="long",
            timeout=3600,
            job_name=f"Trigger {student_status} Flow: {onboarding_set} - {onboarding_stage}",
            onboarding_set=onboarding_set,
            onboarding_stage=onboarding_stage,
            student_status=student_status,
            flow_id=flow_id,
            flow_type=flow_type
        )
        
        frappe.logger().info(f"Background job created with ID: {job_id}")
        return {"success": True, "job_id": job_id}
        
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(message=f"Error triggering onboarding flow: {str(e)}\n{error_traceback}", 
                        title="Onboarding Flow Trigger Error")
        frappe.throw(_("Error triggering onboarding flow: {0}").format(str(e)))



def _trigger_onboarding_flow_job(onboarding_set, onboarding_stage, student_status=None, flow_id=None, flow_type=None):
    """
    Background job for triggering Glific flows
    
    Args:
        onboarding_set (str): DocName of Backend Student Onboarding
        onboarding_stage (str): DocName of OnboardingStage
        student_status (str, optional): Filter students by their stage status
        flow_id (str): Glific flow ID to trigger
        flow_type (str): Type of flow (Group or Personal)
        
    Returns:
        dict: Results of the flow trigger operation
    """
    import time as _time
    from tap_lms.monitoring import record_job
    _t0 = _time.monotonic()
    _status = "success"
    _error = None

    try:
        frappe.logger().info(f"Starting background job for onboarding set: {onboarding_set}, stage: {onboarding_stage}, status: {student_status}")
        
        # Get stage details
        stage = frappe.get_doc("OnboardingStage", onboarding_stage)
        
        # Get onboarding set details
        onboarding = frappe.get_doc("Backend Student Onboarding", onboarding_set)
        
        # Get the Glific settings
        glific_settings = frappe.get_doc("Glific Settings")

        # CR-025 follow-up: NO pre-flight auth fetch. Every Glific POST inside
        # trigger_group_flow / start_contact_flow routes through
        # _glific_post_with_401_retry, which fetches + refreshes the token
        # per-call (and invalidates on 401). The old pre-flight guard returned
        # {"error": ...} on a transiently-stale token, which RQ counts as a
        # SUCCESSFUL job (L-056 silent failure). auth_token is passed as None —
        # it is unused downstream (kept only for signature compatibility).
        results = {}

        # Process based on flow type
        if flow_type == "Group":
            # Group flow processing with status filter
            results = trigger_group_flow(onboarding, stage, None, student_status, flow_id)
        else:
            # Individual flow processing with status filter
            results = trigger_individual_flows(onboarding, stage, None, student_status, flow_id)
        
        frappe.logger().info(f"Flow trigger job completed successfully: {results}")
        return results
        
    except Exception as e:
        _status = "error"
        _error = str(e)
        error_traceback = traceback.format_exc()
        frappe.log_error(message=f"Error in onboarding flow job: {str(e)}\n{error_traceback}",
                        title="Onboarding Flow Job Error")
        # L-056: this is an frappe.enqueue RQ job — re-raise after logging so
        # RQ records the failure (FailedJobRegistry + queue-depth watchers).
        # Returning {"error": ...} made every failure look like a successful
        # job. The page enqueues fire-and-forget (job_id only), so raising does
        # not affect the UI.
        raise
    finally:
        try:
            record_job(
                job_name="trigger_onboarding_flow_job",
                status=_status,
                duration_ms=(_time.monotonic() - _t0) * 1000,
                error=_error,
                onboarding_set=onboarding_set,
                onboarding_stage=onboarding_stage,
                student_status=student_status,
                flow_type=flow_type,
            )
        except Exception:
            pass





            

def trigger_group_flow(onboarding, stage, auth_token, student_status=None, flow_id=None):
    """
    Trigger Glific flow for a contact group

    Args:
        onboarding (Document): Backend Student Onboarding document
        stage (Document): OnboardingStage document
        auth_token (str): Glific API auth token
        student_status (str, optional): Filter students by their stage status
        flow_id (str, optional): Specific Glific flow ID to use

    Returns:
        dict: Results of the group flow trigger
    """
    try:
        frappe.logger().info(f"Triggering group flow for onboarding set: {onboarding.name}, stage: {stage.name}, status filter: {student_status}")

        # Use the provided flow_id instead of stage.glific_flow_id
        flow_id_to_use = flow_id

        if not flow_id_to_use:
            frappe.throw(_("No Glific flow ID available for this stage and status"))

        # Get or create contact group for this onboarding set
        contact_group_info = create_or_get_glific_group_for_batch(onboarding.name)

        if not contact_group_info:
            frappe.throw(_("Could not find or create contact group for this onboarding set"))

        contact_group = frappe.get_doc("GlificContactGroup", {"backend_onboarding_set": onboarding.name})

        # Prepare the GraphQL mutation for starting group flow
        mutation = """
        mutation startGroupFlow($flowId: ID!, $groupId: ID!, $defaultResults: Json!) {
            startGroupFlow(flowId: $flowId, groupId: $groupId, defaultResults: $defaultResults) {
                success
                errors {
                    key
                    message
                }
            }
        }
        """

        # Prepare variables for the GraphQL mutation
        variables = {
            "flowId": flow_id_to_use,  # Use the provided flow ID
            "groupId": contact_group.group_id,
            "defaultResults": json.dumps({
                "onboarding_stage": stage.name,
                "onboarding_set": onboarding.name,
                "student_status": student_status
            })
        }

        # Make the API call to Glific
        settings = frappe.get_doc("Glific Settings")
        payload = {
            "query": mutation,
            "variables": variables
        }

        frappe.logger().debug(f"Glific API request payload: {payload}")
        # CR-025: use 401-retry helper (was bare requests.post, no session, no timeout)
        response = _glific_post_with_401_retry(settings.api_url + "/api", payload)

        if response.status_code != 200:
            frappe.logger().error(f"Glific API error: Status {response.status_code}, Response: {response.text}")
            frappe.throw(_("Failed to communicate with Glific API: {0} - {1}").format(
                response.status_code, response.text))

        response_data = response.json()
        frappe.logger().debug(f"Glific API response: {response_data}")

        if response_data and response_data.get("data", {}).get("startGroupFlow", {}).get("success"):
            # Get students from this onboarding set with status filter
            students = get_students_from_onboarding(onboarding, stage.name, student_status)

            # Log how many students we found
            frappe.logger().info(f"Found {len(students)} students in onboarding set {onboarding.name} with status {student_status or 'any'}")

            # Update StudentStageProgress for all students in the group
            update_student_stage_progress_batch(students, stage)

            return {
                "group_flow_result": response_data.get("data", {}).get("startGroupFlow"),
                "group_count": len(students)
            }
        else:
            error_data = response_data.get("data", {}).get("startGroupFlow", {}).get("errors", [])
            if error_data and len(error_data) > 0:
                error_msg = error_data[0].get("message", "Unknown error")
            else:
                error_msg = "Unknown error"

            frappe.logger().error(f"Failed to trigger group flow: {error_msg}")
            frappe.throw(_("Failed to trigger group flow: {0}").format(error_msg))

    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(message=f"Error in group flow trigger: {str(e)}\n{error_traceback}",
                        title="Group Flow Trigger Error")
        frappe.throw(_("Error triggering group flow: {0}").format(str(e)))





def trigger_individual_flows(onboarding, stage, auth_token, student_status=None, flow_id=None):
    """
    Trigger Glific flows for individual students
    
    Args:
        onboarding (Document): Backend Student Onboarding document
        stage (Document): OnboardingStage document
        auth_token (str): Glific API auth token
        student_status (str, optional): Filter students by their stage status
        flow_id (str, optional): Specific Glific flow ID to use
        
    Returns:
        dict: Results of the individual flow triggers
    """
    try:
        frappe.logger().info(f"Triggering individual flows for onboarding set: {onboarding.name}, stage: {stage.name}, status filter: {student_status}")
        
        # Use the provided flow_id instead of stage.glific_flow_id
        flow_id_to_use = flow_id
        
        if not flow_id_to_use:
            frappe.throw(_("No Glific flow ID available for this stage and status"))
        
        # Get students from this onboarding set with status filter
        students = get_students_from_onboarding(onboarding, stage.name, student_status)
        
        if not students:
            frappe.logger().warning(f"No students found in onboarding set {onboarding.name} with status {student_status or 'any'}")
            frappe.throw(_("No students found in this onboarding set with the selected status"))
        
        # Process students in batches to avoid timeout
        batch_size = 10
        success_count = 0
        error_count = 0
        results = []
        
        for i in range(0, len(students), batch_size):
            batch = students[i:i+batch_size]
            
            for student in batch:
                # Skip if student doesn't have a Glific ID
                if not student.glific_id:
                    frappe.logger().warning(f"Student {student.name} does not have a Glific ID")
                    continue
                
                # Use the start_contact_flow function from glific_integration
                default_results = {
                    "onboarding_stage": stage.name,
                    "onboarding_set": onboarding.name,
                    "student_id": student.name,
                    "student_status": student_status
                }
                
                try:
                    frappe.logger().debug(f"Starting flow for student: {student.name1}, glific_id: {student.glific_id}")
                    success = start_contact_flow(flow_id_to_use, student.glific_id, default_results)
                    
                    if success:
                        # Update StudentStageProgress for this student
                        update_student_stage_progress(student, stage)
                        success_count += 1
                        results.append({
                            "student": student.name,
                            "student_name": student.name1,
                            "glific_id": student.glific_id,
                            "success": True
                        })
                        frappe.logger().debug(f"Flow started successfully for student: {student.name1}")
                    else:
                        error_count += 1
                        results.append({
                            "student": student.name,
                            "student_name": student.name1,
                            "glific_id": student.glific_id,
                            "success": False,
                            "error": "Failed to start flow"
                        })
                        frappe.logger().error(f"Failed to start flow for student: {student.name1}")
                except Exception as e:
                    error_count += 1
                    results.append({
                        "student": student.name,
                        "student_name": student.name1,
                        "glific_id": student.glific_id,
                        "success": False,
                        "error": str(e)
                    })
                    frappe.logger().error(f"Exception starting flow for student {student.name1}: {str(e)}")
            
            # Sleep between batches to avoid rate limiting
            frappe.db.commit()
            import time
            time.sleep(2)
        
        return {
            "individual_flow_results": results,
            "individual_count": success_count,
            "error_count": error_count
        }
            
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(message=f"Error in individual flow trigger: {str(e)}\n{error_traceback}", 
                        title="Individual Flow Trigger Error")
        frappe.throw(_("Error triggering individual flows: {0}").format(str(e)))


@frappe.whitelist()
def get_stage_flow_statuses(stage_id):
    """
    Get available status options for a given stage
    
    Args:
        stage_id (str): DocName of OnboardingStage
        
    Returns:
        dict: List of status values with configured flows
    """
    try:
        stage = frappe.get_doc("OnboardingStage", stage_id)
        
        # For new structure with child table
        if hasattr(stage, 'stage_flows') and stage.stage_flows:
            statuses = list(set([flow.student_status for flow in stage.stage_flows]))
            return {"statuses": statuses}
        
        # For legacy structure
        if hasattr(stage, 'glific_flow_id') and stage.glific_flow_id:
            # Default to all statuses for legacy flows
            return {"statuses": ["not_started", "assigned", "in_progress", "completed", "incomplete", "skipped"]}
        
        return {"statuses": []}
        
    except Exception as e:
        frappe.log_error(message=f"Error getting stage statuses: {str(e)}", 
                        title="Stage Status Error")
        return {"statuses": [], "error": str(e)}


def get_students_from_onboarding(onboarding, stage_name=None, student_status=None):
    """
    Get all student documents associated with a Backend Student Onboarding set
    with optional filtering by stage and status
    
    Args:
        onboarding (Document): Backend Student Onboarding document
        stage_name (str, optional): Name of the OnboardingStage to filter by
        student_status (str, optional): Status to filter by (not_started, assigned, etc.)
        
    Returns:
        list: List of Student documents
    """
    student_list = []
    
    try:
        frappe.logger().debug(f"Getting students from onboarding set: {onboarding.name}, stage: {stage_name}, status: {student_status}")
        
        # Get all successful backend students from the onboarding set
        backend_students = frappe.get_all(
            "Backend Students", 
            filters={
                "parent": onboarding.name,
                "processing_status": "Success"
            },
            fields=["student_id"]
        )
        
        if not backend_students:
            frappe.logger().warning(f"No successful backend students found for onboarding set {onboarding.name}")
            return []
            
        frappe.logger().debug(f"Found {len(backend_students)} backend students records")
        
        # For each student, check if they have the required status in the given stage
        for bs in backend_students:
            if bs.student_id:
                try:
                    # First check if the student exists
                    student = frappe.get_doc("Student", bs.student_id)
                    
                    # If we're filtering by stage and status
                    if stage_name and student_status:
                        # Check if there's a matching stage progress record
                        stage_progress = frappe.get_all(
                            "StudentStageProgress",
                            filters={
                                "student": student.name,
                                "stage_type": "OnboardingStage",
                                "stage": stage_name,
                                "status": student_status
                            },
                            fields=["name"]
                        )
                        
                        if stage_progress:
                            student_list.append(student)
                            
                    # If we're filtering only by stage (any status)
                    elif stage_name:
                        # Check if there's any stage progress record for this stage
                        stage_progress = frappe.get_all(
                            "StudentStageProgress",
                            filters={
                                "student": student.name,
                                "stage_type": "OnboardingStage",
                                "stage": stage_name
                            },
                            fields=["name"]
                        )
                        
                        if stage_progress:
                            student_list.append(student)
                            
                    # If no stage specified (or if no stage/status filters)
                    else:
                        student_list.append(student)
                        
                except Exception as e:
                    frappe.logger().error(f"Error fetching student {bs.student_id}: {str(e)}")
    
        # Extra handling for 'not_started' status - special case as records might not exist yet
        if stage_name and student_status == "not_started":
            # For "not_started" status, we need to find students who don't have a record for this stage
            for bs in backend_students:
                if bs.student_id:
                    try:
                        student = frappe.get_doc("Student", bs.student_id)
                        
                        # Check if there's any stage progress record for this stage
                        stage_progress = frappe.get_all(
                            "StudentStageProgress",
                            filters={
                                "student": student.name,
                                "stage_type": "OnboardingStage",
                                "stage": stage_name
                            },
                            fields=["name"]
                        )
                        
                        # If no stage progress record exists, this student is in "not_started" status
                        if not stage_progress and student.name not in [s.name for s in student_list]:
                            student_list.append(student)
                            
                    except Exception as e:
                        frappe.logger().error(f"Error checking not_started status for student {bs.student_id}: {str(e)}")
        
        frappe.logger().debug(f"Retrieved {len(student_list)} valid student documents after filtering")
        return student_list
        
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(
            message=f"Error getting students from onboarding: {str(e)}\n{error_traceback}",
            title="Get Students Error"
        )
        return []

def update_student_stage_progress(student, stage):
    """
    Create or update StudentStageProgress for a student and stage
    
    Args:
        student (Document): Student document
        stage (Document): OnboardingStage document
    """
    try:
        # Check if StudentStageProgress already exists
        existing = frappe.get_all(
            "StudentStageProgress",
            filters={
                "student": student.name,
                "stage_type": "OnboardingStage",
                "stage": stage.name
            }
        )
        
        timestamp = now_datetime()
        
        if existing:
            # Update existing record
            progress = frappe.get_doc("StudentStageProgress", existing[0].name)
            
            # Only update if not already completed or in progress
            if progress.status in ["not_started", "incomplete"]:
                progress.status = "assigned"
                progress.last_activity_timestamp = timestamp
                if not progress.start_timestamp:
                    progress.start_timestamp = timestamp
                progress.save()
                frappe.logger().debug(f"Updated existing progress record for student {student.name}")
        else:
            # Create new record
            progress = frappe.new_doc("StudentStageProgress")
            progress.student = student.name
            progress.stage_type = "OnboardingStage"
            progress.stage = stage.name
            progress.status = "assigned"
            progress.start_timestamp = timestamp
            progress.last_activity_timestamp = timestamp
            progress.insert()
            frappe.logger().debug(f"Created new progress record for student {student.name}")
        
        frappe.db.commit()
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(
            message=f"Error updating stage progress for student {student.name}: {str(e)}\n{error_traceback}",
            title="Update Stage Progress Error"
        )


def update_student_stage_progress_batch(students, stage):
    """
    Update StudentStageProgress for multiple students in batch
    
    Args:
        students (list): List of Student documents
        stage (Document): OnboardingStage document
    """
    if not students:
        frappe.logger().warning("No students provided to update_student_stage_progress_batch")
        return
    
    timestamp = now_datetime()
    updated_count = 0
    created_count = 0
    error_count = 0
    
    try:
        frappe.logger().info(f"Updating stage progress for {len(students)} students")
        
        for student in students:
            try:
                # Check if StudentStageProgress already exists
                existing = frappe.get_all(
                    "StudentStageProgress",
                    filters={
                        "student": student.name,
                        "stage_type": "OnboardingStage",
                        "stage": stage.name
                    }
                )
                
                if existing:
                    # Update existing record only if not already completed or in progress
                    progress = frappe.get_doc("StudentStageProgress", existing[0].name)
                    if progress.status in ["not_started", "incomplete"]:
                        progress.status = "assigned"
                        progress.last_activity_timestamp = timestamp
                        if not progress.start_timestamp:
                            progress.start_timestamp = timestamp
                        progress.save()
                        updated_count += 1
                else:
                    # Create new record
                    progress = frappe.new_doc("StudentStageProgress")
                    progress.student = student.name
                    progress.stage_type = "OnboardingStage"
                    progress.stage = stage.name
                    progress.status = "assigned"
                    progress.start_timestamp = timestamp
                    progress.last_activity_timestamp = timestamp
                    progress.insert()
                    created_count += 1
            except Exception as e:
                error_count += 1
                frappe.logger().error(f"Error updating stage progress for student {student.name}: {str(e)}")
        
        frappe.db.commit()
        frappe.logger().info(f"Stage progress update complete. Updated: {updated_count}, Created: {created_count}, Errors: {error_count}")
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(
            message=f"Error in batch update of stage progress: {str(e)}\n{error_traceback}",
            title="Batch Update Stage Progress Error"
        )

@frappe.whitelist()
def get_job_status(job_id):
    """
    Get the status of a background job
    
    Args:
        job_id (str): ID of the background job
        
    Returns:
        dict: Job status and results
    """
    if not job_id:
        return {"status": "unknown"}
    
    try:
        from frappe.utils.background_jobs import get_job_status
        
        status = get_job_status(job_id)
        frappe.logger().debug(f"Job status for {job_id}: {status}")
        
        if status == "failed":
            return {"status": "failed"}
        
        if status == "finished":
            # Try to get the job results
            from rq.job import Job
            from frappe.utils.background_jobs import get_redis_conn
            
            redis_conn = get_redis_conn()
            if redis_conn:
                try:
                    job = Job.fetch(job_id, connection=redis_conn)
                    if job and job.result:
                        return {"status": "complete", "results": job.result}
                except Exception as e:
                    frappe.logger().error(f"Error fetching job results: {str(e)}")
            
            return {"status": "complete"}
        
        return {"status": status}
    
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(
            message=f"Error checking job status: {str(e)}\n{error_traceback}", 
            title="Job Status Check Error"
        )
        return {"status": "error", "message": str(e)}

@frappe.whitelist()
def get_onboarding_progress_report(set=None, stage=None, status=None):
    """
    Get a report of onboarding stage progress
    
    Args:
        set (str, optional): Filter by Backend Student Onboarding set
        stage (str, optional): Filter by OnboardingStage
        status (str, optional): Filter by status
        
    Returns:
        dict: Report data with summary and details
    """
    try:
        frappe.logger().debug(f"Generating onboarding progress report. Set: {set}, Stage: {stage}, Status: {status}")
        
        filters = {
            "stage_type": "OnboardingStage"
        }
        
        if stage:
            filters["stage"] = stage
            
        if status:
            filters["status"] = status
        
        # Get all StudentStageProgress records matching filters
        progress_records = frappe.get_all(
            "StudentStageProgress",
            filters=filters,
            fields=["name", "student", "stage", "status", "start_timestamp", 
                    "last_activity_timestamp", "completion_timestamp"]
        )
        
        frappe.logger().debug(f"Found {len(progress_records)} progress records matching filters")
        
        # Initialize summary counts
        summary = {
            "total": 0,
            "not_started": 0,
            "assigned": 0,
            "in_progress": 0,
            "completed": 0,
            "incomplete": 0,
            "skipped": 0
        }
        
        # Process records to get details
        details = []
        for record in progress_records:
            try:
                # Get student details
                student = frappe.get_doc("Student", record.student)
                
                # If filtering by set, check if student belongs to this onboarding set
                if set:
                    # Get backend students from this set
                    backend_students = frappe.get_all(
                        "Backend Students",
                        filters={
                            "parent": set,
                            "student_id": student.name
                        }
                    )
                    
                    if not backend_students:
                        continue
                
                # Get stage details
                stage_doc = frappe.get_doc("OnboardingStage", record.stage)
                
                # Add to details
                details.append({
                    "student": student.name,
                    "student_name": student.name1 or "Unknown",
                    "phone": student.phone or "No Phone",
                    "stage": stage_doc.name,
                    "status": record.status or "not_started",
                    "start_timestamp": record.start_timestamp,
                    "last_activity_timestamp": record.last_activity_timestamp,
                    "completion_timestamp": record.completion_timestamp
                })
                
                # Update summary counts
                summary["total"] += 1
                if record.status and record.status in summary:
                    summary[record.status] += 1
                else:
                    summary["not_started"] += 1
            except Exception as e:
                frappe.logger().error(f"Error processing record {record.name}: {str(e)}")
                continue
                
        # Special handling for students with no stage progress record (not_started)
        if stage and (not status or status == "not_started"):
            # Find students who don't have a stage record for this stage
            if set:
                # Find students in this onboarding set
                backend_students = frappe.get_all(
                    "Backend Students",
                    filters={
                        "parent": set,
                        "processing_status": "Success"
                    },
                    fields=["student_id"]
                )
                
                for bs in backend_students:
                    if bs.student_id:
                        try:
                            # Check if student already has a stage progress record
                            existing_record = frappe.get_all(
                                "StudentStageProgress",
                                filters={
                                    "student": bs.student_id,
                                    "stage_type": "OnboardingStage",
                                    "stage": stage
                                }
                            )
                            
                            # If no record, this student is in "not_started" status
                            if not existing_record:
                                student = frappe.get_doc("Student", bs.student_id)
                                stage_doc = frappe.get_doc("OnboardingStage", stage)
                                
                                # Add to details
                                details.append({
                                    "student": student.name,
                                    "student_name": student.name1 or "Unknown",
                                    "phone": student.phone or "No Phone",
                                    "stage": stage_doc.name,
                                    "status": "not_started",
                                    "start_timestamp": None,
                                    "last_activity_timestamp": None,
                                    "completion_timestamp": None
                                })
                                
                                # Update summary counts
                                summary["total"] += 1
                                summary["not_started"] += 1
                        except Exception as e:
                            frappe.logger().error(f"Error processing not_started student {bs.student_id}: {str(e)}")
        
        frappe.logger().debug(f"Generated report with {len(details)} detail records")
        return {
            "summary": summary,
            "details": details
        }
        
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(
            message=f"Error generating onboarding progress report: {str(e)}\n{error_traceback}", 
            title="Onboarding Progress Report Error"
        )
        frappe.throw(_("Error generating onboarding progress report: {0}").format(str(e)))


def update_incomplete_stages():
    """
    Daily scheduled task to update the status of student stages that have been assigned but haven't been started within a reasonable timeframe
    """
    import time as _time
    from tap_lms.monitoring import record_job
    _t0 = _time.monotonic()
    _status = "success"
    _error = None
    _found_count = 0
    _updated_count = 0

    try:
        frappe.logger().info("Running update_incomplete_stages scheduled task")

        # Find students who have been in 'assigned' status for more than 3 days
        three_days_ago = add_to_date(now_datetime(), days=-3)

        # Get all progress records that are still in assigned status
        assigned_records = frappe.get_all(
            "StudentStageProgress",
            filters={
                "stage_type": "OnboardingStage",
                "status": "assigned",
                "start_timestamp": ["<", three_days_ago]
            },
            fields=["name", "student", "stage", "start_timestamp"]
        )
        _found_count = len(assigned_records)

        frappe.logger().info(f"Found {len(assigned_records)} records to mark as incomplete")

        # Process these records
        updated_count = 0
        for record in assigned_records:
            try:
                # Mark as incomplete if no activity after assignment
                progress = frappe.get_doc("StudentStageProgress", record.name)
                # Update status to incomplete
                progress.status = "incomplete"
                progress.save()
                updated_count += 1
            except Exception as e:
                frappe.logger().error(f"Error updating record {record.name}: {str(e)}")
        _updated_count = updated_count

        frappe.db.commit()

        # Log a message about the update
        frappe.logger().info(f"Updated {updated_count} records from 'assigned' to 'incomplete' status")
    except Exception as e:
        _status = "error"
        _error = str(e)
        error_traceback = traceback.format_exc()
        frappe.log_error(
            message=f"Error in updating incomplete stages: {str(e)}\n{error_traceback}",
            title="Update Incomplete Stages Error"
        )
    finally:
        try:
            record_job(
                job_name="update_incomplete_stages",
                status=_status,
                duration_ms=(_time.monotonic() - _t0) * 1000,
                error=_error,
                found_count=_found_count,
                updated_count=_updated_count,
            )
        except Exception:
            pass
