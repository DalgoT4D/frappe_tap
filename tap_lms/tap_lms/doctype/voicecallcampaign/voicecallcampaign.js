frappe.ui.form.on("VoiceCallCampaign", {
    refresh(frm) {
        

        // Generate Queue
        if (["Draft", "Ready"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Generate Queue"), () => {
                frappe.confirm(
                    "This will replace the existing queue with a fresh one. Continue?",
                    () => _call(frm, "generate_queue", "Generating queue...")
                );
            }, __("Actions"));
        }

        // Start / Resume
        if (["Ready", "Paused"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Start Calls"), () => {
                frappe.confirm(
                    `Start placing calls for ${frm.doc.calls_pending || 0} pending students?`,
                    () => _call(frm, "start_calls", "Starting...")
                );
            }, __("Actions")).addClass("btn-primary");
        }

        // Pause
        if (frm.doc.status === "Running") {
            frm.add_custom_button(__("Pause"), () => {
                _call(frm, "pause_calls", "Pausing...");
            }, __("Actions")).addClass("btn-warning");
        }

        // Retry failed
        if (["Ready", "Paused", "Complete"].includes(frm.doc.status)) {
            const failed = (frm.doc.call_queue || []).filter(r => r.status === "Failed").length;
            if (failed > 0) {
                frm.add_custom_button(__(`Retry Failed (${failed})`), () => {
                    _call(frm, "retry_failed", "Resetting failed rows...");
                }, __("Actions"));
            }
        }

        // Clear completed
        const answered = (frm.doc.call_queue || []).filter(r => r.status === "Answered").length;
        if (answered > 0) {
            frm.add_custom_button(__(`Clear Answered (${answered})`), () => {
                _call(frm, "clear_completed", "Clearing...");
            }, __("Actions"));
        }

        // BigQuery sync
        frm.add_custom_button(__("Sync BigQuery Now"), () => {
            _call(frm, "sync_bigquery_now", "Syncing Glific context from BigQuery...");
        }, __("Actions"));

        // Analytics refresh
        if (["Running", "Paused", "Complete"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Refresh Analytics"), () => {
                _call(frm, "refresh_analytics", "Computing analytics...");
            }, __("Analytics"));
        }

        // Show re-engagement rate prominently when available
        if (frm.doc.re_engagement_rate > 0) {
            frm.dashboard.add_indicator(
                __(`Re-engagement Rate: ${frm.doc.re_engagement_rate}%`),
                frm.doc.re_engagement_rate >= 30 ? "green" :
                frm.doc.re_engagement_rate >= 15 ? "orange" : "red"
            );
        }

        // Status colour on header
        const colours = {
            Draft: "gray", Generating: "yellow", Ready: "blue",
            Running: "green", Paused: "orange", Complete: "green", Error: "red"
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || "gray");
        }

        // Colour queue rows
        if (frm.doc.call_queue) {
            frm.doc.call_queue.forEach(row => {
                const colour = {
                    Pending: "", Calling: "blue", Answered: "green",
                    "No Answer": "orange", Failed: "red", Skipped: "gray",
                    Cooldown: "gray", "Permanently Failed": "darkred"
                }[row.status];
                if (colour) {
                    const $row = frm.fields_dict.call_queue.grid.get_row(row.name);
                    if ($row) $row.row.css("background-color", _rowColour(row.status));
                }
            });
        }
    },
});

function _call(frm, method, loadingMsg) {
    frappe.show_alert({ message: loadingMsg, indicator: "blue" });
    frm.call(method).then(r => {
        if (r && r.message) {
            const msg = r.message;
            if (msg.ok === false) {
                frappe.msgprint({ message: msg.message, indicator: "red", title: "Error" });
            } else {
                frappe.show_alert({ message: msg.message || "Done", indicator: "green" });
            }
        }
        frm.refresh();
    });
}

function _rowColour(status) {
    return {
        Answered: "#e8f5e9", Failed: "#ffebee", "Permanently Failed": "#ffcdd2",
        "No Answer": "#fff8e1", Skipped: "#f5f5f5", Cooldown: "#f5f5f5",
        Calling: "#e3f2fd"
    }[status] || "";
}
