"""One-time / operator-run migration scripts for the Summer Program.

These are NOT Frappe `patches/` (which run automatically on `bench migrate`).
Everything here is an explicitly-invoked whitelisted method or console helper
that an operator runs by hand, typically once, against a specific batch.
"""


def coerce_bool(value):
    """Whitelisted-method bool coercion used by every migration endpoint.

    HTTP-delivered string args make the literal "False" / "0" truthy in
    Python (`bool("False") is True`). Treat the usual falsey spellings as
    False; everything else follows normal Python truthiness.

    Shared with `sweep_migration.migrate_behind_students_to_escalation*` and
    `main_collection_backfill.backfill_main_collection` — L-074 anti-drift
    (extracted by CR-029 review feedback after the second copy landed).
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none")
    return bool(value)
