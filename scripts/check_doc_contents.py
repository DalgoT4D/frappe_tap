import frappe
from frappe.utils.file_manager import get_file_path
import base64
assignment_id = "fun-faces-1313"
parent_doc = frappe.get_doc("Assignment", assignment_id)
images = []
for row in parent_doc.reference_images:
    file_url = row.image
    file_doc = frappe.get_doc("File", {"file_url": file_url})

    file_path = file_doc.get_full_path()
    with open(file_path, 'rb') as f:
        content = base64.b64encode(f.read()).decode('utf-8')
    images.append({
        'name': file_doc.file_name,
        'content_type': 'image/jpeg',
        'content': content[:10]  # base64 encoded
    })
context = { "reference_images": images}
print(context)
    
