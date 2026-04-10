# Mini-Agent 学习与重写路线图



ReAct Agent 循环 — 自己实现，能讲清推理和行动的交替逻辑
多模型适配层 — 支持 2-3 个模型就够，展示抽象设计能力
上下文压缩 — 自己设计压缩策略，这是面试高频话题
至少一个有深度的工具 — 比如 AST 代码分析或依赖图生成，而不只是文件读写


> 作者：Codex（GPT-5）
> 日期：2026-03-31
> 面向读者：具备 `Paismart`（Java RAG）+ `GuGoTik`（Go 微服务）背景，希望系统掌握 Mini-Agent，并在后续用 Go 重写它的开发者

---

## 0. 这份文档解决什么问题

这不是一份泛泛的“4 周计划”，而是一份面向你当前背景的**可执行学习手册**。它重点解决以下问题：

1. 你已经会什么，不必从零学什么。
2. 你学 Mini-Agent 之前还欠缺哪些知识，优先级怎么排。
3. 这个项目怎么启动、怎么调试、怎么验证、怎么定位问题。
4. 项目整体结构里，每个目录、模块、文件大概负责什么。
5. 你应该按什么顺序读代码，才能最快建立正确心智模型。
6. 学完之后，如果用 Go 重写，应该先重写什么、后重写什么，哪些设计要保留，哪些应该升级。
7. 如果时间充裕，如何把文档里提到的“demo -> production”改进项变成真正的工程路线。

---

## 1. 先给你一个判断：你学这个项目，不是从零开始

### 1.1 你已经具备的迁移优势

从你的两个项目背景出发，你对这个仓库并不陌生，只是表达形式从 Java/Go 切到了 Python：

#### 来自 Paismart 的迁移优势

- 你已经理解 `LLM API 接入`，所以不会卡在“模型调用是怎么回事”。
- 你已经理解 `RAG 主链路`，所以能很快接受 Agent 的主循环是“RAG 的泛化版”。
- 你已经做过 `WebSocket / 实时交互`，所以理解后续流式输出、状态同步、编辑器集成不会困难。
- 你已经做过 `多租户 / 权限 / 文档处理 / 存储整合`，所以你天然知道 demo 和 production 的差距在哪里。
- 你已经接过 `DeepSeek / Ollama`，所以抽象 provider、封装 API wrapper 这件事你已经有工程直觉。

#### 来自 GuGoTik 的迁移优势

- 你已经习惯 `模块化分层`，所以会很自然地把 `cli / agent / llm / tools / acp / config` 看成不同责任边界。
- 你已经做过 `服务注册发现 / RPC / 配置分层`，所以理解 MCP / ACP 不会太抽象。
- 你已经做过 `容器化 / CI/CD / 可观测性`，所以你能很快识别这个项目哪些地方只是 demo，哪些地方未来必须补齐。
- 你已经有 `Go 并发` 经验，所以后续重写时会天然想到 `goroutine + channel + context.Context` 对应 Python 的 `asyncio`。

### 1.2 你不需要浪费时间补的知识

下面这些你只需要“对照理解”，不用从基础课重新学：

- HTTP API 基础
- 配置文件与环境变量管理
- 业务分层思想
- JSON Schema 基本概念
- 存储系统集成思路
- 服务调用与故障处理思路
- 基本的软件工程能力

### 1.3 你真正欠缺的，不是 Agent 概念，而是 Python Agent 工程语境

你现在最需要补的是：

1. Python 异步编程和进程管理的具体写法。
2. Python 类型系统、Pydantic、包管理、测试工具链。
3. Anthropic/OpenAI 两套 Tool Calling 消息格式差异。
4. Agent 的“消息历史压缩、工具回填、思考块保留”这些运行时细节。
5. MCP / ACP 在这个项目里的具体落地方式。

---

## 2. 项目一句话画像

Mini-Agent 是一个 **Python 单体式 Agent Demo / 教学骨架**。

它做的事情很清楚：

- 通过 `CLI` 接受任务
- 加载 `Config`
- 初始化 `LLM Client`
- 装配 `基础工具 + Skills + MCP 工具`
- 进入 `Agent ReAct 循环`
- 在对话变长时做 `摘要压缩`
- 通过 `ACP` 提供编辑器侧集成

它没有做好的事情也很清楚：

- 没有完整的生产级安全隔离
- 没有完整的持久化上下文体系
- 没有严密的工具权限控制
- 没有系统级 observability
- 没有完整的流式输出和多 Agent 协作
- 没有真正的高可用 provider fallback

所以它很适合你当前阶段：**先快速建立 Agent 系统的正确骨架认知，再决定哪些能力值得用 Go 重建。**

---

## 3. 代码库结构总览：每一部分是干什么的

---

### 3.1 仓库顶层

#### `README.md` / `README_CN.md`

项目定位、安装方式、运行方式、ACP 集成、示例、测试说明。

你要从这里拿到的信息：

- 项目的目标不是“完整产品”，而是“最小但专业的 Agent demo”
- 官方推荐的启动方式是什么
- 哪些功能是主打：工具循环、Session Note、Skills、MCP、日志

#### `pyproject.toml`

Python 项目的包定义、依赖、脚本入口、pytest 配置。

你要重点看：

- 项目名：`mini-agent`
- 脚本入口：
  - `mini-agent = mini_agent.cli:main`
  - `mini-agent-acp = mini_agent.acp.server:main`
- 关键依赖：
  - `anthropic`
  - `openai`
  - `mcp`
  - `agent-client-protocol`
  - `prompt-toolkit`
  - `tiktoken`
  - `pydantic`

