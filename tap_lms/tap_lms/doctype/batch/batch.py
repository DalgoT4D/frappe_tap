# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

from datetime import datetime
class Batch(Document):
	
	def before_save(self):
		title = ''
		if self.name1:
			title += self.name1

		if self.start_date:
			title += f"({datetime.strptime(self.start_date, '%d-%m-%Y').strftime('%b %y')})"

		self.title = title
