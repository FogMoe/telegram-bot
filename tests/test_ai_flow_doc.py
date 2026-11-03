"""docs/ai-conversation-flow.md 与代码保持同步。

- 生成块（工具目录、阶段、阈值）与 `scripts/gen_ai_flow_doc.py` 的输出一致；
- 正文里用反引号引用的符号仍出现在源码里，引用的文件与相对链接仍然存在。

图里的节点都在图下面的表里写了对应符号，所以改名或删除符号时这里会失败。
"""

import functools
import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "ai-conversation-flow.md"
PACKAGE_DIR = REPO_ROOT / "src" / "fogmoe_telegram_bot"

_FENCED_BLOCK_RE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
_GENERATED_BLOCK_RE = re.compile(
    r"<!-- BEGIN GENERATED: (?P<name>[a-z_]+) -->.*?<!-- END GENERATED: (?P=name) -->",
    re.DOTALL,
)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
# `ConversationTurn.run`、`_reply_locked`、`run_tool_loop(max_iterations)` 这类代码符号。
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*(\(\w*\))?$")
_PATH_RE = re.compile(r"^[\w./-]+\.(py|md|toml|ya?ml)$")


def _load_generator():
    path = REPO_ROOT / "scripts" / "gen_ai_flow_doc.py"
    spec = importlib.util.spec_from_file_location("gen_ai_flow_doc", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


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


def test_generated_blocks_are_up_to_date():
    text = _doc_text()
    assert _load_generator().render(text) == text, (
        "docs/ai-conversation-flow.md 的生成块已过期，运行：uv run python scripts/gen_ai_flow_doc.py"
    )


def test_referenced_symbols_and_files_exist():
    words = _source_words()
    missing: list[str] = []
    for reference in _INLINE_CODE_RE.findall(_prose(_doc_text())):
        if _PATH_RE.match(reference):
            if not _path_exists(reference):
                missing.append(reference)
        elif _SYMBOL_RE.match(reference):
            names = re.findall(r"[A-Za-z_]\w*", reference)
            if any(name not in words for name in names):
                missing.append(reference)
    assert not missing, f"文档引用了源码里已经找不到的符号或文件：{missing}"


def test_relative_links_resolve():
    broken = []
    for target in _LINK_RE.findall(_doc_text()):
        if "://" in target or target.startswith("#"):
            continue
        path = target.split("#", 1)[0]
        if not (DOC_PATH.parent / path).exists():
            broken.append(target)
    assert not broken, f"文档里的相对链接指向不存在的文件：{broken}"