#### `examples/`

按学习路径组织的示例。

- `01_basic_tools.py`：不经过 Agent，只看工具本身
- `02_simple_agent.py`：最小 Agent
- `03_session_notes.py`：记忆工具
- `04_full_agent.py`：全量组合
- `05_provider_selection.py` / `06_tool_schema_demo.py`：模型与 Schema 相关

你应把它看成：

- `controller 层 demo`
- `集成测试的人工可读版`

#### `tests/`

这是你建立心智模型的核心材料之一。

不要把 tests 当成“补充材料”，而要把它看成：

- 功能边界文档
- 行为契约
- 最小回归标准

#### `docs/`

开发和生产指南。

注意：文档有一定滞后，部分路径描述和当前代码结构不完全同步，所以不能只信文档，要和源码互相校验。

#### `learning-plan-by-claude.md`

这是一份针对你背景的高层计划，但不够细。你现在看的这份文档，等于是在它的基础上做“落地执行版升级”。

---

### 3.2 `mini_agent/` 核心源码目录

#### `mini_agent/cli.py`

这是**命令行入口和装配器**。

它负责：

- 解析命令行参数
- 加载配置
- 初始化 LLM Client
- 加载基础工具、Skills、MCP 工具
- 加载 System Prompt
- 创建 Agent
- 启动交互循环或单任务运行
- 处理 `/help /clear /history /stats /log`
- 处理 `Esc` 中断
- 清理 MCP 连接

从工程分层看，它更像：

- Java 项目里的 `Application + Bootstrap + Console Controller`
- Go 项目里的 `cmd/xxx/main.go + wire` 的混合体

重点阅读位置：

- `initialize_base_tools`
- `add_workspace_tools`
- `run_agent`
- `main`

#### `mini_agent/agent.py`

这是**核心 Agent 运行时**。

它负责：

- 保存消息历史
- 将用户消息追加到上下文
- 调用 LLM
- 接收 `thinking / content / tool_calls`
- 执行工具
- 把工具结果回填到消息历史
- 控制 `max_steps`
- 在 token 过大时触发摘要压缩
- 处理取消与中断
- 记录日志

这是你需要反复精读的第一核心文件。

从概念上，它对应：

- Paismart 中的“RAG 主流程编排器”的升级版
- GuGoTik 中的“一个单节点内 orchestrator / workflow runner”

#### `mini_agent/config.py`

这是**配置加载和配置优先级解析器**。

它负责：

- 把扁平 YAML 转成结构化配置对象
- 维护 `llm / agent / tools / mcp timeout` 的配置模型
- 定义配置查找优先级：
  1. 当前仓库 `mini_agent/config/`
  2. 用户目录 `~/.mini-agent/config/`
  3. 安装包目录

对应你的经验：

- 类似 Spring Boot 的配置绑定
- 类似 Go 里 `config + env override` 的统一入口

#### `mini_agent/llm/`

这是**模型适配层**。

包含：

- `base.py`：抽象接口
- `llm_wrapper.py`：统一入口
- `anthropic_client.py`：Anthropic 风格消息协议
- `openai_client.py`：OpenAI 风格消息协议

它解决的问题是：

- 同一套内部消息格式，如何适配不同外部协议
- 同一套 Tool 对象，如何变成 Anthropic / OpenAI 需要的 schema
- 如何保留 `thinking` / `reasoning_details`
- 如何做重试

这部分非常值得你认真读，因为它直接对应你将来 Go 重写时的 `provider adapter` 设计。

#### `mini_agent/tools/`

这是**工具系统**。

包含：

- `base.py`：Tool 与 ToolResult 抽象
- `file_tools.py`：读写改文件
- `bash_tool.py`：前台/后台命令执行、输出轮询、终止
- `note_tool.py`：Session Note 记忆
- `skill_loader.py` / `skill_tool.py`：Skills 的 progressive disclosure
- `mcp_loader.py`：MCP 连接、发现、执行

它对应你熟悉的概念：

- Java 里的 `service adapter / capability provider`
- Go 里的 `interface + implementation registry`

#### `mini_agent/schema/`

这是**核心数据结构定义层**。

包含：

- `Message`
- `ToolCall`
- `FunctionCall`
- `LLMResponse`
- `TokenUsage`
- `LLMProvider`

这是整个系统的数据契约层。

你读代码时要始终记住一句话：

> Agent 系统本质上是在搬运和变换这些 schema。

#### `mini_agent/acp/`

这是**ACP（Agent Client Protocol）桥接层**。

作用是把这个本来跑在 CLI 里的 Agent，包装成能和编辑器/客户端交互的 stdio server。

你可以把它类比成：

- 一个面向编辑器侧的 RPC adapter
- 一层 “protocol facade”

这里重点不是功能复杂，而是：

- 它展示了 Agent 运行时如何被协议层包装
- 它是未来 Go 重写时最适合作为单独模块拆出去的边界之一

#### `mini_agent/utils/`

目前主要是终端显示工具，比如 ANSI 宽度处理。

这是辅助层，不是你第一优先级阅读对象。

#### `mini_agent/config/`

项目内置配置模板：

- `config-example.yaml`
- `mcp-example.json`
- `system_prompt.md`

这是运行所需的“模板资产”目录。

#### `mini_agent/skills/`

这是内置 Skill 资源库。

对学习 Mini-Agent 来说，不需要一开始逐个读完所有 skill。

你只需要先理解：

- skill 是什么
- 为什么要 metadata + on-demand full content
- loader 是如何发现 `SKILL.md`
- tool 是如何暴露 `get_skill`

