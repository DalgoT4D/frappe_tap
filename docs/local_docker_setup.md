# Local Docker setup for tap_lms

This guide creates a fresh local Frappe site named `tap_lms.localhost` using Docker, Postgres, Redis, and the local `tap_lms` app source from this repository.

The setup script assumes Docker is already installed. It creates the bench and site inside Docker volumes, mounts this repository into the dev container, installs `tap_lms`, and seeds the local integration settings that the app code expects.

## 1. Install Docker

Install Docker Desktop for your operating system:

- macOS: download Docker Desktop from `https://www.docker.com/products/docker-desktop/`, install it, then start Docker Desktop.
- Windows: install Docker Desktop with WSL 2 enabled, then start Docker Desktop.
- Linux: install Docker Engine and the Docker Compose plugin from your distribution package manager or Docker's official instructions.

Verify Docker is available:

```sh
docker --version
docker compose version
```

## 2. Add the local hostname

Add this line to your hosts file so the browser can resolve the local Frappe site:

```text
127.0.0.1 tap_lms.localhost
```

On macOS or Linux, edit `/etc/hosts`. On Windows, edit `C:\Windows\System32\drivers\etc\hosts` as Administrator.

## 3. Create `env.local`

Copy the example file:

```sh
cp .env.example env.local
```

Fill in at least these values:

```dotenv
SITE_NAME=tap_lms.localhost
FRAPPE_BRANCH=version-16
ADMIN_PASSWORD=admin
POSTGRES_PASSWORD=postgres
BUSINESS_THEME_REPO=https://github.com/Midocean-Technologies/business_theme_v14.git
```

The RabbitMQ queue for this app is external. Log in to CloudAMQP at `https://customer.cloudamqp.com/login`, open the instance used for local testing, and copy the AMQP connection details into `env.local`.

CloudAMQP usually shows a URL like:

```text
amqps://USERNAME:PASSWORD@HOST/VIRTUAL_HOST
```

Split it into:

```dotenv
RABBITMQ_HOST=HOST
RABBITMQ_PORT=5671
RABBITMQ_VIRTUAL_HOST=VIRTUAL_HOST
RABBITMQ_USERNAME=USERNAME
RABBITMQ_PASSWORD=PASSWORD
RABBITMQ_SUBMISSION_QUEUE=your-submission-queue
RABBITMQ_PLAGIARISM_RESULTS_QUEUE=your-plagiarism-results-queue
RABBITMQ_FEEDBACK_RESULTS_QUEUE=your-feedback-results-queue
```

Keep `env.local` private. It contains credentials and should not be committed.

## 4. Start the fresh local setup

Run:

```sh
chmod +x scripts/start_local_docker.sh
./scripts/start_local_docker.sh
```

The script will:

- build a local dev image from `docker/local/Dockerfile`
- start Postgres and two Redis containers under the `tap_lms_local` Compose project
- create `/home/frappe/frappe-bench` in a Docker volume if it does not exist
- create the `tap_lms.localhost` site with Postgres
- symlink `/home/frappe/frappe-bench/apps/tap_lms` to the mounted local repository at `/workspace/frappe_tap`
- install the local `tap_lms` app from that mounted path
- install `business_theme_v14` from the configured theme repository
- run migrations
- seed `RabbitMQ Settings`, `GCS Settings`, `ElevenLabs Settings`, and `VoiceAgentSettings`

After setup, start Frappe:

```sh
docker compose --env-file .env -f docker/local/docker-compose.yml exec dev bash -lc "cd /home/frappe/frappe-bench && bench start"
```

Open:

```text
http://tap_lms.localhost:8000
```

Login with:

```text
User: Administrator
Password: the ADMIN_PASSWORD value from `env.local`
```

## 5. Required local DocTypes discovered in code

The active folders reviewed were:

- `tap_lms/audio`
- `tap_lms/config`
- `tap_lms/feedback_handler`
- `tap_lms/imgana`
- `tap_lms/summer_program`
- `tap_lms/tap_lms`

