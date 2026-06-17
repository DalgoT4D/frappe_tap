"""
Summer Program Master Data Read Cache
tap_lms/summer_program/master_data_lookup.py

CR-024 Phase 2 (ADR-006 Revision, 2026-06-08): synchronous in-process
read-cache of OPERATIONALLY IMMUTABLE master data for the three endpoints
that timed out in the 2026-06-04→05 trainer-cohort incident.

WHAT IS CACHED (ONLY immutable master data):
  - cached_question_details(question_id, language):
      QuizQuestion text, options A-D, correct_option — cached per (id, language)
  - cached_content_details_payload(content_type, content_id):
      VideoClass: name/youtube_url/plio_url/video_file/duration/description,
        raw translations list, assessments list.
      Quiz: question_count/passing_score/time_limit.
      NoteContent/Assignment/CourseProject: their immutable scalar fields.

WHAT IS NEVER CACHED:
  - ProgramEnrollment, StudentStageProgress, StudentQuizAttempt, Submission
  - Student language resolution (per-student live)
  - unguided_text (per-student live)
  - Any field that changes during an active program run

MUTATION SAFETY:
  lru_cache returns the SAME dict object on every cache hit. Callers MUST NOT
  mutate the returned dict/list — they get deep copies via copy.deepcopy()
  at the boundary so cache entries are never corrupted by callers.

INVALIDATION:
  lru_cache has NO automatic invalidation. If a curriculum author edits a
  content item or quiz question while a program run is active, the change is
  invisible to running workers until either:
    1. clear_master_data_cache() is called (e.g. from bench console), OR
    2. The worker process is restarted (bench restart).

  The same constraint applies to lru_cache on the course-content tree. This
  is accepted per ADR-006 because content is operationally immutable during
  an active program run.

WARMING (deferred per ADR-006 Revision, 2026-06-08):
  The cache is SELF-POPULATING — the first request for a given content/question
  per worker does one DB read, then every later request is a cache hit. No
  pre-warm is shipped: the 2026-06-04 incident profile was sustained concurrency
  (466 students over ~25h), which the self-populating cache absorbs after the
  first request. A boot-time pre-warm is deferred until a measured cold-start
  P99 shows it is needed (it would require correct content-tree traversal, which
  the original draft got wrong against the actual schema).
"""
import copy
import frappe
from frappe.utils import cint, flt
from functools import lru_cache

from tap_lms.summer_program.utils import normalize_unicode_surrogates


# ============================================================
# MODULE-LEVEL CONSTANTS
# ============================================================

OPTION_LETTERS = ['A', 'B', 'C', 'D']
DEFAULT_LANGUAGE = "English"

# Maximum lru_cache size per function. Each quiz question entry is a small
# flat dict (~300 bytes); 4096 covers a large multi-course curriculum comfortably.
_CACHE_MAXSIZE = 4096


# ============================================================
# 1. CACHED QUESTION DETAILS
#    Key: (question_id, language)
#    Cached: question text, question_type, correct_option, option_a..d
# ============================================================

@lru_cache(maxsize=_CACHE_MAXSIZE)
def _question_details_raw(question_id: str, language: str) -> dict:
    """
    Inner lru_cache target. Returns a dict with the immutable question payload.

    KEY DESIGN: both args are plain strings (lru_cache requires hashable keys).
    language="" is treated as DEFAULT_LANGUAGE by the caller.

    Never call this directly from outside this module — use
    cached_question_details() which applies normalize_unicode_surrogates and
    returns a deep copy.

    Performance note: each call loads the QuizQuestion doc + up to 4
    QuizOption docs (one per answer option). These are the Frappe get_doc
    calls that hammer the DB on every question in start_quiz / _resume_quiz /
    submit_answer — caching eliminates the per-call re-read.
    """
    from frappe.utils import strip_html_tags

    q = frappe.get_doc("QuizQuestion", question_id)
    question_text = q.question or getattr(q, 'question_name', '') or ""

    # D1: run translation loop unconditionally (no English-skip) so cached path
    # is byte-identical to old _get_question_details for all languages including
    # English overrides. Old helper ran the loop whenever language was truthy.
    if language and hasattr(q, 'question_translations') and q.question_translations:
        for trans in q.question_translations:
            if trans.language == language and trans.translated_question:
                question_text = trans.translated_question
                break

    question_text = strip_html_tags(question_text) if question_text else ""

    options = {}
    if hasattr(q, 'options') and q.options:
        for i, opt_row in enumerate(q.options[:4]):
            letter = OPTION_LETTERS[i].lower()
            option_id = opt_row.options
            if option_id:
                option_doc = frappe.get_doc("QuizOption", option_id)
                option_text = option_doc.option_text or ""
                # D1: no English-skip here either — run unconditionally.
                if language and hasattr(option_doc, 'option_translations') and option_doc.option_translations:
                    for trans in option_doc.option_translations:
                        if trans.language == language and trans.translated_option:
                            option_text = trans.translated_option
                            break
                options[f"option_{letter}"] = strip_html_tags(option_text) if option_text else ""

    correct_num = cint(q.correct_option)
    correct_letter = OPTION_LETTERS[correct_num - 1] if 1 <= correct_num <= 4 else "A"

    result = {
        "question_id": question_id,
        "question": question_text,
        "question_type": getattr(q, 'question_type', 'Multiple Choice'),
        "correct_option": correct_letter,
    }
    result.update(options)
    return result


