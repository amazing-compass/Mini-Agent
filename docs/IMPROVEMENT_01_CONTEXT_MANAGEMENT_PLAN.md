# Mini-Agent 上下文管理优化方案

## Context

当前项目的上下文压缩只有一种策略：超限后对每轮 assistant/tool 做 LLM 摘要。Codex 指出三个问题：
- **问题 2**：没有"先轻压、再重压"的层级化策略
- **问题 3**：摘要产物是自由文本，不够结构化，LLM 难以快速恢复工作状态
- **问题 4**：没有统一的上下文层次模型（热/钉住/冷/可召回）

基于之前的讨论，我们已经设计了方案 A（L1/L2/L4 三级压缩）。本方案将 方案 A + 结构化摘要 + 钉住元信息（Pinned Metadata）整合为完整实现。

---

## 完整方案：四层上下文模型

```
┌────────────────────────────────────────────────────┐
│ 层级          │ 内容              │ 存活范围        │
├────────────────────────────────────────────────────┤
│ 钉住 (Pinned) │ 关键决策/目标/路径  │ 所有压缩 + /clear │
│ 热 (Hot)      │ 最近 N 轮完整消息   │ L1/L2 保留       │
│ 冷 (Cold)     │ L4 摘要           │ L4 后保留        │
│ 可召回        │ 跳过（不实现）      │ -               │
└────────────────────────────────────────────────────┘
```

---

## Part 1: 三级压缩（方案 A）

### 改动文件：`mini_agent/agent.py`

### 1.1 L1 — 截断旧 tool 结果（非 read_file）

- **时机**：每次循环前（替代当前的 `_summarize_messages` 入口）
- **操作**：遍历 messages，找到 N 轮之前的 `role="tool"` 且 `name != "read_file"` 的消息，将 content 替换为 `[Previous {name} executed successfully]`
- **成本**：FREE
- **新方法**：`_truncate_old_tool_results(self, keep_recent_n: int = 3)`

### 1.2 L2 — 截断旧 read_file 结果

- **时机**：L1 之后仍超限时
- **操作**：N 轮之前的 `name == "read_file"` 的 tool 消息，content 替换为 `[Previous read_file: {文件路径}]`
- **成本**：FREE（原文件在磁盘，LLM 需要时重新 read_file）
- **新方法**：`_truncate_old_readfile_results(self, keep_recent_n: int = 3)`

#### ⚠️ 路径提取问题

当前 tool 消息（`agent.py:495-500`）构造时**不保存 arguments**，Message schema（`schema.py:29-37`）也没有 arguments 字段。文件路径只存在于前面 assistant 消息的 `tool_calls[].function.arguments` 里。

**解法：通过 `tool_call_id` 回查参数（不改 schema）**

运行链路中天然保留了关联：assistant 消息有 `tool_calls`（含完整参数），tool 消息有 `tool_call_id`。先建索引，再取参数：

```python
def _build_tool_call_args_index(self) -> dict[str, dict]:
    """构建 tool_call_id → arguments 的索引"""
    index = {}
    for msg in self.messages:
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                index[tc.id] = tc.function.arguments
    return index
```

L2 截断时，用索引取参数，生成带路径的占位符：

```python
args_index = self._build_tool_call_args_index()

for msg in old_tool_messages:
    if msg.name == "read_file" and msg.tool_call_id:
        args = args_index.get(msg.tool_call_id, {})
        path = args.get("file_path", "unknown")
        # 保留 offset/limit 信息，让 LLM 知道是整文件还是局部读取
        offset = args.get("offset")
        limit = args.get("limit")
        if offset or limit:
            params = ", ".join(f"{k}={v}" for k, v in [("offset", offset), ("limit", limit)] if v)
            msg.content = f"[Previous read_file: {path} ({params})]"
        else:
            msg.content = f"[Previous read_file: {path}]"
```

占位符示例：
- `[Previous read_file: mini_agent/agent.py]` — 整文件读取
- `[Previous read_file: mini_agent/agent.py (offset=120, limit=80)]` — 局部读取

回查失败时降级为 `[Previous read_file: unknown]`。

