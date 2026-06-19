"""
Re-trigger failed Vocallabs parent calls from a campaign Data-tab CSV export.

One-off / repeatable operational tool for the EXISTING campaign backlog — the calls
already placed, for which we never captured the real call id (so we can't reconcile
them via our own logs; see docs/vocallabs-api-reference.md). Going forward the webhook
receiver (vocallabs_webhook.py, CR-030) records outcomes live and this export path
isn't needed.

Source of truth = the Vocallabs "Data" tab CSV export (per-call `call_status` +
`phone_to`). Logic:
  1. Aggregate by parent phone (last-10 digits). A phone is CONNECTED if ANY of its
     rows is `completed`; a RETRY candidate if it never connected and has a
     `no-answer` / `busy` / `fail` row. `invalid-number` / `unknown` are skipped.
     (Aggregating first means a number that connected on a later attempt is NOT
     re-called just because an earlier attempt failed.)
  2. Map phone -> Student.phone (last-10) -> most-recent active ProgramEnrollment in
     the batch.
  3. Guardrails: active PE only; skip already-submitted; dedup per PE. (No
     experiment-arm filter — control students (dormant/arm_b) physically can't be
     called: initiate_parent_call no-ops them via welcome_greeting == "None".)
  4. Resolve the canonical parent_call escalation step via _get_escalation_steps_for_pe
     (L-029 — escalation is NEVER hardcoded) and enqueue initiate_parent_call.

SAFETY:
  - `dry_run=True` by default — prints who WOULD be called + the skip breakdown and
    places NO calls. Review, then re-run with dry_run=False.
  - `max_calls` caps a first live wave; pacing (`batch_size` + `sleep_seconds`) spreads
    the enqueue to ease Vocallabs 429s. Transient 429s are auto-retried by
    initiate_parent_call's existing retry/DLQ, so pacing is politeness, not correctness.
  - Re-running fires again — run once per wave (use a fresh export each wave so
    numbers that have since connected drop out).
  - `only_last10=["9876543210", ...]` restricts the run to specific parent numbers.
    Use it with `dry_run=False, max_calls=1` to place ONE controlled TEST call to a
    number you own before a full wave.

Run (dry-run first):
  bench --site <site> execute \\
    tap_lms.summer_program.retry_failed_calls.retry_from_export \\
    --kwargs '{"csv_path": "/path/to/export.csv", "dry_run": true}'
"""

import csv
import time

import frappe

RETRYABLE = {"no-answer", "busy", "fail"}
CONNECTED = "completed"
DEFAULT_BATCH = "BT00000019"


