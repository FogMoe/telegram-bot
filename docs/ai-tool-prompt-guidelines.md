# AI 工具提示词与描述规范

本文规定 AI 工具相关文字应分别放在哪里、承担什么职责，以及如何避免主系统提示词、工具描述、参数描述和工具内部提示词相互重复。

适用于：

- `resources/prompts/system_prompt.md` 中的工具调用规则；
- `modules/features/ai/tools/schemas.py` 中的工具描述；
- `modules/features/ai/tools/models.py` 中的参数描述与约束；
- 由工具调用其他模型时使用的独立 system prompt；
- 工具 handler 中的运行时校验、限流和错误处理。

`advisor` 是当前推荐参考。已有工具如果仍混合了多层职责，可在后续修改对应工具时逐步迁移，不要求一次性重写全部存量提示词。

## 核心原则

### 单一职责

同一条信息只应有一个主要来源：

| 信息 | 应放位置 |
|---|---|
| 什么时候调用、什么时候不要调用 | 主助手 system prompt |
| 工具做什么、怎样使用、能力与结果边界 | tool description |
| 单个参数的含义、格式、单位、默认值 | 参数 description |
| 参数长度、数值范围、枚举、必填关系 | Pydantic/JSON Schema 与 handler |
| 工具内部模型的角色、分析方法、信任边界、输出对象 | 工具独立 system prompt |
| 权限、限流、超时、并发、状态校验、错误脱敏 | handler/config |
| 所有工具共同遵守的输出与交互规则 | 主 system prompt 的通用 Tool Calling 规则 |

### 不依赖提示词实现硬约束

提示词用于引导模型选择和正确使用工具，不能代替代码校验。凡是涉及费用、安全、权限、资源占用或数据完整性的规则，都必须由代码执行。

例如：

- 最大字符串长度使用 Pydantic `max_length`；
- 数值范围使用 `ge`、`le`；
- 调用次数、用户限流、并发上限由 handler 控制；
- provider、model、timeout 等服务器策略不得暴露为模型可传参数；
- 原始 API 异常只写日志，返回给主模型的结果必须脱敏。

### 避免重复

主 system prompt 和 tool description 都会被主模型看到。重复同一段用途、限制或参数说明会：

- 增加上下文 token；
- 造成两处文案逐渐不一致；
- 稀释真正重要的调用条件；
- 让维护者无法判断哪一处是权威来源。

允许不同层从各自职责出发提及同一概念，但不要重复完整句子或规则列表。例如主 system prompt 可以写“不要用于实时事实查询”，tool description 则只需声明“不能验证当前外部事实”。前者是调用决策，后者是能力边界。

## 第一层：主助手 system prompt

位置：`resources/prompts/system_prompt.md`

### 应该包含

每个工具的小节只描述选择策略：

- 哪类用户意图或任务应调用；
- 哪些容易混淆的场景不应调用；
- 必要时说明与另一个工具的选择边界。

推荐格式：

```markdown
### tool_name
- Call this tool when <明确的调用条件>.
- Do not call it for <最重要的反例或替代场景>.
```

简单工具只需一条调用条件，不必强行增加反例。

### 不应该包含

- 参数名、参数拼接格式或默认值；
- 返回 JSON 的字段说明；
- provider、model、timeout 等后端实现；
- handler 已经强制执行的长度、次数或并发限制；
- 工具内部模型的角色提示词；
- 与 tool description 相同的用途和能力介绍。

所有工具共同遵守的规则仍放在 `# Tool Calling / ## Calling Rules`，例如工具结果不直接展示给用户、主模型应综合工具结果后回答。这类规则不应在每个工具小节重复。

## 第二层：tool description

位置：`modules/features/ai/tools/schemas.py`

tool description 是工具自身的使用说明，必须在脱离主 system prompt 时仍能让模型理解：

- 工具的核心用途和产生的价值；
- 怎样组织输入，尤其是多个参数之间的关系；
- 重要的能力边界和副作用；
- 返回结果应如何理解或使用。

推荐结构：

```text
<动作和用途>。Pass/Use <参数之间的组织方式>。
The tool <关键能力边界或副作用>。
Returns/Its response <结果语义>。
```

tool description 不应重复列出完整的调用时机和禁用场景，也不应复制每个参数的长度、枚举和值域。这些信息分别属于主 system prompt 和参数 schema。

### 描述要求

- 使用准确动词开头，例如 `Fetch`、`Generate`、`Schedule`、`Submit`；
- 明确只读、写入、发送消息、生成媒体等副作用；
- 不宣称 handler 或外部服务实际不具备的能力；
- 描述当前行为，不写实现历史或未来计划；
- 保持简洁，复杂工具可使用数句，简单工具通常一句即可。

## 第三层：参数 description 与 Schema

位置：`modules/features/ai/tools/models.py`

每个参数 description 只解释该字段：

- 字段承载什么内容；
- 使用什么格式或单位；
- 何时必填、何时忽略；
- 与另一个参数的直接关系；
- 必要的语义提醒。

结构化约束应直接写进 Pydantic 字段：

```python
task: str = Field(
    min_length=1,
    max_length=6000,
    description="A self-contained reasoning task for the senior advisor.",
)
```

### 参数描述不应该做的事

- 重新介绍整个工具；
- 重复主 system prompt 的调用时机；
- 仅用自然语言描述本可由 `min_length`、`max_length`、`ge`、`le` 或枚举执行的约束；
- 暗示模型可以传入 schema 中不存在的 provider、model 或权限参数。

