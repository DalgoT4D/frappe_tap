import json
import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, call, patch


def _import_save_submission_with_stubs():
    frappe = MagicMock()
    frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    frappe.ValidationError = type("ValidationError", (Exception,), {})
    frappe.whitelist = _frappe_whitelist
    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.now_datetime = MagicMock(return_value="2026-05-12 10:00:00")
    frappe_utils.today = MagicMock(return_value="2026-05-12")
    frappe_utils.getdate = MagicMock(side_effect=lambda value: value)
    frappe_utils.cint = lambda value: int(value or 0)

    state_machine = types.ModuleType("tap_lms.summer_program.state_machine")
    state_machine.get_active_pe = MagicMock()
    state_machine.apply_submission_transition = MagicMock(return_value=("T7", True))
    state_machine.t22_duplicate_submission = MagicMock()

    event_log = types.ModuleType("tap_lms.summer_program.event_log")
    event_log.log_event = MagicMock()

    sys.modules["frappe"] = frappe
    sys.modules["frappe.utils"] = frappe_utils
    sys.modules["tap_lms.summer_program.state_machine"] = state_machine
    sys.modules["tap_lms.summer_program.event_log"] = event_log
    sys.modules.pop("tap_lms.summer_program.save_submission", None)
    return importlib.import_module("tap_lms.summer_program.save_submission")


def _frappe_whitelist(fn=None, **kwargs):
    if fn:
        return fn

    def decorator(func):
        return func

    return decorator


