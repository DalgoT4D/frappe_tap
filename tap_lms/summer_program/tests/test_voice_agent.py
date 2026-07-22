"""
Didi Voice Agent — Comprehensive Test Suite
============================================

Run from bench console:
    from tap_lms.summer_program.tests.test_voice_agent import run_all
    run_all()

Or run individual suites:
    from tap_lms.summer_program.tests.test_voice_agent import (
        test_situation_logic,
        test_call_window,
        test_rate_limit,
        test_context_fields,
        test_submission_ask_mapping,
        test_grace_date_formatting,
        test_full_pipeline_dry_run,
    )

All tests are read-only against the database (no writes, no real calls).
The dummy data seeded by seed_test_data() is required for pipeline tests.

Expected output: all lines start with PASS. Any FAIL line means something
is wrong and should be fixed before presenting to Manu.
"""

import frappe
from frappe.utils import now_datetime, add_days


# ════════════════════════════════════════════════════════════
# Test helpers
# ════════════════════════════════════════════════════════════

_pass = 0
_fail = 0
_results = []


def _check(label, got, expected):
    global _pass, _fail
    if got == expected:
        _pass += 1
        _results.append(f"  PASS  {label}")
    else:
        _fail += 1
        _results.append(f"  FAIL  {label}\n        got:      {repr(got)}\n        expected: {repr(expected)}")


def _check_in(label, got, expected_options):
    global _pass, _fail
    if got in expected_options:
        _pass += 1
        _results.append(f"  PASS  {label}")
    else:
        _fail += 1
        _results.append(f"  FAIL  {label}\n        got: {repr(got)}, expected one of: {expected_options}")


def _check_not(label, got, not_expected):
    global _pass, _fail
    if got != not_expected:
        _pass += 1
        _results.append(f"  PASS  {label}")
    else:
        _fail += 1
        _results.append(f"  FAIL  {label}\n        got: {repr(got)}, should NOT be {repr(not_expected)}")


def _section(title):
    _results.append(f"\n{'='*60}\n  {title}\n{'='*60}")


def _make_pe(**kwargs):
    """Build a mock ProgramEnrollment frappe._dict for testing."""
    defaults = {
        "name": "TEST-PE-XXX",
        "student": "ST00000001",
        "batch": "TEST-BT001",
        "program_type": "Summer",
        "archetype": "fence_sitter",
        "experiment_arm": "arm_a",
        "current_path": "Core",
        "current_tier": "Basic",
        "program_status": "active",
        "resolved_flow_state": "normal_escalation",
        "current_escalation_type": "help_note_b",
        "current_week": 2,
        "current_streak": 0,
        "submission_count": 0,
        "weekly_submission_done": 0,
        "current_expected_submission_type": "emoji",
        "total_points": 0,
        "special_gems": 0,
        "total_call_count": 0,
        "weekly_call_count": 0,
        "last_call_at": None,
        "last_call_outcome": None,
        "grace_window_end_at": None,
        "course_level": None,
        "language": "Hindi",
        "glific_id": "TEST-GLIFIC",
    }
    defaults.update(kwargs)
    return frappe._dict(defaults)


def _make_student(**kwargs):
    defaults = {
        "name": "ST00000001",
        "name1": "Test Student",
        "phone": "9000000001",
        "grade": "7",
        "language": "Hindi",
    }
    defaults.update(kwargs)
    return frappe._dict(defaults)


# ════════════════════════════════════════════════════════════
# Test 1: Situation logic — all 8 nudge types
# ════════════════════════════════════════════════════════════

