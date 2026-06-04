import frappe

from frappe.utils import cint

from tap_lms.summer_program.state_machine import get_active_pe
from tap_lms.summer_program.utils import (
    glific_response,
    normalize_unicode_surrogates,
    resolve_student,
    safe_sp_api_error_response,
)


EXPECTED_SUBMISSION_LABELS = {
    "emoji": ["Emoji response"],
    "word_text_voice": ["Word/voice reflection"],
    "image": ["Observation/image"],
    "summary_text_voice": ["Voice/text"],
    "photo_video_artefact": ["image/video"],
    "video": ["image/video"],
}


@frappe.whitelist(allow_guest=False)
@glific_response
def get_submission_message(student_id, flow_type, **_glific_kwargs):
    """
    Get the appropriate submission message for a student, based on their current
    state and the content they are submitting against.

    `**_glific_kwargs` absorbs Glific-injected fields per task #89 — ignored.

    Args:
        student_id: Student document name
        flow_type: Optional flow type (e.g. "main", "escalation") to tailor message
    Returns:
        dict with message details
    """
    try:
        student_id = resolve_student(student_id)
        if not student_id:
            return {
                "success": False,
                "status": "not_found",
                "error_detail": "Student not found",
            }

        pe = get_active_pe(student_id)
        if not pe:
            return {
                "success": False,
                "status": "no_active_enrollment",
                "student_id": student_id,
                "error_detail": "No active ProgramEnrollment",
            }

        course_level = pe.course_level
        current_week = cint(pe.current_week) or 1
        expected_submission_type = pe.current_expected_submission_type
        language = pe.language

        if not course_level:
            return _error_response(
                "no_course_level",
                student_id,
                "No course level found on active ProgramEnrollment",
                pe,
            )

        if not expected_submission_type:
            return _error_response(
                "no_expected_submission_type",
                student_id,
                "No expected submission type found on active ProgramEnrollment",
                pe,
            )

        if not language:
            return _error_response(
                "no_language",
                student_id,
                "No language found on active ProgramEnrollment",
                pe,
            )

        submission_labels = EXPECTED_SUBMISSION_LABELS.get(expected_submission_type)
        if not submission_labels:
            return _error_response(
                "unsupported_expected_submission_type",
                student_id,
                f"Unsupported expected submission type: {expected_submission_type}",
                pe,
            )

        learning_unit = _get_learning_unit_for_enrollment(pe, current_week)
        if not learning_unit:
            return _error_response(
                "no_learning_unit",
                student_id,
                f"No learning unit found for week {current_week}",
                pe,
            )

        video_class = _get_first_video_class(learning_unit)
        if not video_class:
            return _error_response(
                "no_video_class",
                student_id,
                f"No VideoClass found in learning unit {learning_unit}",
                pe,
                learning_unit=learning_unit,
            )

        assignment = _get_first_assignment_for_video(video_class)
        if not assignment:
            return _error_response(
                "no_assignment",
                student_id,
                f"No Assignment assessment found for VideoClass {video_class}",
                pe,
                learning_unit=learning_unit,
                video_class=video_class,
            )

        rule = _get_matching_submission_rule(
            assignment,
            submission_labels,
            language,
        )
        if not rule:
            return _error_response(
                "no_submission_rule",
                student_id,
                (
                    "No submission rule found for expected submission type "
                    f"{expected_submission_type} and language {language}"
                ),
                pe,
                learning_unit=learning_unit,
                video_class=video_class,
                assignment=assignment,
            )

        message_details = _select_message_for_flow(rule, flow_type)
        if not message_details["message"]:
            return _error_response(
                "no_submission_message",
                student_id,
                f"No submission message configured for rule {rule.name}",
                pe,
                learning_unit=learning_unit,
                video_class=video_class,
                assignment=assignment,
            )

        return {
            "success": True,
            "student_id": student_id,
            "flow_type": flow_type,
            "message_variant": message_details["message_variant"],
            "message": message_details["message"],
            "message_url": message_details["message_url"],
        }

    except Exception as e:
        # BR-003: rollback-first + flat error (L-030 cascade fix). The previous
        # `frappe.log_error(...); return {...}` ran log_error's INSERT on an
        # already-poisoned txn → InFailedSqlTransaction → Glific 400. It also
        # leaked raw `str(e)` to the WhatsApp learner via error_detail.
        return safe_sp_api_error_response(e, "get_submission_message",
                                          student_id=student_id)


