# Remove (or archive) Student records that have no phone number.
#
# Phone is reqd=1 on Student, so phone-less rows are legacy data that bypassed
# validation (bulk import / db.set_value). They can't be messaged via Glific and
# can't be de-duplicated (the onboarding dedup matches on name1 + phone), so they
# are dead weight before the 75k onboarding run.
#
# 24 doctypes Link to Student. A hard delete is blocked by Frappe link-integrity
# unless dependents are removed first; this script discovers every Link-to-Student
# field dynamically and clears standalone dependents before deleting the parent.
#
# === SAFETY ===
#   1. Take a full backup FIRST:  cd ~/frappe-bench && bench --site <site> backup --with-files
#   2. Run in REPORT mode first (default) and read the numbers.
#   3. Prefer ARCHIVE (reversible: sets status='inactive') over DELETE unless you
#      are certain. DELETE is permanent.
#
# Run:
#   cd ~/frappe-bench && bench --site <site> console
#   >>> exec(open("apps/tap_lms/scripts/cleanup_phoneless_students.py").read())

import csv
import frappe
from frappe.utils import now

# ----------------------------------------------------------------------------- CONFIG
MODE       = "delete"          # "report" | "archive" | "delete"
CONFIRM    = True             # must be True for archive/delete to actually run
BATCH_SIZE = 200               # commit cadence
BACKUP_CSV = f"/tmp/phoneless_students_{frappe.utils.nowdate()}.csv"
# -----------------------------------------------------------------------------

def find_phoneless():
    """Student names where phone is NULL or blank."""
    null_phone = set(frappe.get_all("Student", filters={"phone": ["is", "not set"]}, pluck="name"))
    empty_phone = set(frappe.get_all("Student", filters={"phone": ""}, pluck="name"))
    return sorted(null_phone | empty_phone)

def link_fields_to_student():
    """Every (doctype, fieldname) that Links to Student, split into child vs standalone."""
    fields = frappe.get_all("DocField",
                            filters={"fieldtype": "Link", "options": "Student"},
                            fields=["parent", "fieldname"])
    fields += frappe.get_all("Custom Field",
                             filters={"fieldtype": "Link", "options": "Student"},
                             fields=["dt as parent", "fieldname"])
    standalone, child = [], []
    for f in fields:
        try:
            meta = frappe.get_meta(f.parent)
        except Exception:
            continue
        (child if meta.istable else standalone).append((f.parent, f.fieldname))
    return standalone, child

def backup(names):
    """Dump full Student rows + enrollment count to CSV before any mutation."""
    if not names:
        return
    rows = frappe.get_all("Student", filters={"name": ["in", names]}, fields=["*"])
    with open(BACKUP_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  backup written: {BACKUP_CSV} ({len(rows)} rows)")

# ----------------------------------------------------------------------------- RUN
print(f"\n=== Phone-less Student cleanup — MODE={MODE} CONFIRM={CONFIRM} @ {now()} ===")
names = find_phoneless()
total = len(names)
print(f"Phone-less Students: {total}")

if not total:
    print("Nothing to do.\n")
else:
    standalone, child = link_fields_to_student()

    # Blast-radius report: how many dependents / how much real activity exists
    _enr_child = frappe.get_meta("Student").get_field("enrollment").options
    with_enroll = len(set(frappe.get_all(_enr_child,
                                     filters={"parenttype": "Student", "parent": ["in", names]},
                                     pluck="parent")))
    with_glific = len(frappe.get_all("Student",
                                     filters={"name": ["in", names], "glific_id": ["is", "set"]}))
    print(f"  ...of which have enrollments: {with_enroll}")
    print(f"  ...of which have a Glific ID: {with_glific}")
    print(f"  standalone dependent doctypes: {len(standalone)} | child tables (cascade): {len(child)}")
    print(f"  e.g. standalone: {[f'{d}.{f}' for d, f in standalone[:8]]}")

    if MODE == "report":
        print("\nREPORT only. Set MODE='archive' or 'delete' and CONFIRM=True to act.\n")
    elif not CONFIRM:
        print(f"\nMODE={MODE} requested but CONFIRM is False — refusing to mutate. No changes made.\n")
    else:
        backup(names)
        done = 0
        for sname in names:
            try:
                if MODE == "archive":
                    frappe.db.set_value("Student", sname, "status", "inactive",
                                        update_modified=False)
                elif MODE == "delete":
                    # remove standalone dependents first (child tables cascade with parent)
                    for dt, fieldname in standalone:
                        for dep in frappe.get_all(dt, filters={fieldname: sname}, pluck="name"):
                            frappe.delete_doc(dt, dep, force=True, ignore_permissions=True,
                                              delete_permanently=True)
                    frappe.delete_doc("Student", sname, force=True, ignore_permissions=True,
                                      delete_permanently=True)
                done += 1
                if done % BATCH_SIZE == 0:
                    frappe.db.commit()
                    print(f"  {MODE}: {done}/{total}")
            except Exception as e:
                frappe.db.rollback()
                print(f"  FAILED {sname}: {e}")
        frappe.db.commit()
        print(f"\n{MODE.upper()} complete: {done}/{total}. Backup: {BACKUP_CSV}")
        print("NOTE: Glific contacts are external — this does not delete them in Glific.\n")
