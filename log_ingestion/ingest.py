from pathlib import Path
from log_ingestion.parser import (
    is_openstack_log_record_start,
    parse_openstack_log_records,
)
from log_ingestion.postgres_writer import get_postgres_connection, insert_log_batch


PROJECT_ROOT = Path(__file__).resolve().parents[1]

LOG_FILE_PATHS = [
    PROJECT_ROOT / "data/raw_logs/full/openstack/openstack_abnormal.log",
    PROJECT_ROOT / "data/raw_logs/full/openstack/openstack_normal1.log",
    PROJECT_ROOT / "data/raw_logs/full/openstack/openstack_normal2.log"
]


def iter_file_lines(file_path: str):
    with open(file_path, "r", encoding="utf-8") as file:
        for line in file:
            yield line


def iter_parsed_log_batches():
    for file_path in LOG_FILE_PATHS:
        file_path = str(file_path)
        current_log_lines = []
        raw_log_records = []
        for line in iter_file_lines(file_path):
            # 判断是否是一条新日志开头
            starts_new_record = is_openstack_log_record_start(line)
            if starts_new_record and current_log_lines:
                # 一条日志完结，加入列表
                raw_log_records.append("".join(current_log_lines))
                current_log_lines = [line]
            else:
                # 日志未完结
                current_log_lines.append(line)

            if len(raw_log_records) >= 50:
                parsed_batch = parse_openstack_log_records(raw_log_records, file_path)
                raw_log_records = []
                yield parsed_batch

        if current_log_lines:
            raw_log_records.append("".join(current_log_lines))
        if raw_log_records:
            yield parse_openstack_log_records(raw_log_records, file_path)


def ingest_logs():
    with get_postgres_connection() as connection:
        for parsed_batch in iter_parsed_log_batches():
            inserted_count = insert_log_batch(connection, parsed_batch)
            print(f"已写入 {inserted_count} 条日志")


if __name__ == "__main__":
    ingest_logs()
