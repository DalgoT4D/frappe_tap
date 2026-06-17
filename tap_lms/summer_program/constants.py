"""
Summer Program Constants
tap_lms/summer_program/constants.py

Enums and shared constants for the Summer Program module.
All values match the ProgramEnrollment doctype Select options exactly.
"""


# ── Archetypes ──────────────────────────────────────────────
# Canonical archetype values are lowercase-snake_case to match:
#   1. The DocType Select options on `ArchetypeConfig.archetype` and
#      `Student.archetype` (updated 2026-05-14 to align with upstream
#      data shape).
#   2. The Glific collection label form (e.g., `SP_..._dormant_arm_a`).
# Older code that compared against Title Case literals (`"Dormant"`,
# etc.) was a bug; those have been swept and lowercased.
ARCHETYPE_DORMANT = "dormant"
ARCHETYPE_FENCE_SITTER = "fence_sitter"
ARCHETYPE_IRREGULAR_SUBMITTER = "irregular_submitter"
ARCHETYPE_SUBMITTER = "submitter"

ALL_ARCHETYPES = [
    ARCHETYPE_DORMANT,
    ARCHETYPE_FENCE_SITTER,
    ARCHETYPE_IRREGULAR_SUBMITTER,
    ARCHETYPE_SUBMITTER,
]

# ── Experiment Arms ─────────────────────────────────────────
ARM_DEFAULT = "default"
ARM_A = "arm_a"
ARM_B = "arm_b"

ALL_ARMS = [ARM_DEFAULT, ARM_A, ARM_B]


# ── Collection labels ───────────────────────────────────────
def collection_label(batch_id, archetype, arm):
    """Build the Glific collection label for an archetype × arm combo.

    archetype is now the canonical lowercase-snake_case form (e.g., 'dormant'),
    so no transformation is needed. Defensive fallback handles legacy uppercase
    values that may exist in fixtures or migrated rows.
    """
    arch_key = archetype.lower().replace(" ", "_") if archetype else "submitter"
    return f"SP_{batch_id}_{arch_key}_{arm}"


# ── Resolved Flow States (matches PE doctype Select) ───────
# These are the valid states for ProgramEnrollment.resolved_flow_state.
#
# CR-003: STATE_PAUSED_NO_ACTIVITY retired — no new transition writes it.
# The Select option remains in the PE JSON as a legacy value for historical
# rows; the migration patch (`patches.cr_003.grace_and_reengagement`) moves
# any active legacy PEs to program_dropped. New PEs never enter this state.
STATE_NORMAL_CONTENT = "normal_content_delivery"
STATE_NORMAL_ESCALATION = "normal_escalation"
STATE_REMEDIAL_CONTENT = "remedial_content_delivery"
STATE_REMEDIAL_ESCALATION = "remedial_escalation"
STATE_GRACE_WAITING = "grace_waiting"
STATE_PAUSED_BINGE = "paused_binge"
STATE_SUBMITTED_AWAITING = "submitted_awaiting_feedback"
STATE_FEEDBACK_READY = "feedback_ready"
STATE_WEEK_COMPLETED = "week_completed"
STATE_PROGRAM_COMPLETED = "program_completed"
STATE_PROGRAM_DROPPED = "program_dropped"

# Content delivery states (Core or Remedial)
CONTENT_DELIVERY_STATES = [STATE_NORMAL_CONTENT, STATE_REMEDIAL_CONTENT]

# Escalation states
ESCALATION_STATES = [STATE_NORMAL_ESCALATION, STATE_REMEDIAL_ESCALATION]

# Paused states — only binge-pause is live post-CR-003.
PAUSED_STATES = [STATE_PAUSED_BINGE]

# Terminal states (no further scheduling)
TERMINAL_STATES = [STATE_PROGRAM_COMPLETED, STATE_PROGRAM_DROPPED]


