"""把 AI 对话流程文档里会随代码变化的部分同步成代码里的值。

- docs/ai-conversation-flow.md：替换 `<!-- BEGIN GENERATED: 名字 -->` 与 `<!-- END GENERATED: 名字 -->`
  之间的内容（工具目录、阶段顺序、阈值表），手写部分不动；
- docs/ai-conversation-flow.html：改写每个 `<span data-limit="名字">` / `<tspan data-limit="名字">`
  里的数字。

    uv run python scripts/gen_ai_flow_doc.py          # 写回文档
    uv run python scripts/gen_ai_flow_doc.py --check  # 只检查，过期时退出码为 1

数值取代码里的默认值（`AppSettings` 字段默认值、模块常量、函数参数默认值），不读 `.env`，
生成结果与运行环境无关。`tests/test_ai_flow_doc.py` 在 CI 里做同样的检查。
"""

from __future__ import annotations

import argparse
import inspect
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "ai-conversation-flow.md"
HTML_PATH = REPO_ROOT / "docs" / "ai-conversation-flow.html"
PACKAGE_PREFIX = "fogmoe_telegram_bot."

_BLOCK_RE = re.compile(
    r"(?P<begin><!-- BEGIN GENERATED: (?P<name>[a-z_]+) -->\n)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END GENERATED: (?P=name) -->)",
    re.DOTALL,
)
_HTML_VALUE_RE = re.compile(
    r'<(?P<tag>span|tspan) data-limit="(?P<key>[A-Z_]+)"[^>]*>.*?</(?P=tag)>',
    re.DOTALL,
)

# 页面上用「万」表示的 token 数。
_TOKEN_KEYS = {"CHAT_TOKEN_WARN_LIMIT", "CHAT_TOKEN_LIMIT", "REQUEST_HARD_LIMIT"}


def _source_link(module: str) -> str:
    """`fogmoe_telegram_bot.core.config` -> 指向源文件的相对链接（相对 docs/）。"""
    relative = module.removeprefix(PACKAGE_PREFIX).replace(".", "/") + ".py"
    return f"[{relative}](../src/fogmoe_telegram_bot/{relative})"


def _code(value: Any) -> str:
    return f"`{value!r}`" if isinstance(value, tuple) else f"`{value}`"


def _cell(text: str) -> str:
    """表格单元格：合并空白，转义 `|`，把 `<tag>` 转义成文字（GitHub 会吞掉未知的 HTML 标签）。"""
    text = " ".join(str(text).split())
    return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


# -- 阶段 ---------------------------------------------------------------------


def stage_values() -> list[str]:
    from fogmoe_telegram_bot.features.conversation.turn_types import Stage

    return [stage.value for stage in Stage]


def render_stages() -> str:
    return " → ".join(f"`{stage}`" for stage in stage_values()) + "\n"


# -- 阈值与上限 -----------------------------------------------------------------


@dataclass(frozen=True)
class LimitRow:
    key: str  # 页面里 data-limit 用的名字
    label: str  # 表格里显示的名字
    value: Any
    module: str  # 定义所在模块
    purpose: str


