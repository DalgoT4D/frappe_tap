frappe.ui.form.on("VocalLabsSettings", {
    refresh(frm) {
        // Test agent resolution
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
                        args: { doctype: "VocalLabsSettings", name: "VocalLabsSettings" },
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
                                frappe.msgprint({
                                    title: __("No Match"),
                                    message: `No active mapping for <b>${values.language}</b>. Check agents table.`,
                                    indicator: "red"
                                });
                            }
                        }
                    });
                }
            });
            d.show();
        }, __("Testing"));
    }
});
