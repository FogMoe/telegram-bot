"""分层的 import 方向：core 不 import features 与 app，features 之间默认不互相 import。

见 docs/architecture.md 的「依赖方向」。检查的是代码结构（AST），函数里的延迟 import 与 `TYPE_CHECKING`
下的 import 一样算数；清单（ALLOWED_CROSS_FEATURE）记录了仍然存在的跨功能 import 与理由。
"""

import ast
import functools
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
PACKAGE = "fogmoe_telegram_bot"
PACKAGE_DIR = SRC_DIR / PACKAGE
FEATURES = f"{PACKAGE}.features"

# 仍然存在的跨功能 import：（源功能，目标模块）。源功能里的任何模块都可以 import 目标模块及其子模块；
# 新增必须在这里写明理由。
ALLOWED_CROSS_FEATURE = {
    # 对话是 AI 业务的调用方，AI 不反向依赖对话。
    ("conversation", f"{FEATURES}.ai"),
    # `/start` 处理推广邀请码，待用启动参数回调解耦，见「已知遗留」。
    ("profile", f"{FEATURES}.economy.ref"),
}


def source_files():
    return sorted(path for path in PACKAGE_DIR.rglob("*.py") if "__pycache__" not in path.parts)


def module_name(path: Path) -> str:
    parts = path.relative_to(SRC_DIR).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def is_module(name: str) -> bool:
    path = SRC_DIR.joinpath(*name.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


@functools.cache
def project_imports(path: Path) -> frozenset[str]:
    """这个文件 import 的项目内模块；`from pkg import sub` 在 `sub` 是模块时记作 `pkg.sub`。"""
    name = module_name(path)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    imports = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                anchor = parts[: len(parts) - node.level + 1]
                base = ".".join([*anchor, base] if base else anchor)
            for alias in node.names:
                submodule = f"{base}.{alias.name}"
                imports.add(submodule if is_module(submodule) else base)
    return frozenset(name for name in imports if name == PACKAGE or name.startswith(PACKAGE + "."))


def layer(module: str) -> str | None:
    """`core` / `app` / `features`；包入口与 `main` 不属于任何一层。"""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[1] in {"core", "app", "features"} else None


def feature(module: str) -> str | None:
    parts = module.split(".")
    return parts[2] if len(parts) > 2 and parts[1] == "features" else None


def within(module: str, target: str) -> bool:
    return module == target or module.startswith(target + ".")


def cross_feature_imports():
    """（源模块，目标模块）：两者属于不同的功能。"""
    for path in source_files():
        source = module_name(path)
        if feature(source) is None:
            continue
        for target in project_imports(path):
            if feature(target) not in (None, feature(source)):
                yield source, target


def allowances(source: str, target: str):
    return {
        (source_feature, allowed)
        for source_feature, allowed in ALLOWED_CROSS_FEATURE
        if feature(source) == source_feature and within(target, allowed)
    }


def relative(path: Path) -> str:
    return path.relative_to(PACKAGE_DIR).as_posix()


def test_importing_a_submodule_from_its_package_names_the_submodule():
    # `from fogmoe_telegram_bot.features.economy import ref`
    assert f"{FEATURES}.economy.ref" in project_imports(
        PACKAGE_DIR / "features" / "profile" / "handlers.py"
    )


@pytest.mark.slow
def test_every_import_resolves_to_an_existing_module():
    # 相对 import 解析错了会悄悄指向不存在的模块，下面的方向检查就会漏掉它。
    unresolved = {
        relative(path): sorted(name for name in project_imports(path) if not is_module(name))
        for path in source_files()
    }

    assert {path: names for path, names in unresolved.items() if names} == {}


@pytest.mark.slow
def test_core_does_not_import_features_or_app():
    offenders = {
        relative(path): sorted(
            name for name in project_imports(path) if layer(name) in {"features", "app"}
        )
        for path in source_files()
        if layer(module_name(path)) == "core"
    }

    assert {path: names for path, names in offenders.items() if names} == {}


@pytest.mark.slow
def test_features_do_not_import_each_other():
    offenders = sorted(
        f"{source} -> {target}"
        for source, target in cross_feature_imports()
        if not allowances(source, target)
    )

    assert offenders == []


@pytest.mark.slow
def test_every_allowed_cross_feature_import_is_still_used():
    # 解耦之后要把对应条目从清单里删掉，免得它继续放行新的 import。
    used = set().union(*(allowances(source, target) for source, target in cross_feature_imports()))

    assert used == ALLOWED_CROSS_FEATURE
