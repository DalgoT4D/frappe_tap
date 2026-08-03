#!/usr/bin/env python
# Feedback queue consumer — bootstraps Frappe context then starts consuming
# from the plagiarism_feedback RabbitMQ queue.
#
# Usage (from any directory):
#   SITE_NAME=tap_lms.dev python scripts/console_consumer.py
#
# Or via supervisor (see docs/server_setup.md):
#   environment=SITE_NAME="tap_lms.dev"
#   directory=/home/lms-dev/frappe-bench/sites
#   command=.../env/bin/python .../scripts/console_consumer.py

# ── STEP 1: STABLE STANDALONE FRAPPE BOOTSTRAP ───────────────────────────────
import importlib
import os
import sys

import frappe

# Bench sites path — explicit so the script works from any working directory,
# not just when invoked from the sites/ folder.
bench_sites_path = os.getenv(
    "BENCH_SITES_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "sites"),
)
bench_sites_path = os.path.abspath(bench_sites_path)

site_name = os.getenv("SITE_NAME", "tap_lms.localhost")

# 1. Init Frappe with explicit sites_path so it resolves site_config.json
#    regardless of the current working directory.
frappe.init(site=site_name, sites_path=bench_sites_path)

# 2. Populate site context flags manually before connecting — mirrors what
#    rag_service does to avoid silent failures in Frappe internals that read
#    frappe.local.conf before a full request context is established.
frappe.local.site = site_name
frappe.local.conf = frappe.get_site_config()
frappe.local.lang = "en"

# 3. Connect to the database pool.
frappe.connect()
frappe.set_user("Administrator")

# 4. Force-register installed app modules so DocType lookups and custom
#    app imports work correctly (prevents ImportError on business_theme_v14
#    and similar apps installed alongside tap_lms).
installed_apps = frappe.get_installed_apps()
frappe.local.app_modules = {}
for app in installed_apps:
    try:
        frappe.local.app_modules[app] = importlib.import_module(app)
    except ImportError:
        continue


# ── STEP 2: RUN THE QUEUE LISTENER ───────────────────────────────────────────
from tap_lms.feedback_handler.feedback_consumer import FeedbackConsumer


def run():
    """
    Main entry point — can be called as a module or run directly as a script.

    Entrypoint (module):  python -c "import scripts.console_consumer as cc; cc.run()"
    Entrypoint (script):  python scripts/console_consumer.py
    Supervisor:           command=.../env/bin/python .../scripts/console_consumer.py
    """
    print("\n=== Starting Feedback Consumer ===")
    print(f"    site : {site_name}")
    print(f"    sites: {bench_sites_path}\n")

    consumer = FeedbackConsumer()
    consumer.setup_rabbitmq()

    # Print pending message count on startup — useful for diagnosing backlogs.
    try:
        queue_state = consumer.channel.queue_declare(
            queue=consumer.settings.feedback_results_queue, passive=True
        )
        print(
            f"Found {queue_state.method.message_count} messages in queue "
            f"'{consumer.settings.feedback_results_queue}'\n"
        )
    except Exception as e:
        print(f"Could not check queue depth: {e}\n")

    print("Starting consumer... (waiting for messages, press CTRL+C to exit)")
    consumer.start_consuming()


if __name__ == "__main__":
    run()
