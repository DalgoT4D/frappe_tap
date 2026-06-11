# tap_lms/monitoring.py
#
# Structured logging core for tap_lms.
# All other monitoring modules import from here.
#
# Logs are emitted as JSON to stdout. On GCP VMs with the Cloud Ops Agent
# installed, stdout is automatically shipped to Cloud Logging and every
# field becomes a queryable key in Log Explorer.
#
# Locally (Docker), the same lines appear in `docker compose logs`.
#
# Log volume strategy (per client requirement):
#   - ERROR and WARNING logs always emitted
#   - INFO logs emitted only for business-critical pipeline events:
#       submission_published, feedback_*, glific_notification_sent,
#       plg_result_published, dispatcher_cycle (errors only)
#   - Routine http_request INFO logs emitted for metrics extraction
#     but filtered out of Cloud Logging storage via Ops Agent config
#
# IMPORTANT: Every function swallows its own exceptions.
# Monitoring must never crash the application.

import json
import os
import frappe
from frappe.utils import now_datetime

# Read once at import time — stable for process lifetime
_APP_ENV = os.environ.get("APP_ENV", "unknown")


def emit_structured_log(severity: str, message: str, **kwargs) -> None:
    """
    Emit a single JSON log line to stdout.

    All kwargs are included as top-level fields in the JSON object and
    become queryable in GCP Cloud Logging.

    Args:
        severity: "INFO" | "WARNING" | "ERROR" | "CRITICAL"
        message:  short machine-readable event name e.g. "submission_published"
        **kwargs: any additional fields (submission_id, duration_ms, etc.)
    """
    try:
        payload = {
            "severity": severity,
            "message": message,
            "timestamp": str(now_datetime()),
            "app": "tap_lms",
            "app_env": _APP_ENV,
        }
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    except Exception:
        pass


# ── HTTP request metrics ──────────────────────────────────────────────────────

def record_request(
    path: str,
    method: str,
    status_code: int,
    duration_ms: float,
    student_id: str = None,
    glific_id: str = None,
    user: str = None,
) -> None:
    """
    Emit one log line per HTTP request. Called by middleware.after_request.

    Includes student_id and glific_id when available so every Glific→tap_lms
    API call is attributable to a specific student in Cloud Logging.

    Log volume note: http_request INFO logs are emitted here for metrics
    extraction (P95 latency, error rate) but the Ops Agent config filters
    them out of long-term Cloud Logging storage. Only ERROR status requests
    are stored. See infra/ops_agent/frappe_vm_config.yaml.
    """
    try:
        emit_structured_log(
            severity="INFO" if status_code < 400 else "ERROR",
            message="http_request",
            http_path=path,
            http_method=method,
            http_status=status_code,
            duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            student_id=student_id,
            glific_id=glific_id,
            user=user or (frappe.session.user if hasattr(frappe, "session") else None),
        )
    except Exception:
        pass


# ── Background job metrics ────────────────────────────────────────────────────

def record_job(
    job_name: str,
    status: str,
    duration_ms: float = None,
    error: str = None,
    **extra,
) -> None:
    """
    Emit one log line per background job execution.

    Args:
        job_name:    e.g. "run_daily_actions", "run_escalation_check"
        status:      "success" | "error" | "skip"
        duration_ms: total wall-clock time for the job
        error:       exception string if status == "error"
        **extra:     any additional context fields
    """
    try:
        emit_structured_log(
            severity="INFO" if status in ("success", "skip") else "ERROR",
            message="background_job",
            job_name=job_name,
            job_status=status,
            duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
            error=str(error) if error else None,
            **extra,
        )
    except Exception:
        pass


def record_dispatcher_cycle(
    processed: int,
    skipped: int,
    errors: int,
    duration_ms: float,
    queue_depth: int = None,
) -> None:
    """
    Specific metric for pe_dispatcher — the 1-minute hot cron path.

    Emitted as message="dispatcher_cycle" so it can be filtered separately
    from generic background_job events in Cloud Logging.
    Only emits at ERROR severity (stored long-term) when errors > 0.
    INFO cycles are captured by metrics but not stored.

    queue_depth: remaining PEs with next_action_at <= now after this cycle.
    Used by the Cloud Monitoring dashboard lag indicator (architecture §8.8).
    """
    try:
        emit_structured_log(
            severity="ERROR" if errors > 0 else "INFO",
            message="dispatcher_cycle",
            processed=processed,
            skipped=skipped,
            errors=errors,
            queue_depth=queue_depth,
            duration_ms=round(duration_ms, 2),
        )
    except Exception:
        pass


# ── Submission pipeline tracing ───────────────────────────────────────────────

def record_submission_published(
    submission_id: str,
    student_id: str,
    assign_id: str,
    submission_type: str = None,
    queue: str = None,
    queue_name: str = None,   # accepted alias — submission.py passes queue_name
) -> None:
    """
    Emitted immediately after the message is published to RabbitMQ.

    Accepts both `queue` and `queue_name` so that call sites using either
    keyword work correctly. The value is stored in the log as `queue`.
    """
    try:
        emit_structured_log(
            severity="INFO",
            message="submission_published",
            submission_id=submission_id,
            student_id=student_id,
            assign_id=assign_id,
            submission_type=submission_type,
            queue=queue or queue_name,   # normalise to a single field name
        )
    except Exception:
        pass


def record_feedback_result_received(
    submission_id: str,
    student_id: str = None,
) -> None:
    try:
        emit_structured_log(
            severity="INFO",
            message="feedback_result_received",
            submission_id=submission_id,
            student_id=student_id,
        )
    except Exception:
        pass


def record_feedback_processing_complete(submission_id: str) -> None:
    try:
        emit_structured_log(
            severity="INFO",
            message="feedback_processing_complete",
            submission_id=submission_id,
        )
    except Exception:
        pass


def record_feedback_processing_failed(
    submission_id: str,
    error: str,
    retryable: bool,
    failure_reason: str = None,   # classify_error() reason string e.g. "not_found"
    error_type: str = None,       # exception class name e.g. "ValueError"
    retry_count: int = None,      # RabbitMQ delivery_count if available
    student_id: str = None,
) -> None:
    try:
        emit_structured_log(
            severity="ERROR",
            message="feedback_processing_failed",
            submission_id=submission_id,
            student_id=student_id,
            error=error,
            error_type=error_type,
            retryable=retryable,
            failure_reason=failure_reason,
            retry_count=retry_count,
        )
    except Exception:
        pass


def record_glific_notification(
    submission_id: str,
    success: bool,
    error: str = None,
) -> None:
    try:
        emit_structured_log(
            severity="INFO" if success else "WARNING",
            message="glific_notification_sent",
            submission_id=submission_id,
            success=success,
            error=str(error) if error else None,
        )
    except Exception:
        pass
