import frappe
import json
from frappe import _
from frappe.utils import now_datetime, today, get_datetime
import traceback

# Task #1 (2026-05-28): journey APIs deprecated pre-launch — see
# tap_lms/journey/_deprecation.py for the shared response helper. Old
# function bodies are preserved below `_DEPRECATED_*` markers so the
# legacy code is still readable but no longer reachable by Glific
# callers. Internal helpers like handle_stage_event, find_student, etc.
# are left intact because they may be imported elsewhere.
from tap_lms.journey._deprecation import journey_deprecated_response


@frappe.whitelist(allow_guest=False)
def track_interaction(**_kw):
    """[DEPRECATED 2026-05-28] Was a stage-based webhook handler for
    tracking student interactions. Replaced by state-machine transitions
    in summer_program/state_machine.py. Returns a deprecation envelope
    immediately — no side effects."""
    return journey_deprecated_response("track_interaction", _kw)


# _DEPRECATED_track_interaction_original ============================
# Original body preserved for one release cycle in case any helper
# below needs to be lifted out. Not callable from Glific anymore.
def _DEPRECATED_track_interaction_original():
    try:
        # Get the request data
        if frappe.request.method != "POST":
            return {"success": False, "message": "Only POST method is supported"}
            
        data = frappe.request.get_json()
        
        # Authentication check
        if frappe.session.user == 'Guest':
            frappe.throw(_("Authentication required"), frappe.AuthenticationError)
        
        # Extract basic information
        event_type = data.get('event_type')
        contact_info = data.get('contact', {})
        
        # REQUIRED: Direct stage references
        stage_id = data.get('stage_id')  # OnboardingStage.stage_name or LearningStage name
        stage_type = data.get('stage_type')
        
        # Extract course context
        course_context = data.get('course_context')
        
        # Content and progress information
        content_info = data.get('content', {})
        progress_info = data.get('progress', {})
        
        # Validate required data
        if not event_type:
            return {"success": False, "message": "Missing required field: event_type"}
            
        if not contact_info.get('id') and not contact_info.get('phone'):
            return {"success": False, "message": "Contact ID or phone number is required"}
        
        if not stage_id or not stage_type:
            return {"success": False, "message": "Both stage_id and stage_type are required"}
        
        # Find the student
        student = find_student(contact_info)
        if not student:
            frappe.log_error(
                f"Student not found for contact: {json.dumps(contact_info)}", 
                "Journey Tracking Error"
            )
            return {"success": False, "message": "Student not found"}
        
        # Get stage document
        stage = get_stage_by_stage_name(stage_id, stage_type)
        if not stage:
            return {"success": False, "message": f"Stage '{stage_id}' of type '{stage_type}' not found"}
        
        # Derive status from event type
        new_status = derive_status_from_event(event_type)
        
        # Create interaction log
        interaction_log = create_interaction_log(
            student, stage, stage_type, event_type, 
            content_info.get('message', {}), progress_info, course_context
        )
        
        # Handle the stage event
        result = handle_stage_event(
            student, stage, stage_type, new_status, event_type, 
            progress_info, course_context
        )
        
        # Add interaction log info to result
        if result.get("success"):
            if "data" not in result:
                result["data"] = {}
            result["data"]["interaction_log_id"] = interaction_log.name if interaction_log else None
        
        return result
            
    except frappe.AuthenticationError as e:
        frappe.log_error(f"Authentication error: {str(e)}", "Journey Tracking Error")
        return {"success": False, "message": str(e)}
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(f"Error tracking interaction: {str(e)}\n{error_traceback}", "Journey Tracking Error")
        return {"success": False, "message": str(e)}

@frappe.whitelist(allow_guest=False)
def update_student_stage(**_kw):
    """[DEPRECATED 2026-05-28] Was a direct API endpoint for external apps
    to update student stages. Replaced by summer_program/dev_tools.update_student_state
    for QA use and by canonical state-machine transitions for production.
    Returns a deprecation envelope immediately — no side effects."""
    return journey_deprecated_response("update_student_stage", _kw)