# ── Journey Labels (matches PE doctype Select) ─────────────
LABEL_ENROLLED = "enrolled"
LABEL_CONTENT_DELIVERED = "content_delivered"
# CR-004: distinct journey label for remedial entry (T6 and T6b), to separate
# the analytics signal "student is in Core content" from "student fell back to
# Remedial". Both T6 (escalation-exhausted) and T6b (failed AI feedback) write
# this same value so downstream analytics can identify any remedial entry
# without needing to join on `current_path`.
LABEL_REMEDIAL_STARTED = "remedial_started"
LABEL_SUBMITTED = "submitted"
LABEL_FEEDBACK_DELIVERED = "feedback_delivered"
LABEL_WEEK_SUMMARY_SENT = "week_summary_sent"
LABEL_GRACE_WINDOW = "grace_window"
LABEL_PAUSED = "paused"
LABEL_RESUMED = "resumed"
LABEL_COMPLETED = "completed"
LABEL_DROPPED = "dropped"
LABEL_WEEK_ADVANCED = "week_advanced"


# ── Program Status (matches PE doctype Select) ─────────────
PROGRAM_ACTIVE = "active"
PROGRAM_PAUSED = "paused"
PROGRAM_COMPLETED = "completed"
PROGRAM_DROPPED = "dropped"


# ── Paths ──────────────────────────────────────────────────
PATH_CORE = "Core"
PATH_REMEDIAL = "Remedial"


# ── Scheduler Action Types (matches PE doctype Select) ─────
# CR-003: ACTION_GRACE_REMINDER and ACTION_RE_ENGAGEMENT are retired.
# Proactive grace reminders are gone (escalation steps within the week are
# the only reminders); proactive re-engagement is gone (re-engagement is
# inbound-only via SP_Incoming_Router). The Select options remain in the PE
# JSON as legacy values, but no new transition writes them. The migration
# patch nulls these values on any in-flight PEs.
ACTION_CONTENT_DELIVERY = "content_delivery"
ACTION_ESCALATION = "escalation"
ACTION_WEEK_ADVANCEMENT = "week_advancement"
ACTION_FEEDBACK_NOTIFICATION = "feedback_notification"
ACTION_GRACE_CHECK = "grace_check"
ACTION_PAUSE_CHECK = "pause_check"
ACTION_FEEDBACK_TIMEOUT = "feedback_timeout"

ALL_ACTION_TYPES = [
    ACTION_CONTENT_DELIVERY,
    ACTION_ESCALATION,
    ACTION_WEEK_ADVANCEMENT,
    ACTION_FEEDBACK_NOTIFICATION,
    ACTION_GRACE_CHECK,
    ACTION_PAUSE_CHECK,
    ACTION_FEEDBACK_TIMEOUT,
]

# Collection-based actions (one API call per collection)
COLLECTION_ACTIONS = [
    ACTION_CONTENT_DELIVERY,
    ACTION_ESCALATION,
]

PER_STUDENT_ACTIONS = [
    ACTION_WEEK_ADVANCEMENT,
    ACTION_GRACE_CHECK,
    ACTION_PAUSE_CHECK,
    ACTION_FEEDBACK_TIMEOUT,
]

# Maps action type → BatchProgramRun field that stores the Glific flow ID
ACTION_FLOW_FIELD_MAP = {
    ACTION_CONTENT_DELIVERY: "content_delivery_flow",
    ACTION_ESCALATION: "escalation_flow",
    ACTION_GRACE_CHECK: "grace_notification_flow",
    ACTION_PAUSE_CHECK: "binge_info_flow",
    "program_complete": "program_complete_flow",
    # NOTE: feedback_delivery_flow removed — FeedbackConsumer handles
    # feedback notification via its own Glific Flow lookup (label="feedback").
    # CR-003: reengagement_flow / grace_notification_flow (reminder) entries
    # removed — those handlers are gone.
}


# ── Pause Reasons ──────────────────────────────────────────
PAUSE_NO_ACTIVITY = "no_activity"
PAUSE_BINGE_LIMIT = "binge_limit"


# ── BatchProgramRun statuses ──────────────────────────────
BPR_DRAFT = "draft"
BPR_IMPORTING = "importing"
BPR_ENROLLING = "enrolling"
BPR_COLLECTIONS_READY = "collections_ready"
BPR_ACTIVE = "active"
BPR_COMPLETED = "completed"

