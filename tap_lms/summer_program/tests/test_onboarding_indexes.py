"""
CR-004 Slice 1 — AC-8: index patch idempotency and index existence.

Tests:
1. After execute(), both idx_student_phone and idx_gclm_lookup exist in pg_indexes.
2. A second execute() does not raise (IF NOT EXISTS idempotency).

EXPLAIN-shows-index-scan verification is NOT asserted here because:
- The test DB is typically too small for the planner to prefer an index scan
  over a seq scan.
- The test wraps each run in a FrappeTestCase transaction that is rolled back,
  so statistics may not be representative.
- EXPLAIN verification is a manual post-deploy step: after bench migrate +
  VACUUM ANALYZE, run:
      EXPLAIN (ANALYZE, BUFFERS) SELECT name FROM "tabStudent" WHERE phone = '9876543210';
  and verify "Index Scan using idx_student_phone" appears.

The backfill SQL correctness is tested implicitly via the patch executing
without error; full backfill semantics are confirmed manually post-deploy
by querying glific_sync_status distribution on Backend Students.
"""
import unittest
from unittest.mock import patch, MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase


class TestOnboardingIndexes(FrappeTestCase):
    """Verify the add_onboarding_indexes patch is idempotent and creates the
    expected Postgres indexes."""

    def _index_exists(self, indexname):
        """Query pg_indexes for the given index name."""
        rows = frappe.db.sql(
            "SELECT indexname FROM pg_indexes WHERE indexname = %s",
            (indexname,),
            as_dict=True,
        )
        return len(rows) > 0

    def test_indexes_created_after_execute(self):
        """Both idx_student_phone and idx_gclm_lookup must exist after execute().

        The patch uses CREATE INDEX IF NOT EXISTS so this is safe to run on a
        site that already has the indexes (e.g., second CI run).
        """
        from tap_lms.patches.cr_004_onboarding_throughput import add_onboarding_indexes

        # We do NOT want the patch to call frappe.db.commit() inside the test
        # because FrappeTestCase relies on transaction rollback for isolation.
        # Mock commit() so it is a no-op; the DDL still executes (DDL in PG
        # is transactional and will be visible within the same connection).
        with patch.object(frappe.db, "commit", MagicMock()):
            add_onboarding_indexes.execute()

        self.assertTrue(
            self._index_exists("idx_student_phone"),
            "idx_student_phone must exist in pg_indexes after execute()",
        )
        self.assertTrue(
            self._index_exists("idx_gclm_lookup"),
            "idx_gclm_lookup must exist in pg_indexes after execute()",
        )

    def test_idempotent_second_run(self):
        """Running execute() twice must not raise any exception.

        IF NOT EXISTS makes the CREATE INDEX statements no-ops on the second
        run.  The backfill UPDATE only touches rows WHERE glific_sync_status
        IS NULL / '' so it is also a no-op on an already-backfilled DB.
        """
        from tap_lms.patches.cr_004_onboarding_throughput import add_onboarding_indexes

        with patch.object(frappe.db, "commit", MagicMock()):
            # First run
            add_onboarding_indexes.execute()
            # Second run — must not raise
            try:
                add_onboarding_indexes.execute()
            except Exception as exc:
                self.fail(
                    f"add_onboarding_indexes.execute() raised on second run: {exc}"
                )

    def test_patch_survives_missing_glific_sync_status_column(self):
        """If glific_sync_status column does not exist, the backfill must be
        skipped gracefully (has_column guard, L-059).

        We simulate the absent-column case by forcing has_column to return
        False and verifying no SQL error is raised.
        """
        from tap_lms.patches.cr_004_onboarding_throughput import add_onboarding_indexes

        with patch.object(frappe.db, "commit", MagicMock()), \
             patch.object(frappe.db, "has_column", return_value=False), \
             patch.object(frappe.db, "table_exists", return_value=True):
            try:
                add_onboarding_indexes.execute()
            except Exception as exc:
                self.fail(
                    f"execute() must not raise when glific_sync_status column "
                    f"is absent (has_column=False): {exc}"
                )

    def test_patch_survives_missing_table(self):
        """If the Backend Students table does not exist at all, the backfill
        must be skipped (table_exists guard, L-061).
        """
        from tap_lms.patches.cr_004_onboarding_throughput import add_onboarding_indexes

        with patch.object(frappe.db, "commit", MagicMock()), \
             patch.object(frappe.db, "table_exists", return_value=False):
            try:
                add_onboarding_indexes.execute()
            except Exception as exc:
                self.fail(
                    f"execute() must not raise when Backend Students table "
                    f"does not exist (table_exists=False): {exc}"
                )


if __name__ == "__main__":
    unittest.main()
