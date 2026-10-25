import csv
import io
import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

import psycopg2
from psycopg2 import sql


CONFIRM_ENV = "BUS_TERMINAL_CONFIRM_DB_MIGRATION"
CONFIRM_VALUE = "MIGRATE_SQLITE_TO_POSTGRES"
SOURCE_ENV = "BUS_TERMINAL_SQLITE_SOURCE"
TARGET_ENV = "TARGET_DATABASE_URL"
COPY_BATCH_SIZE = 10_000
MANAGED_SCHEMA_TABLES = {"schema_migrations"}


def require_confirmation() -> None:
    if os.getenv(CONFIRM_ENV) != CONFIRM_VALUE:
        raise RuntimeError(
            f"Migration refused. Set {CONFIRM_ENV}={CONFIRM_VALUE} after taking a backup."
        )


def get_source_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def get_source_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    escaped = table.replace('"', '""')
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")')]


def get_target_columns(conn, table: str) -> dict[str, str]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return {row[0]: row[1] for row in cursor.fetchall()}


def target_table_count(conn, table: str) -> int:
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
        )
        return int(cursor.fetchone()[0])


def normalize_value(value, target_type: str):
    if value is None:
        return None
    if target_type == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if target_type == "date":
        text = str(value).strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    if target_type in {"json", "jsonb"}:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        text = str(value).strip()
        if not text:
            return None
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            return json.dumps(text, ensure_ascii=False)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def write_copy_batch(cursor, table: str, columns: list[str], rows: list[tuple]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(
        tuple("\\N" if value is None else value for value in row)
        for row in rows
    )
    buffer.seek(0)
    copy_sql = sql.SQL(
        "COPY {} ({}) FROM STDIN WITH (FORMAT CSV, NULL '\\N')"
    ).format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    cursor.copy_expert(copy_sql.as_string(cursor.connection), buffer)


def copy_table(
    source: sqlite3.Connection,
    target,
    table: str,
    source_columns: list[str],
    target_columns: dict[str, str],
) -> int:
    missing = [column for column in source_columns if column not in target_columns]
    if missing:
        raise RuntimeError(f"Target table {table} is missing source columns: {missing}")

    escaped_table = table.replace('"', '""')
    escaped_columns = [column.replace('"', '""') for column in source_columns]
    select_columns = ", ".join(f'"{column}"' for column in escaped_columns)
    source_cursor = source.execute(f'SELECT {select_columns} FROM "{escaped_table}"')
    copied = 0
    with target.cursor() as target_cursor:
        while True:
            batch = source_cursor.fetchmany(COPY_BATCH_SIZE)
            if not batch:
                break
            normalized = [
                tuple(
                    normalize_value(value, target_columns[column])
                    for column, value in zip(source_columns, row)
                )
                for row in batch
            ]
            write_copy_batch(target_cursor, table, source_columns, normalized)
            copied += len(normalized)
    target.commit()
    return copied


def migrate() -> int:
    require_confirmation()
    source_path = Path(os.getenv(SOURCE_ENV, "/app/data/movies.db")).resolve()
    target_url = str(os.getenv(TARGET_ENV, "")).strip()
    if not source_path.is_file():
        raise RuntimeError(f"SQLite source does not exist: {source_path.name}")
    if not target_url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError(f"{TARGET_ENV} must be a PostgreSQL URL")

    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    target = psycopg2.connect(target_url)
    try:
        source_tables = get_source_tables(source)
        migration_tables = [
            table for table in source_tables if table not in MANAGED_SCHEMA_TABLES
        ]

        table_metadata = {}
        for table in migration_tables:
            source_columns = get_source_columns(source, table)
            target_columns = get_target_columns(target, table)
            if not target_columns:
                raise RuntimeError(f"Target table is missing: {table}")
            table_metadata[table] = (source_columns, target_columns)
            existing_count = target_table_count(target, table)
            if existing_count:
                raise RuntimeError(
                    f"Target table {table} is not empty ({existing_count} rows); migration aborted."
                )

        copied_counts = {}
        for table in migration_tables:
            source_columns, target_columns = table_metadata[table]
            copied = copy_table(
                source,
                target,
                table,
                source_columns,
                target_columns,
            )
            copied_counts[table] = copied
            print(f"copied {table}: {copied}", flush=True)

        mismatches = []
        for table, copied in copied_counts.items():
            source_count = int(
                source.execute(
                    f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            )
            target_count = target_table_count(target, table)
            if source_count != copied or source_count != target_count:
                mismatches.append((table, source_count, copied, target_count))

        if mismatches:
            raise RuntimeError(f"Row count verification failed: {mismatches}")

        with target.cursor() as cursor:
            cursor.execute("ANALYZE")
        target.commit()
        print(f"migration complete: {len(copied_counts)} tables", flush=True)
        return 0
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    raise SystemExit(migrate())