# _DEPRECATED_update_student_stage_original =========================
# Original body preserved for one release cycle. Not callable from Glific.
def _DEPRECATED_update_student_stage_original(student_id, stage_name,
                                                event_type="manual_assignment",
                                                course_context=None):
    try:
        # Find student
        if isinstance(student_id, dict):
            student = find_student(student_id)
        else:
            student = find_student_by_id(student_id)
            
        if not student:
            return {"success": False, "message": "Student not found"}
        
        # Determine stage type and get stage document
        stage_doc, stage_type = get_stage_document_by_name(stage_name)
        if not stage_doc:
            return {"success": False, "message": f"Stage '{stage_name}' not found"}
        
        # Derive status from event type
        new_status = derive_status_from_event(event_type)
        
        # Handle the stage event
        return handle_stage_event(
            student, stage_doc, stage_type, new_status, event_type, 
            {}, course_context
        )
        
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(f"Error updating student stage: {str(e)}\n{error_traceback}", "External Stage Update Error")
        return {"success": False, "message": str(e)}

def handle_stage_event(student, stage, stage_type, new_status, event_type, progress_info, course_context=None):
    """
    Core handler for stage events
    """
    try:
        # Get current stage progress
        current_progress = get_current_stage_progress(student, stage, stage_type, course_context)
        
        if current_progress:
            # Update existing stage
            result = handle_existing_stage_update(
                student, stage, stage_type, current_progress, new_status, 
                event_type, progress_info, course_context
            )
        else:
            # New stage assignment  
            result = handle_new_stage_assignment(
                student, stage, stage_type, new_status, event_type, 
                progress_info, course_context
            )
        
        return result
        
    except Exception as e:
        error_traceback = traceback.format_exc()
        frappe.log_error(f"Error in stage event handler: {str(e)}\n{error_traceback}", "Stage Event Handler Error")
        return {"success": False, "message": str(e)}

def handle_existing_stage_update(student, stage, stage_type, current_progress, new_status, event_type, progress_info, course_context):
    """
    Update existing stage progress
    """
    try:
        old_status = current_progress.status
        
        # Update progress record
        current_progress.status = new_status
        current_progress.last_activity_timestamp = now_datetime()
        
        # Set completion timestamp if completed
        if new_status == "completed":
            current_progress.completion_timestamp = now_datetime()
        
        # Update performance metrics if available
        update_performance_metrics(current_progress, progress_info)
        
        current_progress.save()
        frappe.db.commit()
        
        # Update student learning states
        state_updates = update_student_states(student, event_type, stage_type, progress_info, course_context)
        
        # Check for stage transitions using StageFlow
        transition_info = evaluate_stage_transition(student, stage, stage_type, new_status, course_context)
        
        return {
            "success": True,
            "action": "existing_stage_updated",
            "stage": get_stage_identifier(stage, stage_type),
            "stage_type": stage_type,
            "status_change": f"{old_status} → {new_status}",
            "data": {
                "student_id": student.name,
                "stage_progress": {
                    "id": current_progress.name,
                    "status": new_status,
                    "updated": True
                },
                "state_updates": state_updates,
                "transitions": transition_info
            },
            "course_context": course_context
        }
        
    except Exception as e:
        frappe.log_error(f"Error updating existing stage: {str(e)}", "Existing Stage Update Error")
        return {"success": False, "error": str(e)}

def handle_new_stage_assignment(student, stage, stage_type, new_status, event_type, progress_info, course_context):
    """
    Assign student to new stage
    """
    try:
        # Create new progress record
        progress = frappe.new_doc("StudentStageProgress")
        progress.student = student.name
        progress.stage_type = stage_type
        progress.stage = get_stage_identifier(stage, stage_type)
        progress.status = new_status
        progress.start_timestamp = now_datetime()
        progress.last_activity_timestamp = now_datetime()
        
        if stage_type == "LearningStage" and course_context:
            progress.course_context = course_context
        
        # Set completion timestamp if status is completed
        if new_status == "completed":
            progress.completion_timestamp = now_datetime()
        
        # Update performance metrics if available
        update_performance_metrics(progress, progress_info)
            
        progress.insert()
        
        # Update current stage pointer for onboarding stages
        if stage_type == "OnboardingStage":
            update_onboarding_current_stage(student, stage.stage_name)
        
        frappe.db.commit()
        
        # Update student learning states
        state_updates = update_student_states(student, event_type, stage_type, progress_info, course_context)
        
        # Check for stage transitions
        transition_info = evaluate_stage_transition(student, stage, stage_type, new_status, course_context)
        
        return {
            "success": True,
            "action": "new_stage_assigned",
            "stage": get_stage_identifier(stage, stage_type),
            "stage_type": stage_type,
            "status": new_status,
            "data": {
                "student_id": student.name,
                "stage_progress": {
                    "id": progress.name,
                    "status": new_status,
                    "updated": True
                },
                "state_updates": state_updates,
                "transitions": transition_info
            },
            "course_context": course_context
        }
        
    except Exception as e:
        frappe.log_error(f"Error in new stage assignment: {str(e)}", "New Stage Assignment Error")
        return {"success": False, "error": str(e)}

