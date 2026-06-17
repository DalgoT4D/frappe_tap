"""
CR-024 Phase 2 — in-process read cache for immutable master data.

ADR-006 Revision (2026-06-08): synchronous lru_cache-backed functions for
the three SP endpoints that timed out in the 2026-06-04 incident:
  get_content_details, start_quiz, submit_answer.

Test coverage:
  1. Golden: cached output == uncached helper output for same inputs.
  2. Cache-hit avoids re-query: frappe.get_doc called ONCE for 2 calls.
  3. clear_master_data_cache() forces re-query (2 separate DB calls).
  4. Mutation safety: mutating returned dict does not corrupt cache.
  5. Language keying: same question_id + two languages → two distinct entries.
  6. Endpoint regression: get_content_details and start_quiz/submit_answer
     return same response shape/values as before the cache.
  7. get_cache_info() observability.

Design notes:
  - All tests mock frappe.get_doc / frappe.get_all — no bench DB required.
  - No frappe.db.commit() calls (L-017).
  - Mocks are scoped to each test so cache state is isolated.
  - clear_master_data_cache() is called in setUp so each test starts cold.
"""
import unittest
from unittest.mock import patch, MagicMock


# ============================================================
# HELPERS — minimal Frappe doc-like objects
# ============================================================

def _make_quiz_question(question_id="Q-001", question_text="What is 2+2?",
                         question_type="Multiple Choice", correct_option=2,
                         options=None, translations=None):
    """Build a minimal QuizQuestion-like MagicMock."""
    q = MagicMock()
    q.question = question_text
    q.question_type = question_type
    q.correct_option = correct_option   # 1-based integer
    q.question_translations = translations or []
    # Build options child rows
    if options is None:
        options = ["One", "Four", "Three", "Two"]
    opt_rows = []
    for text in options:
        opt_mock = MagicMock()
        opt_mock.options = f"OPT-{text[:4]}"
        # No translations on options by default
        opt_mock.option_translations = []
        opt_rows.append(opt_mock)
    q.options = opt_rows
    return q, [t for t in options]


def _make_option_doc(option_text, translations=None):
    """Build a minimal QuizOption-like MagicMock."""
    o = MagicMock()
    o.option_text = option_text
    o.option_translations = translations or []
    return o


def _make_video_doc(video_name="TestVideo", youtube_url="https://yt.com/v=x",
                    plio_url=None, video_file=None, duration="5.00",
                    description="A test video"):
    """Build a minimal VideoClass-like MagicMock."""
    doc = MagicMock()
    doc.video_name = video_name
    doc.video_youtube_url = youtube_url
    doc.video_plio_url = plio_url
    doc.video_file = video_file
    doc.duration = duration
    doc.description = description
    doc.video_translations = []
    return doc


def _make_quiz_doc(quiz_name="TestQuiz", passing_score=60.0, time_limit=None,
                   question_count=3):
    """Build a minimal Quiz-like MagicMock."""
    doc = MagicMock()
    doc.quiz_name = quiz_name
    doc.passing_score = passing_score
    doc.time_limit = time_limit
    q_rows = []
    for i in range(question_count):
        row = MagicMock()
        row.question = f"Q-{i+1:03d}"
        row.idx = i + 1
        row.question_number = i + 1
        q_rows.append(row)
    doc.questions = q_rows
    return doc


def _make_note_doc(note_name="TestNote", content="Note body text."):
    doc = MagicMock()
    doc.note_name = note_name
    doc.content = content
    return doc


# ============================================================
# 1. GOLDEN TESTS — cache output == live helper output
# ============================================================

