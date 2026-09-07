"""基于规则的 OpenStack 错误特征提取。

分类结果用于日志聚合，不代表已经确定的根因。原始日志始终保存在
``raw_log`` 中，分析时仍应结合代表日志进行确认。
"""

import hashlib
import re
from dataclasses import dataclass


CLASSIFIER_VERSION = "rules-v1"
ERROR_LEVELS = frozenset({"ERROR", "CRITICAL"})

# 匹配常见的异常类名，例如：
# ConnectionError
# ValueError
# nova.exception.ComputeHostNotFoundException
# sqlalchemy.exc.DatabaseError
# 要求异常名以 Error / Exception / Fault / Failure 结尾。
# 如果后面存在 ": 错误详情"，也会顺便匹配 detail。
EXCEPTION_PATTERN = re.compile(
    r"(?P<name>(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*"
    r"(?:Error|Exception|Fault|Failure))"
    r"(?:\s*:\s*(?P<detail>.*))?"
)

# 匹配一部分 OpenStack 中常见、但名称不一定以
# Error / Exception / Fault / Failure 结尾的异常。
# 例如：
# NoValidHost
# MessagingTimeout
# ServiceUnavailable
# NotAuthorized
# Forbidden
# NotFound
# Conflict
OPENSTACK_EXCEPTION_PATTERN = re.compile(
    r"\b(?P<name>NoValidHost|MessagingTimeout|ServiceUnavailable|"
    r"NotAuthorized|Unauthorized|Forbidden|NotFound|Conflict|"
    r"ResourceBusy|InvalidInput)\b"
)


# 匹配 HTTP 4xx / 5xx 状态码。
# 支持类似：
# HTTP 404
# HTTP status 500
# HTTP status code 503
# status 404
# status code=500
# 最终提取出的 code 为 404、500、503 等。
HTTP_STATUS_PATTERN = re.compile(
    r"\b(?:HTTP(?:\s+status(?:\s+code)?)?|status(?:\s+code)?\s*[=:]?)"
    r"\s*(?P<code>[45]\d{2})\b",
    re.IGNORECASE,
)

# 匹配标准 UUID，例如：
# 550e8400-e29b-41d4-a716-446655440000
# 用于归一化日志中的动态资源 ID，
# 避免同一种错误因为 UUID 不同而生成不同 fingerprint。
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

# 匹配 OpenStack 常见 request id，例如：
# req-addc1839-2ed5-4778-b57e-5854eb7b8b09
# 归一化时替换成 <request_id>，
# 避免请求 ID 不同导致同类日志无法聚合。
REQUEST_ID_PATTERN = re.compile(
    r"\breq-[0-9a-f-]{36}\b",
    re.IGNORECASE,
)

# 匹配ip地址
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# 匹配常见时间戳
TIMESTAMP_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)

# 匹配十六进制形式的 ID
HEX_ID_PATTERN = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)

# 匹配一个或多个连续空白字符，
# 包括普通空格、Tab、换行等。
# 归一化时统一压缩成一个普通空格。
WHITESPACE_PATTERN = re.compile(r"\s+")

MESSAGE_LABELS = (
    ("connection refused", "Connection refused"),
    ("connection reset", "Connection reset"),
    ("timed out", "Timeout"),
    ("timeout", "Timeout"),
    ("no valid host", "No valid host"),
    ("authentication failed", "Authentication failed"),
    ("permission denied", "Permission denied"),
    ("disk full", "Disk full"),
    ("out of memory", "Out of memory"),
)


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    is_error: bool
    error_kind: str | None
    error_label: str | None
    error_template: str | None
    error_fingerprint: str | None
    classifier_version: str


def classify_log(
    *,
    log_type: str,
    level: str | None,
    logger: str | None,
    message: str | None,
    raw_log: str,
) -> ErrorClassification:
    """提取可稳定聚合的错误特征。

    提取顺序：异常类型 -> OpenStack 常见异常 -> HTTP 状态 -> 常见故障短语
    -> 归一化首条消息。无法可靠命名时仍生成 unknown 指纹，以避免丢失
    ERROR/CRITICAL 日志。
    """
    if (level or "").upper() not in ERROR_LEVELS:
        return ErrorClassification(
            is_error=False,
            error_kind=None,
            error_label=None,
            error_template=None,
            error_fingerprint=None,
            classifier_version=CLASSIFIER_VERSION,
        )

    text = message or raw_log
    kind, label, template_source = _classify_error_text(text)
    template = normalize_error_template(template_source)
    fingerprint = _build_fingerprint(
        log_type=log_type,
        logger=logger,
        error_kind=kind,
        error_template=template,
    )
    return ErrorClassification(
        is_error=True,
        error_kind=kind,
        error_label=label,
        error_template=template,
        error_fingerprint=fingerprint,
        classifier_version=CLASSIFIER_VERSION,
    )


def _classify_error_text(text: str) -> tuple[str, str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Traceback 的异常通常出现在最后一行，因此倒序扫描。
    for line in reversed(lines):
        match = EXCEPTION_PATTERN.search(line)
        if match:
            exception_name = match.group("name").rsplit(".", maxsplit=1)[-1]
            return "exception", exception_name, line

        openstack_match = OPENSTACK_EXCEPTION_PATTERN.search(line)
        if openstack_match:
            return "exception", openstack_match.group("name"), line

    for line in lines:
        match = HTTP_STATUS_PATTERN.search(line)
        if match:
            return "http_error", f"HTTP {match.group('code')}", line

    lowered_text = text.lower()
    for phrase, label in MESSAGE_LABELS:
        if phrase in lowered_text:
            matching_line = next(
                (line for line in lines if phrase in line.lower()),
                phrase,
            )
            return "message", label, matching_line

    return "unknown", "Unclassified error", _first_meaningful_line(lines)


def _first_meaningful_line(lines: list[str]) -> str:
    for line in lines:
        if line == "Traceback (most recent call last):":
            continue
        if line.startswith('File "') or line.startswith("File '"):
            continue
        return line
    return "error without a readable message"


def normalize_error_template(value: str) -> str:
    """移除请求相关的动态值，使同类错误能聚合到同一指纹。"""
    normalized = REQUEST_ID_PATTERN.sub("<request_id>", value)
    normalized = UUID_PATTERN.sub("<uuid>", normalized)
    normalized = IPV4_PATTERN.sub("<ip>", normalized)
    normalized = TIMESTAMP_PATTERN.sub("<timestamp>", normalized)
    normalized = HEX_ID_PATTERN.sub("<hex>", normalized)
    normalized = WHITESPACE_PATTERN.sub(" ", normalized).strip()
    return normalized[:500] or "error without a readable message"


def _build_fingerprint(
    *,
    log_type: str,
    logger: str | None,
    error_kind: str,
    error_template: str,
) -> str:
    value = "|".join(
        (
            CLASSIFIER_VERSION,
            log_type.lower(),
            (logger or "unknown").lower(),
            error_kind,
            error_template.lower(),
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
