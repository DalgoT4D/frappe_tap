#!/usr/bin/env python3
"""
Mock feedback producer for local development.
Publishes randomized feedback payloads for pending submissions to the local RabbitMQ queue.

Usage (inside bench):
    bench execute tap_lms.scripts.mock_feedback_producer  # not ideal
    
Or run directly from the bench environment:
    cd /path/to/frappe-bench
    python apps/frappe_tap/scripts/mock_feedback_producer.py [--count N]
"""

import argparse
import json
import random
from datetime import datetime

import frappe
import pika

# ---------------------------------------------------------------------------
# Randomisation data
# ---------------------------------------------------------------------------

SCENARIOS = [
    # (is_plagiarized, is_ai_generated, match_type, plagiarism_source, similarity_score, ai_confidence)
    (False, False, "original",              "none",                  0.0,  0.0),
    (False, False, "resubmission_allowed",  "none",                  0.0,  0.0),
    (False, True,  "ai_generated",          "none",                  0.0,  0.92),
    (True,  False, "exact_duplicate",       "peer",                  0.98, 0.0),
    (True,  False, "near_duplicate",        "peer_collusion",        0.81, 0.0),
    (True,  False, "semantic_match",        "self_cross_assignment", 0.75, 0.0),
    (True,  False, "exact_duplicate",       "self_late_resubmission",0.99, 0.0),
    (True,  False, "exact_duplicate",       "reference",             0.88, 0.0),
]

STRENGTHS = [
    "Good use of colour",
    "Creative composition",
    "Clear lines and shapes",
    "Expressive use of texture",
    "Strong understanding of the brief",
]

IMPROVEMENTS = [
    "Add more detail to the background",
    "Experiment with shading",
    "Try a wider variety of colours",
    "Work on proportions",
    "Review the assignment guidelines",
]

OBJECTIVES = [
    "Demonstrates understanding of visual balance",
    "Shows creativity in interpretation",
    "Applies learned techniques effectively",
]

ENCOURAGEMENTS = [
    "Keep up the great work!",
    "You're improving every week!",
    "We believe in your creative abilities!",
    "Every submission makes you better!",
]

LANGUAGES = ["Hindi", "English", "Marathi", "Punjabi", "Kannada"]

OVERALL_FEEDBACK_TEMPLATES = [
    "Hi Champ! Your work shows real effort. {encouragement}",
    "Great submission! I can see you worked hard on this. {encouragement}",
    "Nice work this week! Keep experimenting with your style. {encouragement}",
]


def _build_payload(submission_id: str, student_id: str, assignment_id: str) -> dict:
    scenario = random.choice(SCENARIOS)
    is_plagiarized, is_ai_generated, match_type, plagiarism_source, similarity_score, ai_confidence = scenario

    language = random.choice(LANGUAGES)
    encouragement = random.choice(ENCOURAGEMENTS)
    grade = 0 if (is_plagiarized or is_ai_generated) else round(random.uniform(50, 100), 1)

    if is_ai_generated:
        overall_feedback = "Hi Champ, I found you have sent AI created work. Please send your work. I'm excited to see what you made."
        overall_feedback_translated = overall_feedback
    elif is_plagiarized:
        overall_feedback = "Hi Champ, I found you have sent another student's work. Please send your own work."
        overall_feedback_translated = overall_feedback
    else:
        overall_feedback = random.choice(OVERALL_FEEDBACK_TEMPLATES).format(encouragement=encouragement)
        overall_feedback_translated = overall_feedback  # mock: same text, real service would translate

    return {
        "submission_id": submission_id,
        "student_id": student_id,
        "assignment_id": assignment_id,
        "feedback": {
            "overall_feedback": overall_feedback,
            "overall_feedback_translated": overall_feedback_translated,
            "strengths": random.sample(STRENGTHS, k=2),
            "areas_for_improvement": random.sample(IMPROVEMENTS, k=2),
            "learning_objectives_feedback": random.sample(OBJECTIVES, k=2),
            "final_grade": grade,
            "encouragement": encouragement,
            "rubric_evaluations": [
                {
                    "Skill": "Content Knowledge",
                    "grade_value": grade,
                    "observation": "Good understanding of the topic." if grade > 0 else "N/A",
                },
                {
                    "Skill": "Creativity",
                    "grade_value": grade,
                    "observation": "Shows creative thinking." if grade > 0 else "N/A",
                },
            ],
            "plagiarism_output": {
                "is_plagiarized": is_plagiarized,
                "is_ai_generated": is_ai_generated,
                "match_type": match_type,
                "plagiarism_source": plagiarism_source,
                "similarity_score": similarity_score,
                "ai_detection_source": "C2PA: Unknown AI generator" if is_ai_generated else "",
                "ai_confidence": ai_confidence,
                "similar_sources": [],
            },
            "translation_language": language,
        },
        "generated_at": datetime.now().isoformat(),
    }


def _get_pending_submissions(count: int) -> list[dict]:
    return frappe.get_all(
        "Submission",
        filters={"status": "Pending"},
        fields=["name", "student", "assignment"],
        limit=count,
    )


def main():
    parser = argparse.ArgumentParser(description="Publish mock feedback messages to local RabbitMQ.")
    parser.add_argument("--count", type=int, default=5, help="Number of submissions to process (default: 5)")
    args = parser.parse_args()

    frappe.connect()

    settings = frappe.get_single("RabbitMQ Settings")
    queue = settings.feedback_results_queue

    credentials = pika.PlainCredentials(settings.username, settings.get_password("password"))
    connection = pika.BlockingConnection(pika.ConnectionParameters(
        host=settings.host,
        port=int(settings.port),
        virtual_host=settings.virtual_host,
        credentials=credentials,
    ))
    channel = connection.channel()
    channel.queue_declare(queue=queue, durable=True)

    submissions = _get_pending_submissions(args.count)
    if not submissions:
        print("No pending submissions found.")
        connection.close()
        return

    print(f"\n=== Mock Feedback Producer ===")
    print(f"Queue : {queue}")
    print(f"Found : {len(submissions)} pending submission(s)\n")

    for sub in submissions:
        payload = _build_payload(sub["name"], sub["student"], sub["assignment"])
        channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=json.dumps(payload, ensure_ascii=False),
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )
        scenario_label = payload["feedback"]["plagiarism_output"]["match_type"]
        print(f"  ✓ {sub['name']} → scenario: {scenario_label}, grade: {payload['feedback']['final_grade']}")

    connection.close()
    print(f"\nDone. {len(submissions)} message(s) published to '{queue}'.")


if __name__ == "__main__":
    main()
