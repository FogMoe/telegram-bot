# Description: Configuration file for the bot
# replace with secure storage (e.g., environment variable / secrets manager)
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# 仓库根目录（.env、resources/、logs/ 所在处）。项目以可编辑方式安装（uv sync 的默认行为，
# 镜像里也是），这里的 __file__ 始终在 src/ 下，而不是 site-packages 里。
BASE_DIR = Path(__file__).resolve().parents[3]

ENV_FILE_VAR = "BOT_ENV_FILE"


def resolve_env_file(environ: Mapping[str, str] | None = None) -> Path | None:
    """决定从哪个文件读取配置：BOT_ENV_FILE 未设置时用仓库根的 .env，
    设为路径则读取该文件，设为空字符串则完全不读 env 文件（只用进程环境变量）。"""
    override = (os.environ if environ is None else environ).get(ENV_FILE_VAR)
    if override is None:
        return BASE_DIR / ".env"
    override = override.strip()
    return Path(override) if override else None


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=resolve_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    GEMINI_API_KEY: str | None = None
    GEMINI_API_BASE: str | None = None
    GEMINI_OPENAI_COMPATIBLE: bool = False
    GEMINI_CHAT_MODEL: str | None = None
    GEMINI_CHAT_FALLBACK_MODEL: str | None = None
    GEMINI_RECAP_MODEL: str | None = None
    GEMINI_SUMMARY_MODEL: str | None = None
    GEMINI_SUMMARY_FALLBACK_MODEL: str | None = None
    GEMINI_TRANSLATE_MODEL: str | None = None
    GEMINI_VISION_MODEL: str | None = None
    GEMINI_CLASSIFIER_MODEL: str | None = None
    GEMINI_ADVISOR_MODEL: str | None = None

    ZAI_API_KEY: str | None = None
    ZAI_API_BASE: str | None = None
    ZHIPU_CHAT_MODEL: str | None = None
    ZHIPU_RECAP_MODEL: str | None = None
    ZHIPU_SUMMARY_MODEL: str | None = None
    ZHIPU_TRANSLATE_MODEL: str | None = None
    ZHIPU_VISION_MODEL: str | None = None
    ZHIPU_CLASSIFIER_MODEL: str | None = None
    ZHIPU_ADVISOR_MODEL: str | None = None

    SERPAPI_API_KEY: str | None = None
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CONNECT_TIMEOUT: float = 10.0
    TELEGRAM_READ_TIMEOUT: float = 30.0
    TELEGRAM_WRITE_TIMEOUT: float = 30.0
    TELEGRAM_POOL_TIMEOUT: float = 10.0
    TELEGRAM_GET_UPDATES_TIMEOUT: int = 30
    TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT: float = 10.0
    TELEGRAM_GET_UPDATES_READ_TIMEOUT: float = 35.0
    TELEGRAM_GET_UPDATES_WRITE_TIMEOUT: float = 30.0
    TELEGRAM_GET_UPDATES_POOL_TIMEOUT: float = 10.0

    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str | None = None
    OPENAI_CHAT_MODEL: str | None = None
    OPENAI_RECAP_MODEL: str | None = None
    OPENAI_SUMMARY_MODEL: str | None = None
    OPENAI_TRANSLATE_MODEL: str | None = None
    OPENAI_VISION_MODEL: str | None = None
    OPENAI_CLASSIFIER_MODEL: str | None = None
    OPENAI_ADVISOR_MODEL: str | None = None

    OPENROUTER_API_KEY: str | None = None
    OPENROUTER_API_BASE: str = "https://openrouter.ai/api/v1"
    OPENROUTER_CHAT_MODEL: str | None = None
    OPENROUTER_RECAP_MODEL: str | None = None
    OPENROUTER_SUMMARY_MODEL: str | None = None
    OPENROUTER_TRANSLATE_MODEL: str | None = None
    OPENROUTER_VISION_MODEL: str | None = None
    OPENROUTER_CLASSIFIER_MODEL: str | None = None
    OPENROUTER_ADVISOR_MODEL: str | None = None

    FOGMOE_API_KEY: str | None = None
    FOGMOE_API_BASE: str | None = None
    FOGMOE_CHAT_MODEL: str | None = None
    FOGMOE_RECAP_MODEL: str | None = None
    FOGMOE_SUMMARY_MODEL: str | None = None
    FOGMOE_TRANSLATE_MODEL: str | None = None
    FOGMOE_VISION_MODEL: str | None = None
    FOGMOE_CLASSIFIER_MODEL: str | None = None
    FOGMOE_ADVISOR_MODEL: str | None = None

    AZURE_OPENAI_API_KEY: str | None = None
    AZURE_OPENAI_API_ENDPOINT: str | None = None
    AZURE_OPENAI_API_VERSION: str | None = None
    AZURE_OPENAI_DEPLOYMENT: str | None = None
    AZURE_OPENAI_BASE_URL: str | None = None
    AZURE_OPENAI_CHAT_MODEL: str | None = None
    AZURE_OPENAI_RECAP_MODEL: str | None = None
    AZURE_OPENAI_SUMMARY_MODEL: str | None = None
    AZURE_OPENAI_TRANSLATE_MODEL: str | None = None
    AZURE_OPENAI_VISION_MODEL: str | None = None
    AZURE_OPENAI_CLASSIFIER_MODEL: str | None = None
    AZURE_OPENAI_ADVISOR_MODEL: str | None = None

    SILICONFLOW_API_KEY: str | None = None
    SILICONFLOW_API_BASE: str = "https://api.siliconflow.cn/v1"
    SILICONFLOW_CHAT_MODEL: str = "deepseek-ai/DeepSeek-V4-Flash"
    SILICONFLOW_RECAP_MODEL: str = "deepseek-ai/DeepSeek-V4-Flash"
    SILICONFLOW_SUMMARY_MODEL: str = "deepseek-ai/DeepSeek-V4-Flash"
    SILICONFLOW_TRANSLATE_MODEL: str = "deepseek-ai/DeepSeek-V4-Flash"
    SILICONFLOW_VISION_MODEL: str = "deepseek-ai/DeepSeek-V4-Flash"
    SILICONFLOW_CLASSIFIER_MODEL: str = "deepseek-ai/DeepSeek-V4-Flash"
    SILICONFLOW_ADVISOR_MODEL: str | None = None

    AI_RECAP_PROVIDER: str | None = None
    AI_RECAP_FALLBACK_PROVIDER: str | None = None
    AI_SUMMARY_PROVIDER: str | None = None
    AI_SUMMARY_FALLBACK_PROVIDER: str | None = None
    AI_TRANSLATE_PROVIDER: str | None = None
    AI_TRANSLATE_FALLBACK_PROVIDER: str | None = None
    AI_VISION_PROVIDER: str | None = None
    AI_VISION_FALLBACK_PROVIDER: str | None = None
    AI_CLASSIFIER_PROVIDER: str | None = None
    AI_CLASSIFIER_FALLBACK_PROVIDER: str | None = None
    AI_ADVISOR_PROVIDER: str | None = None
    AI_ADVISOR_FALLBACK_PROVIDER: str | None = None
    AI_ADVISOR_TIMEOUT_SECONDS: int = Field(default=120, ge=5, le=180)
    AI_ADVISOR_MAX_CALLS_PER_REQUEST: int = Field(default=1, ge=0, le=3)
    AI_ADVISOR_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=300, ge=1, le=86400)
    AI_ADVISOR_RATE_LIMIT_MAX_CALLS: int = Field(default=3, ge=1, le=100)
    AI_ADVISOR_MAX_CONCURRENT_REQUESTS: int = Field(default=3, ge=1, le=50)
    AI_CHAT_COMPLETION_TIMEOUT_SECONDS: int = Field(default=300, ge=30, le=600)
    AI_CHAT_ORDER: str = ""
    AI_CHAT_TEXT_ONLY_MODELS: str = "deepseek-ai/DeepSeek-V4-Flash"

    # Token estimates apply DEFAULT_GUARD_RATIO before comparing these limits.
    CHAT_TOKEN_WARN_LIMIT: int = 114000
    CHAT_TOKEN_LIMIT: int = 120000
    CHAT_CONTEXT_HARD_LIMIT_RATIO: float = Field(default=1.25, ge=1.0, le=2.0)
    CHAT_CONTEXT_SAFETY_TOKENS: int = Field(default=2048, ge=0, le=32000)
    CHAT_BATCH_WINDOW_SECONDS: float = 1.0

    # 运行时：准入、整轮截止时间、线程适配器与指标，取值依据见 docs/runtime.md。
    CHAT_MAX_CONCURRENT_TURNS: int = Field(default=32, ge=1, le=512)
    CHAT_MAX_QUEUED_TURNS: int = Field(default=32, ge=0, le=1024)
    CHAT_MAX_PENDING_PER_USER: int = Field(default=3, ge=1, le=20)
    CHAT_QUEUE_MAX_WAIT_SECONDS: float = Field(default=20.0, ge=0, le=300)
    CHAT_TURN_DEADLINE_SECONDS: float = Field(default=360.0, ge=30, le=3600)
    TELEGRAM_CONCURRENT_UPDATES: int = Field(default=128, ge=1, le=1024)
    BLOCKING_TOOL_THREADS: int = Field(default=8, ge=1, le=64)
    BLOCKING_IO_THREADS: int = Field(default=4, ge=1, le=32)
    RUNTIME_METRICS_LOG_INTERVAL_SECONDS: float = Field(default=300.0, ge=0, le=86400)
    RUNTIME_SHUTDOWN_GRACE_SECONDS: float = Field(default=8.0, ge=0, le=300)

    TELEGRAM_HISTORY_RATE_WINDOW_SECONDS: float = Field(default=0.5, gt=0, le=60)
    TELEGRAM_HISTORY_RATE_MAX_EVENTS: int = Field(default=8, ge=1, le=100)

    JUDGE0_API_URL: str = "https://ce.judge0.com"
    JUDGE0_API_KEY: str | None = None
    E2B_API_KEY: str | None = None

    IMAGE_GEN_API_URL: str = ""
    IMAGE_GEN_API_TOKEN: str = ""
    IMAGE_GEN_TIMEOUT: int = 45

    FISH_AUDIO_API_KEY: str | None = None
    FISH_AUDIO_MODEL: str = "s2.1-pro-free"
    FISH_AUDIO_REFERENCE_ID: str = "dc020cb237df4248907565718715b20b"

    ADMIN_USER_ID: int = 1002288404
    NEW_USER_BONUS_COINS: int = 10

    MYSQL_HOST: str | None = None
    MYSQL_USER: str | None = None
    MYSQL_PASSWORD: str | None = None
    MYSQL_DATABASE: str | None = None
    MYSQL_PORT: int | None = None
    MYSQL_POOL_SIZE: int = 5
    MYSQL_MAX_OVERFLOW: int = 10
    MYSQL_POOL_RECYCLE: int = 1800
    MYSQL_CONNECT_TIMEOUT: int = 10
    DATABASE_URL: str | None = None

    LOG_LEVEL: str = "INFO"
    LOG_TO_STDOUT: bool = True

    @field_validator("GEMINI_OPENAI_COMPATIBLE", mode="before")
    @classmethod
    def _parse_gemini_openai_compatible(cls, value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @field_validator("MYSQL_PORT", mode="before")
    @classmethod
    def _parse_optional_port(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @classmethod
    def from_values(cls, **values: Any) -> AppSettings:
        """只用代码默认值和显式传入的值构造配置，不读 env 文件，也不读进程环境变量。

        给测试和嵌入式装配用：结果不依赖开发者的 `.env` 或 shell。未知的名字会报错。
        """
        return _ExplicitSettings(**values)


class _ExplicitSettings(AppSettings):
    model_config = SettingsConfigDict(env_file=None, extra="forbid")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def _parse_csv_value(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    values = [item.strip().lower() for item in raw_value.split(",") if item.strip()]
    return values


def _build_azure_base_url(settings: AppSettings) -> str:
    endpoint = settings.AZURE_OPENAI_API_ENDPOINT
    deployment = settings.AZURE_OPENAI_DEPLOYMENT
    if not endpoint or not deployment:
        return ""
    return f"{endpoint.rstrip('/')}/openai/deployments/{deployment}"


def _build_mysql_dsn(settings: AppSettings) -> str:
    user = settings.MYSQL_USER or ""
    password = settings.MYSQL_PASSWORD or ""
    host = settings.MYSQL_HOST or "localhost"
    database = settings.MYSQL_DATABASE or ""
    port = settings.MYSQL_PORT

    auth = user
    if password:
        auth = f"{user}:{quote_plus(password)}"

    location = f"{host}:{port}" if port else host
    return f"mysql+asyncmy://{auth}@{location}/{database}?charset=utf8mb4"


def _derived_values(settings: AppSettings) -> dict[str, Any]:
    """模块级常量里不是「字段原样复制」的那几个，由设置推导。导入时和换配置时共用这一份。"""
    return {
        "AZURE_OPENAI_BASE_URL": settings.AZURE_OPENAI_BASE_URL
        or _build_azure_base_url(settings),
        "MYSQL_CONFIG": {
            "host": settings.MYSQL_HOST,
            "user": settings.MYSQL_USER,
            "password": settings.MYSQL_PASSWORD,
            "database": settings.MYSQL_DATABASE,
        },
        "SQLALCHEMY_DATABASE_URI": settings.DATABASE_URL or _build_mysql_dsn(settings),
        # AI 服务的排序，按照优先级从高到低排序
        "AI_SERVICE_ORDER": _parse_csv_value(settings.AI_CHAT_ORDER),
        "AI_CHAT_TEXT_ONLY_MODELS": _parse_csv_value(settings.AI_CHAT_TEXT_ONLY_MODELS),
    }


SETTINGS = AppSettings()
_DERIVED = _derived_values(SETTINGS)


GEMINI_API_KEY = SETTINGS.GEMINI_API_KEY
GEMINI_API_BASE = SETTINGS.GEMINI_API_BASE
GEMINI_OPENAI_COMPATIBLE = SETTINGS.GEMINI_OPENAI_COMPATIBLE
GEMINI_CHAT_MODEL = SETTINGS.GEMINI_CHAT_MODEL
GEMINI_CHAT_FALLBACK_MODEL = SETTINGS.GEMINI_CHAT_FALLBACK_MODEL
GEMINI_RECAP_MODEL = SETTINGS.GEMINI_RECAP_MODEL
GEMINI_SUMMARY_MODEL = SETTINGS.GEMINI_SUMMARY_MODEL
GEMINI_SUMMARY_FALLBACK_MODEL = SETTINGS.GEMINI_SUMMARY_FALLBACK_MODEL
GEMINI_TRANSLATE_MODEL = SETTINGS.GEMINI_TRANSLATE_MODEL
GEMINI_VISION_MODEL = SETTINGS.GEMINI_VISION_MODEL
GEMINI_CLASSIFIER_MODEL = SETTINGS.GEMINI_CLASSIFIER_MODEL
GEMINI_ADVISOR_MODEL = SETTINGS.GEMINI_ADVISOR_MODEL

ZAI_API_KEY = SETTINGS.ZAI_API_KEY
ZAI_API_BASE = SETTINGS.ZAI_API_BASE
ZHIPU_CHAT_MODEL = SETTINGS.ZHIPU_CHAT_MODEL
ZHIPU_RECAP_MODEL = SETTINGS.ZHIPU_RECAP_MODEL
ZHIPU_SUMMARY_MODEL = SETTINGS.ZHIPU_SUMMARY_MODEL
ZHIPU_TRANSLATE_MODEL = SETTINGS.ZHIPU_TRANSLATE_MODEL
ZHIPU_VISION_MODEL = SETTINGS.ZHIPU_VISION_MODEL
ZHIPU_CLASSIFIER_MODEL = SETTINGS.ZHIPU_CLASSIFIER_MODEL
ZHIPU_ADVISOR_MODEL = SETTINGS.ZHIPU_ADVISOR_MODEL
SERPAPI_API_KEY = SETTINGS.SERPAPI_API_KEY
TELEGRAM_BOT_TOKEN = SETTINGS.TELEGRAM_BOT_TOKEN
TELEGRAM_CONNECT_TIMEOUT = SETTINGS.TELEGRAM_CONNECT_TIMEOUT
TELEGRAM_READ_TIMEOUT = SETTINGS.TELEGRAM_READ_TIMEOUT
TELEGRAM_WRITE_TIMEOUT = SETTINGS.TELEGRAM_WRITE_TIMEOUT
TELEGRAM_POOL_TIMEOUT = SETTINGS.TELEGRAM_POOL_TIMEOUT
TELEGRAM_GET_UPDATES_TIMEOUT = SETTINGS.TELEGRAM_GET_UPDATES_TIMEOUT
TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT = SETTINGS.TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT
TELEGRAM_GET_UPDATES_READ_TIMEOUT = SETTINGS.TELEGRAM_GET_UPDATES_READ_TIMEOUT
TELEGRAM_GET_UPDATES_WRITE_TIMEOUT = SETTINGS.TELEGRAM_GET_UPDATES_WRITE_TIMEOUT
TELEGRAM_GET_UPDATES_POOL_TIMEOUT = SETTINGS.TELEGRAM_GET_UPDATES_POOL_TIMEOUT
OPENAI_API_KEY = SETTINGS.OPENAI_API_KEY
OPENAI_BASE_URL = SETTINGS.OPENAI_BASE_URL
OPENAI_CHAT_MODEL = SETTINGS.OPENAI_CHAT_MODEL
OPENAI_RECAP_MODEL = SETTINGS.OPENAI_RECAP_MODEL
OPENAI_SUMMARY_MODEL = SETTINGS.OPENAI_SUMMARY_MODEL
OPENAI_TRANSLATE_MODEL = SETTINGS.OPENAI_TRANSLATE_MODEL
OPENAI_VISION_MODEL = SETTINGS.OPENAI_VISION_MODEL
OPENAI_CLASSIFIER_MODEL = SETTINGS.OPENAI_CLASSIFIER_MODEL
OPENAI_ADVISOR_MODEL = SETTINGS.OPENAI_ADVISOR_MODEL
OPENROUTER_API_KEY = SETTINGS.OPENROUTER_API_KEY
OPENROUTER_API_BASE = SETTINGS.OPENROUTER_API_BASE
OPENROUTER_CHAT_MODEL = SETTINGS.OPENROUTER_CHAT_MODEL
OPENROUTER_RECAP_MODEL = SETTINGS.OPENROUTER_RECAP_MODEL
OPENROUTER_SUMMARY_MODEL = SETTINGS.OPENROUTER_SUMMARY_MODEL
OPENROUTER_TRANSLATE_MODEL = SETTINGS.OPENROUTER_TRANSLATE_MODEL
OPENROUTER_VISION_MODEL = SETTINGS.OPENROUTER_VISION_MODEL
OPENROUTER_CLASSIFIER_MODEL = SETTINGS.OPENROUTER_CLASSIFIER_MODEL
OPENROUTER_ADVISOR_MODEL = SETTINGS.OPENROUTER_ADVISOR_MODEL
FOGMOE_API_KEY = SETTINGS.FOGMOE_API_KEY
FOGMOE_API_BASE = SETTINGS.FOGMOE_API_BASE
FOGMOE_CHAT_MODEL = SETTINGS.FOGMOE_CHAT_MODEL
FOGMOE_RECAP_MODEL = SETTINGS.FOGMOE_RECAP_MODEL
FOGMOE_SUMMARY_MODEL = SETTINGS.FOGMOE_SUMMARY_MODEL
FOGMOE_TRANSLATE_MODEL = SETTINGS.FOGMOE_TRANSLATE_MODEL
FOGMOE_VISION_MODEL = SETTINGS.FOGMOE_VISION_MODEL
FOGMOE_CLASSIFIER_MODEL = SETTINGS.FOGMOE_CLASSIFIER_MODEL
FOGMOE_ADVISOR_MODEL = SETTINGS.FOGMOE_ADVISOR_MODEL
AZURE_OPENAI_API_KEY = SETTINGS.AZURE_OPENAI_API_KEY
AZURE_OPENAI_API_ENDPOINT = SETTINGS.AZURE_OPENAI_API_ENDPOINT
AZURE_OPENAI_API_VERSION = SETTINGS.AZURE_OPENAI_API_VERSION
AZURE_OPENAI_DEPLOYMENT = SETTINGS.AZURE_OPENAI_DEPLOYMENT
AZURE_OPENAI_CHAT_MODEL = SETTINGS.AZURE_OPENAI_CHAT_MODEL
AZURE_OPENAI_RECAP_MODEL = SETTINGS.AZURE_OPENAI_RECAP_MODEL
AZURE_OPENAI_SUMMARY_MODEL = SETTINGS.AZURE_OPENAI_SUMMARY_MODEL
AZURE_OPENAI_TRANSLATE_MODEL = SETTINGS.AZURE_OPENAI_TRANSLATE_MODEL
AZURE_OPENAI_VISION_MODEL = SETTINGS.AZURE_OPENAI_VISION_MODEL
AZURE_OPENAI_CLASSIFIER_MODEL = SETTINGS.AZURE_OPENAI_CLASSIFIER_MODEL
AZURE_OPENAI_ADVISOR_MODEL = SETTINGS.AZURE_OPENAI_ADVISOR_MODEL

SILICONFLOW_API_KEY = SETTINGS.SILICONFLOW_API_KEY
SILICONFLOW_API_BASE = SETTINGS.SILICONFLOW_API_BASE
SILICONFLOW_CHAT_MODEL = SETTINGS.SILICONFLOW_CHAT_MODEL
SILICONFLOW_RECAP_MODEL = SETTINGS.SILICONFLOW_RECAP_MODEL
SILICONFLOW_SUMMARY_MODEL = SETTINGS.SILICONFLOW_SUMMARY_MODEL
SILICONFLOW_TRANSLATE_MODEL = SETTINGS.SILICONFLOW_TRANSLATE_MODEL
SILICONFLOW_VISION_MODEL = SETTINGS.SILICONFLOW_VISION_MODEL
SILICONFLOW_CLASSIFIER_MODEL = SETTINGS.SILICONFLOW_CLASSIFIER_MODEL
SILICONFLOW_ADVISOR_MODEL = SETTINGS.SILICONFLOW_ADVISOR_MODEL

AZURE_OPENAI_BASE_URL = _DERIVED["AZURE_OPENAI_BASE_URL"]

AI_RECAP_PROVIDER = SETTINGS.AI_RECAP_PROVIDER
AI_RECAP_FALLBACK_PROVIDER = SETTINGS.AI_RECAP_FALLBACK_PROVIDER
AI_SUMMARY_PROVIDER = SETTINGS.AI_SUMMARY_PROVIDER
AI_SUMMARY_FALLBACK_PROVIDER = SETTINGS.AI_SUMMARY_FALLBACK_PROVIDER
AI_TRANSLATE_PROVIDER = SETTINGS.AI_TRANSLATE_PROVIDER
AI_TRANSLATE_FALLBACK_PROVIDER = SETTINGS.AI_TRANSLATE_FALLBACK_PROVIDER
AI_VISION_PROVIDER = SETTINGS.AI_VISION_PROVIDER
AI_VISION_FALLBACK_PROVIDER = SETTINGS.AI_VISION_FALLBACK_PROVIDER
AI_CLASSIFIER_PROVIDER = SETTINGS.AI_CLASSIFIER_PROVIDER
AI_CLASSIFIER_FALLBACK_PROVIDER = SETTINGS.AI_CLASSIFIER_FALLBACK_PROVIDER
AI_ADVISOR_PROVIDER = SETTINGS.AI_ADVISOR_PROVIDER
AI_ADVISOR_FALLBACK_PROVIDER = SETTINGS.AI_ADVISOR_FALLBACK_PROVIDER
AI_ADVISOR_TIMEOUT_SECONDS = SETTINGS.AI_ADVISOR_TIMEOUT_SECONDS
AI_ADVISOR_MAX_CALLS_PER_REQUEST = SETTINGS.AI_ADVISOR_MAX_CALLS_PER_REQUEST
AI_ADVISOR_RATE_LIMIT_WINDOW_SECONDS = SETTINGS.AI_ADVISOR_RATE_LIMIT_WINDOW_SECONDS
AI_ADVISOR_RATE_LIMIT_MAX_CALLS = SETTINGS.AI_ADVISOR_RATE_LIMIT_MAX_CALLS
AI_ADVISOR_MAX_CONCURRENT_REQUESTS = SETTINGS.AI_ADVISOR_MAX_CONCURRENT_REQUESTS
AI_CHAT_COMPLETION_TIMEOUT_SECONDS = SETTINGS.AI_CHAT_COMPLETION_TIMEOUT_SECONDS

CHAT_TOKEN_WARN_LIMIT = SETTINGS.CHAT_TOKEN_WARN_LIMIT
CHAT_TOKEN_LIMIT = SETTINGS.CHAT_TOKEN_LIMIT
CHAT_CONTEXT_HARD_LIMIT_RATIO = SETTINGS.CHAT_CONTEXT_HARD_LIMIT_RATIO
CHAT_CONTEXT_SAFETY_TOKENS = SETTINGS.CHAT_CONTEXT_SAFETY_TOKENS
CHAT_BATCH_WINDOW_SECONDS = SETTINGS.CHAT_BATCH_WINDOW_SECONDS
CHAT_MAX_CONCURRENT_TURNS = SETTINGS.CHAT_MAX_CONCURRENT_TURNS
CHAT_MAX_QUEUED_TURNS = SETTINGS.CHAT_MAX_QUEUED_TURNS
CHAT_MAX_PENDING_PER_USER = SETTINGS.CHAT_MAX_PENDING_PER_USER
CHAT_QUEUE_MAX_WAIT_SECONDS = SETTINGS.CHAT_QUEUE_MAX_WAIT_SECONDS
CHAT_TURN_DEADLINE_SECONDS = SETTINGS.CHAT_TURN_DEADLINE_SECONDS
TELEGRAM_CONCURRENT_UPDATES = SETTINGS.TELEGRAM_CONCURRENT_UPDATES
BLOCKING_TOOL_THREADS = SETTINGS.BLOCKING_TOOL_THREADS
BLOCKING_IO_THREADS = SETTINGS.BLOCKING_IO_THREADS
RUNTIME_METRICS_LOG_INTERVAL_SECONDS = SETTINGS.RUNTIME_METRICS_LOG_INTERVAL_SECONDS
RUNTIME_SHUTDOWN_GRACE_SECONDS = SETTINGS.RUNTIME_SHUTDOWN_GRACE_SECONDS
TELEGRAM_HISTORY_RATE_WINDOW_SECONDS = SETTINGS.TELEGRAM_HISTORY_RATE_WINDOW_SECONDS
TELEGRAM_HISTORY_RATE_MAX_EVENTS = SETTINGS.TELEGRAM_HISTORY_RATE_MAX_EVENTS

JUDGE0_API_URL = SETTINGS.JUDGE0_API_URL
JUDGE0_API_KEY = SETTINGS.JUDGE0_API_KEY
E2B_API_KEY = SETTINGS.E2B_API_KEY

IMAGE_GEN_API_URL = SETTINGS.IMAGE_GEN_API_URL
IMAGE_GEN_API_TOKEN = SETTINGS.IMAGE_GEN_API_TOKEN
IMAGE_GEN_TIMEOUT = SETTINGS.IMAGE_GEN_TIMEOUT
FISH_AUDIO_API_KEY = SETTINGS.FISH_AUDIO_API_KEY
FISH_AUDIO_MODEL = SETTINGS.FISH_AUDIO_MODEL
FISH_AUDIO_REFERENCE_ID = SETTINGS.FISH_AUDIO_REFERENCE_ID

ADMIN_USER_ID = SETTINGS.ADMIN_USER_ID
NEW_USER_BONUS_COINS = SETTINGS.NEW_USER_BONUS_COINS

MYSQL_CONFIG = _DERIVED["MYSQL_CONFIG"]

MYSQL_POOL_SIZE = SETTINGS.MYSQL_POOL_SIZE
MYSQL_MAX_OVERFLOW = SETTINGS.MYSQL_MAX_OVERFLOW
MYSQL_POOL_RECYCLE = SETTINGS.MYSQL_POOL_RECYCLE
MYSQL_CONNECT_TIMEOUT = SETTINGS.MYSQL_CONNECT_TIMEOUT

SQLALCHEMY_DATABASE_URI = _DERIVED["SQLALCHEMY_DATABASE_URI"]

# 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL = SETTINGS.LOG_LEVEL
# 日志始终写入轮转文件；容器场景同时输出到 stdout，供 docker logs 查看
LOG_TO_STDOUT = SETTINGS.LOG_TO_STDOUT
LOG_DIR = BASE_DIR / "logs"
LOG_FILE_PATH = LOG_DIR / "tgbot.log"


def _read_text_resource(relative_path: str) -> str:
    return (BASE_DIR / relative_path).read_text(encoding="utf-8")

# help 命令的帮助信息
HELP_TEXT = _read_text_resource("resources/telegram_help.md")

# AI 系统提示词
SYSTEM_PROMPT = _read_text_resource("resources/prompts/system_prompt.md")
ADVISOR_SYSTEM_PROMPT = _read_text_resource(
    "resources/prompts/advisor_system_prompt.md"
)
SUMMARY_SYSTEM_PROMPT = _read_text_resource(
    "resources/prompts/summary_system_prompt.md"
)
IDLE_RECAP_SYSTEM_PROMPT = _read_text_resource(
    "resources/prompts/idle_recap_system_prompt.md"
)

# AI 可按主题查阅的内部文档库，文件名（不含扩展名）即主题名
INTERNAL_DOCS_DIR = BASE_DIR / "resources" / "docs"


def _load_internal_docs() -> dict[str, str]:
    if not INTERNAL_DOCS_DIR.is_dir():
        return {}
    docs: dict[str, str] = {}
    for path in sorted(INTERNAL_DOCS_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            docs[path.stem] = text
    return docs


INTERNAL_DOCS: dict[str, str] = _load_internal_docs()

# AI 服务的排序，按照优先级从高到低排序
AI_SERVICE_ORDER = _DERIVED["AI_SERVICE_ORDER"]
AI_CHAT_TEXT_ONLY_MODELS = _DERIVED["AI_CHAT_TEXT_ONLY_MODELS"]
AI_DIRECT_TRIGGER_PHRASES = [
    "/fogmoebot",
    "雾萌",
    "fog moe",
    "萌娘",
    "fogmoe",
]


# ---------------------------------------------------------------------------
# 配置注入
#
# 读取配置的代码一律在调用时访问 `config.<NAME>`；换配置就是重新发布这些模块级名字。
# 契约与迁移计划见 docs/architecture.md 的「配置注入」。
# ---------------------------------------------------------------------------

# 与设置字段同名、原样复制成模块常量的名字（推导出来的几个在 `_DERIVED` 里单独处理）。
_PLAIN_FIELDS = tuple(
    name
    for name in AppSettings.model_fields
    if name in globals() and name not in _DERIVED
)


def _publish(settings: AppSettings) -> None:
    global SETTINGS
    SETTINGS = settings
    namespace = globals()
    for name in _PLAIN_FIELDS:
        namespace[name] = getattr(settings, name)
    namespace.update(_derived_values(settings))


def current_settings() -> AppSettings:
    """当前生效的设置对象；进程启动时是从 `.env` 与环境变量加载的那一份。"""
    return SETTINGS


def install_settings(settings: AppSettings) -> AppSettings:
    """让 `settings` 成为生效配置：重新发布所有模块级常量，返回之前的设置对象。

    只影响在调用时才读 `config.<NAME>` 的代码；`from core.config import X` 或模块顶层
    `X = config.X` 在导入时已经取走了旧值，见 docs/architecture.md 里的清单。
    会覆盖此前对这些常量的手工 monkeypatch，所以先装配置，再 patch 个别值。
    """
    previous = SETTINGS
    _publish(settings)
    return previous


@contextmanager
def use_settings(settings: AppSettings) -> Iterator[AppSettings]:
    """块内使用 `settings`，退出时恢复之前的设置对象。"""
    previous = install_settings(settings)
    try:
        yield settings
    finally:
        _publish(previous)


@contextmanager
def override_settings(**values: Any) -> Iterator[AppSettings]:
    """块内使用「代码默认值 + `values`」构成的配置，不读 `.env` 与进程环境变量。

    没有传入的设置回到代码默认值，而不是沿用当前值：测试不会因为开发者的环境变量而变化。
    """
    with use_settings(AppSettings.from_values(**values)) as settings:
        yield settings
