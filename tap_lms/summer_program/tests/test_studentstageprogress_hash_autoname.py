"""
BR-003 — StudentStageProgress (and the other hot SP doctypes) use hash autoname.

The root of the 2026-06-04 SerializationFailure was the counter autoname
(`format:{student}-{stage}-{status}-{###}`) locking `tabSeries.current FOR
UPDATE` on every insert. Hash naming has no shared counter, so concurrent
inserts can't contend on tabSeries.

These tests assert the schema change is live and that real inserts get 10-char
hash names with no tabSeries series row created for the doctype.
"""
import re
import frappe
from frappe.tests.utils import FrappeTestCase

# Frappe's generate_hash() returns 10 chars from a base-36-ish alphabet
# (e.g. "m93jqs1uv8"), NOT pure hex.
_HASH_RE = re.compile(r"^[a-z0-9]{10}$")


class TestHotDoctypesHashAutoname(FrappeTestCase):

    def test_meta_autoname_is_hash(self):
        for dt in ("StudentStageProgress", "StudentContentLog",
                   "Submission", "ImgSubmission"):
            meta = frappe.get_meta(dt)
            self.assertEqual(meta.autoname, "hash",
                             f"{dt} must use hash autoname (BR-003)")

    def test_hash_autoname_no_tabseries_contention(self):
        """Two StudentStageProgress inserts with different field combos both get
        distinct 10-char hash names, and no tabSeries row is created for them."""
        a = frappe.get_doc({
            "doctype": "StudentStageProgress",
            "stage_type": "LearningUnit",
            "status": "assigned",
            "current_week": 1,
            "current_tier": "Basic",
        }).insert(ignore_permissions=True)

        b = frappe.get_doc({
            "doctype": "StudentStageProgress",
            "stage_type": "LearningUnit",
            "status": "assigned",
            "current_week": 2,
            "current_tier": "Intermediate",
        }).insert(ignore_permissions=True)

        # Hash naming: 10 hex chars, and the two are distinct.
        self.assertTrue(_HASH_RE.match(a.name), f"name not a hash: {a.name}")
        self.assertTrue(_HASH_RE.match(b.name), f"name not a hash: {b.name}")
        self.assertNotEqual(a.name, b.name)

        # No tabSeries counter row exists for the old format prefix — hash
        # naming never touches tabSeries, so there is nothing to serialize on.
        stale_series = frappe.db.sql(
            """SELECT name FROM "tabSeries" WHERE name LIKE %s""",
            ("%-LearningUnit-assigned-%",),
        )
        self.assertFalse(stale_series,
                         "hash autoname must not create a tabSeries counter row")


if __name__ == "__main__":
    import unittest
    unittest.main()