def _get_learning_unit_for_enrollment(pe, current_week):
    tier = pe.current_tier
    course_level = normalize_unicode_surrogates(pe.course_level)
    params = {
        "course_level": course_level,
        "current_week": current_week,
    }
    tier_filter = ""
    if tier:
        tier_filter = "AND lu.difficulty_tier = %(tier)s"
        params["tier"] = tier

    rows = frappe.db.sql(
        f"""
        SELECT lul.learning_unit
        FROM `tabLearningUnitList` lul
        INNER JOIN `tabLearningUnit` lu ON lu.name = lul.learning_unit
        WHERE lul.parent = %(course_level)s
          AND lul.parenttype = 'Course Level'
          AND lul.week_no = %(current_week)s
          {tier_filter}
        ORDER BY lul.idx ASC
        LIMIT 1
        """,
        params,
        as_dict=True,
    )
    if rows:
        return rows[0].learning_unit

    if tier:
        rows = frappe.db.sql(
            """
            SELECT lul.learning_unit
            FROM `tabLearningUnitList` lul
            WHERE lul.parent = %(course_level)s
              AND lul.parenttype = 'Course Level'
              AND lul.week_no = %(current_week)s
            ORDER BY lul.idx ASC
            LIMIT 1
            """,
            params,
            as_dict=True,
        )
        if rows:
            return rows[0].learning_unit

    return None


def _get_first_video_class(learning_unit):
    learning_unit = normalize_unicode_surrogates(learning_unit)
    row = frappe.db.get_value(
        "UnitContentItem",
        {
            "parent": learning_unit,
            "parenttype": "LearningUnit",
            "content_type": "VideoClass",
        },
        "content",
        order_by="idx asc",
    )
    return row


def _get_first_assignment_for_video(video_class):
    rows = frappe.get_all(
        "AssessmentList",
        filters={
            "parent": video_class,
            "parenttype": "VideoClass",
            "assessment_type": "Assignment",
        },
        fields=["assessment"],
        order_by="idx asc",
        limit_page_length=1,
    )
    return normalize_unicode_surrogates(rows[0].assessment) if rows else None


def _get_matching_submission_rule(assignment, submission_labels, language):
    assignment = normalize_unicode_surrogates(assignment)
    rows = frappe.get_all(
        "Assignment Submission Rule",
        filters={
            "parent": assignment,
            "parenttype": "Assignment",
            "submission_label": ["in", submission_labels],
            "language": language,
        },
        fields=[
            "name",
            "submission_label",
            "language",
            "submission_title",
            "allowed_submission_types",
            "guided_text",
            "guided_text_audio",
            "unguided_text",
            "unguided_text_audio",
        ],
        order_by="display_order asc, idx asc",
        limit_page_length=1,
    )
    return rows[0] if rows else None


def  _select_message_for_flow(rule, flow_type):
    normalized_flow_type = (flow_type or "").strip().lower()
    if normalized_flow_type == "escalation":
        preferred_variant = "guided"
    else:
        preferred_variant = "unguided"

    alternate_variant = "guided" if preferred_variant == "unguided" else "unguided"

    for variant in (preferred_variant, alternate_variant):
        text_field = f"{variant}_text"
        audio_field = f"{variant}_text_audio"
        message = rule.get(text_field)
        if message:
            return {
                "message_variant": variant,
                "message": _strip_html_text(message),
                "message_url": rule.get(audio_field),
            }

    return {
        "message_variant": preferred_variant,
        "message": None,
        "message_url": None,
    }


def _strip_html_text(value):
    if not value:
        return value
    from frappe.utils import strip_html_tags
    return strip_html_tags(value).strip()


def _error_response(status, student_id, error_detail, pe=None, **kwargs):
    response = {
        "success": False,
        "status": status,
        "student_id": student_id,
        "error_detail": error_detail,
    }
    if pe:
        response.update({
            "course_level": pe.course_level,
            "week": cint(pe.current_week) or 1,
            "current_tier": pe.current_tier,
            "current_path": pe.current_path,
            "expected_submission_type": pe.current_expected_submission_type,
            "language": pe.language,
        })
    response.update(kwargs)
    return response