class TestCachedQuestionDetailsGolden(unittest.TestCase):
    """cached_question_details output matches what _get_question_details produces."""

    def setUp(self):
        # Reset cache before each test to avoid cross-test contamination.
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def _build_get_doc_side_effect(self, q_doc, option_docs):
        """Return a get_doc side effect that dispatches by doctype."""
        option_iter = iter(option_docs)
        def _side_effect(doctype, name=None):
            if doctype == "QuizQuestion":
                return q_doc
            if doctype == "QuizOption":
                return next(option_iter)
            raise ValueError(f"Unexpected get_doc({doctype!r})")
        return _side_effect

    def test_golden_english_question(self):
        """cached output matches expected payload for English question."""
        from tap_lms.summer_program.master_data_lookup import cached_question_details

        q_doc, option_texts = _make_quiz_question(
            question_id="Q-ENG-001",
            question_text="What is photosynthesis?",
            correct_option=1,
            options=["Process by which plants make food", "Rain cycle",
                     "Evaporation", "Combustion"],
        )
        opt_docs = [_make_option_doc(t) for t in option_texts]

        opt_iter = iter(opt_docs)

        def _get_doc(doctype, name=None):
            if doctype == "QuizQuestion":
                return q_doc
            # Each QuizOption get_doc call returns the next option in order
            return next(opt_iter, _make_option_doc(""))

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   side_effect=_get_doc):
            result = cached_question_details("Q-ENG-001", "English")

        self.assertEqual(result["question"], "What is photosynthesis?")
        self.assertEqual(result["question_type"], "Multiple Choice")
        self.assertEqual(result["correct_option"], "A")   # correct_option=1 → A
        self.assertIn("option_a", result)
        self.assertEqual(result["option_a"], "Process by which plants make food")

    def test_golden_hindi_translation(self):
        """cached output for Hindi uses the translated question text."""
        from tap_lms.summer_program.master_data_lookup import cached_question_details

        hindi_trans = MagicMock()
        hindi_trans.language = "Hindi"
        hindi_trans.translated_question = "प्रकाश संश्लेषण क्या है?"

        q_doc, option_texts = _make_quiz_question(
            question_id="Q-HI-001",
            question_text="What is photosynthesis?",
            correct_option=1,
            translations=[hindi_trans],
        )
        opt_docs = [_make_option_doc(t) for t in option_texts]

        opt_iter = iter(opt_docs)

        def _get_doc(doctype, name=None):
            if doctype == "QuizQuestion":
                return q_doc
            return next(opt_iter, _make_option_doc(""))

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   side_effect=_get_doc):
            result = cached_question_details("Q-HI-001", "Hindi")

        self.assertEqual(result["question"], "प्रकाश संश्लेषण क्या है?",
                         "Hindi translation must be used for Hindi language")

    def test_golden_video_class_payload(self):
        """cached_content_details_payload returns correct VideoClass fields."""
        from tap_lms.summer_program.master_data_lookup import (
            cached_content_details_payload, clear_master_data_cache,
        )
        clear_master_data_cache()

        doc = _make_video_doc(
            video_name="Week 1 Video",
            youtube_url="https://youtube.com/watch?v=abc123",
        )
        assessment_row = MagicMock()
        assessment_row.assessment_type = "Assignment"
        assessment_row.assessment = "ASN-001"

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   return_value=doc), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_all",
                   return_value=[assessment_row]):
            result = cached_content_details_payload("VideoClass", "VC-001")

        self.assertEqual(result["youtube_url"], "https://youtube.com/watch?v=abc123")
        self.assertEqual(result["name"], "Week 1 Video")
        self.assertIn("assessments", result)
        self.assertEqual(len(result["assessments"]), 1)
        self.assertEqual(result["assessments"][0]["assessment_id"], "ASN-001")

    def test_golden_quiz_payload(self):
        """cached_content_details_payload returns correct Quiz fields."""
        from tap_lms.summer_program.master_data_lookup import (
            cached_content_details_payload, clear_master_data_cache,
        )
        clear_master_data_cache()

        doc = _make_quiz_doc(quiz_name="Week 1 Quiz", passing_score=70.0,
                             question_count=5)

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   return_value=doc), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_all",
                   return_value=[]):
            result = cached_content_details_payload("Quiz", "QZ-001")

        self.assertEqual(result["name"], "Week 1 Quiz")
        self.assertEqual(result["total_questions"], 5)
        self.assertAlmostEqual(result["passing_score"], 70.0)

    def test_golden_note_content_payload(self):
        """cached_content_details_payload returns correct NoteContent fields."""
        from tap_lms.summer_program.master_data_lookup import (
            cached_content_details_payload, clear_master_data_cache,
        )
        clear_master_data_cache()

        doc = _make_note_doc(note_name="Week 1 Reading", content="Read this.")

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   return_value=doc), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_all",
                   return_value=[]):
            result = cached_content_details_payload("NoteContent", "NC-001")

        self.assertEqual(result["name"], "Week 1 Reading")
        self.assertEqual(result["content"], "Read this.")


