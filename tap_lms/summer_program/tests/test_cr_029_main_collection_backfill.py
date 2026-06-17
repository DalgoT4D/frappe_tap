"""CR-029 — backfill MAIN collection + shared bulk-add circuit-breaker helper.

Two coverage areas:
  - TestBulkAddCircuitBreaker: the new shared helper in glific_extensions.py.
    Pure-logic tests with add_contacts_to_group_bulk mocked.
  - TestBackfillMainCollection: the operator-run backfill in
    migrations/main_collection_backfill.py. frappe.db fully mocked so the
    state-filter / group-resolution / member-count / destructive-guard /
    role-guard behavior is asserted without 60K real PE rows.

Helper test layout mirrors test_cr_027_weekly_sweep.py (FrappeTestCase only for
auto-discovery; the transaction wrapper is unused in the mocked tests).
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch, MagicMock

from tap_lms.summer_program.constants import COLLECTION_BATCH_SIZE

GLIFIC_EXT = "tap_lms.summer_program.glific_extensions"
BACKFILL_MOD = "tap_lms.summer_program.migrations.main_collection_backfill"


# ════════════════════════════════════════════════════════════
# Shared helper: bulk_add_to_group_with_circuit_breaker
# ════════════════════════════════════════════════════════════

class TestBulkAddCircuitBreaker(FrappeTestCase):
    """Direct tests of the shared helper. add_contacts_to_group_bulk is mocked
    so the tests assert chunking + circuit-breaker logic, not real Glific."""

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_chunks_correctly_at_boundary(self, mock_bulk):
        """1000 contacts at default chunk_size (500) → 2 calls, 500 each."""
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )
        mock_bulk.return_value = True
        contact_ids = [f"c{i}" for i in range(1000)]

        result = bulk_add_to_group_with_circuit_breaker(contact_ids, "G1")

        self.assertEqual(mock_bulk.call_count, 2)
        self.assertEqual(len(mock_bulk.call_args_list[0].args[0]), 500)
        self.assertEqual(len(mock_bulk.call_args_list[1].args[0]), 500)
        self.assertEqual(result["added"], 1000)
        self.assertEqual(result["chunks_attempted"], 2)
        self.assertEqual(result["chunks_failed"], 0)
        self.assertFalse(result["circuit_tripped"])

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_custom_chunk_size_honored(self, mock_bulk):
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )
        mock_bulk.return_value = True
        contact_ids = [f"c{i}" for i in range(7)]

        result = bulk_add_to_group_with_circuit_breaker(
            contact_ids, "G1", chunk_size=3,
        )

        # ceil(7/3) = 3 chunks of sizes 3, 3, 1.
        self.assertEqual(mock_bulk.call_count, 3)
        self.assertEqual(
            [len(c.args[0]) for c in mock_bulk.call_args_list],
            [3, 3, 1],
        )
        self.assertEqual(result["added"], 7)

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_three_consecutive_failures_trip_circuit(self, mock_bulk):
        """4 chunks, all fail. After 3 consecutive failures the breaker trips
        and the 4th chunk is skipped (counted in skipped_after_trip)."""
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )
        mock_bulk.return_value = False
        contact_ids = [f"c{i}" for i in range(20)]  # 4 chunks @ chunk_size=5

        result = bulk_add_to_group_with_circuit_breaker(
            contact_ids, "G1", chunk_size=5, max_consecutive_failures=3,
        )

        # Helper attempted 3 chunks, then tripped. The 4th chunk is NOT
        # sent to add_contacts_to_group_bulk — it's only counted as skipped.
        self.assertEqual(mock_bulk.call_count, 3)
        self.assertEqual(result["chunks_attempted"], 3)
        self.assertEqual(result["chunks_failed"], 3)
        self.assertEqual(result["added"], 0)
        self.assertTrue(result["circuit_tripped"])
        self.assertEqual(result["skipped_after_trip"], 5)

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_nonconsecutive_failures_do_not_trip(self, mock_bulk):
        """The verification demo case in the CR brief: helper called with 4
        chunks where chunk #2 fails. Circuit MUST NOT trip — a successful chunk
        resets the consecutive counter."""
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )
        # Chunks: 1 OK, 2 FAIL, 3 OK, 4 OK.
        mock_bulk.side_effect = [True, False, True, True]
        contact_ids = [f"c{i}" for i in range(20)]  # 4 chunks @ chunk_size=5

        result = bulk_add_to_group_with_circuit_breaker(
            contact_ids, "G1", chunk_size=5, max_consecutive_failures=3,
        )

        self.assertEqual(mock_bulk.call_count, 4)
        self.assertEqual(result["chunks_attempted"], 4)
        self.assertEqual(result["chunks_failed"], 1)
        # 3 successful chunks of size 5 = 15.
        self.assertEqual(result["added"], 15)
        self.assertFalse(result["circuit_tripped"])
        self.assertEqual(result["skipped_after_trip"], 0)

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_exception_in_bulk_call_counts_as_failure(self, mock_bulk):
        """add_contacts_to_group_bulk normally catches its own exceptions and
        returns False, but the helper is defensive: if one escapes, it's
        caught and counted as a failure (with a log_error)."""
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )
        mock_bulk.side_effect = [True, RuntimeError("boom"), True]
        contact_ids = [f"c{i}" for i in range(15)]  # 3 chunks @ chunk_size=5

        with patch(f"{GLIFIC_EXT}.frappe.log_error"):
            result = bulk_add_to_group_with_circuit_breaker(
                contact_ids, "G1", chunk_size=5,
            )

        self.assertEqual(result["chunks_attempted"], 3)
        self.assertEqual(result["chunks_failed"], 1)
        self.assertEqual(result["added"], 10)
        self.assertFalse(result["circuit_tripped"])

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_empty_contact_ids_returns_zero_summary(self, mock_bulk):
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )

        result = bulk_add_to_group_with_circuit_breaker([], "G1")

        mock_bulk.assert_not_called()
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["chunks_attempted"], 0)
        self.assertFalse(result["circuit_tripped"])

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_missing_group_id_returns_error(self, mock_bulk):
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )

        result = bulk_add_to_group_with_circuit_breaker(["c0", "c1"], "")

        mock_bulk.assert_not_called()
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("group_id", result["errors"][0])

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_per_chunk_progress_logged(self, mock_bulk):
        """Each chunk emits a frappe.logger().info line — operators rely on
        this for live tail during a 60K backfill."""
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )
        mock_bulk.return_value = True
        contact_ids = [f"c{i}" for i in range(10)]

        with patch(f"{GLIFIC_EXT}.frappe.logger") as mock_logger:
            bulk_add_to_group_with_circuit_breaker(
                contact_ids, "G1", chunk_size=5,
            )

        # 2 chunks → at least 2 info logs (one per chunk) + 1 done line.
        info_calls = mock_logger.return_value.info.call_args_list
        self.assertGreaterEqual(len(info_calls), 3)
        # The per-chunk lines name the op_label (default "bulk_add") and the
        # chunk position — a regression here would break operator-tail UX.
        info_text = " ".join(str(c) for c in info_calls)
        self.assertIn("bulk_add", info_text)

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_op_label_threaded_into_errors_and_logs(self, mock_bulk):
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )
        mock_bulk.return_value = False
        contact_ids = [f"c{i}" for i in range(5)]

        result = bulk_add_to_group_with_circuit_breaker(
            contact_ids, "G1", chunk_size=5, op_label="backfill.main.BT01",
        )

        self.assertEqual(result["chunks_failed"], 1)
        self.assertIn("backfill.main.BT01", result["errors"][0])

    @patch(f"{GLIFIC_EXT}.add_contacts_to_group_bulk")
    def test_default_chunk_size_is_collection_batch_size(self, mock_bulk):
        from tap_lms.summer_program.glific_extensions import (
            bulk_add_to_group_with_circuit_breaker,
        )
        mock_bulk.return_value = True
        # Exactly COLLECTION_BATCH_SIZE+1 → 2 chunks proving the default boundary.
        contact_ids = [f"c{i}" for i in range(COLLECTION_BATCH_SIZE + 1)]

        bulk_add_to_group_with_circuit_breaker(contact_ids, "G1")

        self.assertEqual(mock_bulk.call_count, 2)
        self.assertEqual(len(mock_bulk.call_args_list[0].args[0]), COLLECTION_BATCH_SIZE)
        self.assertEqual(len(mock_bulk.call_args_list[1].args[0]), 1)


# ════════════════════════════════════════════════════════════
# backfill_main_collection (whitelisted operator endpoint)
# ════════════════════════════════════════════════════════════

class TestBackfillMainCollection(FrappeTestCase):
    """frappe.db mocked end-to-end. Each test wires the BPR lookup, the
    PGCollection lookup, and the PE fetch — then asserts the call structure
    through the shared helper."""

    @staticmethod
    def _wire_db(mock_db, *, bpr="BPR1", pg_collection=None, pes=None):
        """Wire mock_db.get_value (BPR lookup) and mock_db.sql (PGCollection
        + PE fetch). pg_collection is the result for the first sql call; pes
        is the result for the second."""
        pg_collection = pg_collection if pg_collection is not None else [
            {"name": "PGC-main", "glific_group_id": "G-main"}
        ]
        pes = pes if pes is not None else []
        mock_db.get_value = MagicMock(return_value=bpr)
        mock_db.sql = MagicMock(side_effect=[pg_collection, pes])
        mock_db.set_value = MagicMock()
        mock_db.commit = MagicMock()

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_dry_run_reports_count_without_writes(self, mock_db):
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        pes = [{"name": f"PE{i}", "glific_id": f"g{i}"} for i in range(50)]
        self._wire_db(mock_db, pes=pes)

        with patch(f"{BACKFILL_MOD}.bulk_add_to_group_with_circuit_breaker") as mock_helper:
            result = backfill_main_collection("BT01", dry_run=True)

        # Dry run never touches the helper or set_value.
        mock_helper.assert_not_called()
        mock_db.set_value.assert_not_called()
        # Counts are still reported so the operator can preview impact.
        self.assertEqual(result["candidates"], 50)
        self.assertEqual(result["dry_run"], True)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["bpr"], "BPR1")
        self.assertEqual(result["group_id"], "G-main")

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_filters_to_main_eligible_states_only(self, mock_db):
        """The PE-fetch SQL must filter on resolved_flow_state IN
        MAIN_ELIGIBLE_STATES — never include grace_waiting, normal_escalation,
        or program_dropped (L-055). Assert via the SQL structure + the bound
        states tuple."""
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        from tap_lms.summer_program.collection_membership import (
            MAIN_ELIGIBLE_STATES,
        )
        self._wire_db(mock_db, pes=[])

        backfill_main_collection("BT01", dry_run=True)

        # Second SQL call is the PE fetch.
        pe_sql_call = mock_db.sql.call_args_list[1]
        sql_text = pe_sql_call.args[0]
        sql_params = pe_sql_call.args[1]

        # The state filter uses `resolved_flow_state IN %s` with a tuple param
        # (L-038 — IN-tuple is the safe pattern in this Frappe version).
        self.assertIn("resolved_flow_state IN", sql_text)
        # Third positional param is the MAIN_ELIGIBLE_STATES tuple.
        states_bound = sql_params[2]
        self.assertIsInstance(states_bound, tuple)
        self.assertEqual(set(states_bound), set(MAIN_ELIGIBLE_STATES))
        # Defensive: a non-main-eligible state is explicitly NOT in the tuple.
        self.assertNotIn("grace_waiting", states_bound)
        self.assertNotIn("normal_escalation", states_bound)

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_excludes_pes_without_glific_id(self, mock_db):
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        self._wire_db(mock_db, pes=[])

        backfill_main_collection("BT01", dry_run=True)

        pe_sql = mock_db.sql.call_args_list[1].args[0]
        self.assertIn("glific_id IS NOT NULL", pe_sql)
        self.assertIn("glific_id != ''", pe_sql)

    @patch(f"{BACKFILL_MOD}.bulk_add_to_group_with_circuit_breaker")
    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_non_dry_run_calls_helper_with_extracted_glific_ids(self, mock_db, mock_helper):
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        pes = [{"name": f"PE{i}", "glific_id": f"g{i}"} for i in range(10)]
        self._wire_db(mock_db, pes=pes)
        mock_helper.return_value = {
            "chunks_attempted": 1, "chunks_failed": 0, "added": 10,
            "errors": [], "circuit_tripped": False, "skipped_after_trip": 0,
        }

        result = backfill_main_collection(
            "BT01", dry_run=False, i_know_this_is_destructive=True,
        )

        # Helper receives the extracted contact ids + the resolved group id.
        mock_helper.assert_called_once()
        args, kwargs = mock_helper.call_args
        self.assertEqual(args[0], [f"g{i}" for i in range(10)])
        self.assertEqual(args[1], "G-main")
        # op_label embeds the batch — searchable in the Error Log.
        self.assertIn("BT01", kwargs.get("op_label", ""))
        # Summary mirrors the helper.
        self.assertEqual(result["added"], 10)
        self.assertEqual(result["candidates"], 10)
        self.assertFalse(result["circuit_tripped"])

    @patch(f"{BACKFILL_MOD}.bulk_add_to_group_with_circuit_breaker")
    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_member_count_set_to_actual_added_after_run(self, mock_db, mock_helper):
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        pes = [{"name": f"PE{i}", "glific_id": f"g{i}"} for i in range(10)]
        self._wire_db(mock_db, pes=pes)
        # Helper succeeds for 7 of 10, breaker tripped for 3.
        mock_helper.return_value = {
            "chunks_attempted": 2, "chunks_failed": 1, "added": 7,
            "errors": ["chunk@5 failed"], "circuit_tripped": True,
            "skipped_after_trip": 3,
        }

        result = backfill_main_collection(
            "BT01", dry_run=False, i_know_this_is_destructive=True,
        )

        # member_count SET (not incremented) to the count actually added —
        # so a partial-then-retry run does NOT double-count.
        mock_db.set_value.assert_called_once_with(
            "PGCollection", "PGC-main", "member_count", 7,
            update_modified=False,
        )
        self.assertEqual(result["member_count_after"], 7)
        self.assertTrue(result["circuit_tripped"])
        self.assertEqual(result["skipped_after_trip"], 3)

    @patch(f"{BACKFILL_MOD}.bulk_add_to_group_with_circuit_breaker")
    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_idempotent_re_run(self, mock_db, mock_helper):
        """Two consecutive non-dry-runs with the same PE set: helper called
        twice, member_count SET each time to the same value (not doubled)."""
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        pes = [{"name": f"PE{i}", "glific_id": f"g{i}"} for i in range(10)]
        # Two runs → two PE fetches; collection lookup also runs twice.
        mock_db.get_value = MagicMock(return_value="BPR1")
        mock_db.sql = MagicMock(side_effect=[
            [{"name": "PGC-main", "glific_group_id": "G-main"}], pes,
            [{"name": "PGC-main", "glific_group_id": "G-main"}], pes,
        ])
        mock_db.set_value = MagicMock()
        mock_db.commit = MagicMock()
        mock_helper.return_value = {
            "chunks_attempted": 1, "chunks_failed": 0, "added": 10,
            "errors": [], "circuit_tripped": False, "skipped_after_trip": 0,
        }

        backfill_main_collection("BT01", dry_run=False, i_know_this_is_destructive=True)
        backfill_main_collection("BT01", dry_run=False, i_know_this_is_destructive=True)

        # Two set_value calls, both with the same `added`, never multiplied.
        self.assertEqual(mock_db.set_value.call_count, 2)
        for call in mock_db.set_value.call_args_list:
            self.assertEqual(call.args[3], 10)

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_destructive_guard_blocks_real_run_without_flag(self, mock_db):
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        with self.assertRaises(frappe.ValidationError):
            backfill_main_collection("BT01", dry_run=False)

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_destructive_guard_rejects_truthy_string_false(self, mock_db):
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        with self.assertRaises(frappe.ValidationError):
            backfill_main_collection(
                "BT01", dry_run="False", i_know_this_is_destructive="False",
            )

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_role_guard_calls_only_for_admin_roles(self, mock_db):
        # frappe.only_for is a no-op when flags.in_test is set; assert the
        # guard is wired with the right roles (same pattern as CR-027's tests).
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        self._wire_db(mock_db, pes=[])
        with patch(f"{BACKFILL_MOD}.frappe.only_for") as m_only_for:
            backfill_main_collection("BT01", dry_run=True)
        m_only_for.assert_called_once_with(["TAP Admin", "System Manager"])

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_missing_bpr_returns_error_no_raise(self, mock_db):
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        mock_db.get_value = MagicMock(return_value=None)  # no active BPR
        mock_db.sql = MagicMock()
        mock_db.set_value = MagicMock()

        result = backfill_main_collection("BT01", dry_run=True)

        # No raise — operator gets a structured error message.
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["bpr"], None)
        self.assertTrue(any("no active BPR" in e for e in result["errors"]))
        # No PE fetch was attempted.
        mock_db.sql.assert_not_called()

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_missing_main_collection_returns_error_no_raise(self, mock_db):
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        mock_db.get_value = MagicMock(return_value="BPR1")
        # PGCollection lookup returns empty.
        mock_db.sql = MagicMock(side_effect=[[]])
        mock_db.set_value = MagicMock()

        result = backfill_main_collection("BT01", dry_run=True)

        self.assertEqual(result["bpr"], "BPR1")
        self.assertEqual(result["candidates"], 0)
        self.assertTrue(any("'main' PGCollection" in e for e in result["errors"]))

    @patch(f"{BACKFILL_MOD}.bulk_add_to_group_with_circuit_breaker")
    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_real_run_zero_candidates_returns_zero_not_none(self, mock_db, mock_helper):
        """Real-run with 0 main-eligible PEs: member_count_after MUST be 0,
        not None — None misleads the operator summary into looking like
        nothing ran. The DB member_count is intentionally not touched (there
        may be legitimate non-active main-eligible contacts in MAIN we don't
        want to lie about)."""
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        self._wire_db(mock_db, pes=[])

        result = backfill_main_collection(
            "BT01", dry_run=False, i_know_this_is_destructive=True,
        )

        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["member_count_after"], 0)
        # Helper never invoked (no candidates) and DB never written.
        mock_helper.assert_not_called()
        mock_db.set_value.assert_not_called()

    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_dry_run_zero_candidates_leaves_member_count_after_none(self, mock_db):
        """Dry-run is a preview — `member_count_after = None` signals 'we did
        not measure or write; re-run without dry_run to act'."""
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        self._wire_db(mock_db, pes=[])

        result = backfill_main_collection("BT01", dry_run=True)

        self.assertEqual(result["candidates"], 0)
        self.assertIsNone(result["member_count_after"])

    def test_shared_coerce_bool_handles_string_falsey_values(self):
        """The shared `coerce_bool` lives in `migrations/__init__.py` after the
        CR-029 review pulled it out of the two migration modules (L-074). It
        must reject the same string-falsey values both call sites depend on."""
        from tap_lms.summer_program.migrations import coerce_bool

        # Truthy spellings the operator might pass — non-empty non-zero strings
        # come back True.
        self.assertTrue(coerce_bool("True"))
        self.assertTrue(coerce_bool("yes"))
        self.assertTrue(coerce_bool("1"))
        self.assertTrue(coerce_bool(1))
        self.assertTrue(coerce_bool(True))
        # Falsey spellings (the whole point of this helper).
        self.assertFalse(coerce_bool("False"))
        self.assertFalse(coerce_bool("false"))
        self.assertFalse(coerce_bool("0"))
        self.assertFalse(coerce_bool("no"))
        self.assertFalse(coerce_bool("None"))
        self.assertFalse(coerce_bool(""))
        self.assertFalse(coerce_bool(False))
        self.assertFalse(coerce_bool(None))

    def test_sweep_migration_coerce_bool_resolves_to_shared(self):
        """The CR-029 review extraction must not break `sweep_migration._coerce_bool`
        — existing callers (and the local alias preserved at module load) must
        still resolve to the package-level coerce_bool."""
        from tap_lms.summer_program.migrations import coerce_bool as shared
        from tap_lms.summer_program.migrations import sweep_migration

        self.assertIs(sweep_migration._coerce_bool, shared)

    @patch(f"{BACKFILL_MOD}.bulk_add_to_group_with_circuit_breaker")
    @patch(f"{BACKFILL_MOD}.frappe.db")
    def test_returns_full_documented_summary_shape(self, mock_db, mock_helper):
        """Pin the public response contract — operators paste this dict into
        a ticket; missing keys here mean a silent UX regression."""
        from tap_lms.summer_program.migrations.main_collection_backfill import (
            backfill_main_collection,
        )
        pes = [{"name": "PE1", "glific_id": "g1"}]
        self._wire_db(mock_db, pes=pes)
        mock_helper.return_value = {
            "chunks_attempted": 1, "chunks_failed": 0, "added": 1,
            "errors": [], "circuit_tripped": False, "skipped_after_trip": 0,
        }

        result = backfill_main_collection(
            "BT01", dry_run=False, i_know_this_is_destructive=True,
        )

        expected_keys = {
            "batch", "bpr", "dry_run", "candidates", "group_id",
            "chunks_attempted", "chunks_failed", "added",
            "circuit_tripped", "skipped_after_trip",
            "member_count_after", "errors",
        }
        self.assertEqual(set(result.keys()), expected_keys)
        self.assertEqual(result["batch"], "BT01")
        self.assertEqual(result["bpr"], "BPR1")
        self.assertEqual(result["dry_run"], False)


if __name__ == "__main__":
    import unittest
    unittest.main()