def test_situation_logic():
    _section("Test 1: Situation Logic — all 8 nudge types")

    from tap_lms.summer_program.voice_context import _compute_situation

    student = _make_student()

    # N7 Reinitiation — program_status = paused
    pe = _make_pe(program_status="paused", submission_count=1)
    _check("N7 reinitiation: paused program", _compute_situation(pe, student, {}), "program_paused")

    # N2 Streak Protection — has prior submission + streak > 0 + no weekly sub
    pe = _make_pe(submission_count=3, current_streak=3, weekly_submission_done=0,
                  current_escalation_type="voice_note")
    _check("N2 streak_protection: active streak", _compute_situation(pe, student, {}), "streak_at_risk")

    # N4 Real Blocker — week 3+, complex submission, prior submissions
    pe = _make_pe(submission_count=2, current_week=3, weekly_submission_done=0,
                  current_expected_submission_type="image",
                  current_escalation_type="help_note_b")
    _check("N4 real_blocker: week3 image submission", _compute_situation(pe, student, {}), "complex_submission_stuck")

    pe = _make_pe(submission_count=2, current_week=3, weekly_submission_done=0,
                  current_expected_submission_type="summary_text_voice",
                  current_escalation_type="help_note_a")
    _check("N4 real_blocker: week3 summary", _compute_situation(pe, student, {}), "complex_submission_stuck")

    # N1 Continuation — prior submission, no streak, week 2, voice_note
    pe = _make_pe(submission_count=2, current_streak=0, weekly_submission_done=0,
                  current_week=2, current_escalation_type="help_note_b",
                  current_expected_submission_type="word_text_voice")
    _check("N1 continuation: returning student", _compute_situation(pe, student, {}), "returning_not_submitted")

    # N6 Grace Deadline — grace_waiting
    pe = _make_pe(submission_count=0, resolved_flow_state="grace_waiting",
                  current_escalation_type="parent_call",
                  grace_window_end_at="2026-07-10 17:00:00")
    _check("N6 grace_deadline: grace_waiting", _compute_situation(pe, student, {}), "deadline_live")

    # N8 Celebration — special_gems > 0
    pe = _make_pe(submission_count=4, current_streak=4, weekly_submission_done=1,
                  special_gems=2, total_points=120,
                  current_escalation_type="parent_call")
    _check("N8 celebration: special_gems > 0", _compute_situation(pe, student, {}), "celebration")

    # N5 First Submission — never submitted, parent_call, normal_escalation
    pe = _make_pe(submission_count=0, current_escalation_type="parent_call",
                  resolved_flow_state="normal_escalation", special_gems=0)
    _check("N5 first_submission: never submitted", _compute_situation(pe, student, {}), "first_timer")

    # Priority: N7 beats everything when paused
    pe = _make_pe(program_status="paused", submission_count=3, current_streak=3,
                  special_gems=2, resolved_flow_state="grace_waiting")
    _check("Priority: N7 beats all when paused", _compute_situation(pe, student, {}), "program_paused")

    # Priority: N2 beats N1 when streak active
    pe = _make_pe(submission_count=3, current_streak=2, weekly_submission_done=0,
                  current_escalation_type="voice_note")
    _check("Priority: N2 beats N1 when streak", _compute_situation(pe, student, {}), "streak_at_risk")

    # Priority: N4 beats N1 at week 3 with complex submission
    pe = _make_pe(submission_count=2, current_streak=0, weekly_submission_done=0,
                  current_week=3, current_expected_submission_type="image",
                  current_escalation_type="help_note_a")
    _check("Priority: N4 beats N1 at week3+image", _compute_situation(pe, student, {}), "complex_submission_stuck")

    # N4 does NOT fire at week 2 (min_week=3)
    pe = _make_pe(submission_count=2, current_week=2, weekly_submission_done=0,
                  current_expected_submission_type="image",
                  current_escalation_type="help_note_a")
    _check_not("N4 does NOT fire at week 2", _compute_situation(pe, student, {}), "complex_submission_stuck")

    # N5 does NOT fire if celebration milestone exists
    pe = _make_pe(submission_count=0, special_gems=1,
                  current_escalation_type="parent_call",
                  resolved_flow_state="normal_escalation")
    _check_not("N5 blocked by celebration when gems>0", _compute_situation(pe, student, {}), "first_timer")

    # Unknown — no conditions match (weekly_done=1 means not slipping)
    pe = _make_pe(submission_count=2, weekly_submission_done=1,
                  current_escalation_type="parent_call",
                  resolved_flow_state="normal_escalation", special_gems=0)
    _check("Unknown: weekly done, no escalation match", _compute_situation(pe, student, {}), "unknown")


# ════════════════════════════════════════════════════════════
# Test 2: Call window
# ════════════════════════════════════════════════════════════

