"""工具循环测试用的工具替身。

工具循环从 `TOOL_SPECS` 声明表取工具（schema、处理函数与执行方式），测试换处理函数也改声明表。
"""

import dataclasses
import inspect
from types import MappingProxyType

from fogmoe_telegram_bot.features.ai.tools import specs
from fogmoe_telegram_bot.features.ai.tools.dispatch import ToolExecution
from fogmoe_telegram_bot.features.ai.tools.models import ToolArguments


def _execution_of(handler):
    return ToolExecution.ASYNC if inspect.iscoroutinefunction(handler) else ToolExecution.THREAD


def _always_configured(_settings=None):
    return True


def use_tool(monkeypatch, name, handler, *, execution=None):
    """把声明表里 `name` 的处理函数换成 `handler`，并当作已配置凭据（出现在 agent 的工具列表里）。

    执行方式默认按 `handler` 是否 `async def` 推断，同步的走线程适配器。
    """
    if name not in specs.TOOL_SPEC_BY_NAME:
        raise KeyError(name)
    replaced = tuple(
        dataclasses.replace(
            spec,
            handler=handler,
            execution=execution or _execution_of(handler),
            is_configured=_always_configured,
        )
        if spec.name == name
        else spec
        for spec in specs.TOOL_SPECS
    )
    monkeypatch.setattr(specs, "TOOL_SPECS", replaced)
    monkeypatch.setattr(
        specs,
        "TOOL_SPEC_BY_NAME",
        MappingProxyType({spec.name: spec for spec in replaced}),
    )


def fake_tool(name, handler, *, description="", execution=None, arg_model=ToolArguments):
    """不在声明表里的临时工具，传给 `run_tool_loop(tools=...)`。"""
    return specs.ToolSpec(
        name=name,
        description=description,
        arg_model=arg_model,
        handler=handler,
        execution=execution or _execution_of(handler),
        agents=("main",),
    )
