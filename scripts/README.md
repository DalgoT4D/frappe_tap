# scripts/

Utility scripts for local development and testing. All scripts run inside the
Frappe bench environment (`frappe.connect()` is called internally).

```bash
cd /path/to/frappe-bench
python apps/frappe_tap/scripts/<script>.py
```

---

## Scripts

### `seed_local.py`

Creates the minimal DB fixtures needed to exercise both submission flows locally.
Safe to re-run — all inserts are idempotent (skips if already exists).

Creates:
- **Batch** — `LOCAL_DEV_001`, Summer program, 8 weeks
- **Assignment** — `MockAssign-Basic`
- **Student** — phone `9999900001`, glific_id `LOCAL_GLIFIC_001`
- **ProgramEnrollment** — one per submittable PE state (6 total, covering every
  state machine transition that `save_submission` can trigger)
- **API Key** — `local-dev-api-key-001` (for the `submit_artwork` flow)

Prints all created IDs at the end for use in `test_submissions.py`.

```bash
python apps/frappe_tap/scripts/seed_local.py
```

---

### `test_submissions.py`

Exercises all meaningful input combinations for both submission flows against
the seeded local DB. Run after `seed_local.py`.

Each test resets the PE back to its original state after running, so the script
is safe to re-run.

**Flow 1 — `save_submission` (Summer Program)**
- `[1a]` Every PE state transition: T7, T3, T9, T17, T22 (duplicate)
- `[1b]` All submission types: image URL, plain text, emoji
- `[1c]` All student identifier variants: student_id, phone, glific_id
- `[1d]` Error cases: unknown student, empty submission, no active PE

**Flow 2 — `submit_artwork` (imgana, legacy)**
- `[2a]` Happy path: valid API key + known student
- `[2b]` Error cases: bad API key, unknown student, unreachable image URL

GCS, RabbitMQ, and Glific calls will fail — that is expected and does not
block the Frappe-side assertions.

```bash
python apps/frappe_tap/scripts/test_submissions.py
```

---

### `mock_feedback_producer.py`

Publishes randomised feedback payloads to the local RabbitMQ queue, standing
in for the external AI/ML service that would normally produce them.

Pulls real `Pending` Submission IDs from the DB (so the consumer won't reject
them as "not found"), then randomly picks one of 8 plagiarism/AI/original
scenarios and publishes a complete, valid feedback payload.

```bash
# default: 5 submissions
python apps/frappe_tap/scripts/mock_feedback_producer.py

# specify how many
python apps/frappe_tap/scripts/mock_feedback_producer.py --count 10
```

Typical local dev loop:
1. `seed_local.py` — once
2. `test_submissions.py` — triggers `save_submission`, which creates `Pending`
   Submission docs and publishes to the outbound queue
3. `mock_feedback_producer.py` — publishes fake feedback to the inbound queue
4. `console_consumer.py` — consumes and processes the feedback

---

## System flows

This is not a traditional LMS. It is a **WhatsApp-first learning platform** for
school students in India, delivered entirely through Glific (WhatsApp chatbot).
The Frappe backend is the operational core; students and teachers never log into
it directly.

### Onboarding

**Teacher onboarding** (`api.py`)
- `create_teacher` / `create_teacher_web` — teacher registers via a web form or
  WhatsApp keyword; OTP verified via Gupshup/WhatsApp; Glific contact created
  and added to the batch teacher group
- `send_otp` / `verify_otp` — OTP flow with context (new teacher vs. existing
  teacher joining a new batch)

**Student onboarding** (`api.py`)
- `create_student` — called by Glific when a student registers via batch keyword;
  resolves school + batch from keyword, determines course level via
  `GradeCourseLevelMapping`, creates Student + Enrollment docs

### Content delivery

**Video content** (`api.py`)
- `get_youtube_url` — Glific calls this to get the week's video URL for a
  student; resolves CourseLevel → LearningUnit → VideoClass → VideoTranslation
- `get_student_video_content` — same but keyed by student + batch + language

**Quiz content** (`api.py`)
- `get_student_quiz_content` — returns the week's quiz questions (with
  translations) for a student
- `get_quiz_question_details` — returns a single question with options and
  correct answer, used by Glific to render MCQ flows

### Submission & feedback (the two flows in this scripts folder)

**Flow 1 — Summer Program** (`summer_program/save_submission.py`)
- Entry point: `save_submission` API, called by Glific when a student sends
  their work
- Resolves student → active ProgramEnrollment → applies state machine
  transition (T3/T7/T9/T17/T22) → creates Submission doc → background job
  uploads to GCS → publishes to RabbitMQ outbound queue
- External AI/ML service consumes from outbound queue, grades the submission,
  publishes feedback to inbound queue