BPR_STATUS_FLOW = [
    BPR_DRAFT, BPR_IMPORTING, BPR_ENROLLING,
    BPR_COLLECTIONS_READY, BPR_ACTIVE, BPR_COMPLETED,
]

# ── Validation statuses ──────────────────────────────────
VALIDATION_NOT_RUN = "not_run"
VALIDATION_PASSED = "passed"
VALIDATION_FAILED = "failed"


# ── Glific Contact Field Keys ─────────────────────────────
# These are the 28 contact field names set on Glific contacts (post CR-003).
# Cache size evolution:
#   - Original baseline: 18 fields
#   - +CR-002 v2: +8 gamification fields (activity/quiz/submission points split,
#     special_gems, weekly_submission_done sticky flag) → 26
#   - +CR-003: +2 escalation routing fields (escalation_order, escalation_type)
#     → 28
# `weekly_video_done` is intentionally NOT pushed — internal-only state-machine
# flag (see CR-002 v2 §"Five gamification dimensions on PE").
# `bonus_quiz_points` is recorded on ProgramEnrollment but is intentionally
# excluded from the standard contact-field sync payload in this version.
# See state_machine._enqueue_contact_field_sync for the canonical field
# provenance docstring listing all 28 fields and their sources.
CF_STUDENT_ID = "student_id"
CF_BATCH_ID = "batch_id"
CF_ARCHETYPE = "archetype"
# CF_LANGUAGE_ID (2026-05-19 rename, was CF_LANGUAGE="language"):
#   Avoids name collision with Glific's CORE `language` field (a built-in
#   that stores the Glific language integer ID and is set via the
#   updateContact mutation's `languageId` input, NOT via `fields`).
#   Our custom `language_id` field acts as a flow-readable BACKUP/cache;
#   the authoritative value lives on Glific CORE.
#   Value is the Glific INTEGER language ID resolved from
#   TAP Language.glific_language_id at push time — NOT the language NAME.
CF_LANGUAGE_ID = "language_id"
CF_RESOLVED_FLOW_STATE = "resolved_flow_state"
CF_CURRENT_WEEK = "current_week"
CF_CURRENT_PATH = "current_path"
CF_CURRENT_TIER = "current_tier"
CF_PROGRAM_STATUS = "program_status"
CF_TOTAL_POINTS = "total_points"
CF_CURRENT_STREAK = "current_streak"
CF_GRACE_WINDOW_END = "grace_window_end_at"
CF_EXPECTED_SUBMISSION = "current_expected_submission_type"
CF_EXPERIMENT_ARM = "experiment_arm"
CF_COURSE_LEVEL = "course_level"
CF_STUDENT_NAME = "student_name"
CF_LAST_ESCALATION_STEP = "last_escalation_step"
CF_SUBMISSION_COUNT = "submission_count"

# ── CR-002 v2 gamification fields ─────────────────────────
# 8 new contact fields pushed alongside the existing 18 (cache size 26 after
# this CR; 28 after CR-003 also ships escalation_order + escalation_type;
# 29 after task #98 also ships bonus_quiz_points — 2026-05-25;
# 30 after task #7 ships weekly_engagement_points — 2026-05-26).
# Glific gamification rendering reads these directly via @contact.<field_name>;
# per L-008 the names are public contract — do not rename.
CF_TOTAL_ACTIVITY_POINTS = "total_activity_points"
CF_WEEKLY_ACTIVITY_POINTS = "weekly_activity_points"
CF_TOTAL_QUIZ_POINTS = "total_quiz_points"
CF_WEEKLY_QUIZ_POINTS = "weekly_quiz_points"
CF_BONUS_QUIZ_POINTS = "bonus_quiz_points"
CF_TOTAL_SUBMISSION_POINTS = "total_submission_points"
CF_WEEKLY_SUBMISSION_POINTS = "weekly_submission_points"
CF_SPECIAL_GEMS = "special_gems"
CF_WEEKLY_SUBMISSION_DONE = "weekly_submission_done"