# ============================================================
# 2. CACHE-HIT AVOIDS RE-QUERY
# ============================================================

class TestCacheHitAvoidsRequery(unittest.TestCase):
    """Two calls with the same key must trigger only ONE underlying DB call."""

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def test_question_cache_hit(self):
        """cached_question_details called twice → frappe.get_doc called ONCE."""
        from tap_lms.summer_program.master_data_lookup import cached_question_details

        q_doc, option_texts = _make_quiz_question(
            question_id="Q-CACHE-001",
            question_text="Cache test question?",
            correct_option=2,
        )
        opt_doc = _make_option_doc("Option text")

        q_fetch_count = [0]

        def _counting_get_doc(doctype, name=None):
            if doctype == "QuizQuestion":
                q_fetch_count[0] += 1
                return q_doc
            return opt_doc

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   side_effect=_counting_get_doc):
            result1 = cached_question_details("Q-CACHE-001", "English")
            result2 = cached_question_details("Q-CACHE-001", "English")

        # Both results should be equal
        self.assertEqual(result1["question"], result2["question"])
        # QuizQuestion should have been fetched only once (second call is cache hit)
        self.assertEqual(q_fetch_count[0], 1,
                         "QuizQuestion doc should be fetched only ONCE on cache hit")

    def test_content_cache_hit(self):
        """cached_content_details_payload called twice → frappe.get_doc called ONCE."""
        from tap_lms.summer_program.master_data_lookup import cached_content_details_payload

        doc = _make_note_doc("W1 Note", "Body")

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   return_value=doc) as mock_get_doc, \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_all",
                   return_value=[]):
            cached_content_details_payload("NoteContent", "NC-HIT-01")
            cached_content_details_payload("NoteContent", "NC-HIT-01")

        self.assertEqual(mock_get_doc.call_count, 1,
                         "get_doc should be called ONCE; second call is a cache hit")


# ============================================================
# 3. CLEAR FORCES RE-QUERY
# ============================================================

class TestClearForcesRequery(unittest.TestCase):
    """clear_master_data_cache() causes the next call to re-hit the DB."""

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def test_clear_question_cache(self):
        """After clear, a second cached_question_details call re-queries."""
        from tap_lms.summer_program.master_data_lookup import (
            cached_question_details, clear_master_data_cache,
        )

        q_doc, option_texts = _make_quiz_question(
            question_id="Q-CLEAR-001",
            question_text="Clear test?",
            correct_option=1,
        )
        opt_doc = _make_option_doc("Answer text")

        # Track QuizQuestion fetch count separately
        q_fetch_count = [0]

        def _get_doc(doctype, name=None):
            if doctype == "QuizQuestion":
                q_fetch_count[0] += 1
                return q_doc
            return opt_doc   # all option lookups return the same mock

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   side_effect=_get_doc), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.logger",
                   return_value=MagicMock()):
            # First call — populates cache
            cached_question_details("Q-CLEAR-001", "English")
            count_after_first = q_fetch_count[0]

            # Clear cache — lru_cache entries evicted
            clear_master_data_cache()

            # Second call — must re-query (cache miss after clear)
            cached_question_details("Q-CLEAR-001", "English")
            count_after_second = q_fetch_count[0]

        self.assertEqual(count_after_first, 1,
                         "First call should query QuizQuestion once")
        self.assertEqual(count_after_second, 2,
                         "After clear, second call should re-query (total 2 QuizQuestion fetches)")


# ============================================================
# 4. MUTATION SAFETY
# ============================================================