def evaluate_stage_transition(student, current_stage, stage_type, current_status, course_context=None):
    """
    Evaluate stage transitions using StageFlow configurations
    Restores old graceful behavior for terminal stages
    """
    try:
        # Check if stage has any flows configured
        if not hasattr(current_stage, 'stage_flows') or not current_stage.stage_flows:
            return {
                "transitions_processed": False,
                "reason": "No stage flows configured"
            }
        
        # Find applicable StageFlow for current student situation
        applicable_flow = find_applicable_stage_flow(current_stage, student, current_status)
        
        if not applicable_flow:
            return {
                "transitions_processed": False,
                "reason": "No applicable stage flow found for current status",
                "current_status": current_status,
                "available_flows": [flow.student_status for flow in current_stage.stage_flows]
            }
        
        # Execute StageFlow-based transition (handles both terminal and progression)
        return execute_stageflow_transition(student, current_stage, stage_type, applicable_flow, course_context)
        
    except Exception as e:
        frappe.log_error(f"Error evaluating stage transition: {str(e)}", "Stage Transition Evaluation Error")
        return {"transitions_processed": False, "error": str(e)}

def find_applicable_stage_flow(stage, student, current_status):
    """
    Find the most appropriate StageFlow configuration for student's current situation
    """
    try:
        # Look for exact status match first
        for stage_flow in stage.stage_flows:
            if stage_flow.student_status == current_status:
                return stage_flow
        
        # Look for default flow as fallback
        for stage_flow in stage.stage_flows:
            if stage_flow.student_status == "default":
                return stage_flow
                
        return None
        
    except Exception as e:
        frappe.log_error(f"Error finding applicable stage flow: {str(e)}", "Stage Flow Resolution Error")
        return None

def execute_stageflow_transition(student, current_stage, stage_type, stage_flow, course_context=None):
    """
    Execute transition based on StageFlow configuration
    Restored old graceful behavior for terminal stages (null next_stage)
    """
    try:
        next_stage_name = stage_flow.next_stage
        
        # ✅ RESTORED: Handle terminal stages gracefully (like old code)
        if not next_stage_name:
            # Check if this is a final stage (like old code did)
            if hasattr(current_stage, 'is_final') and current_stage.is_final:
                # Handle onboarding completion
                completion_info = handle_onboarding_completion(student, current_stage)
                
                return {
                    "transitions_processed": True,
                    "transition_type": "journey_completion",
                    "stage": get_stage_identifier(current_stage, stage_type),
                    "stage_type": stage_type,
                    "triggered_by": "final_stage_completion",
                    "flow_configuration": {
                        "student_status": stage_flow.student_status,
                        "glific_flow_id": stage_flow.glific_flow_id,
                        "flow_type": stage_flow.flow_type,
                        "description": stage_flow.description
                    },
                    "completion_details": completion_info,
                    "course_context": course_context
                }
            else:
                # Terminal stage - just like old code behavior (no error!)
                return {
                    "transitions_processed": False,  # ← Same as old code
                    "reason": "Terminal stage - no next stage configured",  # ← Explanation, not error
                    "flow_configuration": {
                        "student_status": stage_flow.student_status,
                        "glific_flow_id": stage_flow.glific_flow_id,
                        "flow_type": stage_flow.flow_type,
                        "description": stage_flow.description
                    }
                }
        
        # Regular transition logic for stages with next_stage
        next_stage = get_stage_by_stage_name(next_stage_name, stage_type)
        
        if not next_stage:
            return {"transitions_processed": False, "error": f"Next stage '{next_stage_name}' not found"}
        
        # Create progress record for next stage
        create_next_stage_progress(student, next_stage, stage_type, course_context)
        
        # Update current stage pointer for onboarding
        if stage_type == "OnboardingStage":
            update_onboarding_current_stage(student, next_stage.stage_name)
        
        # Create transition history
        create_transition_history(student, current_stage, next_stage)
        
        frappe.db.commit()
        
        return {
            "transitions_processed": True,
            "transition_type": "stage_progression",
            "from_stage": get_stage_identifier(current_stage, stage_type),
            "to_stage": get_stage_identifier(next_stage, stage_type),
            "stage_type": stage_type,
            "triggered_by": "stageflow_configuration",
            "flow_configuration": {
                "student_status": stage_flow.student_status,
                "glific_flow_id": stage_flow.glific_flow_id,
                "flow_type": stage_flow.flow_type
            },
            "course_context": course_context
        }
        
    except Exception as e:
        frappe.log_error(f"Error executing StageFlow transition: {str(e)}", "StageFlow Transition Error")
        return {"transitions_processed": False, "error": str(e)}

