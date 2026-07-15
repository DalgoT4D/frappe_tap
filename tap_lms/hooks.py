from . import __version__ as app_version
from frappe import get_all


app_name = "tap_lms"
app_title = "Tap Lms"
app_publisher = "Techt4dev"
app_description = "Lms system for tap"
app_email = "tech4dev@gmail.com"
app_license = "MIT"

before_migrate = "tap_lms.migrate.before_migrate"


doc_events = {
    "School": {
        "before_save": "tap_lms.tap_lms.doctype.school.school.before_save"
    },
}

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
        "0 0 * * 1": [
            "tap_lms.summer_program.batch_admin.auto_advance_batch_week",
        ],
        "0 6 * * 1": [
            "tap_lms.summer_program.scheduler.weekly_content_sweep",
        ],
        "30 3 * * 2": [
            "tap_lms.summer_program.scheduler.weekly_content_delivery_trigger",
        ],
        "0 * * * *": [
            "tap_lms.summer_program.pre_launch.feedback_ready_watchdog",
            "tap_lms.summer_program.scheduler.glific_sync_dlq_watcher",
            "tap_lms.summer_program.scheduler.rq_queue_depth_watcher",
            "tap_lms.glific_integration.probe_token_health",
            "tap_lms.summer_program.campaign_processor.trigger_scheduled_campaigns",
            "tap_lms.summer_program.campaign_processor.check_reengagement",
        ],
        "0 12 * * 1-6": [
            "tap_lms.summer_program.bigquery_sync.sync_bigquery_glific_context",
        ],
    },
}

report_script_custom_doctypes = ["StudentStageProgress"]

fixtures = [{ "doctype": "Client Script", "filters": [ ["module", "in", ( "Tap Lms" )] ] }]
