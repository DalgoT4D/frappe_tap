# Copyright (c) 2024, Techt4dev and contributors
# For license information, please see license.txt

import os
from frappe.model.document import Document

class Reference_Image_Item(Document):
    def before_insert(self):
        if self.image and not self.image_name:
            self.image_name = "abc"
