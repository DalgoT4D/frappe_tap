frappe.ui.form.on("VoiceAgentSettings", {
    refresh(frm) {

        // Sync BigQuery now
        frm.add_custom_button(__("Sync BigQuery Now"), () => {
            frappe.confirm(
                "Sync Glific contact fields from BigQuery into StudentGlificContext now?",
                () => {
                    frappe.show_alert({ message: "Syncing...", indicator: "blue" });
                    frappe.call({
                        method: "tap_lms.summer_program.bigquery_sync.sync_bigquery_glific_context",
                        callback() {
                            frappe.show_alert({ message: "Sync triggered. Check Error Log for results.", indicator: "green" });
                        }
                    });
                }
            );
        }, __("Glific"));

        frm.add_custom_button(__("View Glific Context Records"), () => {
            frappe.set_route("List", "StudentGlificContext");
        }, __("Glific"));

        // Test which agent would fire for a given language
        frm.add_custom_button(__("Test Agent Resolution"), () => {
            const d = new frappe.ui.Dialog({
                title: __("Test: Which Agent Gets Selected?"),
                fields: [{
                    label: __("Language"),
                    fieldname: "language",
                    fieldtype: "Link",
                    options: "TAP Language",
                    reqd: 1,
                }],
                primary_action_label: __("Test"),
                primary_action(values) {
                    d.hide();
                    frappe.call({
                        method: "frappe.client.get",
                        args: { doctype: "VoiceAgentSettings", name: "VoiceAgentSettings" },
                        callback(r) {
                            const agents = (r.message && r.message.agents) || [];
                            const lang = (values.language || "").toLowerCase().trim();
                            const match = agents.find(
                                a => a.enabled && (a.language || "").toLowerCase().trim() === lang
                            );
                            if (match) {
                                frappe.msgprint({
                                    title: __("Agent Found"),
                                    message: `Language <b>${values.language}</b> → Agent ID: <code>${match.agent_id}</code>`,
                                    indicator: "green"
                                });
                            } else {
                                const fallback = r.message && r.message.agent_id;
                                frappe.msgprint({
                                    title: __("No Match"),
                                    message: `No active mapping for <b>${values.language}</b>. ` +
                                             (fallback ? `Fallback: <code>${fallback}</code>` : "No fallback set — calls blocked."),
                                    indicator: fallback ? "orange" : "red"
                                });
                            }
                        }
                    });
                }
            });
            d.show();
        }, __("Testing"));

        // Enabled/disabled indicator
        frm.page.set_indicator(
            frm.doc.enabled ? __("Calls Enabled") : __("Calls Disabled"),
            frm.doc.enabled ? "green" : "red"
        );
    }
});
