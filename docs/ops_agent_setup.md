# GCP Ops Agent Setup — TAP LMS Server

This document covers installing and configuring the GCP Ops Agent on the
TAP LMS server to ship structured application logs to Cloud Logging and
hardware metrics (CPU, memory, disk, network) to Cloud Monitoring.

---

## Overview

The Ops Agent is Google Cloud's unified telemetry agent for Compute Engine.
It uses Fluent Bit for log collection and OpenTelemetry for metrics — both
managed via a single YAML config file.

**What this setup covers:**

| Source | What gets shipped |
|---|---|
| `gcp_structured.log` | Structured JSON business events (submissions, feedback, errors) |
| `rag_gcp_structured.log` | rag_service structured events (if co-located) |
| `frappe.log`, `worker.log` | Frappe application and worker logs |
| nginx access/error logs | HTTP access and nginx errors |
| `feedback-consumer.log` | Feedback consumer stdout/stderr |
| `supervisor/supervisord.log` | Supervisor process management events |
| Host metrics | CPU, memory, disk, network — automatic, no config needed |

---

## Prerequisites

- GCP Compute Engine VM (Ubuntu 22.04)
- The VM's service account must have these IAM roles:
  - `roles/logging.logWriter` — to write logs to Cloud Logging
  - `roles/monitoring.metricWriter` — to write metrics to Cloud Monitoring

Verify in GCP Console → IAM & Admin → Service Accounts → find the VM's
service account → check roles. If missing, add them.

---

## 1. Install the Ops Agent

```bash
curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
sudo bash add-google-cloud-ops-agent-repo.sh --also-install
```

Verify it's running:

```bash
sudo systemctl status google-cloud-ops-agent
```

All three sub-services should show `active (running)`:
- `google-cloud-ops-agent.service`
- `google-cloud-ops-agent-fluent-bit.service`
- `google-cloud-ops-agent-otel-collector.service`

---

## 2. Deploy the Config File

Copy the config from the repo to the Ops Agent config location:

```bash
sudo cp /home/lms-dev/frappe-bench/apps/tap_lms/deployment_configs/ops_agent_config.yaml \
    /etc/google-cloud-ops-agent/config.yaml
```

Validate the config syntax:

```bash
sudo google-cloud-ops-agent --config /etc/google-cloud-ops-agent/config.yaml --dryrun
```

Restart the agent to apply:

```bash
sudo systemctl restart google-cloud-ops-agent
sudo systemctl status google-cloud-ops-agent
```

---

## 3. Key Config Decisions

### severity field promotion

`monitoring.py` emits `"severity": "ERROR"` as a plain JSON field. The Ops
Agent only promotes a field to `LogEntry.severity` if it is named
`logging.googleapis.com/severity`. Without promotion:

- Log Explorer shows all entries as grey (no severity colour)
- Alert policies can't filter on `severity=ERROR`
- The severity histogram in the sidebar is empty

The `promote_severity` processor in the config moves `jsonPayload.severity`
to `logging.googleapis.com/severity` automatically. No changes to
`monitoring.py` are needed.

### timestamp parsing

Our logs emit `"timestamp": "2026-07-28T11:25:22.536652"`. The
`parse_tap_lms_json` processor extracts this via `time_key: timestamp` so it
becomes `LogEntry.timestamp` in Cloud Logging rather than the agent's
ingestion time.

### INFO http_request exclusion

Every API call emits an `http_request` INFO log (used for P95 latency and
error rate metrics). These are high volume and don't need long-term storage.
The `exclude_http_info` processor drops them before ingestion.

To keep all HTTP logs, comment out `exclude_http_info` from the pipeline:

```yaml
tap_lms_pipeline:
  receivers: [tap_lms_structured]
  processors: [parse_tap_lms_json, promote_severity]  # exclude_http_info removed
```

---

## 4. Hardware Metrics

Hardware metrics are collected automatically by the built-in `hostmetrics`
receiver — no additional configuration needed. Available immediately after
install.

View in GCP Console → **Monitoring → Metrics Explorer**:

| Metric | Filter |
|---|---|
| CPU utilization | `agent.googleapis.com/cpu/utilization` |
| Memory used | `agent.googleapis.com/memory/usage` + `state=used` |
| Disk usage | `agent.googleapis.com/disk/usage` + `state=used` |
| Network bytes | `agent.googleapis.com/interface/traffic` |
| Process count | `agent.googleapis.com/processes/count` |

