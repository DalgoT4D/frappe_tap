"""
tap_plg/docker/tap_plg_stub/app_stub.py

Single-process stub replacing BOTH tap_plg_worker and tap_plg_api.

Runs two things concurrently inside one process:
  1. RabbitMQ consumer  — consumes from SUBMISSION_QUEUE, publishes to FEEDBACK_QUEUE
  2. FastAPI HTTP server — serves /health on TAP_PLG_API_PORT (default 8080)

No CLIP model, no FAISS, no PostgreSQL required.
Every submission is marked "original" and forwarded to rag_service.
"""

import asyncio
import json
import logging
import os
import random
import time
import threading
from datetime import datetime, timezone

import aio_pika
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ── Config ────────────────────────────────────────────────────────────────────
RABBITMQ_HOST             = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT             = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER             = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS             = os.getenv("RABBITMQ_PASS", "guest")
RABBITMQ_VHOST            = os.getenv("RABBITMQ_VHOST", "/")
SUBMISSION_QUEUE          = os.getenv("SUBMISSION_QUEUE", "submission_queue")
FEEDBACK_QUEUE            = os.getenv("FEEDBACK_QUEUE", "plagiarism_queue")
APP_ENV                   = os.getenv("APP_ENV", "dev")
API_PORT                  = int(os.getenv("TAP_PLG_API_PORT", "8080"))
STUB_PROCESSING_DELAY_MS  = int(os.getenv("STUB_PROCESSING_DELAY_MS", "500"))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("tap_plg_stub")

# ── Shared state for health endpoint ─────────────────────────────────────────
_state = {
    "rabbitmq_connected": False,
    "messages_processed": 0,
    "started_at": datetime.now(timezone.utc).isoformat(),
}

# ══════════════════════════════════════════════════════════════════════════════
# FastAPI health server
# ══════════════════════════════════════════════════════════════════════════════

api = FastAPI(title="tap_plg stub", docs_url=None, redoc_url=None)

@api.get("/health")
def health():
    status = "healthy" if _state["rabbitmq_connected"] else "starting"
    return JSONResponse(
        content={
            "status": status,
            "service": "tap_plg_stub",
            "stub_mode": True,
            "rabbitmq_connected": _state["rabbitmq_connected"],
            "messages_processed": _state["messages_processed"],
            "started_at": _state["started_at"],
        },
        status_code=200,   # always 200 so compose healthcheck passes on startup
    )

@api.get("/stub/stats")
def stats():
    """Developer endpoint — shows stub processing statistics."""
    return JSONResponse(content=_state)


def _run_api():
    """Run FastAPI in a background thread so the async consumer can use the main loop."""
    uvicorn.run(api, host="0.0.0.0", port=API_PORT, log_level="warning")


# ══════════════════════════════════════════════════════════════════════════════
# Structured logging
# ══════════════════════════════════════════════════════════════════════════════

def _log(severity: str, message: str, **kwargs):
    payload = {
        "severity": severity,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "app": "tap_plg",
        "app_env": APP_ENV,
        "stub_mode": True,
    }
    payload.update({k: v for k, v in kwargs.items() if v is not None})
    print(json.dumps(payload, ensure_ascii=False), flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# RabbitMQ consumer
# ══════════════════════════════════════════════════════════════════════════════

def _make_result(data: dict) -> dict:
    """
    Build a plagiarism result matching the exact schema rag_service expects.
    Always marks submissions as original — no real ML processing.
    """
    result = {
        **data,
        "assignment_id": data.pop("assign_id", data.get("assignment_id", "")),
        "similar_sources":     [],
        "similarity_score":    round(random.uniform(0.01, 0.15), 4),
        "is_plagiarized":      False,
        "match_type":          "original",
        "plagiarism_source":   "",
        "is_ai_generated":     False,
        "ai_detection_source": "",
        "ai_confidence":       round(random.uniform(0.01, 0.08), 4),
    }
    result.pop("db_record_id", None)
    return result


async def _process(message: aio_pika.IncomingMessage, channel, feedback_queue: str):
    submission_id = "unknown"
    t0 = time.monotonic()
    try:
        data = json.loads(message.body.decode("utf-8"))
        submission_id = data.get("submission_id", "unknown")

        _log("INFO", "plg_submission_received",
             submission_id=submission_id,
             student_id=data.get("student_id"),
             submission_type=data.get("submission_type"))

        if STUB_PROCESSING_DELAY_MS > 0:
            await asyncio.sleep(STUB_PROCESSING_DELAY_MS / 1000)

        result = _make_result(data)

        await channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(result, ensure_ascii=False).encode(),
                content_type="application/json",
            ),
            routing_key=feedback_queue,
        )

        duration_ms = (time.monotonic() - t0) * 1000
        _log("INFO", "plg_result_published",
             submission_id=submission_id,
             plagiarism_status="original",
             is_plagiarized=False,
             is_ai_generated=False,
             total_duration_ms=round(duration_ms, 2))

        _state["messages_processed"] += 1
        await message.ack()

    except Exception as e:
        _log("ERROR", "detection_step_failed",
             submission_id=submission_id,
             step="stub_processing",
             error=str(e))
        await message.nack(requeue=True)


async def _consume():
    for attempt in range(1, 13):
        try:
            conn = await aio_pika.connect_robust(
                host=RABBITMQ_HOST, port=RABBITMQ_PORT,
                login=RABBITMQ_USER, password=RABBITMQ_PASS,
                virtualhost=RABBITMQ_VHOST,
            )
            _state["rabbitmq_connected"] = True
            logger.info(f"[STUB] Connected to RabbitMQ ({RABBITMQ_HOST}:{RABBITMQ_PORT})")
            break
        except Exception as e:
            logger.warning(f"[STUB] RabbitMQ attempt {attempt}/12: {e}")
            if attempt == 12:
                raise
            await asyncio.sleep(5)

    async with conn:
        ch = await conn.channel()
        await ch.set_qos(prefetch_count=1)

        sub_q = await ch.declare_queue(SUBMISSION_QUEUE, durable=True)
        await ch.declare_queue(FEEDBACK_QUEUE, durable=True)

        logger.info(f"[STUB] Listening on '{SUBMISSION_QUEUE}' → '{FEEDBACK_QUEUE}'")
        async with sub_q.iterator() as it:
            async for msg in it:
                await _process(msg, ch, FEEDBACK_QUEUE)


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint — run API in thread, consumer in async loop
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logger.info(f"[STUB] tap_plg stub starting (API on :{API_PORT}, no CLIP/FAISS/DB)")

    # Start FastAPI in a background thread
    api_thread = threading.Thread(target=_run_api, daemon=True)
    api_thread.start()
    logger.info(f"[STUB] Health endpoint: http://0.0.0.0:{API_PORT}/health")

    # Run RabbitMQ consumer in the main async loop
    asyncio.run(_consume())
