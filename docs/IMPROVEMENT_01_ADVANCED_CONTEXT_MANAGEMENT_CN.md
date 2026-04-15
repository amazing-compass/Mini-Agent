# 改进设计 01：高级上下文管理

> 作者：Codex（GPT-5）
> 日期：2026-03-31
> 适用仓库：`Mini-Agent`
> 本文聚焦范围：更精确的 Token 计算、可组合的消息压缩策略、上下文保留与召回机制

---

## 1. 这份文档要解决什么问题

当前 `Mini-Agent` 已经具备基础的长上下文保护能力：

- 在 [`mini_agent/agent.py`](../mini_agent/agent.py) 中使用 `tiktoken` 做本地 Token 估算
- 当本地估算值或 API 返回的 `usage.total_tokens` 超过 `token_limit` 时触发摘要
- 以“按用户轮次压缩 Agent 执行过程”的方式保留主线历史
- 通过 [`mini_agent/tools/note_tool.py`](../mini_agent/tools/note_tool.py) 提供简单的会话笔记能力

这套机制对 Demo 来说已经够用，但从“可长期运行的 Agent Runtime”角度看，还存在明显不足：

1. Token 估算精度有限，且没有 provider-aware 的预算管理。
2. 压缩策略单一，几乎只有“整轮摘要”这一种手段。
3. 摘要后的信息组织方式较弱，缺少显式的“热上下文 / 冷上下文 / 元信息 / 可召回知识”分层。
4. `note_tool` 和自动摘要是分离的，两套记忆能力没有统一成一个上下文治理系统。
5. 当任务很长、需求频繁变动或工具输出很多时，系统缺少更细粒度的裁剪与召回策略。

本文的目标不是“给 Demo 再加一个摘要开关”，而是为 `Mini-Agent` 设计一套**可扩展的上下文治理层**，使它具备更强的：

- 可预测性
- 可解释性
- 长会话稳定性
- 资源利用效率
- 后续演进空间

---

## 2. 当前实现评估

### 2.1 当前实现在哪里

当前和上下文管理最相关的代码主要在：

- [`mini_agent/agent.py`](../mini_agent/agent.py)
- [`mini_agent/schema/schema.py`](../mini_agent/schema/schema.py)
- [`mini_agent/tools/note_tool.py`](../mini_agent/tools/note_tool.py)

其中核心逻辑包括：

- `_estimate_tokens()`
- `_estimate_tokens_fallback()`
- `_summarize_messages()`
- `_create_summary()`

### 2.2 当前实现的优点

现有方案并不是“没有设计”，它已经有几个不错的点：

- 有双重触发条件：本地估算和 API usage 都能触发压缩。
- 摘要按“用户轮次”切分，而不是简单截断最近消息，这比很多 demo 更稳。
- 摘要时保留所有用户消息，只压缩执行过程，能减少主任务目标丢失。
- 摘要失败时有 fallback，不会因为摘要失败直接中断整个执行链。
- 已经有 `Session Note` 工具，说明仓库本身接受“记忆不是只靠 message list”的设计方向。

### 2.3 当前实现的主要问题

#### 问题 1：Token 估算方式过于统一

当前实现使用 `cl100k_base` 统一估算所有消息的 Token。这个策略简单可用，但有几个现实问题：

- OpenAI / Anthropic / 兼容接口的消息计费并不完全一致。
- `tool_calls`、`thinking`、结构化 content block 的实际开销与 `str()` 序列化后的开销不完全一致。
- 工具 schema 本身也会占 token，但当前预算感知较弱。
- 估算只输出一个总量，没有显式区分：
  - system prompt
  - user history
  - assistant history
  - tool output
  - tools schema

这会导致系统虽然“知道快爆了”，但不知道“到底是谁吃掉了预算”。

#### 问题 2：压缩策略只有一种主路径

当前系统基本上是：

1. 超预算
2. 遍历用户轮次
3. 把该轮执行过程变成摘要
4. 用 `system + user messages + summaries` 替换整个消息列表

这有两个后果：

