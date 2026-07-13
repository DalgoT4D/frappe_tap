from __future__ import annotations

import frappe


def execute() -> None:
    frappe.db.rollback()

    if getattr(frappe.db, "db_type", None) != "postgres":
        return

    frappe.db.sql(
        """
        UPDATE "tabStudent Bulk Import Job"
           SET processing_log = json_build_object(
               'entries',
               CASE
                   WHEN processing_log IS NULL THEN '[]'::json
                   WHEN json_typeof(processing_log) = 'object'
                        AND processing_log::jsonb ? 'entries'
                        AND json_typeof(processing_log->'entries') = 'array'
                       THEN processing_log->'entries'
                   WHEN json_typeof(processing_log) = 'array'
                       THEN processing_log
                   WHEN json_typeof(processing_log) = 'string'
                       THEN json_build_array(
                           json_build_object(
                               'timestamp', NULL,
                               'message', trim(both '"' from processing_log::text)
                           )
                       )::json
                   ELSE '[]'::json
               END
           )
         WHERE processing_log IS NULL
            OR json_typeof(processing_log) != 'object'
            OR NOT (processing_log::jsonb ? 'entries')
            OR json_typeof(processing_log->'entries') != 'array'
        """
    )
    frappe.db.commit()