# CF_WEEKLY_ENGAGEMENT_POINTS (2026-05-26):
# Computed (NOT stored on PE) — `weekly_submission_points + weekly_activity_points`.
# Pushed alongside the canonical weekly_* fields so Glific flow templates can
# render a single "engagement" stat without computing the sum in the flow.
# Resets naturally with the source columns at the CR-008 lazy reset (first
# VideoClass of new week) because both addends reset to 0 in the same SQL
# UPDATE — no separate column for this handler chain to maintain.
CF_WEEKLY_ENGAGEMENT_POINTS = "weekly_engagement_points"

# IMPORTANT — multi-tenant Glific boundary (2026-05-26):
# Legacy Glific contact fields like `streak`, `gems`, `bonus_points`,
# `current_activity_points`, `recent_quiz_points`, `streak_v1`,
# `orginal_streak`, `total_p`, etc. exist on the shared Glific organisation
# and are OWNED BY OTHER PROGRAMS (TLM, scert, pocflow, …). The Glific org
# has ~470 contact field definitions across all programs that share it.
#
# **The SP backend must NEVER write to a contact field it didn't create.**
# Writing to `streak` or `gems` from SP would silently clobber values that
# other programs depend on for their own flow logic. Treat the SP namespace
# as exactly the 29 fields registered by `dev_tools.bootstrap_sp_contact_fields()`
# — anything else on the contact is out of bounds.
#
# Therefore: SP writes to `current_streak` and `special_gems` (our canonical,
# SP-owned keys); we do NOT alias-bridge to the legacy `streak`/`gems` keys
# even if pre-CR-002 SP flows still reference them. The fix for any
# SP-owned flow still reading legacy keys is to update the flow to point
# at the SP-owned key. Other programs' flows reading their own legacy keys
# are untouched, as intended.

# ── CR-003 escalation channel routing fields ──────────────
# Pushed before the SP_Escalation flow trigger so Glific can branch on the
# current step's escalation_order and escalation_type (help_note_a /
# help_note_b / voice_note / parent_call). Cache size 26 → 28 after CR-003;
# → 29 after task #98 (bonus_quiz_points, 2026-05-25);
# → 30 after task #7 (weekly_engagement_points, 2026-05-26).
# Per L-008, these names are public contract — do not rename.
CF_ESCALATION_ORDER = "escalation_order"
CF_ESCALATION_TYPE = "escalation_type"


# ── Glific sync retry policy (pattern P-007 / lesson L-015) ───
# Background-job retry budget for update_contact_fields failures.
# After MAX_RETRIES attempts the job logs to a DLQ-titled Error Log entry
# so operators can replay manually. Increase if Glific outages become longer
# than ~5 minutes typical.
# Retry budget: 5 immediate retries (no backoff in this revision — see
# follow-up task for proper exponential backoff scheduler). 5 is chosen so
# Redis blips and Glific transient 502/503s self-heal, but a sustained
# Glific outage (>~30 seconds) will still DLQ. Acceptable trade-off vs.
# the previous behavior which silently dropped on first failure.
GLIFIC_SYNC_MAX_RETRIES = 5
GLIFIC_SYNC_RETRY_LOG_TITLE = "SP Glific Sync Retry"
GLIFIC_SYNC_DLQ_LOG_TITLE = "SP Glific Sync DLQ — manual replay required"

# ── RabbitMQ feedback pipeline retry policy (pattern P-007 / lesson L-015) ─
# Mirror of the Glific sync policy. When the broker is briefly unreachable
# (network blip, RabbitMQ restart), retry the publish so the submission isn't
# lost. After MAX_RETRIES attempts, drop to DLQ with a structured payload that
# includes student_id, submission_id, and the original message so operators
# can replay.
FEEDBACK_PIPELINE_MAX_RETRIES = 5
FEEDBACK_PIPELINE_RETRY_LOG_TITLE = "SP Feedback Pipeline Retry"
FEEDBACK_PIPELINE_DLQ_LOG_TITLE = "SP Feedback Pipeline DLQ — manual replay required"


# ── Tier mapping ──────────────────────────────────────────
TIER_BY_WEEK = {
    1: "Basic",
    2: "Basic",
    3: "Intermediate",
    4: "Intermediate",
    5: "Advanced",
}
DEFAULT_TIER = "Advanced"
REMEDIAL_TIER = "Remedial"