class TestSaveSubmissionContentLogBridge(unittest.TestCase):
    def test_normalizes_submission_without_inferring_kind(self):
        save_submission = _import_save_submission_with_stubs()

        with patch("tap_lms.imgana.media_detection.detect_url_media_type") as mock_detect:
            payload = save_submission._normalize_submission_payload(
                "https://filemanager.gupshup.io/wa/account/wa/media/1372345368111462?download=false"
            )

        self.assertIsNone(payload["submission_type"])
        self.assertIsNone(payload["submission_text"])
        self.assertIsNone(payload["submission_url"])
        self.assertEqual(
            payload["raw_submission"],
            "https://filemanager.gupshup.io/wa/account/wa/media/1372345368111462?download=false",
        )
        mock_detect.assert_not_called()

        text_payload = save_submission._normalize_submission_payload("hello world")
        self.assertEqual(text_payload["raw_submission"], "hello world")
        self.assertIsNone(text_payload["submission_type"])
        self.assertIsNone(text_payload["submission_text"])
        self.assertIsNone(text_payload["submission_url"])

    def test_async_processing_detects_media_type_and_uploads_after_response(self):
        save_submission = _import_save_submission_with_stubs()

        submission = MagicMock()
        submission.name = "SUB-001"
        submission.submission_type = None
        submission.submission_url = "https://filemanager.gupshup.io/wa/account/wa/media/1372345368111462"

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch("tap_lms.imgana.media_detection.detect_url_media_type", return_value="audio") as mock_detect, \
                patch("tap_lms.imgana.gcs_client.upload_to_gcs", return_value="https://storage.example/SUB-001.ogg") as mock_upload, \
                patch.object(save_submission, "enqueue_submission") as mock_enqueue:
            mock_frappe.get_doc.return_value = submission

            save_submission.process_submission_async(
                "SUB-001",
                raw_submission="https://filemanager.gupshup.io/wa/account/wa/media/1372345368111462",
                pe_context={"program_enrollment": "PE-001"},
            )

        mock_detect.assert_called_once_with(
            "https://filemanager.gupshup.io/wa/account/wa/media/1372345368111462",
            default="image",
        )
        mock_upload.assert_called_once_with(
            "https://filemanager.gupshup.io/wa/account/wa/media/1372345368111462",
            "SUB-001",
            media_type="audio",
        )
        self.assertEqual(submission.submission_type, "audio")
        self.assertEqual(submission.submission_url, "https://storage.example/SUB-001.ogg")
        self.assertEqual(submission.status, "Processing")
        submission.save.assert_called_once_with(ignore_permissions=True)
        mock_enqueue.assert_called_once_with("SUB-001", pe_context={"program_enrollment": "PE-001"})

    def test_async_processing_classifies_text_after_response(self):
        save_submission = _import_save_submission_with_stubs()

        submission = MagicMock()
        submission.name = "SUB-001"
        submission.submission_type = None
        submission.submission_text = None
        submission.submission_url = None

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch("tap_lms.imgana.media_detection.detect_url_media_type") as mock_detect, \
                patch.object(save_submission, "enqueue_submission") as mock_enqueue:
            mock_frappe.get_doc.return_value = submission

            save_submission.process_submission_async(
                "SUB-001",
                raw_submission="hello world",
                pe_context={"program_enrollment": "PE-001"},
            )

        mock_detect.assert_not_called()
        self.assertEqual(submission.submission_type, "text")
        self.assertEqual(submission.submission_text, "hello world")
        self.assertIsNone(submission.submission_url)
        self.assertEqual(submission.status, "Processing")
        submission.save.assert_called_once_with(ignore_permissions=True)
        mock_enqueue.assert_called_once_with("SUB-001", pe_context={"program_enrollment": "PE-001"})

    def test_primary_submission_writes_student_content_log(self):
        save_submission = _import_save_submission_with_stubs()

        pe = MagicMock()
        pe.name = "PE-001"
        pe.course_level = "CL-001"
        pe.current_tier = "Basic"
        pe.current_expected_submission_type = "photo_video_artefact"
        submission_doc = MagicMock()
        submission_doc.name = "SUB-001"

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.db.exists.return_value = None
            log = MagicMock()
            mock_frappe.new_doc.return_value = log

            save_submission._log_student_content_submission(
                pe=pe,
                student_id="STU-001",
                week=2,
                payload={"submission_type": "image"},
                assignment_id="ASN-001",
                points=10,
                submission_doc=submission_doc,
            )

        mock_frappe.new_doc.assert_called_once_with("StudentContentLog")
        self.assertEqual(log.student, "STU-001")
        self.assertEqual(log.course_level, "CL-001")
        self.assertEqual(log.stage_no, 2)
        self.assertEqual(log.content_type, "Assignment")
        self.assertEqual(log.content_id, "ASN-001")
        self.assertEqual(log.action, "completed")
        self.assertEqual(log.tier, "Basic")
        metadata = json.loads(log.metadata)
        self.assertEqual(metadata["source"], "save_submission")
        self.assertEqual(metadata["submission_id"], "SUB-001")
        self.assertEqual(metadata["program_enrollment"], "PE-001")
        self.assertTrue(metadata["is_valid"])
        log.insert.assert_called_once_with(ignore_permissions=True)

    def test_existing_student_content_log_is_not_duplicated(self):
        save_submission = _import_save_submission_with_stubs()

        pe = MagicMock()

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.db.exists.return_value = "SCL-001"

            save_submission._log_student_content_submission(
                pe=pe,
                student_id="STU-001",
                week=1,
                payload={"submission_type": "text"},
                assignment_id="ASN-001",
                points=0,
                submission_doc=None,
            )

        mock_frappe.new_doc.assert_not_called()

    def test_expected_submission_type_compatibility(self):
        save_submission = _import_save_submission_with_stubs()
        _is_expected_submission_type = save_submission._is_expected_submission_type

        self.assertTrue(_is_expected_submission_type("image", "photo"))
        self.assertTrue(_is_expected_submission_type("video", "photo_video_artefact"))
        self.assertTrue(_is_expected_submission_type("text", "voice_note_text_summary"))
        self.assertFalse(_is_expected_submission_type("image", "voice_note_text_summary"))


