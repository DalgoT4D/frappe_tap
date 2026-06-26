"""
Configure rag_service's LLM Settings (Gemini) and GCS Settings from
service-account JSON files, passed in as file paths.

Must be run with the bench env's Python (so `frappe` is importable), against
the rag_service site — NOT a plain `python3` invocation.

Usage (run inside the dev-lms container):

    cd /home/frappe/frappe-bench
    RAG_SITE_NAME=rag.localhost ../env/bin/python3 /workspace/frappe_tap/scripts/configure_credentials.py \\
        --gemini-creds /workspace/frappe_tap/.secrets/gemini-sa.json \\
        --gcs-creds    /workspace/frappe_tap/.secrets/gcs-sa.json \\
        --location us-central1

Notes:
  - The JSON files must be reachable from INSIDE the container. Either drop
    them somewhere under /workspace/frappe_tap (bind-mounted from your repo
    checkout — keep them out of git, e.g. a gitignored .secrets/ folder), or
    `podman cp <file> dev-lms:/tmp/` and point at that path instead.
  - --location is required if --gemini-creds is set: rag_service's
    GeminiProvider passes it straight into vertexai.init() with no fallback,
    so a missing value fails loudly rather than silently.
  - project_id is read from each JSON file's own "project_id" key by default;
    override with --gemini-project-id / --gcs-project-id if you need a
    different project than what's embedded in the key file.
  - By default this creates/updates one LLM Settings record per model name in
    --model-names (default: both models actually hardcoded across
    rag_service's feedback_utils — gemini-2.5-pro and gemini-2.5-flash-lite).
    All records share the same credentials_json/project_id/location.
  - Safe to re-run — updates existing records (matched by provider+model_name
    for LLM Settings; GCS Settings is a Single doctype, so there's only ever
    one) instead of duplicating.
"""

import argparse
import json
import os

import frappe

RAG_SITE_NAME = os.environ.get("RAG_SITE_NAME", "rag.localhost")

DEFAULT_MODEL_NAMES = ["gemini-2.5-pro", "gemini-2.5-flash-lite"]


def read_json_file(path):
    with open(path, "r") as f:
        content = f.read()
    json.loads(content)  # validate before we ever write it to a DB field
    return content


def configure_gemini(creds_path, location, model_names, project_id_override):
    raw = read_json_file(creds_path)
    key_data = json.loads(raw)
    project_id = project_id_override or key_data.get("project_id")
    if not project_id:
        raise SystemExit(
            "Could not determine project_id from the Gemini creds file — "
            "pass --gemini-project-id explicitly"
        )

    for model_name in model_names:
        existing = frappe.get_list(
            "LLM Settings",
            filters={"provider": "Gemini", "model_name": model_name},
            limit=1,
        )
        if existing:
            doc = frappe.get_doc("LLM Settings", existing[0].name)
            is_new = False
        else:
            doc = frappe.new_doc("LLM Settings")
            doc.provider = "Gemini"
            is_new = True

        doc.model_name = model_name
        doc.location = location
        doc.project_id = project_id
        doc.credentials_json = raw
        doc.is_active = 1

        if is_new:
            doc.insert(ignore_permissions=True)
            print(f"✓ Created LLM Settings: provider=Gemini model_name={model_name}")
        else:
            doc.save(ignore_permissions=True)
            print(f"✓ Updated LLM Settings: provider=Gemini model_name={model_name}")

    frappe.db.commit()
    print(f"  project_id={project_id} location={location}")


def configure_gcs(creds_path, project_id_override):
    raw = read_json_file(creds_path)
    key_data = json.loads(raw)
    project_id = project_id_override or key_data.get("project_id")
    if not project_id:
        raise SystemExit(
            "Could not determine project_id from the GCS creds file — "
            "pass --gcs-project-id explicitly"
        )

    doc = frappe.get_single("GCS Settings")
    doc.project_id = project_id
    doc.credentials_json = raw
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"✓ Updated GCS Settings: project_id={project_id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gemini-creds", help="Path to Gemini/Vertex AI service-account JSON")
    parser.add_argument("--gcs-creds", help="Path to GCS service-account JSON")
    parser.add_argument(
        "--location",
        help="Vertex AI region, e.g. us-central1 (required if --gemini-creds is set)",
    )
    parser.add_argument(
        "--model-names",
        nargs="+",
        default=DEFAULT_MODEL_NAMES,
        help=f"Model name(s) to create/update LLM Settings for (default: {DEFAULT_MODEL_NAMES})",
    )
    parser.add_argument("--gemini-project-id", help="Override project_id instead of reading it from the Gemini creds file")
    parser.add_argument("--gcs-project-id", help="Override project_id instead of reading it from the GCS creds file")
    args = parser.parse_args()

    if not args.gemini_creds and not args.gcs_creds:
        parser.error("Pass at least one of --gemini-creds or --gcs-creds")
    if args.gemini_creds and not args.location:
        parser.error(
            "--location is required when --gemini-creds is set "
            "(rag_service's GeminiProvider has no fallback region)"
        )

    frappe.init(RAG_SITE_NAME)
    frappe.connect()
    frappe.set_user("Administrator")

    if args.gemini_creds:
        configure_gemini(args.gemini_creds, args.location, args.model_names, args.gemini_project_id)
    if args.gcs_creds:
        configure_gcs(args.gcs_creds, args.gcs_project_id)


if __name__ == "__main__":
    main()
