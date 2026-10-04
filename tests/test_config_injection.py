"""配置注入：显式构造、整体覆盖、覆盖对调用时读配置的代码可见、应用组装接受显式配置。"""

import ast

import pytest
from pydantic import ValidationError

from app import bot_app
from core import config
from core.telegram_history import HistoryTrackingExtBot
from features.ai import provider_resolver

# 设置里有、但不作为 `config.<NAME>` 模块常量暴露的字段：由推导出来的常量取代，或只用于推导。
NOT_PUBLISHED = {
    "AI_CHAT_ORDER",
    "MYSQL_HOST",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
    "MYSQL_PORT",
    "DATABASE_URL",
}


def test_from_values_ignores_process_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "from-the-shell")
    monkeypatch.setenv("ADMIN_USER_ID", "42")

    settings = config.AppSettings.from_values()

    assert settings.OPENAI_CHAT_MODEL is None
    assert settings.ADMIN_USER_ID == config.AppSettings.model_fields["ADMIN_USER_ID"].default


def test_from_values_takes_explicit_values_and_rejects_unknown_names():
    settings = config.AppSettings.from_values(OPENAI_CHAT_MODEL="m", MYSQL_PORT="3307")

    assert settings.OPENAI_CHAT_MODEL == "m"
    assert settings.MYSQL_PORT == 3307
    with pytest.raises(ValidationError):
        config.AppSettings.from_values(OPENAI_CHAT_MODL="typo")


def test_override_republishes_plain_and_derived_constants_then_restores():
    before = (
        config.OPENAI_CHAT_MODEL,
        config.AI_SERVICE_ORDER,
        config.SQLALCHEMY_DATABASE_URI,
        config.AZURE_OPENAI_BASE_URL,
    )

    with config.override_settings(
        OPENAI_CHAT_MODEL="gpt-test",
        AI_CHAT_ORDER="OpenAI, Gemini",
        MYSQL_USER="bot",
        MYSQL_PASSWORD="p@ss",
        MYSQL_HOST="db",
        MYSQL_PORT=3307,
        MYSQL_DATABASE="app",
        AZURE_OPENAI_API_ENDPOINT="https://azure.test/",
        AZURE_OPENAI_DEPLOYMENT="dep",
    ):
        assert config.OPENAI_CHAT_MODEL == "gpt-test"
        assert config.AI_SERVICE_ORDER == ["openai", "gemini"]
        assert config.SQLALCHEMY_DATABASE_URI == (
            "mysql+asyncmy://bot:p%40ss@db:3307/app?charset=utf8mb4"
        )
        assert config.MYSQL_CONFIG["host"] == "db"
        assert config.AZURE_OPENAI_BASE_URL == "https://azure.test/openai/deployments/dep"

    assert (
        config.OPENAI_CHAT_MODEL,
        config.AI_SERVICE_ORDER,
        config.SQLALCHEMY_DATABASE_URI,
        config.AZURE_OPENAI_BASE_URL,
    ) == before


def test_explicit_database_url_wins_over_the_mysql_parts():
    with config.override_settings(DATABASE_URL="mysql+asyncmy://u@h/d", MYSQL_HOST="ignored"):
        assert config.SQLALCHEMY_DATABASE_URI == "mysql+asyncmy://u@h/d"


def test_override_restores_the_previous_settings_when_the_block_fails():
    previous = config.current_settings()

    with pytest.raises(RuntimeError):
        with config.override_settings(AI_CHAT_ORDER="openai"):
            assert config.AI_SERVICE_ORDER == ["openai"]
            raise RuntimeError("boom")

    assert config.current_settings() is previous


def test_values_that_are_not_passed_fall_back_to_code_defaults(settings_override, monkeypatch):
    monkeypatch.setenv("SILICONFLOW_CHAT_MODEL", "from-the-shell")

    settings_override(OPENAI_CHAT_MODEL="gpt-test")

    assert config.SILICONFLOW_CHAT_MODEL == "deepseek-ai/DeepSeek-V4-Flash"
    assert config.current_settings().OPENAI_CHAT_MODEL == "gpt-test"