- 压缩粒度比较粗，一旦触发就是整轮重写。
- 没有“先轻压、再重压”的层级化策略。

在真实任务里，不同内容其实应该区别对待：

- 最近几轮原始消息应该优先保留
- 用户硬约束、当前目标、活跃文件、打开的问题应该尽量不压
- 大段工具输出应优先裁剪或结构化保留
- 很旧但可能有价值的历史应转成可召回信息，而不是完全塞进主上下文

#### 问题 3：摘要产物本身不够结构化

当前 `_create_summary()` 的输出是自由文本摘要，适合读，但不够利于系统治理。

缺失的能力包括：

- 无显式字段区分“已完成工作 / 未完成事项 / 关键事实 / 活跃文件 / 风险”
- 无法精确召回某一类信息
- 无法做后续二次压缩或摘要质量校验

#### 问题 4：没有统一的上下文层次模型

当前的上下文基本可以理解为：

- 主消息历史
- 一个独立的 `note_tool` 文件

但如果要做更强的上下文治理，至少应该区分：

- 热上下文：当前几轮必须直接送给模型的内容
- 钉住的元信息：目标、约束、todo、关键决策、重要路径
- 冷上下文摘要：旧轮次的压缩结果
- 可召回知识：按需回填的历史信息

当前系统还没有这一层统一抽象。

---

## 3. 改进目标

本次改进建议把目标明确收敛为三类。

### 3.1 目标一：做 provider-aware 的 Token 预算管理

不是只要“算得更准”，而是要做到：

- 预算有来源：不同 provider / model 可切换不同估算策略
- 预算可拆解：知道各类消息各占多少
- 预算可操作：在调用前知道该压谁、该留谁

### 3.2 目标二：把单一摘要升级为可组合压缩策略

从“只有整轮摘要”升级为“多策略组合”：

- 最近窗口保留
- 元信息固定保留
- 大工具输出裁剪
- 旧轮次结构化摘要
- 必要时从历史中召回相关信息

### 3.3 目标三：建立统一的上下文治理抽象

目标不是再往 `Agent` 里塞更多 if/else，而是引入一层独立上下文管理模块，使其负责：

- 预算评估
- 压缩决策
- 摘要产物管理
- 元信息提炼
- 历史召回

这样后面即使再加 planner/todo、模型回退、持久化记忆，也不会把 `agent.py` 继续堆胖。

---

## 4. 建议的总体设计

### 4.1 设计原则

本改进建议遵守以下原则：

1. **先解耦，再增强**
   先把上下文治理从 `Agent` 主循环里抽象出来，再慢慢叠加更复杂的策略。

2. **优先做可解释的策略**
   比起上来就做向量召回，更优先做“看得懂、调得动”的层级压缩。

3. **先本地一致，再追求复杂记忆**
   第一阶段先把内存态和本地持久化设计清楚，再考虑远程存储或更重的检索系统。

4. **预算管理必须前置**
   上下文治理不是“超了再想办法”，而应该是“发送前就知道本轮预算怎么分配”。

5. **保留人工调试能力**
   每次压缩、召回、裁剪都应该能在日志里解释清楚发生了什么。

### 4.2 推荐的新模块边界

建议新增目录：

```text
mini_agent/context/
  __init__.py
  manager.py
  budget.py
  compaction.py
  summarizer.py
  recall.py
  models.py
```

各模块职责建议如下：

- `manager.py`
  - ContextManager 主入口
  - 负责在 Agent 主循环前后执行上下文治理流程

- `budget.py`
  - TokenEstimator 抽象
  - provider/model 级别预算配置
  - 消息分项统计

- `compaction.py`
  - 各种压缩策略
  - 负责组合策略执行顺序

- `summarizer.py`
  - 结构化摘要逻辑
  - 摘要 prompt 管理
  - 摘要质量校验

- `recall.py`
  - 轻量历史召回
  - 可以先做关键词 / 规则 / 标签召回

- `models.py`
  - 定义上下文快照、摘要块、预算统计等数据结构

