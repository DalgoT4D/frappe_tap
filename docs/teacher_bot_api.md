# Teacher Bot API — teacher submission flow

Backend for the teacher submission module. Glific runs the conversation;
Frappe is the system of record.

**Base URL (local):** `http://tap_lms.localhost:8000`
**Method:** all `POST`, `Content-Type: application/json`
**Auth:** none in V0 — the sender is identified by phone number (PRD section 5)

Three endpoints, in conversation order:

| # | Endpoint | Purpose |
|---|---|---|
| 1 | `start_submission` | save phone + timestamp (+ optional images), return a submission id |
| 2 | `save_course_grade` | attach course and grade to that id |
| 3 | `save_submission_images` | attach image/file URLs to that id |

Full paths:

```
/api/method/tap_lms.teacher_bot.api.start_submission
/api/method/tap_lms.teacher_bot.api.save_course_grade
/api/method/tap_lms.teacher_bot.images.save_submission_images
```

---

## 1. `start_submission`

Opens a submission and hands back its id. Everything later refers to that id.

### Request

| Field | Required | Notes |
|---|---|---|
| `phone` | yes | 10 digits, or 12 starting with `91` |
| `submitted_at` | no | when she messaged; server time if omitted |

### Response

```json
{
  "status": "success",
  "submission_id": "TSUB-00001",
  "known": true,
  "phone": "919068076307",
  "teacher_name": "Himani",
  "school_id": "Test School Moga-SC00001",
  "school_name": "Test School Moga",
  "submitted_at": "2026-08-20 14:30:00"
}
```

Glific should store `submission_id` in a **contact variable** — result variables
don't survive past one flow.

### Cases

| # | Case | Body | Expect |
|---|---|---|---|
| 1 | happy path | `{"phone":"919068076307"}` | 200, `known: true`, an id |
| 2 | Glific timestamp | `{"phone":"919068076307","submitted_at":"2026-08-19 21:45:00"}` | 200, that exact time stored |
| 3 | 10-digit phone | `{"phone":"9068076307"}` | 200, `known: true` — both forms match |
| 4 | unknown number | `{"phone":"919000009999"}` | **200**, `known: false`, row still created |
| 5 | bad phone | `{"phone":"123"}` | 400 |
| 6 | bad timestamp | `{"phone":"919068076307","submitted_at":"not-a-date"}` | 400 |

Case 4 is the one to understand: an unregistered number is **not** an error.
The row is created and flagged `is_unknown_number` for PI to link later.
PRD section 5 — never go silent, never refuse.

Case 6 fails loudly on purpose. Silently substituting server time would hide a
broken Glific flow for weeks.

### curl

```sh
# 1. happy path
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.start_submission" \
  -H "Content-Type: application/json" \
  -d '{"phone":"919068076307"}'

# 2. with Glific timestamp
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.start_submission" \
  -H "Content-Type: application/json" \
  -d '{"phone":"919068076307","submitted_at":"2026-08-19 21:45:00"}'

# 3. ten-digit phone
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.start_submission" \
  -H "Content-Type: application/json" \
  -d '{"phone":"9068076307"}'

# 4. unknown number
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.start_submission" \
  -H "Content-Type: application/json" \
  -d '{"phone":"919000009999"}'

# 5. bad phone -> 400
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.start_submission" \
  -H "Content-Type: application/json" \
  -d '{"phone":"123"}'

# 6. bad timestamp -> 400
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.start_submission" \
  -H "Content-Type: application/json" \
  -d '{"phone":"919068076307","submitted_at":"not-a-date"}'
```

---

## 2. `save_course_grade`

Fills course and grade into an existing submission.

### Request

| Field | Required | Notes |
|---|---|---|
| `submission_id` | yes | from call 1 |
| `course` | yes | a `Course Verticals` name |
| `grade` | yes | `"1"` … `"12"` |

Valid courses on this site: `Arts`, `Dance`, `Coding`, `Science Lab`, `Financial Literacy`
(check `/app/course-verticals` for the live list).

⚠️ Production's `Course` table says **"Science"**; `Course Verticals` says
**"Science Lab"**. `student_registration.py` already aliases this
(`STUDENT_COURSE_NAME_ALIASES`). This endpoint does **not** yet — decide whether
it should before handing the API to the bot builder.

### Response

```json
{
  "status": "success",
  "submission_id": "TSUB-00001",
  "course": "Arts",
  "grade": "7",
  "submission_status": "Details Added"
}
```

### Cases