class TestGetSubmissionFeedback(unittest.TestCase):
    def test_completed_submission_returns_feedback_fields(self):
        save_submission = _import_save_submission_with_stubs()

        submission = MagicMock()
        submission.status = "Completed"
        submission.overall_feedback = "Good work"
        submission.overall_feedback_translated = "Good work translated"
        submission.audio_feedback_url = "https://example.com/audio.mp3"

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.get_doc.return_value = submission

            response = save_submission.get_submission_feedback("SUB-001")

        mock_frappe.get_doc.assert_called_once_with("Submission", "SUB-001")
        self.assertEqual(response, {
            "status": "Completed",
            "overall_feedback": "Good work",
            "overall_feedback_translated": "Good work translated",
            "audio_feedback_url": "https://example.com/audio.mp3",
        })

    def test_pending_submission_returns_status_only(self):
        save_submission = _import_save_submission_with_stubs()

        submission = MagicMock()
        submission.status = "Processing"

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.get_doc.return_value = submission

            response = save_submission.get_submission_feedback("SUB-001")

        self.assertEqual(response, {"status": "Processing"})

    def test_missing_submission_returns_not_found_error(self):
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
            mock_frappe.get_doc.side_effect = mock_frappe.DoesNotExistError()

            response = save_submission.get_submission_feedback("SUB-MISSING")

        self.assertEqual(response, {"error": "Submission not found"})

    def test_unexpected_error_is_logged(self):
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
            mock_frappe.get_doc.side_effect = RuntimeError("database unavailable")

            response = save_submission.get_submission_feedback("SUB-001")

        self.assertEqual(
            response,
            {"error": "An error occurred while checking submission feedback"},
        )
        mock_frappe.log_error.assert_called_once()


class TestReadyToReceiveFeedback(unittest.TestCase):
    def test_pending_submission_sets_feedback_request_without_trigger(self):
        save_submission = _import_save_submission_with_stubs()

        submission = MagicMock()
        submission.name = "SUB-READY-001"
        submission.status = "Processing"

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.get_doc.return_value = submission
            mock_frappe.db.sql.return_value = [("SUB-READY-001",)]

            response = save_submission.ready_to_receive_feedback("SUB-READY-001")

        mock_frappe.get_doc.assert_called_once_with("Submission", "SUB-READY-001")
        self.assertEqual(
            mock_frappe.db.sql.call_args.args[1],
            ("2026-05-12 10:00:00", "SUB-READY-001"),
        )
        self.assertEqual(response, {
            "success": True,
            "status": "success",
            "message": "Feedback will be given once ready.",
        })

    def test_completed_submission_claims_and_triggers_feedback_flow(self):
        save_submission = _import_save_submission_with_stubs()
        events = []

        submission = MagicMock()
        submission.name = "SUB-READY-002"
        submission.status = "Completed"
        submission.student_id = "STU-READY-002"
        submission.overall_feedback = "Done"

        fake_module = types.ModuleType("tap_lms.feedback_handler.feedback_consumer")

        class FakeFeedbackConsumer:
            def process_feedback_ready(self, submission_id, message_data):
                events.append(("feedback_ready", submission_id, message_data))
                return True

            def _claim_feedback_flow(self, submission_id):
                events.append(("claim", submission_id))
                return True

            def trigger_feedback_flow(self, submission_id, message_data):
                events.append(("trigger", submission_id, message_data))

        fake_module.FeedbackConsumer = FakeFeedbackConsumer
        old_module = sys.modules.get("tap_lms.feedback_handler.feedback_consumer")
        sys.modules["tap_lms.feedback_handler.feedback_consumer"] = fake_module

        try:
            with patch.object(save_submission, "frappe") as mock_frappe:
                mock_frappe.get_doc.return_value = submission
                mock_frappe.db.sql.return_value = [("SUB-READY-002",)]

                response = save_submission.ready_to_receive_feedback("SUB-READY-002")
        finally:
            if old_module is None:
                sys.modules.pop("tap_lms.feedback_handler.feedback_consumer", None)
            else:
                sys.modules["tap_lms.feedback_handler.feedback_consumer"] = old_module

        self.assertEqual(events, [
            (
                "feedback_ready",
                "SUB-READY-002",
                {
                    "submission_id": "SUB-READY-002",
                    "student_id": "STU-READY-002",
                    "feedback": {"overall_feedback": "Done"},
                },
            ),
            ("claim", "SUB-READY-002"),
            (
                "trigger",
                "SUB-READY-002",
                {
                    "submission_id": "SUB-READY-002",
                    "student_id": "STU-READY-002",
                    "feedback": {"overall_feedback": "Done"},
                },
            ),
        ])
        mock_frappe.db.commit.assert_called_once()
        self.assertEqual(response, {
            "success": True,
            "status": "success",
            "message": "Feedback flow triggered.",
        })

    def test_already_triggered_submission_does_not_retrigger(self):
        save_submission = _import_save_submission_with_stubs()

        submission = MagicMock()
        submission.name = "SUB-READY-003"
        submission.status = "Completed"

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.get_doc.return_value = submission
            mock_frappe.db.sql.return_value = []

            response = save_submission.ready_to_receive_feedback("SUB-READY-003")

        self.assertEqual(response, {
            "success": True,
            "status": "success",
            "message": "Feedback flow already triggered.",
        })