参数模型生成的 JSON Schema 是工具调用参数的权威来源，handler 仍应保护直接调用或异常输入等边界。

## 第四层：工具独立 system prompt

只有当工具内部还会调用一个模型时，才需要独立 system prompt。资源文件放在 `resources/prompts/`，例如：

```text
resources/prompts/advisor_system_prompt.md
```

它面向工具内部模型，而不是主助手，应定义：

- 内部模型扮演的角色；
- 应采用的分析或生成方式；
- 输入内容的信任边界；
- 是否有工具、是否可以采取行动；
- 输出交给谁以及应采用什么表达方式。

它不应说明主助手什么时候调用该工具，也不应重复工具参数 schema。长提示词应保存为资源文件，由 `config.py` 统一加载，不要内嵌在 handler 或放进 `.env`。

## 第五层：handler 与配置

位置通常为 `modules/features/ai/tools/*_tools.py` 和 `modules/core/config.py`。

以下规则必须由运行时保证：

- 身份、群组、权限或用户状态检查；
- 每请求调用次数、用户限流和全局并发；
- 网络超时、输出大小和资源生命周期；
- provider/model 的服务器端选择；
- 不允许模型覆盖的固定策略；
- API 异常、凭据和 endpoint 信息的脱敏；
- 可 JSON 序列化且稳定的成功/失败结果结构。

如果一项限制已经由代码执行，面向主模型的文字只需说明对正确使用有帮助的语义，不必复制具体实现值。运维默认值和环境变量应记录在开发文档中，而不是放入主 system prompt。

## `advisor` 参考实现

### 主 system prompt：仅说明调用时机

```markdown
### advisor
- Call this tool only when a complex decision, difficult analysis, conflicting evidence, or important plan would materially benefit from a senior second opinion.
- Do not call it for ordinary conversation, simple questions, calculations, or real-time factual lookup.
```

### Tool description：说明用途和用法

```text
Submit a self-contained reasoning task to a read-only senior advisor. Pass the question or decision in task and only its relevant facts, evidence, options, and constraints in context. Each call is a single-turn consultation: the advisor cannot receive follow-up messages, so include everything needed in one request. The advisor cannot use tools, verify current external facts, contact users, or take actions. Its response is advisory material for you to evaluate and synthesize.
```

### 参数描述：分别解释字段

- `task`：需要分析的自包含问题、决策或主张；
- `context`：与该问题直接相关的事实、证据、选项和约束；
- provider、model、timeout、token 上限均为服务器配置，不是工具参数。

### 独立 system prompt：约束内部 advisor 模型

`resources/prompts/advisor_system_prompt.md` 定义 advisor 的分析职责、不可信输入边界、无工具/无行动权限，以及“输出给主助手而非直接回复用户”的要求。

### Handler：执行硬约束

`advisor_tools.py` 负责配置检查、请求级调用上限、用户限流、进程内并发、超时、输出 token 上限和错误脱敏。这些规则不能只依赖文字提示。

## 常见反模式

### 在主 system prompt 复制完整使用手册

错误示例：

```markdown
### example_tool
- Call it when ...
- Pass foo in ...
- bar defaults to 10 and must be 1-20.
- It returns {"status": ..., "data": ...}.
- The request timeout is 30 seconds.
```

这里混合了调用时机、参数 schema、返回结构和运行配置。主 system prompt 应只保留第一条，其余内容分别移到 tool description、参数模型、handler 或开发文档。

### 在 tool description 重复调用策略

如果主 system prompt 已详细规定“复杂决策才调用、简单问题不要调用”，tool description 不要再次复制相同条件列表。它应改为解释工具提供什么、输入如何组织、结果是什么性质。

### 用文字代替代码限制

“最多调用一次”“最大 6000 字符”“只能由付费用户使用”等规则，如果只写在提示词中就不构成可靠限制，必须同时由 handler 或 schema 强制执行。

### 把工具内部 prompt 写进工具描述

工具描述不应要求主模型模拟内部模型的完整角色和推理流程。内部模型的行为放入独立 system prompt，主模型只需知道怎样提交任务和怎样使用结果。

## 新增或修改工具的流程

1. 在 `models.py` 定义参数模型、字段说明和结构化约束。
2. 在 `schemas.py` 编写用途、用法、边界和结果语义。
3. 在主 `system_prompt.md` 仅增加必要的调用/不调用条件。
4. 在 `registry.py` 注册 handler。
5. 在 handler 中执行权限、资源、状态和安全限制。
6. 如果工具内部调用模型，在 `resources/prompts/` 新增独立 prompt，并由 `config.py` 加载。
7. 将运维配置记录在对应开发文档中。
8. 按 `docs/testing-guidelines.md` 添加小而稳定的测试，不访问真实外部服务。

## Review 清单

提交工具相关改动前确认：

- [ ] 主 system prompt 是否只描述调用选择？
- [ ] tool description 是否独立说明用途、用法和边界？
- [ ] 参数 description 是否只解释对应字段？
- [ ] 同一规则是否在多个位置逐字或近似重复？
- [ ] 长度、值域、枚举和必填关系是否进入 Schema？
- [ ] 权限、费用和资源限制是否由 handler 执行？
- [ ] provider/model 等服务器策略是否未暴露为工具参数？
- [ ] 原始外部错误是否只进入日志，返回结果是否脱敏？
- [ ] 内部模型 prompt 是否独立保存并明确输入信任边界？
- [ ] 工具名、schema 名、registry 名和 system prompt 标题是否完全一致？
- [ ] 测试是否覆盖注册、参数校验、关键限制和错误路径？
