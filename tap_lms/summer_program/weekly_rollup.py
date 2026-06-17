"""Pure weekly rollup helpers for Summer Program gamification.

This module intentionally has no Frappe imports so the core points math can be
tested with plain local Python.
"""


def _field(source, name, default=0):
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _int_field(source, name):
    return int(_field(source, name, 0) or 0)


def calculate_week_advance_rollup(pe):
    """Return cumulative point/streak/gem updates for week advance.

    Pre-CR-011 this added weekly_* to total_* (the "lazy rollup" design):
    per-event handlers only bumped weekly_* columns, and T14 rolled them
    into total_*. Mid-week, totals were incoherent — a student who earned
    quiz points saw weekly_quiz_points=N but total_quiz_points=0 until the
    week advance.

    Post-CR-011 (2026-05-25) totals are **eager**. The per-event handlers
    (`quiz_points.py`, `activity_points.py`, `feedback_consumer_hook.py`)
    bump total_* AND weekly_* in the same atomic UPDATE, so totals stay
    coherent at all times. T14 therefore only needs to RESET weekly_* —
    the totals returned here are pass-through (unchanged from input). The
    invariant `stream_sum == total_points` holds at ALL TIMES under
    CR-011, not just post-T14.

    The streak/gem penalty branch (weekly_video_done && !weekly_submission_done)
    is independent of the points design and survives unchanged.
    """
    streak_update = _int_field(pe, "current_streak")
    gems_update = _int_field(pe, "special_gems")
    if bool(_field(pe, "weekly_video_done", 0)) and not bool(_field(pe, "weekly_submission_done", 0)):
        streak_update = 0
        gems_update = max(0, gems_update - 1)

    return {
        "current_streak": streak_update,
        "special_gems": gems_update,
        # CR-011: total_* fields are now updated eagerly per-event (see
        # quiz_points.py, activity_points.py, feedback_consumer_hook.py).
        # T14's rollup no longer adds weekly→total — it just resets weekly_*.
        # Returning the existing total values unchanged signals to the caller
        # that T14 should NOT mutate them.
        "total_activity_points": _int_field(pe, "total_activity_points"),
        "total_submission_points": _int_field(pe, "total_submission_points"),
        "total_quiz_points": _int_field(pe, "total_quiz_points"),
        "total_points": _int_field(pe, "total_points"),
    }
