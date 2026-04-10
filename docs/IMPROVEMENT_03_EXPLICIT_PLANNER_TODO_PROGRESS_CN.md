# 改进设计 03：显式 Planner / Todo / Progress 系统

> 作者：Codex（GPT-5）
> 日期：2026-03-31
> 适用仓库：`Mini-Agent`
> 本文聚焦范围：任务规划、任务状态管理、执行进度可视化、动态重规划

---

## 1. 这份文档要解决什么问题

当前 `Mini-Agent` 的核心执行形态是典型的工具调用循环：

1. 把当前消息历史发送给模型
2. 模型决定是否调用工具
3. 执行工具
4. 把工具结果写回消息历史
5. 继续下一轮

这使它本质上是一个 **ReAct 风格的 Agent Runtime**，而不是一个显式的 `plan-and-execute` 系统。

从代码上看，这一点很清楚：

- [`mini_agent/agent.py`](../mini_agent/agent.py) 的 `run()` 主循环只围绕“消息 -> LLM -> tool_calls -> 结果回填”展开
- [`mini_agent/config/system_prompt.md`](../mini_agent/config/system_prompt.md) 虽然要求模型“Break down complex tasks”和“Report progress”，但这是 prompt-level guidance，不是代码级任务状态系统
- [`mini_agent/cli.py`](../mini_agent/cli.py) 向用户暴露的是：
  - Session Info
  - Session Statistics
  - message count
  - log file

当前系统已经能显示：

- 当前是第几步：`Step n/max_steps`
- 会话级统计：消息数、tool call 数、token 用量
- 日志文件位置

但它还缺少真正的**任务级状态表示**：

- 当前任务拆成了哪些子步骤
- 哪些步骤已完成
- 哪些步骤被阻塞
- 当前正在推进哪一个任务
- 为什么下一步是这个，而不是别的
- 当执行偏离原计划时，如何更新计划

这会导致几个现实问题：

1. 用户只能看到“Agent 正在跑”，但看不到“Agent 在完成什么结构化目标”。
2. 长任务中，模型很容易因为上下文漂移而反复试错。
3. 压缩上下文时，没有一个稳定的任务骨架可供保留。
4. 失败恢复时，系统缺少显式的“中间状态”可复用。
5. 现有日志虽然记录了请求与工具结果，但不记录任务计划的演化过程。

本文的目标不是把 `Mini-Agent` 彻底改造成重型 workflow engine，而是设计一套**显式任务状态层**，使它具备：

- 更清晰的任务分解
- 更可见的执行进度
- 更稳定的长任务推进
- 更合理的失败恢复
- 和现有 ReAct 主循环兼容的动态重规划能力

---

## 2. 先说清楚：这不是“放弃 ReAct”

这是这项改进里最容易混淆、也最需要讲清楚的一点。

### 2.1 ReAct 和 plan-and-execute 不是一回事

- **ReAct**
  - 关注“单步决策方式”
  - 当前看到什么，就想一步、调一个工具、看结果、再决定下一步

- **plan-and-execute**
  - 关注“高层任务组织方式”
  - 先生成计划，再按计划推进，必要时重规划

它们不是完全对立的两种世界观，而是可以叠加在不同层面上。

### 2.2 本改进的目标形态是什么

本文推荐的目标形态不是：

- 纯 ReAct
- 也不是严格的纯 plan-and-execute

而是：

**plan-guided ReAct**

也可以理解为：

- 底层执行内核仍然是 ReAct
- 上层增加显式的 plan / todo / progress 状态层

这意味着：

- 模型仍然可以根据观察结果灵活决策
- 但系统会把任务骨架、当前步骤、已完成事项显式保存下来
- 并在必要时做动态 replan

### 2.3 为什么这是一种优化，而不是架构背叛

原因很简单：

- `Mini-Agent` 现在已经有不错的工具循环和上下文机制
- 你前面还计划补“高级上下文管理”和“模型回退机制”

在这个背景下，加 planner/todo/progress 的最大价值不是替换 ReAct，而是：

- 给上下文管理提供稳定锚点
- 给用户提供任务级可见性
- 给失败恢复提供中间状态
- 给模型路由提供任务复杂度信号

所以这一步不是和现有架构对着干，而是在补“任务执行可控性”这一块。

---

## 3. 当前实现评估

