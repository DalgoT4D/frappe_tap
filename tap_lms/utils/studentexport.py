#!/usr/bin/env python3

import frappe
import csv
from frappe.utils import now, now_datetime
import os
import sys
import math

def export_students_simple(site_name=None, batch_size=10000):
    """
    Export minimal student data with enrollments in multiple CSV files
    Each file contains batch_size records to avoid file size limits
    """
    
    if site_name:
        frappe.init(site_name)
        frappe.connect()
    else:
        frappe.init_site()
        frappe.connect()
    
    try:
        # Save to site private files
        private_files_path = frappe.get_site_path("private", "files")
        OUTPUT_DIR = os.path.join(private_files_path, "student_exports")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        
        print("Starting student export in batches...")
        
        # Get total count first
        total_count = frappe.db.sql("""
            SELECT COUNT(*) as count
            FROM "tabStudent" s
            LEFT JOIN "tabStudent Enrollment" e ON s.name = e.parent
        """, as_dict=True)[0]['count']
        
        print(f"Total records to export: {total_count}")
        
        # Calculate number of files needed
        num_files = math.ceil(total_count / batch_size)
        print(f"Will create {num_files} files with max {batch_size} records each")
        
        # Generate timestamp for this export batch
        timestamp = now_datetime().strftime("%Y%m%d_%H%M%S")
        
        # Process in batches
        file_info = []
        for batch_num in range(num_files):
            offset = batch_num * batch_size
            
            print(f"Processing batch {batch_num + 1}/{num_files} (records {offset + 1} to {min(offset + batch_size, total_count)})")
            
            # Get data for this batch
            data = frappe.db.sql("""
                SELECT 
                    s.name as student_id,
                    s.name1 as student_name,
                    s.phone,
                    s.school_id as school,
                    s.glific_id,
                    s.language,
                    s.grade as student_grade,
                    s.gender,
                    e.name as enrollment_id,
                    e.course,
                    e.date_joining,
                    e.batch,
                    e.grade as enrollment_grade,
                    e.school as enrollment_school
                FROM "tabStudent" s
                LEFT JOIN "tabStudent Enrollment" e ON s.name = e.parent
                ORDER BY s.name, e.idx
                LIMIT %s OFFSET %s
            """, (batch_size, offset), as_dict=True)
            
            if not data:
                break
            
            # Generate filename for this batch
            filename = f"students_export_{timestamp}_batch_{batch_num + 1:03d}.csv"
            csv_file = os.path.join(OUTPUT_DIR, filename)
            
            # Write to CSV
            with open(csv_file, 'w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=[
                    'student_id', 'student_name', 'phone', 'school', 'glific_id', 
                    'language', 'student_grade', 'gender', 'enrollment_id', 
                    'course', 'date_joining', 'batch', 'enrollment_grade', 'enrollment_school'
                ])
                
                # Write headers
                writer.writeheader()
                
                # Write data
                writer.writerows(data)
            
            # Create File record in Frappe for this batch
            file_url = f"/private/files/student_exports/{filename}"
            try:
                file_doc = frappe.get_doc({
                    "doctype": "File",
                    "file_name": filename,
                    "file_url": file_url,
                    "is_private": 1,
                    "folder": "Home/Attachments"
                })
                file_doc.insert()
                frappe.db.commit()
            except Exception as file_error:
                print(f"Warning: Could not create File record for {filename}: {str(file_error)}")
            
            file_info.append({
                'filename': filename,
                'file_url': file_url,
                'records': len(data),
                'file_size_mb': round(os.path.getsize(csv_file) / (1024*1024), 2)
            })
            
            print(f"  -> Created {filename} with {len(data)} records ({file_info[-1]['file_size_mb']} MB)")
        
        # Create summary file
        summary = {
            'export_time': now(),
            'total_records': total_count,
            'batch_size': batch_size,
            'total_files': len(file_info),
            'files': file_info,
            'timestamp': timestamp
        }
        
        summary_filename = f"export_summary_{timestamp}.json"
        summary_file = os.path.join(OUTPUT_DIR, summary_filename)
        with open(summary_file, 'w') as f:
            import json
            json.dump(summary, f, indent=2, default=str)
        
        # Create index file with download links
        create_index_file(OUTPUT_DIR, timestamp, file_info, summary)
        
        print(f"\nExport completed successfully!")
        print(f"Total files created: {len(file_info)}")
        print(f"Total records exported: {total_count}")
        print(f"Files saved in: {OUTPUT_DIR}")
        
        return {
            'files': file_info,
            'total_records': total_count,
            'total_files': len(file_info),
            'timestamp': timestamp,
            'summary_file': summary_filename
        }
        
    except Exception as e:
        print(f"Export failed: {str(e)}")
        frappe.log_error(f"Simple Student Export Error: {str(e)}")
        frappe.db.rollback()
        return None
    finally:
        frappe.destroy()

