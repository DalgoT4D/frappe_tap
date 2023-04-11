# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

class Course(Document):
	
	@property
	def title(self):
		title = ''
		if self.name:
			title += self.name1

		if self.type:
			title += ' ' + f"({self.type})"

		return title