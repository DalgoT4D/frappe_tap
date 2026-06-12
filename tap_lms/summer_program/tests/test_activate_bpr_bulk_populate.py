"""
BR-002 — activate_bpr bulk-populates the main collection.

Bug (2026-06-03, BT00000019 / 71,240 PEs): activate_bpr created the 5 kind-keyed
PGCollections but never populated `main`. PEs are inserted directly into
normal_content_delivery and never transition INTO it, so maintain_collections
never fires for them — `main` stayed empty (member_count=0) and an operator had
to bulk-add 71K contacts by hand.

Fix: activate_bpr ENQUEUES `_bulk_populate_kind_keyed_collections` (long queue),
which bulk-adds each kind's contacts via add_contacts_to_group_bulk
(~60x faster than per-PE _enqueue_group_write) and SETs member_count.

Test layout (mirrors test_batch_activation.py):
  - TestActivateBprEnqueues: real BPR docs (FrappeTestCase) — assert activate_bpr
    enqueues the population job and is idempotent (early-return on re-activate).
  - TestBulkPopulateJob: frappe.db fully mocked (FrappeTestCase only for
    discovery) — assert the job's bucketing / chunking / member_count / failure
    behavior without needing 1000 real PE rows (PE has heavy Link deps).
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch, MagicMock

from tap_lms.summer_program.constants import (
    BPR_COLLECTIONS_READY,
    BPR_ACTIVE,
    VALIDATION_PASSED,
    COLLECTION_BATCH_SIZE,
)
from tap_lms.summer_program.tests.factories import make_batch

_JOB = "tap_lms.summer_program.batch_activation._bulk_populate_kind_keyed_collections"


# ════════════════════════════════════════════════════════════
# activate_bpr → enqueue + idempotency (real BPR docs)
# ════════════════════════════════════════════════════════════

class TestActivateBprEnqueues(FrappeTestCase):

    def setUp(self):
        # Shared factory populates every mandatory Batch field (L-037).
        self.batch_name = make_batch("BulkPopTestBatch", "BULKPOP01",
                                     start_date="2026-06-01", end_date="2026-08-31")

    def _create_bpr(self):
        bpr = frappe.new_doc("BatchProgramRun")
        bpr.batch = self.batch_name
        bpr.status = BPR_COLLECTIONS_READY
        bpr.total_imported = 100
        bpr.total_enrolled = 100
        bpr.content_delivery_flow = 101
        bpr.escalation_flow = 102
        bpr.validation_status = VALIDATION_PASSED
        bpr.insert(ignore_permissions=True)
        return bpr

    def tearDown(self):
        for bpr in frappe.get_all("BatchProgramRun", filters={"batch": self.batch_name}):
            frappe.delete_doc("BatchProgramRun", bpr.name, force=True)

    @patch("tap_lms.glific_integration.create_group_if_missing", return_value="G-FAKE")
    @patch("tap_lms.summer_program.batch_activation.frappe.enqueue")
    def test_activate_bpr_enqueues_bulk_population(self, mock_enqueue, _mock_grp):
        from tap_lms.summer_program.batch_activation import activate_bpr

        bpr = self._create_bpr()
        result = activate_bpr(bpr.name)

        self.assertTrue(result["success"])
        bpr.reload()
        self.assertEqual(bpr.status, BPR_ACTIVE)

        # The population job is enqueued exactly once, on the long queue, for this BPR.
        job_calls = [c for c in mock_enqueue.call_args_list
                     if c.args and c.args[0] == _JOB]
        self.assertEqual(len(job_calls), 1, "population job must be enqueued once")
        self.assertEqual(job_calls[0].kwargs.get("bpr_name"), bpr.name)
        self.assertEqual(job_calls[0].kwargs.get("queue"), "long")

    @patch("tap_lms.glific_integration.create_group_if_missing", return_value="G-FAKE")
    @patch("tap_lms.summer_program.batch_activation.frappe.enqueue")
    def test_activate_bpr_idempotent_no_double_enqueue(self, mock_enqueue, _mock_grp):
        """Second activate_bpr returns 'already active' and does NOT re-enqueue
        (the BPR_ACTIVE early-return guards re-entry)."""
        from tap_lms.summer_program.batch_activation import activate_bpr

        bpr = self._create_bpr()
        first = activate_bpr(bpr.name)
        second = activate_bpr(bpr.name)

        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertIn("already active", second["message"].lower())

        job_calls = [c for c in mock_enqueue.call_args_list
                     if c.args and c.args[0] == _JOB]
        self.assertEqual(len(job_calls), 1, "no duplicate enqueue on re-activation")


# ════════════════════════════════════════════════════════════
# _bulk_populate_kind_keyed_collections (frappe.db mocked)
# ════════════════════════════════════════════════════════════

class TestBulkPopulateJob(FrappeTestCase):
    """All DB access mocked — verifies bucketing, chunking, member_count, and
    failure handling without 1000 real PE rows. FrappeTestCase only for
    auto-discovery (the transaction wrapper is unused here)."""

    def _pe(self, gid, state):
        return {"name": f"PE-{gid}", "glific_id": gid, "resolved_flow_state": state}

    @staticmethod
    def _wire(mock_db, sql_results, batch="BATCH-TEST"):
        """Force the patched frappe.db methods to be SYNC MagicMocks.

        Patching `frappe.db` wholesale yields a mock whose `.sql` is an
        AsyncMock (Frappe v15's Database exposes async-capable methods), so a
        bare `mock_db.sql(...)` returns an un-awaited coroutine — truthy, and
        not iterable (L-060 async-mock leak). Reassigning plain MagicMocks
        restores sync semantics so side_effect return values flow through.
        """
        mock_db.get_value = MagicMock(return_value=batch)
        mock_db.sql = MagicMock(side_effect=sql_results)
        mock_db.set_value = MagicMock()
        mock_db.commit = MagicMock()

    @patch("tap_lms.summer_program.glific_extensions.add_contacts_to_group_bulk")
    @patch("tap_lms.summer_program.batch_activation.frappe.db")
    def test_bulk_populates_main_with_chunking(self, mock_db, mock_bulk):
        """1000 main-eligible PEs → 2 bulk calls (500+500) to the main group,
        member_count SET to 1000."""
        from tap_lms.summer_program.batch_activation import (
            _bulk_populate_kind_keyed_collections,
        )

        cols = [{"name": "PGC-main", "kind": "main", "glific_group_id": "G-main"}]
        pes = [self._pe(f"c{i}", "normal_content_delivery") for i in range(1000)]
        self._wire(mock_db, [cols, pes])
        mock_bulk.return_value = True

        _bulk_populate_kind_keyed_collections("BPR-TEST")

        # 1000 / 500 = 2 bulk calls, both to the main group.
        self.assertEqual(mock_bulk.call_count, 2)
        for call in mock_bulk.call_args_list:
            self.assertEqual(call.args[1], "G-main")
        # All 1000 contact ids were sent, split across the two batches.
        sent = mock_bulk.call_args_list[0].args[0] + mock_bulk.call_args_list[1].args[0]
        self.assertEqual(len(sent), 1000)
        self.assertEqual(set(sent), {f"c{i}" for i in range(1000)})
        # member_count SET to the count actually added.
        mock_db.set_value.assert_called_once_with(
            "PGCollection", "PGC-main", "member_count", 1000, update_modified=False
        )

    @patch("tap_lms.summer_program.batch_activation.frappe.db")
    def test_pe_query_filters_null_and_empty_glific_id(self, mock_db):
        """The NULL/empty glific_id skip lives in the PE-fetch SQL (validated on
        real PG by bench run-tests). Assert the guard is present in the query —
        same structural-assertion approach as test_batch_activation's
        idempotency-guard test."""
        from tap_lms.summer_program.batch_activation import (
            _bulk_populate_kind_keyed_collections,
        )

        # cols present, no PEs → job returns after the two SELECTs.
        self._wire(mock_db, [
            [{"name": "PGC-main", "kind": "main", "glific_group_id": "G-main"}],
            [],
        ])

        _bulk_populate_kind_keyed_collections("BPR-TEST")

        # Second frappe.db.sql call is the PE fetch.
        pe_sql = mock_db.sql.call_args_list[1].args[0]
        self.assertIn("glific_id IS NOT NULL", pe_sql)
        self.assertIn("glific_id != ''", pe_sql)

    @patch("tap_lms.summer_program.glific_extensions.add_contacts_to_group_bulk")
    @patch("tap_lms.summer_program.batch_activation.frappe.db")
    def test_buckets_by_resolved_flow_state(self, mock_db, mock_bulk):
        """100 normal_content_delivery → main, 20 paused_binge → binge_paused,
        5 program_dropped → program_dropped. escalation/program_completed empty."""
        from tap_lms.summer_program.batch_activation import (
            _bulk_populate_kind_keyed_collections,
        )

        cols = [
            {"name": "PGC-main", "kind": "main", "glific_group_id": "G-main"},
            {"name": "PGC-esc", "kind": "escalation", "glific_group_id": "G-esc"},
            {"name": "PGC-binge", "kind": "binge_paused", "glific_group_id": "G-binge"},
            {"name": "PGC-drop", "kind": "program_dropped", "glific_group_id": "G-drop"},
            {"name": "PGC-comp", "kind": "program_completed", "glific_group_id": "G-comp"},
        ]
        pes = (
            [self._pe(f"m{i}", "normal_content_delivery") for i in range(100)]
            + [self._pe(f"b{i}", "paused_binge") for i in range(20)]
            + [self._pe(f"d{i}", "program_dropped") for i in range(5)]
        )
        self._wire(mock_db, [cols, pes])
        mock_bulk.return_value = True

        _bulk_populate_kind_keyed_collections("BPR-TEST")

        # One bulk call per non-empty bucket (each count <= COLLECTION_BATCH_SIZE).
        added_by_group = {c.args[1]: len(c.args[0]) for c in mock_bulk.call_args_list}
        self.assertEqual(added_by_group.get("G-main"), 100)
        self.assertEqual(added_by_group.get("G-binge"), 20)
        self.assertEqual(added_by_group.get("G-drop"), 5)
        # Empty buckets are never called.
        self.assertNotIn("G-esc", added_by_group)
        self.assertNotIn("G-comp", added_by_group)

        # member_count SET per populated collection.
        set_calls = {c.args[1]: c.args[3] for c in mock_db.set_value.call_args_list}
        self.assertEqual(set_calls.get("PGC-main"), 100)
        self.assertEqual(set_calls.get("PGC-binge"), 20)
        self.assertEqual(set_calls.get("PGC-drop"), 5)

    @patch("tap_lms.summer_program.batch_activation.frappe.log_error")
    @patch("tap_lms.summer_program.glific_extensions.add_contacts_to_group_bulk")
    @patch("tap_lms.summer_program.batch_activation.frappe.db")
    def test_bulk_add_failure_logged_and_raises(self, mock_db, mock_bulk, mock_log):
        """A failing bulk-add is logged with batch info AND the job raises at the
        end so RQ records the failure (L-056). member_count reflects only the
        successful adds (0 here)."""
        from tap_lms.summer_program.batch_activation import (
            _bulk_populate_kind_keyed_collections,
        )

        cols = [{"name": "PGC-main", "kind": "main", "glific_group_id": "G-main"}]
        pes = [self._pe(f"c{i}", "normal_content_delivery") for i in range(10)]
        self._wire(mock_db, [cols, pes])
        mock_bulk.return_value = False  # every bulk-add fails

        with self.assertRaises(RuntimeError):
            _bulk_populate_kind_keyed_collections("BPR-TEST")

        # Failure logged with identifying batch info.
        mock_log.assert_called()
        logged = " ".join(f"{k}={v}" for k, v in mock_log.call_args.kwargs.items())
        self.assertIn("G-main", logged)
        self.assertIn("BPR-TEST", logged)
        # member_count SET to 0 (nothing successfully added).
        mock_db.set_value.assert_called_once_with(
            "PGCollection", "PGC-main", "member_count", 0, update_modified=False
        )

    @patch("tap_lms.summer_program.glific_extensions.add_contacts_to_group_bulk")
    @patch("tap_lms.summer_program.batch_activation.frappe.db")
    def test_no_collections_is_noop(self, mock_db, mock_bulk):
        """No kind-keyed collections (e.g. Glific group creation all failed) →
        the job returns without touching Glific."""
        from tap_lms.summer_program.batch_activation import (
            _bulk_populate_kind_keyed_collections,
        )

        self._wire(mock_db, [[]])  # no collections

        _bulk_populate_kind_keyed_collections("BPR-TEST")

        mock_bulk.assert_not_called()
        mock_db.set_value.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
