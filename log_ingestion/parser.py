import re
from datetime import datetime

from log_ingestion.error_classifier import classify_log
from log_ingestion.models import LogInsertRecord


LOG_TYPE = "openstack"

OPENSTACK_LOG_RECORD_START_PATTERN = re.compile(
    r"^[A-Za-z0-9_-]+\.log(?:\.\d+)?\.\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}(?:\s|$)"
)

OPENSTACK_LOG_HEADER_PATTERN = re.compile(
    r"^(?P<source>\S+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"(?P<pid>\d+)\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"(?P<logger>\S+)"
    r"(?:[ \t]+(?P<remainder>.*))?$",
    re.DOTALL,
)


def is_openstack_log_record_start(line: str) -> bool:
    return OPENSTACK_LOG_RECORD_START_PATTERN.match(line) is not None


def parse_openstack_log_record(
    raw_log_record: str,
    file_path: str,
) -> LogInsertRecord:
    record = raw_log_record.rstrip("\r\n")
    header_match = OPENSTACK_LOG_HEADER_PATTERN.fullmatch(record)
    if header_match is None:
        preview = record[:200].replace("\n", "\\n")
        raise ValueError(f"无法解析日志头: {preview}")

    remainder = header_match.group("remainder")

    occurred_at = datetime.fromisoformat(
        f"{header_match.group('date')}T{header_match.group('time')}"
    )

    message = remainder.strip() if remainder else None
    classification = classify_log(
        log_type=LOG_TYPE,
        level=header_match.group("level"),
        logger=header_match.group("logger"),
        message=message,
        raw_log=record,
    )

    return LogInsertRecord(
        log_type=LOG_TYPE,
        file_path=file_path,
        source=header_match.group("source"),
        occurred_at=occurred_at,
        pid=int(header_match.group("pid")),
        level=header_match.group("level"),
        logger=header_match.group("logger"),
        message=message,
        raw_log=record,
        is_error=classification.is_error,
        error_kind=classification.error_kind,
        error_label=classification.error_label,
        error_template=classification.error_template,
        error_fingerprint=classification.error_fingerprint,
        classifier_version=classification.classifier_version,
    )


def parse_openstack_log_records(
    raw_log_records: list[str],
    file_path: str,
) -> list[LogInsertRecord]:
    return [parse_openstack_log_record(record, file_path) for record in raw_log_records]
