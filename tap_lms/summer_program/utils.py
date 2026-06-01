"""
Shared utility helpers for the Summer Program module.
tap_lms/summer_program/utils.py
"""
import frappe
import functools
import hashlib
from datetime import timedelta
from frappe.utils import now_datetime, get_datetime


def normalize_unicode_surrogates(value):
    """Convert escaped UTF-16 surrogate pairs into valid Unicode."""
    if not isinstance(value, str):
        return value

    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value

    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


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


def get_student_display_name(student):
    """Return the student's display name for personalization (Glific student_name contact field).

    The Student doctype canonical name field is `name1` (label "Name").
    Historically some code paths read `student.student_name`, which Frappe silently
    resolves to None because the field does not exist — leading to empty-string pushes
    to Glific and broken personalization in WhatsApp messages.

    This helper centralizes the read so we have ONE place to update if the doctype is
    ever normalized to a proper `student_name` / `first_name` / `last_name` split.

    Args:
        student: Student document, dict, or anything with attribute/dict access.

    Returns:
        str — student's display name, or "" if nothing usable is set.
    """
    if student is None:
        return ""

    # Attribute access (Frappe Document) — try in priority order.
    # Order: name1 (canonical) → student_name (future-proof if field is added) →
    # first_name (some legacy paths) → "".
    for attr in ("name1", "student_name", "first_name"):
        value = _safe_get(student, attr)
        if value:
            return str(value).strip()

    return ""


def _safe_get(obj, attr):
    """Read `attr` from `obj` whether it's a dict, Frappe Document, or plain object."""
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


def glific_response(fn):
    """Decorator that writes a whitelisted endpoint's return dict directly to
    `frappe.local.response`, bypassing the Frappe `message` envelope.

    Per docs/api-standard-glific.md Rule 1: Glific consumes flat top-level
    keys (`@results.webhook.<field>`), not the `@results.webhook.message.<field>`
    pattern Frappe defaults to. Apply this decorator INSIDE `@frappe.whitelist`:

        @frappe.whitelist(allow_guest=False)
        @glific_response
        def my_endpoint(...):
            return {"success": True, "status": "ok", "field": "value"}

    The endpoint keeps its natural `return {dict}` style. The decorator
    intercepts the return value, writes it to `frappe.local.response`, and
    returns None — Frappe then sets `response.message = None` (a single null
    field Glific ignores). All other keys are at the top level.

    If the function returns None (or falsy), the decorator is a no-op — the
    function is expected to have written to `frappe.local.response` directly.

    Note: `frappe` is the module-level import at the top of utils.py — NOT
    re-imported inside this function. That matters for tests: patches against
    `tap_lms.summer_program.utils.frappe` need to actually shadow the binding
    the wrapper uses, and module-attribute lookup (via `utils.__dict__`) IS
    what `unittest.mock.patch` replaces. A local `import frappe` here would
    create a closure cell that the patch couldn't reach.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        payload = fn(*args, **kwargs)
        if payload:
            frappe.local.response.update(payload)
        # Return None — Frappe sets response.message = None (Glific ignores)

    return wrapper


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
