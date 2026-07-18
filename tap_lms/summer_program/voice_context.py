"""
Didi Voice Agent — Student context and nudge situation computation.

Called by vocallabs.py at call time to build the personalised data_block
sent to Vocallabs. All data comes from Frappe (ProgramEnrollment, Student,
StudentGlificContext). No BigQuery queries at runtime.

Public API
----------
    _build_student_context(pe, student) -> dict
        Builds the full context dict with all 10 new template variables.
        Called in vocallabs._call_vocallabs() and _render_status_template().

    _check_call_window(settings) -> bool
        True if current IST time is within the configured call window.

    _check_rate_limit(pe, settings) -> bool
        True if this call is within rate limits (cooldown + weekly cap).

L-NNN notes
-----------
L-VC-001: All functions in this module are pure reads. They never write to
          the database. Call-count updates happen in vocallabs.py after
          a successful call, not here.
L-VC-002: _compute_situation() evaluates VoiceNudgeConfig records in
          priority order (sort_field=priority ASC). First match wins.
          N8 (celebration) is NOT computed here — it is triggered
          separately via ProgramEventLog events.
L-VC-003: Glific context (StudentGlificContext) is read via frappe.db.get_value
          with ignore_permissions=True. These records are system-managed
          and have no user-level permission gate.
"""

from datetime import datetime

import frappe
import pytz

_IST = pytz.timezone("Asia/Kolkata")

# ── Submission type -> plain language ask ─────────────────────────────────
_SUBMISSION_ASK = {
    "emoji":               "koi bhi emoji bhejna hai",
    "word_text_voice":     "ek chota sa voice note ya text bhejna hai",
    "summary_text_voice":  "aaj aapne kya seekha, woh likho ya awaaz mein batao",
    "image":               "apni activity ki photo bhejna hai",
    "video":               "ek chota video bhejna hai",
    "photo_video_artefact": "apne banaye huye cheez ki photo ya video bhejna hai",
}


# ════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════


def build_student_context(pe, student):
    """Build the full personalisation context dict.

    Returns a dict with keys matching the new _TEMPLATE_VARS entries.
    All values are strings (Vocallabs data_block is string-only).
    Falls back gracefully for any missing field.
    """
    glific_ctx = _get_glific_context(student.name)

    situation = _compute_situation(pe, student, glific_ctx)
    course = _get_course_name(pe)
    submission_count = str(int(pe.submission_count or 0))
    streak = str(int(pe.current_streak or 0))
    submission_ask = _SUBMISSION_ASK.get(
        (pe.current_expected_submission_type or "").strip(),
        "kuch bhi bhejna hai",
    )
    grace_deadline = _format_grace_date(pe.grace_window_end_at)
    grade_group = _get_grade_group(student)
    call_attempt = str(int(pe.total_call_count or 0) + 1)

    # Glific-sourced fields (often empty — handled gracefully in template)
    last_problem = (glific_ctx.get("sp_submission_link") or "").strip()
    last_message = (glific_ctx.get("last_inbound_message") or "").strip()

    return {
        "situation":             situation,
        "course":                course,
        "submission_count":      submission_count,
        "streak":                streak,
        "submission_ask":        submission_ask,
        "grace_deadline":        grace_deadline,
        "grade_group":           grade_group,
        "call_attempt":          call_attempt,
        "last_problem_reported": last_problem,
        "last_message":          last_message,
    }


def check_call_window(settings):
    """Return True if the current IST time is within the configured call window.

    Returns True if either window field is not set (no restriction).
    """
    window_start = getattr(settings, "call_window_start", None)
    window_end = getattr(settings, "call_window_end", None)

    if not window_start or not window_end:
        return True

    now_ist = datetime.now(_IST).time()

    # Handle Time fields that may come back as timedelta or time objects
    start = _to_time(window_start)
    end = _to_time(window_end)

    if start is None or end is None:
        return True

    if start <= end:
        return start <= now_ist <= end
    else:
        # Window wraps midnight (e.g. 22:00 to 06:00)
        return now_ist >= start or now_ist <= end


def check_rate_limit(pe, settings):
    """Return True if this call is within rate limits.

    Checks two limits:
    1. Cooldown: hours since last_call_at >= call_cooldown_hours.
    2. Weekly cap: weekly_call_count < max_calls_per_student_per_week.

    Returns True if limits are not configured (no restriction).
    """
    from frappe.utils import now_datetime, get_datetime

    # Cooldown check
    cooldown_hours = int(getattr(settings, "call_cooldown_hours", 0) or 0)
    if cooldown_hours and pe.last_call_at:
        hours_since = (now_datetime() - get_datetime(pe.last_call_at)).total_seconds() / 3600
        if hours_since < cooldown_hours:
            return False

    # Weekly cap check
    max_weekly = int(getattr(settings, "max_calls_per_student_per_week", 0) or 0)
    if max_weekly and int(pe.weekly_call_count or 0) >= max_weekly:
        return False

    return True


