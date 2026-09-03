import functools
import threading
import time
import uuid

import frappe

from tap_lms.utils.api_failures import _write_api_failure, log_api_failure


def _safe_dict(value):
    try:
        return dict(value or {})
    except Exception:
        return str(value)


def _safe_get_site():
    try:
        return getattr(frappe.local, "site", None)
    except Exception:
        return None


def _safe_get_user():
    try:
        return getattr(frappe.session, "user", None)
    except Exception:
        return None


def _safe_get_request_meta():
    try:
        request = getattr(frappe, "request", None)
        if not request:
            return {}

        return {
            "path": getattr(request, "path", None),
            "method": getattr(request, "method", None),
        }
    except Exception:
        return {}


def _build_input_payload(fn, args, kwargs, request_id, timeout_seconds):
    payload = {
        "request_id": request_id,
        "timeout_seconds": timeout_seconds,
        "function": f"{fn.__module__}.{fn.__name__}",
        "args": args,
        "kwargs": kwargs,
        "form_dict": _safe_dict(getattr(frappe, "form_dict", None)),
        "request": _safe_get_request_meta(),
    }

    site = _safe_get_site()
    if site:
        payload["site"] = site

    user = _safe_get_user()
    if user:
        payload["user"] = user

    return payload


def _timeout_error(method_name, request_id, timeout_seconds, elapsed_seconds):
    return (
        "Glific webhook exceeded timeout threshold. "
        f"method_name={method_name}, "
        f"request_id={request_id}, "
        f"timeout_seconds={timeout_seconds}, "
        f"elapsed_seconds={elapsed_seconds:.3f}"
    )


def _write_timeout_failure_in_new_context(site, method_name, input_payload, error):
    connected = False
    try:
        if site:
            frappe.init(site=site)
            frappe.connect()
            connected = True

        _write_api_failure(method_name, input_payload, error)
    except Exception:
        try:
            frappe.logger().error(
                f"Failed to write Glific timeout API Failure for {method_name}"
            )
        except Exception:
            pass
    finally:
        if connected:
            try:
                frappe.destroy()
            except Exception:
                pass


def log_glific_timeout(fn=None, *, timeout_seconds=4.5, method_name=None):
    """Log Glific-facing endpoint calls that run past the timeout threshold.

    Usage:
        @frappe.whitelist(allow_guest=True)
        @log_glific_timeout
        def endpoint(...):
            ...

        @frappe.whitelist(allow_guest=True)
        @log_glific_timeout(method_name="endpoint")
        def endpoint(...):
            ...

    The decorator does not stop the wrapped function. It records an API Failures
    row when the function is still running at the threshold, or when it finishes
    after the threshold before the watchdog thread has fired.
    """
    if fn is not None and not callable(fn):
        timeout_seconds = fn
        fn = None

    timeout_seconds = float(timeout_seconds)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            started_at = time.monotonic()
            request_id = str(uuid.uuid4())
            resolved_method_name = method_name or func.__name__
            site = _safe_get_site()
            done = threading.Event()
            state_lock = threading.Lock()
            state = {"logged": False}
            input_payload = _build_input_payload(
                func,
                args,
                kwargs,
                request_id,
                timeout_seconds,
            )

            def mark_logged():
                with state_lock:
                    if state["logged"]:
                        return False
                    state["logged"] = True
                    return True

            def elapsed():
                return time.monotonic() - started_at

            def watchdog():
                if done.is_set() or not mark_logged():
                    return

                elapsed_seconds = elapsed()
                error = _timeout_error(
                    resolved_method_name,
                    request_id,
                    timeout_seconds,
                    elapsed_seconds,
                )
                _write_timeout_failure_in_new_context(
                    site,
                    resolved_method_name,
                    input_payload,
                    error,
                )

            timer = threading.Timer(timeout_seconds, watchdog)
            timer.daemon = True
            timer.start()

            try:
                return func(*args, **kwargs)
            finally:
                elapsed_seconds = elapsed()
                done.set()
                timer.cancel()

                if elapsed_seconds >= timeout_seconds and mark_logged():
                    log_api_failure(
                        resolved_method_name,
                        input_payload,
                        _timeout_error(
                            resolved_method_name,
                            request_id,
                            timeout_seconds,
                            elapsed_seconds,
                        ),
                    )

        return wrapper

    if fn is not None:
        return decorate(fn)

    return decorate