**为什么不扩展 Message schema 存 arguments**：
- 回查已经能解决问题，不需要先改 schema
- 不会把参数在 assistant 和 tool 两处重复存储
- 不引入额外协议层负担
- 如果后续需要更通用的 compaction/recall/replay，再考虑给 Message 加内部字段

**注意**：L1 的截断标记 `[Previous {name} executed successfully]` 只用 `msg.name`，不需要 arguments，不受此问题影响。

### 1.3 L4 — 全量压缩（保留最近 N 轮）

- **时机**：L1 + L2 之后仍超限时
- **操作**：
  1. 保留最近 N 轮完整消息（不动）
  2. N 轮之前的消息：拼接 user prompts（确定性，零损失）+ 1 次 LLM 调用做**结构化摘要**
  3. messages 变为：`[system, summary_message, ...最近 N 轮完整 messages...]`
- **成本**：1 次 LLM 调用（输入是 N 轮之前的、已被 L1/L2 截断过的消息，体量不大）
- **改造现有方法**：`_summarize_messages()` 重写
- **N 的取值**：与 L1/L2 共用同一个 N（默认 3），三级压缩统一保护最近 N 轮

#### L4 结构化摘要格式

L4 的 LLM 摘要 prompt 要求按固定格式输出，而非自由文本。这样 LLM 压缩后能快速恢复工作状态：

```
## Completed Work
- 探索项目结构，确认入口为 cli.py
- 修改 agent.py 的压缩逻辑
- 创建 test_agent.py

## Active Files
- agent.py（已修改）
- test_agent.py（新建）

## Key Findings
- 项目用 Python 3.12
- token 限制 80000
- 压缩在每次循环前触发

## Pending / TODO
- 测试尚未运行
- L2 截断逻辑待验证
```

**优点**：LLM 看到结构化摘要后，能快速定位"我在做什么、改了哪些文件、还有什么没做"，而不是从一段散文里慢慢找。

**实现**：仅需修改 `_full_compress()` 中调用 LLM 的 prompt，加上输出格式要求即可，不涉及代码逻辑变动。

### 1.4 "轮次"的定义

需要确定"N 轮"的边界。一轮 = 一个 user message 到下一个 user message 之间的所有消息。用 `user_indices` 来划分（现有代码已有此逻辑，line 213）。

### 1.5 压缩流程

```python
async def _compress_context(self):
    """三级压缩，替代原 _summarize_messages"""
    if self._skip_next_token_check:
        self._skip_next_token_check = False
        return

    estimated = self._estimate_tokens()
    if estimated <= self.token_limit and self.api_total_tokens <= self.token_limit:
        return

    # L1: 截断非 read_file tool 结果
    self._truncate_old_tool_results(keep_recent_n=3)

    estimated = self._estimate_tokens()
    if estimated <= self.token_limit:
        return

    # L2: 截断 read_file 结果
    self._truncate_old_readfile_results(keep_recent_n=3)

    estimated = self._estimate_tokens()
    if estimated <= self.token_limit:
        return

    # L4: 全量压缩（保留最近 N 轮，摘要之前的）
    await self._full_compress(keep_recent_n=3)
    self._skip_next_token_check = True
```

---

## Part 2: 钉住元信息（Pinned Metadata）

### 核心思路

**notes 注入 system prompt → system prompt 永远保留 → notes 自动存活所有压缩。**

零压缩逻辑改动。`messages[0]`（system prompt）在所有压缩级别和 `/clear` 中都保留，让 pinned notes 成为 system prompt 的动态扩展即可。

### 改动文件及内容

#### 2.1 `mini_agent/agent.py` — Agent 类

新增字段：
```python
self.pinned_notes: list[dict] = []
self._base_system_prompt: str = system_prompt  # 原始 system prompt
```

新增方法：
```python
def _rebuild_system_prompt(self):
    """将 pinned notes 注入 system prompt"""
    # 拼接 _base_system_prompt + pinned notes section
    # 更新 self.messages[0].content

def load_pinned_notes(self, memory_file: str):
    """启动时从 JSON 文件加载已有 notes"""
```

