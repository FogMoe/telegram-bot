"""工具循环测试用的工具替身。

工具循环只从 `TOOL_SPECS` 声明表按 agent 取工具（schema、处理函数与执行方式），测试换工具也改声明表。
"""

import dataclasses
import inspect
from types import MappingProxyType

from fogmoe_telegram_bot.features.ai.agents import MAIN_AGENT
from fogmoe_telegram_bot.features.ai.tools import specs
from fogmoe_telegram_bot.features.ai.tools.dispatch import ToolExecution
from fogmoe_telegram_bot.features.ai.tools.models import ToolArguments

# 只看得到 `register_tool` 登记的工具的 agent；其余参数与主聊天相同。
TEST_AGENT = dataclasses.replace(MAIN_AGENT, name="test")


def _execution_of(handler):
    return ToolExecution.ASYNC if inspect.iscoroutinefunction(handler) else ToolExecution.THREAD


def _always_configured(_settings=None):
    return True


def _install(monkeypatch, replaced):
    monkeypatch.setattr(specs, "TOOL_SPECS", tuple(replaced))
    monkeypatch.setattr(
        specs,
        "TOOL_SPEC_BY_NAME",
        MappingProxyType({spec.name: spec for spec in replaced}),
    )


def use_tool(monkeypatch, name, handler, *, execution=None):
    """把声明表里 `name` 的处理函数换成 `handler`，并当作已配置凭据（出现在 agent 的工具列表里）。

    执行方式默认按 `handler` 是否 `async def` 推断，同步的走线程适配器。
    """
    if name not in specs.TOOL_SPEC_BY_NAME:
        raise KeyError(name)
    _install(
        monkeypatch,
        [
            dataclasses.replace(
                spec,
                handler=handler,
                execution=execution or _execution_of(handler),
                is_configured=_always_configured,
            )
            if spec.name == name
            else spec
            for spec in specs.TOOL_SPECS
        ],
    )


def register_tool(monkeypatch, name, handler, *, description=None, execution=None):
    """把一个只属于 `TEST_AGENT` 的工具登记进声明表。

    名字已经存在时替换那一条（保留它的参数模型，其他 agent 看不到它了）；否则新建一条，不接受参数。
    """
    existing = specs.TOOL_SPEC_BY_NAME.get(name)
    if existing is None:
        spec = specs.ToolSpec(
            name=name,
            description=description or "",
            arg_model=ToolArguments,
            handler=handler,
            execution=execution or _execution_of(handler),
            agents=(TEST_AGENT.name,),
        )
    else:
        spec = dataclasses.replace(
            existing,
            description=existing.description if description is None else description,
            handler=handler,
            execution=execution or _execution_of(handler),
            agents=(TEST_AGENT.name,),
            is_configured=_always_configured,
        )
    _install(
        monkeypatch,
        [*(item for item in specs.TOOL_SPECS if item.name != name), spec],
    )
