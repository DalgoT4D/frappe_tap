frappe.ui.form.on("ParentCallConfig", {
    refresh(frm) {
        if (!frm.is_new()) {
            frm.add_custom_button(__("Preview Prompt"), () => {
                _show_preview_dialog(frm);
            });
        }
    }
});

function _show_preview_dialog(frm) {
    const d = new frappe.ui.Dialog({
        title: __("Preview Rendered Prompt"),
        fields: [
            {
                label: __("ProgramEnrollment Name"),
                fieldname: "pe_name",
                fieldtype: "Link",
                options: "ProgramEnrollment",
                reqd: 1,
                description: __("Enter a PE document name to preview what this template renders to for that student.")
            }
        ],
        primary_action_label: __("Render"),
        primary_action(values) {
            d.disable_primary_action();
            frappe.call({
                method: "tap_lms.tap_lms.doctype.parentcallconfig.parentcallconfig.preview_prompt",
                args: { config_name: frm.doc.name, pe_name: values.pe_name },
                callback(r) {
                    d.enable_primary_action();
                    if (r.exc) {
                        frappe.msgprint({ message: r.exc, indicator: "red", title: "Error" });
                        return;
                    }
                    _show_result_dialog(r.message);
                    d.hide();
                },
            });
        }
    });
    d.show();
}

function _show_result_dialog(res) {
    const ctx_rows = Object.entries(res.context || {})
        .filter(([, v]) => v)
        .map(([k, v]) => `<tr>
            <td style="font-family:monospace;padding:4px 8px;color:#555;">{${k}}</td>
            <td style="padding:4px 8px;">${frappe.utils.escape_html(String(v))}</td>
        </tr>`).join("");

    const html = `
        <div style="margin-bottom:12px;font-size:12px;color:#888;">
            Student: <strong>${frappe.utils.escape_html(res.student_name)}</strong> &nbsp;|&nbsp;
            PE: <code>${res.pe_name}</code> &nbsp;|&nbsp;
            Situation: <strong style="color:#1F4E78;">${res.situation}</strong> &nbsp;|&nbsp;
            Language: <strong>${res.language}</strong>
        </div>
        <div style="background:#f8f9fa;border:1px solid #dee2e6;border-radius:4px;padding:16px;margin-bottom:16px;">
            <div style="font-size:11px;font-weight:600;color:#888;margin-bottom:8px;letter-spacing:0.5px;">RENDERED PROMPT</div>
            <div style="font-size:14px;line-height:1.7;color:#222;">${frappe.utils.escape_html(res.rendered)}</div>
        </div>
        <details>
            <summary style="font-size:11px;font-weight:600;color:#888;cursor:pointer;letter-spacing:0.5px;">
                VARIABLES RESOLVED (click to expand)
            </summary>
            <table style="width:100%;margin-top:8px;font-size:12px;border-collapse:collapse;">
                <thead><tr>
                    <th style="text-align:left;padding:4px 8px;border-bottom:1px solid #dee2e6;">Variable</th>
                    <th style="text-align:left;padding:4px 8px;border-bottom:1px solid #dee2e6;">Value</th>
                </tr></thead>
                <tbody>${ctx_rows}</tbody>
            </table>
        </details>`;

    frappe.msgprint({ title: __("Prompt Preview"), message: html, indicator: "blue", wide: true });
}