# ════════════════════════════════════════════════════════════
# Audit-fix tests (2026-05-15) — tasks #77/79, #78, #80, #81
# Uses the same import-stub strategy as the bridge tests above so it can
# run without a live bench. No frappe.db.commit() per L-017.
# ════════════════════════════════════════════════════════════


class TestSaveSubmissionAuditFixes(unittest.TestCase):
    """Tests for the 2026-05-15 audit-fix bundle (tasks #77-#82).

    - test_save_submission_accepts_legacy_content_id — task #78 (P-006/L-009).
    - test_save_submission_in_clause_handles_all_pre_submission_labels —
      task #77 (L-005 fix) + task #79 (MariaDB fallback removed).
    - test_submission_count_increments_exactly_once — task #80
      (state-machine transitions no longer bump submission_count;
      _try_claim_primary owns the column).
    - test_create_submission_failure_does_not_bump_state — task #81
      (insert-first ordering; savepoint rolls back on insert failure).
    """

    def _build_pe(self, journey_label="content_delivered"):
        pe = MagicMock()
        pe.name = "PE-001"
        pe.student = "STU-001"
        pe.batch = "BATCH-001"
        pe.glific_id = "GL-001"
        pe.current_week = 1
        pe.resolved_flow_state = "normal_content_delivery"
        pe.journey_label = journey_label
        pe.submission_count = 0
        pe.current_escalation_step = 0
        pe.current_expected_submission_type = "text_word"
        pe.current_tier = "Basic"
        pe.current_path = "core"
        pe.archetype = "submitter"
        pe.experiment_arm = "default"
        pe.course_level = "CL-001"
        pe.language = "en"
        pe.program_status = "active"
        pe.next_action_type = ""
        pe.next_action_at = None
        return pe

    def test_save_submission_accepts_legacy_content_id(self):
        """Task #78 (L-009 / P-006): content_id is a deprecated alias for
        assignment_id. Calling with `content_id=` only (no `assignment_id=`)
        must succeed and log a deprecation warning."""
        save_submission = _import_save_submission_with_stubs()
        pe = self._build_pe()
        submission_doc = MagicMock()
        submission_doc.name = "SUB-001"

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch.object(save_submission, "_resolve_student", return_value="STU-001"), \
                patch.object(save_submission, "get_active_pe", return_value=pe), \
                patch.object(save_submission, "_try_claim_primary", return_value=True), \
                patch.object(save_submission, "_create_submission", return_value=submission_doc), \
                patch.object(save_submission, "_log_student_content_submission"), \
                patch.object(save_submission, "_update_engagement"), \
                patch.object(save_submission, "_queue_submission_processing"), \
                patch.object(save_submission, "apply_submission_transition", return_value=("T7", True)), \
                patch.object(save_submission, "log_event"):
            mock_frappe.local.response = {}
            mock_frappe.utils.random_string = lambda n: "abcd1234"

            response = save_submission.save_submission(
                student_id="STU-001",
                content_id="ASN-LEGACY",  # legacy param — no assignment_id
                submission="my answer",
            )

        self.assertTrue(response.get("success"))
        self.assertEqual(response.get("status"), "accepted")
        # Deprecation log should have fired with "SP API Deprecation" title.
        deprecation_logged = any(
            call_args.args and "SP API Deprecation" in (call_args.args[1] if len(call_args.args) > 1 else "")
            for call_args in mock_frappe.log_error.call_args_list
        )
        self.assertTrue(
            deprecation_logged,
            f"Expected deprecation log; got calls: {mock_frappe.log_error.call_args_list}",
        )

    def test_duplicate_submission_gets_stock_feedback_in_student_language(self):
        save_submission = _import_save_submission_with_stubs()
        pe = self._build_pe(journey_label="submitted")
        pe.language = "Hindi"
        submission_doc = MagicMock()
        submission_doc.name = "SUB-DUP-001"

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch.object(save_submission, "_resolve_student", return_value="STU-001"), \
                patch.object(save_submission, "get_active_pe", return_value=pe), \
                patch.object(save_submission, "_try_claim_primary", return_value=False), \
                patch.object(save_submission, "_create_submission", return_value=submission_doc), \
                patch.object(save_submission, "_update_engagement"), \
                patch.object(save_submission, "_queue_submission_processing") as mock_queue, \
                patch.object(save_submission, "log_event"):
            mock_frappe.local.response = {}
            mock_frappe.utils.random_string = lambda n: "abcd1234"

            response = save_submission.save_submission(
                student_id="STU-001",
                assignment_id="ASN-001",
                submission="my answer",
            )

        self.assertTrue(response.get("success"))
        self.assertEqual(response.get("status"), "duplicate")
        mock_queue.assert_not_called()

        duplicate_update = mock_frappe.db.set_value.call_args
        self.assertEqual(duplicate_update.args[0], "Submission")
        self.assertEqual(duplicate_update.args[1], "SUB-DUP-001")
        updates = duplicate_update.args[2]
        self.assertEqual(updates["result_status"], "Success - Flagged")
        self.assertEqual(
            updates["overall_feedback"],
            "Hey champ, you've already submitted this activity! Hang tight — the next one is coming soon. 🏆",
        )
        self.assertEqual(
            updates["overall_feedback_translated"],
            "अरे चैंप, तुमने इस activity को पहले ही submit कर दिया है! थोड़ा रुको — अगली activity जल्द आ रही है। 🏆",
        )
        self.assertEqual(
            updates["audio_feedback_url"],
            "https://storage.googleapis.com/tap-lms-submissions/audio_feedback/double_submission_hindi.mp3",
        )
        self.assertEqual(submission_doc.result_status, "Success - Flagged")

    def test_save_submission_missing_assignment_id_is_rejected(self):
        """Companion to the previous: with neither param the call must fail
        cleanly with status='missing_param'."""
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.local.response = {}

            save_submission.save_submission(
                student_id="STU-001",
                submission="my answer",
            )

        self.assertEqual(mock_frappe.local.response.get("status"), "missing_param")
        self.assertFalse(mock_frappe.local.response.get("success"))

    def test_save_submission_in_clause_handles_all_pre_submission_labels(self):
        """Task #77 (L-005 fix): the atomic claim's
        `journey_label IN (%s, %s, %s, %s, %s)` must accept every
        pre-submission label without the IN-tuple mangling that the previous
        `IN %s` binding suffered on Postgres.

        We verify by inspecting the SQL+params passed to frappe.db.sql:
          - the SQL contains a flat IN-list of 5 placeholders;
          - the params include the PE name plus the 5 expected labels.
        """
        save_submission = _import_save_submission_with_stubs()
        pe = self._build_pe()

        with patch.object(save_submission, "frappe") as mock_frappe:
            # RETURNING-shaped result — single row means the claim succeeded.
            mock_frappe.db.sql.return_value = [("PE-001",)]
            result = save_submission._try_claim_primary(pe, week=1)

        self.assertTrue(result)
        self.assertEqual(mock_frappe.db.sql.call_count, 1)
        sql_call = mock_frappe.db.sql.call_args
        sql_text = sql_call.args[0]
        params = sql_call.args[1]

        # Flat IN list (no `IN %s` mangling).
        self.assertIn("IN (%s, %s, %s, %s, %s)", sql_text)
        # PE name + 5 labels = 6 params total.
        self.assertEqual(len(params), 6)
        self.assertEqual(params[0], "PE-001")
        self.assertEqual(
            set(params[1:]),
            {"enrolled", "content_delivered", "grace_window", "resumed", "week_advanced"},
        )

    def test_try_claim_primary_returns_false_on_zero_rows(self):
        """Task #79 corollary: with the MariaDB fallback gone, a 0-row
        UPDATE (RETURNING []) must short-circuit to is_primary=False without
        falling through to a second UPDATE."""
        save_submission = _import_save_submission_with_stubs()
        pe = self._build_pe(journey_label="submitted")

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.db.sql.return_value = []  # nothing claimed
            result = save_submission._try_claim_primary(pe, week=1)

        self.assertFalse(result)
        # Exactly one UPDATE — the old fallback would have made it 2.
        self.assertEqual(mock_frappe.db.sql.call_count, 1)

    def test_submission_count_not_in_primary_transition_source(self):
        """Task #80: submission_count is bumped only by _try_claim_primary's
        atomic UPDATE. The four primary transitions (T7/T9/T17/T3) must NOT
        include submission_count in their `extra_updates` dict.

        We can't import state_machine inside the stub environment (it pulls
        in real frappe and glific_integration), so we assert against the
        source file itself: each primary-transition function body must not
        contain a `submission_count` write.
        """
        import os
        import re

        here = os.path.dirname(__file__)
        sm_path = os.path.join(here, "..", "state_machine.py")
        with open(sm_path, "r", encoding="utf-8") as fh:
            source = fh.read()

        # Extract each function body. Each `def tX_...` ends at the next
        # top-level `def` or `# ──` separator. Splitting on "\ndef " is
        # robust enough for this audit-grade check.
        primary_fns = [
            "t7_core_submission",
            "t9_remedial_submission",
            "t17_grace_submission",
            "t3_escalation_submission",
        ]
        for fn_name in primary_fns:
            m = re.search(
                rf"def {fn_name}\([^)]*\):(.*?)(?=\ndef |\Z)",
                source,
                re.DOTALL,
            )
            self.assertIsNotNone(m, f"Could not locate {fn_name} in state_machine.py")
            body = m.group(1)
            # The body must not contain a `submission_count` write
            # (e.g. `"submission_count": ...`). We exclude the
            # human-readable NOTE comment that explains the ownership.
            body_no_comments = re.sub(r"#[^\n]*", "", body)
            self.assertNotIn(
                '"submission_count"', body_no_comments,
                f"{fn_name} still writes submission_count (task #80 regression).",
            )

    def test_create_submission_failure_does_not_bump_state(self):
        """Task #81: if _create_submission raises, the savepoint must roll
        back and _try_claim_primary must NOT have been called. The PE
        journey_label and submission_count remain untouched."""
        save_submission = _import_save_submission_with_stubs()
        pe = self._build_pe()

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch.object(save_submission, "_resolve_student", return_value="STU-001"), \
                patch.object(save_submission, "get_active_pe", return_value=pe), \
                patch.object(save_submission, "_try_claim_primary") as mock_claim, \
                patch.object(save_submission, "_create_submission",
                             side_effect=RuntimeError("DB write failed")), \
                patch.object(save_submission, "_update_engagement"), \
                patch.object(save_submission, "log_event"):
            mock_frappe.local.response = {}
            mock_frappe.utils.random_string = lambda n: "abcd1234"

            response = save_submission.save_submission(
                student_id="STU-001",
                assignment_id="ASN-001",
                submission="my answer",
            )

        # _try_claim_primary must NOT have run — insert failed first.
        mock_claim.assert_not_called()
        # Savepoint rollback was invoked.
        mock_frappe.db.rollback.assert_called_once()
        # Response surfaces the failure cleanly.
        self.assertEqual(mock_frappe.local.response.get("status"), "insert_failed")
        self.assertFalse(mock_frappe.local.response.get("success"))

    def test_serialization_failure_during_insert_retries_whole_submission(self):
        """Postgres concurrent-update/serialization failures must retry the
        whole save_submission attempt, not return insert_failed from the
        savepoint handler."""
        save_submission = _import_save_submission_with_stubs()
        pe = self._build_pe()
        submission_doc = MagicMock()
        submission_doc.name = "SUB-001"

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch.object(save_submission.time, "sleep") as mock_sleep, \
                patch.object(save_submission, "_resolve_student", return_value="STU-001"), \
                patch.object(save_submission, "get_active_pe", return_value=pe), \
                patch.object(save_submission, "_try_claim_primary", return_value=True), \
                patch.object(
                    save_submission,
                    "_create_submission",
                    side_effect=[
                        RuntimeError("could not serialize access due to concurrent update"),
                        submission_doc,
                    ],
                ) as mock_create, \
                patch.object(save_submission, "_log_student_content_submission"), \
                patch.object(save_submission, "_update_engagement"), \
                patch.object(save_submission, "_queue_submission_processing"), \
                patch.object(save_submission, "apply_submission_transition", return_value=("T7", True)), \
                patch.object(save_submission, "log_event"):
            mock_frappe.local.response = {}
            mock_frappe.ValidationError = type("ValidationError", (Exception,), {})
            mock_frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
            mock_frappe.utils.random_string = lambda n: "abcd1234"

            response = save_submission.save_submission(
                student_id="STU-001",
                assignment_id="ASN-001",
                submission="my answer",
            )

        self.assertTrue(response.get("success"))
        self.assertEqual(response.get("status"), "accepted")
        self.assertEqual(mock_create.call_count, 2)
        mock_sleep.assert_called_once()
        self.assertIn(call(save_point="sub_create_abcd1234"), mock_frappe.db.rollback.call_args_list)
        self.assertIn(call(), mock_frappe.db.rollback.call_args_list)


