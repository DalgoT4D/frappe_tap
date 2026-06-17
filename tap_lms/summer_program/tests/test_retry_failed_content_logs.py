"""Regression for BUG-1 (2026-06-17): retry_failed_content_logs ran a MariaDB
`DATE_SUB(NOW(), INTERVAL 24 HOUR)` clause, which raises
`function date_sub(...) does not exist` on PostgreSQL (L-002). Fixed to
`NOW() - INTERVAL '24 hours'`.

This must be a REAL-DB test (FrappeTestCase) — the only way to catch a
Postgres-invalid SQL string is to actually execute it. A mock-stubbed test (as
in test_background_jobs.py) would pass against the broken SQL.

NOTE: kept in its OWN module on purpose. test_background_jobs.py replaces
`sys.modules["frappe"]` with a MagicMock in its import-stub helper and never
restores it, so a real-DB test cannot share that module. Run standalone:
  bench --site <site> run-tests --module \
    tap_lms.summer_program.tests.test_retry_failed_content_logs
"""
from frappe.tests.utils import FrappeTestCase

from tap_lms.summer_program.background_jobs import retry_failed_content_logs


class TestRetryFailedContentLogsPostgres(FrappeTestCase):
    def test_runs_on_postgres_and_returns_dict_shape(self):
        """Executes the real query against Postgres. Pre-fix this raised
        `function date_sub does not exist`; post-fix it returns the
        {"failed_jobs": int, "details": list} envelope."""
        res = retry_failed_content_logs()
        self.assertIsInstance(res, dict)
        self.assertIn("failed_jobs", res)
        self.assertIn("details", res)
        self.assertIsInstance(res["failed_jobs"], int)
        self.assertIsInstance(res["details"], list)
        # failed_jobs count must equal the number of detail rows returned
        self.assertEqual(res["failed_jobs"], len(res["details"]))
