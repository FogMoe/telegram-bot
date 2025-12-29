# AI 工具

该目录包含 AI 聊天层使用的 OpenAI 兼容工具定义与处理函数。

## 目录结构

- `context.py`：每次请求的上下文存储（用户/群组/消息元信息）
- `schemas.py`：工具 schema 定义（OpenAI JSON Schema）
- `registry.py`：工具注册中心（名称 -> 处理函数）
- `http_tools.py`：外部 HTTP 工具（SerpApi、Jina Reader）
- `code_tools.py`：Judge0 执行工具
- `user_tools.py`：用户/金币/好感/印象相关工具
- `memory_tools.py`：群聊上下文与永久摘要工具

## 添加新工具

1) 在合适模块里实现工具函数（必要时新建模块）。
2) 在 `schemas.py` 添加对应 schema。
3) 在 `registry.py` 注册工具处理函数。
4) 只有在其他模块需要直接调用时，才在 `__init__.py` 里导出。

## 注意事项

- 工具处理函数必须是同步函数，且返回可 JSON 序列化的 dict。
- 依赖聊天/用户上下文的工具务必通过 `context.get_tool_request_context()` 读取。
- 尽量避免长耗时网络调用，超时设置要保守。
