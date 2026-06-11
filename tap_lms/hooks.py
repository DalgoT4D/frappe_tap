app_name = "tap_lms"
app_title = "Tap Lms"
app_publisher = "Techt4dev"
app_description = "Lms system for tap"
app_email = "tech4dev@gmail.com"
app_license = "MIT"

# ── SRE Monitoring ────────────────────────────────────────────────────────────
# List syntax required — Frappe merges hook lists across apps.
# on_exception does NOT exist in Frappe v14 — exception detection is handled
# inside after_request via sys.exc_info(). See tap_lms/middleware.py.
# Verified against: Frappe 14.29.0 / bench 5.16.2
before_request = ["tap_lms.middleware.before_request"]
after_request = ["tap_lms.middleware.after_request"]


# Document Events
doc_events = {
    "School": {"before_save": "tap_lms.tap_lms.doctype.school.school.before_save"},
    "Teacher": {"on_update": "tap_lms.glific_webhook.update_glific_contact"},
    "StudentStageProgress": {
        "after_insert": "tap_lms.tap_lms.doctype.studentonboardingprogress.studentonboardingprogress.update_student_progress",
        "on_update": "tap_lms.tap_lms.doctype.studentonboardingprogress.studentonboardingprogress.update_student_progress",
    },
    # CR-002 v2 gamification (2026-05-13): VideoClass completion via
    # StudentContentLog drives the activity-points handler. The handler also
    # arms the grace clock on the first VideoClass of each week (CR-003
    # follow-up 2: atomic Postgres CASE WHEN on `weekly_video_done`). Without
    # this hook the activity-points pipeline AND the grace clock are dead.
    "StudentContentLog": {
        "after_insert": "tap_lms.summer_program.activity_points.handle_content_log"
    },
    # CR-002 v2 gamification (2026-05-13): quiz attempts award per-question
    # points (correct → q.points; wrong → q.failed_points). The handler is
    # idempotent via `attempt.points_earned` so re-saves of completed
    # attempts are no-ops.
    "StudentQuizAttempt": {
        "on_update": "tap_lms.summer_program.quiz_points.handle_attempt_update"
    },
}

# Scheduled Tasks
#
# Daily:
#   - update_incomplete_stages: legacy onboarding sweep (pre-existing)
#   - run_daily_actions: SP daily housekeeping (scheduler.py)
#   - check_auto_activate: SP — auto-activates BPRs whose batch.start_date has
#                          arrived; seeds next_action_at on PEs so the per-PE
#                          dispatcher has work. See task #19 for details.
#
# Cron:
#   - */1 * * * *  — pe_dispatcher: per-PE event-driven dispatcher (task #15);
#                    processes overdue next_action_at, routes by next_action_type.
#                    Tightened from */2 to */1 min for the 100K-student MVP target
#                    (architecture §8.8 + ADR-003 audit log 2026-05-13). Combined
#                    with DISPATCH_BATCH_SIZE=1000 and 4 parallel workers gives
#                    240K actions/hour — drains a 100K week-boundary T19 burst in
#                    ~25 min. Hard prerequisite: partial index idx_pe_next_action
#                    (task #24, patch cr_004_scale.idx_pe_next_action) so the
#                    SELECT stays <50ms at scale.
#   - 0 */2 * * *  — escalation_runner: 6-hour bulk escalation sweep (legacy
#                    batcher; will eventually be replaced by escalation_batcher
#                    in collection-mode rollout)
#   - 0 0 * * 1    — auto_advance_batch_week: weekly Monday sweep that bumps
#                    Batch.current_calendar_week and unblocks max_allowed_week
#                    on each PE
scheduler_events = {
    "daily": [
        "tap_lms.tap_lms.page.onboarding_flow_trigger.onboarding_flow_trigger.update_incomplete_stages",
        "tap_lms.summer_program.scheduler.run_daily_actions",
        "tap_lms.summer_program.batch_activation.check_auto_activate",
    ],
    "cron": {
        "*/1 * * * *": [
            "tap_lms.summer_program.pe_dispatcher.process_program_actions",
        ],
        "0 */2 * * *": [
            "tap_lms.summer_program.escalation_runner.run_escalation_check",
        ],
        "0 0 * * 1": [
            "tap_lms.summer_program.batch_admin.auto_advance_batch_week",
        ],
    },
}

# Page configurations
page_js = {"onboarding-flow-trigger": "public/js/onboarding_flow_trigger.js"}

# Reports
report_script_custom_doctypes = ["StudentStageProgress"]


# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/tap_lms/css/tap_lms.css"
# app_include_js = "/assets/tap_lms/js/tap_lms.js"

# include js, css files in header of web template
# web_include_css = "/assets/tap_lms/css/tap_lms.css"
# web_include_js = "/assets/tap_lms/js/tap_lms.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "tap_lms/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
#       "Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
#       "methods": "tap_lms.utils.jinja_methods",
#       "filters": "tap_lms.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "tap_lms.install.before_install"
# after_install = "tap_lms.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "tap_lms.uninstall.before_uninstall"
# after_uninstall = "tap_lms.uninstall.after_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "tap_lms.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
#       "Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
#       "Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
#       "ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
#       "*": {
#               "on_update": "method",
#               "on_cancel": "method",
#               "on_trash": "method"
#       }
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
#       "all": [
#               "tap_lms.tasks.all"
#       ],
#       "daily": [
#               "tap_lms.tasks.daily"
#       ],
#       "hourly": [
#               "tap_lms.tasks.hourly"
#       ],
#       "weekly": [
#               "tap_lms.tasks.weekly"
#       ],
#       "monthly": [
#               "tap_lms.tasks.monthly"
#       ],
# }

# Testing
# -------

# before_tests = "tap_lms.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
#       "frappe.desk.doctype.event.event.get_events": "tap_lms.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
#       "Task": "tap_lms.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]


# User Data Protection
# --------------------

# user_data_fields = [
#       {
#               "doctype": "{doctype_1}",
#               "filter_by": "{filter_by}",
#               "redact_fields": ["{field_1}", "{field_2}"],
#               "partial": 1,
#       },
#       {
#               "doctype": "{doctype_2}",
#               "filter_by": "{filter_by}",
#               "partial": 1,
#       },
#       {
#               "doctype": "{doctype_3}",
#               "strict": False,
#       },
#       {
#               "doctype": "{doctype_4}"
#       }
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
#       "tap_lms.auth.validate"
# ]

fixtures = [{"doctype": "Client Script", "filters": [["module", "in", ("Tap Lms")]]}]