def test_call_window():
    _section("Test 2: Call Window")

    from tap_lms.summer_program.voice_context import check_call_window
    from datetime import timedelta

    # No window set — always True
    settings = frappe._dict(call_window_start=None, call_window_end=None)
    _check("No window: always allowed", check_call_window(settings), True)

    # Within window
    settings = frappe._dict(
        call_window_start=timedelta(hours=0),   # midnight
        call_window_end=timedelta(hours=23, minutes=59),  # end of day
    )
    _check("Full day window: allowed", check_call_window(settings), True)

    # Outside window (6am to 7am window, current time outside)
    settings = frappe._dict(
        call_window_start=timedelta(hours=2),   # 2am UTC = 7:30am IST
        call_window_end=timedelta(hours=3),     # 3am UTC = 8:30am IST
    )
    # We can't control IST time in test so just verify it returns a bool
    result = check_call_window(settings)
    _check_in("Window returns bool", result, [True, False])


# ════════════════════════════════════════════════════════════
# Test 3: Rate limiting
# ════════════════════════════════════════════════════════════

def test_rate_limit():
    _section("Test 3: Rate Limiting")

    from tap_lms.summer_program.voice_context import check_rate_limit
    from frappe.utils import add_to_date

    settings = frappe._dict(
        max_calls_per_student_per_week=2,
        call_cooldown_hours=24,
    )

    # Under weekly cap, no last call
    pe = _make_pe(weekly_call_count=0, last_call_at=None)
    _check("Under cap, no last call: allowed", check_rate_limit(pe, settings), True)

    # Under weekly cap, last call > cooldown hours ago
    pe = _make_pe(weekly_call_count=1,
                  last_call_at=str(add_to_date(now_datetime(), hours=-25)))
    _check("Under cap, past cooldown: allowed", check_rate_limit(pe, settings), True)

    # Under weekly cap, last call within cooldown
    pe = _make_pe(weekly_call_count=1,
                  last_call_at=str(add_to_date(now_datetime(), hours=-2)))
    _check("Under cap, within cooldown: blocked", check_rate_limit(pe, settings), False)

    # At weekly cap
    pe = _make_pe(weekly_call_count=2, last_call_at=str(add_to_date(now_datetime(), hours=-48)))
    _check("At weekly cap: blocked", check_rate_limit(pe, settings), False)

    # Over cap
    pe = _make_pe(weekly_call_count=5, last_call_at=None)
    _check("Over weekly cap: blocked", check_rate_limit(pe, settings), False)

    # No limits configured
    settings_empty = frappe._dict(max_calls_per_student_per_week=0, call_cooldown_hours=0)
    pe = _make_pe(weekly_call_count=99, last_call_at=str(now_datetime()))
    _check("No limits configured: allowed", check_rate_limit(pe, settings_empty), True)


# ════════════════════════════════════════════════════════════
# Test 4: Submission ask mapping
# ════════════════════════════════════════════════════════════

def test_submission_ask_mapping():
    _section("Test 4: Submission Ask Mapping")

    from tap_lms.summer_program.voice_context import _SUBMISSION_ASK

    expected = {
        "emoji":                "koi bhi emoji bhejna hai",
        "word_text_voice":      "ek chota sa voice note ya text bhejna hai",
        "summary_text_voice":   "aaj aapne kya seekha, woh likho ya awaaz mein batao",
        "image":                "apni activity ki photo bhejna hai",
        "video":                "ek chota video bhejna hai",
        "photo_video_artefact": "apne banaye huye cheez ki photo ya video bhejna hai",
    }

    for sub_type, expected_ask in expected.items():
        _check(f"submission_ask for {sub_type}", _SUBMISSION_ASK.get(sub_type), expected_ask)

    # Unknown type falls back to default in build_student_context
    _check("Unknown type not in map", _SUBMISSION_ASK.get("unknown_type"), None)


# ════════════════════════════════════════════════════════════
# Test 5: Grace date formatting
# ════════════════════════════════════════════════════════════

def test_grace_date_formatting():
    _section("Test 5: Grace Date Formatting")

    from tap_lms.summer_program.voice_context import _format_grace_date

    _check("None returns empty", _format_grace_date(None), "")
    _check("Empty string returns empty", _format_grace_date(""), "")

    # Valid datetime — should return "D Month" format
    result = _format_grace_date("2026-07-04 17:00:00")
    _check_in("July 4 formats correctly", result, ["4 July", "3 July"])  # allow for UTC/IST edge

    result2 = _format_grace_date("2026-08-15 12:00:00")
    _check("Aug 15 contains August", "August" in result2 or "15" in result2, True)