def handle_onboarding_completion(student, final_stage):
    """
    Handle completion of onboarding journey when reaching a final stage
    """
    try:
        # Update StudentOnboardingProgress to completed
        onboarding_records = frappe.get_all(
            "StudentOnboardingProgress", 
            filters={"student": student.name}, 
            fields=["name"]
        )
        
        completion_info = {
            "journey_completed": True,
            "completion_timestamp": now_datetime(),
            "final_stage": final_stage.stage_name
        }
        
        if onboarding_records:
            onboarding = frappe.get_doc("StudentOnboardingProgress", onboarding_records[0].name)
            onboarding.status = "completed"
            onboarding.completion_timestamp = now_datetime()
            onboarding.current_stage = final_stage.stage_name
            onboarding.save()
            
            completion_info["onboarding_progress_id"] = onboarding.name
        
        # Initialize learning stages if student is enrolled in courses
        learning_initialization = initialize_learning_stages_for_completed_onboarding(student)
        completion_info["learning_stages_initialized"] = learning_initialization
        
        return completion_info
        
    except Exception as e:
        frappe.log_error(f"Error completing onboarding journey: {str(e)}", "Onboarding Completion Error")
        return {"journey_completed": False, "error": str(e)}

def initialize_learning_stages_for_completed_onboarding(student):
    """
    Initialize learning stages for student who completed onboarding
    """
    try:
        # Get enrolled courses
        enrolled_courses = get_student_enrolled_courses(student)
        
        if not enrolled_courses:
            return {"courses_found": False, "message": "No enrolled courses found"}
        
        initialized_stages = []
        
        for course in enrolled_courses:
            # Get initial learning stage for this course
            initial_stage = get_initial_learning_stage_for_course(course)
            
            if initial_stage:
                # Check if progress already exists
                existing = frappe.get_all(
                    "StudentStageProgress",
                    filters={
                        "student": student.name,
                        "stage_type": "LearningStage",
                        "stage": initial_stage,
                        "course_context": course
                    }
                )
                
                if not existing:
                    # Create new learning stage progress
                    progress = frappe.new_doc("StudentStageProgress")
                    progress.student = student.name
                    progress.stage_type = "LearningStage"
                    progress.stage = initial_stage
                    progress.status = "assigned"
                    progress.start_timestamp = now_datetime()
                    progress.last_activity_timestamp = now_datetime()
                    progress.course_context = course
                    progress.insert()
                    
                    initialized_stages.append({
                        "course": course,
                        "initial_stage": initial_stage,
                        "progress_id": progress.name
                    })
        
        return {
            "courses_found": True,
            "total_courses": len(enrolled_courses),
            "initialized_stages": len(initialized_stages),
            "stages_details": initialized_stages
        }
        
    except Exception as e:
        frappe.log_error(f"Error initializing learning stages: {str(e)}", "Learning Stage Initialization Error")
        return {"courses_found": False, "error": str(e)}

def get_student_enrolled_courses(student):
    """
    Get list of courses the student is enrolled in
    """
    try:
        student_doc = frappe.get_doc("Student", student.name)
        courses = []
        
        if hasattr(student_doc, 'enrollment') and student_doc.enrollment:
            for enrollment in student_doc.enrollment:
                if enrollment.course and enrollment.batch:
                    courses.append(enrollment.course)
        
        return courses
        
    except Exception as e:
        frappe.log_error(f"Error getting student courses: {str(e)}", "Course Lookup Error")
        return []