---

## 4. 运行时主流程：你必须建立的心智模型

把 Mini-Agent 的主流程记成下面这张“脑内图”：

```text
用户输入
  -> CLI 接收
  -> 加载配置与 system prompt
  -> 初始化 provider
  -> 装配工具集合
  -> 创建 Agent
  -> Agent.run()
       -> 检查是否需要摘要压缩
       -> 调用 LLM.generate(messages, tools)
       -> 获得 assistant 内容 / thinking / tool_calls
       -> 如果无 tool_calls：结束
       -> 如果有 tool_calls：
            -> 逐个执行 tool.execute(**arguments)
            -> 把 tool result 作为 role=tool 消息回填
            -> 进入下一轮
  -> 输出最终结果
```

你后面所有阅读，都要围绕这条链路展开。

如果你能把这条链路讲清楚，你就已经真正入门这个项目了。

---

## 5. 你当前最欠缺的知识清单

下面我按照优先级来列，不是按学科分类。

---

### 5.1 P0：必须先补，不补会卡住读源码

#### A. Python 基础语法差异

你不是要系统学 Python，而是要补齐“读源码无阻碍”的程度。

必须掌握：

- `Pathlib`
- `with open(...)`
- 列表推导式
- `dict / list / tuple`
- 类型注解
- `Optional`
- `dataclass` 和 `BaseModel` 的使用习惯
- 异常处理写法

目标标准：

- 看到 Python 代码时，不会再先翻译成 Java/Go 才能理解

#### B. asyncio 基础

这是你最需要补的技术点。

必须掌握：

- `async def`
- `await`
- `asyncio.create_task`
- `asyncio.wait_for`
- `asyncio.timeout`
- `asyncio.Event`
- 异步 subprocess
- 事件循环的基本工作方式

你要建立的对照：

- `goroutine` 不等于 `asyncio task`
- `context.Context` 的取消语义，对应这里的 `asyncio.Event` 和显式检查
- Go 的阻塞 I/O 和 Python 的协程挂起点不是同一种成本模型

#### C. Pydantic v2

必须掌握：

- `BaseModel`
- 字段定义
- 默认值
- 校验器
- `model_dump`

因为这个项目里：

- `ToolResult`
- `Message`
- `LLMResponse`

都依赖 schema 化数据对象。

#### D. Python 项目工具链

你需要会：

- `uv sync`
- `uv run python -m ...`
- `uv run pytest`
- `pip install -e .` 和 `uv tool install -e .` 的区别

---

### 5.2 P1：一周内补齐，决定你能不能真正理解 Agent runtime

#### E. Tool Calling 协议差异

你必须理解：

- Anthropic 格式下 assistant / tool_use / tool_result 如何表示
- OpenAI 格式下 assistant.tool_calls / tool role / reasoning_details 如何表示
- 为什么内部统一 schema 很重要

这是 `llm/` 目录的核心。

#### F. Token / 上下文压缩机制

你已经懂 RAG 的 chunking，但这里要补：

- 会话历史是如何估算 token 的
- 为什么摘要触发条件既看本地估算又看 API usage
- 为什么压缩的是“轮次执行过程”而不是直接裁掉旧消息

#### G. Agent 消息状态机

你必须明确：

- `system`
- `user`
- `assistant`
- `tool`

四种消息在每轮中的顺序与作用。

否则你后面看 provider adapter 会很乱。

---

### 5.3 P2：第二阶段补，不会立刻卡住，但决定你能不能做重构和重写

#### H. MCP 协议基础

你不用一开始就去啃全部 MCP 规范，但至少要知道：

- MCP server 是什么
- stdio / sse / streamable_http 是什么
- tool discovery 和 tool execution 怎么发生
- 为什么它像“外部工具注册中心”

#### I. ACP 协议基础

你要知道：

- ACP 不等于 MCP
- ACP 更像“Agent runtime 对客户端的会话协议”
- 这里 ACP 的核心不是复杂业务，而是“如何把本地 Agent 包成协议服务”

#### J. Python 进程与终端交互

重点理解 `bash_tool.py`：

- 前台命令
- 后台命令
- 输出轮询
- kill
- 监控任务

这和你未来用 Go 重写 subprocess runner 强相关。

---

## 6. 如何启动当前项目

下面是最稳的路径。

---

### 6.1 环境准备

建议环境：

- macOS / Linux / WSL
- Python 3.10+
- `uv`