# ── Scheduler constants ──────────────────────────────────
ENROLLMENT_QUEUE = "long"
COLLECTION_BATCH_SIZE = 500
ENROLLMENT_CHUNK_SIZE = 100

# Grace window
# CR-003: per-week grace clock. Duration sourced from Batch.grace_window_days
# (per-cohort, default 14). The previous hardcoded GRACE_WINDOW_DAYS = 14 and
# GRACE_REMINDER_DAYS = [7, 11, 13] (proactive reminders) are removed —
# escalation steps within the week are the only reminders.
DEFAULT_GRACE_WINDOW_DAYS = 14  # only used if batch.grace_window_days is unset

# Feedback
FEEDBACK_TIMEOUT_HOURS = 4
MAX_FEEDBACK_RETRIES = 3

# Delivery
MAX_DELIVERY_FAILURES = 3

# ── Vocallabs (CR-003) ──────────────────────────────────
# Parent-call integration retry budget; same shape as the Glific sync and
# feedback pipeline policies above (P-007 / L-015). On exhaustion the job
# DLQs to Error Log with student_id, pe_name, week, escalation_order,
# parent_phone, and the final error. Per CR-003 §E4 a DLQ does NOT extend
# the grace window — the PE proceeds toward drop on the existing schedule.
VOCALLABS_MAX_RETRIES = 5
VOCALLABS_RETRY_LOG_TITLE = "SP Vocallabs Retry"
VOCALLABS_DLQ_LOG_TITLE = "SP Vocallabs DLQ — manual replay required"
# Task #80: distinct title for permanent (no-retry) failures where the parent
# phone is already in the Vocallabs prospect group. These are NOT real outages
# — they're a known limitation of addMultipleContactsToGroup (Hasura uniqueness
# constraint on client_id+prospect_group_id+phone). Logging under a separate
# title lets ops filter them from real transient failures and surfaces the
# scale of the problem until task #81 (Vocallabs lookup endpoint) ships.
VOCALLABS_DUPLICATE_PROSPECT_LOG_TITLE = "SP Vocallabs Duplicate Prospect (lookup required)"
# Sentinel constraint name returned by Vocallabs Hasura when the parent's
# (client_id, prospect_group_id, phone) tuple already exists in the
# vocallabs_prospects table. Matched as a substring against the error message
# so a future Vocallabs constraint rename only requires updating this constant.
VOCALLABS_DUPLICATE_PROSPECT_CONSTRAINT = (
    "prospects_client_id_prospect_group_id_phone_key"
)
VOCALLABS_HTTP_TIMEOUT_SECONDS = 10
VOCALLABS_TOKEN_CACHE_KEY = "vocallabs:auth_token"
VOCALLABS_DEFAULT_TOKEN_TTL = 3600  # seconds; used if VoiceAgentSettings.auth_token_cache_ttl unset

# ── Vocallabs auto-backfill (task #81) ──────────────────
# When addMultipleContactsToGroup returns the uniqueness-constraint error
# (parent phone already in the prospect group AND Student.vocallabs_prospect_id
# is empty), the wrapper paginates GET /b2b/vocallabs/getContacts to find
# the existing prospect_id by phone, caches it on Student, then proceeds
# with the call. Without this, every cold-cache encounter with a previously
# inserted phone would require manual operator backfill.
#
# Cost: each lookup is a sequential pagination over getContacts. PAGE_SIZE
# tuned to balance HTTP roundtrips against payload size; MAX_PAGES caps the
# worst-case at 10k contacts scanned (≈1-2 min wall-clock at typical
# Vocallabs response times) so a misconfigured matcher can't hang a worker
# indefinitely. The lookup is rare in steady state — only fires on the very
# first call for a parent whose phone Vocallabs already has on file.
VOCALLABS_LOOKUP_PAGE_SIZE = 200
VOCALLABS_LOOKUP_MAX_PAGES = 50
VOCALLABS_LOOKUP_LOG_TITLE = "SP Vocallabs Lookup"