def limit_rows() -> list[LimitRow]:
    from fogmoe_telegram_bot.core import chat_records
    from fogmoe_telegram_bot.core.config import AppSettings
    from fogmoe_telegram_bot.features.ai import litellm_client, summary, tool_runner

    def setting(name: str, purpose: str) -> LimitRow:
        return LimitRow(
            name, name, AppSettings.model_fields[name].default, AppSettings.__module__, purpose
        )

    def constant(module: Any, name: str, purpose: str) -> LimitRow:
        return LimitRow(name, name, getattr(module, name), module.__name__, purpose)

    def param(key: str, func: Callable[..., Any], name: str, purpose: str) -> LimitRow:
        return LimitRow(
            key,
            f"{func.__name__}({name})",
            inspect.signature(func).parameters[name].default,
            func.__module__,
            purpose,
        )

    token_limit = AppSettings.model_fields["CHAT_TOKEN_LIMIT"].default
    hard_ratio = AppSettings.model_fields["CHAT_CONTEXT_HARD_LIMIT_RATIO"].default

    return [
        setting(
            "CHAT_BATCH_WINDOW_SECONDS",
            "同一个人在同一个聊天里连发的消息，等这么多秒后合成一批，只调用一次模型",
        ),
        setting(
            "CHAT_TURN_DEADLINE_SECONDS",
            "整轮截止时间，从进入队列开始计时，覆盖排队、媒体、模型与投递",
        ),
        setting(
            "CHAT_TOKEN_WARN_LIMIT",
            "写入用户消息后活跃历史超过它：插入 near_limit 事件并提醒用户",
        ),
        setting(
            "CHAT_TOKEN_LIMIT",
            "写入后活跃历史超过它：压缩（裁剪、归档、生成摘要）",
        ),
        setting(
            "CHAT_CONTEXT_HARD_LIMIT_RATIO",
            "单次模型请求的硬上限倍数，见下一行",
        ),
        LimitRow(
            "REQUEST_HARD_LIMIT",
            "请求硬上限（推导）",
            int(token_limit * hard_ratio),
            litellm_client.__name__,
            "CHAT_TOKEN_LIMIT × 倍数；消息 + 工具 schema + 最大输出 + 预留超过它就报错，"
            "不删也不截断任何内容",
        ),
        setting(
            "CHAT_CONTEXT_SAFETY_TOKENS",
            "估算请求大小时额外预留的 token",
        ),
        param(
            "MAX_TOOL_ITERATIONS",
            tool_runner.run_tool_loop,
            "max_iterations",
            "工具循环最多几轮；用完后不带工具再要一次最终回复",
        ),
        constant(
            tool_runner,
            "POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS",
            "执行过工具或发出过可见内容之后，模型调用遇到临时错误时的重试间隔（同一模型，不换 provider）",
        ),
        param(
            "KEEP_RECENT_MESSAGES",
            chat_records._trim_messages_with_tool_context,
            "keep_non_tool",
            "压缩时保留最近几条非 tool 消息；它们引用的 tool_call 会一并保留，保持配对",
        ),
        constant(
            chat_records,
            "PERMANENT_RECORDS_KEEP",
            "每个用户保留的归档快照数（user.permanent_records_limit 可单独设置），"
            "更早的打包发给用户后删除",
        ),
        constant(
            summary,
            "SUMMARY_IMMEDIATE_TIMEOUT_SECONDS",
            "压缩时这一轮最多等多久的即时摘要，超时转后台生成",
        ),
        constant(summary, "SUMMARY_CONCURRENCY", "同时生成的摘要数（即时与后台共用）"),
        constant(summary, "SUMMARY_TOOL_MAX_ITERATIONS", "摘要 agent 的工具循环轮数"),
        constant(
            summary,
            "SUMMARY_CONTEXT_HARD_LIMIT_RATIO",
            "摘要请求的硬上限倍数（快照可能比活跃历史长）",
        ),
        constant(summary, "SUMMARY_MAX_TOKENS", "摘要正文超过它会被截短"),
        constant(
            summary,
            "SUMMARY_RETRY_LIMIT",
            "摘要整体重试次数（每次都把 provider 顺序走一遍）",
        ),
    ]


def render_limits() -> str:
    return _table(
        ["名字", "默认值", "定义", "作用"],
        [
            [_code(row.label), _code(row.value), _source_link(row.module), _cell(row.purpose)]
            for row in limit_rows()
        ],
    )


# -- 工具目录 -------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogTool:
    name: str
    function: Mapping[str, Any]
    handler: Callable[..., Any]
    agents: tuple[str, ...]  # AGENT_LABELS 的键，按 agent 顺序


AGENT_LABELS = {"main": "主聊天", "recap": "空闲回顾", "summary": "摘要"}


def _agents() -> list[tuple[str, list[dict[str, Any]], Mapping[str, Callable[..., Any]]]]:
    """每个 agent 能看到的工具 schema 与处理函数；同一个工具可能出现在多个 agent 里。"""
    from fogmoe_telegram_bot.features.ai import idle_followup, summary
    from fogmoe_telegram_bot.features.ai.tools.registry import AI_TOOL_HANDLERS, OPENAI_TOOLS

    return [
        ("main", OPENAI_TOOLS, AI_TOOL_HANDLERS),
        ("recap", idle_followup.IDLE_RECAP_TOOLS, idle_followup.IDLE_RECAP_TOOL_HANDLERS),
        ("summary", summary.SUMMARY_TOOLS, summary.SUMMARY_TOOL_HANDLERS),
    ]