| # | Case | Body | Expect |
|---|---|---|---|
| 7 | happy path | id + `Arts` + `7` | 200, `Details Added` |
| 8 | same call twice | identical to 7 | 200 again — Glific retries webhooks |
| 9 | no `submission_id` | course + grade only | 400 |
| 10 | unknown id | `TSUB-DOES-NOT-EXIST` | **404**, not 500 |
| 11 | invalid course | `"Underwater Basket Weaving"` | 400 **with `allowed_values`** |
| 12 | invalid grade | `"13"` | 400 |

Case 11 returns the valid list in the error, so the bot builder never has to ask
what the options are.

After a rejected course the record stays `Started` with no course set — the
rollback works, so there are no half-updated rows.

### curl

Replace `TSUB-00001` with the id from call 1.

```sh
# 7. happy path
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.save_course_grade" \
  -H "Content-Type: application/json" \
  -d '{"submission_id":"TSUB-00001","course":"Arts","grade":"7"}'

# 8. run 7 again -> still 200
# 9. missing submission_id -> 400
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.save_course_grade" \
  -H "Content-Type: application/json" \
  -d '{"course":"Arts","grade":"7"}'

# 10. unknown submission -> 404
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.save_course_grade" \
  -H "Content-Type: application/json" \
  -d '{"submission_id":"TSUB-DOES-NOT-EXIST","course":"Arts","grade":"7"}'

# 11. invalid course -> 400 + allowed_values
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.save_course_grade" \
  -H "Content-Type: application/json" \
  -d '{"submission_id":"TSUB-00001","course":"Underwater Basket Weaving","grade":"7"}'

# 12. invalid grade -> 400
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.save_course_grade" \
  -H "Content-Type: application/json" \
  -d '{"submission_id":"TSUB-00001","course":"Arts","grade":"13"}'
```

---

## 3. `save_submission_images`

Attaches image or file URLs to an existing submission. Up to **20 per submission**
(`MAX_IMAGES_PER_SUBMISSION` in `teacher_bot/images.py`).

All image data lives on the `Teacher Submission` record itself — there is no child table:

| Field | Holds |
|---|---|
| `image_urls` | one URL per line |
| `image_count` | derived automatically on every save |
| `source_row_ids` | source row identifiers already attached, one per line |
| `source_time` | timestamp of the source row (e.g. the BigQuery form-submit time) |
| `media_match_status` | Pending / Matched / Not Found / Ambiguous |

### Request

```json
{
  "submission_id": "TSUB-00001",
  "images": [
    "https://storage.googleapis.com/bucket/photo1.jpg",
    "https://storage.googleapis.com/bucket/photo2.jpg"
  ]
}
```

Or with per-file detail, which the BigQuery matching job will use:

```json
{
  "submission_id": "TSUB-00001",
  "images": [
    {"url": "https://.../photo1.jpg", "type": "image",
     "source_time": "2026-08-20 10:29:01", "source_row_id": "bq-row-8842"}
  ]
}
```

### Response

```json
{"status":"success","submission_id":"TSUB-00001","added":2,
 "skipped_duplicates":0,"image_count":2,"max_images":20}
```

### Behaviour

| Situation | What happens |
|---|---|
| Same URL sent again | Skipped, counted in `skipped_duplicates` |
| Same `source_row_id`, different URL | Skipped — protects re-runs of the matching job |
| More than 20 files | Extras rejected, reported as `rejected_over_cap` |
| Unknown file extension | Stored anyway, type recorded as `unknown` |
| `images` as plain URL strings | Accepted; type guessed from the extension |
| Unknown `submission_id` | 404 |
| Empty `images` | 400 |

### curl

```sh
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.images.save_submission_images" \
  -H "Content-Type: application/json" \
  -d '{"submission_id":"TSUB-00001","images":[
        "https://example.com/photo1.jpg",
        "https://example.com/photo2.jpg",
        "https://example.com/voice.ogg"]}'
```

Run it twice — the second call must report `"added": 0` with `skipped_duplicates: 3`.
That is the idempotency guarantee the BigQuery job depends on.

---

## 4. `save_images_by_phone` — when you don't have the submission_id

For the BigQuery matching job, or any pipeline that only knows the WhatsApp form's
**phone and timestamp**. It finds the submission itself.

```
/api/method/tap_lms.teacher_bot.images.save_images_by_phone
```

### The problem it solves

The form is submitted first; the webhook fires seconds later. So the two systems
never hold the same timestamp:

```
10:29:01   form submitted   ->  source_time   (BigQuery)
10:29:30   webhook fired     ->  submitted_at  (Frappe)
```

### The rule

> For this phone, take the **first** submission whose `submitted_at` is at or after
> `source_time`, within `MATCH_WINDOW_MINUTES`.

Ordering, not proximity — which is why a 3-second delay and a 40-second delay both
resolve correctly. It only ever looks **forward** from the source time, so it can
never steal the previous submission's files.

### Request