### 3.1 当前已有的“隐式规划”

当前系统不是完全没有规划意识。

现有规划能力主要来自三处：

1. **System Prompt**
   - 在 [`mini_agent/config/system_prompt.md`](../mini_agent/config/system_prompt.md) 中明确要求模型：
     - Analyze
     - Break down complex tasks
     - Execute systematically
     - Report progress

2. **ReAct 执行循环**
   - 在 [`mini_agent/agent.py`](../mini_agent/agent.py) 中，模型可以基于上一轮 observation 自主决定下一步

3. **Session Note**
   - [`mini_agent/tools/note_tool.py`](../mini_agent/tools/note_tool.py) 允许 Agent 记录关键事实和阶段性信息

这说明当前系统已经具备“隐式 planning”基础，只是没有把它工程化成显式状态。

### 3.2 当前用户可见的状态信息

当前 CLI 主要提供的是会话级状态，而不是任务级状态。

比如 [`mini_agent/cli.py`](../mini_agent/cli.py) 里：

- `print_session_info()` 展示：
  - model
  - workspace
  - message history count
  - available tools

- `print_stats()` 展示：
  - session duration
  - total messages
  - user / assistant / tool 消息数
  - API token used

这些都很有用，但它们回答不了：

- “这个任务做到哪里了？”
- “接下来要做什么？”

### 3.3 当前日志能力

[`mini_agent/logger.py`](../mini_agent/logger.py) 目前会记录：

- LLM request
- LLM response
- tool execution result

这已经很好，但缺少：

- 计划创建日志
- 计划更新日志
- 进度变更日志
- replan 触发原因

这意味着即使任务失败，开发者也很难从日志中快速看出：

- 计划是否合理
- 是在哪一步开始漂移
- 失败后有没有正确调整计划

### 3.4 当前实现的主要问题

#### 问题 1：任务骨架完全依赖模型临场记忆

只要上下文变长、摘要发生、工具输出过多，模型就可能忘掉：

- 最初目标
- 当前子目标
- 已完成内容
- 还没做完的关键步骤

#### 问题 2：没有“执行状态”这一等数据结构

当前消息历史里有过程，但没有清晰的结构化任务状态。

缺失的最小单位包括：

- 计划步骤
- 步骤状态
- 当前活跃步骤
- blocker
- 最近完成项

#### 问题 3：没有动态重规划机制

当前系统即使执行中发现：

- 原路径行不通
- 工具结果与预期不符
- 用户追加要求

也没有代码级 replan 机制。  
只能依赖模型在下一轮“自己想起来调整”。

#### 问题 4：无法稳定地把“任务进度”反馈给用户

现在能反馈的是“Agent 还在跑第几轮”，不是“任务做到了第几步”。

这对短任务问题不大，但对复杂代码任务影响很明显：

- 用户难以判断 Agent 是否偏航
- 用户难以中途纠偏
- 用户难以建立信任

---

## 4. 改进目标

建议把目标明确收敛为三类。

### 4.1 目标一：建立显式任务状态

系统需要显式维护：

- 当前计划
- 当前 todo
- 当前 progress

而不是只把它们藏在模型的自然语言思考里。

### 4.2 目标二：保持 ReAct 的灵活性

目标不是让 Agent 变成死板工作流，而是让它：

- 有骨架
- 但不被骨架锁死

也就是说：

- 计划用于指导
- 不是用于完全约束

### 4.3 目标三：让计划状态成为上下文治理和日志的一部分

planner/todo/progress 不应该只是 CLI 上显示一下，而应该进入：

- 压缩保留层
- 日志层
- 失败恢复层

这样它才是真正的 runtime 组成部分。

---

## 5. 总体设计建议

### 5.1 设计原则

建议遵守以下原则：

1. **先做最小可行任务状态层**
   不要一上来做 DAG、依赖图、并行子任务调度。

2. **先支持单任务主线**
   先把“一条任务主线 + 动态调整”做好，再考虑复杂任务图。

3. **计划用于指导，不用于强约束**
   保留 ReAct 弹性，避免计划一旦错了整个系统就僵住。

4. **计划状态必须结构化**
   不要只把 plan 存成一段文本。

5. **重规划必须可解释**
   每次 replan 都要有明确触发原因。

### 5.2 推荐的新模块边界

建议新增目录：

