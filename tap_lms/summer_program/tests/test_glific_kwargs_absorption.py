"""
Contract tests for Glific-consumed whitelisted endpoints (task #89).

Every endpoint Glific can call MUST accept arbitrary extra kwargs so that
Glific can add new top-level fields to its webhook payload (like the
`organization_id` multi-tenant tag added 2026-05-25 — discord report:
Himani re Mayank ST00052222) without crashing us with a Python-level
TypeError at parameter binding.

Without `**kwargs`, the TypeError surfaces as a raw HTML 500 from
Frappe's request dispatcher — violating api-standard-glific.md Rule 7
(every error must return the flat envelope) and breaking the Glific
flow that was expecting `{"success": false, "status": "..."}` to branch on.

These tests use `inspect.signature` to verify each endpoint accepts
`**kwargs`. They run fast (no DB), so they're cheap to keep as a
permanent contract guard. New endpoints added by future CRs should
also appear in `GLIFIC_ENDPOINTS` below — otherwise they'll silently
re-introduce the same bug class.

Future hardening (task deferred to post-launch, "Layer 3"):
  - `@glific_request` decorator that wraps the kwargs absorption +
    TypeError-to-envelope conversion in one place
  - api-standard-glific.md Rule 11 requiring **kwargs on all such handlers
  - L-043 lesson broadening L-009 to cover field additions (not just renames)
"""
import importlib
import inspect
import unittest


# The complete list of Glific-consumed whitelisted endpoints, per the
# 2026-05-25 root-cause investigation. When a new endpoint is added
# (or one of these is renamed/moved), update this list. The test class
# below iterates this list and fails if any entry is missing `**kwargs`.
GLIFIC_ENDPOINTS = [
    "tap_lms.summer_program.save_submission.save_submission",
    "tap_lms.summer_program.save_submission.get_submission_feedback",
    "tap_lms.summer_program.flow_callback.update_flow_status",
    "tap_lms.summer_program.student_progression_sp.get_weekly_content",
    "tap_lms.summer_program.student_progression_sp.get_next_content",
    "tap_lms.summer_program.student_progression_sp.get_content_details",
    "tap_lms.summer_program.student_progression_sp.complete_content",
    "tap_lms.summer_program.student_progression_sp.start_quiz",
    "tap_lms.summer_program.student_progression_sp.submit_answer",
    "tap_lms.summer_program.quiz_points.award_bonus_quiz_points",
    "tap_lms.summer_program.custom_messages.get_submission_message",
    "tap_lms.summer_program.reactivation.reactivate_student",
]


def _resolve(path):
    """Return the function object at a dotted module.function path."""
    module_path, fn_name = path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    fn = getattr(module, fn_name)
    # Unwrap any decorators (@frappe.whitelist, @glific_response) so we
    # inspect the original signature, not the wrapper's.
    return inspect.unwrap(fn)


class TestGlificEndpointsAbsorbExtraKwargs(unittest.TestCase):
    """Contract: every Glific-consumed endpoint MUST accept **kwargs.

    The bug pattern this guards against: Glific adds a new top-level
    field to its webhook payload (organization_id was the 2026-05-25
    example), Python parameter binding fails with TypeError, Frappe
    returns raw HTML 500, the Glific flow breaks because it expected
    the flat-envelope JSON per api-standard-glific.md Rule 7.

    Failure here means a future Glific payload expansion WILL break
    that endpoint. Fix: add `**_glific_kwargs` to the signature.
    """

    def test_every_endpoint_accepts_var_keyword(self):
        missing = []
        for path in GLIFIC_ENDPOINTS:
            fn = _resolve(path)
            sig = inspect.signature(fn)
            has_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if not has_var_keyword:
                missing.append(f"{path}{sig}")

        self.assertEqual(
            missing, [],
            "These Glific-consumed endpoints are missing **kwargs "
            "absorption (task #89). Glific can add new top-level "
            "webhook fields at any time; without **kwargs the next "
            "field they add will TypeError → raw HTML 500 → broken "
            "Glific flow. Fix: append `**_glific_kwargs` to the "
            "signature. See test docstring for context.\n\n  "
            + "\n  ".join(missing)
        )

    def test_save_submission_accepts_organization_id(self):
        """The specific case from the 2026-05-25 discord report — pass
        the Glific webhook body verbatim and confirm Python parameter
        binding doesn't reject it. We don't care about the function's
        return value here — just that the call doesn't TypeError before
        the handler body runs.
        """
        from tap_lms.summer_program.save_submission import save_submission

        try:
            # Match the exact payload Glific sent for Mayank:
            # {"submission":"","student_id":"ST00052222","organization_id":12,
            #  "assignment_id":"GetReadyForScratchJr Main-Basic"}
            #
            # We bind only — don't care if the call succeeds. If the
            # student doesn't exist in the test DB the function will
            # raise its OWN validation error, which is fine. We're
            # specifically guarding against TypeError from Python's
            # parameter binding (which happens BEFORE the function
            # body executes).
            save_submission(
                student_id="DOES_NOT_EXIST_BUT_THAT_IS_OK",
                submission="",
                organization_id=12,
                assignment_id="GetReadyForScratchJr Main-Basic",
            )
        except TypeError as exc:
            self.fail(
                f"save_submission TypeError'd on organization_id "
                f"kwarg — task #89 regression: {exc}"
            )
        except Exception:
            # Any non-TypeError exception (student-not-found,
            # validation, etc.) is the function's normal error path
            # and not what we're testing here.
            pass

    def test_save_submission_accepts_arbitrary_future_kwargs(self):
        """Defensive: even fields Glific hasn't added yet must be
        absorbed. This is the whole point of `**kwargs` — forward
        compatibility with whatever Glific decides to add next."""
        from tap_lms.summer_program.save_submission import save_submission

        try:
            save_submission(
                student_id="DOES_NOT_EXIST_BUT_THAT_IS_OK",
                submission="",
                assignment_id="X",
                # Hypothetical future Glific fields:
                organization_id=12,
                contact_id=9999,
                flow_run_uuid="some-uuid",
                webhook_signature="hmac-sha256-foo",
            )
        except TypeError as exc:
            self.fail(
                f"save_submission rejected a hypothetical future "
                f"Glific kwarg with TypeError — task #89 contract "
                f"violation: {exc}"
            )
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