def get_initial_learning_stage_for_course(course_context):
    """
    Get the initial learning stage for a specific course
    """
    try:
        # Look for stages marked as initial for this course
        stages = frappe.get_all(
            "LearningStage",
            filters={
                "course_level": course_context,
                "is_initial": 1,
                "is_active": 1
            },
            fields=["name"],
            order_by="order ASC",
            limit=1
        )
        
        if stages:
            return stages[0].name
        
        # Fallback: get stage with lowest order for this course
        stages = frappe.get_all(
            "LearningStage",
            filters={
                "course_level": course_context,
                "is_active": 1
            },
            fields=["name"],
            order_by="order ASC",
            limit=1
        )
        
        if stages:
            return stages[0].name
            
        return None
        
    except Exception as e:
        frappe.log_error(f"Error getting initial learning stage for course {course_context}: {str(e)}", "Learning Stage Lookup Error")
        return None

def derive_status_from_event(event_type):
    """
    Derive student status from event type - single source of truth
    """
    event_to_status_map = {
        # Flow-based events
        "flow_started": "assigned",
        "message_received": "in_progress",
        "flow_step_completed": "in_progress", 
        "flow_completed": "completed",
        "flow_expired": "incomplete",
        "flow_loop":"in_loop",
        "stage_failed":"failed",
        "stage_skipped":"skipped",


        # Assessment events
        "assessment_started": "in_progress",
        "assessment_submitted": "in_progress",
        "assessment_passed": "completed",
        "assessment_failed": "incomplete",
        
        # Manual/External events
        "manual_assignment": "assigned",
        "manual_completion": "completed",
        "external_update": "assigned",
        "direct_stage_event": "assigned",
        "stage_assigned": "assigned",
        "stage_completed": "completed",
        
        # Administrative events
        "teacher_override": "assigned",
        "system_reset": "assigned",
        "remediation_assigned": "assigned"
    }
    
    return event_to_status_map.get(event_type, "assigned")

def update_performance_metrics(progress, progress_info):
    """
    Update performance metrics on progress record
    """
    try:
        if hasattr(progress, 'performance_metrics') and (
            progress_info.get("completion_percentage") or 
            progress_info.get("assessment_results")
        ):
            try:
                metrics = json.loads(progress.performance_metrics or "{}")
            except (json.JSONDecodeError, TypeError):
                metrics = {}
            
            if progress_info.get("completion_percentage"):
                metrics["completion_percentage"] = progress_info.get("completion_percentage")
                
            if progress_info.get("assessment_results"):
                metrics["assessment_results"] = progress_info.get("assessment_results")
                
            progress.performance_metrics = json.dumps(metrics)
            
            # Update mastery level if available
            if hasattr(progress, 'mastery_level') and progress_info.get("assessment_results"):
                score = progress_info.get("assessment_results", {}).get("score", 0)
                if score >= 90:
                    progress.mastery_level = "Advanced"
                elif score >= 75:
                    progress.mastery_level = "Proficient"
                elif score >= 50:
                    progress.mastery_level = "Basic"
                else:
                    progress.mastery_level = "Struggling"
    
    except Exception as e:
        frappe.log_error(f"Error updating performance metrics: {str(e)}", "Performance Metrics Error")

# UTILITY FUNCTIONS

def get_stage_by_stage_name(stage_name, stage_type):
    """
    Get stage document by stage_name field (for OnboardingStage) or name (for LearningStage)
    """
    if stage_type == "OnboardingStage":
        return get_onboarding_stage_by_name(stage_name)
    elif stage_type == "LearningStage":
        return get_learning_stage_by_name(stage_name)
    return None

def get_onboarding_stage_by_name(stage_name):
    """
    Get OnboardingStage by stage_name field
    """
    try:
        stages = frappe.get_all(
            "OnboardingStage",
            filters={"stage_name": stage_name, "is_active": 1},
            fields=["name"]
        )
        if stages:
            return frappe.get_doc("OnboardingStage", stages[0].name)
    except Exception as e:
        frappe.log_error(f"Error getting OnboardingStage by name '{stage_name}': {str(e)}", "Stage Lookup Error")
    return None

