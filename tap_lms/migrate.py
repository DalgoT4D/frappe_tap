from __future__ import annotations


def before_migrate() -> None:
    from tap_lms.patches.v0_3.student_bulk_import_job_processing_log_json import (
        execute as ensure_processing_log_json,
    )

    from tap_lms.patches.v0_3.normalize_student_bulk_import_job_processing_log_shape import (
        execute as normalize_processing_log_shape,
    )

    ensure_processing_log_json()
    normalize_processing_log_shape()
