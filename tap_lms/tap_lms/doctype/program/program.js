frappe.ui.form.on("Program", {

    refresh(frm) {
        frm.trigger("add_custom_buttons");
        frm.trigger("render_dashboard_headline");
    },

    add_custom_buttons(frm) {
        if (!frm.is_new()) {
            frm.add_custom_button(
                __("Create Batch"),
                () => {
                    frappe.new_doc("Batch", { program: frm.doc.name });
                },
                __("Actions")
            );

            frm.add_custom_button(
                __("View Batches"),
                () => {
                    frappe.set_route("List", "Batch", { program: frm.doc.name });
                },
                __("Actions")
            );

            frm.add_custom_button(
                __("View Course Levels"),
                () => {
                    frappe.set_route("List", "Course Level", { program: frm.doc.name });
                },
                __("Actions")
            );

            frm.add_custom_button(
                __("Program Summary"),
                () => {
                    frappe.call({
                        method: "tap_lms.tap_lms.doctype.program.program.get_program_summary",
                        args: { program_name: frm.doc.name },
                        callback(r) {
                            if (r.message) {
                                const d = r.message;

                                const batch_rows = d.batches.map(b => `
                                    <tr>
                                        <td>${b.name1 || "-"}</td>
                                        <td>${b.batch_id || "-"}</td>
                                        <td>${b.start_date || "-"}</td>
                                        <td>${b.end_date || "-"}</td>
                                        <td>${b.active ? "Yes" : "No"}</td>
                                    </tr>
                                `).join("");

                                const course_rows = d.course_levels.map(c => `
                                    <tr>
                                        <td>${c.name1 || "-"}</td>
                                        <td>${c.vertical || "-"}</td>
                                        <td>${c.stage || "-"}</td>
                                        <td>${c.kit_less ? "Yes" : "No"}</td>
                                    </tr>
                                `).join("");

                                frappe.msgprint({
                                    title: __("Program Summary"),
                                    message: `
                                        <p><b>Program:</b> ${d.program || "-"}</p>
                                        <p><b>Total Batches:</b> ${d.total_batches} &nbsp;|&nbsp; <b>Active:</b> ${d.active_batches}</p>
                                        <p><b>Total Course Levels:</b> ${d.total_course_levels}</p>

                                        <h6 style="margin-top:12px;">Batches</h6>
                                        <table class="table table-bordered">
                                            <thead>
                                                <tr>
                                                    <th>Name</th>
                                                    <th>Batch ID</th>
                                                    <th>Start</th>
                                                    <th>End</th>
                                                    <th>Active</th>
                                                </tr>
                                            </thead>
                                            <tbody>${batch_rows || "<tr><td colspan='5'>No batches found.</td></tr>"}</tbody>
                                        </table>

                                        <h6 style="margin-top:12px;">Course Levels</h6>
                                        <table class="table table-bordered">
                                            <thead>
                                                <tr>
                                                    <th>Name</th>
                                                    <th>Vertical</th>
                                                    <th>Stage</th>
                                                    <th>Kit Less</th>
                                                </tr>
                                            </thead>
                                            <tbody>${course_rows || "<tr><td colspan='4'>No course levels found.</td></tr>"}</tbody>
                                        </table>
                                    `,
                                    indicator: "blue",
                                });
                            }
                        },
                    });
                },
                __("Actions")
            );
        }
    },

    render_dashboard_headline(frm) {
        if (frm.is_new()) return;

        frappe.call({
            method: "tap_lms.tap_lms.doctype.program.program.get_program_summary",
            args: { program_name: frm.doc.name },
            callback(r) {
                if (r.message) {
                    const d = r.message;
                    frm.dashboard.set_headline_alert(
                        __("{0} batch(es) — {1} active &nbsp;|&nbsp; {2} course level(s)", [
                            d.total_batches,
                            d.active_batches,
                            d.total_course_levels,
                        ]),
                        d.active_batches > 0 ? "green" : "orange"
                    );
                }
            },
        });
    },
});