### 4.3 推荐的数据模型

建议至少引入以下结构：

```python
ContextSnapshot:
  system_prompt: Message
  recent_messages: list[Message]
  pinned_facts: list[PinnedFact]
  round_summaries: list[RoundSummary]
  recalled_memories: list[RecalledMemory]
  token_budget: TokenBudgetReport

RoundSummary:
  round_id: int
  user_goal: str
  completed_actions: list[str]
  important_findings: list[str]
  files_touched: list[str]
  open_questions: list[str]
  raw_summary: str

PinnedFact:
  key: str
  value: str
  priority: str
  source: str

TokenBudgetReport:
  total_estimated_tokens: int
  system_tokens: int
  conversation_tokens: int
  tool_schema_tokens: int
  recall_tokens: int
  reserved_completion_tokens: int
  provider: str
  model: str
```

这里的关键不是类名本身，而是两个设计变化：

- 摘要不再只是自由文本，而是带结构字段
- Token 不再只是单一整数，而是分项预算报告

---

## 5. Token 计算改进方案

### 5.1 难点判断

技术难度：**中等偏上**

原因不是“写一个 tokenizer 很难”，而是要在“精度、通用性、复杂度”之间做取舍。

### 5.2 当前问题归纳

当前 `Agent._estimate_tokens()` 有三个主要问题：

- 对不同 provider 的计费模型感知不足
- 对 tool schema、thinking、content block 的计算较粗
- 无法形成“发送前预算分配”，只能形成“发送前总量预警”

### 5.3 推荐方案

建议不要追求“完全精确复刻所有 provider 的计费公式”，而是采用**分层估算**：

#### 第一层：Provider-aware 本地估算

提供统一接口：

```python
class TokenEstimator(Protocol):
    def estimate_messages(self, messages: list[Message]) -> TokenBreakdown: ...
    def estimate_tools(self, tools: list[Tool]) -> int: ...
```

推荐至少实现两类估算器：

- `OpenAICompatibleEstimator`
- `AnthropicCompatibleEstimator`

如果后续模型池里引入更多 OpenAI-compatible 服务，也可以共用前者。

#### 第二层：API usage 反向校准

每次 LLM 返回 `usage` 后，用实际 usage 去更新一个滚动校准系数，例如：

- `estimated_prompt_tokens -> actual_prompt_tokens`
- 建立近几轮误差平均值

这不需要把 estimator 做成机器学习模型，只要能做到：

- 发现某 provider 长期高估或低估
- 对下一轮预算做轻微修正

就已经很有价值。

#### 第三层：预留 completion 预算

发送前不要只看 prompt tokens，还要预留 completion 空间。

建议把总预算拆成：

- prompt 可用预算
- completion 预留预算
- 安全缓冲区

例如：

- `context_window = 128k`
- `reserved_completion = 12k`
- `safety_margin = 8k`
- `prompt_budget = 108k`

只有在 `prompt_estimate > prompt_budget` 时才触发压缩。

### 5.4 实施建议

建议先做到以下程度即可：

第一阶段：

- 把消息估算与工具 schema 估算拆开
- 引入 provider-aware estimator
- 引入 reserved completion 预算
- 输出结构化 TokenBudgetReport

第二阶段：

- 加入 usage-based calibration
- 根据不同模型配置默认安全边界

不建议第一阶段就做的事情：

- 试图 100% 复刻官方闭源计费规则
- 为每一种第三方兼容 API 定制特殊估算器

---

## 6. 消息压缩与保留策略改进方案

### 6.1 难点判断

技术难度：**高**

真正难的是“压缩后还能不能继续稳定完成任务”，不是“能不能把文本变短”。

### 6.2 推荐的压缩策略层次

建议采用**分层压缩流水线**，而不是单一摘要。

推荐顺序如下：

1. **固定保留层**
   - system prompt
   - 当前用户目标
   - 当前活跃 todo / plan
   - 高优先级用户约束
   - 活跃文件与关键路径

