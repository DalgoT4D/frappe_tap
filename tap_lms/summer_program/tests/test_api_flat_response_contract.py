"""
Tests for the Glific flat-map response contract.

Per docs/api-standard-glific.md (Rules 2 + 3): every whitelisted endpoint that
Glific consumes must return a flat dict — every value must be a scalar
(string, int, float, bool, None). No nested dicts. No lists.

These tests do NOT execute the full business logic of each endpoint (that's
covered by integration tests on the bench). They mock the minimum needed to
reach the return statement and assert the response shape complies with the
contract.

Reference: docs/api-standard-glific.md sections 2, 3, 6.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock


# ════════════════════════════════════════════════════════════
# Flat-response predicate (the contract this test file enforces)
# ════════════════════════════════════════════════════════════


def assert_flat_response(response, *, allow_none=True):
    """Assert a response dict has only scalar values (no nested dicts or lists).

    Allowed value types: str, int, float, bool, None (or None disallowed
    per allow_none).

    Raises AssertionError with the offending key on first violation.
    """
    assert isinstance(response, dict), (
        f"Response must be a dict; got {type(response).__name__}"
    )
    SCALAR_TYPES = (str, int, float, bool)
    for key, value in response.items():
        if value is None:
            assert allow_none, f"Field '{key}' is None and allow_none=False"
            continue
        # bool is a subclass of int — both pass the SCALAR_TYPES check
        assert isinstance(value, SCALAR_TYPES), (
            f"Field '{key}' is not a scalar — got {type(value).__name__} "
            f"({value!r}). Flatten via numeric-suffix expansion (see "
            f"docs/api-standard-glific.md Rule 3)."
        )


class TestGlificResponseDecorator(unittest.TestCase):
    """The @glific_response decorator writes the wrapped function's return
    dict to frappe.local.response and returns None.

    Endpoints in student_progression_sp.py use this decorator (per task #69)
    so Glific reads `@results.webhook.<field>` directly instead of going
    through `@results.webhook.message.<field>`. Endpoint tests in this file
    call the underlying function via `.__wrapped__` to bypass the decorator
    and assert the dict structure; this class proves the decorator's own
    contract."""

    def test_decorator_writes_dict_to_frappe_local_response(self):
        from tap_lms.summer_program.utils import glific_response

        @glific_response
        def my_endpoint():
            return {"success": True, "status": "ok", "field": "value"}

        # Patch frappe.local at the module the decorator imports from
        with patch("tap_lms.summer_program.utils.frappe") as mock_frappe:
            mock_response = {}
            mock_frappe.local.response = mock_response

            result = my_endpoint()

            # Decorator returns None — Frappe will set response.message = None
            self.assertIsNone(result)
            # The endpoint's dict landed in frappe.local.response
            self.assertEqual(mock_response, {"success": True, "status": "ok", "field": "value"})

    def test_decorator_handles_none_return(self):
        """If the endpoint returns None (e.g. it wrote to local.response
        directly already), the decorator is a no-op — no write attempt."""
        from tap_lms.summer_program.utils import glific_response

        @glific_response
        def my_endpoint():
            return None

        with patch("tap_lms.summer_program.utils.frappe") as mock_frappe:
            mock_response = {}
            mock_frappe.local.response = mock_response

            result = my_endpoint()

            self.assertIsNone(result)
            self.assertEqual(mock_response, {})

    def test_decorator_preserves_wrapped_function_signature(self):
        """`__wrapped__` lets tests bypass the decorator to inspect the raw
        return value. functools.wraps preserves docstring + name too."""
        from tap_lms.summer_program.utils import glific_response

        @glific_response
        def my_endpoint(a, b):
            """An endpoint."""
            return {"sum": a + b}

        self.assertTrue(hasattr(my_endpoint, "__wrapped__"))
        # Direct call via __wrapped__ returns the raw dict
        self.assertEqual(my_endpoint.__wrapped__(2, 3), {"sum": 5})
        # Docstring and name preserved
        self.assertEqual(my_endpoint.__name__, "my_endpoint")
        self.assertEqual(my_endpoint.__doc__, "An endpoint.")


class TestFlatResponsePredicate(unittest.TestCase):
    """Sanity tests for the predicate itself."""

    def test_flat_dict_passes(self):
        assert_flat_response({"a": "x", "b": 1, "c": True, "d": None})

    def test_nested_dict_fails(self):
        with self.assertRaises(AssertionError) as ctx:
            assert_flat_response({"a": "x", "options": {"A": "1"}})
        self.assertIn("options", str(ctx.exception))
        self.assertIn("dict", str(ctx.exception))

    def test_list_fails(self):
        with self.assertRaises(AssertionError) as ctx:
            assert_flat_response({"a": "x", "items": ["a", "b"]})
        self.assertIn("items", str(ctx.exception))
        self.assertIn("list", str(ctx.exception))

    def test_none_can_be_disallowed(self):
        with self.assertRaises(AssertionError):
            assert_flat_response({"a": None}, allow_none=False)


# ════════════════════════════════════════════════════════════
# Endpoint shape contracts — assert each endpoint's return paths
# produce a flat response dict
# ════════════════════════════════════════════════════════════


class TestStartQuizFlatShape(unittest.TestCase):
    """start_quiz must produce flat responses for new + resume variants."""

    @patch("tap_lms.summer_program.student_progression_sp._get_language_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._get_question_details")
    @patch("tap_lms.summer_program.student_progression_sp._get_quiz_questions")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_new_quiz_response_is_flat(
        self, mock_frappe, mock_resolve, mock_get_questions, mock_get_q_details,
        mock_get_language
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_resolve.return_value = "STU-001"
        mock_get_language.return_value = "Hindi"
        mock_frappe.db.exists.return_value = True
        mock_frappe.db.get_value.return_value = {
            "name": "PROG-1",
            "stage": "LU-1",
            "current_week": 1,
            "current_tier": "Basic",
            "current_content_index": 0,
            "is_on_remedial": 0,
            "active_quiz_attempt": None,
        }
        mock_frappe.db.count.return_value = 0

        # Quiz doc
        quiz_doc = MagicMock()
        quiz_doc.quiz_name = "Q1"
        quiz_doc.passing_score = 60
        # Question rows
        q_row = MagicMock()
        q_row.question = "QN001"
        mock_get_questions.return_value = [q_row]
        mock_get_q_details.return_value = {
            "question": "What is X?",
            "question_type": "Multiple Choice",
            "option_a": "A",
            "option_b": "B",
            "option_c": "C",
            "correct_option": "A",
        }

        # frappe.get_doc returns the quiz_doc for "Quiz" lookup, then attempt
        attempt = MagicMock()
        attempt.name = "QA-1"
        attempt.quizname = "Q1"
        attempt.passing_score = 60
        mock_frappe.get_doc.side_effect = [quiz_doc, attempt]

        # The function does pe.insert(); make sure insert is a no-op on attempt
        attempt.insert = MagicMock()

        resp = api.start_quiz.__wrapped__("STU-001", "CL-1", "Q1", language="English")
        assert_flat_response(resp)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["status"], "quiz_started")
        # Spot-check the flattened option fields
        self.assertEqual(resp["option_a"], "A")
        self.assertNotIn("option_d", resp)
        self.assertEqual(resp["question_index"], 1)
        mock_get_language.assert_called_once_with("STU-001", "CL-1")
        mock_get_q_details.assert_called_once_with("QN001", "Hindi")

    @patch("tap_lms.summer_program.student_progression_sp._get_question_details")
    @patch("tap_lms.summer_program.student_progression_sp._get_quiz_questions")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_resume_quiz_omits_absent_option_fields(
        self, mock_frappe, mock_get_questions, mock_get_q_details
    ):
        from tap_lms.summer_program import student_progression_sp as api

        attempt = MagicMock()
        attempt.quiz = "Q1"
        attempt.name = "QA-1"
        attempt.quizname = "Q1"
        attempt.total_questions = 2
        attempt.answers = [SimpleNamespace(question_index=1, is_correct=1)]

        q1 = MagicMock()
        q1.question = "QN001"
        q2 = MagicMock()
        q2.question = "QN002"
        mock_get_questions.return_value = [q1, q2]
        mock_get_q_details.return_value = {
            "question": "What is next?",
            "question_type": "Multiple Choice",
            "option_a": "A",
            "option_b": "B",
            "option_c": "C",
            "correct_option": "A",
        }

        resp = api._resume_quiz(attempt, {"name": "PROG-1"}, "Hindi")

        assert_flat_response(resp)
        self.assertEqual(resp["status"], "quiz_resumed")
        self.assertEqual(resp["option_c"], "C")
        self.assertNotIn("option_d", resp)


class TestQuestionDetailsTranslations(unittest.TestCase):
    """_get_question_details should translate question and option text."""

    def test_question_details_uses_option_translations_for_language(self):
        from tap_lms.summer_program import student_progression_sp as api

        question = SimpleNamespace(
            question="Which is an example of a need?",
            question_type="Multiple Choice",
            correct_option=3,
            question_translations=[
                SimpleNamespace(
                    language="Hindi",
                    translated_question="Hindi question text",
                )
            ],
            options=[
                SimpleNamespace(options="OPT-1"),
                SimpleNamespace(options="OPT-2"),
                SimpleNamespace(options="OPT-3"),
                SimpleNamespace(options="OPT-4"),
            ],
        )
        option_docs = {
            "OPT-1": SimpleNamespace(
                option_text="Chocolate",
                option_translations=[
                    SimpleNamespace(language="Hindi", translated_option="Hindi Chocolate")
                ],
            ),
            "OPT-2": SimpleNamespace(
                option_text="Video game",
                option_translations=[
                    SimpleNamespace(language="Hindi", translated_option="Hindi Video game")
                ],
            ),
            "OPT-3": SimpleNamespace(
                option_text="Food",
                option_translations=[
                    SimpleNamespace(language="Hindi", translated_option="Hindi Food")
                ],
            ),
            "OPT-4": SimpleNamespace(
                option_text="Movie ticket",
                option_translations=[
                    SimpleNamespace(language="Hindi", translated_option="Hindi Movie ticket")
                ],
            ),
        }

        def get_doc(doctype, name):
            if doctype == "QuizQuestion":
                return question
            if doctype == "QuizOption":
                return option_docs[name]
            raise AssertionError(f"Unexpected get_doc({doctype!r}, {name!r})")

        with patch.object(api, "frappe") as mock_frappe:
            mock_frappe.get_doc.side_effect = get_doc

            resp = api._get_question_details("QN-1", "Hindi")

        self.assertEqual(resp["question"], "Hindi question text")
        self.assertEqual(resp["option_a"], "Hindi Chocolate")
        self.assertEqual(resp["option_b"], "Hindi Video game")
        self.assertEqual(resp["option_c"], "Hindi Food")
        self.assertEqual(resp["option_d"], "Hindi Movie ticket")
        self.assertEqual(resp["correct_option"], "C")

    def test_question_details_omits_absent_option_fields(self):
        from tap_lms.summer_program import student_progression_sp as api

        question = SimpleNamespace(
            question="Pick one",
            question_type="Multiple Choice",
            correct_option=2,
            question_translations=[],
            options=[
                SimpleNamespace(options="OPT-1"),
                SimpleNamespace(options="OPT-2"),
                SimpleNamespace(options="OPT-3"),
            ],
        )
        option_docs = {
            "OPT-1": SimpleNamespace(option_text="One", option_translations=[]),
            "OPT-2": SimpleNamespace(option_text="Two", option_translations=[]),
            "OPT-3": SimpleNamespace(option_text="Three", option_translations=[]),
        }

        def get_doc(doctype, name):
            if doctype == "QuizQuestion":
                return question
            if doctype == "QuizOption":
                return option_docs[name]
            raise AssertionError(f"Unexpected get_doc({doctype!r}, {name!r})")

        with patch.object(api, "frappe") as mock_frappe:
            mock_frappe.get_doc.side_effect = get_doc

            resp = api._get_question_details("QN-1", "English")

        self.assertEqual(resp["option_a"], "One")
        self.assertEqual(resp["option_b"], "Two")
        self.assertEqual(resp["option_c"], "Three")
        self.assertNotIn("option_d", resp)
        self.assertEqual(resp["correct_option"], "B")

    def test_question_details_ignores_options_after_d(self):
        from tap_lms.summer_program import student_progression_sp as api

        question = SimpleNamespace(
            question="Pick one",
            question_type="Multiple Choice",
            correct_option=5,
            question_translations=[],
            options=[
                SimpleNamespace(options="OPT-1"),
                SimpleNamespace(options="OPT-2"),
                SimpleNamespace(options="OPT-3"),
                SimpleNamespace(options="OPT-4"),
                SimpleNamespace(options="OPT-5"),
            ],
        )
        option_docs = {
            "OPT-1": SimpleNamespace(option_text="One", option_translations=[]),
            "OPT-2": SimpleNamespace(option_text="Two", option_translations=[]),
            "OPT-3": SimpleNamespace(option_text="Three", option_translations=[]),
            "OPT-4": SimpleNamespace(option_text="Four", option_translations=[]),
            "OPT-5": SimpleNamespace(option_text="Five", option_translations=[]),
        }

        def get_doc(doctype, name):
            if doctype == "QuizQuestion":
                return question
            if doctype == "QuizOption":
                return option_docs[name]
            raise AssertionError(f"Unexpected get_doc({doctype!r}, {name!r})")

        with patch.object(api, "frappe") as mock_frappe:
            mock_frappe.get_doc.side_effect = get_doc

            resp = api._get_question_details("QN-1", "English")

        self.assertEqual(resp["option_a"], "One")
        self.assertEqual(resp["option_b"], "Two")
        self.assertEqual(resp["option_c"], "Three")
        self.assertEqual(resp["option_d"], "Four")
        self.assertNotIn("option_e", resp)
        self.assertEqual(resp["correct_option"], "A")


class TestSubmitAnswerFlatShape(unittest.TestCase):
    """submit_answer must produce flat responses for both 'next_question'
    and 'quiz complete' variants."""

    @patch("tap_lms.summer_program.student_progression_sp._get_language_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._get_question_details")
    @patch("tap_lms.summer_program.student_progression_sp._get_quiz_questions")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_next_question_response_is_flat(
        self, mock_frappe, mock_resolve, mock_get_questions, mock_get_q_details,
        mock_get_language
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_resolve.return_value = "STU-001"
        mock_get_language.return_value = "Hindi"
        mock_frappe.db.exists.return_value = True

        attempt = MagicMock()
        attempt.student = "STU-001"
        attempt.course_level = "CL-1"
        attempt.status = "in_progress"
        attempt.total_questions = 3
        attempt.quiz = "Q1"
        attempt.question_started_at = None
        attempt.started_at = None
        attempt.answers = []
        attempt.correct_answers = 0
        attempt.student_progress = None
        attempt.append = MagicMock()
        attempt.save = MagicMock()

        quiz_doc = MagicMock()
        mock_frappe.get_doc.side_effect = [attempt, quiz_doc]

        q1 = MagicMock()
        q1.question = "QN001"
        q2 = MagicMock()
        q2.question = "QN002"
        mock_get_questions.return_value = [q1, q2, MagicMock()]

        mock_get_q_details.side_effect = [
            {  # current question details
                "correct_option": "A",
                "option_a": "A", "option_b": "B",
                "option_c": "C", "option_d": "D",
                "question": "Q1?",
                "question_type": "Multiple Choice",
            },
            {  # next question details
                "correct_option": "B",
                "option_a": "W", "option_b": "X",
                "option_c": "Y", "option_d": "Z",
                "question": "Q2?",
                "question_type": "Multiple Choice",
            },
        ]

        resp = api.submit_answer.__wrapped__("STU-001", "QA-1", 1, "A", language="English")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "next_question")
        self.assertEqual(resp["question_index"], 2)
        self.assertEqual(resp["option_b"], "X")
        mock_get_language.assert_called_once_with("STU-001", "CL-1")
        mock_get_q_details.assert_any_call("QN002", "Hindi")

    @patch("tap_lms.summer_program.student_progression_sp._get_language_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._get_question_details")
    @patch("tap_lms.summer_program.student_progression_sp._get_quiz_questions")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_next_question_omits_absent_option_fields(
        self, mock_frappe, mock_resolve, mock_get_questions, mock_get_q_details,
        mock_get_language
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_resolve.return_value = "STU-001"
        mock_get_language.return_value = "Hindi"
        mock_frappe.db.exists.return_value = True

        attempt = MagicMock()
        attempt.student = "STU-001"
        attempt.course_level = "CL-1"
        attempt.status = "in_progress"
        attempt.total_questions = 3
        attempt.quiz = "Q1"
        attempt.question_started_at = None
        attempt.started_at = None
        attempt.answers = []
        attempt.correct_answers = 0
        attempt.student_progress = None
        attempt.append = MagicMock()
        attempt.save = MagicMock()

        quiz_doc = MagicMock()
        mock_frappe.get_doc.side_effect = [attempt, quiz_doc]

        q1 = MagicMock()
        q1.question = "QN001"
        q2 = MagicMock()
        q2.question = "QN002"
        mock_get_questions.return_value = [q1, q2, MagicMock()]

        mock_get_q_details.side_effect = [
            {
                "correct_option": "A",
                "option_a": "A",
                "option_b": "B",
                "option_c": "C",
                "question": "Q1?",
                "question_type": "Multiple Choice",
            },
            {
                "correct_option": "B",
                "option_a": "W",
                "option_b": "X",
                "option_c": "Y",
                "question": "Q2?",
                "question_type": "Multiple Choice",
            },
        ]

        resp = api.submit_answer.__wrapped__("STU-001", "QA-1", 1, "A", language="English")

        assert_flat_response(resp)
        self.assertEqual(resp["status"], "next_question")
        self.assertEqual(resp["option_c"], "Y")
        self.assertNotIn("option_d", resp)


class TestGetContentDetailsFlatShape(unittest.TestCase):
    """get_content_details has 6 content-type branches; all must be flat.
    `assessments` array (was the only nested field) was dropped in this refactor."""

    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_video_class_response_is_flat(self, mock_frappe):
        from tap_lms.summer_program import student_progression_sp as api

        mock_frappe.db.exists.return_value = True
        doc = MagicMock()
        doc.video_name = "V1"
        doc.video_youtube_url = "https://youtu.be/x"
        doc.video_plio_url = None
        doc.video_file = None
        doc.duration = 300
        doc.description = "..."
        doc.video_translations = []
        mock_frappe.get_doc.return_value = doc

        resp = api.get_content_details.__wrapped__("VideoClass", "VC-1")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "video_class")
        self.assertNotIn("assessments", resp,
                         "assessments[] array must not be in flat response")

    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_quiz_response_is_flat(self, mock_frappe):
        from tap_lms.summer_program import student_progression_sp as api

        mock_frappe.db.exists.return_value = True
        doc = MagicMock()
        doc.questions = [MagicMock(), MagicMock(), MagicMock()]
        doc.quiz_name = "Q1"
        doc.passing_score = 60
        doc.time_limit = None
        mock_frappe.get_doc.return_value = doc

        resp = api.get_content_details.__wrapped__("Quiz", "Q1")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "quiz")
        self.assertEqual(resp["total_questions"], 3)
        self.assertNotIn("questions", resp,
                         "questions[] array must not be in flat response — "
                         "use start_quiz to walk through questions")


class TestAdvanceToNextContentFlatShape(unittest.TestCase):
    """_advance_to_next_content is the helper called by complete_content and
    _complete_quiz_sp. All 3 variants (next_content, next_learning_unit,
    week_complete) must be flat."""

    @patch("tap_lms.summer_program.student_progression_sp._get_content_items")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_next_content_variant_is_flat(self, mock_frappe, mock_items):
        from tap_lms.summer_program import student_progression_sp as api

        mock_items.return_value = [
            {"content_type": "VideoClass", "content_id": "VC-1", "content_name": "Vid 1", "is_optional": 0},
            {"content_type": "Quiz", "content_id": "Q1", "content_name": "Quiz 1", "is_optional": 0},
        ]

        progress = {
            "name": "PROG-1",
            "current_content_index": 0,
            "current_week": 1,
            "current_tier": "Basic",
            "stage": "LU-1",
        }
        resp = api._advance_to_next_content(progress, "CL-1")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "next_content")
        # Spot-check the flattened next_content_* fields
        self.assertEqual(resp["next_content_type"], "Quiz")
        self.assertEqual(resp["next_content_id"], "Q1")
        self.assertEqual(resp["progress_completed"], 1)

    @patch("tap_lms.summer_program.student_progression_sp._get_next_learning_unit")
    @patch("tap_lms.summer_program.student_progression_sp._get_content_items")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_week_complete_variant_is_flat(
        self, mock_frappe, mock_items, mock_next_lu
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_items.return_value = [
            {"content_type": "VideoClass", "content_id": "VC-1", "content_name": "Vid 1", "is_optional": 0},
        ]
        mock_next_lu.return_value = None  # no more LUs

        progress = {
            "name": "PROG-1",
            "current_content_index": 0,
            "current_week": 1,
            "current_tier": "Basic",
            "stage": "LU-1",
        }
        resp = api._advance_to_next_content(progress, "CL-1")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "week_complete")
        self.assertEqual(resp["completed_week"], 1)


class TestErrorPathFlatShape(unittest.TestCase):
    """Per Rule 6: every error path must also be flat AND use the
    status + error_detail envelope (not the legacy `error` key)."""

    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_start_quiz_validation_error_uses_status_envelope(
        self, mock_frappe, mock_resolve
    ):
        from tap_lms.summer_program import student_progression_sp as api

        # Missing required params → invalid_input
        resp = api.start_quiz.__wrapped__("", "", "")
        assert_flat_response(resp)
        self.assertFalse(resp["success"])
        self.assertEqual(resp["status"], "invalid_input")
        self.assertIn("error_detail", resp)
        self.assertNotIn(
            "error", resp,
            "legacy 'error' key must not appear — use 'error_detail'",
        )

    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_start_quiz_student_not_found_uses_status_envelope(
        self, mock_frappe, mock_resolve
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_resolve.return_value = None
        resp = api.start_quiz.__wrapped__("STU-???", "CL-1", "Q1")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "not_found")
        self.assertNotIn("error", resp)

    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_submit_answer_invalid_answer_uses_status_envelope(
        self, mock_frappe, mock_resolve
    ):
        from tap_lms.summer_program import student_progression_sp as api

        # Answer not in A/B/C/D → invalid_answer
        resp = api.submit_answer.__wrapped__("STU-1", "QA-1", 1, "Z")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "invalid_answer")
        self.assertNotIn("error", resp)

    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_get_content_details_invalid_content_type(self, mock_frappe):
        from tap_lms.summer_program import student_progression_sp as api

        resp = api.get_content_details.__wrapped__("NotARealType", "X-1")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "invalid_content_type")
        self.assertNotIn("error", resp)

    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_get_content_details_not_found(self, mock_frappe):
        from tap_lms.summer_program import student_progression_sp as api

        mock_frappe.db.exists.return_value = False
        resp = api.get_content_details.__wrapped__("Quiz", "MISSING-Q")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "not_found")
        self.assertNotIn("error", resp)


class TestAdvanceToNextLearningUnitFlatShape(unittest.TestCase):
    """H3 follow-up: cover the `next_learning_unit` variant of
    _advance_to_next_content (the case where current LU is exhausted but
    another LU exists for the same week/tier)."""

    @patch("tap_lms.summer_program.student_progression_sp._get_learning_unit_info")
    @patch("tap_lms.summer_program.student_progression_sp._get_next_learning_unit")
    @patch("tap_lms.summer_program.student_progression_sp._get_content_items")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_next_learning_unit_variant_is_flat(
        self, mock_frappe, mock_items, mock_next_lu, mock_lu_info
    ):
        from tap_lms.summer_program import student_progression_sp as api

        # Current LU has 1 item; new_index would be 1 == len → no more items
        # in current LU. _get_next_learning_unit returns a new LU.
        mock_items.side_effect = [
            # First call: items in current LU (1 item, current_content_index = 0)
            [{"content_type": "VideoClass", "content_id": "VC-1",
              "content_name": "Vid 1", "is_optional": 0}],
            # Second call: items in the NEXT LU (for first_content lookup)
            [{"content_type": "Quiz", "content_id": "Q1",
              "content_name": "Quiz 1", "is_optional": 0}],
        ]
        mock_next_lu.return_value = "LU-2"
        mock_lu_info.return_value = {"name": "Learning Unit 2"}

        progress = {
            "name": "PROG-1",
            "current_content_index": 0,
            "current_week": 1,
            "current_tier": "Basic",
            "stage": "LU-1",
        }
        resp = api._advance_to_next_content(progress, "CL-1")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "next_learning_unit")
        self.assertEqual(resp["new_learning_unit"], "LU-2")
        self.assertEqual(resp["new_learning_unit_name"], "Learning Unit 2")
        # Flattened next_content_* fields
        self.assertEqual(resp["next_content_type"], "Quiz")
        self.assertEqual(resp["next_content_id"], "Q1")
        self.assertEqual(resp["next_content_order"], 1)


class TestCompleteQuizSpRenames(unittest.TestCase):
    """B1 regression: verify _complete_quiz_sp no longer has a `quiz_passed`
    boolean field colliding with the `status` enum, and uses
    `next_action_status` instead of the ambiguous `next_action` key."""

    def test_quiz_passed_bool_field_removed(self):
        """The boolean `quiz_passed` field was removed because it collided
        with the status enum value "quiz_passed" / "quiz_failed". Flows
        branch on status, not on the bool field."""
        from tap_lms.summer_program import student_progression_sp as api
        import inspect
        source = inspect.getsource(api._complete_quiz_sp)
        # The string "quiz_passed" appears in the status enum value but NOT
        # as a key holding a bool. Check that there's no `"quiz_passed": passed`
        # in the response-build code.
        self.assertNotIn(
            '"quiz_passed": passed',
            source,
            "Boolean quiz_passed field should be removed — it collides with "
            "the status enum value 'quiz_passed'. Branch on status instead.",
        )

    def test_next_action_renamed_to_next_action_status(self):
        from tap_lms.summer_program import student_progression_sp as api
        import inspect
        source = inspect.getsource(api._complete_quiz_sp)
        self.assertIn(
            'response["next_action_status"]',
            source,
            "The merged child status should be assigned to next_action_status, "
            "not the ambiguous next_action key.",
        )


class TestGetWeeklyContentFlatShape(unittest.TestCase):
    """get_weekly_content must produce a flat response where content_items[]
    is expanded to content_<i>_type / content_<i>_id / etc."""

    @patch("tap_lms.summer_program.student_progression_sp._get_or_create_sp_progress")
    @patch("tap_lms.summer_program.student_progression_sp._get_week_rule")
    @patch("tap_lms.summer_program.student_progression_sp._get_content_items")
    @patch("tap_lms.summer_program.student_progression_sp._get_learning_unit")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_path")
    @patch("tap_lms.summer_program.student_progression_sp._get_current_week")
    @patch("tap_lms.summer_program.student_progression_sp._get_course_level_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._get_active_bpr_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_content_items_flattened_to_suffixed_keys(
        self, mock_frappe, mock_resolve, mock_bpr, mock_cl,
        mock_week, mock_path, mock_lu, mock_items, mock_wr, mock_progress,
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_resolve.return_value = "STU-001"
        batch = MagicMock()
        batch.total_weeks = 8
        bpr = MagicMock()
        mock_bpr.return_value = (batch, bpr)
        mock_cl.return_value = "CL-1"
        mock_week.return_value = 1
        mock_path.return_value = "Core"
        mock_lu.return_value = "LU-1"
        mock_items.return_value = [
            {"content_type": "VideoClass", "content_id": "VC-1", "content_name": "Vid", "is_optional": 0},
            {"content_type": "Quiz", "content_id": "Q1", "content_name": "Quiz", "is_optional": 0},
        ]
        mock_wr.return_value = {"expected_submission_type": "photo", "submission_validation_enabled": 1}
        mock_frappe.get_doc.return_value = MagicMock()
        mock_frappe.db.get_value.return_value = "Week 1: Basics"

        resp = api.get_weekly_content.__wrapped__("STU-001")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "content_available")
        self.assertEqual(resp["content_count"], 2)
        self.assertEqual(resp["content_1_type"], "VideoClass")
        self.assertEqual(resp["content_1_id"], "VC-1")
        self.assertEqual(resp["content_2_type"], "Quiz")
        # The old nested key must NOT appear
        self.assertNotIn("content_items", resp,
                         "content_items[] array must not be in flat response")


class TestGetContentDetailsAssessmentsPreserved(unittest.TestCase):
    """Critical regression: get_content_details for VideoClass MUST return
    `assessment_<i>_id` because that's the assignment_id Glific passes to
    save_submission. Dropping it (an earlier mistake) broke the submission flow."""

    @patch("tap_lms.summer_program.student_progression_sp._get_video_assessments")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_video_class_includes_assessment_ids_as_flat_suffixed_keys(
        self, mock_frappe, mock_assessments
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_frappe.db.exists.return_value = True
        doc = MagicMock()
        doc.video_name = "V1"
        doc.video_youtube_url = "https://youtu.be/x"
        doc.video_plio_url = None
        doc.video_file = None
        doc.duration = 300
        doc.description = "..."
        doc.video_translations = []
        mock_frappe.get_doc.return_value = doc

        # Simulate 2 assessments linked to the video
        mock_assessments.return_value = [
            {"assessment_type": "Assignment", "assessment_id": "ASN-001"},
            {"assessment_type": "CourseProject", "assessment_id": "CP-002"},
        ]

        resp = api.get_content_details.__wrapped__("VideoClass", "VC-1")
        assert_flat_response(resp)
        self.assertEqual(resp["assessment_count"], 2)
        self.assertEqual(resp["assessment_1_type"], "Assignment")
        self.assertEqual(resp["assessment_1_id"], "ASN-001",
                         "assignment_id is the input to save_submission — "
                         "MUST be preserved as a top-level flat key.")
        self.assertEqual(resp["assessment_2_type"], "CourseProject")
        self.assertEqual(resp["assessment_2_id"], "CP-002")
        # And the array form is GONE
        self.assertNotIn("assessments", resp)

    @patch("tap_lms.summer_program.student_progression_sp._get_video_assessments")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_video_class_no_assessments_returns_zero_count(
        self, mock_frappe, mock_assessments
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_frappe.db.exists.return_value = True
        doc = MagicMock()
        doc.video_name = "V1"
        doc.video_youtube_url = None
        doc.video_plio_url = None
        doc.video_file = None
        doc.duration = None
        doc.description = ""
        doc.video_translations = []
        mock_frappe.get_doc.return_value = doc

        mock_assessments.return_value = None  # no assessments

        resp = api.get_content_details.__wrapped__("VideoClass", "VC-1")
        assert_flat_response(resp)
        self.assertEqual(resp["assessment_count"], 0)
        # No assessment_<i>_* keys when count is 0
        self.assertNotIn("assessment_1_id", resp)


class TestGetContentDetailsLanguageResolution(unittest.TestCase):
    """Language for content details is resolved once at the endpoint boundary."""

    def test_resolves_language_from_input_then_pe_then_english(self):
        from tap_lms.summer_program import student_progression_sp as api

        with patch.object(api, "resolve_student") as mock_resolve, \
                patch.object(api, "get_active_pe") as mock_get_active_pe:
            self.assertEqual(
                api._resolve_content_language("Marathi", "GLIFIC-1"),
                "Marathi",
            )
            mock_resolve.assert_not_called()
            mock_get_active_pe.assert_not_called()

            mock_resolve.return_value = "STU-001"
            pe = MagicMock()
            pe.language = "Hindi"
            mock_get_active_pe.return_value = pe
            self.assertEqual(
                api._resolve_content_language(None, "GLIFIC-1"),
                "Hindi",
            )

            pe.language = None
            self.assertEqual(
                api._resolve_content_language(None, "GLIFIC-1"),
                "English",
            )

    def test_unguided_message_requires_resolved_language_argument(self):
        from tap_lms.summer_program import student_progression_sp as api

        with patch.object(api, "resolve_student") as mock_resolve, \
                patch.object(api, "get_active_pe") as mock_get_active_pe, \
                patch.object(api, "frappe") as mock_frappe:
            mock_resolve.return_value = "STU-001"
            pe = MagicMock()
            pe.language = "Hindi"
            pe.current_expected_submission_type = "video"
            mock_get_active_pe.return_value = pe

            response = api._get_video_unguided_submission_message(
                "GLIFIC-1",
                [{"assessment_type": "Assignment", "assessment_id": "ASN-001"}],
                language=None,
            )

            self.assertEqual(
                response,
                {"unguided_text": "Not Found", "unguided_text_url": "Not Found"},
            )
            mock_frappe.get_all.assert_not_called()


class TestGetNextContentFlatShape(unittest.TestCase):
    """Task #68 — `get_next_content` previously returned nested `position` and
    `content` objects (plus `assessments[]` array inside content) for 3 variants
    (quiz_in_progress, content_available current LU, content_available next LU).
    All flattened now."""

    @patch("tap_lms.summer_program.student_progression_sp._get_or_create_sp_progress")
    @patch("tap_lms.summer_program.student_progression_sp._get_learning_unit")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_path")
    @patch("tap_lms.summer_program.student_progression_sp._get_effective_week")
    @patch("tap_lms.summer_program.student_progression_sp._get_current_week")
    @patch("tap_lms.summer_program.student_progression_sp._get_course_level_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._get_active_bpr_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_quiz_in_progress_variant_is_flat(
        self, mock_frappe, mock_resolve, mock_bpr, mock_cl, mock_week,
        mock_effective, mock_path, mock_lu, mock_progress,
    ):
        from tap_lms.summer_program import student_progression_sp as api

        mock_resolve.return_value = "STU-001"
        student = MagicMock()
        student.name = "STU-001"
        batch = MagicMock()
        batch.total_weeks = 8
        bpr = MagicMock()
        mock_frappe.get_doc.return_value = student
        mock_bpr.return_value = (batch, bpr)
        mock_cl.return_value = "CL-1"
        mock_week.return_value = 1
        mock_effective.return_value = 1
        mock_path.return_value = "Core"
        mock_lu.return_value = "LU-1"
        mock_progress.return_value = "PROG-1"

        # Progress data has an active quiz attempt
        mock_frappe.db.get_value.return_value = {
            "name": "PROG-1",
            "student": "STU-001",
            "stage": "Understanding Money-Basic-0",
            "status": "in_progress",
            "current_week": 1,
            "current_tier": "Basic",
            "current_content_index": 0,
            "is_on_remedial": 0,
            "remedial_attempts": 0,
            "active_content_type": "Quiz",
            "active_content_id": "BasicQuiz_Quiz_B-basic",
            "content_started_at": None,
            "active_quiz_attempt": "2rscijc6nd",
            "question_started_at": None,
            "course_context": "CL-1",
        }

        resp = api.get_next_content.__wrapped__("STU-001")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "quiz_in_progress")
        # Flattened position.* (was the user-reported nesting)
        self.assertEqual(resp["position_week"], 1)
        self.assertEqual(resp["position_tier"], "Basic")
        self.assertEqual(resp["position_learning_unit"], "Understanding Money-Basic-0")
        self.assertFalse(resp["position_is_remedial"])
        self.assertEqual(resp["position_path"], "Core")
        # Flattened content.*
        self.assertEqual(resp["content_type"], "Quiz")
        self.assertEqual(resp["content_id"], "BasicQuiz_Quiz_B-basic")
        # Other top-level fields preserved
        self.assertTrue(resp["has_active_quiz"])
        self.assertEqual(resp["quiz_attempt_id"], "2rscijc6nd")
        # Nested keys MUST be absent
        self.assertNotIn("position", resp)
        self.assertNotIn("content", resp)

    @patch("tap_lms.summer_program.student_progression_sp._get_video_assessments")
    @patch("tap_lms.summer_program.student_progression_sp._get_learning_unit_info")
    @patch("tap_lms.summer_program.student_progression_sp._get_content_items")
    @patch("tap_lms.summer_program.student_progression_sp._get_or_create_sp_progress")
    @patch("tap_lms.summer_program.student_progression_sp._get_learning_unit")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_path")
    @patch("tap_lms.summer_program.student_progression_sp._get_effective_week")
    @patch("tap_lms.summer_program.student_progression_sp._get_current_week")
    @patch("tap_lms.summer_program.student_progression_sp._get_course_level_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._get_active_bpr_for_student")
    @patch("tap_lms.summer_program.student_progression_sp._resolve_student_id")
    @patch("tap_lms.summer_program.student_progression_sp.frappe")
    def test_content_available_variant_flat_with_assessments(
        self, mock_frappe, mock_resolve, mock_bpr, mock_cl, mock_week,
        mock_effective, mock_path, mock_lu, mock_progress,
        mock_items, mock_lu_info, mock_assessments,
    ):
        """content_available for a VideoClass with linked assessments — the
        most-used variant. assessment_<i>_id must be flat and present."""
        from tap_lms.summer_program import student_progression_sp as api

        mock_resolve.return_value = "STU-001"
        student = MagicMock()
        student.name = "STU-001"
        batch = MagicMock()
        batch.total_weeks = 8
        bpr = MagicMock()
        mock_frappe.get_doc.return_value = student
        mock_bpr.return_value = (batch, bpr)
        mock_cl.return_value = "CL-1"
        mock_week.return_value = 1
        mock_effective.return_value = 1
        mock_path.return_value = "Core"
        mock_lu.return_value = "LU-1"
        mock_progress.return_value = "PROG-1"

        mock_frappe.db.get_value.return_value = {
            "name": "PROG-1",
            "student": "STU-001",
            "stage": "LU-1",
            "status": "in_progress",
            "current_week": 1,
            "current_tier": "Basic",
            "current_content_index": 0,
            "is_on_remedial": 0,
            "remedial_attempts": 0,
            "active_content_type": None,
            "active_content_id": None,
            "content_started_at": None,
            "active_quiz_attempt": None,
            "question_started_at": None,
            "course_context": "CL-1",
        }
        mock_items.return_value = [
            {"content_type": "VideoClass", "content_id": "VC-1",
             "content_name": "Vid 1", "is_optional": 0},
        ]
        mock_lu_info.return_value = {"name": "LU 1: Money Basics"}
        mock_assessments.return_value = [
            {"assessment_type": "Assignment", "assessment_id": "ASN-W1-001"},
        ]

        resp = api.get_next_content.__wrapped__("STU-001")
        assert_flat_response(resp)
        self.assertEqual(resp["status"], "content_available")
        # Flattened position
        self.assertEqual(resp["position_week"], 1)
        self.assertEqual(resp["position_learning_unit_name"], "LU 1: Money Basics")
        # Flattened content
        self.assertEqual(resp["content_type"], "VideoClass")
        self.assertEqual(resp["content_id"], "VC-1")
        self.assertEqual(resp["content_order"], 1)
        # Flattened assessments — assignment_id preserved as flat key
        self.assertEqual(resp["assessment_count"], 1)
        self.assertEqual(resp["assessment_1_type"], "Assignment")
        self.assertEqual(resp["assessment_1_id"], "ASN-W1-001")
        # No nested objects
        self.assertNotIn("position", resp)
        self.assertNotIn("content", resp)
        self.assertNotIn("assessments", resp)


class TestGetStudentStateCR002V2FlatShape(unittest.TestCase):
    """CR-002 v2 §"API surface": get_student_state response must include the 8
    new gamification fields at top level (flat-map per
    docs/api-standard-glific.md Rule 1) so SP_Incoming_Router can read
    @results.webhook.<field> as a fallback when contact-field cache is stale.

    `weekly_video_done` is intentionally NOT in the response — it's an
    internal-only flag the streak/gem state machine uses; Glific never sees it.
    """

    EXPECTED_NEW_FIELDS = {
        "total_activity_points",
        "weekly_activity_points",
        "total_quiz_points",
        "weekly_quiz_points",
        "total_submission_points",
        "weekly_submission_points",
        "special_gems",
        "weekly_submission_done",
        "bonus_quiz_points",
    }

    @patch("tap_lms.summer_program.program_enrollment_api._resolve_student")
    @patch("tap_lms.summer_program.program_enrollment_api.frappe")
    def test_response_includes_all_eight_new_fields_and_is_flat(
        self, mock_frappe, mock_resolve,
    ):
        from tap_lms.summer_program import program_enrollment_api as api

        mock_resolve.return_value = "STU-001"

        # Mock PE data — include the 8 new fields plus a sample of existing.
        pe_data = {
            "name": "PE-1", "batch": "BATCH-1", "program_type": "Summer",
            "archetype": "Submitter", "experiment_arm": "default",
            "resolved_flow_state": "normal_content_delivery",
            "journey_label": "content_delivered", "program_status": "active",
            "current_week": 1, "current_path": "Core", "current_tier": "Basic",
            "total_points": 50, "current_streak": 2, "in_grace_window": 0,
            "grace_window_end_at": None,
            "current_expected_submission_type": "photo",
            "submission_count": 1, "current_escalation_step": 0,
            "current_escalation_type": "",
            "course_level": "CL-1", "language": "English", "glific_id": "G-1",
            # CR-002 v2 fields
            "total_activity_points": 20, "weekly_activity_points": 10,
            "total_quiz_points": 5, "weekly_quiz_points": 5,
            "total_submission_points": 25, "weekly_submission_points": 25,
            "special_gems": 1, "weekly_submission_done": 1,
            "bonus_quiz_points": 0,
        }
        mock_frappe.db.get_value.return_value = pe_data

        # Capture writes to frappe.local.response
        captured = {}
        mock_frappe.local.response = captured

        api.get_student_state("STU-001")

        # All 8 new fields appear at top level
        for fld in self.EXPECTED_NEW_FIELDS:
            self.assertIn(
                fld, captured,
                f"get_student_state response missing CR-002 v2 field '{fld}'",
            )

        # `weekly_video_done` is internal-only and MUST NOT leak to the response
        self.assertNotIn(
            "weekly_video_done", captured,
            "weekly_video_done is an internal sticky flag — it must never be "
            "exposed via get_student_state or the Glific contact-field push.",
        )

        # Flat-map contract: every value is a scalar, no nested dicts or lists
        assert_flat_response(captured)

    @patch("tap_lms.summer_program.program_enrollment_api._resolve_student")
    @patch("tap_lms.summer_program.program_enrollment_api.frappe")
    def test_response_preserves_last_escalation_step_key_for_glific_contract(
        self, mock_frappe, mock_resolve,
    ):
        """L-008 (Glific public contract): the response key is
        `last_escalation_step`, NOT `current_escalation_step`, even though the
        PE column was renamed. Glific flows read
        `@results.webhook.last_escalation_step` from SP_Incoming_Router and
        elsewhere; silently renaming the response key would break every flow
        reading it. Re-keying happens in get_student_state before the dict
        spreads into frappe.local.response.
        """
        from tap_lms.summer_program import program_enrollment_api as api

        mock_resolve.return_value = "STU-001"
        mock_frappe.db.get_value.return_value = {
            "name": "PE-1", "batch": "BATCH-1", "program_type": "Summer",
            "archetype": "Submitter", "experiment_arm": "default",
            "resolved_flow_state": "normal_escalation",
            "journey_label": "content_delivered", "program_status": "active",
            "current_week": 1, "current_path": "Core", "current_tier": "Basic",
            "total_points": 0, "current_streak": 0, "in_grace_window": 0,
            "grace_window_end_at": None,
            "current_expected_submission_type": "photo",
            "submission_count": 0,
            "current_escalation_step": 2,
            "current_escalation_type": "voice_note",
            "course_level": "CL-1", "language": "English", "glific_id": "G-1",
            "total_activity_points": 0, "weekly_activity_points": 0,
            "total_quiz_points": 0, "weekly_quiz_points": 0,
            "total_submission_points": 0, "weekly_submission_points": 0,
            "special_gems": 0, "weekly_submission_done": 0,
            "bonus_quiz_points": 0,
        }
        captured = {}
        mock_frappe.local.response = captured

        api.get_student_state("STU-001")

        # Glific-facing key MUST be present
        self.assertIn(
            "last_escalation_step", captured,
            "Response missing the Glific public-contract key "
            "`last_escalation_step` — SP_Incoming_Router reads this. The PE "
            "column was renamed to `current_escalation_step` but the API key "
            "must stay (L-008).",
        )
        self.assertEqual(captured["last_escalation_step"], 2)

        # The internal column name MUST NOT leak
        self.assertNotIn(
            "current_escalation_step", captured,
            "Internal PE column `current_escalation_step` leaked into the "
            "API response. Re-key to `last_escalation_step` before spreading "
            "into frappe.local.response.",
        )

        # New field uses its natural name — that one is fresh contract surface
        self.assertIn("current_escalation_type", captured)
        self.assertEqual(captured["current_escalation_type"], "voice_note")

    @patch("tap_lms.summer_program.program_enrollment_api._resolve_student")
    @patch("tap_lms.summer_program.program_enrollment_api.frappe")
    def test_response_total_points_includes_bonus_quiz_points(
        self, mock_frappe, mock_resolve,
    ):
        """get_student_state is the Glific fallback; its public total must
        include bonus_quiz_points even if the stored total_points column is
        stale or was written before the bonus stream existed."""
        from tap_lms.summer_program import program_enrollment_api as api

        mock_resolve.return_value = "STU-001"
        mock_frappe.db.get_value.side_effect = [
            {
                "name": "PE-1", "batch": "BATCH-1", "program_type": "Summer",
                "archetype": "Submitter", "experiment_arm": "default",
                "resolved_flow_state": "normal_content_delivery",
                "journey_label": "content_delivered", "program_status": "active",
                "current_week": 1, "current_path": "Core", "current_tier": "Basic",
                # Stale stored total: 20 + 5 + 25, missing +10 bonus.
                "total_points": 50, "current_streak": 0, "in_grace_window": 0,
                "grace_window_end_at": None,
                "current_expected_submission_type": "photo",
                "submission_count": 0,
                "current_escalation_step": 0,
                "current_escalation_type": "",
                "course_level": "CL-1", "language": None, "glific_id": "G-1",
                "total_activity_points": 20, "weekly_activity_points": 0,
                "total_quiz_points": 5, "weekly_quiz_points": 0,
                "total_submission_points": 25, "weekly_submission_points": 0,
                "special_gems": 0, "weekly_submission_done": 0,
                "bonus_quiz_points": 10,
            },
            "Student One",
            "BATCH01",
        ]
        captured = {}
        mock_frappe.local.response = captured

        api.get_student_state("STU-001")

        self.assertEqual(captured["bonus_quiz_points"], 10)
        self.assertEqual(captured["total_points"], 60)


if __name__ == "__main__":
    unittest.main()
