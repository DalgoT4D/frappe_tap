frappe.listview_settings['ImgSubmission'] = {
    add_fields: ["result_status", "plagiarism_status", "is_plagiarized", "is_ai_generated", "grade"],

    get_indicator: function(doc) {
        // Primary indicator based on result_status
        const result_status_map = {
            "Pending": ["orange", "Pending"],
            "Success - Original": ["green", "✓ Original"],
            "Success - Flagged": ["red", "⚠ Flagged"],
            "Failed": ["darkgrey", "✗ Failed"]
        };

        const [color, label] = result_status_map[doc.result_status] || ["grey", "Unknown"];
        return [__(label), color, `result_status,=,${doc.result_status}`];
    },

    formatters: {
        result_status: function(value) {
            const badges = {
                "Pending": '<span class="badge badge-warning">⏳ Pending</span>',
                "Success - Original": '<span class="badge badge-success">✓ Original</span>',
                "Success - Flagged": '<span class="badge badge-danger">⚠ Flagged</span>',
                "Failed": '<span class="badge badge-secondary">✗ Failed</span>'
            };
            return badges[value] || value;
        },

        plagiarism_status: function(value) {
            const colors = {
                "Not Checked": "secondary",
                "Original": "success",
                "Flagged - Exact Match": "danger",
                "Flagged - Near Duplicate": "warning",
                "Flagged - Semantic Match": "info",
                "Flagged - AI Generated": "purple",
                "Flagged - Peer Plagiarism": "danger",
                "Flagged - Self Plagiarism": "warning",
                "Resubmission Allowed": "primary",
                "Error": "dark"
            };
            const color = colors[value] || "secondary";
            return `<span class="badge badge-${color}">${value}</span>`;
        }
    }
};