```text
mini_agent/planning/
  __init__.py
  planner.py
  state.py
  tracker.py
  prompts.py
  formatter.py
  hooks.py
```

各模块职责建议如下：

- `planner.py`
  - 生成初始计划
  - 根据执行结果做 replan

- `state.py`
  - 定义 `Plan`, `PlanStep`, `TodoState`, `ProgressState`

- `tracker.py`
  - 根据工具结果、assistant 输出更新状态
  - 标记步骤为 `pending / in_progress / completed / blocked / skipped`

- `prompts.py`
  - 放规划与重规划 prompt 模板

- `formatter.py`
  - 将 plan/progress 转为用户显示文本
  - 将 plan/progress 转为上下文注入文本

- `hooks.py`
  - 在 Agent 主循环前后挂接 planner 流程

### 5.3 推荐的数据模型

建议至少定义以下结构：

```python
PlanStep:
  step_id: str
  title: str
  description: str
  intended_tools: list[str]
  success_criteria: str
  status: str  # pending / in_progress / completed / blocked / skipped
  result_summary: str | None
  blocker: str | None

Plan:
  task: str
  goal: str
  assumptions: list[str]
  steps: list[PlanStep]
  created_at: datetime
  updated_at: datetime
  version: int

TodoState:
  current_step_id: str | None
  next_step_ids: list[str]
  recently_completed: list[str]
  blockers: list[str]

ProgressState:
  total_steps: int
  completed_steps: int
  blocked_steps: int
  percent: float
  status_summary: str
```

这里的关键不是字段名本身，而是两层变化：

- “计划”成为结构化状态
- “进度”成为计算结果，而不是自由文本

---

## 6. 推荐的运行机制

### 6.1 任务启动阶段

在用户提交任务后，先生成初始计划。

推荐流程：

1. 用户输入任务
2. `Planner.create_plan(task, tools, workspace_context)`
3. 解析出 3-8 个步骤
4. 初始化 `TodoState` 和 `ProgressState`
5. 将计划摘要注入主上下文

这一步不是为了“先规划再完全照着做”，而是为了给后续执行提供任务骨架。

### 6.2 执行阶段

执行阶段仍然保留现有 ReAct 主循环。

推荐机制：

1. 当前计划状态被注入到本轮上下文
2. 模型基于：
   - 用户消息
   - 当前 todo
   - 最近进展
   - 工具结果
   做出下一步决策
3. 工具执行完成后，`tracker` 尝试更新当前步骤状态
4. 每轮结束后重新计算 progress

这意味着“执行权”仍在 ReAct loop，而 planner 提供的是任务级引导。

### 6.3 重规划阶段

当满足特定条件时，触发 replan。

推荐触发条件包括：

- 当前步骤连续失败 `N` 次
- 工具返回结果与 success criteria 明显冲突
- 用户追加新要求
- 发现新的 blocker
- 当前计划步骤全部完成但任务仍未结束

重规划流程建议：

1. 输入：
   - 当前 plan
   - 最近完成项
   - blocker
   - 最新观测
2. 输出：
   - 更新后的 plan
   - 变更原因
   - 新的 current todo

### 6.4 完成阶段

任务结束时，应形成结构化完成记录：

- 最终完成状态
- 已完成步骤列表
- 跳过步骤列表
- blocker 摘要
- 最终结果摘要

这不仅有利于用户展示，也有利于上下文压缩和日志分析。

---

## 7. Todo / Progress 应该长什么样

### 7.1 Todo 的职责

`todo` 不是完整计划，它是计划的“活跃操作视图”。

建议它至少包含：

- 当前正在做的步骤
- 接下来最可能要做的 1-3 步
- 最近完成的事项
- 当前 blocker

这样用户和模型都能快速知道当前焦点在哪里。

### 7.2 Progress 的职责

`progress` 不是“第几轮 tool call”，而是任务级进展。

建议包含：

- 总步骤数
- 完成步骤数
- 阻塞步骤数
- 当前活跃步骤
- 一个短的状态摘要

例如：

```text
Progress: 2/5 steps completed
Current: Inspect test failures
Next: Patch parser -> run targeted tests
Blocked: None
```

### 7.3 对用户的展示建议

CLI 层建议新增至少三个命令：

- `/plan`
  - 显示当前完整计划

- `/todo`
  - 显示当前待办和 blocker