def retry_from_export(csv_path, dry_run=True, batch=DEFAULT_BATCH,
                      batch_size=200, sleep_seconds=30, max_calls=None, only_last10=None):
    from tap_lms.summer_program.pe_dispatcher import _get_escalation_steps_for_pe

    dry_run = _as_bool(dry_run)
    batch_size = int(batch_size)
    sleep_seconds = int(sleep_seconds)
    if max_calls is not None:
        max_calls = int(max_calls)

    # ── 1. aggregate the export by parent phone (last-10) ──
    by_phone = {}   # last10 -> {"connected": bool, "retry": bool, "row": representative_row}
    total_rows = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            total_rows += 1
            last10 = _last10(row.get("phone_to"))
            if not last10:
                continue
            st = (row.get("call_status") or "").strip().lower()
            agg = by_phone.setdefault(last10, {"connected": False, "retry": False, "row": row})
            if st == CONNECTED:
                agg["connected"] = True
            elif st in RETRYABLE:
                agg["retry"] = True

    retry_phones = [p for p, a in by_phone.items() if a["retry"] and not a["connected"]]
    if only_last10:
        wanted = {_last10(x) for x in only_last10}
        retry_phones = [p for p in retry_phones if p in wanted]

    # ── 2/3. map to active PE + guardrails ──
    to_call = []          # (pe_name, step, last10, language)
    seen_pe = set()
    skip = {"no_student": 0, "no_active_pe": 0, "already_submitted": 0,
            "no_parent_call_step": 0, "dup_pe": 0}

    for last10 in retry_phones:
        srow = frappe.get_all("Student", filters={"phone": ["like", "%" + last10]},
                              fields=["name"], limit=1)
        if not srow:
            skip["no_student"] += 1
            continue
        student = srow[0]["name"]
        pes = frappe.get_all(
            "ProgramEnrollment",
            filters={"student": student, "batch": batch, "program_status": "active"},
            fields=["name", "submission_count"],
            order_by="creation desc", limit=1,
        )
        if not pes:
            skip["no_active_pe"] += 1
            continue
        pe = pes[0]
        if pe["name"] in seen_pe:
            skip["dup_pe"] += 1
            continue
        # No experiment-arm filter: control students (dormant/arm_b) can't actually be
        # called — initiate_parent_call no-ops them downstream via welcome_greeting == "None".
        if (pe.get("submission_count") or 0) and int(pe["submission_count"]) > 0:
            skip["already_submitted"] += 1
            continue
        # canonical parent_call escalation step (L-029 — resolved, never hardcoded)
        pe_doc = frappe.get_doc("ProgramEnrollment", pe["name"])
        step = _find_parent_call_step(_get_escalation_steps_for_pe(pe_doc))
        if not step:
            skip["no_parent_call_step"] += 1
            continue
        seen_pe.add(pe["name"])
        to_call.append((pe["name"], step, last10, (by_phone[last10]["row"].get("language") or "")))

    if max_calls is not None:
        to_call = to_call[:max_calls]

    # ── report ──
    print("=" * 62)
    print("RETRY FROM EXPORT   dry_run=%s   batch=%s" % (dry_run, batch))
    print("  CSV rows                 :", total_rows)
    print("  distinct phones          :", len(by_phone))
    print("  retry phones (never conn):", len(retry_phones))
    print("  --- skipped while mapping ---")
    for k in ("no_student", "no_active_pe", "already_submitted", "no_parent_call_step", "dup_pe"):
        print("    %-20s : %d" % (k, skip[k]))
    cap = "" if max_calls is None else "  (capped at %d)" % max_calls
    print("  >>> WOULD CALL           : %d%s" % (len(to_call), cap))
    for pe_name, step, ph, lang in to_call[:8]:
        print("       PE %s | ...%s | %s | step.order=%s" % (pe_name, ph[-4:], lang, _step_order(step)))
    print("=" * 62)

    if dry_run:
        print("DRY RUN — no calls placed. Re-run with dry_run=False to fire.")
        return _summary(total_rows, by_phone, retry_phones, skip, to_call, fired=0)

    # ── 4. live: enqueue paced ──
    fired = 0
    for i, (pe_name, step, ph, lang) in enumerate(to_call):
        frappe.enqueue(
            "tap_lms.summer_program.vocallabs.initiate_parent_call",
            queue="long", timeout=300,
            pe_name=pe_name, escalation_step=step,
        )
        fired += 1
        if batch_size and (i + 1) % batch_size == 0 and (i + 1) < len(to_call):
            print("  enqueued %d/%d — pausing %ss" % (fired, len(to_call), sleep_seconds))
            time.sleep(sleep_seconds)
    print("DONE — enqueued %d parent-call retries." % fired)
    return _summary(total_rows, by_phone, retry_phones, skip, to_call, fired=fired)


# ── helpers ──

def _as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _last10(phone):
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def _find_parent_call_step(steps):
    for s in steps or []:
        et = s.get("escalation_type") if isinstance(s, dict) else getattr(s, "escalation_type", None)
        if et == "parent_call":
            return s
    return None


def _step_order(step):
    return step.get("escalation_order") if isinstance(step, dict) else getattr(step, "escalation_order", None)


def _summary(total_rows, by_phone, retry_phones, skip, to_call, fired):
    return {
        "csv_rows": total_rows,
        "distinct_phones": len(by_phone),
        "retry_phones": len(retry_phones),
        "skipped": skip,
        "would_call": len(to_call),
        "fired": fired,
    }