def cached_question_details(question_id: str, language: str = None) -> dict:
    """
    Return immutable question payload for (question_id, language).

    This is the public API. It normalizes inputs, calls the lru_cache target,
    and returns a DEEP COPY so callers cannot corrupt the cached entry.

    Replaces direct _get_question_details() calls in:
      - start_quiz (first question)
      - _resume_quiz (resume question)
      - submit_answer (current and next question)

    Args:
        question_id: QuizQuestion doc name. Surrogate-normalized before lookup.
        language: Language string (e.g. "Hindi"). None → DEFAULT_LANGUAGE.

    Returns:
        dict with keys: question_id, question, question_type, correct_option,
        option_a, option_b, option_c, option_d (present keys only).
        Always a fresh copy — safe to mutate.
    """
    question_id = normalize_unicode_surrogates(question_id or "")
    resolved_language = (language or DEFAULT_LANGUAGE)
    try:
        raw = _question_details_raw(question_id, resolved_language)
        return copy.deepcopy(raw)
    except Exception as e:
        frappe.log_error(f"cached_question_details error for {question_id!r}: {e}",
                         "master_data_lookup")
        return {"question_id": question_id, "error": str(e)}


# ============================================================
# 2. CACHED CONTENT DETAILS PAYLOAD
#    Key: (content_type, content_id)
#    Cached: ONLY immutable doc-derived fields. Per-student / per-language
#    bits (translation selection, unguided_text) are NOT baked in.
# ============================================================

@lru_cache(maxsize=_CACHE_MAXSIZE)
def _content_details_raw(content_type: str, content_id: str) -> dict:
    """
    Inner lru_cache target. Returns a dict of the IMMUTABLE fields for
    content_type/content_id.

    For VideoClass: includes the full translations list (all languages) and
    the full assessments list. The endpoint overlays the per-language
    translation LIVE using the pre-resolved language.

    For Quiz: includes question_count, passing_score, time_limit.

    Never call this directly — use cached_content_details_payload() which
    returns a deep copy.
    """
    doc = frappe.get_doc(content_type, content_id)

    if content_type == "VideoClass":
        # Pull assessments from AssessmentList child table.
        raw_assessments = frappe.get_all(
            "AssessmentList",
            filters={"parent": content_id, "parenttype": "VideoClass"},
            fields=["assessment_type", "assessment"],
            order_by="idx asc",
        )
        assessments = [
            {
                "assessment_type": r.assessment_type,
                "assessment_id": (
                    normalize_unicode_surrogates(r.assessment)
                    if r.assessment_type == "Assignment"
                    else r.assessment
                ),
            }
            for r in raw_assessments if r.assessment
        ]

        # Store the raw translations list (all languages). The endpoint picks
        # the right one LIVE after resolving the student's language.
        # D2: include all translation fields (plio_url + video_file) so the
        # cache holds the full immutable translation row and the overlay in
        # get_content_details can apply them without a live DB read.
        translations = []
        if hasattr(doc, 'video_translations') and doc.video_translations:
            for trans in doc.video_translations:
                translations.append({
                    "language": trans.language,
                    "translated_name": getattr(trans, 'translated_name', None),
                    "video_youtube_url": getattr(trans, 'video_youtube_url', None),
                    "video_plio_url": getattr(trans, 'video_plio_url', None),
                    "video_file": getattr(trans, 'video_file', None),
                })

        return {
            "_content_type": "VideoClass",
            "name": doc.video_name,
            "youtube_url": doc.video_youtube_url,
            "plio_url": doc.video_plio_url,
            "video_file": doc.video_file,
            "duration": str(doc.duration) if doc.duration else None,
            "description": doc.description,
            "assessments": assessments,          # full list; endpoint caps at 5
            "translations": translations,         # all languages; endpoint picks one
        }

    elif content_type == "Quiz":
        question_count = len(doc.questions) if hasattr(doc, 'questions') else 0
        return {
            "_content_type": "Quiz",
            "name": getattr(doc, 'quiz_name', content_id),
            "total_questions": question_count,
            "passing_score": flt(getattr(doc, 'passing_score', 60)),
            "time_limit": getattr(doc, 'time_limit', None),
        }

    elif content_type == "NoteContent":
        return {
            "_content_type": "NoteContent",
            "name": getattr(doc, 'note_name', content_id),
            "content": getattr(doc, 'content', None),
        }

    elif content_type == "Assignment":
        return {
            "_content_type": "Assignment",
            "name": getattr(doc, 'assignment_name', content_id),
            "description": getattr(doc, 'description', None),
            "assignment_type": getattr(doc, 'assignment_type', None),
        }

    elif content_type == "CourseProject":
        return {
            "_content_type": "CourseProject",
            "name": getattr(doc, 'project_name', content_id),
            "description": getattr(doc, 'description', None),
        }

    # TextMessageContent, VoiceNoteContent, ParentCallConfig — minimal
    return {
        "_content_type": content_type,
        "name": content_id,
    }