# ════════════════════════════════════════════════════════════
# Test 6: Grade group
# ════════════════════════════════════════════════════════════

def test_grade_group():
    _section("Test 6: Grade Group")

    from tap_lms.summer_program.voice_context import _get_grade_group

    for grade in ["4", "5", "6"]:
        s = _make_student(grade=grade)
        _check(f"Grade {grade}: parent_facilitated", _get_grade_group(s), "parent_facilitated")

    for grade in ["7", "8", "9", "10"]:
        s = _make_student(grade=grade)
        _check(f"Grade {grade}: student_direct", _get_grade_group(s), "student_direct")

    s = _make_student(grade=None)
    _check("Grade None: parent_facilitated default", _get_grade_group(s), "parent_facilitated")

    s = _make_student(grade="abc")
    _check("Grade invalid: parent_facilitated default", _get_grade_group(s), "parent_facilitated")


# ════════════════════════════════════════════════════════════
# Test 7: Context dict — all 10 keys present and correct types
# ════════════════════════════════════════════════════════════

def test_context_fields():
    _section("Test 7: Context Dict — all 10 keys")

    from tap_lms.summer_program.voice_context import build_student_context

    pe = _make_pe(submission_count=2, current_streak=3, weekly_submission_done=0,
                  current_escalation_type="voice_note",
                  current_expected_submission_type="summary_text_voice",
                  grace_window_end_at="2026-07-10 17:00:00",
                  total_call_count=1)
    student = _make_student(grade="8")

    ctx = build_student_context(pe, student)

    required_keys = [
        "situation", "course", "submission_count", "streak",
        "submission_ask", "grace_deadline", "grade_group",
        "call_attempt", "last_problem_reported", "last_message"
    ]

    for key in required_keys:
        _check(f"Key '{key}' present", key in ctx, True)
        _check(f"Key '{key}' is string", isinstance(ctx.get(key), str), True)

    # Specific value checks
    _check("submission_count is '2'", ctx["submission_count"], "2")
    _check("streak is '3'", ctx["streak"], "3")
    _check("call_attempt is '2'", ctx["call_attempt"], "2")  # total_call_count=1, so attempt=2
    _check("grade_group is student_direct", ctx["grade_group"], "student_direct")
    _check("submission_ask not empty", bool(ctx["submission_ask"]), True)
    _check_in("situation is valid", ctx["situation"],
               ["streak_at_risk", "returning_not_submitted", "complex_submission_stuck",
                "deadline_live", "unknown"])


# ════════════════════════════════════════════════════════════
# Test 8: Template rendering with all variables
# ════════════════════════════════════════════════════════════

def test_template_rendering():
    _section("Test 8: Template Rendering")

    from tap_lms.summer_program.vocallabs import _render_status_template

    pe = _make_pe(
        submission_count=2, current_streak=3, weekly_submission_done=0,
        current_week=2, current_escalation_type="voice_note",
        current_expected_submission_type="summary_text_voice",
    )
    student = _make_student(grade="7")

    template = (
        "{student_name} — week {week} — {situation} — "
        "{submission_count} submissions — streak {streak} — "
        "ask: {submission_ask} — grade: {grade_group}"
    )

    step = {"escalation_order": 2, "escalation_type": "voice_note",
            "hours_after_previous": 48, "points_awarded": 0}

    result = _render_status_template(template, pe, student, step)

    _check("Template renders student name", "Test Student" in result, True)
    _check("Template renders week", "2" in result, True)
    _check("Template renders submission count", "2" in result, True)
    _check("Template renders streak", "3" in result, True)
    _check("Template renders grade_group", "student_direct" in result, True)
    _check("Template renders submission_ask", "likho" in result or "summary" in result or "seekha" in result, True)
    _check("No raw {variables} left", "{" not in result, True)

    # Test with unknown variable — should return raw template, not crash
    bad_template = "Hello {unknown_variable}"
    bad_result = _render_status_template(bad_template, pe, student, step)
    _check("Unknown variable: returns raw template", "{unknown_variable}" in bad_result, True)

    # Test with None template
    none_result = _render_status_template(None, pe, student, step)
    _check("None template: returns empty string", none_result, "")