# ════════════════════════════════════════════════════════════
# Situation computation
# ════════════════════════════════════════════════════════════


# Module-level nudge config cache. Populated by _load_nudge_cache() and
# invalidated after TTL_SECONDS seconds. This avoids re-querying
# VoiceNudgeConfig + child tables for every student during queue generation
# (79K students × 17 queries = 1.3M queries without this cache).
_NUDGE_CACHE = None
_NUDGE_CACHE_AT = None
_NUDGE_CACHE_TTL = 300  # 5 minutes


def _load_nudge_cache():
    """Load VoiceNudgeConfig + child tables into module-level cache.

    Returns a list of dicts, each containing the config fields plus
    pre-loaded child table lists (allowed escalation types, submission types).
    Cached for 5 minutes to survive a full campaign queue generation pass
    without re-querying. Invalidated automatically after TTL.
    """
    global _NUDGE_CACHE, _NUDGE_CACHE_AT
    import time

    now = time.monotonic()
    if _NUDGE_CACHE is not None and _NUDGE_CACHE_AT is not None:
        if now - _NUDGE_CACHE_AT < _NUDGE_CACHE_TTL:
            return _NUDGE_CACHE

    configs = frappe.get_all(
        "VoiceNudgeConfig",
        filters={"is_active": 1},
        fields=[
            "nudge_type", "situation_label", "priority",
            "require_prior_submission", "require_no_weekly_submission",
            "require_active_streak", "require_program_paused",
            "require_grace_window", "min_week", "min_submission_count",
        ],
        order_by="priority asc",
    )

    # Pre-load all child table rows in two bulk queries (not N per config)
    all_escalation_types = frappe.db.get_all(
        "VoiceNudgeEscalationType",
        filters={"parenttype": "VoiceNudgeConfig"},
        fields=["parent", "escalation_type"],
    )
    all_submission_types = frappe.db.get_all(
        "VoiceNudgeSubmissionType",
        filters={"parenttype": "VoiceNudgeConfig"},
        fields=["parent", "submission_type"],
    )

    # Group by parent (nudge_type)
    esc_by_nudge = {}
    for row in all_escalation_types:
        esc_by_nudge.setdefault(row.parent, []).append(row.escalation_type)

    sub_by_nudge = {}
    for row in all_submission_types:
        sub_by_nudge.setdefault(row.parent, []).append(row.submission_type)

    # Attach to config dicts
    for cfg in configs:
        cfg["_esc_types"] = esc_by_nudge.get(cfg.nudge_type, [])
        cfg["_sub_types"] = sub_by_nudge.get(cfg.nudge_type, [])

    _NUDGE_CACHE = configs
    _NUDGE_CACHE_AT = now
    return _NUDGE_CACHE


def _compute_situation(pe, student, glific_ctx):
    """Evaluate VoiceNudgeConfig records in priority order.

    Uses module-level cache (_load_nudge_cache) to avoid N×17 DB queries
    when computing situations for thousands of students during queue generation.
    Cache is warm for 5 minutes — a full 79K-student pass takes ~2-3 minutes.

    Returns the situation_label of the first matching active config.
    Falls back to 'unknown' if no config matches.
    """
    configs = _load_nudge_cache()

    for cfg in configs:
        if _matches_config(pe, student, glific_ctx, cfg):
            return cfg.situation_label or cfg.nudge_type

    return "unknown"


def _matches_config(pe, student, glific_ctx, cfg):
    """Return True if the student's current state satisfies all conditions."""
    submission_count = int(pe.submission_count or 0)
    weekly_done = int(pe.weekly_submission_done or 0)
    current_streak = int(pe.current_streak or 0)
    current_week = int(pe.current_week or 0)
    program_status = (pe.program_status or "").strip()
    flow_state = (pe.resolved_flow_state or "").strip()
    escalation_type = (pe.current_escalation_type or "").strip()
    submission_type = (pe.current_expected_submission_type or "").strip()

    # ── Hard conditions ─────────────────────────────────────────────────
    if cfg.require_program_paused and program_status != "paused":
        return False

    if cfg.require_grace_window and flow_state != "grace_waiting":
        return False

    if cfg.require_prior_submission and submission_count < 1:
        return False

    if cfg.require_no_weekly_submission and weekly_done != 0:
        return False

    if cfg.require_active_streak and current_streak < 1:
        return False

    min_week = int(cfg.min_week or 0)
    if min_week and current_week < min_week:
        return False

    min_sub = int(cfg.min_submission_count or 0)
    if min_sub and submission_count < min_sub:
        return False

    # ── Child table conditions (from cache, no extra queries) ───────────
    # _esc_types and _sub_types are pre-loaded by _load_nudge_cache().
    # Falls back to live query if cache doesn't have these keys (direct calls).
    allowed_escalation_types = cfg.get("_esc_types") if hasattr(cfg, "get") else (
        frappe.db.get_all("VoiceNudgeEscalationType",
            filters={"parent": cfg.nudge_type, "parenttype": "VoiceNudgeConfig"},
            pluck="escalation_type")
    )
    if allowed_escalation_types and escalation_type not in allowed_escalation_types:
        return False

    allowed_submission_types = cfg.get("_sub_types") if hasattr(cfg, "get") else (
        frappe.db.get_all("VoiceNudgeSubmissionType",
            filters={"parent": cfg.nudge_type, "parenttype": "VoiceNudgeConfig"},
            pluck="submission_type")
    )
    if allowed_submission_types and submission_type not in allowed_submission_types:
        return False

    # ── Special: so_close also checks ProgramEventLog ───────────────────
    if cfg.nudge_type == "so_close":
        return _check_mid_flow_drop(pe)

    # ── Special: celebration only fires on real milestones ────────────
    if cfg.nudge_type == "celebration":
        return _check_celebration_trigger(pe)

    return True