def get_learning_stage_by_name(stage_name):
    """
    Get LearningStage by name field
    """
    try:
        if frappe.db.exists("LearningStage", stage_name):
            return frappe.get_doc("LearningStage", stage_name)
    except Exception as e:
        frappe.log_error(f"Error getting LearningStage by name '{stage_name}': {str(e)}", "Stage Lookup Error")
    return None

def get_stage_document_by_name(stage_name):
    """
    Get stage document and determine type by stage_name
    """
    # Try OnboardingStage first
    stage = get_onboarding_stage_by_name(stage_name)
    if stage:
        return stage, "OnboardingStage"
    
    # Try LearningStage
    stage = get_learning_stage_by_name(stage_name)
    if stage:
        return stage, "LearningStage"
    
    return None, None

def get_stage_identifier(stage, stage_type):
    """
    Get the appropriate stage identifier for API responses
    """
    if stage_type == "OnboardingStage":
        return stage.stage_name
    else:
        return stage.name

def find_student_by_id(student_id):
    """
    Find student by various ID types
    Note: phone + name combination is unique, phone alone is not unique
    """
    # If it's already a proper student ID
    if frappe.db.exists("Student", student_id):
        return frappe.get_doc("Student", student_id)
    
    # Try as Glific ID (should be unique)
    students = frappe.get_all("Student", filters={"glific_id": student_id}, fields=["name"])
    if students:
        return frappe.get_doc("Student", students[0].name)
    
    # Don't try phone number alone since it's not unique
    # This prevents incorrect student matching
    
    return None

def get_current_stage_progress(student, stage, stage_type, course_context=None):
    """
    Get current progress record for specific stage
    """
    filters = {
        "student": student.name,
        "stage_type": stage_type,
        "stage": get_stage_identifier(stage, stage_type)
    }
    
    if stage_type == "LearningStage" and course_context:
        filters["course_context"] = course_context
    
    progress_records = frappe.get_all("StudentStageProgress", filters=filters, fields=["name"])
    
    if progress_records:
        return frappe.get_doc("StudentStageProgress", progress_records[0].name)
    
    return None

def update_onboarding_current_stage(student, new_stage_name):
    """
    Update StudentOnboardingProgress current_stage (using stage_name)
    """
    try:
        onboarding_records = frappe.get_all("StudentOnboardingProgress", filters={"student": student.name}, fields=["name"])
        
        if onboarding_records:
            onboarding = frappe.get_doc("StudentOnboardingProgress", onboarding_records[0].name)
            onboarding.current_stage = new_stage_name  # Store stage_name
            onboarding.last_activity_timestamp = now_datetime()
            onboarding.save()
        else:
            # Create new onboarding progress
            onboarding = frappe.new_doc("StudentOnboardingProgress")
            onboarding.student = student.name
            onboarding.current_stage = new_stage_name  # Store stage_name
            onboarding.status = "in_progress"
            onboarding.start_timestamp = now_datetime()
            onboarding.last_activity_timestamp = now_datetime()
            onboarding.insert()
        
        frappe.db.commit()
        
    except Exception as e:
        frappe.log_error(f"Error updating onboarding progress: {str(e)}", "Onboarding Progress Error")

def create_next_stage_progress(student, next_stage, stage_type, course_context=None):
    """
    Create progress record for next stage
    """
    try:
        stage_identifier = get_stage_identifier(next_stage, stage_type)
        
        filters = {
            "student": student.name,
            "stage_type": stage_type,
            "stage": stage_identifier
        }
        
        if course_context:
            filters["course_context"] = course_context
        
        # Check if progress already exists
        existing = frappe.get_all("StudentStageProgress", filters=filters)
        
        if not existing:
            progress = frappe.new_doc("StudentStageProgress")
            progress.student = student.name
            progress.stage_type = stage_type
            progress.stage = stage_identifier
            progress.status = "assigned"
            progress.start_timestamp = now_datetime()
            progress.last_activity_timestamp = now_datetime()
            
            if course_context:
                progress.course_context = course_context
                
            progress.insert()
            frappe.db.commit()
        
    except Exception as e:
        frappe.log_error(f"Error creating next stage progress: {str(e)}", "Next Stage Progress Error")

# STUDENT AND INTERACTION FUNCTIONS

