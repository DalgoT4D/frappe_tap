# Copyright (c) 2025, Techt4dev and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class GCSSettings(Document):
	def validate(self):
		if not cint(self.enabled):
			return

		if not self.bucket_name:
			frappe.throw("Bucket Name is required when GCS Storage is enabled.")

		existing_enabled_setting = frappe.db.exists(
			"GCS Settings",
			{
				"bucket_type": self.bucket_type,
				"enabled": 1,
				"name": ["!=", self.name],
			},
		)

		if existing_enabled_setting:
			frappe.throw(
				f"Only one enabled GCS Settings record is allowed for {self.bucket_type} buckets."
			)