在 `run()` 循环中，tool 执行后拦截 `record_note`：
```python
# agent.py run() 中，tool_msg append 之后（约 line 501）
if function_name == "record_note" and result.success:
    self.pinned_notes.append({
        "category": arguments.get("category", "general"),
        "content": arguments.get("content", ""),
    })
    self._rebuild_system_prompt()
```

pinned notes 的 token 上限：~4000 字符（约 1000 tokens），超出时保留最新的。

#### 2.2 `mini_agent/cli.py`

Agent 创建后加载已有 notes：
```python
if config.tools.enable_note:
    agent.load_pinned_notes(str(workspace_dir / ".agent_memory.json"))
```

#### 2.3 `mini_agent/tools/note_tool.py`

仅更新 `RecallNoteTool.description`，说明 notes 已自动 pin 到 system prompt，不需要频繁调用 recall。

#### 2.4 `mini_agent/config/system_prompt.md`

末尾添加 Session Notes 使用指南（~5 行）。

### 不改动的文件

- `schema.py` — 无新字段
- `note_tool.py` 的 `SessionNoteTool` — 不改代码（Option B：Agent 侧拦截）
- `config.py` — 用现有 `enable_note` 开关

---

## 改动量汇总

| 文件 | 改动 | 约行数 |
|------|------|--------|
| `agent.py` | L1/L2/L4 压缩 + pinned notes | ~120 行 |
| `cli.py` | 加载 pinned notes | ~3 行 |
| `note_tool.py` | 更新 description | ~2 行 |
| `system_prompt.md` | 添加 notes 指南 | ~5 行 |
| **合计** | | **~130 行** |

---

## 验证方式

1. **L1 测试**：构造超过 token_limit 的消息历史，包含多轮 bash/ls tool 结果。触发压缩后检查旧 tool 结果被替换为 `[Previous ...]`，最近 N 轮保持原样。
2. **L2 测试**：L1 后仍超限，检查旧 read_file 结果也被截断。
3. **L4 测试**：L1+L2 后仍超限，检查最终 messages 变为 `[system, 合并消息]`，合并消息包含所有 user prompt + LLM 摘要。
4. **Pinned 测试**：调用 record_note → 检查 messages[0].content 包含 note 内容 → 触发 L4 压缩 → 检查 note 仍在 messages[0] 中。
5. **跨 session 测试**：记录 note → 退出 → 重启 → 检查 notes 自动加载到 system prompt。
6. **运行实际任务**：启动 CLI，给一个需要多步工具调用的任务，观察压缩行为。

---

## 附录：Codex 提出的架构级重构方案（待评估）

> 以下是 Codex 建议的更彻底的方案。核心思想：**把"内部存储"和"API 发送"拆开**，不再让 `self.messages` 身兼两职。目前仅作记录，尚未决定是否采纳。

### 问题根源

当前 `self.messages: list[Message]` 同时承担两个职责：
1. **内部存储**：agent.py 用来记录所有历史、做压缩
2. **API 输入**：直接传给 LLM client 发请求

这导致所有内部概念（summary、pinned notes）都必须塞进 `Message(role=???)` 里，而 API 只认 `system/user/assistant/tool` 四种 role。于是：
- summary 用 `role="user"` → 可能导致连续 user，Anthropic 不兼容
- summary 塞进 system prompt → system prompt 膨胀且不可压缩
- 插 dummy assistant → hack，不是根本解法

### 方案：拆分内部存储 + 渲染层

#### 新增数据结构

```python
class ContextSummary(BaseModel):
    """L4 压缩产物，独立于 Message"""
    covered_rounds: list[int]       # 覆盖了哪些轮次
    user_goals: list[str]           # 用户原始 prompt（拼接保留）
    completed_work: list[str]       # 已完成的工作
    active_files: list[str]         # 活跃文件
    key_findings: list[str]         # 关键发现
    pending_todo: list[str]         # 待办事项
    raw_text: str                   # 渲染后的文本（发给 API 用）
```

#### Agent 内部存储拆分