The local setup needs these integration settings available:

- `RabbitMQ Settings`: used by `tap_lms/imgana/submission.py` and `tap_lms/feedback_handler/feedback_consumer.py`.
- `GCS Settings`: used by image submission upload flows in `tap_lms/imgana/submission.py`. It can stay disabled for a basic local site.
- `ElevenLabs Settings`: used by `tap_lms/audio/audio_helpers.py`. It can stay disabled for a basic local site.
- `VoiceAgentSettings`: used by `tap_lms/summer_program/vocallabs.py`. It can stay disabled for a basic local site.

The script seeds those single DocTypes from `.env`. RabbitMQ should be filled when you want image submission publishing or feedback consumption to work. The other integrations can remain disabled unless you are testing those flows.

## 6. Create the API user after install

Some image submission endpoints authenticate through the custom `API Key` DocType, not Frappe's built-in API key fields. The code checks for an enabled `API Key` row where `key` matches the submitted `api_key`.

In the Frappe UI:

1. Log in as `Administrator`.
2. Open **Users** and create a user such as `local.api@tap-lms.local`.
3. Assign the roles needed for the flow you are testing. For broad local development, use `System Manager`.
4. Open **API Key**.
5. Create a new row:
   - `user`: the user you created
   - `key`: a local-only secret, for example `local-dev-api-key`
   - `enabled`: checked
6. Save the document.

Use that `key` value as the `api_key` parameter when calling endpoints such as `tap_lms.imgana.submission.submit_artwork`.

## 7. Useful commands

Start containers:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml up -d
```

Start Frappe:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash -lc "cd /home/frappe/frappe-bench && bench start"
```

Run migrations:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash -lc "cd /home/frappe/frappe-bench && bench --site tap_lms.localhost migrate"
```

Open a bench shell:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash
```

Stop containers:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml down
```

Reset the local bench and database volumes:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml down -v
```

Only use the reset command when you are comfortable deleting the local Docker database and bench volumes.

## 8. Manual testing cheat sheet

These are the commands needed for a typical round of manual Summer Program testing: creating an assignment, enrolling a student in a program, resetting a prior submission, and making sure the student's batch is actually active.

Open a bench console first:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash -lc "cd /home/frappe/frappe-bench && bench --site tap_lms.localhost console"
```

Everything below is pasted into that console.

### 8.1 Create an assignment (if it doesn't already exist)

`Assignment.autoname` is `format:{assignment_name}-{difficulty_tier}`, so the doc name is predictable and you can check existence directly by name:

```python
assignment_name = "MockAssign"
difficulty_tier = "Basic"          # Remedial | Basic | Intermediate | Advanced
assign_id = f"{assignment_name}-{difficulty_tier}"

if not frappe.db.exists("Assignment", assign_id):
    a = frappe.new_doc("Assignment")
    a.assignment_name = assignment_name
    a.difficulty_tier = difficulty_tier
    a.assignment_type = "Written"    # Written | Practical | Performance | Collaborative
    a.max_score = "10"
    a.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"created {a.name}")
else:
    print(f"already exists: {assign_id}")
```

### 8.2 Enroll a student in a program

Use `create_test_student_with_pe` from `tap_lms.summer_program.dev_tools` rather than creating `Student`/`ProgramEnrollment` docs by hand. It's idempotent (reuses an existing `Student` on `phone`+`name1`, and an existing active/paused `ProgramEnrollment` on `student`+`batch`), and by default skips real Glific HTTP calls:

```python
from tap_lms.summer_program.dev_tools import create_test_student_with_pe

