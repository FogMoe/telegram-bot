"""生成 docs/ai-conversation-flow.md 里标了「自动生成」的块。

只替换 `<!-- BEGIN GENERATED: 名字 -->` 与 `<!-- END GENERATED: 名字 -->` 之间的内容，手写部分不动：

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
from pathlib import Path
from typing import Any

# 不读开发者的 .env：生成结果只取决于代码。必须在 import 项目模块之前设置。
os.environ.setdefault("BOT_ENV_FILE", "")

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "ai-conversation-flow.md"
PACKAGE_PREFIX = "fogmoe_telegram_bot."

_BLOCK_RE = re.compile(
    r"(?P<begin><!-- BEGIN GENERATED: (?P<name>[a-z_]+) -->\n)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END GENERATED: (?P=name) -->)",
    re.DOTALL,
)


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


def render_stages() -> str:
    from fogmoe_telegram_bot.features.conversation.turn_types import Stage

    return " → ".join(f"`{stage.value}`" for stage in Stage) + "\n"


# -- 阈值与上限 -----------------------------------------------------------------


def render_limits() -> str:
    from fogmoe_telegram_bot.core import chat_records
    from fogmoe_telegram_bot.core.config import AppSettings
    from fogmoe_telegram_bot.features.ai import summary, tool_runner

    def setting(name: str) -> Any:
        return AppSettings.model_fields[name].default

    def param(func: Callable[..., Any], name: str) -> Any:
        return inspect.signature(func).parameters[name].default

    config_module = AppSettings.__module__
    token_limit = setting("CHAT_TOKEN_LIMIT")
    hard_ratio = setting("CHAT_CONTEXT_HARD_LIMIT_RATIO")

    # (名字, 默认值, 定义所在模块, 作用)
    rows: list[tuple[str, Any, str, str]] = [
        (
            "CHAT_BATCH_WINDOW_SECONDS",
            setting("CHAT_BATCH_WINDOW_SECONDS"),
            config_module,
            "同一个人在同一个聊天里连发的消息，等这么多秒后合成一批，只调用一次模型",
        ),
        (
            "CHAT_TURN_DEADLINE_SECONDS",
            setting("CHAT_TURN_DEADLINE_SECONDS"),
            config_module,
            "整轮截止时间，从进入队列开始计时，覆盖排队、媒体、模型与投递",
        ),
        (
            "CHAT_TOKEN_WARN_LIMIT",
            setting("CHAT_TOKEN_WARN_LIMIT"),
            config_module,
            "写入用户消息后活跃历史超过它：插入 near_limit 事件并提醒用户",
        ),
        (
            "CHAT_TOKEN_LIMIT",
            token_limit,
            config_module,
            "写入后活跃历史超过它：压缩（裁剪、归档、生成摘要）",
        ),
        (
            "CHAT_CONTEXT_HARD_LIMIT_RATIO",
            hard_ratio,
            config_module,
            "单次模型请求的硬上限倍数，见下一行",
        ),
        (
            "请求硬上限（推导）",
            int(token_limit * hard_ratio),
            "fogmoe_telegram_bot.features.ai.litellm_client",
            "CHAT_TOKEN_LIMIT × 倍数；消息 + 工具 schema + 最大输出 + 预留超过它就报错，"
            "不删也不截断任何内容",
        ),
        (
            "CHAT_CONTEXT_SAFETY_TOKENS",
            setting("CHAT_CONTEXT_SAFETY_TOKENS"),
            config_module,
            "估算请求大小时额外预留的 token",
        ),
        (
            "run_tool_loop(max_iterations)",
            param(tool_runner.run_tool_loop, "max_iterations"),
            tool_runner.__name__,
            "工具循环最多几轮；用完后不带工具再要一次最终回复",
        ),
        (
            "POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS",
            tool_runner.POST_TOOL_COMPLETION_RETRY_DELAYS_SECONDS,
            tool_runner.__name__,
            "工具执行过之后，模型调用遇到临时错误时的重试间隔（同一模型，不换 provider）",
        ),
        (
            "_trim_messages_with_tool_context(keep_non_tool)",
            param(chat_records._trim_messages_with_tool_context, "keep_non_tool"),
            chat_records.__name__,
            "压缩时保留最近几条非 tool 消息；它们引用的 tool_call 会一并保留，保持配对",
        ),
        (
            "PERMANENT_RECORDS_KEEP",
            chat_records.PERMANENT_RECORDS_KEEP,
            chat_records.__name__,
            "每个用户保留的归档快照数（user.permanent_records_limit 可单独设置），"
            "更早的打包发给用户后删除",
        ),
        (
            "SUMMARY_IMMEDIATE_TIMEOUT_SECONDS",
            summary.SUMMARY_IMMEDIATE_TIMEOUT_SECONDS,
            summary.__name__,
            "压缩时这一轮最多等多久的即时摘要，超时转后台生成",
        ),
        (
            "SUMMARY_CONCURRENCY",
            summary.SUMMARY_CONCURRENCY,
            summary.__name__,
            "同时生成的摘要数（即时与后台共用）",
        ),
        (
            "SUMMARY_TOOL_MAX_ITERATIONS",
            summary.SUMMARY_TOOL_MAX_ITERATIONS,
            summary.__name__,
            "摘要 agent 的工具循环轮数",
        ),
        (
            "SUMMARY_CONTEXT_HARD_LIMIT_RATIO",
            summary.SUMMARY_CONTEXT_HARD_LIMIT_RATIO,
            summary.__name__,
            "摘要请求的硬上限倍数（快照可能比活跃历史长）",
        ),
        (
            "SUMMARY_MAX_TOKENS",
            summary.SUMMARY_MAX_TOKENS,
            summary.__name__,
            "摘要正文超过它会被截短",
        ),
        (
            "SUMMARY_RETRY_LIMIT",
            summary.SUMMARY_RETRY_LIMIT,
            summary.__name__,
            "摘要整体重试次数（每次都把 provider 顺序走一遍）",
        ),
    ]
    return _table(
        ["名字", "默认值", "定义", "作用"],
        [
            [_code(name), _code(value), _source_link(module), _cell(purpose)]
            for name, value, module, purpose in rows
        ],
    )


# -- 工具目录 -------------------------------------------------------------------


def render_tools() -> str:
    from fogmoe_telegram_bot.features.ai import idle_followup, summary
    from fogmoe_telegram_bot.features.ai.tools.registry import AI_TOOL_HANDLERS, OPENAI_TOOLS

    # 每个 agent 能看到的工具 schema 与处理函数；同一个工具可能出现在多个 agent 里。
    agents: list[tuple[str, list[dict[str, Any]], Mapping[str, Callable[..., Any]]]] = [
        ("主聊天", OPENAI_TOOLS, AI_TOOL_HANDLERS),
        ("空闲回顾", idle_followup.IDLE_RECAP_TOOLS, idle_followup.IDLE_RECAP_TOOL_HANDLERS),
        ("摘要", summary.SUMMARY_TOOLS, summary.SUMMARY_TOOL_HANDLERS),
    ]

    order: list[str] = []
    functions: dict[str, dict[str, Any]] = {}
    handlers: dict[str, Callable[..., Any]] = {}
    users: dict[str, list[str]] = {}
    for agent, tools, agent_handlers in agents:
        for tool in tools:
            function = tool["function"]
            name = function["name"]
            if name not in functions:
                order.append(name)
                functions[name] = function
                handlers[name] = agent_handlers[name]
            users.setdefault(name, []).append(agent)

    rows = []
    for name in order:
        function = functions[name]
        parameters = function.get("parameters") or {}
        required = set(parameters.get("required") or [])
        params = ", ".join(
            f"`{param}`" if param in required else f"`{param}?`"
            for param in (parameters.get("properties") or {})
        )
        handler = handlers[name]
        module_name = handler.__module__.rsplit(".", 1)[-1]
        rows.append(
            [
                f"`{name}`",
                "、".join(users[name]),
                params or "—",
                f"`{module_name}.{handler.__name__}`",
                _cell(function.get("description") or ""),
            ]
        )

    counts = "，".join(
        f"{agent} {len(tools)} 个" for agent, tools, _ in agents
    )
    header = f"共 {len(order)} 个工具：{counts}。参数后面带 `?` 的是可选参数。\n\n"
    return header + _table(["工具", "谁能调用", "参数", "处理函数", "描述（模型看到的原文）"], rows)


GENERATORS: dict[str, Callable[[], str]] = {
    "stages": render_stages,
    "limits": render_limits,
    "tools": render_tools,
}


def render(text: str) -> str:
    """把文档里每个生成块替换成最新内容；块名未知或缺块时报错。"""
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只检查，不写回")
    args = parser.parse_args(argv)

    doc_name = DOC_PATH.relative_to(REPO_ROOT).as_posix()
    current = DOC_PATH.read_text(encoding="utf-8")
    rendered = render(current)
    if rendered == current:
        print(f"{doc_name} is up to date")
        return 0
    if args.check:
        print(
            f"{doc_name} is out of date; run: uv run python scripts/gen_ai_flow_doc.py",
            file=sys.stderr,
        )
        return 1
    DOC_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"updated {doc_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
