"""
Shared utility helpers for the Summer Program module.
tap_lms/summer_program/utils.py
"""
import frappe
import hashlib
from datetime import timedelta
from frappe.utils import now_datetime, get_datetime


def resolve_student(identifier):
    """Resolve a student identifier (Student name, glific_id, or phone) to Student document name.

    Args:
        identifier: Student document name, Glific ID, or phone number.

    Returns:
        Student document name (str) or None if not found.
    """
    if not identifier:
        return None
    if frappe.db.exists("Student", identifier):
        return identifier
    # Try glific_id
    student = frappe.db.get_value("Student", {"glific_id": identifier}, "name")
    if student:
        return student
    # Try phone
    return frappe.db.get_value("Student", {"phone": str(identifier).strip()}, "name")


def staggered_action_time(base_time, pe_name, window_minutes=30):
    """
    Add deterministic jitter to a base time to prevent thundering herd.

    Uses a hash of the PE name to compute a stable offset within [0, window_minutes).
    Re-running for the same PE always produces the same offset, so retries
    don't scramble the schedule.

    Args:
        base_time: datetime — the base action time (e.g., batch start_date)
        pe_name: str — ProgramEnrollment document name (used as hash seed)
        window_minutes: int — jitter window in minutes (default 30)

    Returns:
        datetime with jitter added

    Example:
        With 100K students and window_minutes=30, students spread evenly across
        a 30-minute window (~55 students per second instead of 100K at once).
    """
    if not pe_name or window_minutes <= 0:
        return base_time

    # Deterministic hash → float in [0, 1)
    h = hashlib.md5(pe_name.encode()).hexdigest()
    fraction = int(h[:8], 16) / 0xFFFFFFFF

    # Convert to seconds within the window
    jitter_seconds = int(fraction * window_minutes * 60)

    return get_datetime(base_time) + timedelta(seconds=jitter_seconds)
