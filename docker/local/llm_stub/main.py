"""
infra/docker/llm_stub/main.py

Lightweight FastAPI stub that mimics the OpenAI / TogetherAI / Vertex AI
response format used by rag_service's EvaluationGenerator.

Replaces real LLM API calls in local development so the full pipeline
can be tested end-to-end without incurring API costs or needing
production credentials.

Returns a realistic but randomised feedback JSON that matches the exact
schema tap_lms's feedback_consumer.py expects.

Usage:
  Configured in docker-compose.local.yml as service "llm-stub".
  Point rag_service's LLM provider base URL at http://llm-stub:8001.

Endpoints:
  POST /v1/chat/completions   — OpenAI-compatible chat endpoint
  POST /v1/completions        — legacy completions endpoint
  GET  /health                — health check
"""

import json
import logging
import random
import time
import uuid
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# remove health check pings from logs
class _HealthCheckFilter(logging.Filter):
    def filter(self, record):
        return "/health" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())

app = FastAPI(title="LLM Stub", description="Local LLM stub for pipeline testing")

# ── Canned feedback content ───────────────────────────────────────────────────
# Varied enough that repeated test submissions feel distinct, but structured
# exactly as rag_service's EvaluationGenerator produces.

_STRENGTHS = [
    "Strong use of colour contrast to create visual interest",
    "Confident line work that demonstrates good motor control",
    "Creative composition that fills the page effectively",
    "Good understanding of light and shadow relationships",
    "Expressive use of texture to add depth and character",
    "Clear focal point that draws the viewer's attention",
    "Thoughtful use of negative space in the composition",
    "Consistent and controlled brushwork throughout the piece",
]

_IMPROVEMENTS = [
    "Try varying line thickness to add more dynamism",
    "Consider adding more detail to the background elements",
    "Experiment with mixing colours directly on the canvas",
    "Work on proportions by observing the subject more carefully",
    "Add a mid-tone layer between the highlights and shadows",
]

_ENCOURAGEMENTS = [
    "Keep experimenting — every artwork teaches you something new!",
    "You are developing a unique artistic voice. Keep going!",
    "Great effort this week. Practice makes progress!",
    "Your creativity shines through in this piece. Well done!",
    "You are growing as an artist with every submission!",
]

_OVERALL_TEMPLATES = [
    "This is a {quality} piece of work that shows {quality2} understanding of the assignment. "
    "Your use of {technique} is particularly noteworthy.",
    "A {quality} submission that demonstrates {quality2} progress. "
    "The {technique} in this piece is well-executed.",
    "This artwork shows {quality} creativity and {quality2} technical skill, "
    "especially in the way you have handled {technique}.",
]

_QUALITIES = ["good", "strong", "impressive", "solid", "creative"]
_TECHNIQUES = ["colour", "composition", "line work", "shading", "texture"]

_RUBRIC_SKILLS = [
    "Content Knowledge",
    "Creativity",
    "Technical Skill",
    "Composition",
    "Use of Colour",
]


def _random_feedback(submission_id: str) -> Dict[str, Any]:
    """
    Generate a randomised but structurally valid feedback object.
    The submission_id is seeded into the random selection so the same
    submission always gets the same stub feedback (deterministic per run).
    """
    rng = random.Random(submission_id)

    quality = rng.choice(_QUALITIES)
    quality2 = rng.choice([q for q in _QUALITIES if q != quality])
    technique = rng.choice(_TECHNIQUES)

    overall = rng.choice(_OVERALL_TEMPLATES).format(
        quality=quality, quality2=quality2, technique=technique
    )

    strengths = rng.sample(_STRENGTHS, k=rng.randint(2, 3))
    improvements = rng.sample(_IMPROVEMENTS, k=rng.randint(1, 2))
    encouragement = rng.choice(_ENCOURAGEMENTS)
    final_grade = rng.randint(60, 95)

    rubric_evaluations = [
        {
            "Skill": skill,
            "grade_value": rng.randint(2, 4),
            "observation": f"Student demonstrated {rng.choice(_QUALITIES)} ability in {skill.lower()}.",
        }
        for skill in _RUBRIC_SKILLS
    ]

    return {
        "overall_feedback": overall,
        "overall_feedback_translated": f"[STUB TRANSLATION] {overall}",
        "strengths": strengths,
        "areas_for_improvement": improvements,
        "encouragement": encouragement,
        "rubric_evaluations": rubric_evaluations,
        "learning_objectives_feedback": [
            f"Objective met: {rng.choice(_TECHNIQUES)} was applied effectively."
        ],
        "final_grade": final_grade,
        "translation_language": "English",
    }


def _wrap_as_openai_response(content: str, model: str = "stub-gpt-4") -> Dict[str, Any]:
    """Wrap the feedback JSON string in an OpenAI-compatible chat completion response."""
    return {
        "id": f"chatcmpl-stub-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": random.randint(800, 1200),
            "completion_tokens": random.randint(200, 400),
            "total_tokens": random.randint(1000, 1600),
        },
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok", "service": "llm-stub"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint.
    Reads the submission_id from the prompt if present (for deterministic
    responses), otherwise uses a random ID.
    """
    body = await request.json()

    # Attempt to extract submission_id from the prompt for deterministic output
    submission_id = _extract_submission_id(body)

    # Simulate realistic LLM latency (0.5–2s locally)
    await _simulate_latency()

    feedback = _random_feedback(submission_id)
    content = json.dumps(feedback, ensure_ascii=False)

    model = body.get("model", "stub-gpt-4")
    return JSONResponse(content=_wrap_as_openai_response(content, model=model))


@app.post("/v1/completions")
async def completions(request: Request):
    """Legacy completions endpoint — same stub response."""
    body = await request.json()
    submission_id = _extract_submission_id(body)
    await _simulate_latency()
    feedback = _random_feedback(submission_id)
    content = json.dumps(feedback, ensure_ascii=False)
    return JSONResponse(
        content={
            "id": f"cmpl-stub-{uuid.uuid4().hex[:12]}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": body.get("model", "stub-gpt-4"),
            "choices": [{"text": content, "index": 0, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 900,
                "completion_tokens": 300,
                "total_tokens": 1200,
            },
        }
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_submission_id(body: Dict) -> str:
    """
    Try to find a submission_id in the prompt messages.
    Falls back to a random UUID so the stub always returns something valid.
    """
    try:
        messages: List[Dict] = body.get("messages", [])
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and "submission_id" in content.lower():
                # Simple extraction — look for "SUB-" prefix pattern
                import re

                match = re.search(r"SUB-[\w-]+", content, re.IGNORECASE)
                if match:
                    return match.group(0)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and "submission_id" in str(part).lower():
                        import re

                        match = re.search(r"SUB-[\w-]+", str(part), re.IGNORECASE)
                        if match:
                            return match.group(0)
    except Exception:
        pass
    return str(uuid.uuid4())


async def _simulate_latency():
    """Simulate realistic LLM response latency."""
    import asyncio

    delay = random.uniform(0.5, 2.0)
    await asyncio.sleep(delay)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