def cached_content_details_payload(content_type: str, content_id: str) -> dict:
    """
    Return immutable content-item fields for (content_type, content_id).

    This is the public API. Returns a DEEP COPY so callers can safely
    add/modify keys (e.g. overlay per-student unguided_text) without
    corrupting the cached entry.

    The returned dict contains ONLY immutable fields. Callers MUST overlay
    the following LIVE (not cached):
      - VideoClass: language-specific translation selection (from the
        returned "translations" list), unguided_text / unguided_text_url
        (per-student, from _get_video_unguided_submission_message).

    Args:
        content_type: DocType name (VideoClass, Quiz, NoteContent, etc.)
        content_id: Document name. Surrogate-normalized before lookup.

    Returns:
        dict — a fresh copy of the cached payload, safe to mutate.
    """
    content_id = normalize_unicode_surrogates(content_id or "")
    try:
        raw = _content_details_raw(content_type, content_id)
        return copy.deepcopy(raw)
    except Exception as e:
        frappe.log_error(
            f"cached_content_details_payload error for {content_type}/{content_id!r}: {e}",
            "master_data_lookup",
        )
        return {"_content_type": content_type, "error": str(e)}


# ============================================================
# 3. CACHE MANAGEMENT
# ============================================================

def clear_master_data_cache():
    """
    Clear all lru_caches in this module.

    REQUIRED after any curriculum edit (VideoClass, Quiz, QuizQuestion,
    QuizOption, NoteContent, Assignment, CourseProject) to make the running
    worker reflect the change WITHOUT a full restart.

    Usage from bench console:
        from tap_lms.summer_program.master_data_lookup import clear_master_data_cache
        clear_master_data_cache()

    Or equivalently:
        bench --site <site> restart  (recycles worker processes; same effect)

    After clearing, the cache self-populates again on the next request for each
    content/question (one DB read per entry, then cache hits).
    """
    _question_details_raw.cache_clear()
    _content_details_raw.cache_clear()
    frappe.logger("master_data_lookup").info(
        "master_data_lookup: all lru_caches cleared"
    )


def get_cache_info() -> dict:
    """Return lru_cache statistics for observability / diagnostics."""
    q_info = _question_details_raw.cache_info()
    c_info = _content_details_raw.cache_info()
    return {
        "question_details": {
            "hits": q_info.hits,
            "misses": q_info.misses,
            "maxsize": q_info.maxsize,
            "currsize": q_info.currsize,
        },
        "content_details": {
            "hits": c_info.hits,
            "misses": c_info.misses,
            "maxsize": c_info.maxsize,
            "currsize": c_info.currsize,
        },
    }