- `/progress`
  - 显示进度摘要

如果只做一个命令，我建议先做 `/progress`，因为它最轻、最常用。

---

## 8. 和现有 Agent 主循环怎么接

### 8.1 推荐的最小改动点

当前 [`mini_agent/agent.py`](../mini_agent/agent.py) 已有明确主循环，适合做“最小侵入式挂接”。

建议在以下位置接入：

1. **任务开始前**
   - 创建初始计划

2. **每轮 LLM 调用前**
   - 把 plan/todo/progress 的简化视图注入上下文

3. **每轮工具执行后**
   - 根据观察结果更新 tracker

4. **每轮结束后**
   - 判断是否需要 replan

### 8.2 推荐的 Agent 集成方式

建议给 `Agent` 增加一个可选依赖：

```python
planner_runtime: PlannerRuntime | None = None
```

其中 `PlannerRuntime` 负责：

- 初始化计划
- 返回当前 todo/progress 文本
- 处理步骤状态更新
- 决定是否重规划

这样 `Agent` 不需要直接管理大量 planning 细节。

### 8.3 不建议的做法

不建议把 planning 逻辑直接硬塞进 `agent.py` 的主循环里，例如：

- 一堆 plan-related if/else
- 直接在消息列表里拼装大量临时计划文本
- 由 CLI 单独维护 plan 状态

原因是这些做法会让后续：

- 上下文管理
- 失败恢复
- 测试
- 日志

都变得很难扩展。

---

## 9. 与高级上下文管理的关系

这项改进和第一份文档的关系非常紧密。

### 9.1 planner/todo/progress 是天然的 pinned metadata

在高级上下文管理里，我们建议保留：

- 当前目标
- 活跃文件
- blocker
- 高优先级约束

而显式 planner/todo/progress 正好提供这些信息的结构化来源。

### 9.2 它会显著提升摘要稳定性

有了显式任务状态之后，压缩时就不用只依赖自由文本摘要去推断：

- 到底做到了哪一步
- 还有哪些没完成

而是可以直接保留：

- 当前步骤
- 已完成步骤
- blocker

这会显著降低长会话漂移风险。

### 9.3 召回系统也会更有目标

有 planner 之后，历史召回可以根据：

- 当前步骤标题
- 活跃文件
- blocker

来做更精准的召回，而不是只靠任务原文关键词。

---

## 10. 与模型回退机制的关系

planner/todo/progress 还能反过来帮助第二项改进。

### 10.1 任务分级路由

有了显式计划后，后续可以按步骤类型做模型路由：

- 简单读文件 / 列目录：较普通模型即可
- 复杂修复 / 方案设计 / 总结：优先更强模型

虽然这不是第一阶段必须做的，但它是一个非常自然的后续方向。

### 10.2 故障恢复更容易

如果主模型失败并切换到备用模型，显式计划状态能帮助备用模型更快接上上下文，因为：

- 当前任务骨架已经结构化保存
- 最近完成事项明确
- blocker 明确

这会比只靠自由文本消息历史更稳。

---

## 11. 推荐的实现路线

### 11.1 Phase 1：最小可行 Planner

目标：

- 新增 `PlanStep`, `Plan`, `TodoState`, `ProgressState`
- 任务开始时生成 3-8 步计划
- 每轮执行后能标记简单完成状态
- 新增 `/progress`

这期完成后，你就已经得到：

- 任务可见性
- 初步执行骨架
- 对上下文治理有帮助的结构化状态

### 11.2 Phase 2：动态更新与 Todo 视图

目标：

- 新增 tracker
- 支持 `pending / in_progress / completed / blocked / skipped`
- 新增 `/plan` 和 `/todo`
- 将 plan/todo/progress 注入上下文

这期完成后，planner 才开始真正影响执行稳定性。

### 11.3 Phase 3：重规划与日志闭环

目标：

- 新增 replan 触发条件
- 日志记录计划创建、更新、重规划原因
- 让 context manager 保留 plan 骨架

这期完成后，planner 才真正成为 runtime 组成部分，而不是“显示层小功能”。

---

## 12. 推荐修改的文件范围

建议的变更集合大致如下：

- 新增：
  - `mini_agent/planning/planner.py`
  - `mini_agent/planning/state.py`
  - `mini_agent/planning/tracker.py`
  - `mini_agent/planning/prompts.py`
  - `mini_agent/planning/formatter.py`
  - `mini_agent/planning/hooks.py`

