"""
Shared utility helpers for the Summer Program module.
tap_lms/summer_program/utils.py
"""
import frappe
import functools
import hashlib
import time
import random
import psycopg2.errors as pg_errors
from datetime import timedelta
from frappe.utils import now_datetime, get_datetime


# ── CR-024: Glific placeholder detection ─────────────────────────────────────
#
# Production incident 2026-06-04→05: Glific timed out on SP webhook calls,
# substituted nulls, and then passed LITERAL "@results.X.Y" placeholder strings
# BACK into downstream SP API calls. These look like valid string parameters so
# they bypass absence checks and make it deep enough into the endpoint to cause
# "Error getting assignment context" cascade errors (81 occurrences).
#
# Layer 3 (this): detect the placeholder at endpoint entry, return a clean
# status so the Glific flow can retry instead of crashing a lookup.
# Architecture review: COMPATIBLE (ADR-006). Zero behavior change for healthy
# calls — the guard only rejects already-broken input.

_PLACEHOLDER_PREFIX = "@results."


def is_unresolved_glific_placeholder(value):
    """Return True if `value` is Glific's literal unresolved template token.

    Glific substitutes real values into webhook parameters at flow execution
    time. When an upstream webhook times out (as in the 2026-06-04 incident),
    Glific fails to substitute and instead passes the raw token string —
    e.g. '@results.content_details.youtube_url' — as the parameter value.

    Detection rule: starts with "@results." AND has a second dot after the
    prefix (e.g. "@results.foo.bar" → True; "@results.foo" → False because
    that's only one path segment and could be a legitimate Glific variable
    reference that never contained a dot, which is an unexpected shape).

    Returns False for non-strings, None, and empty strings so callers can
    use this as a direct replacement for identity checks.

    Examples:
        is_unresolved_glific_placeholder("@results.content_details.youtube_url")
        → True
        is_unresolved_glific_placeholder("@results.quiz_response.option_a")
        → True
        is_unresolved_glific_placeholder("@results.foo")
        → False  (no second dot — ambiguous, treated as safe)
        is_unresolved_glific_placeholder("ReadingFluency-Literacy-C0001")
        → False  (normal course_level value)
        is_unresolved_glific_placeholder(None)
        → False
    """
    if not isinstance(value, str):
        return False
    if not value.startswith(_PLACEHOLDER_PREFIX):
        return False
    # Require a second dot AFTER the prefix, e.g. "@results.X.Y" (not "@results.X")
    return "." in value[len(_PLACEHOLDER_PREFIX):]


def check_glific_placeholders(params, api_name, student_id=None):
    """Check a sequence of (param_name, value) pairs for unresolved Glific
    placeholders and return a flat api-standard error dict on the first hit.

    Returns None when all params are clean (no placeholder detected) — the
    caller continues normally.  Returns the error dict on the first hit — the
    caller should return it immediately without further processing.

    The ProgramEventLog write is attempted (rollback-safe per L-077 / L-030):
    if the PE is not yet resolved at entry-guard time, we log via frappe.log_error
    (DB-independent, no FK required) instead of inserting a ProgramEventLog row.
    If even that fails, the error is swallowed so the clean status is always
    returned to Glific.

    Usage:
        hit = check_glific_placeholders(
            [("student_id", student_id), ("course_level", course_level)],
            api_name="get_weekly_content",
            student_id=student_id,
        )
        if hit:
            return hit

    Args:
        params: iterable of (str param_name, any value) — only str values are
                ever flagged; non-str values are skipped (absence checks live
                elsewhere).
        api_name: endpoint name for the log (snake_case, e.g. "get_content_details").
        student_id: optional raw student identifier for log context (truncated
                    to 50 chars so an unresolved placeholder itself can't bloat
                    the log record).

    Returns:
        None — all params clean.
        dict  — flat api-standard error: success=False,
                status="upstream_resolution_failed",
                error_detail="..." (safe, no Glific internal detail leaked).
    """
    for param_name, value in params:
        if not is_unresolved_glific_placeholder(value):
            continue

        # Hit — attempt a ProgramEventLog-level record. We don't have a PE at
        # entry guard time, so fall back to frappe.log_error (no FK needed,
        # DB-independent title column — L-077 / L-030 rollback-safe pattern).
        safe_sid = str(student_id)[:50] if student_id else ""
        details_dict = {
            "api": api_name,
            "param": param_name,
            "value": str(value)[:200],
            "student_id": safe_sid,
        }
        try:
            frappe.db.rollback()
        except Exception:
            pass
        try:
            frappe.log_error(
                title="SP Glific placeholder",
                message=(
                    f"glific_unresolved_placeholder | api={api_name} "
                    f"param={param_name} "
                    f"value={str(value)[:200]} "
                    f"student_id={safe_sid}"
                ),
            )
        except Exception:
            try:
                frappe.logger().warning(
                    f"SP Glific placeholder (log_error double-fault): "
                    f"api={api_name} param={param_name} value={str(value)[:200]}"
                )
            except Exception:
                pass

        return {
            "success": False,
            "status": "upstream_resolution_failed",
            "error_detail": (
                "Upstream Glific webhook did not resolve a placeholder; "
                "flow should retry."
            ),
        }

    return None


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