def _check_mid_flow_drop(pe):
    """N3 (so_close): True if flow was triggered this week without a submission."""
    current_week = int(pe.current_week or 0)
    if not current_week:
        return False

    # Check for a flow_triggered event this week with no submission_received
    flow_triggered = frappe.db.exists(
        "ProgramEventLog",
        {
            "enrollment": pe.name,
            "event_type": "flow_triggered",
            "week": current_week,
        },
    )
    if not flow_triggered:
        return False

    submission_received = frappe.db.exists(
        "ProgramEventLog",
        {
            "enrollment": pe.name,
            "event_type": "submission_received",
            "week": current_week,
        },
    )
    return not submission_received


# ════════════════════════════════════════════════════════════
# Helper functions
# ════════════════════════════════════════════════════════════


def _check_celebration_trigger(pe):
    """N8 (celebration): True only when student hit a real milestone.

    Fires when special_gems > 0 OR a week_completed event exists in
    ProgramEventLog. Without this guard, celebration (no conditions) matches
    every student and fires at priority 7, blocking first_submission at 8.
    """
    if int(pe.special_gems or 0) > 0:
        return True
    return bool(
        frappe.db.exists(
            "ProgramEventLog",
            {"enrollment": pe.name, "event_type": "week_completed"},
        )
    )


def _get_glific_context(student_name):
    """Fetch StudentGlificContext fields for this student. Returns {} if not found."""
    try:
        ctx_name = frappe.db.get_value(
            "StudentGlificContext",
            {"student": student_name},
            "name",
        )
        if not ctx_name:
            return {}

        return frappe.db.get_value(
            "StudentGlificContext",
            ctx_name,
            ["sp_submission_link", "last_flow_incomplete", "last_assignment_name",
             "last_inbound_message", "last_inbound_at"],
            as_dict=True,
        ) or {}
    except Exception:
        return {}


def _get_course_name(pe):
    """Resolve the course vertical name from ProgramEnrollment.course_level.

    CourseLevel.vertical is a Select field whose value IS the plain-text
    course name (e.g. "Coding", "Science", "Arts", "Financial Literacy").
    No join to Course Verticals needed.
    """
    try:
        course_level_name = pe.course_level
        if not course_level_name:
            return ""
        vertical = frappe.db.get_value("Course Level", course_level_name, "vertical")
        return (vertical or "").strip()
    except Exception:
        return ""


def _format_grace_date(dt):
    """Format grace_window_end_at to 'July 4' style. Returns '' if None."""
    if not dt:
        return ""
    try:
        from frappe.utils import get_datetime
        d = get_datetime(dt)
        if d.tzinfo is None:
            d = pytz.utc.localize(d)
        d_ist = d.astimezone(_IST)
        return d_ist.strftime("%-d %B")  # e.g. "4 July"
    except Exception:
        return str(dt)[:10] if dt else ""


def _get_grade_group(student):
    """Return 'parent_facilitated' for grades 1-6, 'student_direct' for 7-12."""
    try:
        grade = int(student.grade or 0)
        return "student_direct" if grade >= 7 else "parent_facilitated"
    except (TypeError, ValueError):
        return "parent_facilitated"


def _to_time(val):
    """Convert a Frappe Time field value (timedelta or time) to datetime.time."""
    from datetime import time as dt_time, timedelta
    if val is None:
        return None
    if isinstance(val, dt_time):
        return val
    try:
        if isinstance(val, timedelta):
            total_seconds = int(val.total_seconds())
            h, rem = divmod(total_seconds, 3600)
            m, s = divmod(rem, 60)
            return dt_time(hour=h % 24, minute=m, second=s)
        if isinstance(val, str):
            parts = val.split(":")
            return dt_time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    except Exception:
        pass
    return None
