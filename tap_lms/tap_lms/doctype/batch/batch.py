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
			print(self.start_date)
			if isinstance(self.start_date, str):
				title += f" ({datetime.strptime(self.start_date, '%Y-%m-%d').strftime('%b %y')})"
			elif isinstance(self.start_date, datetime):
				title += f" ({self.start_date.strftime('%b %y')})"
		self.title = title
