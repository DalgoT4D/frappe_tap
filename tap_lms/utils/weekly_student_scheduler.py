import frappe
from frappe.utils import today, add_days, get_first_day_of_week, get_last_day_of_week
from datetime import datetime, timedelta

def get_weekly_student():
    """
    Weekly scheduled function to identify eligible students for weekly activity flow
    Runs every Monday morning IST
    """
    try:
        frappe.logger().info("Starting GetWeeklyStudent function")
        
        # 1. Calculate current week window
        week_start_date = get_first_day_of_week(today())  # Monday
        week_end_date = get_last_day_of_week(today())     # Sunday
        
        frappe.logger().info(f"Week window: {week_start_date} to {week_end_date}")
        
        # 2. Check if WeeklyStudentFlow already exists for this week
        existing_flow = frappe.db.exists("WeeklyStudentFlow", {
            "week_start_date": week_start_date
        })
        
        if existing_flow:
            frappe.logger().info(f"WeeklyStudentFlow already exists for week starting {week_start_date}")
            return
        
        # 3. Find all students with active enrollments
        students_data = get_eligible_students(week_start_date, week_end_date)
        
        print(students_data)
        if not students_data:
            frappe.logger().info("No eligible students found for this week")
            return
        
        # 4. Create WeeklyStudentFlow document
        create_weekly_student_flow(week_start_date, week_end_date, students_data)
        
        frappe.logger().info(f"Successfully created WeeklyStudentFlow for {len(students_data)} students")
        
    except Exception as e:
        frappe.log_error(f"Error in GetWeeklyStudent: {str(e)}", "GetWeeklyStudent Error")
        raise
def get_current_week_dates():
    """
    Get the Monday and Sunday of the current week.
    
    Returns:
        tuple: (week_start_date, week_end_date) as date objects
    """
    today = getdate()
    # Calculate days since Monday (Monday is 0)
    days_since_monday = today.weekday()
    week_start_date = add_days(today, -days_since_monday)
    week_end_date = add_days(week_start_date, 6)
    return week_start_date, week_end_date

def get_eligible_students(week_start_date, week_end_date):
    """
    Get all eligible students for the current week based on:
    - Active enrollments
    - Batch with regular_activity_start_date within the week
    - Batch onboarding exists for that batch
    """
    eligible_students = []
    processed_students = set()  # Track students already added
    
    frappe.logger().info("Querying active students...")
    
    # Get all active students
    students = frappe.get_all(
        "Student",
        filters={"status": "active"},
        fields=["name", "name1", "phone", "glific_id"]
    )
    
    frappe.logger().info(f"Total active students: {len(students)}")
    
    for idx, student in enumerate(students, 1):
        
        # Skip if student already processed
        if student.name in processed_students:
            continue
        
        if idx % 100 == 0:
            frappe.logger().info(f"Processing student {idx}/{len(students)}")
        
        try:
            # Get student's enrollments
            enrollments = frappe.get_all(
                "Student Enrollment",
                filters={"parent": student.name},
                fields=["batch", "school", "grade"]
            )
            
            if not enrollments:
                continue
            
            # Check each enrollment for eligible batch
            for enrollment in enrollments:
                
                if not enrollment.batch:
                    continue
                
                try:
                    # Get batch details (regular_activity_start_date is here)
                    batch = frappe.get_doc("Batch", enrollment.batch)
                    
                    # Check if batch is active
                    if not batch.active:
                        continue
                    
                    # Check if regular_activity_start_date exists and is within current week
                    if not batch.regular_activity_start_date:
                        continue
                    
                    # Convert to date for comparison
                    activity_start_date = frappe.utils.getdate(batch.regular_activity_start_date)
                    week_start_date, week_end_date = get_current_week_dates()

                    current_week_no = calculate_current_week_no(
                        batch.regular_activity_start_date,
                        week_start_date
                    )
            
                    
                    # Check if activity start date is within current week
                    if not (week_start_date <= activity_start_date <= week_end_date):
                        print("Not in current week")
                        continue
                    
                    frappe.logger().debug(f"Batch {enrollment.batch} has activity start date in current week: {activity_start_date}")
                    
                    # Now find the batch onboarding for this batch
                    batch_onboardings = frappe.get_all(
                        "Batch onboarding",
                        filters={"batch": enrollment.batch},
                        fields=["name", "batch_skeyword", "school"],
                        limit=1
                    )
                    
                    if not batch_onboardings:
                        frappe.logger().warning(f"No batch onboarding found for batch {enrollment.batch}")
                        continue
                    
                    batch_onboarding = batch_onboardings[0]
                    
                    # Create student record
                    student_record = {
                        "student": student.name,
                        "student_name": student.name1,
                        "phone_number": student.phone,
                        "glific_id": student.glific_id,
                        "batch": enrollment.batch,
                        "batch_onboarding": batch_onboarding.name,
                        #"batch_keyword": batch_onboarding.batch_skeyword,
                        "regular_activity_start_date": batch.regular_activity_start_date,
                        "current_week_no": current_week_no,
                        #"school": batch_onboarding.school or enrollment.school,
                        "flow_trigger_status": "Pending"
                    }
                    
                    eligible_students.append(student_record)
                    processed_students.add(student.name)
                    
                    frappe.logger().debug(f"Added student: {student.name1} - Batch: {enrollment.batch}")
                    
                    # Only add student once per week (use first eligible batch found)
                    break
                    
                except Exception as batch_error:
                    frappe.logger().error(f"Error processing batch {enrollment.batch}: {str(batch_error)}")
                    continue
                    
        except Exception as student_error:
            frappe.logger().error(f"Error processing student {student.name}: {str(student_error)}")
            continue
    
    frappe.logger().info(f"Found {len(eligible_students)} eligible students")
    return eligible_students