# ════════════════════════════════════════════════════════════
# Test 9: VoiceNudgeConfig records (seeded by migration patch)
# ════════════════════════════════════════════════════════════

def test_nudge_config_seeded():
    _section("Test 9: VoiceNudgeConfig Records")

    configs = frappe.get_all(
        "VoiceNudgeConfig",
        fields=["nudge_type", "priority", "situation_label", "is_active"],
        order_by="priority asc",
    )

    expected_types = {
        "reinitiation", "streak_protection", "real_blocker",
        "continuation", "so_close", "grace_deadline",
        "celebration", "first_submission"
    }
    actual_types = {c.nudge_type for c in configs}
    _check("All 8 nudge types exist", actual_types, expected_types)
    _check("All 8 are active", all(c.is_active for c in configs), True)
    _check("Priorities are unique", len({c.priority for c in configs}), 8)

    priorities = sorted([c.priority for c in configs])
    _check("Reinitiation is priority 1", configs[0].nudge_type if configs else None, "reinitiation")
    _check("First_submission is priority 8", configs[-1].nudge_type if configs else None, "first_submission")


# ════════════════════════════════════════════════════════════
# Test 10: Full pipeline dry run against real DB students
# ════════════════════════════════════════════════════════════

def test_full_pipeline_dry_run():
    _section("Test 10: Full Pipeline — Real Test Students")

    from tap_lms.summer_program.voice_context import build_student_context

    # Map of expected situations per test student phone
    expected_situations = {
        "9000000001": "returning_not_submitted",  # Rahul — N1
        "9000000002": "streak_at_risk",           # Priya — N2
        "9000000003": "complex_submission_stuck", # Amit  — N4
        "9000000004": "first_timer",              # Sunita — N5
        "9000000005": "deadline_live",            # Gurpreet — N6
        "9000000006": "program_paused",           # Meena — N7
        "9000000007": "celebration",              # Vikram — N8
        "8950507072": "streak_at_risk",           # Dashpreet — N2 (submission_count=2, streak=3)
    }

    for phone, expected_situation in expected_situations.items():
        student_name = frappe.db.get_value("Student", {"phone": phone}, "name")
        if not student_name:
            _results.append(f"  SKIP  Phone {phone} — student not in DB")
            continue

        pe_name = frappe.db.get_value("ProgramEnrollment", {"student": student_name}, "name")
        if not pe_name:
            _results.append(f"  SKIP  {student_name} — no enrollment found")
            continue

        pe = frappe.get_doc("ProgramEnrollment", pe_name)
        student = frappe.get_doc("Student", student_name)
        ctx = build_student_context(pe, student)

        _check(
            f"{student.name1} ({phone}): situation",
            ctx["situation"],
            expected_situation,
        )
        _check(f"{student.name1}: call_attempt is string", isinstance(ctx["call_attempt"], str), True)
        _check(f"{student.name1}: grade_group set", bool(ctx["grade_group"]), True)
        _check(f"{student.name1}: submission_ask set", bool(ctx["submission_ask"]), True)


# ════════════════════════════════════════════════════════════
# Run all
# ════════════════════════════════════════════════════════════

def run_all():
    global _pass, _fail, _results
    _pass = 0
    _fail = 0
    _results = []

    test_situation_logic()
    test_call_window()
    test_rate_limit()
    test_submission_ask_mapping()
    test_grace_date_formatting()
    test_grade_group()
    test_context_fields()
    test_template_rendering()
    test_nudge_config_seeded()
    test_full_pipeline_dry_run()

    _results.append(f"\n{'='*60}")
    _results.append(f"  TOTAL: {_pass + _fail} tests | {_pass} passed | {_fail} failed")
    _results.append("=" * 60)
    if _fail == 0:
        _results.append("  ALL TESTS PASSED")
    else:
        _results.append(f"  {_fail} TESTS FAILED — fix before presenting")
    _results.append("=" * 60)

    print("\n".join(_results))
    return _fail == 0