```python
# 现在：
self.messages: list[Message]  # 一个列表管所有事

# 改为：
self.system_prompt: str                    # 固定的系统提示，不会膨胀
self.pinned_notes: list[dict]              # 钉住的元信息
self.cold_summaries: list[ContextSummary]  # L4 摘要（可以有多条）
self.live_messages: list[Message]          # 最近 N 轮的原始消息
```

每一块有独立的生命周期和压缩策略：

| 层 | 存储类型 | 压缩策略 | 上限控制 |
|---|---------|---------|---------|
| system_prompt | str | 不压缩 | 固定不变 |
| pinned_notes | list[dict] | 超限丢旧的 | ~4000 字符 |
| cold_summaries | list[ContextSummary] | 多条可合并/替换 | render 时控制 |
| live_messages | list[Message] | L1/L2 截断 | 最近 N 轮 |

#### 渲染层：render_for_provider()

发送 API 请求前，把四块组装成合法的 message list：

```python
def render_for_provider(self) -> list[Message]:
    """把内部存储组装成 API 合法的消息序列"""
    result = []

    # 1. System prompt（固定，不含 pinned notes 和 summary）
    result.append(Message(role="system", content=self.system_prompt))

    # 2. 上下文块：pinned notes + cold summaries → 合并成一条 user message
    context_parts = []
    if self.pinned_notes:
        notes_text = "\n".join(f"- [{n['category']}] {n['content']}" for n in self.pinned_notes)
        context_parts.append(f"## Pinned Context\n{notes_text}")
    if self.cold_summaries:
        # 多条 summary 合并成一段文本
        merged = "\n\n".join(s.raw_text for s in self.cold_summaries)
        context_parts.append(f"## Historical Summary\n{merged}")

    if context_parts:
        result.append(Message(role="user", content="\n\n".join(context_parts)))
        # 插一条 assistant 保证 user/assistant 交替（兼容 Anthropic）
        result.append(Message(role="assistant", content="Understood, continuing with the task."))

    # 3. 最近 N 轮原始消息（原样）
    result.extend(self.live_messages)

    return result
```

#### 压缩逻辑变化

```python
async def _compress_context(self):
    # token 估算：对 render_for_provider() 的结果估算
    rendered = self.render_for_provider()
    estimated = self._estimate_tokens_for(rendered)

    if estimated <= self.token_limit:
        return

    # L1: 截断 live_messages 中的旧 tool 结果
    self._truncate_old_tool_results(keep_recent_n=3)

    # L2: 截断 live_messages 中的旧 read_file 结果
    ...

    # L4: live_messages 中 N 轮之前的部分 → 生成 ContextSummary → 存入 cold_summaries
    #     live_messages 只保留最近 N 轮
    old_messages = self.live_messages[:split_point]
    self.live_messages = self.live_messages[split_point:]
    new_summary = await self._create_context_summary(old_messages)
    self.cold_summaries.append(new_summary)
```

#### LLM 调用改为使用 render 结果

```python
# 现在：
response = await self.llm.generate(messages=self.messages, tools=tool_list)

# 改为：
rendered_messages = self.render_for_provider()
response = await self.llm.generate(messages=rendered_messages, tools=tool_list)
```

### 这个方案解决了什么

1. **summary 不需要 role** — 它是独立的 `ContextSummary` 对象，不是 Message
2. **system prompt 不会膨胀** — pinned notes 和 summary 在 render 时组装，不塞进 system prompt
3. **不会重复压缩 summary** — summary 在 `cold_summaries` 里，不在 `live_messages` 里，L1/L2/L4 只操作 `live_messages`
4. **API 兼容** — render 层负责处理 role 交替等协议细节
5. **多次 L4 不丢信息** — 每次 L4 产生一条新 ContextSummary 追加到 cold_summaries，旧的不会被再次摘要。render 时合并输出

### 代价

- **改动量大**：agent.py 围绕 `self.messages` 的所有逻辑都要改（token 估算、消息追加、日志记录、取消清理、/clear、/history 等）
- **调试复杂度增加**：内部存储和实际发送内容不一致，排查问题时需要看 render 结果
- **当前方案（Part 1 + Part 2）已经能用**：如果只用 MiniMax API，连续 user 不是问题，不一定需要这么大的重构