# ── Serialization-retry + safe-error plumbing (BR-001 / BR-003 / L-071) ──
#
# Glific webhooks have a hard ~4s budget. Two failure modes bite the SP APIs
# under concurrent webhook dispatch:
#   1. SerializationFailure / DeadlockDetected — transient PG write conflicts on
#      a shared row (BPR counter, PE rollup counters, tabSeries.current for
#      counter-autoname inserts). Retry with bounded backoff + a time budget
#      (stay well under 4s). L-071.
#   2. L-030 poisoned-txn cascade — once any statement in a txn raises, every
#      later statement (including frappe.log_error's own INSERT) raises
#      InFailedSqlTransaction until rollback(). The naive
#      `except: frappe.log_error(...)` handler therefore double-faults and Glific
#      gets a 400 with a corrupted traceback. safe_sp_api_error_response()
#      rolls back FIRST, then logs in a fresh txn, then returns a flat error.


def _commit_with_serialization_retry(do_write, *, context="", max_retries=3,
                                     budget_ms=2500):
    """Run a DB write + commit, retrying on transient Postgres write conflicts.

    `do_write` is a zero-arg callable that issues the UPDATE (but NOT the
    commit — this helper owns commit/rollback so each attempt is its own
    transaction). On `psycopg2.errors.SerializationFailure` / `DeadlockDetected`
    (L-071) we rollback and retry with exponential backoff + jitter
    (50/100/200ms +0-20ms). Any other exception is non-transient and propagates
    immediately after a rollback.

    `budget_ms` caps total wall time so a webhook stays under Glific's ~4s
    deadline; exceeding it raises TimeoutError. On retry exhaustion we log_error
    with `context` then re-raise the last serialization error — callers (RQ jobs
    and Glific endpoints) MUST see the failure (L-056), never a silent swallow.

    Single source of truth for the SP module (moved here from enrollment.py).
    """
    backoffs = (0.05, 0.10, 0.20)
    last_exc = None
    start = time.time()
    for attempt in range(max_retries + 1):
        if (time.time() - start) * 1000 > budget_ms:
            raise TimeoutError(
                f"_commit_with_serialization_retry: budget {budget_ms}ms "
                f"exhausted (context={context})"
            )
        try:
            do_write()
            frappe.db.commit()
            return
        except (pg_errors.SerializationFailure, pg_errors.DeadlockDetected) as e:
            last_exc = e
            frappe.db.rollback()
            if attempt < max_retries:
                delay = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                time.sleep(delay + random.uniform(0, 0.02))
                continue
        except Exception:
            # Non-transient — do not retry; surface immediately.
            frappe.db.rollback()
            raise

    try:
        frappe.log_error(
            message=(
                f"Counter UPDATE: SerializationFailure exhausted after "
                f"{max_retries + 1} attempts. context={context} "
                f"last_exception={last_exc!r}"
            ),
            title="SP counter SerializationFailure exhausted",
        )
    except Exception:
        pass
    raise last_exc