# ════════════════════════════════════════════════════════════
# Task #93 — structured-envelope hardening (api-standard-glific Rule 7)
# ════════════════════════════════════════════════════════════

class TestSaveSubmissionStructuredErrors(unittest.TestCase):
    """Task #93 (2026-05-25): every error path in save_submission MUST
    return the flat envelope via frappe.local.response, never raise.

    Discord report 2026-05-25: Mayank (ST00052222) hit HTTP 500 because
    Glific sent `submission=""` for his emoji flow. The downstream
    _normalize_submission_payload raised
    `frappe.ValidationError("Submission is required")`; the retry loop
    didn't catch it; Frappe's HTTP layer returned raw HTML instead of
    the documented `{success: false, status: ...}` envelope. This
    violates docs/api-standard-glific.md Rule 7. Same bug class as
    Layer 2 (#89) but for in-function exceptions, not parameter binding.
    """

    def test_empty_submission_returns_structured_envelope(self):
        """The headline regression — Mayank's exact failing payload.
        Empty `submission=""` must short-circuit with `status='submission_empty'`,
        not raise / surface as HTML 500."""
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.local.response = {}
            mock_frappe.ValidationError = type(
                "ValidationError", (Exception,), {})
            mock_frappe.DoesNotExistError = type(
                "DoesNotExistError", (Exception,), {})

            result = save_submission.save_submission(
                student_id="ST00052222",
                submission="",                # empty — the bug trigger
                organization_id=12,           # Glific kwarg
                assignment_id="GetReadyForScratchJr Main-Basic",
            )

        # Function MUST return cleanly (not raise) so the HTTP layer
        # serializes the response envelope instead of returning HTML.
        self.assertIsNone(result, "function must use frappe.local.response, not return")
        # Response body matches the documented structured envelope.
        self.assertFalse(mock_frappe.local.response["success"])
        self.assertEqual(mock_frappe.local.response["status"],
                         "submission_empty")
        self.assertIn("user_message", mock_frappe.local.response)
        self.assertIn("error_detail", mock_frappe.local.response)

    def test_whitespace_only_submission_treated_as_empty(self):
        """Defensive: '   ' (whitespace) is functionally empty — same
        structured response, no exception."""
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.local.response = {}
            mock_frappe.ValidationError = type(
                "ValidationError", (Exception,), {})

            save_submission.save_submission(
                student_id="STU-001",
                submission="    ",            # whitespace-only
                assignment_id="ASN-1",
            )

        self.assertEqual(mock_frappe.local.response["status"],
                         "submission_empty")

    def test_none_submission_treated_as_empty(self):
        """`submission=None` (not passed at all) → same structured response."""
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe:
            mock_frappe.local.response = {}
            mock_frappe.ValidationError = type(
                "ValidationError", (Exception,), {})

            save_submission.save_submission(
                student_id="STU-001",
                assignment_id="ASN-1",
                # submission not passed at all
            )

        self.assertEqual(mock_frappe.local.response["status"],
                         "submission_empty")

    def test_validation_error_from_inner_function_returns_envelope(self):
        """If `_save_submission_once` raises ValidationError (e.g., student
        not enrolled, assignment not found, week out of range), the wrapper
        must convert to structured response — NOT let it escape as HTML 500."""
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch.object(save_submission, "_save_submission_once") as inner:
            mock_frappe.local.response = {}
            ValidationError = type("ValidationError", (Exception,), {})
            mock_frappe.ValidationError = ValidationError
            mock_frappe.DoesNotExistError = type(
                "DoesNotExistError", (Exception,), {})

            inner.side_effect = ValidationError("Student not enrolled")

            result = save_submission.save_submission(
                student_id="STU-NOT-EXIST",
                submission="real submission",
                assignment_id="ASN-1",
            )

        self.assertIsNone(result)
        self.assertFalse(mock_frappe.local.response["success"])
        self.assertEqual(mock_frappe.local.response["status"],
                         "validation_error")
        self.assertIn("Student not enrolled",
                      mock_frappe.local.response["error_detail"])

    def test_does_not_exist_error_returns_envelope(self):
        """`frappe.DoesNotExistError` (e.g., assignment_id refers to a
        missing doc) → `status='not_found'` envelope, not HTML 500."""
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch.object(save_submission, "_save_submission_once") as inner:
            mock_frappe.local.response = {}
            DoesNotExistError = type("DoesNotExistError", (Exception,), {})
            mock_frappe.DoesNotExistError = DoesNotExistError
            mock_frappe.ValidationError = type(
                "ValidationError", (Exception,), {})

            inner.side_effect = DoesNotExistError("Assignment ASN-X not found")

            save_submission.save_submission(
                student_id="STU-001",
                submission="real submission",
                assignment_id="ASN-X",
            )

        self.assertFalse(mock_frappe.local.response["success"])
        self.assertEqual(mock_frappe.local.response["status"], "not_found")

    def test_unknown_exception_returns_internal_error_envelope(self):
        """Even a totally unexpected exception (KeyError, TypeError in
        a helper, etc.) must NOT escape as HTML 500. The wrapper logs
        the error AND returns structured response."""
        save_submission = _import_save_submission_with_stubs()

        with patch.object(save_submission, "frappe") as mock_frappe, \
                patch.object(save_submission, "_save_submission_once") as inner, \
                patch.object(save_submission, "_is_serialization_failure",
                             return_value=False):
            mock_frappe.local.response = {}
            mock_frappe.ValidationError = type(
                "ValidationError", (Exception,), {})
            mock_frappe.DoesNotExistError = type(
                "DoesNotExistError", (Exception,), {})

            inner.side_effect = KeyError("unexpected key 'foo'")

            save_submission.save_submission(
                student_id="STU-001",
                submission="real submission",
                assignment_id="ASN-1",
            )

        self.assertFalse(mock_frappe.local.response["success"])
        self.assertEqual(mock_frappe.local.response["status"],
                         "internal_error")
        # log_error MUST have fired so ops can investigate root cause.
        self.assertTrue(
            mock_frappe.log_error.called,
            "internal_error path must log to Error Log for ops visibility",
        )


if __name__ == "__main__":
    unittest.main()