def create_index_file(output_dir, timestamp, file_info, summary):
    """Create an HTML index file with download links"""
    
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Student Export - {timestamp}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f2f2f2; }}
        .summary {{ background-color: #f9f9f9; padding: 20px; margin: 20px 0; border-radius: 5px; }}
    </style>
</head>
<body>
    <h1>Student Data Export</h1>
    <div class="summary">
        <h2>Export Summary</h2>
        <p><strong>Export Time:</strong> {summary['export_time']}</p>
        <p><strong>Total Records:</strong> {summary['total_records']:,}</p>
        <p><strong>Total Files:</strong> {summary['total_files']}</p>
        <p><strong>Batch Size:</strong> {summary['batch_size']:,} records per file</p>
    </div>
    
    <h2>Download Files</h2>
    <table>
        <tr>
            <th>File Name</th>
            <th>Records</th>
            <th>Size (MB)</th>
            <th>Download</th>
        </tr>
"""
    
    for file in file_info:
        html_content += f"""
        <tr>
            <td>{file['filename']}</td>
            <td>{file['records']:,}</td>
            <td>{file['file_size_mb']}</td>
            <td><a href="{file['file_url']}" download>Download</a></td>
        </tr>
"""
    
    html_content += """
    </table>
    
    <h2>CSV Structure</h2>
    <p>Each CSV file contains the following columns:</p>
    <ul>
        <li><strong>student_id</strong> - Student ID</li>
        <li><strong>student_name</strong> - Student Name</li>
        <li><strong>phone</strong> - Phone Number</li>
        <li><strong>school</strong> - School (from Student record)</li>
        <li><strong>glific_id</strong> - Glific ID</li>
        <li><strong>language</strong> - Language</li>
        <li><strong>student_grade</strong> - Grade (from Student record)</li>
        <li><strong>gender</strong> - Gender</li>
        <li><strong>enrollment_id</strong> - Enrollment ID</li>
        <li><strong>course</strong> - Course</li>
        <li><strong>date_joining</strong> - Date of Joining</li>
        <li><strong>batch</strong> - Batch</li>
        <li><strong>enrollment_grade</strong> - Grade (from Student Enrollment)</li>
        <li><strong>enrollment_school</strong> - School (from Student Enrollment)</li>
    </ul>
</body>
</html>
"""
    
    index_file = os.path.join(output_dir, f"index_{timestamp}.html")
    with open(index_file, 'w', encoding='utf-8') as f:
        f.write(html_content)

@frappe.whitelist()
def export_students_web(batch_size=10000):
    """Web-accessible version for calling from Frappe"""
    try:
        result = export_students_simple(batch_size=int(batch_size))
        if result is None:
            return {
                'success': False,
                'message': 'Export failed - check error logs'
            }
        return {
            'success': True,
            'message': f'Export completed successfully! Created {result["total_files"]} files.',
            'total_records': result['total_records'],
            'total_files': result['total_files'],
            'timestamp': result['timestamp'],
            'files': result['files']
        }
    except Exception as e:
        frappe.log_error(f"Web export error: {str(e)}")
        frappe.db.rollback()
        return {
            'success': False,
            'message': f'Export failed: {str(e)}'
        }

if __name__ == "__main__":
    site_name = sys.argv[1] if len(sys.argv) > 1 else None
    batch_size = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
    
    if not site_name:
        print("Usage: python studentexport.py <site_name> [batch_size]")
        print("Default batch size: 10,000 records per file")
        print("Or run: bench --site <site_name> execute tap_lms.utils.studentexport.export_students_simple")
        sys.exit(1)
    
    export_students_simple(site_name, batch_size)
