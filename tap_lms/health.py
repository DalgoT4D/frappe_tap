"""
tap_lms/health.py

Health check endpoint for GCP Uptime Checks.

Endpoint: GET /api/method/tap_lms.health.check
Auth:      allow_guest=True  (no Frappe session needed — GCP probes anonymously)

Returns HTTP 200 with a JSON body when the service is healthy.
Returns HTTP 503 when a critical dependency is unavailable.

GCP Uptime Check configuration:
    URL:      https://<domain>/api/method/tap_lms.health.check
    Interval: 1 minute
    Expect:   HTTP 200
"""

import frappe
import json
import time
from frappe import _


@frappe.whitelist(allow_guest=True)
def check():
    """
    Run all health checks and return a summary.

    Critical checks (failure → HTTP 503):
        - database   : SELECT 1 against PostgreSQL
        - redis      : PING to Frappe cache Redis
        - rq_workers : at least one RQ worker is active

    Non-critical checks (failure → warning in body, still 200):
        - rabbitmq   : lightweight connection test (2s timeout)
    """
    results = {}
    overall_healthy = True

    # ── Database ──────────────────────────────────────────────────────────────
    try:
        t0 = time.monotonic()
        frappe.db.sql("SELECT 1")
        results["database"] = {
            "status": "ok",
            "latency_ms": round((time.monotonic() - t0) * 1000, 2),
        }
    except Exception as e:
        results["database"] = {"status": "error", "error": str(e)}
        overall_healthy = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    try:
        t0 = time.monotonic()
        frappe.cache().ping()
        results["redis"] = {
            "status": "ok",
            "latency_ms": round((time.monotonic() - t0) * 1000, 2),
        }
    except Exception as e:
        results["redis"] = {"status": "error", "error": str(e)}
        overall_healthy = False

    # ── RQ workers ────────────────────────────────────────────────────────────
    try:
        import redis
        from rq import Queue
        from rq.worker import Worker

        r = redis.from_url(frappe.conf.get("redis_queue"))
        workers = Worker.all(connection=r)
        worker_count = len(workers)
        if worker_count == 0:
            results["rq_workers"] = {"status": "error", "active_workers": 0}
            overall_healthy = False
        else:
            results["rq_workers"] = {"status": "ok", "active_workers": worker_count}
    except Exception as e:
        results["rq_workers"] = {"status": "error", "error": str(e)}
        overall_healthy = False

    # ── RabbitMQ (non-critical) ───────────────────────────────────────────────
    try:
        import pika
        settings = frappe.get_single("RabbitMQ Settings")
        credentials = pika.PlainCredentials(settings.username,
                                            settings.get_password("password"))
        params = pika.ConnectionParameters(
            host=settings.host,
            port=int(settings.port),
            virtual_host=settings.virtual_host,
            credentials=credentials,
            socket_timeout=2,
            connection_attempts=1,
        )
        conn = pika.BlockingConnection(params)
        conn.close()
        results["rabbitmq"] = {"status": "ok"}
    except Exception as e:
        # Non-critical — degraded but not down
        results["rabbitmq"] = {"status": "warning", "error": str(e)}

    # ── App version ───────────────────────────────────────────────────────────
    try:
        from tap_lms import __version__
        results["app_version"] = __version__
    except Exception:
        results["app_version"] = "unknown"

    # ── Response ──────────────────────────────────────────────────────────────
    body = {
        "status": "healthy" if overall_healthy else "unhealthy",
        "checks": results,
    }

    if not overall_healthy:
        frappe.local.response["http_status_code"] = 503

    frappe.local.response["type"] = "json"
    frappe.local.response["message"] = body
