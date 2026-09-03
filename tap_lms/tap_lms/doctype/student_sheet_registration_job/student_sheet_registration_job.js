frappe.ui.form.on("Student Sheet Registration Job", {
    refresh(frm) {
        if (frm.is_new()) {
            return;
        }

        const running = ["Preparing", "Uploading"].includes(frm.doc.status);

        if (!running) {
            frm.add_custom_button(__("Prepare Data"), () => {
                frappe.call({
                    method: "tap_lms.tap_lms.doctype.student_sheet_registration_job.student_sheet_registration_job.start_prepare_student_sheet_registration_job",
                    args: { docname: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Queueing student sheet registration prepare..."),
                    callback(r) {
                        if (r.message) {
                            frappe.show_alert({
                                message: __("Prepare job queued"),
                                indicator: "blue",
                            });
                            frm.reload_doc();
                        }
                    },
                });
            }, __("Actions"));
        }

        if (!running && frm.doc.prepared_rows_json && frm.doc.prepared_rows_json !== "[]"
            && frm.doc.status !== "Completed") {
            frm.add_custom_button(__("Complete Upload"), () => {
                frappe.call({
                    method: "tap_lms.tap_lms.doctype.student_sheet_registration_job.student_sheet_registration_job.start_upload_student_sheet_registration_job",
                    args: { docname: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Queueing complete upload..."),
                    callback(r) {
                        if (r.message) {
                            frappe.show_alert({
                                message: __("Complete upload queued"),
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
