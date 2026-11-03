"""docs/ai-conversation-flow.{md,html} 与代码保持同步。

- 两份文档里同步的部分（md 的生成块、HTML 里的数字）与 `scripts/gen_ai_flow_doc.py` 的输出一致；
- HTML 页面的步骤与 `Stage`、工具箱与各 agent 开放的工具一一对应；
- md 正文里用反引号引用的符号仍出现在源码里，引用的文件与两份文档里的相对链接仍然存在。
"""

import functools
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "ai-conversation-flow.md"
HTML_PATH = REPO_ROOT / "docs" / "ai-conversation-flow.html"
PACKAGE_DIR = REPO_ROOT / "src" / "fogmoe_telegram_bot"
UPDATE_HINT = "运行：uv run python scripts/gen_ai_flow_doc.py"

_FENCED_BLOCK_RE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
_GENERATED_BLOCK_RE = re.compile(
    r"<!-- BEGIN GENERATED: (?P<name>[a-z_]+) -->.*?<!-- END GENERATED: (?P=name) -->",
    re.DOTALL,
)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_HTML_LINK_RE = re.compile(r'<a href="([^"]+)"')
# `ConversationTurn.run`、`_reply_locked`、`run_tool_loop(max_iterations)` 这类代码符号。
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*(\(\w*\))?$")
_PATH_RE = re.compile(r"^[\w./-]+\.(py|md|html|toml|ya?ml)$")
_HTML_TOOL_RE = re.compile(r'data-tool="(?P<name>\w+)" data-agents="(?P<agents>[\w ]+)"')


@functools.cache
def _generator():
    path = REPO_ROOT / "scripts" / "gen_ai_flow_doc.py"
    spec = importlib.util.spec_from_file_location("gen_ai_flow_doc", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析注解时要在 sys.modules 里找到所在模块。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _md_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _html_text() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _prose(text: str) -> str:
    """去掉代码块与生成块后的正文：生成块来自代码本身，代码块里是示意。"""
    return _FENCED_BLOCK_RE.sub("", _GENERATED_BLOCK_RE.sub("", text))


@functools.cache
def _source_words() -> frozenset[str]:
    words: set[str] = set()
    for path in PACKAGE_DIR.rglob("*.py"):
        words.update(re.findall(r"\w+", path.read_text(encoding="utf-8")))
    return frozenset(words)


def _path_exists(reference: str) -> bool:
    """仓库根、包目录或 `features/` 下的路径都算（正文里 `ai/summary.py` 这种写法省略了前缀）。"""
    roots = (REPO_ROOT, PACKAGE_DIR, PACKAGE_DIR / "features")
    return any((root / reference).exists() for root in roots)


def _broken_links(links: list[str]) -> list[str]:
    broken = []
    for target in links:
        if "://" in target or target.startswith("#"):
            continue
        if not (DOC_PATH.parent / target.split("#", 1)[0]).exists():
            broken.append(target)
    return broken


def test_md_generated_blocks_are_up_to_date():
    text = _md_text()
    assert _generator().render(text) == text, f"md 的生成块已过期，{UPDATE_HINT}"


def test_html_values_are_up_to_date():
    text = _html_text()
    assert _generator().render_html(text) == text, f"HTML 页面里的数字已过期，{UPDATE_HINT}"


def test_html_steps_match_turn_stages():
    stages = re.findall(r'data-stage="(\w+)"', _html_text())
    assert stages == _generator().stage_values(), (
        "HTML 页面「一轮对话」的步骤要与 Stage 一一对应、顺序一致；改了阶段就同步改页面"
    )


def test_html_toolbox_matches_agent_tools():
    page = {
        match["name"]: tuple(match["agents"].split())
        for match in _HTML_TOOL_RE.finditer(_html_text())
    }
    code = {tool.name: tool.agents for tool in _generator().tool_catalog()}
    missing = sorted(set(code) - set(page))
    extra = sorted(set(page) - set(code))
    wrong_agents = sorted(name for name in set(code) & set(page) if code[name] != page[name])
    assert not (missing or extra or wrong_agents), (
        "HTML 页面「工具箱」与代码不一致："
        f"缺少说明 {missing}，代码里已没有 {extra}，谁能调用不对 {wrong_agents}"
        f"（应为 {[code[name] for name in wrong_agents]}）"
    )


def test_md_referenced_symbols_and_files_exist():
    words = _source_words()
    missing: list[str] = []
    for reference in _INLINE_CODE_RE.findall(_prose(_md_text())):
        if _PATH_RE.match(reference):
            if not _path_exists(reference):
                missing.append(reference)
        elif _SYMBOL_RE.match(reference):
            names = re.findall(r"[A-Za-z_]\w*", reference)
            if any(name not in words for name in names):
                missing.append(reference)
    assert not missing, f"文档引用了源码里已经找不到的符号或文件：{missing}"


def test_relative_links_resolve():
    broken = _broken_links(_MD_LINK_RE.findall(_md_text()))
    broken += _broken_links(_HTML_LINK_RE.findall(_html_text()))
    assert not broken, f"文档里的相对链接指向不存在的文件：{broken}"