2. **最近窗口层**
   - 保留最近 `N` 轮原始消息
   - `N` 建议配置化，而不是写死

3. **工具输出裁剪层**
   - 大文本工具输出优先截断或抽取头尾
   - 保留 exit code、错误摘要、关键片段

4. **旧轮结构化摘要层**
   - 对较旧轮次生成 `RoundSummary`
   - 保留完成事项、重要发现、未完成项、文件触点

5. **按需召回层**
   - 当前轮之前，按任务关键词或标签召回相关历史摘要或 notes

### 6.3 推荐的压缩触发策略

不要只在“已超预算”时触发，建议分两类：

#### 预防性压缩

当预计下一次请求很可能超预算时，提前压缩。

触发条件示例：

- `estimated_prompt_tokens > prompt_budget * 0.85`
- 最近一次工具输出很大
- 当前模型的 completion 预留较高

#### 强制性压缩

当已经超过预算时，必须压缩。

触发条件示例：

- `estimated_prompt_tokens > prompt_budget`

### 6.4 结构化摘要建议

当前 `_create_summary()` 建议升级为输出结构化块，而不是自由文本段落。

推荐摘要 Prompt 输出以下字段：

- 本轮目标
- 已完成的动作
- 调用过的工具
- 关键结果
- 关键文件 / 路径
- 未解决问题
- 后续建议

如果模型输出失败，再降级成自由文本摘要。

### 6.5 元信息保留建议

建议从对话中提取一份“钉住的上下文元信息”，单独维护，不参与普通轮次压缩。

优先级高的元信息包括：

- 用户明确要求
- 禁止事项
- 当前目标与子目标
- 环境信息
- 工作目录
- 重要文件
- 失败原因
- 当前 blocker

这部分可以来自：

- 用户消息提取
- 工具结果提取
- planner/todo 系统
- note_tool 写入

### 6.6 召回系统建议

召回系统第一阶段不要上向量数据库，先做轻量版：

- 标签匹配
- 关键词匹配
- 文件路径匹配
- 最近相似任务匹配

可选实现：

- JSONL + 简单倒排索引
- SQLite FTS

如果第一阶段就引入向量库，会把项目复杂度拉高，而且收益未必立刻明显。

---

## 7. 与现有 note_tool 的关系

`SessionNoteTool` 不应该被废弃，但它需要被纳入统一上下文治理体系。

推荐方向：

- `note_tool` 继续保留为“Agent 主动记录关键事实”的工具
- `ContextManager` 将 note 视为可召回的结构化记忆源之一
- 自动摘要与 note 不再是两条平行线，而是：
  - 摘要负责“压缩轮次过程”
  - note 负责“显式记录关键事实”

这样两者职责更清晰：

- 摘要是 runtime 自动行为
- note 是 agent 主动记忆行为

---

## 8. 推荐的落地方案

### 8.1 推荐分三期做

#### Phase 1：解耦与最小增强

目标：

- 从 `agent.py` 中抽出 `ContextManager`
- 引入 `TokenBudgetReport`
- 改成“最近窗口 + 旧轮摘要”的双层压缩
- 把工具 schema token 纳入预算

这期完成后，你就已经获得：

- 更干净的结构
- 更可控的预算
- 更稳的长会话行为

#### Phase 2：结构化摘要与元信息保留

目标：

- 摘要输出结构化字段
- 新增 pinned metadata 提取与保留
- 大工具输出裁剪
- note_tool 接入 recall 管理

这期完成后，上下文管理会从“能压缩”升级为“有治理能力”。

#### Phase 3：轻量召回与校准

目标：

- 引入关键词 / FTS 召回
- usage-based token calibration
- 更细粒度的 compaction policy 组合

这期完成后，系统就开始接近“真正的长会话 runtime”。

### 8.2 推荐修改的文件范围

建议的变更集合大致如下：

- 新增：
  - `mini_agent/context/manager.py`
  - `mini_agent/context/budget.py`
  - `mini_agent/context/compaction.py`
  - `mini_agent/context/summarizer.py`
  - `mini_agent/context/recall.py`
  - `mini_agent/context/models.py`

