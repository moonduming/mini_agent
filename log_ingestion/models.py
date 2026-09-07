"""日志入库使用的领域对象。"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class LogInsertRecord:
    """一条已经清洗完成、可直接插入 ``logs`` 表的日志记录。

    ``occurred_at`` 使用无时区的 ``datetime``，因为原始 OpenStack 日志
    本身不携带时区信息；它对应数据库的 ``TIMESTAMP(6)`` 字段。
    """

    log_type: str
    file_path: str
    source: str | None
    occurred_at: datetime
    pid: int | None
    level: str | None
    logger: str | None
    message: str | None
    raw_log: str
    is_error: bool
    error_kind: str | None
    error_label: str | None
    error_template: str | None
    error_fingerprint: str | None
    classifier_version: str

    def db_values(self) -> tuple[object, ...]:
        """按 INSERT 语句的字段顺序返回数据库参数。"""
        return (
            self.log_type,
            self.file_path,
            self.source,
            self.occurred_at,
            self.pid,
            self.level,
            self.logger,
            self.message,
            self.raw_log,
            self.is_error,
            self.error_kind,
            self.error_label,
            self.error_template,
            self.error_fingerprint,
            self.classifier_version,
        )