def find_student(contact_info):
    """
    Find student by contact information
    Note: phone + name combination is unique, phone alone is not unique
    """
    # Best match: Glific ID + phone + name (all three match)
    if contact_info.get('id') and contact_info.get('phone') and contact_info.get('name'):
        students = frappe.get_all(
            "Student", 
            filters={
                "glific_id": contact_info.get('id'),
                "phone": contact_info.get('phone'),
                "name1": contact_info.get('name')
            }, 
            fields=["name"]
        )
        if students:
            return frappe.get_doc("Student", students[0].name)
    
    # Good match: Glific ID + phone (without name verification)
    if contact_info.get('id') and contact_info.get('phone'):
        students = frappe.get_all(
            "Student", 
            filters={
                "glific_id": contact_info.get('id'),
                "phone": contact_info.get('phone')
            }, 
            fields=["name"]
        )
        if students:
            return frappe.get_doc("Student", students[0].name)
    
    # Reliable match: Glific ID only (should be unique per student)
    if contact_info.get('id'):
        students = frappe.get_all(
            "Student", 
            filters={"glific_id": contact_info.get('id')}, 
            fields=["name"]
        )
        if students:
            if len(students) > 1:
                frappe.logger().warning(
                    f"Multiple students found with Glific ID {contact_info.get('id')}. Using first student found: {students[0].name}"
                )
            return frappe.get_doc("Student", students[0].name)
    
    # Unique match: phone + name combination (this is unique in system)
    if contact_info.get('phone') and contact_info.get('name'):
        students = frappe.get_all(
            "Student", 
            filters={
                "phone": contact_info.get('phone'),
                "name1": contact_info.get('name')
            }, 
            fields=["name"]
        )
        if students:
            return frappe.get_doc("Student", students[0].name)
    
    # Avoid phone-only lookup since phone is not unique
    # This prevents incorrect student matching
    
    return None

def format_phone_number(phone):
    """
    Format phone number for consistency
    Note: This function is kept for backward compatibility but not used in student lookup
    since phone formatting is not needed and phone + name combination is the unique identifier
    """
    if not phone:
        return None
    phone = phone.strip().replace(' ', '')
    if len(phone) == 10:
        return f"91{phone}"
    elif len(phone) == 12 and phone.startswith('91'):
        return phone
    else:
        return phone

def create_interaction_log(student, stage, stage_type, event_type, message_data, progress_info, course_context=None):
    """
    Create interaction log for audit trail
    """
    try:
        interaction_type_map = {
            "flow_started": "message",
            "message_received": "message", 
            "flow_step_completed": "message",
            "flow_completed": "message",
            "flow_expired": "message",
            "assessment_submitted": "quiz",
            "content_delivered": "message",
            "learning_choice_made": "help_request",
            "external_update": "message",
            "direct_stage_event": "message",
            "manual_assignment": "message",
            "stage_skipped": "message"
        }
        
        interaction_type = interaction_type_map.get(event_type, "message")
        
        log = frappe.new_doc("InteractionLog")
        log.student = student.name
        log.timestamp = now_datetime()
        log.interaction_type = interaction_type
        log.content = message_data.get("body", "")
        log.stage_type = stage_type
        log.stage = get_stage_identifier(stage, stage_type)
        
        response_data = {
            "event_type": event_type,
            "message_type": message_data.get("type", "text"),
            "message_id": message_data.get("id"),
            "progress": {
                "step": progress_info.get("step"),
                "completion_percentage": progress_info.get("completion_percentage")
            }
        }
        
        if course_context:
            response_data["course_context"] = course_context
            
        if progress_info.get("assessment_results"):
            response_data["assessment_results"] = progress_info.get("assessment_results")
        
        log.response_data = json.dumps(response_data)
        
        if message_data.get("id"):
            log.glific_message_id = message_data.get("id")
        
        stage_identifier = get_stage_identifier(stage, stage_type)
        
        if event_type == "flow_completed":
            course_info = f" in {course_context}" if course_context else ""
            log.system_action = f"Completed {stage_type}: {stage_identifier}{course_info}"
        elif event_type == "flow_expired":
            log.system_action = f"{stage_type} {stage_identifier} expired: {progress_info.get('exit_type', 'unknown')}"
        elif event_type == "assessment_submitted":
            log.system_action = f"Assessment submitted for {stage_type}: {stage_identifier}"
        else:
            log.system_action = f"Interaction with {stage_type}: {stage_identifier}"
        
        if event_type in ["message_received", "learning_choice_made"]:
            log.student_agency_indicator = "Self-Determined"
        else:
            log.student_agency_indicator = "Directed"
        
        log.insert()
        frappe.db.commit()
        return log
        
    except Exception as e:
        frappe.log_error(f"Error creating interaction log: {str(e)}", "Journey Tracking Error")
        return None