```json
{
  "phone": "919068076307",
  "source_time": "2026-08-20 10:29:01",
  "images": [
    "https://storage.googleapis.com/bucket/photo1.jpg",
    "https://storage.googleapis.com/bucket/photo2.jpg"
  ]
}
```

Optional: `window_minutes` to override the default for one call.
Optional per file: `source_row_id`, so a re-run cannot attach the same source row twice.

### Response

```json
{"status":"success","match_status":"matched","submission_id":"TSUB-00001",
 "submitted_at":"2026-08-20 10:29:30","attached":2,"skipped_duplicates":0,
 "image_count":2,"window_minutes":5}
```

Always HTTP 200 when the input is valid — read `match_status`:

| `match_status` | Meaning | What is attached |
|---|---|---|
| `matched` | exactly one submission fits | the files |
| `not_found` | no submission in the window — the webhook may not have arrived yet | nothing; safe to retry next run |
| `ambiguous` | two submissions within 90 seconds of each other | nothing; both flagged `Ambiguous` for review |

### Verified behaviour

| Case | Result |
|---|---|
| form 10:29:01, submission 10:29:30 | matched |
| 40-second delay | matched |
| two submissions 78 minutes apart | each matched to its own form |
| two submissions 20 seconds apart | ambiguous — nothing attached |
| submission is 9 min *before* the form | not_found (never looks backward) |
| submission 20 minutes later | not_found (outside window) |
| no submission at all | not_found |

### Tuning

Two constants in `teacher_bot/images.py`:

```python
MATCH_WINDOW_MINUTES = 5      # how far forward to look
AMBIGUITY_GAP_SECONDS = 90    # closer than this and we refuse to guess
```

Set the window from **measured** drift — run an analysis query on real data first.
Guessing 30 minutes makes ordinary double-submits ambiguous for no reason.

### Timezones

Frappe stores local time; BigQuery is almost always UTC. Convert before calling.
If everything comes back `not_found`, check this before suspecting anything else.

### curl

```sh
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.images.save_images_by_phone" \
  -H "Content-Type: application/json" \
  -d '{"phone":"919068076307","source_time":"2026-08-20 10:29:01",
       "images":["https://example.com/photo1.jpg","https://example.com/photo2.jpg"]}'
```

---

### Images in the first call

`start_submission` also accepts an `images` field, so a Glific flow that already
holds the URLs needs only one webhook:

```sh
curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.start_submission" \
  -H "Content-Type: application/json" \
  -d '{"phone":"919068076307","submitted_at":"2026-08-20 10:29:01",
       "images":["https://example.com/photo1.jpg","https://example.com/photo2.jpg"]}'
```

---

## Chain both calls in one command

Runs call 1, reads the id out of the response, and feeds it to call 2:

```sh
SUB=$(curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.start_submission" \
  -H "Content-Type: application/json" -d '{"phone":"919068076307"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['submission_id'])")

echo "created $SUB"

curl -s -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.teacher_bot.api.save_course_grade" \
  -H "Content-Type: application/json" \
  -d "{\"submission_id\":\"$SUB\",\"course\":\"Arts\",\"grade\":\"7\"}"
```

---

## Status codes

| Code | Meaning |
|---|---|
| 200 | done — including `known: false` for an unregistered number |
| 400 | bad input: phone, timestamp, course or grade |
| 404 | `submission_id` doesn't exist |
| 500 | server fault — check Error Log and API Failures |

## The record

`Teacher Submission` (`/app/teacher-submission`)

| Field | Set by |
|---|---|
| `phone_number` | call 1 |
| `teacher`, `school_id` | call 1, resolved from the phone |
| `is_unknown_number` | call 1, when the phone matches no teacher |
| `submitted_at` | call 1 |
| `status` | `Started` → `Details Added` |
| `course`, `grade` | call 2 |

ID format `TSUB-00001`, from a Frappe series counter.

## If something fails

| Where | What it shows |
|---|---|
| the response | `exc_type` and the message |
| `/app/api-failures` | failed calls **with the payload that broke them** |
| `/app/error-log` | full tracebacks |
| the `bench start` tab | the same traceback, live |

## Tests

```sh
bench --site tap_lms.localhost run-tests --module tap_lms.teacher_bot.tests.test_teacher_bot_api
```

15 tests, covering every case above. Requires `bench --site tap_lms.localhost
set-config allow_tests true` once.

## Notes for the bot builder

- Store `submission_id` in a **contact variable**, not a result variable
- `known: false` is a branch, not an error — send her down the unmatched path but keep going
- Retries are safe: calling `save_course_grade` twice overwrites rather than failing
- `IGNORE KEYWORDS` on for this flow, so a keyword mid-submission doesn't lose her progress