To get faster metric resolution (e.g. for sensitive CPU alerts), increase
collection frequency by adding to `config.yaml`:

```yaml
metrics:
  receivers:
    hostmetrics:
      type: hostmetrics
      collection_interval: 30s
```

---

## 5. Verify Logs Are Arriving

Wait ~2 minutes after restart, then check:

```bash
# Check agent is ingesting the file
sudo journalctl -u google-cloud-ops-agent-fluent-bit -n 50

# Check via gcloud
gcloud logging read \
  'logName=~"tap_lms_structured"' \
  --limit=5 \
  --project=<YOUR_PROJECT_ID> \
  --format=json
```

In Log Explorer, use this filter to see structured events:

```
logName=~"tap_lms_structured"
severity=ERROR
```

Or to see a specific submission end-to-end:

```
logName=~"tap_lms_structured"
jsonPayload.submission_id="<submission_id>"
```

---

## 6. Complete Message Catalogue

Every structured log line has a `message` field. The full list below shows
the message name, which file emits it, and what it means operationally.

### Infrastructure & HTTP

| Message | Source | Notes |
|---|---|---|
| `http_request` | `monitoring.py:235` | Every API call — high volume, excluded from storage by default |
| `background_job` | `monitoring.py:321` | Every RQ/scheduler job boundary (v15 only via before/after_job hooks) |
| `unhandled_exception` | `monitoring.py:493` | Genuine unhandled exception — page immediately |
| `watchdog_alert` | `monitoring.py:482` | Intentional operator alert from watchdog/watcher jobs |

### Submission pipeline

| Message | Source | Notes |
|---|---|---|
| `save_submission_called` | `save_submission.py:114` | Entry point — Glific webhook received |
| `save_submission_success` | `save_submission.py:539` | Submission saved to DB |
| `save_submission_empty_payload` | `save_submission.py:132` | Request body missing |
| `save_submission_validation_error` | `save_submission.py:194` | Schema validation failed |
| `save_submission_not_found_error` | `save_submission.py:215` | Submission doc not found |
| `save_submission_internal_error` | `save_submission.py:245` | Unexpected exception |
| `save_submission_serialization_failure` | `save_submission.py:266` | PostgreSQL serialization conflict |
| `save_submission_retry_exhausted` | `save_submission.py:284` | Max retries hit |
| `save_submission_missing_assignment` | `save_submission.py:325` | Assignment not found |
| `save_submission_student_not_resolved` | `save_submission.py:342` | Student ID not in DB |
| `save_submission_no_active_pe` | `save_submission.py:359` | No active ProgramEnrollment |
| `save_submission_terminal_state` | `save_submission.py:378` | PE already in terminal state |
| `save_submission_placeholder_detected` | `save_submission.py:165` | Submission text is a placeholder |
| `save_submission_insert_failed` | `save_submission.py:437` | DB insert failed |
| `save_submission_processing_queued` | `save_submission.py:1211` | Background job enqueued |
| `student_duplicate_submission` | `state_machine.py:1181` | PE already submitted this week |
| `student_delivery_failure` | `state_machine.py:1263` | State machine delivery error |
| `student_state_transition` | `state_machine.py:130` | PE journey_label changed |
| `enqueue_submission_start` | `save_submission.py:1335` | RabbitMQ publish starting |
| `enqueue_submission_retry` | `save_submission.py:1457` | Retrying RabbitMQ publish |
| `enqueue_submission_failed` | `save_submission.py:1440` | RabbitMQ publish failed |
| `enqueue_submission_dlq` | `save_submission.py:1501` | Submission sent to DLQ |
| `submission_published` | `monitoring.py:378` | Successfully published to RabbitMQ |
| `process_submission_async_start` | `save_submission.py:1239` | Async processing started |
| `process_submission_prepared` | `save_submission.py:1281` | Submission prepared for GCS |
| `process_submission_uploading_gcs` | `save_submission.py:1255` | GCS upload in progress |
| `process_submission_async_failed` | `save_submission.py:1305` | Async processing failed |
| `process_submission_failed_status_update_failed` | `save_submission.py:1323` | Both processing and status update failed |
| `submission_background_processing_failed` | `imgana/submission.py:93` | Legacy endpoint — background job failed |
| `submission_enqueued_raw` | `imgana/submission.py:317` | Legacy endpoint — raw enqueue |
| `submission_enqueued` | `imgana/submission.py:343` | Legacy endpoint — enqueued |
| `submission_enqueue_failed` | `imgana/submission.py:350` | Legacy endpoint — enqueue failed |
| `submission_prepared` | `imgana/submission.py:78` | Legacy endpoint — prepared |
| `submission_status_check_failed` | `imgana/submission.py:393` | Legacy endpoint — status check failed |
| `submission_status_update_failed` | `imgana/submission.py:107` | Legacy endpoint — status update failed |
| `assignment_submission_failed` | `imgana/submission.py:253` | Legacy endpoint — submission failed |
| `assignment_submission_internal_failed` | `imgana/submission.py:208` | Legacy endpoint — internal error |
| `get_assignment_context_failed` | `imgana/submission.py:518` | Assignment context lookup failed |