class TestMutationSafety(unittest.TestCase):
    """Mutating a returned dict must NOT corrupt the cached entry."""

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def test_question_mutation_safety(self):
        """Mutate returned dict; next call returns original cached value."""
        from tap_lms.summer_program.master_data_lookup import cached_question_details

        q_doc, option_texts = _make_quiz_question(
            question_id="Q-MUT-001",
            question_text="Mutation test question?",
            correct_option=3,
        )
        opt_docs = [_make_option_doc(t) for t in option_texts]

        opt_doc = _make_option_doc("Mutation test option")

        def _get_doc(doctype, name=None):
            if doctype == "QuizQuestion":
                return q_doc
            return opt_doc

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   side_effect=_get_doc):
            # First call — warm the cache
            result1 = cached_question_details("Q-MUT-001", "English")
            original_question = result1["question"]

            # MUTATE the returned dict
            result1["question"] = "CORRUPTED QUESTION"
            result1["correct_option"] = "CORRUPTED"

            # Second call — must return original value, not the corrupted one
            result2 = cached_question_details("Q-MUT-001", "English")

        self.assertEqual(result2["question"], original_question,
                         "Cache entry must not be corrupted by caller mutation")
        self.assertNotEqual(result2["question"], "CORRUPTED QUESTION",
                            "Mutated value must not appear in subsequent cache hits")

    def test_content_mutation_safety(self):
        """Mutate a VideoClass payload; next call returns original cached value."""
        from tap_lms.summer_program.master_data_lookup import cached_content_details_payload

        doc = _make_video_doc(video_name="Original Name",
                              youtube_url="https://youtube.com/watch?v=original")

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   return_value=doc), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_all",
                   return_value=[]):
            result1 = cached_content_details_payload("VideoClass", "VC-MUT-001")
            original_url = result1["youtube_url"]

            # MUTATE the returned dict
            result1["youtube_url"] = "https://evil.example.com/mutated"
            result1["name"] = "CORRUPTED"
            result1["assessments"].append({"mutated": True})

            # Second call — cache entry must be intact
            result2 = cached_content_details_payload("VideoClass", "VC-MUT-001")

        self.assertEqual(result2["youtube_url"], original_url,
                         "Cache must not be corrupted by caller mutation of assessments or scalars")
        self.assertNotIn({"mutated": True}, result2.get("assessments", []),
                         "Appended item to assessments must not appear in cache")


# ============================================================
# 5. LANGUAGE KEYING
# ============================================================

class TestLanguageKeying(unittest.TestCase):
    """Same question_id with different languages produces separate cache entries."""

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def test_two_languages_produce_distinct_entries(self):
        """English and Hindi entries are keyed separately."""
        from tap_lms.summer_program.master_data_lookup import cached_question_details

        hindi_trans = MagicMock()
        hindi_trans.language = "Hindi"
        hindi_trans.translated_question = "हिंदी प्रश्न"

        q_doc, _ = _make_quiz_question(
            question_id="Q-LANG-001",
            question_text="English question text",
            correct_option=1,
            translations=[hindi_trans],
        )

        opt_doc = _make_option_doc("Option text")

        def _get_doc(doctype, name=None):
            if doctype == "QuizQuestion":
                return q_doc
            return opt_doc

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   side_effect=_get_doc):
            en_result = cached_question_details("Q-LANG-001", "English")
            hi_result = cached_question_details("Q-LANG-001", "Hindi")

        self.assertEqual(en_result["question"], "English question text",
                         "English entry should have English text")
        self.assertEqual(hi_result["question"], "हिंदी प्रश्न",
                         "Hindi entry should have Hindi translation")
        self.assertNotEqual(en_result["question"], hi_result["question"],
                            "Two language entries must be distinct")


# ============================================================
# 6. ENDPOINT REGRESSION — get_content_details
# ============================================================