安装 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.zshrc
```

---

### 6.2 安装依赖

在项目根目录执行：

```bash
cd /Users/repeater/Documents/Code/work/Mini-Agent
uv sync
```

如果你只是想跑命令而不想手动激活虚拟环境，也可以直接用：

```bash
uv run python -m mini_agent.cli --help
```

---

### 6.3 配置 API Key

复制配置模板：

```bash
cp mini_agent/config/config-example.yaml mini_agent/config/config.yaml
```

然后编辑：

```yaml
api_key: "你的 MiniMax API Key"
api_base: "https://api.minimaxi.com"   # 国内
# api_base: "https://api.minimax.io"   # 海外
model: "MiniMax-M2.5"
provider: "anthropic"                  # 建议先用 anthropic 适配路径
max_steps: 100
workspace_dir: "./workspace"
```

说明：

- 当前配置文件是**扁平结构**，不是 Spring Boot 那种多层嵌套。
- `Config.from_yaml` 会在代码里把这些字段转成结构化对象。

---

### 6.4 最小启动方式

#### 方式 1：直接运行 CLI

```bash
uv run python -m mini_agent.cli
```

#### 方式 2：指定工作目录

```bash
uv run python -m mini_agent.cli --workspace /Users/repeater/Documents/Code/work/Mini-Agent/workspace
```

#### 方式 3：单任务模式

```bash
uv run python -m mini_agent.cli --task "列出当前目录下的文件"
```

#### 方式 4：安装为本地工具

```bash
uv tool install -e .
mini-agent --help
```

---

### 6.5 先跑哪些示例

推荐顺序：

```bash
uv run python examples/01_basic_tools.py
uv run python examples/02_simple_agent.py
uv run python examples/03_session_notes.py
uv run python examples/04_full_agent.py
```

其中：

- `01_basic_tools.py` 不依赖 LLM，是最好的热身
- `02/03/04` 依赖 API Key

---

### 6.6 先跑哪些测试

#### 不依赖真实 API 的本地测试

```bash
uv run pytest -q \
  tests/test_tools.py \
  tests/test_bash_tool.py \
  tests/test_note_tool.py \
  tests/test_skill_loader.py \
  tests/test_skill_tool.py \
  tests/test_markdown_links.py \
  tests/test_terminal_utils.py \
  tests/test_tool_schema.py \
  tests/test_session_integration.py \
  tests/test_acp.py
