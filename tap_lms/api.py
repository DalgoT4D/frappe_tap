import frappe
from frappe.utils import cint

def authenticate_api_key(api_key):
    try:
        # Check if the provided API key exists and is enabled
        api_key_doc = frappe.get_doc("API Key", {"key": api_key, "enabled": 1})
        return api_key_doc.name
    except frappe.DoesNotExistError:
        # Handle the case where the API key does not exist or is not enabled
        return None




@frappe.whitelist(allow_guest=True)
def get_school_name_keyword_list(api_key, start=0, limit=10):
    # Verify the API key
    if not authenticate_api_key(api_key):
        frappe.throw("Invalid API key")

    start = cint(start)
    limit = cint(limit)

    # Query the school doctype to fetch the name1 and keyword fields
    schools = frappe.db.get_all("School",
                                fields=["name", "name1", "keyword"],
                                limit_start=start,
                                limit_page_length=limit)

    # Fixed WhatsApp number
    whatsapp_number = "918454812392"

    # Prepare the response data
    response_data = []
    for school in schools:
        # Prepend "tapschool:" to the keyword
        keyword_with_prefix = f"tapschool:{school.keyword}"

        # Create the WhatsApp link using the fixed number and keyword
        whatsapp_link = f"https://api.whatsapp.com/send?phone={whatsapp_number}&text={keyword_with_prefix}"

        school_data = {
            "school_name": school.name1,
            "keyword": keyword_with_prefix,
            "whatsapp_link": whatsapp_link
        }
        response_data.append(school_data)

    # Return the response as a JSON object
    return response_data



@frappe.whitelist(allow_guest=True)
def verify_keyword():
    # Parse the request data
    data = frappe.request.get_json()

    # Verify the API key
    if not data or 'api_key' not in data or not authenticate_api_key(data['api_key']):
        frappe.response.http_status_code = 401
        frappe.response.update({
            "status": "failure",
            "school_name": None,
            "model": None,
            "error": "Invalid API key"
        })
        return

    if 'keyword' not in data:
        frappe.response.http_status_code = 400
        frappe.response.update({
            "status": "failure",
            "school_name": None,
            "model": None,
            "error": "Keyword parameter is missing"
        })
        return

    keyword = data['keyword']

    # Check if the keyword exists in the School doctype and retrieve the smodel and name1 fields
    school = frappe.db.get_value("School", {"keyword": keyword}, ["name1", "model"], as_dict=True)

    if school:
        frappe.response.http_status_code = 200
        frappe.response.update({
            "status": "success",
            "school_name": school.name1,
            "model": school.model
        })
    else:
        frappe.response.http_status_code = 404
        frappe.response.update({
            "status": "failure",
            "school_name": None,
            "model": None
        })



@frappe.whitelist(allow_guest=True)
def create_teacher(api_key, keyword, first_name, phone_number, glific_id, last_name=None, email=None, language=None):
    try:
        # Verify the API key
        if not authenticate_api_key(api_key):
            frappe.throw("Invalid API key")

        # Find the school based on the provided keyword
        school = frappe.db.get_value("School", {"keyword": keyword}, "name")
        if not school:
            return {
                "error": f"No school found with the keyword: {keyword}"
            }

        # Create a new teacher document
        teacher = frappe.new_doc("Teacher")
        teacher.first_name = first_name
        teacher.school = school
        teacher.phone_number = phone_number
        teacher.glific_id = glific_id  # Set the glific_id field (mandatory)

        # Set the optional fields if provided
        if last_name:
            teacher.last_name = last_name
        if email:
            teacher.email = email
        if language:
            teacher.language = language

        # Insert the teacher document
        teacher.insert(ignore_permissions=True)

        # Commit the changes
        frappe.db.commit()

        return {
            "message": "Teacher created successfully",
            "teacher_id": teacher.name
        }
    except frappe.DuplicateEntryError:
        return {
            "error": "Teacher with the same phone number already exists"
        }
    except Exception as e:
        return {
            "error": f"An error occurred while creating teacher: {str(e)}"
        }