def update_student_states(student, event_type, stage_type, progress_info, course_context=None):
    """
    Update LearningState and EngagementState based on interaction
    """
    updates = {"learning_state": False, "engagement_state": False}
    
    try:
        engagement_states = frappe.get_all("EngagementState", filters={"student": student.name}, fields=["name"])
        
        if engagement_states:
            engagement_state = frappe.get_doc("EngagementState", engagement_states[0].name)
            engagement_state.last_activity_date = today()
            
            if event_type == "message_received":
                engagement_state.session_frequency = (float(engagement_state.session_frequency) + 0.1) if engagement_state.session_frequency else 0.1
                
                last_activity = engagement_state.last_activity_date
                today_date = today()
                
                if last_activity:
                    if isinstance(last_activity, str):
                        last_activity = get_datetime(last_activity).date()
                    if isinstance(today_date, str):
                        today_date = get_datetime(today_date).date()
                    
                    try:
                        days_diff = (today_date - last_activity).days
                        if days_diff == 1:
                            engagement_state.current_streak = (engagement_state.current_streak or 0) + 1
                        elif days_diff > 1:
                            engagement_state.current_streak = 1
                    except Exception as e:
                        frappe.log_error(f"Error calculating date difference: {str(e)}", "Journey Tracking Error")
            
            elif event_type == "flow_completed":
                try:
                    completion_rate = float(engagement_state.completion_rate or "0")
                    completion_rate = (completion_rate * 9 + 10) / 10
                    engagement_state.completion_rate = str(min(completion_rate, 100))
                except (ValueError, TypeError) as e:
                    frappe.log_error(f"Error updating completion rate: {str(e)}", "Journey Tracking Error")
                    engagement_state.completion_rate = "10"
            
            engagement_state.save()
            updates["engagement_state"] = True
        
    except Exception as e:
        frappe.log_error(f"Error updating EngagementState: {str(e)}", "Journey Tracking Error")
    
    if event_type in ["assessment_submitted", "flow_completed"] and progress_info.get("assessment_results"):
        try:
            learning_states = frappe.get_all("LearningState", filters={"student": student.name}, fields=["name"])
            
            if learning_states:
                learning_state = frappe.get_doc("LearningState", learning_states[0].name)
                learning_state.last_assessment_date = today()
                learning_state.last_updated = now_datetime()
                
                if stage_type == "LearningStage" and hasattr(learning_state, 'knowledge_map'):
                    try:
                        knowledge_map = json.loads(learning_state.knowledge_map or "{}")
                    except (json.JSONDecodeError, TypeError):
                        knowledge_map = {}
                    
                    subject_key = course_context or "general"
                    score = progress_info.get("assessment_results", {}).get("score", 0)
                    
                    if subject_key in knowledge_map:
                        knowledge_map[subject_key] = (knowledge_map[subject_key] * 0.7) + (score * 0.3)
                    else:
                        knowledge_map[subject_key] = score
                    
                    learning_state.knowledge_map = json.dumps(knowledge_map)
                
                learning_state.save()
                updates["learning_state"] = True
                
        except Exception as e:
            frappe.log_error(f"Error updating LearningState: {str(e)}", "Journey Tracking Error")
    
    frappe.db.commit()
    return updates

def create_transition_history(student, from_stage, to_stage):
    """
    Create transition history record
    """
    try:
        transition = frappe.new_doc("TransitionHistory")
        transition.student = student.name
        transition.timestamp = now_datetime()
        transition.from_stage_type = from_stage.doctype
        transition.from_stage = get_stage_identifier(from_stage, from_stage.doctype)
        transition.to_stage_type = to_stage.doctype
        transition.to_stage = get_stage_identifier(to_stage, to_stage.doctype)
        transition.success_indicator = "Successful"
        transition.insert()
        frappe.db.commit()
        return transition
    except Exception as e:
        frappe.log_error(f"Error creating transition history: {str(e)}", "Journey Tracking Error")
        return None
