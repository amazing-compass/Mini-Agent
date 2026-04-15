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
- **操作**：N 轮之前的 `name == "read_file"` 的 tool 消息，content 替换为 `[Previous read_file: {从 arguments 提取的路径}]`
- **成本**：FREE（原文件在磁盘，LLM 需要时重新 read_file）
- **新方法**：`_truncate_old_readfile_results(self, keep_recent_n: int = 3)`

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
3. **L4 测试**：L1+L2 后仍超限，检查最终 messages 变为 `[system, summary_message, ...最近 N 轮 messages]`，summary_message 包含旧 user prompts + 结构化 LLM 摘要。
4. **Pinned 测试**：调用 record_note → 检查 messages[0].content 包含 note 内容 → 触发 L4 压缩 → 检查 note 仍在 messages[0] 中。
5. **跨 session 测试**：记录 note → 退出 → 重启 → 检查 notes 自动加载到 system prompt。
6. **运行实际任务**：启动 CLI，给一个需要多步工具调用的任务，观察压缩行为。