class TestGetContentDetailsEndpointRegression(unittest.TestCase):
    """
    Regression: get_content_details returns same keys/values as before the cache.

    Tests per ADR-006 Revision: endpoint shape must be byte-identical for
    valid inputs. The cache is a transparent read optimization.
    """

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def _mock_frappe_io(self):
        """Standard io mocks for endpoint tests."""
        mock_local = MagicMock()
        mock_local.response = {}
        return mock_local

    def test_videoclass_youtube_url_populated(self):
        """
        Critical regression: youtube_url must be present in get_content_details
        response for VideoClass. This was a placeholder-incident victim
        (861 of 2029 broken messages had content_details.youtube_url as a
        placeholder — the endpoint returned None instead of the real URL).
        """
        import tap_lms.summer_program.student_progression_sp as sp
        import tap_lms.summer_program.utils as utils
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache

        clear_master_data_cache()
        mock_local = self._mock_frappe_io()

        video_doc = _make_video_doc(
            video_name="Week1 Video",
            youtube_url="https://youtube.com/watch?v=regression_test",
        )

        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(utils.frappe, "local", mock_local), \
             patch.object(sp.frappe, "local", mock_local), \
             patch.object(sp.frappe.db, "exists", return_value=True), \
             patch.object(sp.frappe.db, "get_value", return_value=None), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   return_value=video_doc), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_all",
                   return_value=[]), \
             patch.object(sp, "_resolve_content_language", return_value="English"), \
             patch.object(sp, "_get_video_unguided_submission_message",
                          return_value={"unguided_text": "Submit a photo",
                                        "unguided_text_url": None}):
            sp.get_content_details(
                content_type="VideoClass",
                content_id="VC-REG-001",
                student_id="ST00051383",
            )

        resp = mock_local.response
        self.assertTrue(resp.get("success"), f"Expected success=True, got: {resp}")
        self.assertEqual(resp.get("status"), "video_class")
        self.assertEqual(resp.get("youtube_url"),
                         "https://youtube.com/watch?v=regression_test",
                         "youtube_url must be populated from cache — this was the incident victim field")
        self.assertIsNotNone(resp.get("unguided_text"),
                             "unguided_text must be present (per-student live path)")

    def test_quiz_response_shape(self):
        """get_content_details Quiz returns total_questions, passing_score, time_limit."""
        import tap_lms.summer_program.student_progression_sp as sp
        import tap_lms.summer_program.utils as utils
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache

        clear_master_data_cache()
        mock_local = self._mock_frappe_io()

        quiz_doc = _make_quiz_doc(quiz_name="W1 Quiz", passing_score=65.0,
                                  question_count=4)

        with patch.object(utils.frappe.db, "rollback"), \
             patch.object(utils.frappe, "log_error"), \
             patch.object(utils.frappe, "logger"), \
             patch.object(utils.frappe, "local", mock_local), \
             patch.object(sp.frappe, "local", mock_local), \
             patch.object(sp.frappe.db, "exists", return_value=True), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   return_value=quiz_doc), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_all",
                   return_value=[]):
            sp.get_content_details(
                content_type="Quiz",
                content_id="QZ-REG-001",
            )

        resp = mock_local.response
        self.assertTrue(resp.get("success"))
        self.assertEqual(resp.get("status"), "quiz")
        self.assertEqual(resp.get("total_questions"), 4)
        self.assertAlmostEqual(resp.get("passing_score"), 65.0)


# ============================================================
# 6b. ENDPOINT REGRESSION — start_quiz question fields
# ============================================================