- `FeedbackConsumer` (`feedback_handler/feedback_consumer.py`) consumes
  feedback, updates Submission, sends Glific notification, advances PE state
  to `feedback_ready` (T12)

**Flow 2 — imgana (legacy)** (`imgana/submission.py`)
- Entry point: `submit_artwork` API, called with an API key
- No state machine; directly creates Submission → uploads to GCS → publishes
  to RabbitMQ
- Same `FeedbackConsumer` processes the response

### State machine & scheduler (`summer_program/`)

`ProgramEnrollment` (PE) is a Frappe DocType — a single DB row per student per
batch that tracks their entire Summer Program journey: current state, week,
points, escalation step, grace window, etc. It is not a module; it is the
central record the state machine reads and writes.

The state machine defines ~24 named transitions (T0–T25, with some gaps):

| Transition | From state | To state |
|---|---|---|
| T0 enrollment | *(new)* | `normal_content_delivery` |
| T1 content_no_response | `normal_content_delivery` | `normal_content_delivery` (schedules escalation) |
| T2 start_escalation | `normal_content_delivery` | `normal_escalation` |
| T3 escalation_submission | `normal_escalation` | `submitted_awaiting_feedback` |
| T4 next_escalation_step | `normal_escalation` | `normal_escalation` (next step) |
| T5 escalation_to_grace | `normal_escalation` | `grace_waiting` |
| T6 escalation_to_remedial | `normal_escalation` | `remedial_content_delivery` |
| T6b failed_feedback_to_remedial | `submitted_awaiting_feedback` | `remedial_content_delivery` |
| T7 core_submission | `normal_content_delivery` | `submitted_awaiting_feedback` |
| T8 start_remedial_escalation | `remedial_content_delivery` | `remedial_escalation` |
| T9 remedial_submission | `remedial_content_delivery` or `remedial_escalation` | `submitted_awaiting_feedback` |
| T10 next_remedial_escalation | `remedial_escalation` | `remedial_escalation` (next step) |
| T11 remedial_to_grace | `remedial_escalation` | `grace_waiting` |
| T12 feedback_ready | `submitted_awaiting_feedback` | `feedback_ready` |
| T13 feedback_delivered | `feedback_ready` | `week_completed` |
| T14/T19 week_advance | `week_completed` | `normal_content_delivery` (next week) |
| T15 binge_pause | `week_completed` | `paused_binge` |
| T16 program_completed | `week_completed` | `program_completed` |
| T17 grace_submission | `grace_waiting` | `submitted_awaiting_feedback` |
| T17 grace_expired | `grace_waiting` | `program_dropped` |
| T21 binge_resume | `paused_binge` | `normal_content_delivery` |
| T22 duplicate_submission | `submitted_awaiting_feedback` | *(no change, log only)* |
| T23 auto_drop | any | `program_dropped` |
| T24 admin_drop | any | `program_dropped` |
| T25 delivery_failure | any | *(no change, increments failure counter)* |

Scheduler components:

- `pe_dispatcher.py` — runs every minute via cron; processes overdue
  `next_action_at` on each PE and routes by `next_action_type`
  (content delivery, escalation, week advancement, grace check, etc.)
- `escalation_runner.py` — 6-hour sweep that sends escalation messages to
  students who haven't submitted
- `batch_admin.py` — weekly Monday sweep that advances `Batch.current_calendar_week`
- `scheduler.py` — daily housekeeping (grace expiry, binge-pause checks, etc.)
- `activity_points.py` — hook on `StudentContentLog`; awards activity points
  and arms the grace clock on first VideoClass completion each week
- `quiz_points.py` — hook on `StudentQuizAttempt`; awards quiz points

### Glific integration (`glific_integration.py`, `glific_webhook.py`)

All student/teacher communication goes through Glific (WhatsApp). The backend:
- Creates and updates Glific contacts with ~28 contact fields (state, points,
  streak, language, etc.) that Glific flows read via `@contact.<field>`
- Triggers Glific flows (content delivery, escalation, feedback notification)
  via `start_contact_flow`
- Receives flow callbacks via `flow_callback.py` (e.g. feedback delivered →
  T13 transition)

### Journey (`journey/`)

A separate, parallel content-delivery system for self-paced learning (distinct
from the WhatsApp-push Summer Program model). Students progress through
LearningUnits week by week, with a built-in remedial path on quiz failure:

- `student_progression.py` — 8 API endpoints: `get_next_content`,
  `get_content_details`, `complete_content`, `start_quiz`, `submit_answer`,
  `get_quiz_status`, `get_student_progress_overview`, `get_student_history`
- `api.py` — `track_interaction` webhook (called by Glific on flow events);
  `update_student_stage`; stage transition engine using `StageFlow` config
- `student_api.py` — student profile, search, sibling detection, field updates
- `background_jobs.py` — async `StudentContentLog` writes and statistics updates
