# TAP LMS — Full System Understanding

**Version:** 1.7
**Date:** July 2026
**Purpose:** System architecture, user flows, and observability analysis across all five services — prepared for client validation.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Infrastructure](#2-infrastructure)
3. [The Five Services](#3-the-five-services)
4. [The Summer Program — Architecture Deep Dive](#4-the-summer-program--architecture-deep-dive)
5. [Complete User Flow — Student Submission](#5-complete-user-flow--student-submission)
6. [Complete User Flow — Quiz Assessment](#6-complete-user-flow--quiz-assessment)
7. [Full Pipeline — End to End](#7-full-pipeline--end-to-end)
8. [External Integrations Map](#8-external-integrations-map)
9. [Current Observability Gaps — Per Service](#9-current-observability-gaps--per-service)
10. [What a Stuck Submission Looks Like Today](#10-what-a-stuck-submission-looks-like-today)
11. [What a Stuck Submission Will Look Like After Monitoring](#11-what-a-stuck-submission-will-look-like-after-monitoring)
12. [Risks and Notable Code Issues Found](#12-risks-and-notable-code-issues-found)
13. [Monitoring Implementation Plan — All Three Services](#13-monitoring-implementation-plan--all-three-services)
14. [Glific ↔ tap_lms API Visibility — New Monitoring Scope](#14-glific--tap_lms-api-visibility--new-monitoring-scope)
15. [Local Development Environment and Testing Strategy](#15-local-development-environment-and-testing-strategy)
16. [Open Questions](#16-open-questions)
17. [DLQ Monitoring — Detailed Design](#17-dlq-monitoring--detailed-design)
18. [Error Classification — Retryable vs Non-Retryable Failures](#18-error-classification--retryable-vs-non-retryable-failures)

---

## 1. System Overview

TAP LMS is an online Learning Management System serving students in structured programs (including a Summer Program). Students submit artwork, text, audio, or video assignments via WhatsApp (through the Glific platform). Their submissions are automatically graded for plagiarism and AI-generated content, then evaluated by an AI model, with feedback delivered back to the student via WhatsApp in their local language with an audio component.

The system is composed of **five distinct services**. Three are built and maintained by the TAP team, one is a third-party open-source platform, and one is a separately hosted AI assistant engine.

```
                        Student (WhatsApp)
                              │
                           Glific  ──────── API calls ──────── tap_lms
                         (3rd Party)
                              │
                          tap_lms ────────<──────────────────────┐
                         (Frappe)                                │
                              │                                  │
                    [submission_queue]                    HTTP API calls:
                              │                           - assignment context
                          tap_plg  ───────────────────>─  - reference images
                    (Plagiarism Service)                  - student details
                              │                                  │
                   [plagiarism_feedback queue]                   │
                              │                                  │
                        rag_service  ──────────────────>─────────┘
                    (RAG / LLM Feedback)
                              │
                   [feedback_results_queue]
                              │
                          tap_lms
                         (Frappe)
                              │
                    ElevenLabs (TTS audio)
                              │
                           Glific
                              │
                        Student (WhatsApp)

----------------------------------------------------------------------------------------------
                                               ┌────────────────────────┐
                        Teacher / Student ───▶│     tap_ai             │
                        (via Telegram or       │  (Conversational AI    │
                         tap_lms integration)  │   Engine — Frappe)     │
                                               └──────────┬─────────────┘
                                                          │
                                           RabbitMQ Workers + Pinecone + PostgreSQL
                                                                      (local + tap_lms)
```

---

## 2. Infrastructure

### Hosting

The three services: `tap_lms`, `tap_plg`, `rag_service`, are hosted on **Google Cloud Platform (GCP)** using Compute Engine VMs. The development environment is an *e2-medium VM* (2 vCPUs, 4 GB RAM). Each service runs on its own dedicated VM in production (refer [Open Questions](#15-open-questions)). The fourth service, `tap_ai` is not directly called from `tap_lms` but it queries `tap_lms`'s postgres DB for required information.

### Deployment model per service

| Service | Runtime | Deployment | Database |
|---|---|---|---|
| `tap_lms` | Frappe 14.29.0 / Python | Frappe bench v5.16.2 on VM | PostgreSQL (Frappe-managed) |
| `rag_service` | Frappe 14.29.0 / Python | Frappe bench v5.16.2 on VM | PostgreSQL (Frappe-managed) |
| `tap_plg` | Standalone Python | Docker containers (docker-compose) | Dedicated PostgreSQL with pgvector extension + FAISS index on disk |
| `tap_ai` | Frappe 14.29.0 / Python | Frappe bench v5.16.2 on VM | PostgreSQL (local, tap_lms) + Pinecone + Redis |

### Message broker

All three services share a single **RabbitMQ instance hosted on CloudAMQP**. Three queues are in use:

| Queue name | Direction | Publisher | Consumer | Config method |
|---|---|---|---|---|
| `submission_queue` | tap_lms → tap_plg | tap_lms | tap_plg | DocType in tap_lms; `.env` in tap_plg |
| `plagiarism_feedback` | tap_plg → rag_service | tap_plg | rag_service | `.env` in tap_plg; DocType in rag_service |
| `feedback_results_queue` | rag_service → tap_lms | rag_service | tap_lms | DocType in both |

> **Note:** tap_lms and rag_service store queue names in the `RabbitMQ Settings` DocType (configurable via the Frappe UI). tap_plg stores them as environment variables with hardcoded defaults (`plagiarism_submissions` and `plagiarism_feedback`). These names must be kept in sync across all three services — a mismatch will cause submissions to silently disappear (refer [Open Questions](#15-open-questions)).

---

## 3. The Five Services

### 3.1 tap_lms (Core LMS — Frappe app)

**What it is:** The primary application. Manages all student data, assignments, submissions, program state, and external integrations. Built on the Frappe framework (Python), which provides DocTypes (database-backed data models), a REST API layer, a scheduler, and RQ-based background job processing.

**Key components:**

- **`summer_program/save_submission.py`** — **Active entry point for all student submissions.** Receives student submissions via API (`POST /api/method/tap_lms.summer_program.save_submission.save_submission`), uploads media to Google Cloud Storage, creates a `Submission` DocType record, and publishes the submission to RabbitMQ. Supports four media types: **image** (jpg, png, gif, webp, bmp, svg), **video** (mp4, mov, avi, mkv, webm), **audio** (mp3, wav, ogg, opus, m4a, aac, flac), and **text** (inline, no GCS upload). Media type is auto-detected from the file extension. Also provides `get_submission_feedback` (poll for feedback status) and `ready_to_receive_feedback` (trigger feedback delivery flow) endpoints.
- **`imgana/submission.py`** — **Deprecated.** The original submission entry point (`POST /api/method/tap_lms.imgana.submission.assignment_submission`). No longer in active use; all Glific flows should call `save_submission` instead.
- **`summer_program/student_progression_sp.py`** — Manages the quiz assessment flow. Key whitelisted endpoints: `start_quiz` (initialise a `StudentQuizAttempt`), `submit_answer` (one call per question — records answer, returns next question or final result), and the private `_complete_quiz_sp` (auto-triggered on the last answer — computes score, determines pass/fail, awards points). See Section 6 for the full quiz flow.
- **`feedback_handler/feedback_consumer.py`** — A long-running RabbitMQ consumer that receives graded feedback results, updates the `Submission` record, triggers the ElevenLabs TTS call (for audio feedback), and sends a Glific WhatsApp notification to the student.
- **`summer_program/pe_dispatcher.py`** — A scheduled job running **every 1 minute** that drives a state machine for every active Summer Program student. This is the most performance-critical background process in the system — it processes up to 100,000 students per cycle.
- **`summer_program/escalation_runner.py`** — Runs every 2 hours to handle students whose program progression has stalled past a threshold.
- **`summer_program/scheduler.py`** — Daily batch job for admin-level program actions.
- **`glific_integration.py`** — HTTP client for the Glific API (WhatsApp messaging). Manages OAuth token refresh, contact creation, and flow triggers.

**Scheduled jobs summary:**

| Job | Frequency | What it does |
|---|---|---|
| `pe_dispatcher` | Every 1 minute | Drives Summer Program student state machine |
| `escalation_runner` | Every 2 hours | Handles stalled student progressions |
| `run_daily_actions` | Daily | Batch admin actions |
| `dlq_monitor` | Every 5 minutes | Polls CloudAMQP Management API for DLQ depths; emits structured log per queue |

**External calls made by tap_lms:**

| External service | What for |
|---|---|
| RabbitMQ (CloudAMQP) | Publish submissions; consume feedback results |
| CloudAMQP Management API | Poll DLQ depths every 5 minutes (HTTPS REST — not AMQP) |
| Google Cloud Storage | Store submitted images, audio, video |
| Glific API | Send WhatsApp messages and feedback to students |
| ElevenLabs API | Generate multilingual audio feedback (TTS) |

---

### 3.2 tap_plg (Plagiarism Detection Service — standalone Python)

**What it is:** A completely independent Python service (not Frappe) that performs multi-method plagiarism and AI-content detection on student image submissions. It is the most computationally intensive service in the system. Runs in Docker containers with two processes: a background worker and a FastAPI HTTP API.

**Key components:**

- **`app.py`** — Main async entry point. Initialises the shared database connection pool, the `ImageWorker` (loads ML models), the `SubmissionChecker`, and the RabbitMQ consumer. Handles graceful shutdown on SIGTERM/SIGINT.
- **`image_worker/worker.py` (`ImageWorker`)** — The core processing engine. Runs five detection methods sequentially on each submission.
- **`plag_checker/submissions_checker.py` (`SubmissionChecker`)** — Orchestrates message consumption, calls the `ImageProcessor`, handles ACK/NACK logic with the `MessageAckManager`, and manages retries.
- **`api/api.py`** — FastAPI HTTP server. Provides REST endpoints for direct submission creation, result polling, and a health check endpoint at `GET /health`.
- **`mq/rmq_client.py` (`RabbitMQClient`)** — Async RabbitMQ client using `aio_pika`. Handles connection, queue declaration, message publishing, retry with exponential backoff, and dead letter queue support.
- **`database/db_manager.py`** — `asyncpg`-based PostgreSQL connection pool manager.

**External calls made by tap_plg:**

| External service | What for | Auth method |
|---|---|---|
| RabbitMQ (CloudAMQP) | Consume from `submission_queue`; publish to `plagiarism_feedback` | AMQP credentials |
| Google Cloud Storage | Download submitted images for processing | GCS credentials |
| PostgreSQL (own DB) | Store and query submission hashes and CLIP embeddings | DB credentials |
| **tap_lms HTTP API** | **Fetch assignment context and reference images per assignment** | `FRAPPE_API_KEY` + `FRAPPE_API_SECRET` |

---

### 3.3 rag_service (RAG Feedback Generation — Frappe app)

**What it is:** A Frappe application that receives the plagiarism-checked submission result from tap_plg and uses a Large Language Model (LLM) to generate personalised, rubric-based feedback for the student.

**Key components:**

- **`core/feedback_handler.py` (`FeedbackHandler`)** — Orchestrates the full feedback generation flow.
- **`core/feedback_service.py` (`FeedbackService`)** — Generates feedback; routes by plagiarism result.
- **`core/assignment_context_manager.py` (`AssignmentContextManager`)** — Makes HTTP calls back to tap_lms; implements caching.
- **`core/llm_providers.py`** — Three LLM provider implementations (OpenAI, TogetherAI, Vertex AI). Runtime configurable via `LLM Settings` DocType.

**External calls made by rag_service:**

| External service | What for |
|---|---|
| RabbitMQ (CloudAMQP) | Consume from `plagiarism_feedback`; publish to `feedback_results_queue` |
| tap_lms HTTP API | Fetch assignment context and student details |
| OpenAI / TogetherAI / Vertex AI | LLM feedback generation |

---

### 3.4 Glific (WhatsApp Communication Platform — third-party open source)

**What it is:** Glific is an open-source two-way WhatsApp communication platform. TAP uses a hosted instance as the student-facing communication layer. TAP does not own or modify Glific's codebase.

**How TAP uses Glific:** Students interact via WhatsApp; Glific flows call tap_lms API endpoints for data operations. When tap_lms needs to notify a student, it calls Glific's GraphQL API. Glific logs stream to BigQuery in real time — no additional export is needed for monitoring.

---

### 3.5 tap_ai (Conversational AI Engine — Frappe app)

**What it is:** A separate Frappe application that provides a conversational AI layer over TAP LMS data, supporting text and voice queries via intelligent routing (Knowledge Bank, Text-to-SQL, Vector RAG, Direct LLM). Hosted at `ai.evalix.xyz`. Uses RabbitMQ workers, Pinecone, and a remote PostgreSQL at `data.evalix.xyz`.

---

## 4. The Summer Program — Architecture Deep Dive

### 4.1 What it is and why it exists separately

The Summer Program is a time-bounded intensive program that must **proactively drive each student** through a week-by-week journey: deliver content on a schedule, escalate with nudges if the student goes silent, track whether they are keeping up or falling behind, eventually drop students who never engage, and graduate those who complete — all automatically, at the individual student level, across potentially 100,000 concurrent students.

### 4.2 The ProgramEnrollment state machine

Every Summer Program student gets a `ProgramEnrollment` (PE) Frappe document. Core fields:

| Field | What it holds |
|---|---|
| `resolved_flow_state` | Which stage of the week the student is in (11 possible states) |
| `current_week` | Which week of the program they are on |
| `next_action_at` | **When** the system should next act on this student |
| `next_action_type` | **What** the system should do at that time |
| `program_status` | `active`, `paused`, `completed`, or `dropped` |
| `grace_window_end_at` | Deadline for a late submission before auto-drop |

The 11 states form a directed graph within each week, with 25 named transitions (T0–T25) in `state_machine.py`.

### 4.3 The 1-minute dispatcher (pe_dispatcher.py)

`pe_dispatcher.py` runs **every 1 minute**. It queries all PEs where `next_action_at <= now` and routes each to one of six action handlers: `content_delivery`, `escalation`, `week_advancement`, `feedback_notification`, `grace_check`, `pause_check`. Uses `FOR UPDATE SKIP LOCKED` for parallel safety. Batch size of 1,000 PEs per cycle handles 100K student bursts.

### 4.4 The escalation chain

When a student does not respond to content delivery, they enter an escalation sequence. Steps have types (`help_note_a`, `help_note_b`, `voice_note`, `parent_call`) and `hours_after_previous` delays. `parent_call` steps route through `vocallabs.py` for automated phone calls to parents.

### 4.5 The archetype and A/B experiment system

Each student is assigned an **archetype** and **experiment arm** at enrollment, creating 8 Glific collections per batch. Collection-level flow triggers reduce API calls from 100,000 per cycle to 8.

### 4.6 The BatchProgramRun lifecycle

A `BatchProgramRun` (BPR) manages cohort setup through states: `draft → importing → enrolling → collections_ready → active → completed`.

### 4.7 Gamification

Each PE carries points, streaks, and gems synced to Glific contact fields on every transition.

### 4.8 The flow callback bridge (update_flow_status)

Every Glific SP flow calls `update_flow_status` on completion, bridging Glific's stateless flow engine to the tap_lms state machine.

---

## 5. Complete User Flow — Student Submission

The system supports **four submission types**: image, video, audio, and text. All four types are published to the same `submission_queue` — tap_plg processes image submissions; video, audio, and text bypass plagiarism checking by design.

> **Active endpoint:** `POST /api/method/tap_lms.summer_program.save_submission.save_submission`
> The legacy endpoint `POST /api/method/tap_lms.imgana.submission.assignment_submission` (`imgana/submission.py`) is **deprecated** and should not be used in new Glific flows.

```
Step 1 — Student sends artwork
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Student sends image via WhatsApp
    └── Glific receives the message
    └── Glific calls tap_lms API:
        POST /api/method/tap_lms.summer_program.save_submission.save_submission
        { assignment_id, student_id, submission }

Step 2 — tap_lms processes the submission
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
tap_lms (save_submission.py):
    ├── Authenticates the API key (Authorization header + active ProgramEnrollment check)
    ├── Validates student and assignment; guards against Glific placeholder strings
    ├── Creates a new Submission DocType record (status: "Pending")
    ├── Downloads media from Glific; uploads to Google Cloud Storage
    └── Calls enqueue_submission(submission.name)
            └── Publishes JSON message to [submission_queue]
    └── Returns { submission_id, student_id, status } to Glific

Step 3 — tap_plg detects plagiarism
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
tap_plg (SubmissionChecker):
    ├── Consumes message from [submission_queue]
    └── ImageWorker runs 5 detection steps sequentially
    └── Publishes result to [plagiarism_feedback queue]

Step 4 — rag_service generates feedback
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
rag_service (FeedbackHandler):
    ├── Consumes message from [plagiarism_feedback queue]
    ├── Routes by result: AI-generated / plagiarised → stock feedback; original → LLM call
    └── Publishes to [feedback_results_queue]

Step 5 — tap_lms delivers feedback to student
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
tap_lms (feedback_consumer.py):
    ├── Consumes message from [feedback_results_queue]
    ├── Updates Submission DocType (status: "Completed")
    ├── Sends Glific WhatsApp notification
    ├── Advances Summer Program state machine (T12 transition)
    └── Calls ElevenLabs TTS; uploads audio to GCS

Step 6 — Student requests and receives feedback
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Glific calls:
    POST /api/method/tap_lms.summer_program.save_submission.ready_to_receive_feedback
        └── Marks feedback as requested; triggers FeedbackConsumer flow if ready
    GET  /api/method/tap_lms.summer_program.save_submission.get_submission_feedback
        └── Returns { status, overall_feedback, audio_feedback_url }

Student receives on WhatsApp:
    ├── Text feedback in their local language
    └── Audio feedback (voice message)
```

---

## 6. Complete User Flow — Quiz Assessment

Quizzes are short assessments (typically 3–5 questions) delivered inline through the WhatsApp/Glific flow as part of a learning unit. Unlike submissions, quizzes are **entirely synchronous and self-contained within tap_lms** — no RabbitMQ, no GCS, no tap_plg or rag_service involvement.

**Active file:** `tap_lms/summer_program/student_progression_sp.py`
**Deprecated file:** `tap_lms/journey/student_progression.py` — contains one-line shims pointing to the above; do not use.

**Doctypes involved:** `Quiz`, `QuizQuestion`, `QuizOption` (+ translation variants), `StudentQuizAttempt`, `StudentQuizAnswer`

```
Step 1 — Quiz initiated
━━━━━━━━━━━━━━━━━━━━━━━
Glific calls:
    POST /api/method/tap_lms.summer_program.student_progression_sp.start_quiz
    { student_id, course_level, quiz_id, language }

tap_lms (start_quiz):
    ├── Resolves student_id → Student doc
    ├── Fetches active ProgramEnrollment (must be active or paused)
    ├── Creates StudentQuizAttempt (status: "in_progress")
    │       attempt_number tracks re-attempts for the same quiz
    ├── Loads all questions via _get_quiz_questions(quiz_doc)
    └── Returns first question as flat key/value response (Glific Rule 2):
        { quiz_attempt_id, total_questions, question_index=1,
          question_text, option_a, option_b, option_c, option_d }
    ✦ emit: quiz_started

    If a prior in-progress attempt exists → _resume_quiz():
        └── Returns the next unanswered question
        ✦ emit: quiz_resumed

Step 2 — Student answers each question (one call per question)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Glific calls for each answer:
    POST /api/method/tap_lms.summer_program.student_progression_sp.submit_answer
    { student_id, quiz_attempt_id, question_index, answer }
    answer: one of A, B, C, D

tap_lms (submit_answer):
    ├── Validates attempt ownership and status
    ├── Looks up correct_option from cached QuizQuestion
    ├── Records StudentQuizAnswer child row on the attempt
    │       fields: selected_option, correct_option, is_correct,
    │               started_at, answered_at, time_spent_seconds
    ├── Updates attempt.correct_answers (running total)
    │
    ├── If more questions remain:
    │       └── Returns next question in same response
    │           { status: "next_question", question_index, question_text,
    │             option_a..d, progress_answered, progress_correct }
    │           ✦ emit: quiz_answer_submitted (with was_correct, time_spent_seconds)
    │
    └── If last question (question_index == total_questions):
            └── Calls _complete_quiz_sp() inline — no separate API call needed

Step 3 — Quiz completion (auto-triggered on last answer)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
tap_lms (_complete_quiz_sp):
    ├── Computes score = correct_answers / total_questions × 100
    ├── Determines passed = score >= quiz.passing_score
    ├── Sets attempt.status = "passed" or "failed"
    ├── Awards points via gamification hook (stored as attempt.points_earned)
    ├── Advances Summer Program state machine:
    │       Core quiz FAIL   → advance to next content (no remedial switch)
    │       Remedial quiz FAIL → restart or continue remedial LU
    │       Remedial quiz PASS → advance (exit remedial for next week)
    └── Returns final result as flat response:
        { status: "quiz_passed" | "quiz_failed",
          score, correct_answers, total_questions,
          points_earned, feedback_message }
    ✦ emit: quiz_completed (score, passed, correct_answers, points_earned,
                            time_spent_seconds, attempt_number)
```

**Key design decisions:**
- **One API call per question** — Glific sends answers one at a time as the student responds in WhatsApp. There is no "batch submit all answers" path.
- **Server-side time tracking** — `started_at` / `answered_at` are recorded by tap_lms, not sent by Glific, so time-per-question is tamper-proof.
- **In-process question cache** (`cached_question_details`) — `QuizQuestion` docs are immutable after publishing; they are cached per process lifetime to avoid repeated DB reads during a quiz session.
- **Re-attempt support** — `start_quiz` creates a new `StudentQuizAttempt` with an incremented `attempt_number` if a prior completed attempt exists. If a prior *in-progress* attempt exists, it resumes from the last unanswered question.
- **Structured logging** — All four key events (`quiz_started`, `quiz_resumed`, `quiz_answer_submitted`, `quiz_completed`) emit structured JSON logs via `tap_lms/monitoring.py`, consistent with the rest of the Summer Program module.

---

### Timeline view of a single submission

```
T+0s     Student sends image on WhatsApp
T+1s     Glific calls tap_lms save_submission API
T+4s     tap_lms: Message published to [submission_queue]
T+5s     tap_plg: Message consumed
T+25s    tap_plg: All 5 detection steps complete
T+26s    tap_plg: Result published to [plagiarism_feedback queue]
T+27s    rag_service: Message consumed
T+60s    rag_service: LLM feedback generated
T+62s    rag_service: Result published to [feedback_results_queue]
T+63s    tap_lms: Message consumed by feedback_consumer
T+65s    tap_lms: Glific notification triggered
T+85s    tap_lms: Audio uploaded to GCS, Submission updated
```

### What can go wrong at each handoff

| Handoff point | Common failure modes |
|---|---|
| Glific → tap_lms | API key invalid; student not found; network timeout |
| tap_lms → GCS | Credentials expired; bucket permissions; large file timeout |
| tap_lms → RabbitMQ | Connection limit hit; queue full; credentials expired |
| RabbitMQ → tap_plg | Consumer not running; worker crashed; CLIP model OOM |
| tap_plg → RabbitMQ | Publish failure after long processing; connection dropped |
| RabbitMQ → rag_service | Consumer not running |
| rag_service → LLM provider | Rate limit; API key expired; model timeout |
| RabbitMQ → tap_lms consumer | Consumer not running; message rejected → DLQ |
| tap_lms → Glific | Token expired; student not in Glific; flow not found |

---

## 7. External Integrations Map

| Integration | Used by | Protocol | Risk if down |
|---|---|---|---|
| RabbitMQ (CloudAMQP) | All three services | AMQP | **Critical** — entire pipeline stalls |
| CloudAMQP Management API | tap_lms (DLQ monitor) | HTTPS REST | Low — monitoring only; does not affect pipeline |
| Google Cloud Storage | tap_lms, tap_plg | HTTPS | **Critical** — submissions cannot be stored or processed |
| Glific API | tap_lms | HTTPS (GraphQL) | **High** — students receive no notifications or feedback |
| ElevenLabs API | tap_lms | HTTPS | **Medium** — audio feedback unavailable; text feedback still delivered |
| OpenAI / TogetherAI / Vertex AI | rag_service | HTTPS | **High** — feedback generation fails for original submissions |
| tap_lms HTTP API | rag_service, tap_plg | HTTPS | **High** — assignment context unavailable |
| PostgreSQL (tap_plg's own) | tap_plg | TCP | **Critical** — plagiarism check cannot run |

---

## 8. Current Observability Gaps — Per Service

### 7.1 tap_lms

| What is happening | Is it visible? | Where logs go |
|---|---|---|
| Submission received from Glific | No structured log | — |
| Image uploaded to GCS | Plain text via `frappe.logger()` | Rotating file on disk |
| Message published to RabbitMQ | Plain text via `frappe.logger()` | Rotating file on disk |
| Feedback result consumed from RabbitMQ | Plain text via `frappe.logger()` | Rotating file on disk |
| Glific notification sent / failed | Plain text via `frappe.logger()` | Rotating file on disk |
| ElevenLabs TTS called | Plain text via `frappe.logger()` | Rotating file on disk |
| pe_dispatcher cycle ran | No structured log | — |
| pe_dispatcher cycle errors | No structured log | — |
| HTTP request latency | No log | — |
| Unhandled exceptions | Frappe Error Log (DB only) | Frappe's own DB table |
| **DLQ depth** | **Not monitored** | **—** |

**Root cause:** `frappe.logger()` writes to rotating `.log` files under `frappe-bench/logs/`. These are not shipped to GCP Cloud Logging. They are readable only via SSH on the VM and cannot be queried, alerted on, or visualised.

### 7.2 rag_service

| What is happening | Is it visible? | Where logs go |
|---|---|---|
| Submission message consumed | `print()` to stdout | Unstructured, not shipped |
| LLM call started / completed | `print()` to stdout | Unstructured, not shipped |
| Feedback generated successfully | `print()` to stdout | Unstructured, not shipped |
| Result published to tap_lms queue | `print()` to stdout | Unstructured, not shipped |

**Root cause:** The codebase uses `print()` throughout rather than structured logging.

### 7.3 tap_plg

| What is happening | Is it visible? | Where logs go |
|---|---|---|
| Submission consumed from queue | `logging.info()` — plain text | Docker container stdout |
| Per-step processing duration | **No timing logged** | — |
| DB pool exhaustion | `logging.error()` — plain text | Docker container stdout |

**Root cause:** tap_plg has the best logging foundation (Python `logging` module) but output is plain text, not JSON.

### 7.4 Glific

Glific logs stream to BigQuery in real time. The gap is on the tap_lms side — there is no corresponding structured export from tap_lms, so the two sides cannot currently be joined.

### 7.5 tap_ai

tap_ai has a `ai_query_log` DocType but it is not exported to GCP Cloud Logging. Worker processes use print statements.

---

## 9. What a Stuck Submission Looks Like Today

A support team member receives a complaint: "Student ST-00123 submitted their artwork 2 hours ago and has not received any feedback."

**Current investigation process:**

1. SSH into the VM
2. Open Frappe desk, search for the Submission record — status shows "Pending"
3. `grep "ST-00123" /home/frappe/frappe-bench/logs/*.log` — likely returns nothing
4. Check CloudAMQP management UI — see queue depths but no per-message tracking
5. SSH into the tap_plg container — check Docker logs — unstructured, hard to search
6. Check rag_service Frappe Bench logs — similar problem
7. No way to determine which of the 6 handoff points the submission is stuck at

**Time to identify the root cause: 30–60 minutes minimum.**

---

## 10. What a Stuck Submission Looks Like After Monitoring

**Cloud Logging query (takes 5 seconds):**

```
jsonPayload.submission_id="SUB-2024-00123"
```

**Result — all events in chronological order:**

```
2024-11-15 10:00:01  INFO   tap_lms    submission_published         student_id=ST-00123
2024-11-15 10:00:03  INFO   tap_plg    plg_submission_received      student_id=ST-00123
2024-11-15 10:00:09  ERROR  tap_plg    detection_step_failed        step=ai_detection  error="CUDA OOM"
```

**Conclusion in 5 seconds:** The CLIP/AI detection step crashed with an OOM error. The submission is stuck at step 3. Restart the tap_plg worker container; the message will be requeued automatically.

---

## 11. Risks and Notable Code Issues Found

### 10.1 Hardcoded student ID — Resolved (Internal only)

**File:** `tap_lms/imgana/submission.py` — `submit_artwork_internal()`

Confirmed by the client: `submit_artwork_internal()` is an **internal testing endpoint only** and is not exposed in production. No action required.

### 10.2 Versioned API files — Resolved (Legacy code)

Date-stamped files (`api_19_11_2025.py`, `api_28_11_25.py`) are legacy code. Only `api.py` is in use.

### 10.3 RabbitMQ consumer for rag_service is a CLI command — Resolved (Testing only)

`bench execute start-rag-consumer` is used for testing purposes only. Production consumer mechanism to be confirmed (OQ-10).

### 10.4 CLIP model memory footprint — Resolved (Separate higher-RAM VM)

tap_plg runs on a dedicated VM with higher RAM. Memory pressure from the CLIP model is not a concern in production.

### 10.5 Dead letter queue exists but is not monitored — Addressed in Section 16

Both tap_lms and tap_plg implement DLQs. Messages can accumulate silently indefinitely. This is addressed by the DLQ monitoring implementation in Section 16.

### 10.6 CloudAMQP connection limits

The free/low tier of CloudAMQP has hard connection limits. The health check endpoint opens an AMQP connection on every GCP Uptime Check (once per minute). The DLQ poller in Section 16 uses the HTTPS Management API — not an AMQP connection — and does not consume from the connection limit.

### 10.7 Non-image submissions routed through tap_plg (By design)

Image-only plagiarism checking is by design. However, all four submission types are currently published unconditionally to `submission_queue`. The routing logic in `enqueue_submission()` should branch by `submission_type` so non-image submissions bypass tap_plg.

### 10.8 Error classification in feedback_consumer — Addressed in Section 17

The current `is_retryable_error()` method uses string pattern matching to decide whether a failed message is retried or sent to the DLQ. This is fragile: unrecognised exception types are silently classified as retryable without any log record of why. See Section 17 for the full analysis and the `classify_error()` replacement design.

---

## 12. Monitoring Implementation Plan — All Three Services

### What will be added

#### tap_lms (4 new files, 7 modified files)

| File | Action | What it adds |
|---|---|---|
| `tap_lms/monitoring.py` | Create | Structured JSON logging core — all emit functions |
| `tap_lms/health.py` | Create | Health endpoint: DB + Redis + RQ workers + RabbitMQ checks |
| `tap_lms/middleware.py` | Create | HTTP request latency, error rate, exception hooks |
| `tap_lms/summer_program/dlq_monitor.py` | Create | CloudAMQP Management API poller; structured log per DLQ |
| `tap_lms/hooks.py` | +4 lines | Register before_request / after_request / on_exception + DLQ monitor cron |
| `tap_lms/summer_program/save_submission.py` | Already instrumented (27 emit calls) | `save_submission_called`, `save_submission_success`, `save_submission_*_error`, `feedback_fetched`, `feedback_requested`, `feedback_flow_triggered` — complete |
| `tap_lms/imgana/submission.py` | Deprecated — not instrumented | Legacy entry point; no new monitoring work required |
| `tap_lms/feedback_handler/feedback_consumer.py` | +4 emit calls | `feedback_result_received`, `feedback_processing_complete`, `feedback_processing_failed` (with failure_reason), `glific_notification_sent` |
| `tap_lms/feedback_handler/feedback_processor.py` | Replace method | Replace `is_retryable_error()` with `classify_error()` returning (bool, failure_reason) |
| `tap_lms/summer_program/pe_dispatcher.py` | Wrap entry point | `dispatcher_cycle` metric: processed / skipped / errors / duration |
| `tap_lms/summer_program/scheduler.py` | Wrap entry point | `background_job` success/error/duration |
| `tap_lms/summer_program/escalation_runner.py` | Wrap entry point | `background_job` success/error/duration |

#### tap_plg (1 file modified, 1 endpoint exposed)

| File | Action | What it adds |
|---|---|---|
| `tap_plg/app.py` | Replace log formatter | `StructuredJsonFormatter` — makes all existing `logger.*` calls emit JSON automatically |
| `tap_plg/api/api.py` | Expose existing health check | `GET /health` |
| `tap_plg/plag_checker/submissions_checker.py` | +2 emit calls | `plg_submission_received`, `plg_result_published` |
| `tap_plg/image_worker/worker.py` | +per-step timing | `detection_step_complete` / `detection_step_failed` for each of 5 steps |

#### rag_service (3 new files, 2 modified files)

| File | Action | What it adds |
|---|---|---|
| `rag_service/monitoring.py` | **Already exists** | Structured logging core — complete |
| `rag_service/middleware.py` | **Already exists** | HTTP request hooks — complete |
| `rag_service/hooks.py` | **Already registered** | Middleware hooks — complete |
| `rag_service/core/feedback_handler.py` | **Already instrumented** | `rag_submission_received`, `rag_feedback_complete`, `rag_feedback_failed` — complete |
| `rag_service/health.py` | Create | Health endpoint |
| `rag_service/core/feedback_service.py` | +2 emit calls | `llm_call_complete` / `llm_call_failed` |
| `rag_service/core/assignment_context_manager.py` | +1 emit call | `tap_lms_api_call` |

### The complete traceable log after implementation

```
[tap_lms]      submission_published          ← T+4s
[tap_plg]      plg_submission_received       ← T+5s
[tap_plg]      detection_step_complete       ← T+10s  hash check: 80ms
[tap_plg]      detection_step_complete       ← T+12s  ai_detection: 120ms
[tap_plg]      detection_step_complete       ← T+25s  pgvector_search: 14000ms
[tap_plg]      plg_result_published          ← T+26s
[rag_service]  rag_submission_received       ← T+27s
[rag_service]  llm_call_complete             ← T+60s  provider=gemini, 32000ms
[rag_service]  rag_feedback_complete         ← T+61s
[tap_lms]      feedback_result_received      ← T+63s
[tap_lms]      feedback_processing_complete  ← T+64s
[tap_lms]      glific_notification_sent      ← T+65s  success=true
```

### GCP infrastructure changes

| Change | What it does |
|---|---|
| Install Cloud Ops Agent on each VM | Ships CPU, memory, disk, network metrics automatically |
| Configure Ops Agent to tail Frappe logs | Ships tap_lms and rag_service on-disk logs to Cloud Logging |
| Configure Ops Agent for Docker logs | Ships tap_plg container logs to Cloud Logging |
| GCP Uptime Check (3 endpoints) | Polls health endpoints every 1 minute |
| 5 log-based metrics | `http_error_rate`, `http_latency_ms`, `dispatcher_errors`, `job_failures`, `dlq_depth` |
| 12 alerting policies (Terraform) | Automated alerts for all critical failure modes |

---

## 13. Glific ↔ tap_lms API Visibility — New Monitoring Scope

### The problem in detail

Every time a student or teacher interacts with the WhatsApp bot, Glific executes one or more flows. Each flow node that requires LMS data calls a whitelisted tap_lms API endpoint. Currently:

- tap_lms logs these incoming HTTP requests only as plain text — not queryable, not correlated to `student_id`
- Glific logs are already streaming to BigQuery in real time (confirmed by client)
- There is no single place to see "Glific called tap_lms function X with student_id Y at time T, and tap_lms returned status Z in N milliseconds"

### What needs to change

The `before_request` / `after_request` middleware covers all 42+ Glific → tap_lms calls automatically. Log fields must include `endpoint`, `student_id`, `glific_id`, `http_status`, `duration_ms`, and `error_detail`.

### Recommended single-view correlation approach

Since Glific logs are already in BigQuery and tap_lms logs can be routed via a Cloud Logging sink, the cleanest single-view correlation is a **BigQuery joined view** joining on `student_id` / `glific_id` with a ±5-second timestamp window.

### Log retention

| Table / partition | Retention |
|---|---|
| tap_lms request logs — errors and warnings only | 90 days |
| tap_lms request logs — INFO (200 OK, fast) | 7 days |
| tap_lms pipeline milestones | 365 days |
| Glific BigQuery logs | Per client's existing Glific retention policy |

---

## 14. Local Development Environment and Testing Strategy

### 14.1 Current local environment (8 containers)

The `docker-compose.local.yml` defines a complete local development environment:

| Container | What it replaces | Notes |
|---|---|---|
| `dev-lms` | The Frappe LMS application | tap_lms runs here |
| `postgres` | PostgreSQL for Frappe | |
| `redis-cache`, `redis-queue` | Redis for Frappe cache and RQ | |
| `rabbitmq` | CloudAMQP | Local RabbitMQ with management UI on port 15672 |
| `tap_plg_stub` | Both tap_plg_worker and tap_plg_api | No CLIP/FAISS/GCS — marks every submission "original" |
| `llm-stub` | OpenAI / TogetherAI / Vertex AI | Returns realistic deterministic feedback JSON |
| `glific-stub` | Glific GraphQL API | All 8 Glific operations stubbed |

### 14.2 The tap_plg stub's current limitation

`tap_plg_stub` currently marks every submission as `is_plagiarized: false, is_ai_generated: false`. This means integration tests never exercise the plagiarised or AI-generated branches.

**Recommended improvement:** Add env vars to control result distribution:

```yaml
tap_plg_stub:
  environment:
    STUB_PLAGIARISM_RATE: "0.1"
    STUB_AI_GENERATED_RATE: "0.05"
```

### 14.3 Integrating tap_ai into the local environment

Full tap_ai local integration requires: an `OPENAI_BASE_URL` redirect to the existing `llm-stub`, a new `pinecone-stub` FastAPI service, a `postgres-ai` container seeded from `seed_local.py`, and tap-ai-workers started inside `dev-lms`. Estimated effort: ~2.75 days. Pending client confirmation.

---

## 15. Open Questions

Items marked **Resolved** have been answered by TAP. Items marked  **Open** are still pending.

### Infrastructure

| # | Status | Question | Client Response / Notes |
|---|---|---|---|
| OQ-1 | ✅ Resolved | Does each service run on its own dedicated VM? | Yes |
| OQ-2 | ✅ Resolved | Is the same VM used for production? | No — production uses a different larger configuration |
| OQ-3 | 🔲 Open | Is there a reverse proxy (Nginx, Caddy, or similar) in front of each service? | Client is checking |
| OQ-4 | ✅ Resolved | Does tap_plg have enough RAM for the CLIP model? | Yes — dedicated higher-RAM VM |

### Queue Configuration

| # | Status | Question | Client Response / Notes |
|---|---|---|---|
| OQ-5 | 🔲 Open | tap_plg's default queue name is `plagiarism_submissions` — does this match what tap_lms publishes to? | Pending verification |
| OQ-6 | 🔲 Open | Are queue names consistent across all environments? | Pending verification |
| OQ-21 | 🔲 Open | What are the exact DLQ names in each environment? | Required before DLQ name standardisation; verify against CloudAMQP console |

### Submission Type Routing — Action Required

| # | Status | Finding | Notes |
|---|---|---|---|
| OQ-7 | ✅ Partially resolved | Confirmed that Image-only plagiarism checking is by design | Code shows all four submission types published unconditionally to `submission_queue`. Routing logic in `enqueue_submission()` needs to branch by `submission_type`. |

### pe_dispatcher Architecture

| # | Status | Question | Notes |
|---|---|---|---|
| OQ-8 | 🔲 Open | Has the team considered a trigger-based approach for pe_dispatcher? | 1-minute cron was tuned for 100K-student burst handling. Worth discussing once monitoring data is available. |

### tap_plg Production Environment — Action Required

| # | Status | Finding | Notes |
|---|---|---|---|
| OQ-9 | 🔲 Open | `FRAPPE_API_KEY`, `FRAPPE_API_SECRET`, and `FRAPPE_API_BASE_URL` missing from tap_plg env docs | Required by `assigment_ref_images.py`. Must be added to production environment or reference image comparison silently fails. |

### rag_service Consumer Process Management

| # | Status | Question | Notes |
|---|---|---|---|
| OQ-10 | 🔲 Resolved | How is the rag_service consumer kept running in production? | If no restart policy exists, a consumer crash goes undetected indefinitely. |

### Data and Compliance

| # | Status | Question | Client Response / Notes |
|---|---|---|---|
| OQ-12 | ✅ Resolved | Are there privacy constraints on logging student IDs? | Yes — Cloud Logging access restricted via GCP IAM |
| OQ-13 | 🔲 Open | What is the required log retention period per category? | Recommendation: error logs 30–90 days; pipeline milestone logs 1 year |
| OQ-18 | 🔲 Open | Are there PII fields in Glific's BigQuery webhook log table? | Client to confirm schema |
| OQ-20 | 🔲 Open | What is the exact BigQuery dataset ID for Glific's logs? | Required before the correlation view can be written |

---

## 16. DLQ Monitoring — Detailed Design

### 16.1 Current State

Dead letter queues are implemented but completely dark. The three DLQs in the system can accumulate failed messages indefinitely with no alert, no visibility in any dashboard, and no structured log record of depth over time.

The two monitoring mechanisms that currently exist are inadequate: `feedback_consumer.py` calls `frappe.logger().info()` when it declares the DLQ at startup (plain text, not shipped to Cloud Logging), and `rmq_client.py` calls `publish_to_dlq()` when a message exceeds retries (no structured log, no alert).

A message in a DLQ represents a student whose submission is permanently stuck. Without monitoring, the first indication is typically a student or teacher reporting that feedback never arrived.

### 16.2 The Three DLQs

| DLQ | Declared by | Source of messages |
|---|---|---|
| `{feedback_results_queue}_dead_letter` | `tap_lms/feedback_handler/feedback_consumer.py` | Feedback results that tap_lms could not handle after retries |
| `{plagiarism_results_queue}.dead_letter` | `rag_service/rag_service/utils/rabbitmq_consumer.py` | Plagiarism results rag_service could not process |
| `$DEAD_LETTER_QUEUE` (env var) | `tap_plg/mq/rmq_client.py` | Student submissions tap_plg could not process |

The naming convention is inconsistent across the three. All should be standardised to `{source_queue}.dead_letter` in a coordinated deploy across all three services.

> **Note on naming standardisation:** This requires a coordinated deploy. If any service is deployed ahead of the others, old and new queue names temporarily diverge and messages may land on an unconsumed queue. Plan as a single release across all three services.

### 16.3 Two Complementary Monitoring Mechanisms

| Mechanism | What it catches | Latency |
|---|---|---|
| Event-driven: `feedback_processing_failed` with `failure_reason` | Individual messages rejected by the consumer, with full classification context | Immediate |
| Periodic depth poll (5 min): CloudAMQP Management API | Depth from before monitoring existed; messages from services not yet emitting structured logs; broker-side routing failures | Up to 5 minutes |

Together these two mechanisms ensure no DLQ accumulation goes undetected for more than 5 minutes.

### 16.4 What Happens When a Message Is in a DLQ

A message in a DLQ is not automatically retried. The operator must act. The three options are:

**Replay** — move the message back to the source queue. Appropriate if the failure was transient (temporary Glific outage, DB deadlock). Use the `failure_reason` from the event-driven log to confirm this is safe. The consumer must be idempotent — `feedback_consumer.py`'s `process_message()` is largely idempotent but verify before replaying.

**Fix and replay** — if the failure was caused by a bug, deploy the fix first, then replay. Replaying into broken code causes the same failure again.

**Inspect and discard** — if the message is malformed or refers to a submission that no longer exists (`failure_reason="not_found"` or `"invalid_payload"`), inspect the message body in the CloudAMQP console and discard. The affected student will need to be identified and manually re-triggered.

The `failure_reason` field in the event-driven log (Section 17) tells the operator which path is appropriate without requiring them to inspect the message body first.

### 16.5 The CloudAMQP Management API

The poller uses the RabbitMQ Management Plugin HTTP API, enabled on all CloudAMQP plans including the free tier.

**Base URL:** Available in the CloudAMQP console under "Details". Format: `https://your-instance.cloudamqp.com`. This is an HTTPS endpoint on port 443 — different from the AMQP connection string. One new field (`management_api_url`) is required in the `RabbitMQ Settings` DocType to store this URL.

**Per-queue endpoint:**
```
GET https://{management_api_url}/api/queues/{vhost}/{queue_name}
Authorization: Basic {base64(username:password)}
```

The same `username` and `password` from `RabbitMQ Settings` are reused. The virtual host must be URL-encoded (`/` becomes `%2F`).

**Key response fields:**
```json
{
  "messages": 3,
  "consumers": 0
}
```

`consumers` being 0 on a DLQ is expected — nothing actively consumes the DLQ by design. A non-zero `messages` value is the alert signal.

The DLQ poller makes three HTTPS requests per 5-minute cycle. These are REST calls, not AMQP connections, and do not consume from the CloudAMQP connection limit.

### 16.6 Section 11.5 — Resolved

The risk noted in Section 10.5 (dead letter queue exists but is not monitored) is addressed by this implementation. Once `dlq_monitor.py` is deployed and the `dlq_depth` log-based metric and alert are configured, the risk is resolved.

---

## 17. Error Classification — Retryable vs Non-Retryable Failures

### 17.1 Current State and the Problem

When `feedback_consumer.py` fails to process a message, it calls `feedback_processor.is_retryable_error()` to decide whether to requeue the message (retryable) or route it to the dead letter queue (non-retryable).

The current implementation:

```python
non_retryable_patterns = [
    "does not exist", "not found", "invalid", "permission denied",
    "duplicate", "constraint violation", "missing submission_id",
    "missing feedback data", "validation error",
]
return not any(pattern in error_str for pattern in non_retryable_patterns)
```

This has three problems:

**Problem 1 — Misclassification risk.** The matching is substring-based against the exception message string. An exception containing "invalid" anywhere — even incidentally, like `"invalid state transition after database reconnect"` — is classified as non-retryable and goes straight to the DLQ, even if the root cause is transient. Conversely, an exception with an unusual message (e.g. a PostgreSQL `FATAL: remaining connection slots are reserved`) doesn't match any pattern and is classified as retryable — which is the right call, but it's accidental, not intentional.

**Problem 2 — No diagnostic information in the log.** The `feedback_processing_failed` structured log includes `retryable=true/false` but gives no information about *why* it was classified that way. When the alert fires, the operator must inspect the CloudAMQP message body to understand what happened — a slower path than reading it from the log.

**Problem 3 — Unknown failures are invisible.** Any exception whose message doesn't match a known pattern is silently classified as retryable. There is no way to know from the logs that novel failure modes are accumulating.

### 17.2 The Fix: `classify_error()`

`is_retryable_error()` is replaced by `classify_error()`, which returns both the retryable boolean and a `failure_reason` string. The `failure_reason` is emitted in the structured log alongside the error and `retryable` flag, making every failure immediately actionable from the log alone.

The method maintains an explicit non-retryable section (same patterns as today, now organised by named reason) and an explicit retryable section. Anything that doesn't match either becomes `failure_reason="unknown"` and defaults to retryable — the same safe default as today, but now visible.

### 17.3 What `failure_reason` Values Mean in Practice

This table is the operator's decision guide when a `feedback_processing_failed` alert fires:

| `failure_reason` | `retryable` | What it means | Right operator response |
|---|---|---|---|
| `not_found` | false | Submission doc was deleted or never created in tap_lms | Investigate data integrity; do not replay |
| `invalid_payload` | false | Message from rag_service is malformed JSON or missing required fields | Bug in rag_service serialisation; fix and redeploy before replaying |
| `validation_error` | false | Frappe validation rejected the Submission doc update | Schema mismatch or bad data; inspect message body |
| `db_constraint` | false | Duplicate write attempt on a unique field | Message likely already processed; safe to discard |
| `db_connection` | true | PostgreSQL was temporarily unreachable | Transient; will requeue automatically; confirm DB health |
| `timeout` | true | External call (Glific, ElevenLabs) timed out | Transient; will requeue; verify external service status |
| `broker_error` | true | RabbitMQ channel or connection error during ack/nack | Transient; check broker health |
| `unknown` | true | Exception message did not match any known pattern | **Investigate before deciding whether to replay** — this label signals the classification list needs updating |

### 17.4 The `unknown` Bucket as a Continuous Improvement Signal

The `unknown` bucket is not a failure of the classification system — it is a deliberate signal. A Cloud Logging query for `failure_reason="unknown"` surfaces exception types that have appeared in production but are not yet categorised. Over time, the operator reviews these, adds the appropriate patterns to the non-retryable or retryable sections of `classify_error()`, and the classification becomes more precise.

This is preferable to expanding the non-retryable pattern list aggressively upfront, which risks misclassifying legitimate transient failures as permanent.

### 17.5 Impact on the `feedback_processing_failed` Structured Log

The full structured log payload for a non-retryable failure after this change:

```json
{
  "severity": "ERROR",
  "message": "feedback_processing_failed",
  "app": "tap_lms",
  "submission_id": "SUB-2024-00123",
  "student_id": "ST-00456",
  "error": "Submission SUB-2024-00123 does not exist",
  "error_type": "ValueError",
  "retryable": false,
  "failure_reason": "not_found",
  "retry_count": 1,
  "timestamp": "2024-11-15T10:05:32Z"
}
```

For a transient retryable failure:

```json
{
  "severity": "ERROR",
  "message": "feedback_processing_failed",
  "app": "tap_lms",
  "submission_id": "SUB-2024-00456",
  "student_id": "ST-00789",
  "error": "could not connect to server: Connection refused",
  "error_type": "OperationalError",
  "retryable": true,
  "failure_reason": "db_connection",
  "retry_count": 2,
  "timestamp": "2024-11-15T10:06:14Z"
}
```

The operator can read the second log and know immediately: this is a DB connection error, it will requeue automatically, check the PostgreSQL service health. No CloudAMQP console inspection required.

---

*Document prepared for client validation. Version 1.6. All findings are based on static code analysis of the five provided codebases and client responses received through June 2026.*
