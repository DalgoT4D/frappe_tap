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
# IMPORTANT: Every monitoring function swallows its own exceptions.
# Monitoring must never crash the application.
#
# to enable log recycling where old logs are over written on 14th day:
# sudo nano /etc/logrotate.d/frappe-gcp-structured
# /home/gcp-data/frappe-bench/logs/gcp_structured.log {
#     daily
#     missingok
#     rotate 14
#     compress
#     delaycompress
#     notifempty
#     copytruncate
#     create 0664
# }
#
# Important: refer to ../docs/log-config.md file for more details

import json
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler

import frappe
from frappe.utils import now_datetime

# Read once at import time — stable for process lifetime
_APP_ENV = os.environ.get("APP_ENV", "unknown")


def _get_dynamic_log_path() -> str:
    """
    Safely resolves the absolute path to the logs directory across
    any development, staging, or production server layout.
    """
    # 1. Preferred: derive the bench root from frappe.local.sites_path, which
    # Frappe sets during frappe.init() to the *real* `<bench>/sites` directory.
    # This is reliable regardless of how tap_lms was installed (symlinked into
    # apps/tap_lms, or pip-installed editable straight from a bind-mounted
    # source dir, as in local podman setup)
    try:
        sites_path = getattr(frappe.local, "sites_path", None)
        if sites_path:
            bench_path = os.path.dirname(os.path.abspath(sites_path))
            if bench_path and bench_path.strip():
                return os.path.join(bench_path, "logs", "gcp_structured.log")
    except Exception as e:
        print(f"Hit exception in generate log path (sites_path) ${e}")

    # 2. Fallback: frappe.utils.get_bench_path().
    try:
        bench_path = frappe.utils.get_bench_path()
        if bench_path and bench_path.strip():
            return os.path.join(bench_path, "logs", "gcp_structured.log")
    except Exception as e:
        print(f"Hit exception in generate log path (get_bench_path) ${e}")

    # 3. Last resort: always-writable temp location.
    return "/tmp/gcp_structured.log"


def _get_configured_logger() -> logging.Logger:
    """
    Dynamically configures and returns the logger using a live path.
    """
    logger = logging.getLogger("gcp_structured_logger")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't also hand records to the root logger

    # Resolve the path LIVE right now
    log_file_path = _get_dynamic_log_path()

    # If the handler already matches this path, don't re-add it
    if logger.handlers:
        # Check if the existing file handler is pointing to the right spot
        existing_handler = logger.handlers[0]
        if isinstance(
            existing_handler, logging.FileHandler
        ) and existing_handler.baseFilename == os.path.abspath(log_file_path):
            return logger
        # If it's pointing to a broken path (like /logs), clear it out
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    try:
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,  # keep last 5 files; so total 5 * 10 = 50MB of space
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(file_handler)
    except Exception as e:
        print(
            f"[monitoring] file handler setup failed for {log_file_path}: {e}",
            file=sys.stderr,
        )
        # Emergency fallback to standard error stream if container permissions reject disk operations
        if not logger.handlers:
            stream_handler = logging.StreamHandler(sys.stderr)
            logger.addHandler(stream_handler)

    return logger


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
        request_id = None

        # Pull request tracking ID out of the active execution thread
        # this requires nginx config change to append the request id
        # to the request header:
        #   /home/gcp-data/frappe-bench/config/nginx.conf
        #   proxy_set_header X-Request-Id $request_id;
        # and a config change in /etc/supervisor/conf.d/frappe-bench.conf
        # to get the request_id in the header:
        #   command=/home/gcp-data/frappe-bench/env/bin/gunicorn -b 127.0.0.1:8000 -w 5 --max-requests 5000 --max-requests-jitter 500 -t 120 --graceful-timeout 30 frappe.app:application --preload --capture-output --access-logformat '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" [Request-ID: %{REQUEST_ID}e]'

        if hasattr(frappe.local, "request") and frappe.local.request:
            # 1. Check if Frappe/Werkzeug automatically parsed and assigned it
            request_id = getattr(frappe.local.request, "unique_id", None)

            # 2. Fallback: Search the raw WSGI environment keys directly
            if not request_id and hasattr(frappe.local.request, "environ"):
                env = frappe.local.request.environ
                request_id = (
                    env.get(
                        "HTTP_X_REQUEST_ID"
                    )  # Standard Nginx proxy header translation
                    or env.get(
                        "REQUEST_ID"
                    )  # Raw Gunicorn internal environment variable
                    or env.get(
                        "HTTP_X_CORRELATION_ID"
                    )  # Common 3rd party vendor header variation
                )

        payload = {
            "severity": severity.upper(),
            "message": message,
            "timestamp": now_datetime().isoformat(),
            "request_id": request_id,
            "app": "tap_lms",
            "app_env": os.getenv("APP_ENV", "unknown"),
        }

        # Merge dynamic keyword parameters
        payload.update({k: v for k, v in kwargs.items() if v is not None})

        # Automatic exception tracing for error states
        if severity.upper() in ("ERROR", "CRITICAL") and sys.exc_info()[0] is not None:
            payload["exception"] = traceback.format_exc()

        # Emit 100% clean JSON string
        gcp_logger = _get_configured_logger()
        gcp_logger.info(json.dumps(payload, ensure_ascii=False))

    except Exception as e:
        # Fallback to stderr if the disk write stream encounters an OS failure
        sys.stderr.write(f"\n[STRUCTURED_LOG_FAIL] {str(e)}\n")
        sys.stderr.flush()


