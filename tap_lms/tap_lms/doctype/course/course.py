# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

class Course(Document):
	
	def before_save(self):
		self.title = f"{self.name1} ({self.type})"