### GCS (image/audio uploads)

| Message | Source | Notes |
|---|---|---|
| `gcs_upload_success` | `imgana/gcs_client.py:140` | Image uploaded to GCS |
| `gcs_upload_failed` | `imgana/gcs_client.py:162` | GCS upload failed — submission image lost |
| `gcs_download_failed` | `imgana/gcs_client.py:152` | GCS download failed |

### Feedback pipeline

| Message | Source | Notes |
|---|---|---|
| `feedback_result_received` | `monitoring.py:393` | Result arrived from plagiarism queue |
| `feedback_processing_complete` | `monitoring.py:402` | Feedback fully processed |
| `feedback_processing_failed` | `monitoring.py:418` | Processing failed — check `retryable` field |
| `feedback_requested` | `save_submission.py:636` | Feedback requested from student |
| `feedback_fetched` | `save_submission.py:578` | Feedback doc fetched |
| `feedback_not_ready` | `save_submission.py:593` | Feedback not ready yet |
| `feedback_fetch_failed` | `save_submission.py:612` | Fetch failed |
| `feedback_fetch_submission_not_found` | `save_submission.py:603` | Submission missing |
| `feedback_flow_triggered` | `save_submission.py:684` | Glific feedback flow triggered |
| `feedback_flow_already_triggered` | `save_submission.py:645` | Duplicate trigger prevented |
| `feedback_flow_claim_lost` | `save_submission.py:692` | Race — another process claimed |
| `feedback_flow_trigger_failed` | `save_submission.py:722` | Flow trigger failed |
| `feedback_ready_submission_not_found` | `save_submission.py:706` | Submission missing at trigger time |
| `glific_notification_sent` | `monitoring.py:512` | Glific notification send result |

### Feedback audio generation

| Message | Source | Notes |
|---|---|---|
| `feedback_audio_generation_start` | `audio_creation.py:77` | Audio generation starting |
| `feedback_audio_language_defaulted` | `audio_creation.py:46` | Language fallback applied |
| `feedback_audio_speech_generation_start` | `audio_creation.py:124` | TTS starting |
| `feedback_audio_speech_generated` | `audio_creation.py:132` | TTS complete |
| `feedback_audio_gcs_upload_start` | `audio_creation.py:142` | Uploading to GCS |
| `feedback_audio_generation_success` | `audio_creation.py:154` | Audio ready |
| `feedback_audio_generation_failed` | `audio_creation.py:86` | Audio generation failed |

### Dispatcher (pe_dispatcher.py)

| Message | Source | Notes |
|---|---|---|
| `dispatcher_cycle` | `monitoring.py:340` | Every 1-min cycle summary |
| `dispatcher_content_delivery` | `pe_dispatcher.py:299` | Content delivered to student |
| `dispatcher_escalation_no_config` | `pe_dispatcher.py:353` | Missing archetype config — student stuck |
| `dispatcher_escalation_steps_exhausted` | `pe_dispatcher.py:377` | All escalation steps done |
| `dispatcher_escalation_flow` | `pe_dispatcher.py:480` | Escalation flow triggered |
| `dispatcher_escalation_parent_call` | `pe_dispatcher.py:454` | Parent call initiated |
| `dispatcher_feedback_timeout_stale` | `pe_dispatcher.py:521` | Feedback timeout PE is stale |
| `dispatcher_feedback_timeout_resolved` | `pe_dispatcher.py:543` | Feedback timeout resolved |
| `dispatcher_feedback_timeout_retry` | `pe_dispatcher.py:560` | Feedback timeout retry |
| `dispatcher_feedback_timeout_exhausted` | `pe_dispatcher.py:585` | Feedback timeout exhausted |
| `dispatcher_week_advancement_stale` | `pe_dispatcher.py:616` | Week advancement PE is stale |
| `dispatcher_program_completed` | `pe_dispatcher.py:631` | Student completed program |
| `dispatcher_binge_paused` | `pe_dispatcher.py:647` | Binge protection triggered |
| `dispatcher_week_advanced` | `pe_dispatcher.py:663` | Week advanced for student |
| `dispatcher_binge_resumed` | `pe_dispatcher.py:797` | Binge protection lifted |
| `dispatcher_binge_paused_remaining` | `pe_dispatcher.py:813` | Still in binge pause |
| `dispatcher_grace_check_stale` | `pe_dispatcher.py:698` | Grace check PE is stale |
| `dispatcher_grace_check_submitted` | `pe_dispatcher.py:710` | Grace period submission received |
| `dispatcher_grace_check_rescheduled` | `pe_dispatcher.py:727` | Grace check rescheduled |
| `dispatcher_grace_expired_dropped` | `pe_dispatcher.py:745` | Grace expired — dropped |
| `dispatcher_pause_check_stale` | `pe_dispatcher.py:773` | Pause check PE is stale |