result = create_test_student_with_pe(
    name="Test Student",
    phone="9876543210",
    batch="palv2-test-BT52231",   # Batch doc name -- must already exist
    archetype="submitter",         # must be in ALL_ARCHETYPES
    experiment_arm="default",      # must be in ALL_ARMS
)
print(result)   # {"student_id": ..., "pe_name": ..., "created_student": bool, "created_pe": bool, ...}
```

### 8.3 Reset a prior submission

Use `reset_pe_to_state_0`, not a hand-written `frappe.db.set_value` call -- it resets every state-machine field, counter, grace window, and gamification field the real reset needs (a partial manual reset has caused real drift bugs in the past). Try `dry_run=True` first to see the diff before writing:

```python
from tap_lms.summer_program.dev_tools import reset_pe_to_state_0

reset_pe_to_state_0("ST00062543", dry_run=True)
reset_pe_to_state_0("ST00062543", push_to_glific=False)
```

The reset deliberately **preserves** `Submission` rows (so feedback quality can be compared across reset cycles), so delete those separately for a true clean slate:

```python
frappe.db.delete("Submission", {"student_id": "ST00062543", "assign_id": "MockAssign-Basic"})
frappe.db.commit()
```

### 8.4 Check the student's batch is active

A `BatchProgramRun` (BPR) that isn't `status = "active"` won't deliver content -- if a student stops progressing for no obvious reason, check this before anything else:

```python
status = frappe.db.get_value("BatchProgramRun", "hsugtupp28", "status")
print(status)
```

If it isn't `"active"`, check `validation_status`, then activate:

```python
validation_status = frappe.db.get_value("BatchProgramRun", "hsugtupp28", "validation_status")
print(validation_status)   # must be "passed" for the next call to work

from tap_lms.summer_program.batch_activation import activate_bpr
result = activate_bpr("hsugtupp28")
print(result)
```

If `validation_status` isn't `"passed"`, don't call `validate_bpr()` blindly -- it requires `status == "collections_ready"`, which a previously-active BPR won't have, so it will just fail with a confusing status error. Inspect the BPR's `validation_report` field manually first.

Activation doesn't fire content delivery immediately -- that only happens via the Tuesday 09:00 IST cron, or by triggering it manually for just this batch (see below). It also enqueues a background job to populate the `main` Glific collection, so check that finished before assuming a student will actually receive anything:

```python
frappe.db.sql("""
    SELECT collection_label, glific_group_id, member_count
      FROM "tabPGCollection"
     WHERE parent = %s AND kind = 'main'
""", ("hsugtupp28",), as_dict=True)
```

### 8.5 Make sure the student is actually in the batch's Glific group

`create_test_student_with_pe` defaults to `skip_glific_sync=True`, so a freshly created test student is **not** added to any Glific group, including the batch's `main` collection. Check and fix if needed:

```python
student_id = "ST00062543"
glific_id = frappe.db.get_value("Student", student_id, "glific_id")
print(glific_id)

from tap_lms.glific_integration import add_contact_to_group
add_contact_to_group(contact_id=glific_id, group_id="20529")   # main collection's glific_group_id
```

### 8.6 Trigger content delivery outside the Tuesday cron (optional)

Fire delivery for just this batch, rather than `weekly_content_delivery_trigger()`, which loops over **every** active BPR on the site and would affect other people's test batches too:

```python
from tap_lms.summer_program.glific_extensions import start_group_flow

flow_id = frappe.db.get_value("BatchProgramRun", "hsugtupp28", "content_delivery_flow")
start_group_flow(flow_id=str(flow_id), group_id="20529")
```

### 8.7 All of the above in one script

`tap_lms/summer_program/test_setup_script.py` wraps 8.1-8.5 into a single `run_test_setup(...)` call. Run it via `bench execute` without opening a console at all:

```sh
docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash -lc "cd /home/frappe/frappe-bench && bench --site tap_lms.localhost execute tap_lms.summer_program.test_setup_script.run_test_setup --kwargs '{\"assignment_name\": \"MockAssign\", \"difficulty_tier\": \"Basic\", \"student_name\": \"Test Student\", \"student_phone\": \"9876543210\", \"batch\": \"palv2-test-BT52231\", \"bpr_name\": \"hsugtupp28\", \"reset_existing\": true}'"
```
