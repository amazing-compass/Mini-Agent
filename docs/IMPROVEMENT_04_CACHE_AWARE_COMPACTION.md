# IMPROVEMENT 04 — Cache-Aware Compaction (v1)

> **范围声明**：本文档**只**讨论两项改进——
> 1. Cache-Aligned Summarization（缓存对齐摘要）
> 2. 基于经济学的 DP 压缩决策
>
> 不涉及 session 持久化、stream-json、三层 skill、ACP、router 改造等其它议题。
>
> **这是 v1（简化版）设计**，目标是"实现简单 + 完成功能 + 无 bug"。
> 当前主目标 provider 是 **DeepSeek V4 Pro API**。DeepSeek 使用自动 Context Caching，
> 不依赖 Anthropic 的显式 `cache_control` breakpoint；本文只在兼容官方 Anthropic
> endpoint 时保留显式 breakpoint 逻辑。
> 凡是用了"简化优于极致"取舍的地方，章节里标注 **[v1 简化]**，并写明保留了哪种 v2 优化空间。
>
> 实现 agent 在阅读本文档前**必须先读完整篇**，再开始动代码。
> 涉及的所有已有代码位置都写明了 `file:line`，请按 §4 代码改动定位逐条对照。

---

## 1. 背景与动机

### 1.1 现状问题

