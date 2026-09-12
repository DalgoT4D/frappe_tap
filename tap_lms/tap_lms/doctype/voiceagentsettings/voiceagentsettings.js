frappe.ui.form.on("VoiceAgentSettings", {
    refresh(frm) {
        frm.page.set_indicator(
            frm.doc.enabled ? __("Calls Enabled") : __("Calls Disabled"),
            frm.doc.enabled ? "green" : "red"
        );

        frm.page.set_indicator(
            frm.doc.default_provider || "Vocallabs",
            frm.doc.default_provider === "ElevenLabs" ? "purple" : "blue"
        );
    }
});
