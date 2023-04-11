# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

import datetime
class Batch(Document):
	
	@property
	def title(self):
		title = ''
		if self.name:
			title += self.name1

		if self.start_date:
			title += ' ' + f"({self.start_date.strftime('%b %y')})"

		return title
