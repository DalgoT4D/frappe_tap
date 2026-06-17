# -*- coding: utf-8 -*-
# Copyright (c) 2026, TAP LMS
# File: tap_lms/journey/background_jobs.py
#
# DEPRECATED SHIM — module relocated 2026-05-28.
#
# This module's real implementation moved to
# `tap_lms.summer_program.background_jobs` as part of the journey/ → summer_program/
# pre-launch consolidation (all live SP code in one folder; journey/ is now
# 100% deprecated stubs).
#
# This file is a thin re-export shim so that:
#   (a) In-flight RQ jobs enqueued with the old module path
#       `tap_lms.journey.background_jobs.<func>` before the deploy still
#       resolve correctly when a worker picks them up after the deploy.
#   (b) Any external import `from tap_lms.journey.background_jobs import X`
#       still works for one release cycle.
#
# All NEW enqueue call sites should reference the new module path:
#   `tap_lms.summer_program.background_jobs.<func>`
#
# Sites already migrated 2026-05-28: 4 sites in
# summer_program/student_progression_sp.py (the live enqueue path).
#
# Delete this shim after one release cycle once `bench show-pending-jobs`
# confirms no queued jobs reference the old path.

from tap_lms.summer_program.background_jobs import (  # noqa: F401
    job_log_content_completion,
    job_update_statistics,
    job_finalize_quiz,
    get_content_name,
    retry_failed_content_logs,
    reconcile_statistics,
)