class TestStartQuizEndpointRegression(unittest.TestCase):
    """
    Regression: start_quiz returns question_text and option_a from cache.

    quiz_response.* were the second-largest placeholder-incident victim
    (~289 hits). question_text and option_a are the fields Glific reads
    directly. They must be populated after the Phase 2 cache wiring.
    """

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def test_start_quiz_question_text_and_option_a_populated(self):
        """start_quiz returns question_text and option_a from cached question.

        B1 fix: patch master_data_lookup.cached_question_details directly
        (the new Phase 2 seam) rather than fighting over frappe.get_doc with
        two competing patches. sp.frappe.get_doc is only used for Quiz and
        StudentQuizAttempt lookups inside sp itself.
        """
        import tap_lms.summer_program.student_progression_sp as sp

        mock_local = MagicMock()
        mock_local.response = {}

        # Build mock quiz doc with non-empty questions list.
        quiz_doc = _make_quiz_doc(quiz_name="RegTestQuiz", passing_score=60.0,
                                  question_count=2)
        quiz_doc.questions[0].question = "Q-REG-ST-001"
        quiz_doc.questions[0].question_number = 1
        quiz_doc.questions[1].question = "Q-REG-ST-002"
        quiz_doc.questions[1].question_number = 2

        attempt_doc = MagicMock()
        attempt_doc.name = "ATTEMPT-REG-001"
        attempt_doc.quizname = "RegTestQuiz"
        attempt_doc.passing_score = 60.0
        attempt_doc.answers = []
        attempt_doc.student = "ST-REG-001"
        attempt_doc.quiz = "QZ-REG-ST-001"
        attempt_doc.status = "in_progress"

        progress = {
            "name": "SSP-REG-001",
            "stage": "LU-W1-Basic",
            "current_week": 1,
            "current_tier": "Basic",
            "current_content_index": 0,
            "is_on_remedial": False,
            "active_quiz_attempt": None,
        }

        def _sp_get_doc(doctype, name=None):
            # frappe.get_doc("Quiz", quiz_id) — string doctype
            if doctype == "Quiz":
                return quiz_doc
            # frappe.get_doc({"doctype": "StudentQuizAttempt", ...}) — dict form
            if isinstance(doctype, dict) and doctype.get("doctype") == "StudentQuizAttempt":
                return attempt_doc
            # frappe.get_doc("StudentQuizAttempt", name) — string form
            if doctype == "StudentQuizAttempt":
                return attempt_doc
            raise AssertionError(f"Unexpected sp.frappe.get_doc({doctype!r})")

        # Phase 2 seam: patch cached_question_details in master_data_lookup.
        # This avoids double-patching frappe.get_doc and the empty-quiz bug.
        cached_q_payload = {
            "question_id": "Q-REG-ST-001",
            "question": "What is the capital of France?",
            "question_type": "Multiple Choice",
            "correct_option": "A",
            "option_a": "Paris",
            "option_b": "Berlin",
            "option_c": "London",
            "option_d": "Madrid",
        }

        with patch.object(sp.frappe, "local", mock_local), \
             patch.object(sp.frappe.db, "exists", return_value=True), \
             patch.object(sp.frappe.db, "get_value", return_value=progress), \
             patch.object(sp.frappe.db, "count", return_value=0), \
             patch.object(sp.frappe.db, "set_value"), \
             patch.object(sp.frappe, "get_doc", side_effect=_sp_get_doc), \
             patch.object(sp, "_resolve_student_id", return_value="ST-REG-001"), \
             patch.object(sp, "_get_language_for_student", return_value="English"), \
             patch("tap_lms.summer_program.student_progression_sp.now_datetime",
                   return_value="2026-06-08 10:00:00"), \
             patch("tap_lms.summer_program.student_progression_sp.cached_question_details",
                   return_value=cached_q_payload):
            sp.start_quiz(
                student_id="ST-REG-001",
                course_level="CL-REG-001",
                quiz_id="QZ-REG-ST-001",
            )

        resp = mock_local.response
        self.assertTrue(resp.get("success"), f"Expected success, got: {resp}")
        self.assertEqual(resp.get("status"), "quiz_started")
        self.assertIsNotNone(resp.get("question_text"),
                             "question_text must be populated (was incident victim)")
        self.assertEqual(resp.get("question_text"), "What is the capital of France?")
        self.assertIn("option_a", resp,
                      "option_a must be present in quiz_started response")
        self.assertEqual(resp.get("option_a"), "Paris")


# ============================================================
# 6c. ENDPOINT REGRESSION — submit_answer question fields
# ============================================================