```

我本地实际跑过，结果是：

- `78 passed`
- `1 failed`

失败的是 `tests/test_acp.py::test_acp_invalid_session`，原因是 ACP 缺失 session 时自动构造 `NewSessionRequest(cwd=None)` 不符合当前 schema。

这个失败对你反而是好事，因为它说明：

- 这个项目不是“神圣不可碰”的代码
- 它确实是 demo
- 你可以把它当成“可学习、可修复、可演进的骨架”

#### 依赖真实 API 的测试

```bash
uv run pytest -q tests/test_llm.py tests/test_llm_clients.py
uv run pytest -q tests/test_agent.py tests/test_integration.py
```

注意：

- 这些测试会真实调用模型
- 可能耗时、花 token、受网络影响

---

## 7. 如何调试当前项目

---

### 7.1 你应该如何分层调试

调试顺序建议固定成下面 5 层，不要乱跳：

1. 配置层
2. 工具层
3. LLM 适配层
4. Agent 主循环
5. ACP/MCP 协议层

这个顺序的原因很简单：

- 先排除“系统是否能启动”
- 再排除“工具是否工作”
- 再排除“模型消息格式是否对”
- 最后才碰协议与外部集成

---

### 7.2 你最该下断点的地方

#### 第一组：启动装配链路

- `mini_agent/cli.py` 的 `run_agent`
- `mini_agent/cli.py` 的 `initialize_base_tools`
- `mini_agent/cli.py` 的 `add_workspace_tools`

你要观察：

- config 是怎么被解析的
- tools 最终有哪些
- system prompt 是怎么被拼出来的

#### 第二组：Agent 主循环

- `mini_agent/agent.py` 的 `run`
- `mini_agent/agent.py` 的 `_summarize_messages`
- `mini_agent/agent.py` 的 `_create_summary`

你要观察：

- 每一轮开始时 messages 长什么样
- LLM 返回后 response 长什么样
- tool result 是怎么回填的
- 结束条件是什么

#### 第三组：LLM 适配层

- `mini_agent/llm/llm_wrapper.py` 的 `__init__`
- `mini_agent/llm/anthropic_client.py` 的 `_convert_messages`
- `mini_agent/llm/openai_client.py` 的 `_convert_messages`
- 两个 client 的 `_parse_response`

你要观察：

- 内部消息结构如何变成外部 API 请求
- thinking / tool_call 如何被保留
- OpenAI 和 Anthropic 的消息差异

#### 第四组：工具层

- `mini_agent/tools/file_tools.py` 的三个 `execute`
- `mini_agent/tools/bash_tool.py` 的 `execute`
- `mini_agent/tools/mcp_loader.py` 的 `load_mcp_tools_async`
- `mini_agent/tools/skill_loader.py` 的 `discover_skills`

你要观察：

- relative path 是如何 resolve 到 workspace 的
- 后台进程如何管理
- MCP server 如何连接和发现 tool
- skill 是如何从 `SKILL.md` 进入 prompt 的

#### 第五组：协议层

- `mini_agent/acp/__init__.py` 的 `newSession`
- `mini_agent/acp/__init__.py` 的 `prompt`
- `mini_agent/acp/__init__.py` 的 `_run_turn`

你要观察：

- ACP 其实只是把 Agent runtime 再包一层
- 协议层不应该重复业务逻辑
- 这里有哪些实现还比较 demo

---

### 7.3 最推荐的调试方法

#### 方法 A：最小工具调试

先跑：

```bash
uv run python examples/01_basic_tools.py
```

目标：

- 不经过 LLM，先确认文件工具和 bash 工具的行为

#### 方法 B：带断点跑简单 Agent

跑：

```bash
uv run python examples/02_simple_agent.py
```

断点打在：

- `Agent.run`
- `LLMClient.generate`
- `tool.execute`

目标：

- 看懂一轮最小的 “ask -> think -> tool -> observe -> answer”

#### 方法 C：看日志而不是只看终端

项目会把每次运行记录到：

```text
~/.mini-agent/log/
```

CLI 里还有：

```bash
mini-agent log
mini-agent log agent_run_xxx.log
```

这些日志记录了：

- LLM request
- LLM response
- tool execution result

这对你非常重要，因为你做过复杂系统，应该习惯“读日志重建状态机”，而不是只看终端输出。

---

### 7.4 你应该刻意做的 6 个调试实验

#### 实验 1：只开文件工具，不开 bash

目标：

- 观察 agent 如何在能力受限时调整行为

#### 实验 2：让模型调用不存在的工具

目标：

- 看 `Unknown tool` 的错误路径

#### 实验 3：让 bash 超时

目标：

- 看 timeout 和错误回填路径

#### 实验 4：人为把 `max_steps` 调小

目标：

- 看循环终止条件

#### 实验 5：把 token limit 调低

目标：

- 强制进入 `_summarize_messages`

#### 实验 6：故意让 mcp 配置失效

目标：

- 看 MCP fallback 和失败处理路径

---

## 8. 阅读代码的正确顺序

这是我建议你的**唯一主线顺序**。

---

### 阶段 A：先建立数据模型认知

先读：

- `mini_agent/schema/schema.py`
- `mini_agent/tools/base.py`

你要回答的问题：

1. 一条消息有哪些字段？
2. 一次工具调用长什么样？
3. LLM 返回值包含什么？
4. Tool 的最小抽象接口是什么？

如果这四个问题答不出来，不要继续读 `agent.py`。

---

### 阶段 B：再看工具系统

顺序：

1. `file_tools.py`
2. `bash_tool.py`
3. `note_tool.py`
4. `skill_loader.py` + `skill_tool.py`
5. `mcp_loader.py`

你要回答的问题：

1. Tool 之间有没有统一抽象？
2. 工具参数怎么暴露给模型？
3. 执行结果怎么规范化？
4. 哪些工具是本地实现，哪些工具是“远程代理”？

---

### 阶段 C：再看 LLM 适配层

顺序：

1. `llm/base.py`
2. `llm/llm_wrapper.py`
3. `llm/anthropic_client.py`
4. `llm/openai_client.py`

你要回答的问题：

1. 为什么内部要统一 schema？
2. Anthropic / OpenAI 的工具调用差异是什么？
3. thinking 是如何保留的？
4. 为什么 MiniMax 的 base URL 要根据 provider 自动拼 `/anthropic` 或 `/v1`？

---

### 阶段 D：最后看 Agent 主循环

读：

- `mini_agent/agent.py`

你要回答的问题：

1. 每一轮开始时做了什么？
2. 什么时候算任务完成？
3. 为什么 tool result 也要变成消息？
4. 摘要压缩为什么采用“轮次压缩”而不是简单截断？
5. cancellation 是怎么处理的？

---

### 阶段 E：再回头看 CLI 和 ACP

读：

- `mini_agent/cli.py`
- `mini_agent/acp/__init__.py`

你要回答的问题：

1. CLI 只是启动器，还是也包含运行时逻辑？
2. ACP 是不是重复实现了一遍 Agent？
3. 哪些代码未来 Go 重写时应该抽成独立包？

---

## 9. 你真正要学会的，不是代码，而是 8 个核心问题

学完本项目之前，你必须能口头回答以下 8 个问题：

1. Mini-Agent 的一次任务，从 CLI 到最终回答，中间经过了哪些层？
2. Message / ToolCall / LLMResponse 三个 schema 为什么是核心？
3. 为什么工具结果必须回填成 `role=tool` 消息？
4. Anthropic / OpenAI 两套协议的差异点是什么？
5. Skill 与 MCP 的角色差别是什么？
6. Session Note 和真正的长期记忆系统有什么差距？
7. 当前项目离 production 还差哪些关键能力？
8. 如果用 Go 重写，最先要稳定的边界是什么？

如果这 8 个问题都能讲清楚，你不是“看过这个项目”，而是真正“学会这个项目”。

---

## 10. 面向你的背景，给出一份非常详细的学习路线图

我按 **8 个阶段、约 5~7 周** 来设计。你也可以压缩成 3~4 周高强度版。

---

## 阶段 0：环境与认知对齐（0.5 ~ 1 天）

### 目标

- 能跑起项目
- 知道这是 Python Agent demo，不是完整生产系统
- 建立仓库地图

### 任务

1. 安装 `uv`
2. 执行 `uv sync`
3. 复制并填写 `config.yaml`
4. 运行 `uv run python -m mini_agent.cli --help`
5. 浏览：
   - `README_CN.md`
   - `pyproject.toml`
   - `examples/README_CN.md`
   - `docs/DEVELOPMENT_GUIDE_CN.md`

### 输出物

- 一张你自己的仓库脑图
- 一份你自己的“我预计哪里最难”的问题列表

### 验收标准

- 你能说出项目入口命令
- 你能说出 5 个核心目录分别干什么

---

## 阶段 1：Python 适配训练（2 ~ 3 天）

### 目标

- 达到“读 Python 代码不阻塞”的程度

### 学习重点

#### Day 1

- `Pathlib`
- 文件读写
- 异常处理
- 类型注解
- `dict/list` 常见操作

练习：

- 用 Python 写一个读取文件、加行号输出的函数
- 用 Python 写一个简单 JSON 配置加载器

#### Day 2

- `async/await`
- `asyncio.Event`
- `asyncio.wait_for`
- 异步 subprocess

练习：

- 写一个异步函数并发执行两个 shell 命令
- 加入 timeout
- 加入 cancel

#### Day 3

- Pydantic BaseModel
- `model_dump`
- 字段默认值
- 简单 validator

练习：

- 自己定义 `Message`
- 自己定义 `ToolResult`
- 自己定义一个 `SearchToolArgs`

### 输出物

- 一个你自己写的小型 Python playground 目录

### 验收标准

- 看 `file_tools.py` 不费劲
- 看 `bash_tool.py` 能大致读懂

---

## 阶段 2：只学工具层，不碰 Agent（2 天）

### 目标

- 不依赖模型，先彻底吃透工具系统

### 阅读顺序

1. `mini_agent/tools/base.py`        ✅
2. `mini_agent/tools/file_tools.py`    ✅
3. `mini_agent/tools/bash_tool.py`  ✅ --- 但需要重新复习
4. `mini_agent/tools/note_tool.py`   ✅

### 实操任务

1. 跑 `examples/01_basic_tools.py`   ✅
2. 跑 `tests/test_tools.py`     ✅
3. 跑 `tests/test_bash_tool.py`    Todo
4. 手动调用工具，构造自己的输入

### 你必须记录的观察点

- `ReadTool` 为什么输出带行号
- `EditTool` 为什么要求 exact string replace
- `BashTool` 前台和后台两种模式如何统一返回
- `SessionNoteTool` 为什么只是文件级 JSON 存储

### 小结问题

1. Tool 抽象是否足够通用？
2. Tool schema 是否适合未来 Go 重写？
3. 哪些工具适合继续保留，哪些适合重写时替换？

### 阶段输出

- 一份 `Tool 系统摘要.md`

---

## 阶段 3：只学 LLM 适配层，不碰 Agent 主循环（2 ~ 3 天）

### 目标

- 理解“内部统一 schema -> 外部协议”的转换

### 阅读顺序

1. `mini_agent/schema/schema.py`    ✅
2. `mini_agent/llm/base.py`      ✅
3. `mini_agent/llm/llm_wrapper.py`   ✅
4. `mini_agent/llm/anthropic_client.py`  ✅
5. `mini_agent/llm/openai_client.py`  ✅

### 实操任务

1. 跑 `tests/test_llm.py`
2. 跑 `tests/test_llm_clients.py`
3. 打印请求前后的消息结构
4. 画一张消息转换对照表

### 你必须搞清楚的重点

#### Anthropic 路径

- assistant 消息可以包含：
  - `thinking`
  - `text`
  - `tool_use`
- tool result 需要包装成 user role + `tool_result`

#### OpenAI 路径

- system message 在 messages 数组内
- tool call 的 arguments 是 JSON string
- `reasoning_details` 需要回传以保留思维链条

### 阶段输出

- 一张 “Internal Message -> Anthropic/OpenAI Message 映射表”

### 验收标准

- 你可以不看代码，自己手写出一次 tool call 前后的消息格式

---

## 阶段 4：攻克 Agent 主循环（3 天）

### 目标

- 彻底看懂系统的心脏

### 阅读对象

- `mini_agent/agent.py` ✅

### 建议拆成 3 次阅读

#### 第一次：只看骨架

只看：

- `__init__`
- `add_user_message`
- `run`

目标：

- 看清每轮的主流程

#### 第二次：只看上下文管理

只看：

- `_estimate_tokens`
- `_summarize_messages`
- `_create_summary`

目标：

- 理解上下文压缩的具体策略

#### 第三次：只看中断与一致性

只看：

- `_check_cancelled`
- `_cleanup_incomplete_messages`
- `run` 中的取消检查点

目标：

- 理解为什么取消不是“立即打断一切”，而是“在安全点终止”

### 实操任务

1. 运行 `examples/02_simple_agent.py`
2. 给 `run` 打断点
3. 每一轮都把 `messages` 打印出来
4. 人工标注每条消息的 role

### 必须回答的问题

1. `max_steps` 为什么要存在？
2. 为什么 tool result 必须入历史？
3. 为什么压缩后 summary message 被塞成 `role=user`？
4. 这个设计在 production 中是否合理？

### 阶段输出

- 一份 “Agent 主循环逐轮讲解”

---

## 阶段 5：补齐 Skills / MCP / ACP 三个扩展边界（3 ~ 4 天）

### 目标

- 理解“本地工具”“技能提示”“远程协议工具”“编辑器协议桥接”的差别

---

### 5.1 Skills（1 天）

阅读：

- `mini_agent/tools/skill_loader.py`
- `mini_agent/tools/skill_tool.py`
- `mini_agent/skills/README.md`

理解重点：

- Skill 不是代码插件本身，而是“提示 + 资源 + 脚本”的能力包
- progressive disclosure 是为了控制 prompt 体积
- 为什么一开始只注入 metadata

你只要抽样读 1~2 个 skill 即可，不要一开始全读完。

推荐抽样：

- `webapp-testing`
- `document-skills/pdf`

---

### 5.2 MCP（1 ~ 2 天）

阅读：

- `mini_agent/tools/mcp_loader.py`
- `mini_agent/config/mcp-example.json`
- `tests/test_mcp.py`

理解重点：

- MCP server 如何定义
- stdio / URL 连接如何建立
- tool list 如何发现
- tool execute 如何代理
- timeout 为什么是必需配置

你可以把它类比成：

- “一个外部服务暴露了一组可供 Agent 调用的能力”

---

### 5.3 ACP（1 天）

阅读：

- `mini_agent/acp/__init__.py`
- `tests/test_acp.py`

理解重点：

- ACP 是把 Agent runtime 协议化
- `newSession / prompt / cancel` 分别对应什么
- ACP 当前实现里哪些地方是 demo 级别

必须注意：

- 当前 ACP 有一个已暴露的失败测试，说明协议适配层还不够稳

### 阶段输出

- 一份 “Skills vs MCP vs ACP 对照表”

---

## 阶段 6：从学习转入改造（3 ~ 5 天）

### 目标

- 通过做小改动，验证你真的掌握了项目

### 推荐的 4 个练手改造

#### 练手 1：给 CLI 补上 `RecallNoteTool`

当前默认 CLI 里 `add_workspace_tools` 只加载了 `SessionNoteTool`，没有加载 `RecallNoteTool`。

这很适合作为你的第一个小改造，因为它：

- 范围小
- 业务清楚
- 能暴露你是否理解 tool assembly

#### 练手 2：给 Agent 增加更清晰的 tool execution metrics

比如：

- 单次工具调用耗时
- 总工具调用数
- 按工具分类统计

这和你在 GuGoTik 的可观测性经验高度匹配。

#### 练手 3：给 Config 增加更严格的 provider 校验

例如：

- 非 `anthropic/openai` 时直接报错
- 配置项冲突时给出清晰提示

#### 练手 4：修 ACP invalid session 的 failing test

这是非常适合你的学习型修复，因为它逼你：

- 理解 ACP schema
- 理解 session 生命周期
- 理解测试和实现的关系

### 阶段输出

- 1~2 个小 PR 级别改造

---

## 阶段 7：面向 Go 重写的架构提炼（4 ~ 6 天）

### 目标

- 不急着重写代码，先提炼可迁移架构

### 你应该先抽出来的 8 个 Go 模块

#### 1. `schema`

定义：

- Message
- ToolCall
- ToolResult
- LLMResponse
- Provider enum

#### 2. `provider`

包含：

- Anthropic adapter
- OpenAI adapter
- 统一 Provider interface

#### 3. `tool`

包含：

- Tool interface
- Local tools
- Tool registry

#### 4. `agent`

包含：

- runtime loop
- message history
- cancellation
- max steps
- summary trigger

#### 5. `memory`

初版可以先做 file-based

后续可扩展：

- sqlite
- postgres
- redis

#### 6. `skills`

包含：

- skill discovery
- metadata loader
- on-demand full loader

#### 7. `mcp`

包含：

- server connection
- tool discovery
- execute proxy

#### 8. `cmd` / `cli`

包含：

- 入口
- config
- logging
- terminal UI

### 你要写的不是代码，而是设计文档

建议写一份：

`docs/go-rewrite-design.md`

内容包括：

- 包划分
- 接口定义
- 消息流
- 取消机制
- 并发模型
- 测试策略

### 阶段输出

- Go 重写架构设计文档
- Go 包结构草图

---

## 阶段 8：真正开始 Go 重写（建议至少 2~4 周）

### 核心原则

不要“一次性重写整个 Mini-Agent”，而要按里程碑走。

---

### Milestone 1：只做最小单机 Agent MVP

只实现：

- schema
- provider 抽象
- read/write/bash 3 个工具
- agent loop
- CLI 单任务模式

不要一上来做：

- MCP
- ACP
- Skills
- 复杂 UI
- 长期记忆

验收标准：

- 能跑一个“创建文件并验证”的任务

---

### Milestone 2：补齐 Session Memory 与日志

实现：

- file-based note store
- request/response/tool logs
- 更清晰的 tracing hooks

验收标准：

- 可记录跨轮关键事实
- 可通过日志完整重建一次运行过程

---

### Milestone 3：实现 provider parity

实现：

- Anthropic 风格消息适配
- OpenAI 风格消息适配
- retry / timeout / fallback

验收标准：

- 同一套内部消息结构，可切不同 provider

---

### Milestone 4：引入 MCP

实现：

- stdio transport
- tool discovery
- execute proxy

验收标准：

- 外部 MCP server 的工具能像本地工具一样被 Agent 调用

---

### Milestone 5：引入 ACP 或自己的 editor protocol

这个阶段再做，不要提前。

---

## 11. 如果时间充裕，最值得做的改进项

下面是按价值排序的增强路线。

---

### P0：最值得补的工程能力

#### 1. 工具权限与安全隔离

当前 bash 工具是高风险能力。

生产改进方向：

- allowlist / denylist
- cwd 限制
- 只读模式
- 沙箱进程
- 命令审计

#### 2. 真正的长期记忆

当前 Session Note 只是本地 JSON 文件。

升级方向：

- sqlite / postgres
- 标签化检索
- semantic recall
- 轮次级 memory compaction

#### 3. 更可靠的上下文压缩

当前压缩策略是 demo 风格。

可升级方向：

- 保留系统关键事件
- 结构化摘要
- 面向工具结果的压缩
- recall-aware summarization

#### 4. 可观测性

这是你最适合做的增强点之一。

建议：

- OpenTelemetry trace/span
- request_id / run_id
- tool latency histogram
- token usage metrics
- error type metrics

---

### P1：中期增强

#### 5. Provider fallback / model pool

例如：

- 主模型失败自动切副模型
- 根据任务类型切模型
- 根据 token / latency 做路由

#### 6. 流式输出

当前体验偏“一步一块”。

升级方向：

- streaming assistant content
- streaming thinking
- streaming tool execution status

#### 7. 更严密的配置系统

例如：

- env override
- profile
- secrets management
- schema validation

---

### P2：长期增强

#### 8. 多 Agent 协作

例如：

- Planner / Executor / Reviewer
- Agent as Tool
- Delegation protocol

#### 9. Web UI / Dashboard

例如：

- 会话历史
- tool timeline
- log viewer
- metrics panel

#### 10. 沙箱执行环境

例如：

- Docker-based execution
- Firecracker / nsjail
- workspace quota

---

## 12. 我建议你采用的实际学习节奏

如果你每天能投入 1.5~2 小时，建议如下：

### 轻量版（6~7 周）

- 第 1 周：环境 + Python 适配 + tools
- 第 2 周：LLM 适配 + Agent loop
- 第 3 周：Skills / MCP / ACP
- 第 4 周：测试 + 小改造
- 第 5 周：形成完整源码笔记
- 第 6 周：Go 重写设计
- 第 7 周：Go MVP 开工

### 高强度版（3~4 周）

- 第 1 周：Python + tools + LLM
- 第 2 周：Agent + CLI + tests + 协议层
- 第 3 周：小改造 + Go 设计
- 第 4 周：Go MVP

---

## 13. 每个阶段必须产出的文档

为了避免“看完就忘”，你每个阶段都要留下产物。

### 必做产物

1. `01-tool-system-notes.md`
2. `02-provider-adapter-notes.md`
3. `03-agent-runtime-notes.md`
4. `04-skills-mcp-acp-notes.md`
5. `05-mini-agent-overall-architecture.md`
6. `06-go-rewrite-design.md`

### 这些文档里必须写什么

- 模块职责
- 关键数据结构
- 主要调用链
- 你的问题
- 你认为的改进点

这会直接决定你后面重写时会不会“重构成一团”。

---

## 14. 给你的具体阅读任务清单

下面这组任务是我认为最适合你的“精读脚本”。

### Task 1

读完 `schema.py + base.py`，回答：

- Message 为什么要携带 thinking、tool_calls、tool_call_id？

### Task 2

读完 `file_tools.py`，回答：

- 为什么 read_file 要带行号输出？

### Task 3

读完 `bash_tool.py`，回答：

- 为什么后台命令不能只返回 PID，而要有 `bash_id + output monitor`？

### Task 4

读完 `llm_wrapper.py + anthropic_client.py + openai_client.py`，回答：

- 如果未来接 DeepSeek/OpenRouter/本地模型，抽象边界应该在哪一层？

### Task 5

读完 `agent.py`，回答：

- 这个 Agent 的“状态机”最小状态集合是什么？

### Task 6

读完 `cli.py`，回答：

- 哪些逻辑应该留在 CLI，哪些应该下沉到 runtime / app service？

### Task 7

读完 `mcp_loader.py`，回答：

- MCP tool 在系统里究竟是“远程工具”还是“动态插件”？

### Task 8

读完 `acp/__init__.py`，回答：

- ACP 层是否应该直接持有 Agent 实例？如果 Go 重写，你会怎么做？

---

## 15. 你应该特别关注的“设计不完美点”

这个项目很适合学习，不是因为它完美，而是因为它**足够真实地展示了 demo 到 production 的断层**。

下面这些点你要特别留意：

### 1. CLI 默认只加载 `record_note`，未加载 `recall_notes`

这说明：

- 文档能力宣称和默认装配不完全一致
- 这是很好的学习型改造点

### 2. 摘要压缩策略仍偏 demo

尤其：

- summary message 使用 `role=user`
- 压缩策略简单

这是个很好的重写与改进切入点。

### 3. ACP 测试存在已暴露失败

说明协议适配边界还不稳定。

### 4. 文档与代码结构并非完全同步

例如开发指南里还出现旧路径描述。

这恰好符合真实项目状态：你必须学会“以代码为准，以文档辅助”。

---

## 16. 你的最终学习目标，不应该只是“能跑”

你最终应该达到以下 4 个层级。

### Level 1：会运行

- 会配置
- 会启动
- 会跑 example

### Level 2：会解释

- 能讲清楚整个运行链路
- 能讲清楚 tools / provider / agent / mcp / acp 的边界

### Level 3：会修改

- 能做小功能
- 能修测试
- 能加日志与增强项

### Level 4：会重建

- 能独立写出 Go 版设计
- 能按里程碑重建 MVP
- 能明确哪些部分沿用，哪些部分升级

如果只做到 Level 1，这个项目对你的价值很有限。  
至少做到 Level 3，它才真正配得上你的背景。  
做到 Level 4，这个项目才会成为你下一阶段的跳板。

---

## 17. 我给你的最终建议

### 建议 1

不要把 Mini-Agent 当作“要背完的 Python 项目”，而要把它当作：

> 一个最小可读、最小可改、最小可重建的 Agent 骨架

### 建议 2

不要一开始就想“我能不能直接用 Go 重写”，而要先回答：

> 我到底理解了哪些行为契约？

### 建议 3

你最有价值的发挥点不是“照着抄 Python”，而是：

- 用你的 RAG 经验升级上下文与记忆
- 用你的微服务经验升级协议与模块边界
- 用你的 observability 经验升级可观测性
- 用你的 Go 工程能力重建更稳的 runtime

### 建议 4

学习过程中始终记住一句话：

> 这个项目真正值得你学的，不是某个 API 的写法，而是 Agent runtime 的最小闭环。

---

## 18. 下一步建议

如果你按这份路线继续，我建议你的下一个动作就是下面三选一：

### 选项 A：立即进入精读

从：

- `schema.py`
- `tools/base.py`
- `file_tools.py`

开始，我陪你逐段精读。

### 选项 B：先跑起来再读

从：

- `uv sync`
- 配置 `config.yaml`
- 跑 `examples/01_basic_tools.py`
- 跑 `examples/02_simple_agent.py`

开始，我带你做带断点调试。

### 选项 C：边读边改

先做一个小增强：

- 给 CLI 补 `RecallNoteTool`

这样你会最快进入“理解 + 动手”的状态。

---

署名：**Codex（GPT-5）**