def tool_catalog() -> list[CatalogTool]:
    """所有 agent 的工具，直接从 `TOOL_SPECS` 单点声明生成并校验各 agent 导出一致。"""
    from fogmoe_telegram_bot.features.ai.tools.specs import TOOL_SPECS

    agent_exports = dict((agent, (tools, agent_handlers)) for agent, tools, agent_handlers in _agents())
    catalog: list[CatalogTool] = []
    for spec in TOOL_SPECS:
        schema = spec.schema()
        function = schema["function"]
        for agent in spec.agents:
            tools, agent_handlers = agent_exports[agent]
            exported = next((item for item in tools if item["function"]["name"] == spec.name), None)
            if exported is None or exported["function"] != function or agent_handlers.get(spec.name) is not spec.handler:
                raise ValueError(f"tool {spec.name!r} export for agent {agent!r} drifted from TOOL_SPECS")
        catalog.append(CatalogTool(spec.name, function, spec.handler, spec.agents))
    return catalog


def render_tools() -> str:
    rows = []
    for tool in tool_catalog():
        parameters = tool.function.get("parameters") or {}
        required = set(parameters.get("required") or [])
        params = ", ".join(
            f"`{param}`" if param in required else f"`{param}?`"
            for param in (parameters.get("properties") or {})
        )
        module_name = tool.handler.__module__.rsplit(".", 1)[-1]
        rows.append(
            [
                f"`{tool.name}`",
                "、".join(AGENT_LABELS[agent] for agent in tool.agents),
                params or "—",
                f"`{module_name}.{tool.handler.__name__}`",
                _cell(tool.function.get("description") or ""),
            ]
        )

    counts = "，".join(
        f"{AGENT_LABELS[agent]} {len(tools)} 个" for agent, tools, _ in _agents()
    )
    header = f"共 {len(rows)} 个工具：{counts}。参数后面带 `?` 的是可选参数。\n\n"
    return header + _table(["工具", "谁能调用", "参数", "处理函数", "描述（模型看到的原文）"], rows)


GENERATORS: dict[str, Callable[[], str]] = {
    "stages": render_stages,
    "limits": render_limits,
    "tools": render_tools,
}


def render(text: str) -> str:
    """把 md 文档里每个生成块替换成最新内容；块名未知或缺块时报错。"""
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in GENERATORS:
            raise ValueError(f"unknown generated block: {name}")
        seen.add(name)
        return match.group("begin") + GENERATORS[name]() + match.group("end")

    rendered = _BLOCK_RE.sub(replace, text)
    missing = set(GENERATORS) - seen
    if missing:
        raise ValueError(f"generated blocks missing from document: {sorted(missing)}")
    return rendered


# -- HTML 页面里的数字 ------------------------------------------------------------


def html_values() -> dict[str, Any]:
    from fogmoe_telegram_bot.features.ai.tools.registry import OPENAI_TOOLS

    values: dict[str, Any] = {row.key: row.value for row in limit_rows()}
    values["MAIN_TOOL_COUNT"] = len(OPENAI_TOOLS)
    return values


def _html_display(key: str, value: Any) -> str:
    if key in _TOKEN_KEYS:
        return f"{value / 10000:g} 万"
    if isinstance(value, tuple):
        return "、".join(f"{item:g}" for item in value)
    return f"{value:g}"


def render_html(text: str) -> str:
    """把 HTML 页面里每个 data-limit 元素的数字改成代码里的值；名字未知时报错。"""
    values = html_values()

    def replace(match: re.Match[str]) -> str:
        tag, key = match.group("tag"), match.group("key")
        if key not in values:
            raise ValueError(f"unknown data-limit in page: {key}")
        value = values[key]
        return (
            f'<{tag} data-limit="{key}" data-value="{value}">'
            f"{_html_display(key, value)}</{tag}>"
        )

    return _HTML_VALUE_RE.sub(replace, text)


TARGETS: list[tuple[Path, Callable[[str], str]]] = [
    (DOC_PATH, render),
    (HTML_PATH, render_html),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只检查，不写回")
    args = parser.parse_args(argv)

    # 不读任何 env 文件（包括 shell 里 BOT_ENV_FILE 指向的）：生成结果只取决于代码。
    # 项目模块都在函数里延迟 import，所以在这里设置仍然早于配置加载；测试里由 conftest 负责。
    os.environ["BOT_ENV_FILE"] = ""

    stale = False
    for path, render_target in TARGETS:
        name = path.relative_to(REPO_ROOT).as_posix()
        current = path.read_text(encoding="utf-8")
        rendered = render_target(current)
        if rendered == current:
            print(f"{name} is up to date")
        elif args.check:
            print(
                f"{name} is out of date; run: uv run python scripts/gen_ai_flow_doc.py",
                file=sys.stderr,
            )
            stale = True
        else:
            path.write_text(rendered, encoding="utf-8", newline="\n")
            print(f"updated {name}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
