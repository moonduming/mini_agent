"""项目配置加载入口。

本地敏感配置保存在项目根目录的 config.ini 中；该文件不会提交到仓库。
仓库中的 config.example.ini 用于说明需要配置的字段。
"""

from configparser import ConfigParser, Error as ConfigParserError
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.ini"
CONFIG_PATH_ENV = "MINI_AGENT_CONFIG"


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    chat_model: str
    embedding_model: str


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str


@dataclass(frozen=True)
class QdrantSettings:
    host: str
    port: int
    collection: str


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    postgres: PostgresSettings
    qdrant: QdrantSettings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并校验配置；进程内只加载一次。"""
    config_path = Path(os.getenv(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)).expanduser()
    if not config_path.is_file():
        raise RuntimeError(
            f"配置文件不存在：{config_path}。"
            "请复制 config.example.ini 为 config.ini 并填写真实配置。"
        )

    # 禁用 % 插值，避免密码或密钥中包含 % 时被错误解析。
    parser = ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")

    try:
        settings = Settings(
            llm=LLMSettings(
                api_key=parser.get("llm", "api_key"),
                base_url=parser.get("llm", "base_url"),
                chat_model=parser.get("llm", "chat_model"),
                embedding_model=parser.get("llm", "embedding_model"),
            ),
            postgres=PostgresSettings(
                host=parser.get("postgres", "host"),
                port=parser.getint("postgres", "port"),
                dbname=parser.get("postgres", "dbname"),
                user=parser.get("postgres", "user"),
                password=parser.get("postgres", "password"),
            ),
            qdrant=QdrantSettings(
                host=parser.get("qdrant", "host"),
                port=parser.getint("qdrant", "port"),
                collection=parser.get("qdrant", "collection"),
            ),
        )
    except (ConfigParserError, ValueError) as error:
        raise RuntimeError(f"配置文件格式错误：{config_path}，{error}") from error

    if not settings.llm.api_key.strip():
        raise RuntimeError(f"配置文件中的 llm.api_key 不能为空：{config_path}")
    return settings
