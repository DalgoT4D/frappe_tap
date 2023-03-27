// Copyright (c) 2023, Techt4dev and contributors
// For license information, please see license.txt

frappe.ui.form.on("Student", {
    validate: function (frm) {
        if (!frm.doc.phone.match(/^\d{10}$/)) {
            frappe.throw(__("Please make sure phone number has 10 digits."));
        }
    },
});