当前 mini-agent 的上下文压缩走一条三级阶梯（[mini_agent/agent.py:289](../mini_agent/agent.py#L289) `_compress_context`）：

| 级别 | 触发条件 | 动作 | 对 prompt cache 的影响 |
|---|---|---|---|
| L1 | `estimated > 85% × token_limit` | 把旧 round 的非 `read_file` tool_result **原地改写**为 `[Previous {name} ...]` 占位 | **破坏 cache 前缀** — 改写已发送过的字节，后续所有请求 cache miss |
| L2 | L1 不够 | 把旧 round 的 `read_file` 结果**原地改写**为 `[Previous read_file: {path}]` | 同 L1 |
| L4 | `estimated > token_limit` | LLM 生成结构化 summary，追加到 `cold_summaries`，`live_messages` 保留最近 N round | summary 段变化触发 cache miss，但这是离散事件 |

三个根本缺陷：

1. **L1 / L2 与 prompt caching 直接冲突**：原地改写 `live_messages` 里**已经发送过**的 tool_result。DeepSeek 的自动 Context Caching 和官方 Anthropic 的显式 prompt cache 都依赖稳定前缀；每次 L1 / L2 触发都会让此后请求更容易 cache miss。等于"用一个免费动作换来此后 N 次更贵输入"——DeepSeek 即使没有 `cache_control`，也不能把旧消息原地改掉。
2. **固定阈值（85% / 100%）+ 固定保留量（`keep_recent_n=3`）没有经济模型支撑**：与任务剩余长度、缓存价比、单条 tool_result 大小都无关。
3. **L4 的 summary 调用前缀和主请求前缀不对齐**：[mini_agent/agent.py:542-545](../mini_agent/agent.py#L542) 用独立的 `summary_messages` 数组（只有 system + user 两条），完全不复用主请求前缀，每次 compact 都按全价重发被丢弃的历史。

### 1.2 改进目标

把压缩主路径从"原地改写 + 阈值触发"换成"DP 经济决策 + cache-aligned summary"，达成：

1. **不破坏 prompt cache 前缀**：`live_messages` 只有 append 和切片，**永不原地改写**。
2. **不切断 tool_call / tool_result 协议**：切分必须落在 user-round 边界。
3. **只在经济上划算或容量上必须时 compact**：用 4 项 NetBenefit 公式决策。
4. **compact 后保留最近 round，旧 round 进 `current_summary`**：保留 `ContextSummary` 五段式 schema。
5. **Summary 调用尽量复用主请求稳定前缀**：DeepSeek 走自动 prefix cache，summary call 的 system / tools / dropped 前缀尽量与主请求一致；Anthropic 官方端点则额外使用显式 breakpoint。dropped 部分**不**做强命中假设（v1 保守）。

### 1.3 衡量指标 [v1 目标]

| 指标 | v1 期望 | v2 目标（将来） |
|---|---|---|
| 主请求平均 `TokenUsage.cache_read_tokens / prompt_tokens` | DeepSeek 下记录并观察上升；官方 Anthropic 可按 ≥ 60% 作为参考 | ≥ 75% |
| Summary 调用 `TokenUsage.cache_read_tokens / prompt_tokens` | DeepSeek 不设硬阈值；官方 Anthropic 不强约束（≥ 10% 即可） | ≥ 80%（v2 加 anchor 优化时） |
| 主路径上 `live_messages` 被原地改写次数 | **0**（emergency content truncation 不计） | 同 |

v1 指标偏低是因为我们**主动放弃 dropped 部分的 cache 命中假设**（详见 §3.5）；换取实现简单、不会因为命中假设破灭导致反优化。

---

## 2. 范围（in-scope / out-of-scope）

### 2.1 In-scope（本次改）

- 删除 `_compress_context` 主路径里的 L1 / L2 触发分支
- 删除 `_truncate_old_tool_results` / `_truncate_old_readfile_results` 两个函数
- 重写 `_full_compress` 为 `_run_cache_aligned_compaction()`
- 重写 `_create_structured_summary` 为 cache-aligned 形态
- 删除 `_rebuild_system_prompt` 和 `self.system_prompt` 字段；pinned notes 只在新的 `_render_system_blocks` 里渲染
- 新增 `mini_agent/compaction/` 包（`CompactionPolicy` + `CachePolicy` + `ModelPricing` + `CompactionSnapshot` + `CompactionDecision`）
- 升级 `TokenUsage` schema：新增 `cache_read_tokens` / `cache_creation_tokens` / `cache_miss_tokens` 字段，`prompt_tokens` 语义不变；DeepSeek 的 `prompt_cache_hit_tokens` 映射到 `cache_read_tokens`
- 升级 `AnthropicClient`：支持 `system` 字段是 list[dict] 形式；处理 tools 末尾的 `cache_control`
- 升级 `OpenAIClient`：显式 flatten `system: list[dict]` → `str`，忽略 `cache_control`
- 升级 `ModelRouter`：新增 `peek_primary_node()` 只读方法
- 修改 `_generate_with_overflow_recovery`：去掉 L1/L2 chain，只保留 forced compaction + emergency content truncation
- 修改 `_add_tool_message`：加 ingest 时的内容截断（**不是** L1/L2，详见 §3.3）
- **`cold_summaries: list[ContextSummary]` → `current_summary: ContextSummary | None`** [v1 简化]

### 2.2 Out-of-scope（本次**不**改）

- **不**改 session 持久化（current_summary 仍存内存、`/clear` 行为不变）
- **不**改 router 内部熔断 / TokenBudget / 三桶分类逻辑
- **不**改 ACP / CLI 渲染
- **不**改 tool 自身实现（除 §3.3 ingest 截断这一处）
- **不**给 `Message` schema 加 `cache_control` 字段——cache_control 是请求渲染时的临时元数据，挂到 dict 上即可，不进入 Message 持久状态 [v1 简化]
- **不**实现 stream-json 输出
- **不**实现三层 skill 机制
- **不**追踪 BP #4 anchor index、**不**让 DP 决策器感知 cache anchor [v1 简化，详见 §3.5]
- **不**做 list+cap+sliding merge 形式的 cold summaries 管理 [v1 简化]
- **不**实现 provider 私有 cache 控制 API；DeepSeek 只读取 hit/miss usage 并保持稳定前缀

### 2.3 关键概念区分（务必读懂再开工）

实现者最容易把以下三件事混淆。它们是**完全不同**的动作，发生在**完全不同**的生命周期阶段：

| 动作 | 何时触发 | 改的对象 | 对 cache 前缀的影响 | 本次是否保留 |
|---|---|---|---|---|
| **L1 / L2 占位替换** | `_compress_context` 主路径，soft_limit 命中 | `live_messages` 里**已经发送过**的旧 tool_result（原地 mutate） | 破坏 | **删除** |
| **Ingest 截断** | tool `execute()` 返回**之后**、append 到 `live_messages`**之前** | 即将进入 `live_messages` 的**新** tool 结果（从未发送过） | 零影响（cache 还没见过它） | **保留并强化** |
| **DP-driven compact** | 主循环每 step 头部 + `safe_generate` 的 ContextOverflow 恢复路径 | `live_messages` 切片 → `self.current_summary = new_summary` | 切片不改字节，current_summary 变化触发 1 次 cache 重建（DP 公式已计入） | **新增主路径** |

**关键 invariant**：本次改进完成后，**`live_messages` 里已经存在的 `Message` 对象在任何情况下都不再被原地修改**（不修改 `content`、不修改 `tool_calls`、不修改任何字段）。所有"压缩"通过把 message 移出 `live_messages`、把摘要写入 `current_summary` 实现。

唯一例外：emergency 路径下，当强制 compact 后仍超 limit 时，允许 `_content_truncate_large_tool_results()` 对**当前 round 的超大 tool_result** 做截断——这条路径只在 ContextOverflowError 兜底后还放不下时才走，正常 DP 路径永远碰不到。

---

## 3. 最终设计

### 3.1 请求结构与 cache 布局（DeepSeek 优先）

DeepSeek V4 Pro 的 cache 是**自动 Context Caching**：只要请求前缀稳定，服务端会 best-effort 命中，并在 usage 中返回 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。它的 Anthropic-compatible endpoint 会**忽略** `cache_control` 字段；因此本文后面提到的 BP #1/#2/#3/#4 只对官方 Anthropic endpoint 有实际意义。

对 DeepSeek，核心 invariant 是：**稳定内容尽量靠前，动态内容尽量靠后，不原地改写已发送消息**。

**阅读顺序提示**：v1 主目标是 DeepSeek（自动 cache，不打显式 BP）。本节里所有 BP #1/#2/#3/#4 的设计只在 `supports_explicit_cache_control=True` 时启用，例如官方 Anthropic endpoint；DeepSeek 节点必须保持 `supports_explicit_cache_control=False`。

#### 3.1.1 主请求 system block 顺序（按"改动频率从低到高"）

```python
system=[
    {"type": "text", "text": BASE_SYSTEM_PROMPT,
     "cache_control": {"type": "ephemeral"}},     # ← Anthropic only: BP #1
    # ↑ Workspace 信息 + tool-use guide + skill-index 等永久不变内容

    {"type": "text", "text": PINNED_NOTES_TEXT},  # 无 BP，跟随 #2 一起失效
    # ↑ Session Note Tool 写入，极低频

    {"type": "text", "text": CURRENT_SUMMARY_TEXT,
     "cache_control": {"type": "ephemeral"}},     # ← Anthropic only: BP #2
    # ↑ compact 触发时整段替换，低频

    {"type": "text", "text": CURRENT_PLAN_TEXT},  # 无 BP，全价但小
    # ↑ TodoWrite 每几轮变化，高频；放最后避免污染 BP #2
]
```

**为什么 plan 必须放最后**：TodoWrite 是变更最频繁的来源。若 plan 放在 current_summary 前面，官方 Anthropic 端点下每次 TodoWrite 都让 BP #2 失效——而 current_summary 通常 500-1500 token，是 system 里最大的一块，**血亏**。plan 自身 < 200 token，全价可接受。

**DeepSeek caveat**：DeepSeek 没有显式 BP，plan 只要变化，就可能让 plan 之后的 messages 前缀 miss。v1 为了少改状态流，仍把 plan 放在 system 最后；DP 和日志不要假设 plan 变化后 messages 一定 cache hit。若后续要极致优化 DeepSeek cache，再把 plan 移到 render-time 的尾部临时上下文，不进入稳定 system 前缀。

#### 3.1.2 主请求 tools 数组

```python
tools=[
    Tool(name="read_file", ...),
    ...
    Tool(name="last_tool", ...,
         cache_control={"type": "ephemeral"})    # ← BP #3
]
```

`cache_control` 挂在 tools 数组**最后一个 tool dict 的顶层字段**上（不是 `input_schema` 内部）。仅当 `node.supports_explicit_cache_control=True` 时注入；DeepSeek 默认不注入，因为官方文档说明该字段会被忽略。

#### 3.1.3 主请求 messages 数组

```python
messages=[
    # ... 前面所有 round 不打 cache_control ...
    {"role": "assistant", "content": [
        {"type": "text", "text": "...",
         "cache_control": {"type": "ephemeral"}}  # ← Anthropic only: BP #4
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "...", "content": "..."}
    ]},
    # ↑ 本轮新内容，无 BP，全价
]
```

**BP #4 的位置（仅 Anthropic explicit cache）**：打在**上一次 LLM 调用产出的 assistant message 的最后一个非 thinking content block 上**（最稳定的边界）。下一次 LLM 调用时，到 BP #4 为止的字节可 cache_read。

**BP #4 的实施**：在 `AnthropicClient` 转换 messages 时**动态决定**——找到 messages 数组里最后一条 assistant message，给它最后一个非 thinking content block 挂 `cache_control`。**不**修改 mini-agent 的 `Message` 对象。DeepSeek 下该逻辑默认关闭。

#### 3.1.4 Summary 请求与主请求的差别 [v1 关键设计]

官方 Anthropic endpoint 下，Summary 请求只打 **BP #1 / #2 / #3**，**不**在 dropped messages 上打 BP #4。DeepSeek 下不打任何显式 BP，依赖自动 prefix cache。

| Breakpoint | 主请求 | Summary 请求 |
|---|---|---|
| #1 system base 末 | ✓ 打（Anthropic） | ✓ 打（Anthropic） |
| #2 current_summary / pinned 末 | ✓ 打（Anthropic） | ✓ 打（Anthropic） |
| #3 tools 末 | ✓ 打（Anthropic） | ✓ 打（Anthropic） |
| #4 上轮 stable assistant | ✓ 打 | **✗ 不打** |

**[v1 简化] 为什么 summary 请求不打 BP #4**：

DP 选出的切分点 k 不保证就是上次主请求 BP #4 的位置。若打 BP #4 在 dropped 末尾、而该位置没有匹配的 cache entry：
- Anthropic 会在该位置 `cache_write`（按 1.25× input 价计费）
- 这个新写入的 cache entry **没有任何后续请求会读它**（summary call 是一次性的、主请求不会包含 summary instruction）
- 等于纯亏 1.25× input 价

不打 BP #4：
- 官方 Anthropic endpoint 仍会尝试 implicit prefix matching（隐式前缀匹配），最多回看约 20 个 content block。如果 dropped 末尾靠近上次 BP #4，可能 partial hit
- 即使完全 miss，dropped 部分按 input 价计费（1.0×），不会有 cache_write 升价
- **永远不会反优化**

DeepSeek 下这条更简单：`cache_control` 本来就会被忽略，summary call 只需要保持前缀稳定，让服务端自动 Context Caching best-effort 命中。

v2 优化空间：将来引入 anchor 追踪后，可以判断"k 是否在 cache 覆盖范围内"，命中时才打 BP #4。v1 不做。

#### 3.1.5 Anthropic 上限：4 个 breakpoint 共享

> "If 4 explicit block-level breakpoints already exist, the API returns a 400 error."
> — Anthropic docs

官方 Anthropic endpoint 下：主请求最多打 4 个；summary 请求打 3 个。DeepSeek 下不注入显式 breakpoint。

#### 3.1.6 cache 层次：tools → system → messages

> "Cache prefixes are created in the following order: tools, system, then messages."

失效语义：
- tools 改 → tools + system + messages cache 全失效
- system 改 → system + messages 失效（tools 仍命中）
- messages 改 → 只有改动点之后的 messages 失效

我们的顺序天然顺这条规则走。

#### 3.1.7 Anthropic 最小可缓存长度

Sonnet / Opus 系列 BP 前的累积内容 < 1024 token 时，BP 被**静默忽略**。Haiku 是 2048 token。

启动初期 base_system_prompt 可能不到 1024。处理方式：**始终打 BP，让 Anthropic 自己忽略**——浪费一次 API 字段无影响，下次系统提示长起来后自动生效。

---

### 3.2 状态模型

mini-agent 现有状态分层（[mini_agent/agent.py:109-114](../mini_agent/agent.py#L109)）调整：

| 字段 | v0 当前 | v1 改动 |
|---|---|---|
| `_base_system_prompt: str` | 保留 | 保留——继续是稳定系统提示，workspace info 拼到这里 |
| `system_prompt: str` | base + pinned 渲染后的副本 | **删除** —— pinned notes 不再持久化进 system_prompt |
| `pinned_notes: list[dict]` | 保留 | 保留 |
| `cold_summaries: list[ContextSummary]` | 多次 compact 产物 | **删除**，替换为下面的 `current_summary` |
| `current_summary: ContextSummary \| None` | — | **新增** [v1 简化] single rolling summary |
| `live_messages: list[Message]` | 保留 | 保留，但永不再原地 mutate |
| `_skip_next_token_check: bool` | 存在 | **删除** —— DP 是单点决策，不需要这个 hack |
| `api_total_tokens: int` | 存在 | **保留** |
| `last_usage: TokenUsage \| None` | — | **新增**，缓存上一次 API 返回的真实 usage（含 cache 细分） |
| `compact_count: int` | — | **新增**，DP 公式 ④ 项 `c` 用 |
| `llm_call_count: int` | — | **新增**，估算 `L`（每轮平均 LLM 调用数）用 |
| `user_turn_count: int` | — | **新增**，估算 `E`（预期剩余轮数）用 |
| `_primary_node: ModelNode \| None` | — | **新增**，`__init__` 时从 router 缓存一次，供 pricing 查询 |
| `compaction_policy: CompactionPolicy` | — | **新增**，DP 决策器实例 |

**Message schema**：[mini_agent/schema/schema.py:29-37](../mini_agent/schema/schema.py#L29) **不**新增字段。cache_control 是请求渲染时的临时元数据，由 `AnthropicClient._convert_messages` 在生成 content blocks 时**就地**挂上，不写回 Agent 状态。

---

### 3.3 Ingest 时的 tool_result 内容截断（非 L1/L2，不要混淆）

#### 3.3.1 这件事必须保留

考虑场景：用户让 agent 跑 `find / -name "*.log"`，输出 800KB。如果不在 ingest 时截断：
- 完整 800KB 进 `live_messages` → 落在 kept 区（最近 round）
- DP 算 compact 收益时，K（kept token）暴涨到 200K+
- 失效成本项爆表 → DP 一定说 NO_OP
- 而且 K 占了 kept 区，**DP 没办法通过 compact 救它**（不能丢最近的 round，否则当前任务断了）
- → context 立刻超 limit → forced compact → 还是塞不下 → emergency truncate 救火
- emergency 截断时要改写**已经在 `live_messages` 里**的 message → **回到我们刚批判的 L1/L2 反模式**

所以：**单条超大 tool_result 必须在进入 `live_messages` 之前就截掉**。这不是"压缩策略"，是"输入卫生"。

#### 3.3.2 实现位置

在 [mini_agent/agent.py:186](../mini_agent/agent.py#L186) `_add_tool_message` 内做：

```python
# Constant in Agent class (replaces existing CONTENT_TRUNCATE_KEEP_CHARS).
# 命名: 用 CHARS 不是 BYTES, 因为 Python len(str) 是字符数。
# ASCII 字符: 1 char = 1 byte; 中文/emoji: 1 char = 3-4 bytes UTF-8。
# 我们截的是字符数 — 对英文/代码场景没差, 对中文场景实际生成的字节数会是 char 数的 ~3 倍。
# 选 50_000 char 是为了在英文为主的典型工作流下保证 ~12K token 上限;
# 中文密集场景生成的 message 可能 token 数翻 3 倍, 这是已知 trade-off。
# 如果要按"真实字节数"对齐, 改用 len(content.encode("utf-8")) 即可。
MAX_TOOL_RESULT_CHARS = 50_000

def _add_tool_message(self, tool_call_id: str, function_name: str, result: ToolResult) -> Message:
    """Add a tool result message; truncate at ingest if oversized."""
    content = result.content if result.success else f"Error: {result.error}"

    # Ingest-time truncation — NOT L1/L2 mutation.
    # Operates on content that hasn't entered cache yet.
    if len(content) > self.MAX_TOOL_RESULT_CHARS:
        original_len = len(content)
        keep_head = int(self.MAX_TOOL_RESULT_CHARS * 0.7)
        keep_tail = self.MAX_TOOL_RESULT_CHARS - keep_head - 200
        head = content[:keep_head]
        tail = content[-keep_tail:]
        content = (
            f"{head}\n\n"
            f"...[truncated {original_len - keep_head - keep_tail} chars from middle; "
            f"original {original_len} chars; "
            f"if you need more, use Read with offset/limit, or re-run the tool with narrower scope]\n\n"
            f"{tail}"
        )

    msg = Message(
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
        name=function_name,
    )
    self.live_messages.append(msg)
    return msg
```

**关键设计点**：
- 这条 message 从被构造开始就是截断版，**永远以这个字符序列存在**，第一次发出去时被 cache 哈希的也是这个版本
- 50_000 char 选自 bash-agent 实测值；可以做成 config 项 `tool_result_max_chars`
- 头部 70% + 尾部 30%：bash 输出常见"开头有命令信息、末尾有真实关键结果"，掐中间最安全
- ASCII / 代码场景：1 char ≈ 1 byte ≈ 0.25 token，50K char ≈ ~12K token
- 中文密集场景：1 char ≈ 3 bytes UTF-8 ≈ 1 token，50K char ≈ ~50K token——这种场景下截断会偏宽松，但中文 tool_result 在 agent 任务里少见

#### 3.3.3 `_content_truncate_large_tool_results` 的处置

[mini_agent/agent.py:417](../mini_agent/agent.py#L417) 这个函数**保留**，但只在 emergency 路径调用（§3.6）。它仍然原地改写 message，是 cache 杀手——但我们承认：**当强制 compact 后还塞不下时，cache 失效已经不是首要矛盾，能发出去才是**。这是兜底的兜底，不是常规路径。

---

### 3.4 DP NetBenefit 决策器 [v1 悲观版]

#### 3.4.1 公式 [v1 简化]

对每个候选切分点 k（user-round 边界），计算：

$$
\text{NetBenefit}(k) = \text{future\_savings} - \text{prefix\_rewrite\_cost} - \text{summary\_call\_cost} - \text{information\_loss\_cost}
$$

各项展开：

```
future_savings        = R × P_cache_read × max(H - S, 0)
prefix_rewrite_cost   = (P_cache_write - P_cache_read) × (S + K)
summary_call_cost     = P_summary_input × (V + H) + P_input × L_instr + P_out × S
information_loss_cost = β × (1 - r^(c+1)) × R × avg × P_input

v1 关键参数:
  P_summary_input = P_input
  # 推导: v1 在 dropped 上不打 BP. Anthropic 计费规则:
  #   - 打 BP + cache miss  -> cache_write 价 (1.25× input)
  #   - 不打 BP + cache miss -> input 价 (1.0×, server 不写 cache entry)
  # 所以 dropped 在最坏情况(无 implicit match)走 input, 不是 cache_write.
  # 任何 implicit match 都是白送的折扣, 公式悲观假设没有.
```

**符号含义**（token 单位、价格 USD/1M token）：

| 符号 | 含义 | v1 来源 |
|---|---|---|
| $R = E \times L$ | 预期剩余 LLM 调用总次数 | 由 $E, L$ 派生 |
| $E$ | 预期剩余用户输入轮数 | `max(BASELINE_E - user_turn_count, BASELINE_E // 2)`，单调非递增 |
| $L$ | 每轮用户输入平均 LLM 调用次数 | `llm_call_count / max(user_turn_count, 1)`；冷启动用 `DEFAULT_L` |
| $H$ | 被丢弃的旧消息 token 数 | 遍历 k 计算 |
| $K$ | 保留下来的最近 messages token 数 | 遍历 k 计算 |
| $S$ | 固定摘要长度（输出预算） | 500 |
| $V$ | 固定前缀（base + pinned + current_summary + tools） | 实测，调用决策器前算一次 |
| $L_{\text{instr}}$ | summary 指令长度 | ~200 |
| $c$ | session 累计 compact 次数 | `self.compact_count` |
| $\beta$ | 信息失真权重 | 0.3 |
| $r$ | 失真衰减率，$r_t = r^{c+1}$，上限 0.63 | 0.9 |
| $\text{avg}$ | 平均每条 message token 数 | `H / count(dropped messages)` |
| $P_{\text{input}}, P_{\text{cache\_read}}, P_{\text{cache\_write}}, P_{\text{out}}$ | 当前 model 输入价、缓存读价、缓存写价、输出价 | 由 `CachePolicy.pricing_for_node(node)` 提供 |

**DeepSeek 价格映射**：DeepSeek 没有 Anthropic 式显式 cache write surcharge。对 DeepSeek V4 Pro，`P_input` 取 cache miss 价，`P_cache_read` 取 cache hit 价，`P_cache_write` 也取 cache miss 价（仅用于复用 DP 公式里的"compact 后新 prefix 首次 miss"成本）。

**为什么悲观**：
- `future_savings` 用 `P_cache_read`（最优情况下我们以后真省的钱）
- `prefix_rewrite_cost` 用 `P_cache_write - P_cache_read`（compact 之后新 prefix 第一次主请求时要按 cache_write 算）
- `summary_call_cost` 用 `P_input` 覆盖 V+H（最悲观：v1 在 dropped 不打 BP，cache miss 时按 input 价计费，**不是** cache_write——因为没打 BP 就不会触发 cache write）
- `information_loss_cost` 末项用 `P_input`（衰减后多花的推理 token，按全价算）

**取舍**：DP 错向"少 compact"无害——最多错过边际正收益机会；错向"多 compact"有害——可能净亏损。v1 一律悲观。

#### 3.4.2 决策算法（伪代码）

```python
@dataclass
class CompactionDecision:
    should_compact: bool
    keep_round_count: int           # 决定保留多少个 user-round
    drop_message_count: int         # = boundary index (用于切片)
    reason: str                     # "net_positive" | "force_threshold" | "force_overflow" | "no_benefit" | "nothing_to_drop"
    net_benefit: float              # 最大净收益
    forced: bool

@dataclass
class CompactionSnapshot:
    """决策器只读的状态快照，纯函数输入。"""
    live_messages: list[Message]
    current_summary: ContextSummary | None
    system_token_count: int         # base + pinned + current_summary 当前 token 数
    tools_token_count: int          # tools schema 当前 token 数
    pricing: ModelPricing           # P_input / P_cache_read / P_cache_write / P_out
    user_turn_count: int
    llm_call_count: int
    compact_count: int
    api_input_token_estimate: int   # current render_for_provider()+tools estimate
    max_context: int

class CompactionPolicy:
    BASELINE_E = 20
    DEFAULT_L = 5
    BETA = 0.3
    R_DECAY = 0.9
    SUMMARY_SIZE = 500
    L_INSTR = 200
    MIN_DROP_TOKENS = 1000
    HARD_THRESHOLD = 0.90

    def decide(
        self,
        snapshot: CompactionSnapshot,
        *,
        forced: bool = False,
    ) -> CompactionDecision:
        # —— Step 1: forced 路径(来自 ContextOverflowError 或 90% 阈值) ——
        if forced or snapshot.api_input_token_estimate > self.HARD_THRESHOLD * snapshot.max_context:
            return self._force_compact(snapshot)

        # —— Step 2: 列举候选切分点 ——
        boundaries = self._user_round_boundaries(snapshot.live_messages)
        if len(boundaries) < 2:
            return self._noop("nothing_to_drop")

        # —— Step 3: 计算预期剩余调用数 R ——
        E = max(self.BASELINE_E - snapshot.user_turn_count, self.BASELINE_E // 2)
        L = (snapshot.llm_call_count / snapshot.user_turn_count) if snapshot.user_turn_count > 0 else self.DEFAULT_L
        R = max(E * L, 1)

        # —— Step 4: 价格 ——
        P_in       = snapshot.pricing.input
        P_cache_r  = snapshot.pricing.cache_read
        P_cache_w  = snapshot.pricing.cache_write
        P_out      = snapshot.pricing.output
        P_summary  = P_in                        # [v1 关键: dropped 不打 BP, miss 时走 input 价(非 cache_write)]

        # —— Step 5: DP 遍历 ——
        best: tuple[int, float, int] | None = None  # (drop_count, net_benefit, keep_round_count)

        V = snapshot.system_token_count + snapshot.tools_token_count
        S = self.SUMMARY_SIZE

        # 候选切分点: boundaries[1..n-1] (绝不全部丢光,至少留最后 1 round)
        for round_idx in range(1, len(boundaries)):
            k = boundaries[round_idx]
            dropped = snapshot.live_messages[:k]
            kept = snapshot.live_messages[k:]
            H = self._token_count(dropped)
            K = self._token_count(kept)

            if H < self.MIN_DROP_TOKENS:
                continue

            avg = H / max(len(dropped), 1)

            future_savings  = (R * P_cache_r * max(H - S, 0)) / 1_000_000
            rewrite_cost    = ((S + K) * (P_cache_w - P_cache_r)) / 1_000_000
            summary_cost    = (P_summary * (V + H) + P_in * self.L_INSTR + P_out * S) / 1_000_000
            distortion      = (self.BETA * (1 - self.R_DECAY ** (snapshot.compact_count + 1)) * R * avg * P_in) / 1_000_000
            net = future_savings - rewrite_cost - summary_cost - distortion

            keep_round_count = len(boundaries) - round_idx
            if best is None or net > best[1]:
                best = (k, net, keep_round_count)

        if best is None or best[1] <= 0:
            return self._noop("no_benefit")

        return CompactionDecision(
            should_compact=True,
            keep_round_count=best[2],
            drop_message_count=best[0],
            reason="net_positive",
            net_benefit=best[1],
            forced=False,
        )

    def _force_compact(self, snapshot: CompactionSnapshot) -> CompactionDecision:
        """Forced path: ignore NetBenefit, keep only the most recent round."""
        boundaries = self._user_round_boundaries(snapshot.live_messages)
        if len(boundaries) < 2:
            return self._noop("nothing_to_drop")
        k = boundaries[-1]  # drop everything except the most recent round
        is_overflow = snapshot.api_input_token_estimate > self.HARD_THRESHOLD * snapshot.max_context
        return CompactionDecision(
            should_compact=True,
            keep_round_count=1,
            drop_message_count=k,
            reason="force_overflow" if is_overflow else "force_threshold",
            net_benefit=0.0,
            forced=True,
        )

    def _user_round_boundaries(self, messages: list[Message]) -> list[int]:
        """Return indices in `messages` where each user-round starts.

        关键: 必须是 mini-agent 内部 role="user" 的位置, 不是 role="tool"。
        mini-agent 的 Message.role 已经区分了这两者, 直接 filter 即可。
        """
        return [i for i, m in enumerate(messages) if m.role == "user"]

    @staticmethod
    def _encode_len(value: object) -> int:
        """Best-effort token length for v1 DP estimates.

        v1 不追求 provider tokenizer 完全一致；保持和 Agent._estimate_tokens
        同量级即可。实现时可把 encoder 作为 self._encoder 缓存，避免每次取。
        """
        import json
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        if value is None:
            return 0
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return len(enc.encode(text))

    def _token_count(self, messages: list[Message]) -> int:
        """Token count for a subset of live_messages.

        覆盖 content / thinking / tool_calls / tool_call_id；每条 message 加
        一个小的 role/JSON overhead。这个函数只用于比较候选 k，允许 5-10%
        估算误差，但不能留空或依赖上一次 API usage。
        """
        total = 0
        for msg in messages:
            total += 4  # role + JSON framing overhead
            total += self._encode_len(msg.role)
            total += self._encode_len(msg.content)
            total += self._encode_len(msg.thinking)
            total += self._encode_len(msg.tool_calls)
            total += self._encode_len(msg.tool_call_id)
            total += self._encode_len(msg.name)
        return total

    def _noop(self, reason: str) -> CompactionDecision:
        return CompactionDecision(
            should_compact=False, keep_round_count=0, drop_message_count=0,
            reason=reason, net_benefit=0.0, forced=False,
        )
```

#### 3.4.3 设计要点

1. **决策器是纯函数**：`decide(snapshot, forced) -> Decision`。Agent 在调度时构造 snapshot；测试不需要起 Agent。
2. **k 必须落在 user-round 边界**：永远不切断 tool_use ↔ tool_result。mini-agent 内部 `role="tool"` 与 `role="user"` 区分，所以按 `role="user"` 边界天然安全。
3. **DP 评估时机**：每个主循环 step 头部调用一次（替换现 [agent.py:715](../mini_agent/agent.py#L715) `await self._compress_context()`）。
4. **Token 估算精度**：snapshot 构造时用当前 `render_for_provider()+tools` 的 tiktoken 估算。`last_usage.prompt_tokens` 是上一次 LLM call 的真值，不包含之后 append 的 tool_result，只能用于日志校准，不能作为当前 hard-threshold 判定。
5. **极端 P 值的自我保护**：当 provider 没有任何可建模 cache（非 DeepSeek、非 Anthropic、未知模型且无 override），`CachePolicy` 返回 `cache_read == cache_write == input`。此时公式各项：
   - ① `R × P_input × max(H-S, 0)` **仍为正**——"少发 H 个 token"本身就省钱，与 cache 折扣无关
   - ② `(P_cw - P_cr) × (S+K) = 0`——无 cache 折扣自然无失效升价
   - ③ 全程 input 价
   - ④ 正常
   - 结果：DP **仍可能**建议 compact（基于"减少传输 token"维度），只是没有 cache 折扣放大效应。DeepSeek 不走这个退化分支：它虽然没有 explicit `cache_control`，但有自动 Context Caching，应使用 hit/miss 价格。

---

### 3.5 Cache-Aligned Summary 调用 [v1 简化版]

#### 3.5.1 关键 invariant

**Summary call 的 system blocks / tools 数组的 cacheable prefix 应尽量与上一次主请求一致**。DeepSeek 会自动按稳定前缀 best-effort 命中；官方 Anthropic endpoint 则通过 BP #1 (base) / BP #2 (current_summary or pinned) / BP #3 (tools) 辅助命中。dropped messages 原样接在后面，末尾追加一条 user 消息（summary instruction）。

实际"一致"程度：
- BP #1 (base_system_prompt)：永远字节一致（启动后不变）
- BP #2 (current_summary / pinned)：通常一致；**例外**——如果在上次主请求和本次 summary call 之间有 Session Note Tool 写入了 pinned_notes，pinned 段会变化导致 BP #2 hash 改变，cache miss 一次（罕见）
- BP #3 (tools)：通常字节一致（启动后不变；MCP tool schema 热加载变化除外）
- current_plan 段位于 BP #2 之后：plan 变化不影响 BP #2 hash，所以 plan 是否变化对 cache 命中无影响

**summary call 不在 dropped 上打 BP**（详见 §3.1.4）。

#### 3.5.2 结构示例

```python
# 上一次主请求(compact 触发前的最后一次):
#   system   = [base|BP#1][pinned][current_summary_OLD|BP#2][plan]
#   tools    = [t1, ..., t_last|BP#3]
#   messages = [..., asst_prev|BP#4, user_curr, asst_curr, tool_results, ...]
#                                                          ↑ DP 决定从这里切

# Summary call 立刻发起,请求体:
#   system   = [base|BP#1][pinned][current_summary_OLD|BP#2][plan]   ← 稳定顺序一致
#   tools    = [t1, ..., t_last|BP#3]                                 ← 稳定顺序一致
#   messages = dropped_messages 原样 + [user: SUMMARY_INSTRUCTION]
#                                       ↑ 唯一新字节, 全价
#   (不在 dropped 任何位置打 BP #4)
```

效果：
- DeepSeek：system / tools / dropped 共同作为稳定前缀参与自动 Context Caching，命中是 best-effort，不保证 100%
- Anthropic BP #1 / #2 / #3：通常命中（system + tools 字节一致时）
- dropped messages：DeepSeek 靠自动 prefix cache 的完整 prefix unit 命中规则；官方 Anthropic 靠 implicit prefix matching，部分可能命中（最多回看 ~20 block）
- SUMMARY_INSTRUCTION + 输出：全价

最坏情况（dropped 完全 miss）：summary call 仍只比朴素 summary 小幅便宜（稳定 system/tools 可能命中）。DeepSeek 常见收益取决于服务端自动 cache 是否把 dropped 前缀识别为可复用单元；不要在 DP 里强假设。

#### 3.5.3 实现伪代码

```python
async def _run_cache_aligned_summary(
    self,
    dropped: list[Message],
) -> ContextSummary:
    """Issue a summary LLM call whose system/tools prefix matches the most recent main call.

    Invariant:
    - system blocks & tools are passed in the same stable order as the main call
    - dropped messages are passed UNMODIFIED
    - we DO NOT mark cache_control on any dropped message
    - only the trailing summary instruction is new content
    """
    instruction = SUMMARY_INSTRUCTION   # 字符串常量, 见 §3.5.4

    # render_for_provider() 此时 system 部分包含 current_summary_OLD
    # (本函数在 self.current_summary = new 之前调用)
    base_messages = self.render_for_provider()
    system_msg = next((m for m in base_messages if m.role == "system"), None)
    if system_msg is None:
        raise RuntimeError("render_for_provider produced no system message")

    summary_messages: list[Message] = [
        system_msg,
        *dropped,                                          # 原样, 不打 BP
        Message(role="user", content=instruction),
    ]

    tool_list = list(self.tools.values())                  # tools 与主请求一致

    response = await self.router.internal_call(summary_messages, tools=tool_list)
    return self._parse_structured_summary(response.content, dropped)
```

**关键约束**：
- `summary_messages` 里的 `dropped` 是**原对象引用**，不 deepcopy 不 mutate。`AnthropicClient._convert_messages` 在生成请求 dict 时只读取 Message 字段，不修改对象本身。
- BP #4 由 `AnthropicClient` 在 main call 路径动态挂载（找最后一条 stable assistant）；summary call 路径**不**挂 BP #4——`AnthropicClient` 需要区分这两种调用场景。

**[v1 实现选择]** 让 `LLMClientBase.generate()` 接受 `attach_message_bp: bool = True` 与 `enable_cache_control: bool = False`。main call 传 `attach_message_bp=True`，summary/internal call 传 `False`；`enable_cache_control` 完全由 node 的 `supports_explicit_cache_control` 决定。DeepSeek 节点该值为 `False`。

#### 3.5.4 SUMMARY_INSTRUCTION 文案（使用显式分支版本 A）

```python
SUMMARY_INSTRUCTION = """The system prompt above may contain a "Historical Summary" section
covering earlier rounds. The conversation above shows additional rounds that haven't been
summarized yet. Produce an UPDATED structured summary that incorporates BOTH the prior
summary (if present) and the new rounds, in this EXACT format:

## Completed Work
- (list what was done)

## Active Files
- (list files that were read/written/modified, with status)

## Key Findings
- (list important discoveries or facts)

## Pending / TODO
- (list unfinished work or next steps)

Requirements:
- Use the exact section headers above
- Each item starts with "- "
- Be concise, under 800 words total
- English only
- If no prior summary exists, produce a fresh summary covering only the conversation above"""
```

**为什么显式分支**：消除 LLM 行为的不确定性。统一文案（让 LLM 自己看 system 决定要不要合并）有 silent bug 风险——LLM 偶尔抽风一次只摘要 messages 就丢失所有累积 summary。50 个 token 把这种不确定性堵死，零代价。

**文案约束**：作为 session 级常量字符串，不要塞 round number、timestamp 之类的变动信息。

#### 3.5.5 tools 必须传给 internal_call

当前 [agent.py:550](../mini_agent/agent.py#L550)：
```python
response = await self.router.internal_call(summary_messages)   # 缺 tools
```

改成：
```python
response = await self.router.internal_call(summary_messages, tools=tool_list)
```

`internal_call` 在 [router.py:329](../mini_agent/llm/ha/router.py#L329) 已经支持 `tools` 参数，仅 agent 调用方漏传。

#### 3.5.6 Summary 失败的两条路径

```python
async def _maybe_run_compaction(self, tool_list: list, *, forced: bool = False) -> None:
    snapshot = self._build_compaction_snapshot(tool_list)
    decision = self.compaction_policy.decide(snapshot, forced=forced)

    if not decision.should_compact:
        return

    dropped = self.live_messages[:decision.drop_message_count]
    kept = self.live_messages[decision.drop_message_count:]

    try:
        summary = await self._run_cache_aligned_summary(dropped)
    except Exception as exc:
        if forced:
            # 兜底:用 deterministic fallback,仍然丢 dropped。
            # 否则 forced 路径无限循环。
            summary = self._build_deterministic_fallback_summary(dropped, reason=str(exc))
        else:
            # 正常路径:放弃本次 compact,保留 dropped。下次 step 再试。
            print(f"{Colors.BRIGHT_YELLOW}⚠️  Summary failed; deferring compaction: {exc}{Colors.RESET}")
            return

    # [v1 重要] user_goals deterministic 保留:
    # LLM 在生成新 summary 时可能漏掉 prior summary 里的旧 user_goals 条目。
    # 我们在这里强制把 "旧 user_goals + 新 dropped 抽出的 user_goals" 合并去重写回,
    # 不依赖 LLM 自觉。其他字段 (completed_work / active_files / key_findings / pending_todo)
    # 仍由 LLM 输出, 因为它们语义上需要再加工 (合并/去重/状态推进)。
    new_user_goals = [
        m.content for m in dropped
        if m.role == "user" and isinstance(m.content, str)
    ]
    prior_user_goals = self.current_summary.user_goals if self.current_summary else []
    # 保序去重
    seen = set()
    merged_goals = []
    for g in prior_user_goals + new_user_goals:
        if g not in seen:
            seen.add(g)
            merged_goals.append(g)
    summary = summary.model_copy(update={"user_goals": merged_goals})

    self.current_summary = summary           # 整段替换 (single rolling)
    self.live_messages = kept
    self.compact_count += 1

    print(f"{Colors.BRIGHT_GREEN}✓ Compacted {len(dropped)} messages → summary "
          f"(reason={decision.reason}, net_benefit=${decision.net_benefit:.4f}){Colors.RESET}")


def _build_deterministic_fallback_summary(self, dropped: list[Message], *, reason: str) -> ContextSummary:
    """Lossy but non-empty fallback when LLM summary call fails on forced path."""
    user_goals = [m.content for m in dropped if m.role == "user" and isinstance(m.content, str)]
    tool_names = {tc.function.name for m in dropped if m.tool_calls for tc in m.tool_calls}
    file_paths = self._extract_file_paths_from_tool_args(dropped)  # 从 Read/Write/Edit 参数抽取
    return ContextSummary(
        covered_rounds=[self.compact_count + 1],
        user_goals=user_goals,
        completed_work=[f"(LLM summary unavailable: {reason}; below is deterministic fallback)"],
        active_files=sorted(file_paths),
        key_findings=[f"Tools invoked: {', '.join(sorted(tool_names))}"] if tool_names else [],
        pending_todo=[],
        raw_text=...,  # 渲染上述字段
    )
```

---

### 3.6 安全阀（双重）

DP 是局部最优，需要两道兜底：

1. **Soft threshold（DP 内部）**：`api_input_token_estimate > 0.9 × max_context` → `forced=True`，DP 跳过 NetBenefit，直接取 keep_recent=1。
2. **Hard fallback（router-aware）**：`router.call()` 抛 `ContextOverflowError` 时，强制 forced compact + 重试；重试还失败，走 emergency content truncation。

替换 [agent.py:577-635](../mini_agent/agent.py#L577) 的恢复流程：

```python
async def _generate_with_overflow_recovery(self, tool_list: list) -> Any:
    try:
        return await self.router.call(messages=self.render_for_provider(), tools=tool_list)
    except ContextOverflowError as exc:
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  ContextOverflow: {exc}. Forcing compaction...{Colors.RESET}")

    # 1. Forced compact via DP.
    await self._maybe_run_compaction(tool_list, forced=True)

    try:
        return await self.router.call(messages=self.render_for_provider(), tools=tool_list)
    except ContextOverflowError as exc2:
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  Still overflow: {exc2}. Emergency content truncation...{Colors.RESET}")

    # 2. Emergency: content-truncate oversized tool results in live_messages.
    # 这是仅剩的"原地改写"路径 — 承认这一次 cache miss, 换请求能发出去。
    self._content_truncate_large_tool_results()

    # 3. 最后一次尝试。再失败直接抛 — agent 已无路可走。
    return await self.router.call(messages=self.render_for_provider(), tools=tool_list)
```

---

## 4. 代码改动定位（file by file）

### 4.1 `mini_agent/schema/schema.py`

**改动 1**：扩展 `TokenUsage`，**保持 `prompt_tokens` 语义不变**（总 input，含 cached + uncached）。

```python
class TokenUsage(BaseModel):
    prompt_tokens: int = 0              # 不变:总 input = uncached + cache_read + cache_write
    completion_tokens: int = 0
    total_tokens: int = 0
    # NEW: 仅作为细分字段, 不影响 prompt_tokens 既有语义
    cache_read_tokens: int = 0          # Anthropic cache_read_input_tokens / DeepSeek prompt_cache_hit_tokens
    cache_creation_tokens: int = 0      # Anthropic cache_creation_input_tokens; DeepSeek 为 0
    cache_miss_tokens: int = 0          # DeepSeek prompt_cache_miss_tokens; Anthropic 可填 input_tokens
```

**Message schema 不动**——cache_control 不持久化到 Message。

### 4.2 `mini_agent/llm/anthropic_client.py` + `mini_agent/llm/base.py`

**改动 0（先做这条，否则下游 TypeError / DeepSeek marker 误注入）**：在 `LLMClientBase.generate` 抽象签名里加 `attach_message_bp: bool = True` 和 `enable_cache_control: bool = False` 参数。所有子类（AnthropicClient、OpenAIClient、测试用 fake / mock client）都要在签名里接受这两个参数。

```python
# mini_agent/llm/base.py:
class LLMClientBase:
    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        max_tokens: int | None = None,
        attach_message_bp: bool = True,   # ← 新增, 默认 True 兼容现有 main call 行为
        enable_cache_control: bool = False, # ← 新增, 只有官方 Anthropic 等显式支持者才 True
    ) -> LLMResponse:
        ...
```

- AnthropicClient：只有 `enable_cache_control=True` 时才注入/保留 system/tools/messages 的 `cache_control`；再根据 `attach_message_bp` 决定是否挂 BP #4（见改动 4）
- OpenAIClient：**接受参数但忽略 cache_control 语义**；但要 flatten system list 并丢弃任何 `cache_control`
- 测试 fake client：**接受参数但忽略**

漏掉任何一个子类，router 调 `generate(..., attach_message_bp=False, enable_cache_control=False)` 命到该 client 时直接 TypeError。

**无代码改动 1（仅确认设计意图）**：`_make_api_request` 支持 `system` 为 list 形式。

[anthropic_client.py:87-88](../mini_agent/llm/anthropic_client.py#L87)：
```python
if system_message:
    params["system"] = system_message
```

无需修改这两行——Anthropic SDK 接受 `str` 和 `list[TextBlockParam]` 两种类型。但要确保 `_convert_messages` 返回的 system 部分能是 list[dict]。这里标成"无代码改动"，避免实现时误以为需要重写 `_make_api_request`。

**改动 2**：`_convert_messages` 支持 `Message(role="system", content=list[dict])` 直接透传。

[anthropic_client.py:184-186](../mini_agent/llm/anthropic_client.py#L184)：
```python
if msg.role == "system":
    system_message = msg.content
    continue
```

`msg.content` 可能是 `str` 或 `list[dict]`，都直接赋给 `system_message`。SDK 会接受。**这是 §3.2 里"Message.content: str | list[dict]"的复用，schema 已经支持。**

**改动 3**：`_convert_tools` 支持给最后一个 tool 挂 cache_control。新增参数 `cache_last_tool: bool = False`：

```python
def _convert_tools(self, tools, *, cache_last_tool: bool = False):
    result = [...]  # 原有转换
    if cache_last_tool and result:
        # IMPORTANT: shallow-copy 最后一个 dict 再加 cache_control。
        # 现有转换里 result.append(tool) 分支(Anthropic-shape dict input)
        # 是直接复用调用方的对象引用 — 如果直接 result[-1]["cache_control"] = ...
        # 会把这个临时 metadata 写回到 self.tools 里的 tool schema dict上,
        # 破坏"cache_control 仅在请求渲染层临时存在"的 invariant。
        result[-1] = {**result[-1], "cache_control": {"type": "ephemeral"}}
    return result
```

调用方 `_prepare_request` 仅在 `enable_cache_control=True` 时传 `cache_last_tool=True`。DeepSeek Anthropic-compatible endpoint 会忽略 `cache_control`，但 v1 默认不注入，避免把 DeepSeek 自动 cache 和 Anthropic explicit cache 混在一起。

**改动 4**：`_convert_messages` 在 main call 路径动态挂 BP #4 到最后一条 stable assistant；internal_call 路径**不挂**。

实现方式：让 `generate()` 接受 `attach_message_bp: bool = True` 和 `enable_cache_control: bool = False` 参数：
- `router.call()` 调 `generate(..., attach_message_bp=True, enable_cache_control=node.supports_explicit_cache_control)`
- `router.internal_call()` 调 `generate(..., attach_message_bp=False, enable_cache_control=node.supports_explicit_cache_control)`
- DeepSeek 节点默认 `supports_explicit_cache_control=False`，因此不会注入 BP #1/#2/#3/#4；cache 由 DeepSeek 自动 Context Caching 处理

`generate` 透传给 `_prepare_request`；后者先根据 `enable_cache_control` 决定是否保留/注入 `cache_control`，再根据 `attach_message_bp` 决定是否挂 BP #4。**不修改原 Message 对象**——挂在转换后的 dict 上。

**关键：BP #4 挂载的 4 个边界情况**

```python
def _convert_messages(self, messages, *, attach_message_bp: bool = True, enable_cache_control: bool = False):
    # ... 原有转换逻辑生成 api_messages ...
    if not enable_cache_control:
        system_message = _strip_cache_control_from_system(system_message)
        api_messages = _strip_cache_control_from_messages(api_messages)
        return system_message, api_messages

    if attach_message_bp:
        last_asst_idx = self._find_last_stable_assistant(api_messages)
        if last_asst_idx is None:
            return system_message, api_messages

        content = api_messages[last_asst_idx]["content"]

        # 边界 1: content 是字符串(没有 tool_calls 也没有 thinking 的简单 assistant)。
        # Anthropic 协议字符串不能挂 cache_control, 需要先提升为 list[block]。
        if isinstance(content, str):
            api_messages[last_asst_idx]["content"] = [
                {"type": "text", "text": content,
                 "cache_control": {"type": "ephemeral"}}
            ]
            return system_message, api_messages

        # 边界 2: content 是 list 但为空 - 跳过, 没有可挂的 block
        if not isinstance(content, list) or not content:
            return system_message, api_messages

        # 边界 3: content 是 list, 但需要跳过 thinking block
        # Anthropic 协议在 thinking block 上挂 cache_control 会 400 error。
        # 通常 thinking 在前 / text 或 tool_use 在后, 但不保证 — 显式跳过。
        # 边界 4: 找到目标 block 后必须 shallow-copy 再挂, 否则改的是 Message
        # 渲染过程中的中间 dict, 但下次同样的 Message 会被重新构造, 所以
        # 严格来说不会污染 self.live_messages — 但 shallow-copy 是防御性编程。
        for i in range(len(content) - 1, -1, -1):
            block = content[i]
            if isinstance(block, dict) and block.get("type") != "thinking":
                content[i] = {**block, "cache_control": {"type": "ephemeral"}}
                break
        # 如果 list 里全是 thinking block, 不挂 BP #4 (异常但合法)。

    return system_message, api_messages

def _find_last_stable_assistant(self, api_messages: list[dict]) -> int | None:
    """Return the index of the last assistant message, or None if none.

    'Stable' 意思是该 message 已经在上一次主请求里发出过并接收过响应,
    所以它的字节内容已经固化。当前函数被调用时 api_messages 末尾
    可能还跟着一条本轮的新 user message — 我们要找的是更前面的 assistant。
    最简单实现:从后往前找第一个 role=="assistant"。
    """
    for i in range(len(api_messages) - 1, -1, -1):
        if api_messages[i].get("role") == "assistant":
            return i
    return None

def _strip_cache_control_from_system(system_message):
    """Remove Anthropic cache_control markers when node does not support them."""
    if isinstance(system_message, list):
        stripped = []
        for block in system_message:
            if isinstance(block, dict):
                stripped.append({k: v for k, v in block.items() if k != "cache_control"})
            else:
                stripped.append(block)
        return stripped
    return system_message

def _strip_cache_control_from_messages(api_messages: list[dict]) -> list[dict]:
    """Remove cache_control from nested message content blocks in request dicts."""
    for msg in api_messages:
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [
                ({k: v for k, v in block.items() if k != "cache_control"}
                 if isinstance(block, dict) else block)
                for block in content
            ]
    return api_messages

def _strip_cache_control_from_tools(api_tools: list[dict] | None) -> list[dict] | None:
    """Remove top-level tool cache_control markers defensively."""
    if not api_tools:
        return api_tools
    stripped = []
    for tool in api_tools:
        if not isinstance(tool, dict):
            stripped.append(tool)
            continue
        clean = {k: v for k, v in tool.items() if k != "cache_control"}
        stripped.append(clean)
    return stripped
```

`_prepare_request` 里要同时处理 messages 和 tools：

```python
system_message, api_messages = self._convert_messages(
    messages,
    attach_message_bp=attach_message_bp,
    enable_cache_control=enable_cache_control,
)
api_tools = self._convert_tools(
    tools or [],
    cache_last_tool=enable_cache_control,
) if tools else None
if not enable_cache_control:
    api_tools = _strip_cache_control_from_tools(api_tools)
```

**改动 5**：`_parse_response` 把 cache token 写进 `TokenUsage`，**保持 `prompt_tokens` 总和语义**。

[anthropic_client.py:296-303](../mini_agent/llm/anthropic_client.py#L296)：

```python
usage_obj = response.usage

# Anthropic Python SDK 0.72.x currently preserves unknown usage fields
# (extra="allow"), so DeepSeek prompt_cache_* fields can be read with getattr
# if the Anthropic-compatible endpoint returns them. Still, the endpoint may
# instead map cache stats into Anthropic-standard fields, so support both paths.
deepseek_hit = getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0
deepseek_miss = getattr(usage_obj, "prompt_cache_miss_tokens", 0) or 0

if deepseek_hit or deepseek_miss:
    input_tokens = deepseek_miss
    cache_read = deepseek_hit
    cache_creation = 0
else:
    input_tokens = usage_obj.input_tokens or 0           # Anthropic uncached input
    cache_read = getattr(usage_obj, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage_obj, "cache_creation_input_tokens", 0) or 0

total_input = input_tokens + cache_read + cache_creation

usage = TokenUsage(
    prompt_tokens=total_input,                            # 保持"总 input"语义
    completion_tokens=output_tokens,
    total_tokens=total_input + output_tokens,
    cache_read_tokens=cache_read,
    cache_creation_tokens=cache_creation,
    cache_miss_tokens=input_tokens,
)
```

**实施前必须实测**：DeepSeek 官方 Context Caching 文档说明 usage 里有 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，但 Anthropic-compatible endpoint 的真实 SDK 对象字段仍要实测确认。用合法 JSON 生成一次长前缀请求，不要手写 `"x"*5000` 这种无效 JSON：

```bash
python3 - <<'PY' >/tmp/deepseek-cache-probe.json
import json
payload = {
    "model": "deepseek-v4-pro",
    "max_tokens": 20,
    "system": [{"type": "text", "text": "x" * 5000, "cache_control": {"type": "ephemeral"}}],
    "messages": [{"role": "user", "content": "hi"}],
}
print(json.dumps(payload))
PY

curl -sS https://api.deepseek.com/anthropic/v1/messages \
  -H "x-api-key: $DEEPSEEK_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  --data @/tmp/deepseek-cache-probe.json | jq '.usage'
```

根据返回结果写一个 fixture 测试：如果返回 `prompt_cache_*`，验证 DeepSeek path；如果返回 `cache_read_input_tokens`，验证 Anthropic-standard path。运行时代码保留双路径解析，避免 provider 后续调整字段名时直接丢 usage。

### 4.3 `mini_agent/llm/openai_client.py`

**改动 0（与 §4.2 改动 0 配套）**：`generate()` 签名同步加 `attach_message_bp: bool = True` 和 `enable_cache_control: bool = False` 参数；OpenAI 实现里**接收但忽略显式 BP 语义**（DeepSeek/OpenAI 都不使用 Anthropic `cache_control`）。

```python
async def generate(
    self,
    messages: list[Message],
    tools: list[Any] | None = None,
    *,
    max_tokens: int | None = None,
    attach_message_bp: bool = True,   # 接受但不用
    enable_cache_control: bool = False, # 接受但不用
) -> LLMResponse:
    ...
```

**改动 1**：当 system message 的 content 是 list[dict] 时（Anthropic-style blocks），显式 flatten 成单个 string，并丢弃 cache_control。

**这是必修项**，否则 OpenAI 会因为 `system.content` 不是 string 而 400。

在 `_convert_messages`（或等价方法）入口处：

```python
def _convert_messages(self, messages: list[Message]) -> list[dict]:
    out = []
    for msg in messages:
        if msg.role == "system" and isinstance(msg.content, list):
            # Anthropic-style system blocks -> flatten to single string;
            # cache_control is dropped (OpenAI auto-caches, no marker needed).
            text = "\n\n".join(
                block["text"]
                for block in msg.content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            out.append({"role": "system", "content": text})
            continue
        # ... 现有逻辑 ...
```

**改动 2**：构造 `TokenUsage` 时兼容 DeepSeek OpenAI-format 与 OpenAI native 的 cache usage 字段。

```python
usage_obj = response.usage
cache_hit = getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0
cache_miss = getattr(usage_obj, "prompt_cache_miss_tokens", 0) or 0

# OpenAI native path: usage.prompt_tokens_details.cached_tokens
details = getattr(usage_obj, "prompt_tokens_details", None)
if not cache_hit and details is not None:
    cache_hit = getattr(details, "cached_tokens", 0) or 0
    prompt_total = usage_obj.prompt_tokens or 0
    cache_miss = max(prompt_total - cache_hit, 0)

usage = TokenUsage(
    prompt_tokens=usage_obj.prompt_tokens or (cache_hit + cache_miss),
    completion_tokens=usage_obj.completion_tokens or 0,
    total_tokens=usage_obj.total_tokens or 0,
    cache_read_tokens=cache_hit,
    cache_creation_tokens=0,
    cache_miss_tokens=cache_miss,
)
```

普通 OpenAI 不返回 cache detail 时保持 0。v1 主目标仍是 DeepSeek；OpenAI native 分支只做低成本兼容，不把 OpenAI 私有 cache 控制纳入本次范围。

### 4.4 `mini_agent/llm/ha/router.py`

**改动 1**：新增 `peek_primary_node()` 方法。

```python
def peek_primary_node(self) -> ModelNode | None:
    """Return the highest-priority enabled node (read-only, no side effects).

    Used by Agent.__init__ to cache the primary node for pricing lookups.
    Doesn't represent the node that will actually serve any specific call
    (failover may pick a different one) — but it's the best ex-ante estimate.
    """
    enabled = self.pool.enabled()
    if not enabled:
        return None
    return min(enabled, key=lambda n: (-n.priority, n.node_id))
```

**改动 2**：`internal_call` 调 `client.generate()` 时传 `attach_message_bp=False`，并按 node 能力传 `enable_cache_control`：

```python
async def internal_call(self, messages, tools=None):
    # ... 现有候选选取 ...
    return await client.generate(
        messages,
        tools,
        max_tokens=node.max_output_tokens,
        attach_message_bp=False,   # ← 新增, summary call 不挂 BP #4
        enable_cache_control=node.supports_explicit_cache_control,
    )
```

`call` 路径传 `attach_message_bp=True`；`enable_cache_control` 必须显式来自 node 能力，不能让 client 自己默认开启。

**实现 agent 选择**：如果不想给 `generate()` 加新参数，也可以让 `internal_call` 通过 `asyncio.contextvars` 或类似机制告知 `AnthropicClient`。**推荐显式参数**，最直接。

**改动 3（DeepSeek / MiniMax 适配）**：按 node 能力决定**是否注入** Anthropic `cache_control` marker。

DeepSeek V4 Pro 有自动 Context Caching，但它的 Anthropic-compatible API 明确把 `cache_control` 标为 ignored；MiniMax / 自部署端点也不能假设支持该字段。安全默认是"未确认支持 explicit marker 就不打 marker"。

实现：让 `ModelNode` 携带两个 cache 能力字段：

```python
supports_explicit_cache_control: bool = False      # Anthropic-style marker
supports_automatic_context_cache: bool = False     # DeepSeek/OpenAI-style automatic prefix cache
```

在 `_try_candidates` 命中具体 node 之后、调 `client.generate` 时显式传：

```python
# router.py 内部, 在调 client.generate 之前:
enable_cache_control = node.supports_explicit_cache_control
return await client.generate(
    messages,
    tools,
    max_tokens=actual_max_tokens,
    attach_message_bp=True,
    enable_cache_control=enable_cache_control,
)
```

`enable_cache_control=False` 时，client 必须：
- 清掉 system/message content block 中已有的 `cache_control`
- 不给最后一个 tool 追加 `cache_control`
- 不给最后 assistant block 追加 BP #4

这样才能避免 router 清理完、client 又重新注入 tool marker 的 bug。

**ModelNode 配置**：v1 默认 `supports_explicit_cache_control=False`。DeepSeek 配 `supports_automatic_context_cache=True`，但 explicit marker 仍为 false：

```yaml
nodes:
  - node_id: deepseek-v4-pro
    provider: anthropic
    protocol_family: anthropic
    api_base: https://api.deepseek.com/anthropic
    model: deepseek-v4-pro
    supports_explicit_cache_control: false
    supports_automatic_context_cache: true

  - node_id: anthropic-prod
    provider: anthropic
    protocol_family: anthropic
    api_base: https://api.anthropic.com
    model: claude-sonnet-4-6
    supports_explicit_cache_control: true
    supports_automatic_context_cache: false
```

这条同时呼应 §4.6 价格表——若 `supports_automatic_context_cache=True`（DeepSeek），即使 `supports_explicit_cache_control=False`，DP 仍应使用 DeepSeek cache hit/miss 价格；只有两种 cache 能力都为 false 时才退化为 `cache_read == input`。

### 4.5 `mini_agent/agent.py`

**核心改动清单**（按出现顺序）：

| 行号 / 区域 | 改动 |
|---|---|
| [L1-17 imports](../mini_agent/agent.py#L1) | 新增 `from .compaction import CompactionPolicy, CachePolicy, CompactionSnapshot, CompactionDecision` |
| [L56-67 `__init__` 签名](../mini_agent/agent.py#L56) | 保持签名 |
| [L105-107](../mini_agent/agent.py#L105) workspace 注入 | **改成拼到 `_base_system_prompt`**（之前拼到 `system_prompt`） |
| [L109-114 状态字段](../mini_agent/agent.py#L109) | 删 `self.system_prompt`；删 `self.cold_summaries`；新增 `self.current_summary: ContextSummary \| None = None`；新增 `self.compact_count = 0`、`self.llm_call_count = 0`、`self.user_turn_count = 0`、`self.last_usage: TokenUsage \| None = None`；新增 `self._primary_node = router.peek_primary_node()`；新增 `self.compaction_policy = CompactionPolicy()` |
| [L121-122 _skip_next_token_check](../mini_agent/agent.py#L121) | **删除** |
| [L127-141 messages property/setter](../mini_agent/agent.py#L127) | `setter` 的 `/clear` 分支：删 `cold_summaries = []`，改为 `current_summary = None`；同步重置 `compact_count = 0`、`llm_call_count = 0`、`user_turn_count = 0`、`last_usage = None` |
| [L145-167 render_for_provider](../mini_agent/agent.py#L145) | 重写：调用新增的 `_render_system_blocks()` 产出 list[dict]，构造 `Message(role="system", content=list[dict])` |
| [L171-173 add_user_message](../mini_agent/agent.py#L171) | 末尾加 `self.user_turn_count += 1` |
| [L186-195 _add_tool_message](../mini_agent/agent.py#L186) | 加 §3.3 的 ingest 截断 |
| [L230-285 _estimate_tokens / fallback](../mini_agent/agent.py#L230) | 保留（DP snapshot 用） |
| [L287-347 _compress_context](../mini_agent/agent.py#L287) | **整体删除**，替换为 `_maybe_run_compaction(tool_list, forced=False)` |
| [L349-359 _get_round_boundary](../mini_agent/agent.py#L349) | **删除**，逻辑搬到 `CompactionPolicy._user_round_boundaries` |
| [L361-411 _truncate_old_tool_results / _truncate_old_readfile_results](../mini_agent/agent.py#L361) | **整体删除** |
| [L415-439 _content_truncate_large_tool_results](../mini_agent/agent.py#L415) | **保留**，只在 emergency 路径调用 |
| [L441-477 _full_compress](../mini_agent/agent.py#L441) | 删，逻辑搬到 `_maybe_run_compaction` 内部 |
| [L504-557 _create_structured_summary](../mini_agent/agent.py#L504) | 重写为 `_run_cache_aligned_summary(dropped)`，按 §3.5 |
| [L550 router.internal_call(summary_messages)](../mini_agent/agent.py#L550) | 改 `internal_call(summary_messages, tools=tool_list)` |
| [L561-635 safe_generate / _generate_with_overflow_recovery](../mini_agent/agent.py#L561) | `safe_generate` 保留；`_generate_with_overflow_recovery` 重写为 §3.6 简化版 |
| [L637-694 pinned notes 区域](../mini_agent/agent.py#L637) | **删除 `_rebuild_system_prompt`**；`_pin_note` / `load_pinned_notes` 改为只 append 到 `self.pinned_notes` 不再调 `_rebuild_system_prompt`；`MAX_PINNED_CHARS` 常量保留 |
| [L715 await self._compress_context()](../mini_agent/agent.py#L715) | 改 `await self._maybe_run_compaction(tool_list, forced=False)`。**注意顺序**：当前 `tool_list = list(self.tools.values())` 在 [L728](../mini_agent/agent.py#L728)，必须把这行**上移到 L715 之前**，否则变量未定义。 |
| [L748-749 api_total_tokens 累计](../mini_agent/agent.py#L748) | 同步：`self.last_usage = response.usage`；`self.llm_call_count += 1` |

**关键新增方法**：

```python
async def _maybe_run_compaction(self, tool_list: list, *, forced: bool = False) -> None:
    """DP-driven compaction. See §3.5.6 for full body."""
    ...   # 详见 §3.5.6

def _build_compaction_snapshot(self, tool_list: list) -> "CompactionSnapshot":
    """Build a read-only snapshot for the policy."""
    # [v1] 直接用 tiktoken 估算当前 context. 不用 last_usage.prompt_tokens, 因为:
    # last_usage 是上次 LLM call 时的 input 总量, 之后追加的 tool_result
    # (尤其大型 Read 结果) 不在内, 会显著低估当前 context size。
    # 直接用 _estimate_tokens 走 render_for_provider, 覆盖最新状态。
    # 这里牺牲一点精度 (cl100k_base vs Claude 实际 tokenizer 差 5-10%) 换准确性。
    api_estimate = self._estimate_tokens() + self._count_tools_tokens(tool_list)

    # cache capability 退化:
    # - DeepSeek: supports_automatic_context_cache=True, 仍使用 hit/miss 价格
    # - Anthropic official: supports_explicit_cache_control=True, 使用 read/write 价格
    # - 其他未知 provider: 两者都 False, 把 cache_read/cache_write 强制等于 input
    pricing = CachePolicy.pricing_for_node(self._primary_node)
    if (
        self._primary_node is not None
        and not getattr(self._primary_node, "supports_explicit_cache_control", False)
        and not getattr(self._primary_node, "supports_automatic_context_cache", False)
    ):
        pricing = ModelPricing(
            input=pricing.input,
            cache_read=pricing.input,
            cache_write=pricing.input,
            output=pricing.output,
        )

    return CompactionSnapshot(
        live_messages=self.live_messages,
        current_summary=self.current_summary,
        system_token_count=self._count_system_tokens(),
        tools_token_count=self._count_tools_tokens(tool_list),
        pricing=pricing,
        user_turn_count=self.user_turn_count,
        llm_call_count=self.llm_call_count,
        compact_count=self.compact_count,
        api_input_token_estimate=api_estimate,
        max_context=self.token_limit,
    )

def _render_system_blocks(self) -> list[dict]:
    """Build Anthropic-shape system blocks with cache_control breakpoints.

    Block 排序 (改动频率从低到高):
      [base | BP #1] [pinned] [current_summary | BP #2] [current_plan]

    BP #2 决策表:
      | 有 pinned + 有 summary  -> BP #2 在 summary 末
      | 有 pinned + 无 summary  -> BP #2 在 pinned 末 (pinned 升级)
      | 无 pinned + 有 summary  -> BP #2 在 summary 末
      | 无 pinned + 无 summary  -> 不打 BP #2 (启动早期, 只用 3 个 BP)
    """
    blocks: list[dict] = []

    # Block 1: base (STABLE FOREVER)
    blocks.append({
        "type": "text",
        "text": self._base_system_prompt,
        "cache_control": {"type": "ephemeral"},     # ← BP #1
    })

    # Block 2: pinned notes (LOW FREQ, optional)
    if self.pinned_notes:
        pinned_text = "## Pinned Context (Important - Always Available)\n"
        for note in self.pinned_notes:
            pinned_text += f"- [{note.get('category', 'general')}] {note.get('content', '')}\n"
        blocks.append({"type": "text", "text": pinned_text})

    # Block 3: current_summary (CHANGES ON COMPACT, optional)
    if self.current_summary is not None:
        blocks.append({
            "type": "text",
            "text": f"## Historical Summary\n{self.current_summary.raw_text}",
            "cache_control": {"type": "ephemeral"},   # ← BP #2 落在这里
        })
    elif self.pinned_notes:
        # 无 summary 但有 pinned, pinned 升级为 BP #2
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    # 都没有: 启动早期, 不打 BP #2

    # Block 4: current plan (HIGH FREQ, no BP)
    if self.planning_manager is not None:
        plan_section = self.planning_manager.render_for_prompt()
        if plan_section:
            blocks.append({"type": "text", "text": plan_section})

    return blocks

async def _run_cache_aligned_summary(self, dropped: list[Message]) -> ContextSummary:
    """See §3.5.3."""
    ...

def _build_deterministic_fallback_summary(self, dropped: list[Message], *, reason: str) -> ContextSummary:
    """See §3.5.6."""
    ...

def _count_system_tokens(self) -> int:
    """Token count of base + pinned + current_summary (excluding plan)."""
    # v1 的 DP 公式把 plan 视为高频动态小段，不放进稳定前缀 V。
    pieces: list[object] = [self._base_system_prompt]
    if self.pinned_notes:
        pieces.append(self.pinned_notes)
    if self.current_summary is not None:
        pieces.append(self.current_summary.raw_text)
    return sum(self._count_value_tokens(piece) + 4 for piece in pieces)

def _count_tools_tokens(self, tool_list: list) -> int:
    """Token count of tools schema."""
    schemas: list[object] = []
    for tool in tool_list:
        if isinstance(tool, dict):
            schemas.append(tool)
        elif hasattr(tool, "to_schema"):
            schemas.append(tool.to_schema())
        elif hasattr(tool, "to_openai_schema"):
            schemas.append(tool.to_openai_schema())
        else:
            schemas.append(str(tool))
    return self._count_value_tokens(schemas)

def _count_value_tokens(self, value: object) -> int:
    """Best-effort token count helper shared by snapshot builders.

    Keep this intentionally local/simple. Provider tokenizers differ; v1 only
    needs a stable estimate for thresholding and relative DP comparisons.
    """
    import json
    import tiktoken

    enc = getattr(self, "_token_encoder", None)
    if enc is None:
        enc = tiktoken.get_encoding("cl100k_base")
        self._token_encoder = enc

    if value is None:
        return 0
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return len(enc.encode(text))

def _extract_file_paths_from_tool_args(self, messages: list[Message]) -> set[str]:
    """Best-effort extraction of file paths from Read/Write/Edit tool_calls in messages."""
    paths: set[str] = set()
    path_keys = {"path", "file_path", "filepath", "filename", "target_file", "target_path"}
    for msg in messages:
        if not msg.tool_calls:
            continue
        for call in msg.tool_calls:
            name = (call.function.name or "").lower()
            if not any(marker in name for marker in ("read", "write", "edit", "file")):
                continue
            args = call.function.arguments or {}
            for key, value in args.items():
                if key in path_keys and isinstance(value, str) and value:
                    paths.add(value)
    return paths
```

### 4.6 新增模块 `mini_agent/compaction/`

```text
mini_agent/compaction/
  __init__.py         # 导出 CompactionPolicy, CompactionSnapshot, CompactionDecision, CachePolicy, ModelPricing
  models.py           # CompactionSnapshot, CompactionDecision, ModelPricing dataclass
  policy.py           # CompactionPolicy + DP 算法 (按 §3.4.2)
  cache_policy.py     # CachePolicy + 价格表
```

`cache_policy.py` 完整内容：

```python
from dataclasses import dataclass
from typing import Optional

# Forward reference - actual import done lazily
class ModelNode:
    """Type-hint stub; real class is in mini_agent.llm.ha.models."""
    protocol_family: str
    model: str
    supports_explicit_cache_control: bool
    supports_automatic_context_cache: bool


@dataclass(frozen=True)
class ModelPricing:
    """USD per 1M tokens."""
    input: float
    cache_read: float
    cache_write: float
    output: float


# Keyed by (protocol_family, model).
# IMPORTANT: 价格会变。DeepSeek V4 Pro 当前有 75% off 促销,官方页写明
# 延长到 2026-05-31 15:59 UTC。实施/合并前请重新核对官方价格。
_PRICING_TABLE: dict[tuple[str, str], ModelPricing] = {
    # DeepSeek V4 (官方 API, automatic Context Caching; no explicit cache_control)
    # 2026-05 促销价: hit/miss/output = 0.003625 / 0.435 / 0.87 USD per 1M
    ("anthropic", "deepseek-v4-pro"):  ModelPricing(0.435, 0.003625, 0.435, 0.87),
    ("openai",    "deepseek-v4-pro"):  ModelPricing(0.435, 0.003625, 0.435, 0.87),
    # 2026-05 价格: hit/miss/output = 0.0028 / 0.14 / 0.28 USD per 1M
    ("anthropic", "deepseek-v4-flash"): ModelPricing(0.14, 0.0028, 0.14, 0.28),
    ("openai",    "deepseek-v4-flash"): ModelPricing(0.14, 0.0028, 0.14, 0.28),

    # Anthropic (官方端点, 支持 explicit cache_control)
    ("anthropic", "claude-opus-4-7"):   ModelPricing(15.0, 1.50, 18.75, 75.0),
    ("anthropic", "claude-sonnet-4-6"): ModelPricing(3.0,  0.30, 3.75,  15.0),
    ("anthropic", "claude-haiku-4-5"):  ModelPricing(0.80, 0.08, 1.00,  4.0),
    # MiniMax via Anthropic-compatible endpoint
    # WARNING: MiniMax 是否接受 cache_control 字段未实测确认。
    # 默认两个 cache 能力都为 False，公式退化到 cache_read == cache_write == input。
    ("anthropic", "MiniMax-M2.5"):      ModelPricing(0.30, 0.30, 0.30,  1.20),
    # MiniMax via OpenAI-compatible endpoint
    ("openai",    "MiniMax-M2.5"):      ModelPricing(0.30, 0.30, 0.30,  1.20),
    # OpenAI (auto-caches, no explicit marker)
    ("openai",    "gpt-4o"):            ModelPricing(2.50, 1.25, 2.50,  10.0),
}

# Fallback: cache_read == cache_write == input → DP loses cache discount but can still compact to reduce tokens.
_DEFAULT_PRICING = ModelPricing(3.0, 3.0, 3.0, 15.0)


class CachePolicy:
    @staticmethod
    def pricing_for_node(node: Optional[ModelNode]) -> ModelPricing:
        """Look up pricing by (protocol_family, model) tuple.

        Note: 这里返回的是"理论可达"的价格表。实际是否享受 cache 折扣要看
        node.supports_explicit_cache_control 或 node.supports_automatic_context_cache。
        Agent._build_compaction_snapshot 只在两者都 False 时退化为 cache==input。
        """
        if node is None:
            return _DEFAULT_PRICING
        return _PRICING_TABLE.get((node.protocol_family, node.model), _DEFAULT_PRICING)
```

**关键设计**：
- 按 `(protocol_family, model)` 索引——cache 能力是 provider/protocol-level 属性。同名 model 跑在不同协议端点上行为可能完全不同。
- DeepSeek 没有 explicit cache_control，但有 automatic Context Caching，所以价格表必须保留 hit/miss 价，不可因为 `supports_explicit_cache_control=False` 而退化。
- 未知 model fallback 返回 `cache_read == cache_write == input`，DP 公式自然失去 cache 优势，但仍可能因减少输入 token 而 compact。

---

## 5. Provider 兼容性矩阵

| Provider | `supports_explicit_cache_control` | `supports_automatic_context_cache` | 是否注入 cache_control marker | 价格表 | DP 行为 |
|---|---|---|---|---|---|
| DeepSeek V4 Pro/Flash Anthropic 端 | **false** | **true** | 否；DeepSeek 会忽略该字段 | DeepSeek hit/miss/output 价 | 自动 prefix cache 公式 |
| DeepSeek V4 Pro/Flash OpenAI 端 | **false** | **true** | 否；flatten system list → str | DeepSeek hit/miss/output 价 | 自动 prefix cache 公式 |
| Anthropic 官方 (`api.anthropic.com`) | **true**（用户显式开） | false | 是 | Anthropic input/cache_read/cache_write/output | 显式 BP 公式 |
| OpenAI 官方 | false | true（自动 prompt cache） | 否；flatten system list → str | OpenAI cached/uncached 价 | 自动 prefix cache 公式 |
| MiniMax / 自部署 / 未知 provider | false（默认） | false（默认） | 否 | fallback (cache=input) | 仅"减少输入 token"维度 |

**关键实现细节**：
1. 所有 client 收到 `Message(role="system", content=list[dict])` 时都要正确处理。OpenAI client 必须 flatten，否则会 400。
2. Router 在 `_try_candidates` 命中具体 node 后，把 `enable_cache_control=node.supports_explicit_cache_control` 传给 client；不能只在 router 层 strip，因为 AnthropicClient 可能在 `_convert_tools` 中重新注入 marker。
3. `Agent._build_compaction_snapshot` 只有在 `supports_explicit_cache_control=False` 且 `supports_automatic_context_cache=False` 时才把 `cache_read / cache_write` 都设为 `input` 价。DeepSeek explicit=false 但 automatic=true，必须保留 hit/miss 价格。
4. DeepSeek usage 中的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 要进入 `TokenUsage.cache_read_tokens` / `cache_miss_tokens`，否则无法验证实际命中率。

---

## 6. 边界情况与失败模式

### 6.1 第一次 compact 之前

- `current_summary is None`
- `_render_system_blocks` 不输出"Historical Summary" 块
- 若 pinned_notes 也为空，连 BP #2 都不打，只用 3 个 BP（启动早期 cache 命中率天然较低，可接受）
- DP 公式里 V 较小（只有 base + tools），不影响正确性

### 6.2 live_messages 不够长

- DP 决策器在 `_user_round_boundaries` 长度 < 2 时返回 `should_compact=False, reason="nothing_to_drop"`
- 即使 forced=True 也不能凭空创造可丢的 round；返回 noop，由上层 emergency content truncation 兜底

### 6.3 Mid-tool-loop 触发 compact

`_compress_context` 现在改名 `_maybe_run_compaction`，仍在 **step 头部**调用（[agent.py:715](../mini_agent/agent.py#L715)）。此时 `live_messages` 末尾要么是 user message、要么是 tool message。DP 切分按 user-round 边界，**绝不会**切出"assistant 有 tool_use 但 tool_result 已丢"的非法序列。

### 6.4 并行 tool calls

mini-agent 内部一个 assistant message 可以带多个 `tool_calls`，紧跟着多个 `role="tool"` messages。DP 切分按 `role="user"` 边界，并行 tool calls 整组要么全保留要么全丢。

但要注意：[anthropic_client.py:172-181](../mini_agent/llm/anthropic_client.py#L172) `_convert_messages` 把多个连续 `role="tool"` 合批成**一个 user message**。BP #4 找"last stable assistant"时要找的是**合批后**的最后一条 assistant message。

### 6.5 取消（cancellation）

[agent.py:207](../mini_agent/agent.py#L207) `_cleanup_incomplete_messages` 在用户按 Esc 时把最后一条 assistant + 所有 tool_results **切片移除**，不原地改写，与 cache invariant 不冲突。

但要确认：cleanup 后下次主请求时 BP #4 位置漂移到一个新位置——这是 OK 的，因为 BP #4 由 `AnthropicClient._convert_messages` **动态决定**，不预先挂在 Message 上。

### 6.6 Summary 调用失败

§3.5.6 已经处理：
- 正常路径：失败 → 放弃 compact，dropped 留在 live_messages，下次 step 再试
- forced 路径：失败 → deterministic fallback summary（保留 user_goals / tool_names / file_paths）+ 丢 dropped

summary 调用走 `internal_call`，**不污染主路径节点熔断状态**（现有 router 语义，本次不动）。

### 6.7 cache TTL 与小 system 下的 cache_control

DeepSeek 自动 Context Caching 的 cache 构建需要几秒；不再使用后通常几小时到几天才清理。因此 DeepSeek 下 `R = E × L` 不需要因为用户 idle 几分钟就打折，但仍要承认 best-effort 命中不保证 100%。

官方 Anthropic endpoint 的默认 prompt cache TTL 更短（通常按分钟级理解，除非启用 extended TTL beta）。高频对话会持续续期；长时间冷启动后 BP 可能 miss，DP 的 future_savings 会偏乐观。v1 不建模 TTL 衰减，只在日志里记录 cache_read/cache_miss 供后续校准。

另外，Sonnet/Opus 系列 BP 前累积内容 < 1024 token 时 Anthropic 可能静默忽略。**不需要特殊处理**——浪费一次 API 字段，下次系统提示长起来后自动生效。

### 6.8 同一 session 内多次 compact（c 增长）

DP 公式 ④ 项 `β × (1 - r^(c+1))` 随 c 增大单调递增，上限约 0.63。失真权重单调递增 → DP 自然变保守 → 避免无限套娃压缩。

### 6.9 `/clear` 行为

[agent.py:131-141](../mini_agent/agent.py#L131) `messages.setter` 的 `/clear` 分支重置：
- `self.live_messages = []`
- `self.current_summary = None`（v0 是 `self.cold_summaries = []`）
- `self.compact_count = 0`
- `self.user_turn_count = 0`
- `self.llm_call_count = 0`
- `self.last_usage = None`
- pinned_notes 保留（不受 `/clear` 影响）

### 6.10 cross-family fallback（Anthropic → OpenAI）

[router.py:198-218](../mini_agent/llm/ha/router.py#L198) 在 fallback 到 OpenAI 时调用 `_prepare_messages_for_family` 去掉 Anthropic 专有 block（如 `thinking`）。

**新增清理动作**：fallback 时遍历 messages 数组，把任何 dict 形式的 content block 里的 `cache_control` 字段**删除**（OpenAI 不认）。同时如果 system message 是 list[dict]，让 OpenAI client 内的 flatten 逻辑处理（§4.3）。

### 6.11 估算与真值的差异

`_estimate_tokens` 用 tiktoken cl100k_base，与 Claude/DeepSeek/MiniMax 实际 tokenizer 有 5-10% 偏差。

容忍策略：
- DP 触发判定用当前 `render_for_provider()+tools` 的本地估算；`last_usage.prompt_tokens` 只用于日志校准
- DP 切片内部计算 H/K 用 tiktoken（误差对相对大小不敏感）
- 决策日志输出 `local={tiktoken}, api={last_usage}, decision={...}`，方便事后核对

### 6.12 `MAX_PINNED_CHARS` 上限

[agent.py:639](../mini_agent/agent.py#L639) 当前 4000 字符（~1000 token），不会自己撑爆 BP #2。保持不变。

### 6.13 PlanningManager 渲染为空

`render_for_prompt()` 返回空字符串时，`_render_system_blocks` 不 append 空 block。

### 6.14 Failover 到不同 model 时 pricing 估算偏差

`self._primary_node` 在 `__init__` 时缓存一次，router 实际 failover 到其他 node 时不更新。

- 主节点是 DeepSeek V4 Pro（有自动 cache），failover 到无 cache provider：DP 按 DeepSeek cache 价算→可能低估 compact 成本
- 主节点是 Sonnet（贵），failover 到 DeepSeek/MiniMax（便宜）：DP 按 Sonnet 价算→更倾向 compact→错向"少省钱"
- 反之：DP 按便宜价算→更不倾向 compact→错向"多塞 context"

错向"少 compact"是安全方向；错向"多塞 context"也安全（最多触发 forced overflow 兜底）。v1 不需要动态更新 pricing。

### 6.15 实际启动到稳定阶段的 cache 命中曲线

DeepSeek 主线（automatic Context Caching）：

| 阶段 | 命中模式 |
|---|---|
| Turn 1 | 通常 miss；服务端开始构建/persist cache prefix unit |
| Turn 2 | 若请求前缀完整匹配上一轮已持久化 prefix unit，稳定 system/tools/旧 messages 可能 hit；新增 user 输入 miss |
| Turn 3-N（无 compact） | `prompt_cache_hit_tokens` 通常逐步升高，但 best-effort 不保证单调 |
| Turn N+1（compact 触发） | summary call 复用 system/tools/dropped 前缀，实际 hit 取决于 DeepSeek 是否已有匹配 prefix unit；DP 按 dropped miss 悲观估算 |
| Turn N+2 起 | 新 summary + kept messages 逐步成为新的稳定前缀 |

官方 Anthropic endpoint（explicit BP）：

| 阶段 | 命中模式 |
|---|---|
| Turn 1 | 所有 BP miss（首次写入） |
| Turn 2 | BP #1 / #3 hit；BP #2 看是否到 1024 阈值；BP #4 hit（上轮写入） |
| Turn 3-N（无 compact） | 全部 hit；messages 增长时 BP #4 持续前移，新增部分全价 |
| Turn N+1（compact 触发） | summary call: BP #1/#2/#3 hit; dropped 部分按悲观全价（实际可能 partial hit）；主请求下一次：BP #1/#3 hit、#2/#4 miss 一次 |
| Turn N+2 起 | 全部 hit 恢复 |

---

## 7. 迁移计划（Phase 0 验证 + 3 阶段，可分别 review/回滚）

**[v1 调整]** Phase 1 一次性完成"状态结构改造 + caching 启用 + 删 L1/L2"，避免跨阶段维护两套 `_render_system_blocks` / 两套 summary 状态。Phase 2 只动 summary call 结构，Phase 3 只引入 DP——每个阶段都是单一职责。

### Phase 0: DeepSeek usage shape 探针（只读验证）

**目标**：在动实现前确认 DeepSeek Anthropic-compatible endpoint 的真实 `usage` 字段名。官方 Context Caching 文档写的是 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`；Anthropic-compatible endpoint 可能原样返回，也可能映射到 Anthropic-standard `cache_read_input_tokens`。运行 §4.2 的 curl probe，把 `.usage` 样例贴进测试 fixture。

**验收**：
- 若返回 `prompt_cache_*`：`AnthropicClient._parse_response` 的 DeepSeek path 必须有单测。
- 若返回 `cache_read_input_tokens` / `input_tokens`：Anthropic-standard path 必须有单测。
- 两条 runtime 解析路径都保留；probe 只决定测试 fixture，不决定删哪条代码。

### Phase 1: 状态结构 + caching + 删 L1/L2

**目标**：状态分层就位，能跑通，DeepSeek 下能看到 `TokenUsage.cache_read_tokens/cache_miss_tokens` 被填充。Summary call 仍是 v0 朴素结构（非 aligned），先不动。

文件：
- `schema/schema.py`: 加 `cache_read_tokens` / `cache_creation_tokens` / `cache_miss_tokens` 字段（`prompt_tokens` 语义不变）
- `llm/base.py`: `generate()` 抽象签名加 `attach_message_bp: bool = True` 与 `enable_cache_control: bool = False` 参数
- `llm/anthropic_client.py`: `generate()` 实参；`_convert_tools` 支持 `cache_last_tool`（shallow copy）；`_convert_messages` 支持 system list + 动态挂 BP #4（含 string content / thinking block 处理）；`_parse_response` 写新字段
- `llm/openai_client.py`: `generate()` 接 `attach_message_bp` / `enable_cache_control` 但忽略显式 BP；显式 flatten system list → str；丢弃 cache_control 字段；读取 DeepSeek `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
- `llm/ha/router.py`: 加 `peek_primary_node()`；调用 `client.generate()` 时传 `enable_cache_control=node.supports_explicit_cache_control`；`internal_call` 透传 `attach_message_bp=False`
- `llm/ha/models.py`: `ModelNode` 加 `supports_explicit_cache_control: bool = False` 与 `supports_automatic_context_cache: bool = False` 字段；`ModelNodeConfig`、`_build_pool_entries()`、CLI/SWE-bench 组装 `ModelNode` 都要同步传递
- `config.py`: `ModelNodeConfig` 新增两个字段；`_build_pool_entries()` 从 YAML 读取；`_resolve_pool_keys()` 保持 model_copy 时字段不丢
- `cli.py`: `ModelNode(...)` 构造时传 `supports_explicit_cache_control=entry.supports_explicit_cache_control` 与 `supports_automatic_context_cache=entry.supports_automatic_context_cache`
- `benchmarks/swebench/task_runner.py`: 同步传两个 cache 能力字段，避免 bench 配置与 CLI 配置行为不一致
- 测试 fixtures：`tests/test_llm.py`、`tests/test_llm_router.py`、`tests/test_agent.py`、`tests/test_cross_family_failover.py`、`tests/test_llm_pool.py`、`tests/test_llm_router_call.py`、`tests/test_llm_ha_fixes.py`、`tests/test_integration.py` 中的 `ModelNode(...)` 因字段默认 False 不要求全改；新增 DeepSeek cache 测试时再显式设 `automatic=true`
- `agent.py`:
  - 删 `_rebuild_system_prompt` 和 `self.system_prompt`；workspace info 拼到 `_base_system_prompt`
  - **`cold_summaries: list` → `current_summary: ContextSummary | None`** 一次性迁移（避免 Phase 3 重做 `_render_system_blocks`）
  - 新增 `_render_system_blocks()`（按 v1 最终形态写）；改 `render_for_provider` 输出结构化 system
  - 改 `_add_tool_message` 加 ingest 截断（用 char 还是 byte 见 §3.3）
  - 删 `_truncate_old_tool_results` / `_truncate_old_readfile_results` 整两个函数
  - `_compress_context` 暂改名 `_maybe_run_compaction` 但内部仍只跑 L4 触发（阈值不变），调用 `_run_cache_aligned_summary`（先朴素版，下个 phase 改）；写入 `self.current_summary = summary`
  - 主循环：`tool_list = list(self.tools.values())` 上移到 step 头部
  - `/clear` 路径：重置 `current_summary = None`
  - 新增 `_primary_node = router.peek_primary_node()` 缓存

**验证**：跑长对话，DeepSeek 下日志看到主请求的 `cache_read_tokens` / `cache_miss_tokens` 有值；触发 compact 后 `current_summary is not None`，旧消息丢失。

### Phase 2: Summary call 改 cache-aligned

**目标**：触发 compact 后 summary call 自身也看到 `cache_read_tokens` 上升。

文件：
- `agent.py`: 重写 `_run_cache_aligned_summary` 按 §3.5.3 结构（system blocks 完全复用上次主请求；只追加 instruction）；改 instruction 文案为 §3.5.4 Version A；user_goals deterministic 合并写回（§4.5 中的代码）
- 之前 Phase 1 已经传 `attach_message_bp=False`，所以 dropped 不会被挂 BP #4（已经成立）

**验证**：触发 compact 后 summary call 的 `cache_read_tokens` 上升或 `cache_miss_tokens` 下降。DeepSeek 是 best-effort 自动 cache，不要求 100% 命中。

### Phase 3: 引入 DP 决策器

**目标**：替换固定阈值触发为 DP NetBenefit 决策。

文件：
- 新增 `mini_agent/compaction/` 包：`models.py`（CompactionSnapshot/CompactionDecision/ModelPricing）+ `policy.py`（CompactionPolicy + DP 算法）+ `cache_policy.py`（CachePolicy + 价格表）
- `agent.py`: 新增 `_build_compaction_snapshot`；`_maybe_run_compaction` 内部调 `compaction_policy.decide(snapshot, forced=forced)` 替换原固定阈值判断；新增 `compact_count` / `llm_call_count` / `user_turn_count` / `last_usage` 状态；`_generate_with_overflow_recovery` 简化为 §3.6 版；删 `_skip_next_token_check`
- 在 [agent.py:171-173](../mini_agent/agent.py#L171) `add_user_message` 加 `self.user_turn_count += 1`
- [agent.py:748-749](../mini_agent/agent.py#L748) 拿到 usage 后同步 `self.last_usage / llm_call_count`

**验证**：模拟不同任务长度，看 DP 决策日志（`net_benefit` 数值）合理。短任务 NetBenefit < 0 时不 compact；长任务自动选择最优 k。

---

## 8. 测试清单

### 8.1 单元测试

`tests/test_compaction_policy.py` 新增：
- `test_decide_below_threshold_returns_noop`
- `test_decide_above_hard_threshold_forces_compact_with_keep_1`
- `test_decide_picks_optimal_k_when_multiple_positive`
- `test_decide_returns_noop_when_all_net_benefit_negative`
- `test_decide_handles_empty_live_messages`
- `test_decide_handles_single_round_cannot_drop_all`
- `test_distortion_term_grows_with_compact_count`
- `test_unknown_model_falls_back_to_conservative_pricing`
- `test_user_round_boundaries_handles_parallel_tool_calls`
- `test_user_round_boundaries_only_includes_real_user_messages` (排除 role="tool")
- `test_force_compact_keeps_only_recent_round`

`tests/test_cache_policy.py` 新增：
- `test_pricing_for_node_uses_protocol_family_and_model`
- `test_deepseek_pricing_uses_hit_miss_prices_without_explicit_cache_control`
- `test_pricing_for_node_falls_back_for_unknown_model`
- `test_pricing_for_node_handles_none_node`

`tests/test_llm_usage.py` 或现有 client 测试中新增：
- `test_anthropic_usage_parses_deepseek_prompt_cache_fields`
- `test_anthropic_usage_parses_standard_cache_fields`
- `test_openai_usage_parses_prompt_tokens_details_cached_tokens`
- `test_bp4_skips_when_only_thinking_blocks`

### 8.2 集成测试

修改 `tests/test_agent.py`：
- `test_ingest_truncation_for_oversized_tool_result`
- `test_no_l1_l2_mutation_on_old_messages` (跑 10 round，对比 round 5 的 message content 在 round 10 时仍字节级一致)
- `test_compaction_replaces_current_summary` (触发 DP compact，验证 `current_summary` 不为 None)
- `test_cache_aligned_summary_call_uses_same_tools` (mock router.internal_call，断言 tools 参数等于主请求)
- `test_render_system_blocks_correct_breakpoint_placement`
- `test_render_system_blocks_no_bp2_when_no_pinned_no_summary`
- `test_render_system_blocks_bp2_promoted_to_pinned_when_no_summary`
- `test_context_overflow_recovery_triggers_forced_compact`
- `test_summary_failure_normal_path_defers_compact` (正常路径 summary 失败，dropped 不丢)
- `test_summary_failure_forced_path_uses_deterministic_fallback`
- `test_clear_resets_all_compaction_state`

### 8.3 手动验证

跑一个 40-round 的长对话，日志里看：
- DeepSeek `cache_read_tokens / prompt_tokens` 稳态比例逐步升高（不设硬性 60%，因为 DeepSeek 是 best-effort 自动 cache）
- `compact_count` 增长曲线（应该平滑递增，不卡在某个 turn）
- 每次 compact 的 `net_benefit` 数值（应该 > 0，除非 forced）
- summary call 的 `cache_read_tokens` / `cache_miss_tokens`，用于判断稳定前缀是否被 DeepSeek 自动 cache 命中

---

## 9. 显式不做的事

| 不做 | 理由 |
|---|---|
| 保留固定 `keep_recent_n=3` 作为主策略 | 用 DP 替代 |
| 把旧 tool_result 替换成 placeholder | 反 cache，删掉 |
| 给 Message schema 加 `cache_control` 字段 | cache_control 不是 message state，挂请求 dict 上即可 |
| cold_summaries 用 list+cap+sliding merge | [v1 简化] 用 single rolling current_summary |
| 追踪 BP #4 anchor index 让 DP 命中保证 | [v1 简化] 公式悲观估算，无需 anchor |
| 在 ToolResult 构造时截断 | 让每个 tool 自己管，太分散 |
| 跨 provider 统一 cache 实现 | 各 provider 的 cache 语义不同；v1 只建模 DeepSeek/Anthropic/OpenAI 的价格与 usage，不做统一控制层 |
| stream-json 输出 | 不在本次范围 |
| session 持久化（jsonl） | 不在本次范围 |
| 启用 Anthropic extended-cache-ttl beta（1h） | 不在本次范围，5min 默认够 |
| Failover 时动态更新 pricing | [v1 简化] 错向"少 compact"安全 |

---

## 10. 关键 invariants（review 时逐条对照）

- [ ] `live_messages` 中已经 append 过的 `Message` 对象**永不被修改字段**（emergency content truncation 是唯一例外）
- [ ] **`Message` schema 没有 `cache_control` 字段**——cache_control 只挂在请求渲染后的 dict 上
- [ ] **`_rebuild_system_prompt` 已删除**；`self.system_prompt` 字段不存在；pinned notes 只由 `_render_system_blocks` 渲染
- [ ] DeepSeek 节点不注入任何 `cache_control`；官方 Anthropic 节点主请求最多 4 个 breakpoint（启动早期 BP #2 缺席时为 3）
- [ ] **Summary call 不在 dropped messages 上打 BP**；官方 Anthropic 只打 BP #1 / #2 / #3；DeepSeek 不打显式 BP
- [ ] Summary call 的 system blocks / tools 数组与主请求保持同样稳定顺序；DeepSeek 命中是 best-effort，不写“必然命中”
- [ ] DP 切分点永远是 `live_messages` 里 `role="user"` 的位置（不切 tool 边界）
- [ ] Forced compact 至少保留最近 1 个 user-round
- [ ] DeepSeek / OpenAI / MiniMax / 未知 provider 默认不注入 `cache_control`；OpenAI format 还要 flatten system list → str
- [ ] `_skip_next_token_check` flag 已删除
- [ ] `_truncate_old_tool_results` / `_truncate_old_readfile_results` 已删除
- [ ] `_content_truncate_large_tool_results` 仅在 emergency 兜底路径调用
- [ ] `cold_summaries: list` 已替换为 `current_summary: ContextSummary \| None`
- [ ] `TokenUsage.prompt_tokens` 语义不变（总 input，含 cache_read + cache_creation），新增字段仅作细分
- [ ] `_primary_node` 在 `__init__` 后**不再更新**
- [ ] `LLMClientBase.generate` 抽象签名包含 `attach_message_bp: bool = True` 与 `enable_cache_control: bool = False`；所有子类（Anthropic / OpenAI / fake / mock）都接受这两个 kwarg
- [ ] `AnthropicClient._convert_tools` 在挂 `cache_control` 时**shallow-copy** 最后一个 dict（`result[-1] = {**result[-1], "cache_control": {...}}`），不直接 mutate
- [ ] `AnthropicClient._convert_messages` 挂 BP #4 时正确处理三个边界：字符串 content（先提升为 list）、跳过 `thinking` block、全是 thinking 时放弃挂 BP
- [ ] DP 公式里 `P_summary_input = P_input`（**不是** `max(P_cache_write, P_input)`）—— summary call 不打 BP 所以 miss 时走 input 不走 cache_write
- [ ] DP snapshot 的 `api_input_token_estimate` 用 `_estimate_tokens()` 实时算，**不**用 `last_usage.prompt_tokens`（后者不含上次 call 后 append 的 tool_result）
- [ ] `ModelNode.supports_explicit_cache_control` 默认 `False`；`ModelNode.supports_automatic_context_cache` 默认 `False`；DeepSeek 配置为 explicit=false、automatic=true
- [ ] Agent `_build_compaction_snapshot` 只有在 explicit=false 且 automatic=false 时才把 `cache_read / cache_write` 强制设为 `input` 价
- [ ] 主循环里 `tool_list = list(self.tools.values())` **在 `_maybe_run_compaction(tool_list)` 调用之前**就定义好（否则 NameError）
- [ ] `_run_cache_aligned_summary` 返回后，user_goals deterministic 合并（prior + dropped 抽出去重）写回 `summary.user_goals`，不依赖 LLM 自觉

---

## 11. 附录：典型场景成本估算

以 DeepSeek V4 Pro 2026-05 促销价为例（cache miss input $0.435/M、cache hit input $0.003625/M、output $0.87/M；促销到 2026-05-31 15:59 UTC，实施前需重新核对官方价格）。

### 11.1 Summary call（v1 悲观估算 — 修正后）

| 段 | tokens | 价格 | 成本 |
|---|---|---|---|
| system base + tools | 6500 | cache hit（自动 cache，若命中） | $0.000024 |
| pinned + current_summary_OLD | 1500 | cache hit（若命中） | $0.000005 |
| dropped messages（H） | 40000 | **cache miss input**（v1 悲观，不假设命中） | $0.0174 |
| SUMMARY_INSTRUCTION | 200 | cache miss input | $0.000087 |
| 输出 | 500 | output | $0.000435 |
| **v1 悲观总成本** | | | **≈ $0.018** |

实际命中后（DeepSeek 自动 Context Caching 命中 dropped 前缀）会更低——但 v1 DP 决策按悲观值算，不强假设。

**乐观命中参考**：若 DeepSeek 自动 cache 把 dropped 40K tokens 也命中，则 dropped 成本约 `40000 × $0.003625 / 1M = $0.000145`，整个 summary call 约 `$0.001` 量级。悲观 `$0.018` 与乐观 `$0.001` 相差约 18×；v1 DP 按悲观值决策，实际收益可能更高。

**对比 v0 朴素 summary call**（独立 prompt，~45K 全 input）：
| 段 | tokens | 价格 | 成本 |
|---|---|---|---|
| 整个独立 prompt | 45000 | cache miss input | $0.0196 |
| 输出 | 500 | output | $0.000435 |
| **总** | | | **≈ $0.020** |

v1 比 v0 的 summary call 本身只小幅节省；DeepSeek 真正的收益来自长期稳定前缀的自动 cache 命中，以及 compact 降低未来上下文体积/overflow 风险。不要把 Anthropic 的 80-90% breakpoint 节省直接套到 DeepSeek。

### 11.2 Compact 后第 1 次主请求（DeepSeek 自动 cache，保守估算）

| 段 | tokens | 价格 | 成本 |
|---|---|---|---|
| system base + tools | 6500 | cache hit（若命中） | $0.000024 |
| pinned + current_summary_NEW | 1500 | cache miss input | $0.000653 |
| current plan | 200 | cache miss input | $0.000087 |
| live_messages（kept） | 5000 | cache miss input | $0.002175 |
| new user input | 100 | cache miss input | $0.000044 |
| 输出 | 1000 | output | $0.00087 |
| **总** | | | **≈ $0.0039** |

### 11.3 Compact 之后第 2 次起的稳态主请求

| 段 | tokens | 价格 | 成本 |
|---|---|---|---|
| system base + pinned + current_summary | 8000 | cache hit（若命中） | $0.000029 |
| tools | 1500 | cache hit（若命中） | $0.000005 |
| current plan | 250 | cache miss input | $0.000109 |
| live_messages | 5500 | cache hit（若命中） | $0.000020 |
| 新一轮新增 | 500 | cache miss input | $0.000218 |
| 输出 | 1000 | output | $0.00087 |
| **总** | | | **≈ $0.00125** |

### 11.4 长任务对比

DeepSeek 下不能简单套 Anthropic 的"compact 一次省 80%"结论：
- 如果 DeepSeek 自动 cache 命中率很高，不 compact 的旧前缀本身也很便宜，经济上可能不急着 compact。
- 如果 plan/tool_result 频繁扰动导致 cache miss，或上下文接近 1M/本地 token limit，compact 仍然有价值。
- 因此 DeepSeek 版 DP 必须使用实际 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 日志校准，不要只看理论价格表。

---

## 12. 给实现 agent 的最后叮嘱

1. **先读 §2.3** —— 不要把 ingest 截断和 L1/L2 混淆。
2. **`live_messages` 的 Message 对象在任何路径都不要 mutate** —— 唯一例外是 emergency content truncation。
3. **Message schema 不要加 `cache_control` 字段** —— cache_control 是请求渲染时的 dict 元数据。
4. **Summary call 上不要打 BP #4** —— DeepSeek 不打显式 BP；官方 Anthropic 只打 BP #1 / #2 / #3。这是 v1 关键决策。
5. **Summary call 失败要分 forced / 正常路径处理** —— 正常路径放弃 compact，forced 用 deterministic fallback。绝不在正常路径丢 dropped。
6. **DP 决策器是纯函数** —— 单测先于集成测试。
7. **`P_summary_input = P_input`**（**不是** `max(P_cw, P_in)`）—— v1 dropped 不打 BP，miss 时走 input 不走 cache_write。这条之前文档有误，已更正。
8. **`LLMClientBase.generate` 签名加 `attach_message_bp` / `enable_cache_control` 时，所有子类都必须接受这两个 kwarg**（OpenAI / fake 接受但忽略）—— 漏一个就 TypeError。
9. **`_convert_tools` 挂 cache_control 时 shallow-copy** —— `result[-1] = {**result[-1], "cache_control": ...}`，不要直接 `result[-1]["cache_control"] = ...`，否则会污染 self.tools 里的对象。
10. **BP #4 挂载处理三个边界**：assistant content 是 str（提升为 list）/ 是空 list / 全是 thinking block —— 详见 §4.2 改动 4 伪代码。
11. **`tool_list = list(self.tools.values())` 必须在 `_maybe_run_compaction(tool_list)` 调用之前定义** —— 这是 v0 主循环里的顺序问题，要主动移上去。
12. **DeepSeek V4 Pro 配置为 `supports_explicit_cache_control=False` + `supports_automatic_context_cache=True`** —— 不注入 Anthropic marker，但 DP 必须使用 DeepSeek hit/miss 价格。MiniMax / 未知 provider 默认两者都 false，才退化为 `cache==input`。
13. **DP snapshot 用 `_estimate_tokens()` 算当前 context** —— 不要用 `last_usage.prompt_tokens`，那是上次 call 时的值，之后追加的 tool_result 不在内。
14. **user_goals deterministic 合并** —— 在 `_maybe_run_compaction` 拿到 LLM summary 后，强制把"prior summary 的 user_goals + 从 dropped 抽出的 user_goals"去重合并写回，不依赖 LLM 自觉保留旧条目。
15. **Phase 1 / 2 / 3 分阶段提交** —— 每个阶段都能跑、能回滚、能 review。**Phase 1 一次性完成状态结构改造**（cold_summaries → current_summary），避免跨阶段维护两套 `_render_system_blocks`。
16. **遇到 schema 兼容性问题（OpenAI 不认 cache_control / system list 等）** —— 方向永远是"client 内部静默处理"，不要往 Agent 层泄漏。
17. **如果发现 DP 公式与本文档某项不一致**，**以本文档为准**，不要回去翻 bash-agent 源码——bash-agent 的实现可能为了 bash 限制做了变体。
18. **`_rebuild_system_prompt` 删除时一并删 `self.system_prompt` 字段** —— 不要留半边代码导致状态分裂。
19. **`cold_summaries` 改为 `current_summary` 时，注意 `/clear` 路径也要同步** —— [agent.py:131-141](../mini_agent/agent.py#L131)。
20. **价格表是 illustrative 值**，[cache_policy.py 最后核对日期 2026-05](../mini_agent/compaction/cache_policy.py)。实施前请到 Anthropic 官方页面核对最新价格再合并。
