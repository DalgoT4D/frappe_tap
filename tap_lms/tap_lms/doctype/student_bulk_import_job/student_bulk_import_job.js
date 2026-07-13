frappe.ui.form.on("Student Bulk Import Job", {
    refresh(frm) {
        if (frm.is_new()) {
            return;
        }

        if (frm.doc.status !== "Processing" && frm.doc.status !== "Queued") {
            frm.add_custom_button(__("Start Processing"), () => {
                frappe.call({
                    method: "tap_lms.tap_lms.doctype.student_bulk_import_job.student_bulk_import_job.start_student_bulk_import_job",
                    args: { docname: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Queueing student bulk import..."),
                    callback(r) {
                        if (r.message) {
                            frappe.show_alert({
                                message: __("Student bulk import queued"),
                                indicator: "blue",
                            });
                            frm.reload_doc();
                        }
                    },
                });
            }, __("Actions"));
        }

        frm.add_custom_button(__("Refresh Status"), () => frm.reload_doc(), __("Actions"));
    },
});