def test_every_setting_is_a_module_constant_unless_it_is_derived_from():
    missing = [
        name
        for name in config.AppSettings.model_fields
        if name not in NOT_PUBLISHED and not hasattr(config, name)
    ]

    assert missing == []


def test_install_settings_returns_the_previous_settings_and_republishes():
    replacement = config.AppSettings.from_values(AI_CHAT_ORDER="zai")
    previous = config.install_settings(replacement)
    try:
        assert config.current_settings() is replacement
        assert config.AI_SERVICE_ORDER == ["zai"]
    finally:
        config.install_settings(previous)

    assert config.current_settings() is previous


def test_readers_of_config_at_call_time_see_the_override(settings_override):
    settings_override(AI_CHAT_ORDER="siliconflow,openai", SILICONFLOW_CHAT_MODEL="sf-model")

    assert provider_resolver.get_provider_order_for_task("chat") == ["siliconflow", "openai"]
    assert provider_resolver.provider_model_for_task("siliconflow", "chat") == "sf-model"


def test_create_application_uses_the_explicit_settings(settings_override):
    settings = config.AppSettings.from_values(TELEGRAM_BOT_TOKEN="123:from-settings")

    application = bot_app.create_application(settings)

    assert application.bot.token == "123:from-settings"
    assert config.current_settings() is settings
    assert sum(len(handlers) for handlers in application.handlers.values()) > 0


def test_create_application_accepts_a_prebuilt_bot(settings_override):
    bot = HistoryTrackingExtBot(token="456:prebuilt")

    application = bot_app.create_application(
        config.AppSettings.from_values(TELEGRAM_BOT_TOKEN="123:ignored"),
        bot=bot,
    )

    assert application.bot is bot


# ---------------------------------------------------------------------------
# 仍在导入时读取配置的模块
#
# 这些模块在导入时就取走了配置值，`install_settings` 影响不到它们。清单是上限：新增一处导入时读取会让
# 下面的测试失败；某个模块迁移成调用时读取之后，把它从清单里删掉。
# ---------------------------------------------------------------------------

IMPORT_TIME_CONFIG_READS = {
    ("features/admin/announce.py", "ADMIN_USER_ID"),
    ("features/ai/prompts.py", "SYSTEM_PROMPT"),  # 来自 resources/ 的文本，不是设置
    ("features/ai/tools/image_tools.py", "BASE_DIR"),  # 路径常量，不是设置
    ("features/ai/tools/sticker_tools.py", "BASE_DIR"),
    ("features/ai/tools/voice_tools.py", "BASE_DIR"),
    ("features/crypto/monitoring.py", "ADMIN_USER_ID"),
    ("features/moderation/spam_control.py", "BASE_DIR"),
}


def _import_time_config_reads(tree: ast.Module) -> set[str]:
    """顶层（含类体、装饰器与默认参数，不含函数体）读取的 `config.<NAME>` 与 `from core.config import`。"""
    names: set[str] = set()

    class Scanner(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
                self.visit(default)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            return

        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == "config":
                names.add(node.attr)
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if node.module in {"core.config", "config"}:
                names.update(alias.name for alias in node.names)

    scanner = Scanner()
    for statement in tree.body:
        scanner.visit(statement)
    return names


def test_the_import_time_scan_sees_module_level_reads_and_ignores_function_bodies():
    tree = ast.parse(
        "from core import config\n"
        "from core.config import BASE_DIR\n"
        "TOKEN = config.TOKEN\n"
        "class Holder:\n"
        "    limit = config.LIMIT\n"
        "def late():\n"
        "    return config.LATE\n"
        "def early(value=config.DEFAULT):\n"
        "    return value\n"
    )

    assert _import_time_config_reads(tree) == {"BASE_DIR", "TOKEN", "LIMIT", "DEFAULT"}


def test_no_new_module_reads_configuration_at_import_time():
    modules_dir = config.BASE_DIR / "modules"
    found = set()
    for path in modules_dir.rglob("*.py"):
        relative = path.relative_to(modules_dir)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.update((relative.as_posix(), name) for name in _import_time_config_reads(tree))

    assert found - IMPORT_TIME_CONFIG_READS == set()