class TestSubmitAnswerEndpointRegression(unittest.TestCase):
    """submit_answer child.* fields populated from cache."""

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def test_next_question_fields_populated(self):
        """submit_answer for non-last question populates next question_text + option_a.

        B2 fix: use a single unified frappe.get_doc mock (sp_get_doc) for all
        sp-internal doctypes, and patch master_data_lookup.cached_question_details
        directly so there is no conflict over the global frappe.get_doc attribute.
        The original test double-patched sp.frappe.get_doc AND
        master_data_lookup.frappe.get_doc — same object, last patch wins —
        which made attempt.student lookup return the mdl mock, breaking the
        attempt.student != student_id guard.
        """
        import tap_lms.summer_program.student_progression_sp as sp

        mock_local = MagicMock()
        mock_local.response = {}

        quiz_doc = _make_quiz_doc(question_count=2)
        quiz_doc.questions[0].question = "Q-SA-001"
        quiz_doc.questions[0].idx = 1
        quiz_doc.questions[0].question_number = 1
        quiz_doc.questions[1].question = "Q-SA-002"
        quiz_doc.questions[1].idx = 2
        quiz_doc.questions[1].question_number = 2

        attempt_doc = MagicMock()
        attempt_doc.name = "ATTEMPT-SA-001"
        attempt_doc.quiz = "QZ-SA-001"
        attempt_doc.student = "ST-SA-001"
        attempt_doc.status = "in_progress"
        attempt_doc.total_questions = 2
        attempt_doc.current_question_index = 0
        attempt_doc.correct_answers = 0
        attempt_doc.answers = []
        attempt_doc.started_at = "2026-06-08 10:00:00"
        attempt_doc.question_started_at = "2026-06-08 10:00:00"
        attempt_doc.student_progress = "SSP-SA-001"
        attempt_doc.passing_score = 60.0
        attempt_doc.course_level = "CL-SA-001"

        def _sp_get_doc(doctype, name=None):
            if doctype == "Quiz":
                return quiz_doc
            if doctype == "StudentQuizAttempt":
                return attempt_doc
            raise AssertionError(f"Unexpected sp.frappe.get_doc({doctype!r})")

        # Q1 details (current question being answered).
        q1_payload = {
            "question_id": "Q-SA-001",
            "question": "Q1 text",
            "question_type": "Multiple Choice",
            "correct_option": "B",   # correct_option=2 → B
            "option_a": "A opt",
            "option_b": "B opt",
            "option_c": "C opt",
            "option_d": "D opt",
        }
        # Q2 details (next question).
        q2_payload = {
            "question_id": "Q-SA-002",
            "question": "Q2 text for next",
            "question_type": "Multiple Choice",
            "correct_option": "A",
            "option_a": "Alpha",
            "option_b": "Beta",
            "option_c": "Gamma",
            "option_d": "Delta",
        }
        # cached_question_details is called twice: first for Q1 (current) then Q2 (next).
        q_payloads = iter([q1_payload, q2_payload])

        with patch.object(sp.frappe, "local", mock_local), \
             patch.object(sp.frappe.db, "exists", return_value=True), \
             patch.object(sp.frappe.db, "set_value"), \
             patch.object(sp.frappe, "get_doc", side_effect=_sp_get_doc), \
             patch.object(sp, "_resolve_student_id", return_value="ST-SA-001"), \
             patch.object(sp, "_get_language_for_student", return_value="English"), \
             patch("tap_lms.summer_program.student_progression_sp.now_datetime",
                   return_value="2026-06-08 10:01:00"), \
             patch("tap_lms.summer_program.student_progression_sp.cached_question_details",
                   side_effect=lambda qid, lang: next(q_payloads)):
            sp.submit_answer(
                student_id="ST-SA-001",
                quiz_attempt_id="ATTEMPT-SA-001",
                question_index=1,
                answer="B",   # correct for Q1 (correct_option=B)
            )

        resp = mock_local.response
        # Non-last question → "next_question" status
        self.assertEqual(resp.get("status"), "next_question",
                         f"Expected next_question, got: {resp}")
        self.assertEqual(resp.get("question_text"), "Q2 text for next",
                         "Next question text must be populated from cache")
        self.assertIn("option_a", resp,
                      "option_a for next question must be present (was incident victim)")


# ============================================================
# 6d. ENDPOINT REGRESSION — _resume_quiz question fields (D3)
# ============================================================

