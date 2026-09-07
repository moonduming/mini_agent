from typing import Any, Sequence
import psycopg

from config import get_settings
from log_ingestion.models import LogInsertRecord


INSERT_LOGS_SQL = """
    INSERT INTO logs (
        log_type,
        file_path,
        source,
        occurred_at,
        pid,
        level,
        logger,
        message,
        raw_log,
        is_error,
        error_kind,
        error_label,
        error_template,
        error_fingerprint,
        classifier_version
    )
    VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s
    )
"""


def get_postgres_connection():
    database = get_settings().postgres
    return psycopg.connect(
        host=database.host,
        port=database.port,
        dbname=database.dbname,
        user=database.user,
        password=database.password,
    )


def insert_log_batch(
    connection: Any,
    log_batch: Sequence[LogInsertRecord],
) -> int:
    """批量写入已经清洗为 LogInsertRecord 的日志。"""
    if not log_batch:
        return 0

    log_rows = [record.db_values() for record in log_batch]

    try:
        with connection.cursor() as cursor:
            cursor.executemany(INSERT_LOGS_SQL, log_rows)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return len(log_rows)