def emit(severity: str, message: str, **kwargs) -> None:
    try:
        emit_structured_log(severity=severity, message=message, **kwargs)
    except Exception as e:
        try:
            frappe.logger().info(f"[{severity}] {message} {kwargs}")
        except Exception:
            pass
        print(f"[monitoring] _emit failed for '{message}': {e}", flush=True)


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
    emit(
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


# ── Background job metrics ────────────────────────────────────────────────────

# v15 automatic job hooks (before_job / after_job in hooks.py, currently
# commented out — uncomment once bench is confirmed on v15).
#
# Frappe passes these keyword arguments:
#   before_job: method (dotted job name), kwargs, transaction_type
#   after_job:  method (dotted job name), kwargs, result
#
# They fire for every RQ/scheduler job automatically, giving the same
# coverage as the manual record_job() decorator pattern but without
# touching each scheduler function individually.

import time as _time  # local alias — avoid shadowing any frappe.utils.now imports

_job_start_times: dict = {}  # keyed by method name; good enough for single-threaded RQ workers


def before_job_hook(method: str = None, kwargs: dict = None, **_) -> None:
    """
    Called by Frappe v15 before_job hook for every background/scheduled job.
    Stores the start time so after_job_hook can compute duration.
    """
    try:
        _job_start_times[method or "unknown"] = _time.monotonic()
    except Exception:
        pass


def after_job_hook(method: str = None, kwargs: dict = None, result=None, **_) -> None:
    """
    Called by Frappe v15 after_job hook for every background/scheduled job.
    Emits a background_job log line with duration and outcome.

    Jobs that raise an unhandled exception are also caught by the
    Error Log doc_events hook (on_error_log_insert), so failures get
    two log lines: one here (job boundary) and one with the full traceback.
    """
    try:
        key = method or "unknown"
        t0 = _job_start_times.pop(key, None)
        duration_ms = (_time.monotonic() - t0) * 1000 if t0 is not None else None

        # A non-None result means the job completed without raising.
        # Frappe sets result=None on exception before calling after_job.
        status = "success" if result is not None else "error"

        record_job(
            job_name=key,
            status=status,
            duration_ms=duration_ms,
        )
    except Exception:
        pass


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
    emit(
        severity="INFO" if status in ("success", "skip") else "ERROR",
        message="background_job",
        job_name=job_name,
        job_status=status,
        duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
        error=str(error) if error else None,
        **extra,
    )


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
    emit(
        severity="ERROR" if errors > 0 else "INFO",
        message="dispatcher_cycle",
        processed=processed,
        skipped=skipped,
        errors=errors,
        queue_depth=queue_depth,
        duration_ms=round(duration_ms, 2),
    )


# ── Submission pipeline tracing ───────────────────────────────────────────────


def record_submission_published(
    submission_id: str,
    student_id: str,
    assign_id: str,
    submission_type: str = None,
    queue: str = None,
    queue_name: str = None,  # accepted alias — submission.py passes queue_name
) -> None:
    """
    Emitted immediately after the message is published to RabbitMQ.

    Accepts both `queue` and `queue_name` so that call sites using either
    keyword work correctly. The value is stored in the log as `queue`.
    """
    emit(
        severity="INFO",
        message="submission_published",
        submission_id=submission_id,
        student_id=student_id,
        assign_id=assign_id,
        submission_type=submission_type,
        queue=queue or queue_name,  # normalise to a single field name
    )


def record_feedback_result_received(
    submission_id: str,
    student_id: str = None,
) -> None:
    emit(
        severity="INFO",
        message="feedback_result_received",
        submission_id=submission_id,
        student_id=student_id,
    )


def record_feedback_processing_complete(submission_id: str) -> None:
    emit(
        severity="INFO",
        message="feedback_processing_complete",
        submission_id=submission_id,
    )


def record_feedback_processing_failed(
    submission_id: str,
    error: str,
    retryable: bool,
    failure_reason: str = None,  # classify_error() reason string e.g. "not_found"
    error_type: str = None,  # exception class name e.g. "ValueError"
    retry_count: int = None,  # RabbitMQ delivery_count if available
    student_id: str = None,
) -> None:
    emit(
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


# ── Unhandled exception tracing via Error Log ─────────────────────────────────


def on_error_log_insert(doc, method) -> None:
    """
    Called by the doc_events hook whenever Frappe writes an Error Log record.

    Frappe creates an Error Log automatically for every unhandled exception in
    both web requests (HTTP 500) and RQ/scheduler workers, so this gives us
    reliable structured coverage of all unhandled exceptions across both
    surfaces without any sys.excepthook or on_exception workaround.

    The Error Log doctype fields used here:
        doc.error           — full traceback string
        doc.method          — the whitelisted method / job function that raised
        doc.reference_doctype / doc.reference_name — linked document if any
    """
    try:
        # Truncate the traceback to keep the log line inside the 256 KB Cloud
        # Logging entry limit — the tail of a traceback is the most useful part.
        traceback_tail = (doc.error or "")[-2000:]

        emit(
            severity="ERROR",
            message="unhandled_exception",
            error_log=doc.name,
            method=doc.method or "unknown",
            traceback=traceback_tail,
            reference_doctype=doc.reference_doctype or None,
            reference_name=doc.reference_name or None,
        )
    except Exception:
        # Never let monitoring crash Frappe's own error handling path.
        pass


def record_glific_notification(
    submission_id: str,
    success: bool,
    error: str = None,
) -> None:
    emit(
        severity="INFO" if success else "WARNING",
        message="glific_notification_sent",
        submission_id=submission_id,
        success=success,
        error=str(error) if error else None,
    )