class TestResumeQuizEndpointRegression(unittest.TestCase):
    """
    D3: _resume_quiz was untested. cached_question_details is called at sp.py
    ~1409; this test exercises the resume branch and asserts question_text and
    option_a are populated from the cache.
    """

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def test_resume_quiz_question_text_populated(self):
        """_resume_quiz returns question_text and option_a from cached question.

        Scenario: student has answered Q1 (index 1). _resume_quiz detects
        Q2 (index 2) as the next unanswered question and returns its details.
        """
        from types import SimpleNamespace
        import tap_lms.summer_program.student_progression_sp as sp

        # Two-question quiz; Q1 is already answered.
        quiz_doc = _make_quiz_doc(quiz_name="ResumeQuiz", passing_score=60.0,
                                  question_count=2)
        quiz_doc.questions[0].question = "Q-RES-001"
        quiz_doc.questions[0].question_number = 1
        quiz_doc.questions[0].idx = 1
        quiz_doc.questions[1].question = "Q-RES-002"
        quiz_doc.questions[1].question_number = 2
        quiz_doc.questions[1].idx = 2

        attempt_doc = MagicMock()
        attempt_doc.name = "ATTEMPT-RES-001"
        attempt_doc.quiz = "QUIZ-RES-001"
        attempt_doc.quizname = "ResumeQuiz"
        attempt_doc.total_questions = 2
        attempt_doc.student = "ST-RES-001"
        attempt_doc.status = "in_progress"
        # Q1 is already answered.
        answered = SimpleNamespace(question_index=1, is_correct=1)
        attempt_doc.answers = [answered]

        progress_data = {
            "name": "SSP-RES-001",
            "stage": "LU-RES-001",
            "current_week": 1,
            "current_tier": "Basic",
            "current_content_index": 0,
            "is_on_remedial": False,
            "active_quiz_attempt": "ATTEMPT-RES-001",
        }

        def _sp_get_doc(doctype, name=None):
            if doctype == "Quiz":
                return quiz_doc
            raise AssertionError(f"Unexpected sp.frappe.get_doc({doctype!r})")

        # Q2 cached payload (the next unanswered question).
        q2_payload = {
            "question_id": "Q-RES-002",
            "question": "What is the speed of light?",
            "question_type": "Multiple Choice",
            "correct_option": "C",
            "option_a": "100 km/s",
            "option_b": "150,000 km/s",
            "option_c": "300,000 km/s",
            "option_d": "600,000 km/s",
        }

        with patch.object(sp.frappe, "get_doc", side_effect=_sp_get_doc), \
             patch.object(sp.frappe.db, "set_value"), \
             patch("tap_lms.summer_program.student_progression_sp.now_datetime",
                   return_value="2026-06-08 10:05:00"), \
             patch("tap_lms.summer_program.student_progression_sp.cached_question_details",
                   return_value=q2_payload):
            resp = sp._resume_quiz(attempt_doc, progress_data, language="English")

        self.assertEqual(resp.get("status"), "quiz_resumed",
                         f"Expected quiz_resumed, got: {resp}")
        self.assertEqual(resp.get("question_index"), 2,
                         "Should resume at question 2 (Q1 already answered)")
        self.assertEqual(resp.get("question_text"), "What is the speed of light?",
                         "question_text must be populated from cache (D3 requirement)")
        self.assertIn("option_a", resp,
                      "option_a must be present in resume response (incident victim field)")
        self.assertEqual(resp.get("option_a"), "100 km/s")
        self.assertEqual(resp.get("option_c"), "300,000 km/s")



# ============================================================
# 8. get_cache_info observable diagnostics
# ============================================================

class TestGetCacheInfo(unittest.TestCase):
    """get_cache_info() returns correct hit/miss structure."""

    def setUp(self):
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    def test_cache_info_structure(self):
        """get_cache_info returns expected keys."""
        from tap_lms.summer_program.master_data_lookup import get_cache_info

        info = get_cache_info()
        self.assertIn("question_details", info)
        self.assertIn("content_details", info)
        for key in ("hits", "misses", "maxsize", "currsize"):
            self.assertIn(key, info["question_details"])
            self.assertIn(key, info["content_details"])

    def test_cache_miss_count_increments(self):
        """A cold-cache lookup increments the miss counter."""
        from tap_lms.summer_program.master_data_lookup import (
            cached_content_details_payload, get_cache_info, clear_master_data_cache,
        )
        clear_master_data_cache()
        doc = _make_note_doc("InfoNote", "Body")

        with patch("tap_lms.summer_program.master_data_lookup.frappe.get_doc",
                   return_value=doc), \
             patch("tap_lms.summer_program.master_data_lookup.frappe.get_all",
                   return_value=[]):
            cached_content_details_payload("NoteContent", "NC-INFO-001")

        info = get_cache_info()
        self.assertEqual(info["content_details"]["misses"], 1)
        self.assertEqual(info["content_details"]["currsize"], 1)


if __name__ == "__main__":
    unittest.main()