### Glific integration

| Message | Source | Notes |
|---|---|---|
| `glific_token_health` | `glific_integration.py:1583` | Token probe result — check `token_status` field |
| `glific_contact_created` | `glific_integration.py:257` | Contact created in Glific |
| `glific_create_contact_failed` | `glific_integration.py:242` | Contact creation failed |
| `glific_contact_fields_updated` | `glific_integration.py:437` | Contact fields updated |
| `glific_contact_fields_update_failed` | `api.py:1576` | Contact fields update failed |
| `glific_update_contact_failed` | `glific_integration.py:448` | Contact update failed |
| `glific_flow_started` | `glific_integration.py:828` | Flow triggered successfully |
| `glific_flow_start_failed` | `glific_integration.py:837` | Flow trigger failed |
| `glific_start_group_flow_success` | `glific_extensions.py:86` | Group flow started |
| `glific_start_group_flow_failed` | `glific_extensions.py:95` | Group flow failed |
| `glific_start_group_flow_api_error` | `glific_extensions.py:72` | API error on group flow |
| `glific_start_group_flow_exception` | `glific_extensions.py:114` | Exception on group flow |
| `glific_add_contacts_bulk_success` | `glific_extensions.py:186` | Bulk add succeeded |
| `glific_add_contacts_bulk_failed` | `glific_extensions.py:195` | Bulk add failed |
| `glific_add_contacts_bulk_api_error` | `glific_extensions.py:172` | Bulk add API error |
| `glific_add_contacts_bulk_exception` | `glific_extensions.py:215` | Bulk add exception |
| `glific_bulk_add_circuit_tripped` | `glific_extensions.py:482` | Circuit breaker open on bulk add |
| `glific_bulk_add_complete` | `glific_extensions.py:512` | Bulk add batch complete |
| `glific_remove_contacts_bulk_success` | `glific_extensions.py:293` | Bulk remove succeeded |
| `glific_remove_contacts_bulk_failed` | `glific_extensions.py:304` | Bulk remove failed |
| `glific_remove_contacts_bulk_api_error` | `glific_extensions.py:279` | Bulk remove API error |
| `glific_remove_contacts_bulk_exception` | `glific_extensions.py:324` | Bulk remove exception |
| `glific_bulk_remove_circuit_tripped` | `glific_extensions.py:628` | Circuit breaker open on bulk remove |
| `glific_bulk_remove_complete` | `glific_extensions.py:658` | Bulk remove batch complete |

### Vocallabs (parent calls)

| Message | Source | Notes |
|---|---|---|
| `vocallabs_initiating_call` | `vocallabs.py:191` | Call being placed |
| `vocallabs_call_success` | `vocallabs.py:225` | Call succeeded |
| `vocallabs_call_transient_failure` | `vocallabs.py:1249` | Transient failure — will retry |
| `vocallabs_call_duplicate_prospect_no_retry` | `vocallabs.py:1218` | Duplicate prospect — not retried |
| `vocallabs_call_double_fault` | `vocallabs.py:1283` | Double fault — check manually |
| `vocallabs_call_dlq_exhausted` | `vocallabs.py:1311` | DLQ exhausted — call permanently failed |
| `vocallabs_disabled` | `vocallabs.py:131` | Vocallabs disabled in settings |
| `vocallabs_dormant_skipped` | `vocallabs.py:177` | Dormant PE skipped |
| `vocallabs_pe_not_found` | `vocallabs.py:103` | PE not found |
| `vocallabs_phone_missing` | `vocallabs.py:165` | Student phone number missing |
| `vocallabs_config_missing` | `vocallabs.py:149` | Vocallabs config not set up |
| `vocallabs_settings_missing` | `vocallabs.py:117` | Settings doc missing |