def _insert_with_serialization_retry(doc, max_retries=3, budget_ms=2500):
    """`doc.insert()` with bounded retry on PG SerializationFailure / Deadlock.

    Triggers: counter-autoname doctypes (`format:...{####}`) lock
    `tabSeries.current FOR UPDATE`, so concurrent inserts for the same series
    prefix serialization-fail under Glific webhook concurrency (BR-003 incident:
    StudentStageProgress). Backoffs 50/100/200ms + jitter; `budget_ms` caps wall
    time so we stay under Glific's ~4s deadline (TimeoutError on exceed).

    On exhaustion: rollback, log_error with doc metadata, re-raise so the caller
    sees the failure (L-056). The complementary fix is hash autoname on the hot
    doctypes (eliminates tabSeries contention at the source); this retry covers
    the residual / not-yet-converted cases.
    """
    backoffs = (0.05, 0.10, 0.20)
    last_exc = None
    start = time.time()
    for attempt in range(max_retries + 1):
        if (time.time() - start) * 1000 > budget_ms:
            raise TimeoutError(
                f"_insert_with_serialization_retry: budget {budget_ms}ms "
                f"exhausted (doctype={getattr(doc, 'doctype', '?')})"
            )
        try:
            doc.insert(ignore_permissions=True)
            return
        except (pg_errors.SerializationFailure, pg_errors.DeadlockDetected) as e:
            last_exc = e
            try:
                frappe.db.rollback()
            except Exception:
                pass
            if attempt < max_retries:
                delay = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
                time.sleep(delay + random.uniform(0, 0.02))
                continue
        except Exception:
            try:
                frappe.db.rollback()
            except Exception:
                pass
            raise

    # Already rolled back inside the loop on the last failed attempt; just log.
    try:
        frappe.log_error(
            title="SP insert: SerializationFailure exhausted",
            message=(
                f"doctype={getattr(doc, 'doctype', '?')} "
                f"attempts={max_retries + 1} "
                f"last_exception={last_exc!r}"
            ),
        )
    except Exception:
        try:
            frappe.logger().error(
                f"_insert_with_serialization_retry double-fault: {last_exc}"
            )
        except Exception:
            pass
    raise last_exc


def safe_sp_api_error_response(exc, endpoint_name, student_id=None, extras=None):
    """Standard error response for SP whitelisted endpoints (Glific-consumed).

    Fixes the L-030 poisoned-txn cascade that turned a handled error into a
    Glific 400 (BR-003): the naive `except: frappe.log_error(...)` ran log_error's
    INSERT on an already-aborted txn → InFailedSqlTransaction → handler died.

    Order (all required):
      1. frappe.db.rollback() FIRST — clear the poisoned txn so the subsequent
         log INSERT can run.
      2. log_error inside a nested try so a double-fault still returns a response.
      3. Write a docs/api-standard-glific.md-compliant FLAT response to
         frappe.local.response (HTTP 200, success=false, status=error,
         snake_case scalars) and return None. The `@glific_response` wrapper
         no-ops on a None return, so this works whether or not the endpoint is
         decorated.

    Caller pattern:
        try:
            ... main work ...
            return {"success": True, "status": "ok", ...}
        except Exception as e:
            return safe_sp_api_error_response(e, "get_next_content",
                                              student_id=student_id)
    """
    try:
        frappe.db.rollback()
    except Exception:
        pass

    msg = f"{endpoint_name} error: {type(exc).__name__}: {str(exc)[:300]}"
    if student_id:
        msg += f" | student_id={student_id}"
    if extras:
        msg += f" | extras={extras}"
    try:
        frappe.log_error(title=endpoint_name, message=msg)
    except Exception:
        try:
            frappe.logger().error(f"{endpoint_name} (double-fault): {exc}")
        except Exception:
            pass

    # Flat, snake_case, scalar-only (docs/api-standard-glific.md). No raw
    # exception text leaks to Glific — detail is in the server log above.
    error_payload = {
        "success": False,
        "status": "error",
        "user_message": "Something went wrong. Please try again shortly.",
    }
    # Write to frappe.local.response so endpoints WITHOUT @glific_response (and
    # the sp_safe_endpoint decorator path) still emit FLAT top-level keys.
    try:
        frappe.local.response.update(error_payload)
    except Exception:
        pass
    # ALSO return the dict: @glific_response flattens a returned dict, direct
    # callers / tests get the api-standard error shape, and a body's
    # `return safe_sp_api_error_response(...)` keeps its natural dict-return style.
    return error_payload


def sp_safe_endpoint(endpoint_name=None):
    """Decorator: catch any unhandled exception in a Glific-facing endpoint and
    return a flat api-standard error (rollback-first) instead of a 400.

    For endpoints that DON'T already have an internal try/except wrapping their
    body (e.g. flow_callback.update_flow_status). Endpoints that already wrap
    their body should call safe_sp_api_error_response in the except directly.

    Stack OUTSIDE @glific_response, INSIDE @frappe.whitelist:

        @frappe.whitelist(allow_guest=True)
        @sp_safe_endpoint("update_flow_status")
        def update_flow_status(student_id, ...): ...
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                sid = kwargs.get("student_id")
                if sid is None and args:
                    sid = args[0]
                # Call for the side-effect (rollback-first + flat keys written to
                # frappe.local.response) but return None: this decorator sits
                # OUTSIDE @glific_response, so returning the dict would make Frappe
                # re-wrap it under `response.message` (nested → L-028 violation).
                # Returning None keeps only the flat top-level keys.
                safe_sp_api_error_response(
                    e, endpoint_name or fn.__name__, student_id=sid
                )
                return None
        return wrapper
    return deco


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
