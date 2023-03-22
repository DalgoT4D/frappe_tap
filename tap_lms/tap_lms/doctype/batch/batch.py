# Copyright (c) 2023, Techt4dev and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

import uuid

class Batch(Document):
	
	def before_save(self):
		print(self)
		self.uuid = str(uuid.uuid4())