def calculate_current_week_no(regular_activity_start_date, week_start_date):
    """
    Calculate the current week number of the activity program.
    
    Formula: days_diff = (week_start_date - regular_activity_start_date).days
             current_week_no = (days_diff // 7) + 1
    
    Args:
        regular_activity_start_date: Date when regular activities started for the batch
        week_start_date: Monday of the current week
        
    Returns:
        int: Week number (1-indexed)
    """
    if not regular_activity_start_date:
        return None
    
    regular_activity_start_date = getdate(regular_activity_start_date)
    week_start_date = getdate(week_start_date)
    
    days_diff = (week_start_date - regular_activity_start_date).days
    
    # Only return positive week numbers (activity has started)
    if days_diff < 0:
        return None
    
    current_week_no = (days_diff // 7) + 1
    return current_week_no

def create_weekly_student_flow(week_start_date, week_end_date, students_data):
    """
    Create WeeklyStudentFlow document with student records
    """
    try:
        frappe.logger().info("Creating WeeklyStudentFlow document...")
        
        # Create parent document
        weekly_flow = frappe.new_doc("WeeklyStudentFlow")
        weekly_flow.week_start_date = week_start_date
        weekly_flow.week_end_date = week_end_date
        weekly_flow.total_students = len(students_data)
        weekly_flow.status = "Draft"
        weekly_flow.triggered_count = 0
        weekly_flow.failed_count = 0
        
        # Add students to child table
        frappe.logger().info(f"Adding {len(students_data)} students to child table...")
        for idx, student_data in enumerate(students_data, 1):
            try:
                weekly_flow.append("students", student_data)
            except Exception as append_error:
                frappe.logger().error(f"Error appending student {idx}: {str(append_error)}")
                frappe.logger().error(f"Student data: {student_data}")
                raise
        
        # Insert document
        frappe.logger().info("Inserting document...")
        weekly_flow.insert(ignore_permissions=True)
        
        # Commit to database
        frappe.logger().info("Committing to database...")
        frappe.db.commit()
        
        frappe.logger().info(f"✓ Created WeeklyStudentFlow: {weekly_flow.name}")
        
        return weekly_flow.name
        
    except Exception as e:
        error_msg = f"Error creating WeeklyStudentFlow: {str(e)}"
        frappe.log_error(error_msg, "WeeklyStudentFlow Creation Error")
        frappe.logger().error(error_msg)
        
        # Log the data that caused the error
        frappe.logger().error(f"Week: {week_start_date} to {week_end_date}")
        frappe.logger().error(f"Students data count: {len(students_data) if students_data else 0}")
        
        raise

# def create_weekly_student_flow(week_start_date, week_end_date, students_data):
#     """
#     Create WeeklyStudentFlow document with student records
#     """
#     try:
#         weekly_flow = frappe.new_doc("WeeklyStudentFlow")
#         weekly_flow.week_start_date = week_start_date
#         weekly_flow.week_end_date = week_end_date
#         weekly_flow.total_students = len(students_data)
#         weekly_flow.status = "Draft"
        
#         # Add students to child table
#         for student_data in students_data:
#             weekly_flow.append("students", student_data)
        
#         weekly_flow.insert(ignore_permissions=True)
#         frappe.db.commit()
        
#         frappe.logger().info(f"Created WeeklyStudentFlow: {weekly_flow.name}")
#         return weekly_flow.name
        
#     except Exception as e:
#         frappe.log_error(f"Error creating WeeklyStudentFlow: {str(e)}", "WeeklyStudentFlow Creation Error")
#         raise