### Teacher onboarding

| Message | Source | Notes |
|---|---|---|
| `teacher_linked_to_glific` | `api.py:1498` | Teacher linked to Glific contact |
| `teacher_missing_glific_id` | `api.py:1478` | Teacher has no Glific ID |
| `teacher_glific_contact_created` | `api.py:1550` | Glific contact created for teacher |
| `teacher_glific_contact_creation_failed` | `api.py:1557` | Contact creation failed |
| `teacher_still_missing_glific_id` | `api.py:1583` | Still no Glific ID after retry |
| `teacher_added_to_batch_group` | `api.py:1607` | Added to batch Glific group |
| `teacher_group_addition_failed` | `api.py:1615` | Group addition failed |
| `teacher_batch_history_creation_failed` | `api.py:1635` | Batch history insert failed |
| `teacher_batch_update_exception` | `api.py:1676` | Batch update exception |
| `teacher_optin_failed` | `background_jobs.py:45` | Teacher opt-in failed |
| `teacher_glific_id_missing_in_background_job` | `background_jobs.py:58` | Glific ID missing in background |
| `teacher_added_to_group_background` | `background_jobs.py:88` | Added to group in background |
| `teacher_group_addition_failed_background` | `background_jobs.py:96` | Group addition failed in background |
| `teacher_group_creation_failed_background` | `background_jobs.py:104` | Group creation failed |
| `teacher_group_management_error` | `background_jobs.py:113` | General group management error |
| `teacher_group_skipped_no_batch` | `background_jobs.py:120` | No batch found for teacher |
| `teacher_onboarding_flow_started_background` | `background_jobs.py:140` | Onboarding flow started |
| `teacher_onboarding_flow_failed_background` | `background_jobs.py:148` | Onboarding flow failed |
| `teacher_onboarding_flow_not_found` | `background_jobs.py:158` | Flow not configured |

### Quiz

| Message | Source | Notes |
|---|---|---|
| `quiz_started` | `student_progression_sp.py:1381` | Student started quiz |
| `quiz_resumed` | `student_progression_sp.py:1449` | Student resumed quiz |
| `quiz_answer_submitted` | `student_progression_sp.py:1631` | Answer submitted |
| `quiz_completed` | `student_progression_sp.py:1685` | Quiz completed |

### API / misc

| Message | Source | Notes |
|---|---|---|
| `active_batch_not_found` | `api.py:64` | No active batch for school |
| `active_batch_not_found_create_teacher` | `api.py:1802` | No batch when creating teacher |
| `api_list_cities_exception` | `api.py:140` | City list API exception |
| `api_list_districts_exception` | `api.py:101` | District list API exception |
| `create_teacher_web_exception` | `api.py:1948` | Teacher creation exception |
| `verify_otp_exception` | `api.py:1708` | OTP verification exception |
| `gupshup_settings_missing` | `api.py:154` | Gupshup settings not configured |
| `gupshup_settings_incomplete` | `api.py:166` | Gupshup settings incomplete |
| `gupshup_send_failed` | `api.py:193` | Gupshup message send failed |
| `no_english_language_found` | `api.py:1523` | No English language record |
| `model_name_not_found` | `api.py:2123` | Model name resolution failed |
| `model_resolved_from_batch_onboarding` | `api.py:2104` | Model from batch onboarding |
| `model_resolved_from_school_default` | `api.py:2113` | Model from school default |
| `process_glific_actions_exception` | `background_jobs.py:171` | Glific actions background job failed |

---

## 7. Recommended Cloud Monitoring Alerts

Set these up in GCP Console → **Monitoring → Alerting → Create Policy**.

### Page immediately (ERROR severity)

```
# Unhandled exception
jsonPayload.message = "unhandled_exception"
severity = "ERROR"
```

```
# Glific token stale
jsonPayload.message = "glific_token_health"
jsonPayload.token_status = "stale"
severity = "ERROR"
```

```
# Non-retryable feedback failure → check DLQ
jsonPayload.message = "feedback_processing_failed"
jsonPayload.retryable = false
severity = "ERROR"
```

