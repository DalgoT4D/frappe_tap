"""CR-2026-06-19 review: convert the Enrollment child doctype from
counter-autoname to hash naming (parallel-worker contention fix).

`Enrollment` (Student's child table, tabEnrollment) used
`autoname: "format:ER{########}"`, which locks the shared `tabSeries.current`
row FOR UPDATE on every insert (frappe/model/naming.getseries). In the backend
onboarding path `process_student_record` appends one Enrollment per student, so
running >1 `long` worker serializes new-Enrollment creation across workers on
the single `ER` counter and can raise `SerializationFailure` under contention —
the same mechanism BR-003 fixed for StudentStageProgress et al. (L-071 / L-075).

`Student` itself can NOT be converted (L-031: Student.name `ST00051383` IS the
canonical student ID, referenced in Glific / CSV / displays). Enrollment can:
nothing constructs or parses `ER…` names (verified 2026-06-19 — the child rows
are only ever read via the parent's `enrollment` child-table collection by
field, never by `.name`).

Self-healing fallback (L-036): `bench migrate` applies the JSON autoname change
during model-sync (this patch runs in [post_model_sync]); reload_doc guarantees
the doctype meta is in sync even if the normal sync was skipped. Existing `ER…`
rows coexist with new hash-named rows — Frappe does not require a uniform name
shape within a doctype.
"""
import frappe


def execute():
    frappe.reload_doc("tap_lms", "doctype", "enrollment")