- 修改：
  - `mini_agent/agent.py`
  - `mini_agent/schema/schema.py`
  - `mini_agent/tools/note_tool.py`

- 新增测试：
  - `tests/test_context_budget.py`
  - `tests/test_context_compaction.py`
  - `tests/test_context_summary.py`
  - `tests/test_context_recall.py`

---

## 9. 技术难度评估

### 9.1 综合难度

综合难度：**高**

这是一个“看起来像优化，实际上在重构 runtime”的改进项。

### 9.2 为什么难

主要难点不在编码量，而在设计正确性：

1. **压缩后是否还保留任务完成能力**
2. **不同 provider 下预算如何统一表示**
3. **哪些信息必须钉住，哪些可以摘要**
4. **摘要质量不稳定时如何 fallback**
5. **召回内容如何避免噪音反向污染主上下文**

### 9.3 难度拆分

- Token 预算系统：中等偏上
- 结构化摘要：中等
- 多策略压缩：高
- 召回系统：中等偏上
- 与现有 Agent 主循环集成：高

---

## 10. 风险与注意事项

### 风险 1：为了“更智能”把系统做得过重

如果一开始就同时做：

- provider-aware token
- 结构化摘要
- FTS 召回
- 向量召回
- 持久化快照

很容易超出这个仓库当前的复杂度承受范围。

建议先做“局部正确且可解释”的版本。

### 风险 2：摘要质量不稳定导致行为回退

摘要一旦丢了：

- 当前约束
- 活跃文件
- 未完成事项

Agent 的后续表现会明显变差。

所以摘要设计必须允许：

- 保底字段
- 严格 fallback
- 关键元信息不走普通摘要路径

### 风险 3：召回过量

召回系统不是召回越多越好。

如果把过多旧信息回填到主上下文，等于压缩完又把预算重新吃满。

召回也必须受预算约束。

### 风险 4：上下文管理和 planner/todo 将来割裂

你后续还计划做显式 planner/todo/progress。

因此从一开始就要预留：

- `pinned metadata` 中可挂载 todo/progress
- 压缩时默认保留活跃计划状态

否则后面还要二次返工。

---

## 11. 验证与测试建议

### 11.1 必须验证的行为

至少要覆盖以下测试场景：

1. **短会话不应误压缩**
2. **长会话应稳定触发压缩**
3. **压缩后用户目标仍保留**
4. **压缩后最近窗口仍保留**
5. **大工具输出会被优先裁剪**
6. **note_tool 中关键事实可被召回**
7. **不同 provider 下预算报告格式一致**
8. **usage 缺失时本地估算仍能工作**
9. **摘要失败时 fallback 不会中断主流程**

### 11.2 推荐增加的观测指标

建议在日志中增加：

- 每轮发送前总 token 估算
- system / messages / tools / recall 的分项 token
- 本轮是否触发压缩
- 触发了哪些压缩策略
- 压缩前后 token 对比
- 摘要生成耗时
- 召回条目数与 token 开销

这些指标对后续调优非常重要。

---

## 12. 与后续两项改进的关系

这一项不是孤立的，它会直接影响后续两个改进的设计。

### 对模型回退机制的影响

不同模型上下文窗口不同，因此模型池切换后：

- TokenBudgetReport 必须支持不同窗口大小
- 压缩阈值必须动态调整
- 某些 fallback 模型可能需要更激进压缩

### 对 planner/todo/progress 的影响

planner/todo/progress 将天然成为 pinned metadata 的重要来源：

- 当前任务目标
- 当前步骤
- 已完成事项
- blocker

所以本设计最好把“固定保留元信息”作为一等概念提前做好。

---

## 13. 最终建议

如果只给一个判断：

**这项改进值得做，而且应该优先于显式 planner 之前打底。**

原因很简单：

- planner/todo 会增加更多状态
- 模型池会引入不同上下文窗口
- 如果没有更稳的上下文治理层，后面两项越做越容易混乱

我建议的实际执行顺序是：