- 修改：
  - `mini_agent/agent.py`
  - `mini_agent/cli.py`
  - `mini_agent/config/system_prompt.md`
  - `mini_agent/logger.py`

- 可选联动：
  - `mini_agent/tools/note_tool.py`
  - 未来的 `mini_agent/context/*`

- 新增测试：
  - `tests/test_planner_state.py`
  - `tests/test_planner_generation.py`
  - `tests/test_planner_tracker.py`
  - `tests/test_progress_commands.py`
  - `tests/test_replan_triggers.py`

---

## 13. 技术难度评估

### 13.1 综合难度

综合难度：**中等偏上**

它没有“高级上下文管理”和“模型高可用”那么底层，也没有那么多协议与可靠性问题，但它仍然是一个实打实的 runtime 设计改造。

### 13.2 难点拆分

- 结构化计划生成：中等
- 执行状态更新：中等偏上
- 动态 replan：高
- CLI 展示：低
- 与上下文管理联动：中等偏上
- 与日志层联动：中等

### 13.3 为什么难

难点主要不在“写个 planner prompt”，而在下面这些地方：

1. 计划太刚，Agent 会变笨
2. 计划太松，计划就没意义
3. tracker 更新不准，progress 会误导用户
4. replan 太频繁，会变成“每轮都推翻自己”
5. replan 太保守，又起不到纠偏作用

---

## 14. 风险与注意事项

### 风险 1：一上来做太重

如果一开始就做：

- DAG
- 步骤依赖图
- 并行子任务
- 多计划版本合并

很容易把项目复杂度拉爆。

当前仓库最合适的是：

- 单任务主线
- 线性步骤
- 可解释重规划

### 风险 2：把 planner 做成硬约束

如果要求模型必须严格按步骤机械执行，会损失 ReAct 的灵活性。

这会在以下场景出问题：

- 工具返回意外结果
- 用户突然追加约束
- 某一步发现更优路径

所以计划应是“强引导，弱约束”。

### 风险 3：tracker 过度依赖自然语言猜测

如果 tracker 完全靠模糊文本判断步骤是否完成，误判会很多。

建议优先结合：

- 当前激活步骤
- 工具类型
- 结果成功与否
- success criteria

来做更稳的状态更新。

### 风险 4：计划污染上下文

计划也会占 token。  
如果每轮都把完整大计划塞进上下文，反而会加重上下文负担。

所以建议只注入：

- 当前步骤
- 最近完成项
- blocker
- 短版计划摘要

完整计划保留给：

- CLI 展示
- 日志
- 压缩层的 pinned metadata

---

## 15. 验证与测试建议

### 15.1 必测场景

至少要覆盖以下测试场景：

1. 任务启动时成功生成计划
2. 计划生成失败时系统回退到原始 ReAct 模式
3. 工具执行成功后，当前步骤能更新为 completed
4. 工具连续失败时，当前步骤能标记 blocked
5. 用户追加新要求后触发 replan
6. `/progress` 能正确显示当前状态
7. 上下文压缩后仍保留当前任务骨架
8. 日志中能看到计划创建和更新记录

### 15.2 推荐新增日志

建议至少增加：

- PLAN_CREATED
- PLAN_UPDATED
- PLAN_REPLANNED
- STEP_STATUS_CHANGED
- PROGRESS_SNAPSHOT

这样后续调试时，你就不仅能看到“模型说了什么、工具做了什么”，还能看到“任务状态是怎么演化的”。

---

## 16. 最终建议

如果只给一个判断：

**这项改进非常值得做，而且它是把 `Mini-Agent` 从“强工具循环”推向“完整 code agent harness”的关键一步。**

但它最好的落地方式不是“全面 workflow 化”，而是：

1. 保留现有 ReAct 主循环
2. 增加显式 plan/todo/progress 状态层
3. 在必要时动态 replan
4. 把这层状态和上下文管理、日志、CLI 打通

这是当前仓库最稳、也最有工程价值的路线。

---

## 17. 一句话总结

这项改进的本质，不是“给 Agent 加一个待办列表”，而是：

**把 `Mini-Agent` 从“隐式规划的 ReAct Demo”升级为“具备显式任务状态的 plan-guided ReAct Runtime”。**