```
# GCS upload failed — submission image lost
jsonPayload.message = "gcs_upload_failed"
severity = "ERROR"
```

```
# Vocallabs DLQ exhausted — call permanently failed
jsonPayload.message = "vocallabs_call_dlq_exhausted"
severity = "ERROR"
```

### Notify Slack (WARNING — investigate)

```
# Watchdog alerts (stuck PEs, DLQ depth etc.)
jsonPayload.message = "watchdog_alert"
severity = "WARNING"
```

```
# Glific flow not triggering for student
jsonPayload.message = "glific_flow_start_failed"
```

```
# Feedback not being delivered after processing
jsonPayload.message = "feedback_flow_trigger_failed"
```

```
# Dispatcher missing archetype config — student stuck
jsonPayload.message = "dispatcher_escalation_no_config"
```

```
# Retryable feedback failure — watch for repeated occurrences
jsonPayload.message = "feedback_processing_failed"
jsonPayload.retryable = true
severity = "WARNING"
```

```
# Submission sent to DLQ
jsonPayload.message = "enqueue_submission_dlq"
```

### Hardware metric alerts

| Metric | Threshold | Action |
|---|---|---|
| `agent.googleapis.com/cpu/utilization` | > 80% for 5 min | Slack |
| `agent.googleapis.com/memory/percent_used` | > 85% for 5 min | Slack |
| `agent.googleapis.com/disk/percent_used` | > 80% | Slack + email |

---

## 8. Useful Log Explorer Queries

**Trace a submission end-to-end:**
```
jsonPayload.submission_id="<submission_id>"
```

**All errors in the last hour:**
```
severity=ERROR
timestamp >= "2026-07-29T01:00:00Z"
```

**Which students had feedback failures today:**
```
jsonPayload.message="feedback_processing_failed"
jsonPayload.retryable=false
```

**Dispatcher stuck PEs:**
```
jsonPayload.message="dispatcher_escalation_no_config"
```

**Glific bulk operation failures:**
```
jsonPayload.message=~"glific_(add|remove)_contacts_bulk_failed"
```

**All Vocallabs failures:**
```
jsonPayload.message=~"vocallabs_call_(transient_failure|double_fault|dlq_exhausted)"
```

---

## 9. Troubleshooting

**Agent not starting:**
```bash
sudo journalctl -u google-cloud-ops-agent -n 50
sudo journalctl -u google-cloud-ops-agent-fluent-bit -n 50
```

**Logs not appearing in Cloud Logging:**
```bash
# Check the agent can read the log file
sudo -u root cat /home/lms-dev/frappe-bench/logs/gcp_structured.log | head -5

# Check IAM permissions
gcloud projects get-iam-policy <PROJECT_ID> \
  --flatten="bindings[].members" \
  --filter="bindings.members:<SERVICE_ACCOUNT_EMAIL>"
```

**severity still showing as grey in Log Explorer:**
Check the `promote_severity` processor is in the pipeline and the config
was reloaded:
```bash
sudo systemctl restart google-cloud-ops-agent
```

**Config syntax error:**
```bash
sudo google-cloud-ops-agent --config /etc/google-cloud-ops-agent/config.yaml --dryrun
```

**After `bench setup nginx` overwrites nginx.conf:**
The Ops Agent config at `/etc/google-cloud-ops-agent/config.yaml` is
unaffected — only nginx's config is overwritten. No Ops Agent action needed.

---

## 10. Adding to Deployment Runbook

After any code deploy that adds new log message types or changes field names
in `monitoring.py`, check:

1. The new message type appears in Log Explorer
2. The `severity` field is being promoted correctly
3. Any new intentional `frappe.log_error()` calls have their titles added
   to `_OPERATOR_ALERT_TITLES` in `monitoring.py`
4. If the new message type needs a Cloud Monitoring alert, add it

---

## 11. File Locations

| File | Purpose |
|---|---|
| `/etc/google-cloud-ops-agent/config.yaml` | Active Ops Agent config |
| `deployment_configs/ops_agent_config.yaml` | Config stored in repo (source of truth) |
| `/home/lms-dev/frappe-bench/logs/gcp_structured.log` | tap_lms structured log |
| `/home/lms-dev/frappe-bench/logs/rag_gcp_structured.log` | rag_service structured log |
| `/var/log/google-cloud-ops-agent/` | Ops Agent self logs |