1. 先抽出 `ContextManager` 和 `TokenBudgetReport`
2. 再做“最近窗口 + 旧轮摘要 + 工具输出裁剪”
3. 再做 pinned metadata
4. 最后再补轻量 recall

这条路径最稳，也最符合当前仓库的复杂度水平。

---

## 14. 一句话总结

这项改进的本质，不是“把摘要做得更花”，而是：

**把 `Mini-Agent` 从“有长上下文保护的 Demo”升级为“有上下文治理能力的 Agent Runtime”。**

------- s06 总结

s06 Context Compact 分析与业界对比
s06 的三层架构回顾
s06 采用了三层压缩管道：

层级	触发时机	策略
Layer 1: micro_compact	每轮静默执行	将旧 tool_result 替换为占位符，保留最近 3 个和 read_file 结果
Layer 2: auto_compact	token > 50000 时	LLM 摘要，替换全部历史，原始记录存磁盘
Layer 3: compact tool	模型主动调用	同 Layer 2，但由模型自主判断何时触发
这是一个优秀的教学架构，清晰地展示了分层压缩的核心理念。但与生产级实现相比，有明显可改进之处。

s06 的具体改进点
1. Token 估算太粗糙（s06_context_compact.py:63-65）


def estimate_tokens(messages: list) -> int:
    return len(str(messages)) // 4  # 误差可达 30-50%
改进：使用 tiktoken（OpenAI）或 Anthropic 的 count_tokens API 做精确计算
Aider 默认用 Llama2 tokenizer 做保守估算，宁可高估也不低估
2. 摘要后丢失了关键上下文（s06_context_compact.py:125-127）

压缩后只剩一条 user 消息，没有保留最近的对话
改进：像 Aider 那样保留最近 N 轮对话（tail），只压缩旧的部分
3. 没有 tool_use/tool_result 配对保护

如果在 auto_compact 边界处截断，可能破坏 tool 调用对
OpenClaw 明确保证压缩边界不会拆分 tool 调用和返回
4. 缺少压缩前的记忆持久化

OpenClaw 在压缩前会提示 agent 先把关键信息写入磁盘文件
这样即使摘要质量不好，关键信息也不会丢失
5. 摘要质量没有验证

完全依赖 LLM 一次性生成摘要，无校验机制
Aider 使用递归分割：如果摘要+尾部仍然超限，最多递归 3 次继续压缩
业界主流方案对比
项目	策略	亮点
Aider	递归 LLM 摘要	将消息分 head/tail，只摘要 head；超限时递归压缩最多 3 次；后台线程执行
Cline	两级：文件读取去重 → 截断/LLM 摘要	先去重同一文件的多次读取；新版 auto-condense 摘要后自动重新读取最多 8 个关键文件
OpenClaw	可插拔 context engine	插件式架构，支持 DAG 摘要、向量检索等自定义策略；压缩前 flush 记忆到磁盘；/compact "focus" 可引导摘要方向
Opencode	Go 实现的 LLM 摘要	摘要聚焦四个维度：已完成操作、当前工作、修改文件、后续任务
Continue	纯剪枝，无摘要	最简方案——从头部删除旧消息直到 fit，始终保留 system prompt 和最后一轮
给你自己项目的建议
如果你要为自己的 agent 实现 context compact，从 s06 出发可以做这些改进：

保留尾部对话 — 压缩时只摘要前半部分，保留最近 N 轮完整对话（Aider 做法）
精确 token 计算 — 用 tokenizer 替代 len//4
压缩前持久化 — 在摘要前让 agent 把关键状态写入文件（OpenClaw 做法）
文件读取去重 — 同一文件被多次读取时只保留最新一次（Cline 做法）
摘要后重新加载关键文件 — Cline 的 auto-condense 会在摘要中识别 "Required Files" 并自动重新读取
tool 配对保护 — 确保压缩边界不拆分 tool_use 和 tool_result
这些改进可以按优先级逐步加入，其中 1（保留尾部）和 4（文件去重）投入产出比最高。