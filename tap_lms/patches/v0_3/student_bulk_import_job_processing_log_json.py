from __future__ import annotations

import frappe


def execute() -> None:
    frappe.db.rollback()

    if getattr(frappe.db, "db_type", None) != "postgres":
        return

    table_exists = frappe.db.sql(
        """
        SELECT 1
          FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name = 'tabStudent Bulk Import Job'
        """
    )
    if not table_exists:
        return

    column_info = frappe.db.sql(
        """
        SELECT data_type
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'tabStudent Bulk Import Job'
           AND column_name = 'processing_log'
        """
    )
    if not column_info:
        return

    data_type = (column_info[0][0] or "").lower()
    if data_type in {"json", "jsonb"}:
        frappe.db.sql(
            """
            UPDATE "tabStudent Bulk Import Job"
               SET processing_log = '[]'::json
             WHERE processing_log IS NULL
            """
        )
        frappe.db.commit()
        return

    frappe.db.sql(
        """
        CREATE OR REPLACE FUNCTION __tap_safe_processing_log_json(raw_value text)
        RETURNS json
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF raw_value IS NULL OR btrim(raw_value) = '' THEN
                RETURN '[]'::json;
            END IF;

            BEGIN
                RETURN raw_value::json;
            EXCEPTION
                WHEN others THEN
                    RETURN json_build_array(
                        json_build_object(
                            'timestamp', NULL,
                            'message', raw_value
                        )
                    )::json;
            END;
        END;
        $$;
        """
    )

    try:
        frappe.db.sql(
            """
            ALTER TABLE "tabStudent Bulk Import Job"
            ALTER COLUMN processing_log
            TYPE json
            USING __tap_safe_processing_log_json(processing_log)
            """
        )
        frappe.db.sql(
            """
            UPDATE "tabStudent Bulk Import Job"
               SET processing_log = '[]'::json
             WHERE processing_log IS NULL
            """
        )
        frappe.db.commit()
    finally:
        frappe.db.sql("DROP FUNCTION IF EXISTS __tap_safe_processing_log_json(text)")
        frappe.db.commit()
