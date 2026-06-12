"""BR-003: convert hot SP doctypes from counter-autoname to hash naming.

StudentStageProgress / StudentContentLog / Submission / ImgSubmission used
`autoname: "format:...{####}"`, which locks the shared `tabSeries.current` row
`FOR UPDATE` on every insert. Under concurrent Glific-webhook dispatch (the
2026-06-04 trainer-cohort incident: get_next_content → _get_or_create_sp_progress
→ StudentStageProgress insert) that produced `SerializationFailure: could not
serialize access due to concurrent update`, which then cascaded via the L-030
poisoned-txn path into a Glific 400. Hash naming has no shared counter, so the
contention disappears at the source. L-071.

This patch is a self-healing fallback (L-036): `bench migrate` already applies
the JSON autoname change during model-sync (this runs in [post_model_sync]), so
reload_doc here just guarantees the doctype meta is in sync even if the normal
sync was skipped. Existing format-named docs coexist with new hash-named docs —
Frappe does not require a uniform name shape within a doctype, and nothing in
the codebase constructs or parses these names (verified: lookups are by field
filter or by the stored `.name`).
"""
import frappe


def execute():
    for dt in (
        "studentstageprogress",
        "studentcontentlog",
        "submission",
        "imgsubmission",
    ):
        frappe.reload_doc("tap_lms", "doctype", dt)
