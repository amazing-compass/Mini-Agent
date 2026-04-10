# DeerFlow、Kode-Agent 与本地 Mini-Agent 横向对比报告

## 文档信息

- 撰写时间：2026-04-01
- 本地对照对象：`/Users/repeater/Documents/Code/study/Mini-Agent`
- 公开仓库对照对象：
  - [bytedance/deer-flow](https://github.com/bytedance/deer-flow)
  - [shareAI-lab/Kode-Agent](https://github.com/shareAI-lab/Kode-Agent)
  - [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)（仅用于补充上游公开指标；代码判断仍以本地工作区为准）
- 对比目标：
  - 判断三者分别处在 agent 体系的哪一层
  - 区分“平台型 harness”、“终端型 coding agent”、“可读型单 agent runtime”
  - 给出本地 `mini-agent` 与另外两者相比的真实差距、优势和演进建议

---
## 一句话结论

- **DeerFlow** 最像“平台级 super agent harness”。
- **Kode-Agent** 最像“终端优先的产品型 coding agent workbench”。
- **本地 Mini-Agent** 最像“高可读、单 agent、适合学习和二次改造的轻量 runtime”。

如果只问一句“谁更成熟”：

- 以**平台化、长周期任务编排、沙箱隔离**看，`DeerFlow` 最成熟。
- 以**终端交互、coding workflow、multi-model workbench**看，`Kode-Agent` 最成熟。
- 以**代码可读性、学习成本、自己继续改造的起点**看，本地 `Mini-Agent` 反而是最友好的。

所以三者不是一条直线上的“大中小版本”，而是三个不同方向的成熟度。

---

## 结论摘要

### 1. 定位上，三者不是同赛道

- `DeerFlow` 不是单纯的“coding agent CLI”，而是带 `LangGraph + Gateway API + Frontend + Sandbox + Memory + Subagents` 的完整 agent 平台。
- `Kode-Agent` 不是研究型 orchestration 框架，而是更接近“终端里的 AI 开发工作台”，强调 REPL、模型切换、权限交互、子代理、技能、AGENTS.md 标准兼容。
- 本地 `Mini-Agent` 不是产品级平台，也不是重型终端 workbench，而是一个把单 agent 核心闭环做完整的 Python runtime：工具循环、上下文摘要、session note、skills、MCP、CLI、ACP 都具备，但依然保持较低复杂度。

### 2. DeerFlow 和 Kode-Agent 的“成熟”，成熟在不同地方

- `DeerFlow` 的成熟，主要体现在**系统分层、服务化边界、长任务 orchestration、隔离执行环境、平台治理能力**。
- `Kode-Agent` 的成熟，主要体现在**终端产品体验、权限与交互设计、多模型协作、插件/skills/agents 体系、工程打磨程度**。

### 3. 本地 Mini-Agent 的短板很明确，但它不是“弱化版失败品”

- 它确实没有 `DeerFlow` 那种**多进程/多服务/多代理 orchestration**。
- 它也没有 `Kode-Agent` 那种**重交互、多模型、多权限模式、终端产品化 UI**。
- 但它有一个非常重要的优点：**代码体量和抽象层级刚好处在“能看懂、能改、能自己继续长”的区间**。

### 4. 如果你的目标是继续把本地 Mini-Agent 做强

- 应该从 `DeerFlow` 学**运行时架构和平台能力**。
- 应该从 `Kode-Agent` 学**终端工作流、模型策略和权限交互**。
- 但不应该直接把任一项目原样照搬进 `Mini-Agent`，否则很容易把当前仓库的可读性优势彻底丢掉。

---

## 方法与证据边界

本报告基于以下材料形成判断：

- 公开仓库 README、架构文档、关键实现文件
- GitHub 仓库元信息：stars、forks、贡献者数量、创建/更新时间、语言分布
- 本地 `Mini-Agent` 当前工作区中的实际代码，而不是只看其上游 README

本报告**没有**做的事：

- 没有完整运行 `DeerFlow` 和 `Kode-Agent` 的端到端 demo
- 没有基于一次短期试用就对“模型效果”下结论
- 没有把 README 里的功能宣传全部等同于“经过生产验证的能力”

因此，下面的判断重点放在：

- 架构是否成体系
- 抽象是否稳定
- 代码中是否已经有对应的实现落点
- 工程信号是否支持“成熟”这个结论

---

## 快照总览

### 1. 公开仓库指标快照

| 项目 | 公开定位 | 主要语言 | License | Stars | Forks | Contributors | 创建时间 | 最近更新 |
|---|---|---:|---|---:|---:|---:|---|---|
| [DeerFlow](https://github.com/bytedance/deer-flow) | Super agent harness | Python + TypeScript | MIT | 55,814 | 6,807 | 100 | 2025-05-07 | 2026-04-01 |
| [Kode-Agent](https://github.com/shareAI-lab/Kode-Agent) | AI terminal coding workbench | TypeScript | Apache-2.0 | 4,834 | 728 | 11 | 2025-07-12 | 2026-04-01 |
| [Mini-Agent 上游](https://github.com/MiniMax-AI/Mini-Agent) | Single-agent demo/runtime | Python | MIT | 2,171 | 318 | 12 | 2025-10-31 | 2026-04-01 |

### 2. 当前代码快照的粗略工程信号

| 项目 | 运行形态 | 测试文件数（粗略） | docs 文件数（粗略） | 备注 |
|---|---|---:|---:|---|
| DeerFlow | Web/API/Agent Platform | 82 | 26 | 平台拆分明显，后端文档密集 |
| Kode-Agent | Terminal App + ACP/MCP entrypoints | 136 | 36 | 终端产品与开发文档都比较完整 |
| 本地 Mini-Agent | CLI + ACP server | 16 | 10 | 测试和文档规模明显更小，但核心闭环完整 |

说明：

- 公开指标反映的是**社区与维护信号**，不直接等于代码质量。
- 测试和 docs 文件数只是**粗粒度工程密度信号**，不直接等于覆盖率。

---

## 三个项目的本质画像

## 1. DeerFlow：平台级 super agent harness

从 README 和后端架构文档看，`DeerFlow` 的目标不是做一个简洁 CLI，而是构造一个**长时任务、多能力编排、可部署的 agent 平台**。

它的核心画像是：

- 以 `LangGraph` 为 agent runtime
- 用 `FastAPI Gateway` 暴露 models、skills、MCP、memory、uploads、artifacts 等管理面
- 提供 `Next.js` 前端和统一反向代理入口
- 用 middleware 串接 thread data、uploads、sandbox、summarization、todo、title、memory、vision、clarification 等横切关注点
- 原生支持 subagents、memory、sandbox、skills、MCP、embedded client

这意味着 `DeerFlow` 的“成熟”首先是**系统工程成熟**，而不是“terminal 里操作顺手”。

### DeerFlow 最值得重视的实现信号

- `backend/README.md` 明确给出了 `Nginx -> LangGraph Server -> Gateway API -> Frontend` 的平台结构。
- `backend/docs/ARCHITECTURE.md` 明确划分了运行时、网关、配置、前端四层。
- `backend/packages/harness/deerflow/agents/lead_agent/agent.py` 显示它把 summarization、todo、memory、vision、subagent limit、clarification 作为显式 middleware 链处理。
- `backend/packages/harness/deerflow/subagents/executor.py` 不是“伪多 agent prompt 技巧”，而是有真实的 executor、状态、线程池、结果管理。
- `backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py` 说明它有结构化 memory 更新队列，而不是简单把历史拼接回 prompt。

### DeerFlow 的核心判断

- 它最强的不是“写代码体验”，而是**把 agent 从单轮工具循环提升到平台化运行时**。
- 它最适合的不是“拿来读懂 agent 最小闭环”，而是“研究复杂 agent 系统如何分层”。

---

## 2. Kode-Agent：终端优先的产品型 coding agent workbench

`Kode-Agent` 的 README 和 docs 传达的重点非常清楚：它是一个**终端内的 AI 开发工作台**。

它的核心画像是：

- 终端 REPL/Ink UI 是一等公民
- 子代理、技能、模型切换、@mention、AGENTS.md、命令系统、权限系统、MCP、ACP 都围绕 terminal workflow 组织
- 不是重平台服务编排，而是重“人类开发者在 terminal 里怎么和 agent 合作”

它的架构文档把系统分成：

- UI layer
- Command & control layer
- Tool execution layer
- Service integration layer
- Infrastructure layer

这个分层比 `Mini-Agent` 明显更产品化，但又不像 `DeerFlow` 那样走 web platform 路线。

### Kode-Agent 最值得重视的实现信号

- `docs/develop/architecture.md` 明确显示其核心是 CLI/REPL + Query Engine + Tool Registry + Providers/MCP。
- `src/app/query.ts` 说明它的核心循环不是简单串行 tool call，而是有 tool queue、并发安全判断、progress message、hook system、auto compact。
- `src/tools/agent/TaskTool/TaskTool.tsx` 显示它的 subagent 是一等能力，并支持 `subagent_type`、`resume`、`run_in_background`。
- `src/tools/ai/AskExpertModelTool/AskExpertModelTool.tsx` 显示它不是单模型终端，而是明确支持“咨询其他模型”的工作模式。
- `src/utils/config/projectInstructions.ts` 显示其 `AGENTS.md / AGENTS.override.md / CLAUDE.md` 发现逻辑已经做得很系统。
- `src/utils/session/autoCompactCore.ts` 显示它对上下文压缩已经做成独立机制，并引入 `compact` model pointer。
- `src/services/mcp/tools-integration.ts` 说明它对 MCP 的接入不是点缀，而是动态集成进工具系统。

### Kode-Agent 的核心判断

- 它的成熟度，首先表现在**terminal coding UX 和多模型工作流**。
- 它并不追求 `DeerFlow` 那种平台编排强度，但在“人在终端里协作开发”这个场景上非常完整。

---

## 3. 本地 Mini-Agent：高可读的单 agent runtime

本地 `Mini-Agent` 的定位，在 README 和代码里都相对一致：

- 单 agent
- Python CLI
- 明确的工具调用循环
- session memory
- 上下文摘要
- Claude skills
- MCP
- ACP server

它不像 `DeerFlow` 那样试图构造完整平台，也不像 `Kode-Agent` 那样把终端产品体验做得很重。

它的核心价值在于：

- 代码结构够小
- 核心闭环够完整
- 扩展点够真实
- 但复杂度还没有失控

### Mini-Agent 最值得重视的实现信号

- `mini_agent/agent.py` 里已经有完整的 step loop、tool call 执行、取消、中间结果记录和 token-aware summarization。
- `mini_agent/tools/note_tool.py` 提供最直接、可理解的跨 session note 记忆。
- `mini_agent/tools/skill_loader.py` 和 `mini_agent/tools/skill_tool.py` 实现了很适合教学的 progressive disclosure 技能加载。
- `mini_agent/tools/mcp_loader.py` 提供真实 MCP 客户端接入和超时控制。
- `mini_agent/cli.py` 把 retry、skills metadata 注入、MCP 初始化、workspace tools、interactive CLI、Esc cancel 等串到一起。

### Mini-Agent 的核心判断

- 它最强的地方不是“规模最大”，而是**在足够实用的同时仍然容易完全读懂**。
- 如果目标是自己继续做 agent runtime，`Mini-Agent` 比另外两者更像一个可操作的起点。

---

## 详细横向对比

## 1. 产品定位与目标用户

| 维度 | DeerFlow | Kode-Agent | 本地 Mini-Agent |
|---|---|---|---|
| 主要定位 | 平台级 super agent harness | 终端型 coding agent workbench | 轻量单 agent runtime |
| 主要用户 | 想搭平台、长任务编排、Web/API 工作流的人 | 终端重度开发者、希望一个 CLI 代理长期常驻的人 | 想理解 agent 基本闭环、自己继续迭代 Python runtime 的人 |
| 设计重心 | orchestration、service boundary、sandbox、memory、subagent | terminal UX、multi-model、permissions、agents/skills/plugin | readability、single-agent loop、实用增强 |
| 更像什么 | agent platform | developer product | educational-but-real runtime |

**判断**：

- 如果你把三者放在同一维度比较，很容易得出错误结论。
- 正确做法是先看“它打算解决什么问题”，再比较成熟度。

---

## 2. 系统架构与运行形态

### DeerFlow

`DeerFlow` 的架构是三者里最重的：

- `Nginx` 统一入口
- `LangGraph Server` 承载 agent runtime
- `Gateway API` 提供 models/skills/MCP/memory/uploads/artifacts 管理
- `Frontend` 作为对话界面

这说明它本质上是一个**服务化 agent 平台**。

### Kode-Agent

`Kode-Agent` 的架构是三者里最像“产品 CLI”的：

- CLI entrypoint
- REPL / React-Ink UI
- Query Engine
- Tool system
- Provider/MCP integration
- ACP/MCP 入口

它不是多服务平台，而是**终端工作台**。

### Mini-Agent

本地 `Mini-Agent` 是最典型的**单进程 Python CLI runtime**：

- `cli.py` 负责启动、配置、工具装配、交互循环
- `agent.py` 负责 agent loop
- tools/skills/MCP 是模块化扩展

它的形态更轻，也因此部署和理解成本最低。

**这一维度的结论**：

- 平台化：`DeerFlow` 最强
- 终端工作台：`Kode-Agent` 最强
- 轻量单体：`Mini-Agent` 最清晰

---

## 3. Agent 执行模型

### DeerFlow

`DeerFlow` 的执行模型最复杂：

- lead agent
- middleware chain
- subagent delegation
- per-thread state
- sandbox/thread-data 生命周期
- SSE streaming

它更像“可编排 runtime”而非单一 ReAct loop。

### Kode-Agent

`Kode-Agent` 的执行模型比 `Mini-Agent` 复杂，但比 `DeerFlow` 更贴近单会话终端协作：

- 主 query loop
- Tool queue
- 并发安全判断
- TaskTool 子任务代理
- AskExpertModel 外部模型咨询
- auto compact
- hook system

它是“终端工作流增强型 agent loop”。

### Mini-Agent

本地 `Mini-Agent` 的执行模型是最标准、最易读的：

- 用户消息进入历史
- 调 LLM
- 解析 tool calls
- 逐个执行工具
- 将结果回填消息历史
- 达到终止条件或最大步数结束

没有显式多 agent，也没有复杂的运行时编排层。

**这一维度的结论**：

- 长时任务编排能力：`DeerFlow` 明显领先
- 单会话复杂 coding workflow：`Kode-Agent` 更强
- 学习和改造门槛：`Mini-Agent` 明显更低

---

## 4. 上下文压缩与长期记忆

### DeerFlow

`DeerFlow` 在这方面是三者里最完整的：

- session 内有 summarization middleware
- state 里保留 todo、title、artifacts、sandbox、thread_data 等运行信息
- conversation 结束后进入 memory queue
- memory 不是简单全文回灌，而是带过滤、去重、结构化更新

这是**真正的“上下文管理 + 跨会话记忆”体系**。

### Kode-Agent

`Kode-Agent` 更偏向：

- 项目指令发现与拼接
- 会话上下文自动压缩
- `compact` model pointer
- 文件恢复
- 会话与 transcript 管理

它在“上下文维护”上很成熟，但重点不在用户画像式长期 memory。

更准确地说：

- 它强在**会话延续与项目约束注入**
- 不像 `DeerFlow` 那样显式强调“长期用户记忆系统”

### Mini-Agent

本地 `Mini-Agent` 的这部分设计很朴素，但很实用：

- `SessionNoteTool` / `RecallNoteTool` 提供最直接的跨 session 记忆
- `Agent._summarize_messages()` 负责 token 超限后的历史摘要

这套东西不如 `DeerFlow` 成体系，也不如 `Kode-Agent` 有多模型 compact 策略，但已经构成单 agent runtime 的完整闭环。

**这一维度的结论**：

- 长期记忆体系：`DeerFlow` 最强
- 会话级上下文压缩与延续：`Kode-Agent` 更高级
- 简洁有效的单 agent 记忆：`Mini-Agent` 最易理解

---

## 5. 工具、技能与扩展体系

### DeerFlow

`DeerFlow` 的扩展体系是平台化的：

- 内置工具
- sandbox tools
- community tools
- MCP tools
- skills
- Gateway 层的 skill 安装与管理

它把扩展能力当成“平台资产”来做。

### Kode-Agent

`Kode-Agent` 的扩展体系则是终端产品化的：

- 本地工具系统
- MCP 动态集成
- skill tool
- plugin marketplace
- custom commands
- subagents
- AGENTS.md / CLAUDE.md / skills 的兼容体系

它最突出的差异点是：**把“项目约束文件、技能、子代理、模型选择”统一进终端工作流里**。

### Mini-Agent

本地 `Mini-Agent` 的扩展体系规模较小，但设计上很干净：

- 文件与 shell 基础工具
- note tool
- MCP loader
- Claude skills progressive disclosure

尤其 skill loader 的实现很适合作为学习材料，因为它直接把 `SKILL.md` 解析、路径展开和按需注入做成了清晰模块。

**这一维度的结论**：

- 平台扩展治理：`DeerFlow` 最强
- 终端开发工作流整合：`Kode-Agent` 最强
- 轻量技能体系可读性：`Mini-Agent` 最好

---

## 6. 沙箱、安全与权限模型

### DeerFlow

`DeerFlow` 在安全和隔离上最完整：

- thread-scoped workspace/uploads/outputs
- `SandboxProvider` 抽象
- `LocalSandboxProvider` 与 `AioSandboxProvider`
- Docker / provisioner / Kubernetes 路线
- 明确的虚拟路径映射
- README 专门给出公开部署风险提示

这已经不是“加个权限确认弹窗”的级别，而是**运行环境隔离设计**。

### Kode-Agent

`Kode-Agent` 的安全模型偏“terminal product safety”：

- permission mode
- safe mode
- tool-level permission engine
- 文件与 Bash 的规则系统
- 可选 system sandbox

不过需要注意一个细节：

- README 对默认模式的描述非常激进，强调 YOLO / skip permissions 的倾向
- 但安全模型文档和权限状态代码又显示出 `default` conversation permission mode、`safe` 模式、permission engine 等更细致的设计

这说明 `Kode-Agent` 的安全体系本身很丰富，但**README、文档与代码口径之间存在一定张力**。更稳妥的说法不是“它默认一定安全”或“它默认一定 YOLO”，而是：在落地使用前，最好按实际运行行为核对默认权限策略。

### Mini-Agent

本地 `Mini-Agent` 在这一维度明显最弱：

- Bash 直接运行在宿主环境的当前工作目录
- 没有 DeerFlow 那样的 sandbox provider abstraction
- 也没有 Kode-Agent 那样的 permission engine / mode system

这并不意味着它“写得差”，而是说明它当前目标不是高风险环境下的产品化代理。

**这一维度的结论**：

- 隔离与风险控制：`DeerFlow` 明显第一
- 终端交互式权限治理：`Kode-Agent` 明显更成熟
- 宿主环境直接执行、信任边界最薄：`Mini-Agent`

---

## 7. 模型与 Provider 策略

### DeerFlow

`DeerFlow` 的模型策略是平台化配置：

- 通过 `config.yaml` 声明多个模型
- 支持 OpenAI-compatible provider
- 支持 CLI-backed provider，如 Codex/Claude provider
- 支持 thinking / vision / responses API 等能力差异

### Kode-Agent

`Kode-Agent` 在模型策略上最激进：

- `main / task / compact / quick` model pointers
- `AskExpertModel` 工具
- 多模型协作是产品主卖点之一
- 允许按任务性质切模型、让子代理用不同模型

如果你把“模型调度能力”也算在 agent 架构里，`Kode-Agent` 是三者里最强的。

### Mini-Agent

本地 `Mini-Agent` 的模型策略相对简单：

- 支持 Anthropic/OpenAI 兼容 provider
- 允许通过配置切模型
- 但没有 `Kode-Agent` 那种多模型分工体系

它更接近“一个 agent，一个主要模型，再加一些实用增强”。

**这一维度的结论**：

- 多模型协作与模型分工：`Kode-Agent` 最强
- 平台级 provider 配置：`DeerFlow` 很强
- 单模型 runtime 的清晰性：`Mini-Agent` 最好

---

## 8. UI、交互与工作流

### DeerFlow

`DeerFlow` 的交互主阵地是 Web 前端和 Gateway API，不是 terminal-first。

它更适合：

- 长任务可视化
- thread 管理
- 文件上传
- artifact 浏览
- service integration

### Kode-Agent

`Kode-Agent` 在这方面是最完整的：

- Ink/React 终端 UI
- slash commands
- @mention
- external editor integration
- model selector
- permission request UI
- session selector
- cost tracking
- todo / plan mode / background tasks

这使它非常像“产品级 CLI”，而不只是命令行包装。

### Mini-Agent

本地 `Mini-Agent` 的 CLI 是干净的、够用的，但明显更轻：

- prompt_toolkit 交互
- 历史记录
- Esc cancel
- 日志浏览
- session info

它更像“工程师写给工程师用的实用 CLI”，不是强调终端产品设计的 workbench。

**这一维度的结论**：

- Terminal productization：`Kode-Agent` 最强
- Web/API workflow：`DeerFlow` 更强
- 轻量 CLI：`Mini-Agent` 足够但不重

---

## 9. 工程成熟度与维护信号

### DeerFlow

成熟度高的信号非常明显：

- 大规模社区反馈
- contributor 数量高
- 后端/网关/前端清晰分层
- 较多测试
- 文档多，架构文档和配置文档都较完整
- security notice、embedded client、gateway alignment 等都显示出平台治理意识

它的问题不是不成熟，而是**复杂度很高，不适合作为轻松阅读入口**。

### Kode-Agent

成熟度也相当高，但体现在另一侧：

- 终端产品化细节密度高
- 文档体系完整
- 测试文件数量多
- 多模型、子代理、插件、MCP、ACP、AGENTS.md 全都做了集成
- 架构虽不如 DeerFlow 那样服务化，但产品层完成度很高

它的问题不是能力不够，而是**系统面很广，理解成本不低，而且某些安全默认口径需要额外核对**。

### Mini-Agent

本地 `Mini-Agent` 的工程成熟度不能和前两者硬碰硬：

- 测试规模、文档规模、社区体量都更小
- 缺少平台级沙箱、子代理、权限层

但它有一个非常明确的优点：

- 当前复杂度和功能密度的比例很好

也就是说，它不是“成熟度低到不值一看”，而是**成熟度集中在单 agent runtime 的核心闭环上**。

---

## 与本地 Mini-Agent 相比：真实差距在哪里

## 1. Mini-Agent 相对 DeerFlow 的差距

最核心的差距有五个：

1. **运行时层级差距**  
   `Mini-Agent` 目前是单体 CLI runtime；`DeerFlow` 已经是平台级 agent harness。

2. **多 agent / 长时任务编排差距**  
   `Mini-Agent` 没有内建 subagent executor、thread-state orchestration、background subtask framework。

3. **隔离执行环境差距**  
   `Mini-Agent` 主要仍是宿主机工具调用；`DeerFlow` 已经有明确 sandbox provider 体系。

4. **管理面差距**  
   `Mini-Agent` 没有 DeerFlow 那样的 Gateway API、thread uploads、artifacts、skills 管理接口。

5. **记忆体系差距**  
   `Mini-Agent` 的 note memory 是实用的，但还不是 DeerFlow 那种结构化长期记忆机制。

## 2. Mini-Agent 相对 Kode-Agent 的差距

最核心的差距也很明显：

1. **终端工作流成熟度差距**  
   `Mini-Agent` 的 CLI 更轻；`Kode-Agent` 已经是 workbench 级别。

2. **多模型策略差距**  
   `Mini-Agent` 还没有 `main / task / compact / quick` 这种模型分工体系。

3. **子代理工作流差距**  
   `Mini-Agent` 没有 `TaskTool + background agent + resume transcript` 这一层。

4. **权限模式差距**  
   `Mini-Agent` 没有系统化的 permission engine、safe mode、plan mode。

5. **项目指令系统差距**  
   `Mini-Agent` 有 skills，但没有像 `Kode-Agent` 那样把 `AGENTS.md` 根到叶的发现逻辑做成核心能力。

---

## 与本地 Mini-Agent 相比：Mini-Agent 反而更强的地方

这部分很重要，因为很多比较报告容易只写“别人更强”。

## 1. 可读性

在“一个工程师用几天时间完全读通”的维度上，本地 `Mini-Agent` 明显优于另外两者。

它的优势是：

- 目录深度更浅
- 抽象层数更少
- 工具、skills、MCP、agent loop 之间的关系非常直接

## 2. 单 agent 基本闭环的教学价值

如果你的目标是：

- 学会 agent loop
- 学会工具调用
- 学会长上下文摘要
- 学会 session memory
- 学会技能按需加载
- 学会 MCP 接入

那么 `Mini-Agent` 其实比 `DeerFlow` 和 `Kode-Agent` 更适合作为起点。

## 3. Python 改造成本

如果你后续想自己继续改：

- 加 planner
- 加 subagent
- 加 permission layer
- 加更细的 memory

在当前代码规模下，`Mini-Agent` 是更容易动手的。

这点很现实：

- 在 `DeerFlow` 里你更像是在扩平台
- 在 `Kode-Agent` 里你更像是在改产品
- 在 `Mini-Agent` 里你更像是在继续造你自己的 runtime

---

## 如果继续演进 Mini-Agent，应该向谁学什么

## 1. 应该向 DeerFlow 学的部分

优先借鉴这些思想，而不是照搬全部实现：

- **显式 middleware 管线**  
  让 summarization、memory、title、todo、subagent limit、tool error handling 不再散落在主循环里。

- **thread-scoped runtime state**  
  把 workspace、uploads、outputs、artifacts、thread metadata 从“隐式文件路径”提升为显式 state。

- **sandbox provider abstraction**  
  至少把本地执行和隔离执行抽象成统一接口，为以后接 Docker/容器做准备。

- **subagent executor**  
  不一定一开始就做成 DeerFlow 那么重，但应该把“主代理调用子代理”设计成真正 runtime 能力。

- **memory pipeline**  
  把 note memory 升级为“过滤消息 -> 结构化抽取 -> 异步更新”的机制。

## 2. 应该向 Kode-Agent 学的部分

- **AGENTS.md / 项目指令发现链**  
  这是对 coding agent 非常有价值的一层。

- **模型指针体系**  
  `main / task / compact / quick` 非常值得借鉴。

- **TaskTool 风格子任务接口**  
  即使不做完整子代理系统，也可以先定义统一任务委派接口。

- **plan mode / todo mode**  
  如果你想把 `Mini-Agent` 从“隐式规划”升级到“显式任务状态”，这是很自然的下一步。

- **更强的终端交互**  
  包括 permission UI、model selector、session resume、tool progress 展示等。

## 3. Mini-Agent 自己应该保留的东西

这点同样重要。

不要在演进中丢掉这些优势：

- **清晰的 Agent 主循环**
- **简单直接的 Tool 抽象**
- **progressive disclosure 的 skill 设计**
- **低复杂度的代码组织**
- **“一个人能完整掌控全局”的可维护性**

---

## 场景建议

## 1. 如果你的目标是“研究复杂 agent 平台怎么搭”

优先看：`DeerFlow`

原因：

- 你会看到 runtime、gateway、frontend、sandbox、subagent、memory、skills、MCP 如何放进一个统一平台里。

## 2. 如果你的目标是“做一个终端里的 coding agent 产品”

优先看：`Kode-Agent`

原因：

- 你会看到用户输入、工具权限、模型切换、子代理、技能、项目约束文件、MCP、ACP 如何围绕 terminal UX 组织起来。

## 3. 如果你的目标是“自己继续把 Mini-Agent 做强”

优先顺序建议：

1. 先继续读透本地 `Mini-Agent`
2. 读 `Kode-Agent` 的 terminal workflow 部分
3. 读 `DeerFlow` 的 runtime/platform 部分

原因很简单：

- 如果先啃 DeerFlow，你容易在复杂平台细节里迷路
- 如果先啃 Kode-Agent，你可能会过早陷入产品交互层
- 先稳住 `Mini-Agent`，你才有清晰参照系去吸收另外两者

---

## 最终结论

### 1. DeerFlow 和 Kode-Agent 确实都属于“非常成熟的代码”

但成熟方向不同：

- `DeerFlow`：成熟在平台架构、长任务 orchestration、隔离执行和管理面
- `Kode-Agent`：成熟在 terminal 产品体验、多模型协作、权限交互和工作流组织

### 2. 本地 Mini-Agent 不在同一个规模层级

这是事实。

如果按“平台能力”和“产品完成度”直接比：

- `Mini-Agent` 都不占优

但如果按“可读性 + 改造成本 + 单 agent runtime 学习价值”比：

- `Mini-Agent` 其实非常占优

### 3. 对你当前最有价值的判断

如果你的目标是继续把本地 `Mini-Agent` 变成更强的 agent runtime，那么最合理的路线不是“选一个替代它”，而是：

- 保留 `Mini-Agent` 的轻量与清晰
- 向 `Kode-Agent` 借终端工作流和模型策略
- 向 `DeerFlow` 借平台抽象和运行时分层

这才是最现实、也最不会把当前项目带偏的路径。

---

## 参考与证据清单

### 公开仓库

- [DeerFlow 仓库](https://github.com/bytedance/deer-flow)
- [DeerFlow README](https://github.com/bytedance/deer-flow/blob/main/README.md)
- [DeerFlow backend README](https://github.com/bytedance/deer-flow/blob/main/backend/README.md)
- [DeerFlow Architecture](https://github.com/bytedance/deer-flow/blob/main/backend/docs/ARCHITECTURE.md)
- [DeerFlow lead agent](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/agents/lead_agent/agent.py)
- [DeerFlow subagent executor](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/subagents/executor.py)
- [DeerFlow memory middleware](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py)
- [DeerFlow skills loader](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/skills/loader.py)
- [DeerFlow MCP tools](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/mcp/tools.py)

- [Kode-Agent 仓库](https://github.com/shareAI-lab/Kode-Agent)
- [Kode-Agent README](https://github.com/shareAI-lab/Kode-Agent/blob/main/README.md)
- [Kode-Agent architecture doc](https://github.com/shareAI-lab/Kode-Agent/blob/main/docs/develop/architecture.md)
- [Kode-Agent security model](https://github.com/shareAI-lab/Kode-Agent/blob/main/docs/develop/security-model.md)
- [Kode-Agent query loop](https://github.com/shareAI-lab/Kode-Agent/blob/main/src/app/query.ts)
- [Kode-Agent TaskTool](https://github.com/shareAI-lab/Kode-Agent/blob/main/src/tools/agent/TaskTool/TaskTool.tsx)
- [Kode-Agent AskExpertModel](https://github.com/shareAI-lab/Kode-Agent/blob/main/src/tools/ai/AskExpertModelTool/AskExpertModelTool.tsx)
- [Kode-Agent project instructions](https://github.com/shareAI-lab/Kode-Agent/blob/main/src/utils/config/projectInstructions.ts)
- [Kode-Agent auto compact](https://github.com/shareAI-lab/Kode-Agent/blob/main/src/utils/session/autoCompactCore.ts)
- [Kode-Agent MCP integration](https://github.com/shareAI-lab/Kode-Agent/blob/main/src/services/mcp/tools-integration.ts)
- [Kode-Agent permission mode state](https://github.com/shareAI-lab/Kode-Agent/blob/main/src/utils/permissions/permissionModeState.ts)

- [Mini-Agent 上游仓库](https://github.com/MiniMax-AI/Mini-Agent)

### 本地 Mini-Agent 代码快照

- `README.md`
- `pyproject.toml`
- `mini_agent/agent.py`
- `mini_agent/cli.py`
- `mini_agent/tools/bash_tool.py`
- `mini_agent/tools/note_tool.py`
- `mini_agent/tools/skill_loader.py`
- `mini_agent/tools/skill_tool.py`
- `mini_agent/tools/mcp_loader.py`
- `examples/04_full_agent.py`

### 说明

- 对 `Mini-Agent` 的能力判断，以本地工作区中的当前代码为准。
- 对公开社区指标的判断，以 2026-04-01 拉取到的 GitHub 数据为准。
