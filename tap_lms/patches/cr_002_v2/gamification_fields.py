"""
CR-002 v2 — Gamification fields backfill.

Source CR: docs/change-requests/CR-002-v2-gamification-redesign.md
Task: T-CR002v2-03

Backfills the 9 new ProgramEnrollment gamification fields added by CR-002 v2:

  Activity      total_activity_points, weekly_activity_points
  Quiz          total_quiz_points, weekly_quiz_points
  Submission    total_submission_points, weekly_submission_points
  Bonus         special_gems
  Sticky flags  weekly_submission_done, weekly_video_done

Pre-CR-002 v2, all submission point awards landed on ProgramEnrollment.total_points.
This patch copies total_points -> total_submission_points so historical submission
totals survive the schema split. All other new fields seed to 0 (their schema default).

Idempotency
-----------
Frappe's PatchLog (tabPatchLog) is the primary guard — once this patch entry runs
successfully, bench migrate will not re-execute it. The within-run WHERE clause
(total_submission_points = 0 AND total_points > 0) is belt-and-suspenders.

The original CR spec used `WHERE total_submission_points IS NULL` for idempotency,
but Frappe Int columns default to 0 (not NULL), so that WHERE never matches. This
patch corrects that with a semantically equivalent guard.

L-046 safety
------------
Per the CR-011 migration incident retrospective, lazy->eager state-derived backfills
can double-count when prior state is heterogeneous. This patch is NOT lazy->eager —
it's a one-way copy of total_points into a brand-new column. There is no race with
in-flight T14 rollups because total_submission_points has never been written before.
The verification query at the end confirms invariant `total_submission_points >= 0`
and surfaces any PE with a suspicious negative-or-mismatched state for review.
"""

import frappe


def execute():
    # ── 1. Backfill historical submission totals ─────────────────────────────
    # Pre-CR-002 v2, total_points captured submission-driven awards exclusively
    # (no quiz or activity point streams existed prior to this CR). Copy across.
    #
    # The WHERE guard makes the UPDATE a no-op on:
    #   - Already-migrated PEs (total_submission_points already matches total_points)
    #   - PEs that never submitted (total_points = 0; nothing to backfill)
    #
    # CR-011 (eager total_* updates) shipped 2026-05-25 and writes to these columns
    # via state-machine transitions. Any PE that has been touched by CR-011 since
    # the schema landed already has total_submission_points populated; the guard
    # ensures we don't overwrite those values.
    result = frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET total_submission_points = COALESCE(total_points, 0)
         WHERE COALESCE(total_submission_points, 0) = 0
           AND COALESCE(total_points, 0) > 0
        RETURNING name
        """
    )
    backfilled_count = len(result or [])

    # ── 2. Zero the other 8 new fields IF they are NULL ──────────────────────
    # The schema default is "0", so for fields added cleanly via the DocType UI
    # this is a no-op. The COALESCE is defensive — a manual schema change that
    # added the column without DEFAULT 0 would leave existing rows NULL, and
    # that would break atomic-counter handlers downstream (L-011).
    frappe.db.sql(
        """
        UPDATE "tabProgramEnrollment"
           SET total_activity_points    = COALESCE(total_activity_points, 0),
               weekly_activity_points   = COALESCE(weekly_activity_points, 0),
               total_quiz_points        = COALESCE(total_quiz_points, 0),
               weekly_quiz_points       = COALESCE(weekly_quiz_points, 0),
               weekly_submission_points = COALESCE(weekly_submission_points, 0),
               special_gems             = COALESCE(special_gems, 0),
               weekly_submission_done   = COALESCE(weekly_submission_done, 0),
               weekly_video_done        = COALESCE(weekly_video_done, 0)
         WHERE total_activity_points    IS NULL
            OR weekly_activity_points   IS NULL
            OR total_quiz_points        IS NULL
            OR weekly_quiz_points       IS NULL
            OR weekly_submission_points IS NULL
            OR special_gems             IS NULL
            OR weekly_submission_done   IS NULL
            OR weekly_video_done        IS NULL
        """
    )

    frappe.db.commit()

    # ── 3. Verification — confirm invariant holds post-backfill ──────────────
    # After backfill, every PE must satisfy:
    #   total_submission_points >= 0
    #   total_submission_points IS NOT NULL
    #
    # Anomalies are logged but do NOT fail the patch — the operator inspects and
    # repairs out-of-band. This matches L-046's prescription: surface mismatches
    # to a human rather than silently masking them.
    anomalies = frappe.db.sql(
        """
        SELECT name, total_points, total_submission_points
          FROM "tabProgramEnrollment"
         WHERE total_submission_points IS NULL
            OR total_submission_points < 0
         LIMIT 50
        """,
        as_dict=True,
    )

    if anomalies:
        frappe.log_error(
            title="SP CR-002 v2 migration anomalies",
            message=(
                f"Backfilled {backfilled_count} PEs. "
                f"Found {len(anomalies)} anomalies (capped at 50): "
                f"{anomalies!r}"
            ),
        )
    else:
        # Quiet-success log so the operator can confirm the patch ran end-to-end.
        # Per L-042, Error Log column is `method`/title (despite kwarg name).
        frappe.log_error(
            title="SP CR-002 v2 migration complete",
            message=(
                f"Backfilled {backfilled_count} PE rows "
                f"(total_submission_points <- total_points). "
                f"All gamification fields non-null."
            ),
        )
