# IMPROVEMENT 04 — Code Review Report

**Reviewer:** independent agent (no prior context on this branch)
**Date:** 2026-05-11
**Scope:**
- 全量对照 `docs/IMPROVEMENT_04_CACHE_AWARE_COMPACTION.md` 的实现
- 整仓回归审查（agent / router / clients / config / tests / benchmarks）

**Verification baseline:** `pytest tests/ -q` → **428 passed, 1 skipped** （含 `test_compaction_policy` 11、`test_cache_policy` 8、`test_llm_cache_unit` 12、`test_agent_compaction` 12、`test_agent_overflow_recovery` 4、`test_config_pool` 15、`test_mcp` 29 + 1 skip）。

---

## 摘要

IMPROVEMENT 04 文档列出的所有硬性 invariant（§10 / §12）**全部通过**——核心改造可以合并。

但本 PR 同时带入了 **6 个范围外或边缘问题** 需要处理，按合并风险排序如下。

| 优先级 | 编号 | 题目 | 备注 |
|---|---|---|---|
| **P0 阻塞合并** | F1 | README 还引用被删的 docs 文件，链接和图片全裂 | 合并前必修，1 个 commit 就能搞定 |
| **P0 阻塞合并** | F3 | `AgentLogger` 在受限 HOME 下直接 `PermissionError`，破坏测试隔离和沙箱运行 | 不是 IMPROVEMENT_04 引入，但 14/15 个 planner/session 集成测试在 `HOME=/dev/null` 下挂 |
| **P1 应修** | F2 | `swebench/task_runner.py` 把 `len(agent.live_messages)` 当 `steps_taken` | compaction 后会下降，trajectory 丢 dropped 段 |
| **P1 应修** | F4 | `_attach_bp4` 在 list-shape content 上原地改 `msg.content` 列表 | 当前 schema 允许的路径今天触发不到（assistant 实际只用 str），是 latent invariant violation |
| **P2 语义** | F5 | `ContextSummary.covered_rounds` 是"摘要内局部编号"，与设计文档伪代码不符 | 跨多次 compact 失去全局轮次语义；今天没有下游读它 |
| **P2 文档** | F6 | 多处 docstring/注释仍写 "L1/L2/L4 compression" | 误导维护者，全局 grep 替换 |
| **P3 边缘** | F7 | `internal_call` 选 node 的策略与 `call` 不一致，可能让 summary 落到不同 node | 异质池里 cache prefix 假设可能失效 |
| **P3 cleanup** | F8 | `_content_truncate_large_tool_results` 仍跳过 `[Previous ` 前缀的 tool message | v0 占位写入路径已删，但旧 session 兼容性留下的死分支 |

---

## P0 阻塞合并

### F1 — README 链接和图片在 PR 中批量失效

**状态：合并前必修**

`.gitignore` 中 `docs/` 被移除（这是为了让 `IMPROVEMENT_04_CACHE_AWARE_COMPACTION.md` 能被 git 看到），同时**整个 `docs/` 目录的旧文件物理删除**（用户确认是有意精简文档）：

```
D  docs/DEVELOPMENT_GUIDE.md
D  docs/DEVELOPMENT_GUIDE_CN.md
D  docs/PRODUCTION_GUIDE.md
D  docs/PRODUCTION_GUIDE_CN.md
D  docs/assets/demo1-task-execution.gif
D  docs/assets/demo2-claude-skill.gif
D  docs/assets/demo3-web-search.gif
```

但 `README.md` / `README_CN.md` 还在引用这些路径：

```
README.md:208      [Development Guide](docs/DEVELOPMENT_GUIDE.md)
README.md:210      [Production Guide](docs/PRODUCTION_GUIDE.md)
README.md:248      ![Demo GIF 1](docs/assets/demo1-task-execution.gif)
README.md:254      ![Demo GIF 2](docs/assets/demo2-claude-skill.gif)
README.md:260      ![Demo GIF 3](docs/assets/demo3-web-search.gif)
README.md:314-315  开发指南 / 生产指南
README_CN.md:208-260, 315-316   同上中文版
```

**后果：** 合并后 GitHub 首页所有外链 404、图片全裂。

**修法（既然文档精简是有意的）：** 从 README.md / README_CN.md 中删掉这 5 行链接和 3 张 demo gif 引用即可。不需要恢复任何文件。

---

### F3 — `AgentLogger` 强制写 `~/.mini-agent/log`，无降级

**状态：合并前必修**（不是 IMPROVEMENT 04 引入的回归，但本次 review 暴露了它）

**复现：**

```bash
HOME=/dev/null .venv/bin/python -m pytest tests/test_agent_planner_integration.py tests/test_session_integration.py -q
# → 14 failed, 1 passed in 0.53s（PermissionError: ~/.mini-agent/log/...）
```

**证据：**

```python
# mini_agent/logger.py:25-26
self.log_dir = Path.home() / ".mini-agent" / "log"
self.log_dir.mkdir(parents=True, exist_ok=True)   # 无 try/except，无 fallback
```

```python
# mini_agent/agent.py:918
self.logger.start_new_run()   # 每次 run 无条件触发
```

**影响：**
- 受限容器 / read-only HOME / 测试沙箱 / CI runner 用户没权限写 `~`，整个 agent 起不来
- 测试隔离破：14/15 个 planner+session 测试在 HOME 不可写时全挂

**修法：**

```python
# logger.py — 两种之一
# 方案 A: 优雅降级
try:
    self.log_dir = Path.home() / ".mini-agent" / "log"
    self.log_dir.mkdir(parents=True, exist_ok=True)
except OSError:
    self.log_dir = Path(tempfile.gettempdir()) / "mini-agent-log"
    self.log_dir.mkdir(parents=True, exist_ok=True)

# 方案 B: lazy init（推荐）
self.log_dir = Path.home() / ".mini-agent" / "log"
# 不在 __init__ 里 mkdir；start_new_run() 首次写时再 mkdir，且包 try/except
```

方案 B 更彻底：构造 Agent 不再有任何 filesystem 副作用，单测可以零配置跑。

---

## P1 应修

### F2 — `swebench/task_runner.py` 用 `len(live_messages)` 衡量 `steps_taken`

**证据：**

```python
mini_agent/benchmarks/swebench/task_runner.py:237: "steps_taken": len(agent.live_messages),
mini_agent/benchmarks/swebench/task_runner.py:358: steps_taken = len(agent.live_messages)
```

**分析：**

v0 时代 `live_messages` 单调累积，长度是合理的活动量代理。IMPROVEMENT 04 之后 `live_messages` 会因 DP/forced compact 显著缩水：

- `steps_taken` 在 compaction 触发后**会突然下降**——违反 "step 单调非递减" 的直觉
- `_serialize_messages`（task_runner.py:208）只写 `agent.live_messages`，**dropped 段从轨迹文件里彻底消失**

设计文档 §2.2 把 "session 持久化" 标为 out-of-scope，但 **benchmark trajectory 不是 session 持久化**——它是评测可重现性的依据。IMPROVEMENT 04 的 `cold_summaries → current_summary` 改造事实上让它退化了。

**修法：**

```python
# task_runner.py:237 + 358
steps_taken = agent.llm_call_count   # 新增的单调计数器，正好是 "LLM 调用 ≈ 一步"

# task_runner.py:208 — _serialize_messages 内
def _serialize_messages(agent: Agent) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # NEW: 如果有 current_summary，先把它作为 "compacted" 块写入轨迹
    if agent.current_summary is not None:
        out.append({
            "role": "_compacted_summary",
            "content": agent.current_summary.raw_text,
            "user_goals": list(agent.current_summary.user_goals),
        })

    for msg in agent.live_messages:
        try:
            out.append(msg.model_dump())
        except Exception as exc:
            logger.warning("Failed to dump message: %s", exc)
            out.append({"role": getattr(msg, "role", "?"), "content": str(msg)})
    return out
```

至少留住 summary 内容；要完全恢复 trajectory，需要把 dropped 原文也保存（v2 工作）。

---

### F4 — `_attach_bp4` 在 list-shape content 上原地修改 `msg.content` 列表

**状态：latent invariant violation**

**复现：**

```bash
$ python -c '
from mini_agent.llm.anthropic_client import AnthropicClient
from mini_agent.schema import Message

c = AnthropicClient(api_key="sk", api_base="https://t", model="m")
m = Message(role="assistant", content=[{"type": "text", "text": "hello"}])
_, api = c._convert_messages(
    [Message(role="user", content="q"), m],
    attach_message_bp=True, enable_cache_control=True,
)
print(m.content)
print("SAME LIST?", m.content is api[-1]["content"])
'
# →  [{'type': 'text', 'text': 'hello', 'cache_control': {'type': 'ephemeral'}}]
# →  SAME LIST? True
```

**证据：**

```python
# mini_agent/llm/anthropic_client.py:275
else:
    api_messages.append({"role": msg.role, "content": msg.content})  # 同引用

# mini_agent/llm/anthropic_client.py:330-334
for i in range(len(content) - 1, -1, -1):
    block = content[i]
    if isinstance(block, dict) and block.get("type") != "thinking":
        content[i] = {**block, "cache_control": {"type": "ephemeral"}}  # 改 LIST
        return
```

`content[i] = {**block, ...}` 替换的是 dict 元素（新 dict，没污染原 dict），**但承载它的 list 是 Message 的 list**——元素被替换 = list 被原地改。

**为什么今天爆不出来：**
- `_add_assistant_message` 用 `response.content`，Anthropic/OpenAI 解析器都把 text block 合成 `str`
- assistant content 在 mini-agent 运行时永远是字符串
- 字符串分支（[anthropic_client.py:317-325](../mini_agent/llm/anthropic_client.py#L317-L325)）走 `api_messages[idx]["content"] = [新 list]`，没碰 `msg.content`

**为什么应该修：**
- `Message.content` schema 允许 `str | list[dict]`
- 设计文档 §3.1.3 边界 4 自己写过 "shallow-copy 是防御性编程"
- 一旦哪天有人构造 `Message(role="assistant", content=[...])`，invariant 立刻破

**修法（一行）：**

```python
# 替换
content[i] = {**block, "cache_control": {"type": "ephemeral"}}

# 为
new_content = list(content)  # shallow-copy 列表本身
new_content[i] = {**block, "cache_control": {"type": "ephemeral"}}
api_messages[last_asst_idx]["content"] = new_content
return
```

---

### F5 — `ContextSummary.covered_rounds` 是"摘要内局部编号"

**状态：latent semantic 漂移**

**证据：**

```python
# mini_agent/agent.py:518-521 (LLM path)
rounds_in_dropped = len([m for m in dropped if m.role == "user"])
return ContextSummary(
    covered_rounds=list(range(1, rounds_in_dropped + 1)),
    ...
)

# mini_agent/agent.py:649-651 (deterministic fallback)
rounds_in_dropped = len([m for m in dropped if m.role == "user"])
return ContextSummary(
    covered_rounds=list(range(1, rounds_in_dropped + 1)),
    ...
)
```

**实际语义：**

- 第 1 次 compact 丢 3 个 user round → `covered_rounds=[1,2,3]`
- 第 2 次 compact 又丢 2 个 user round → `covered_rounds=[1,2]`（**不是 [4,5]**）
- 跨多次 compact，`covered_rounds` 反复 reset

**与设计文档的差异：**

`IMPROVEMENT_04_CACHE_AWARE_COMPACTION.md` §3.5.6 伪代码：

```python
return ContextSummary(
    covered_rounds=[self.compact_count + 1],   # 全局 compact ID
    ...
)
```

**影响：**

今天没有任何下游代码读 `covered_rounds`，只是 schema 字段。属于 schema 语义漂移，不影响功能；但将来如果实现 session 持久化、trajectory 分析、debug 工具，这条会咬人。

**修法（任选其一，需先定语义）：**

```python
# 方案 A: 按伪代码（全局 compact ID，与 fallback 路径中 self.compact_count 一致）
covered_rounds=[self.compact_count + 1],

# 方案 B: 真实全局轮次区间（需要传入全局 base）
covered_rounds=list(range(self.user_turn_count - rounds_in_dropped + 1, self.user_turn_count + 1)),
```

建议方案 A——与现有伪代码一致，1 行就能改。

---

## P2 文档/注释

### F6 — 多处 docstring/注释仍写 "L1/L2/L4 compression"

**状态：误导维护者**

**证据：**

```
mini_agent/agent.py:77         router.internal_call 替换 the L4 summary path
mini_agent/agent.py:813        triggering the L1/L2/L4 compression + retry flow
mini_agent/llm/ha/router.py:11    agent's L4 summary
mini_agent/llm/ha/router.py:21    agent owns the L1/L2/L4 retry loop
mini_agent/llm/ha/router.py:298   the agent's L1/L2/L4 path is the right recovery
mini_agent/llm/ha/router.py:350   the agent's L4 summary
mini_agent/llm/ha/router.py:374   the L4 prompt is modest
mini_agent/llm/ha/budget.py:5     All compression decisions (L1/L2/L4) stay in the agent
```

实际行为：L1/L2 已删，L4 改名为 "forced DP compaction + cache-aligned summary"。

**修法：** 全局 sed 替换：

```
L1/L2/L4 compression  →  forced DP compaction
L4 summary            →  cache-aligned summary (internal_call)
L1/L2/L4 retry loop   →  ContextOverflow recovery loop
```

---

### F3-补 — `tests/test_agent_overflow_recovery.py` 注释过期

**证据：**

```
tests/test_agent_overflow_recovery.py:87   `_full_compress` has something to fold
tests/test_agent_overflow_recovery.py:99   the old code would skip `_full_compress()` ...
tests/test_agent_overflow_recovery.py:100  must call `_full_compress()` unconditionally
tests/test_agent_overflow_recovery.py:113  internal_call was used by _create_structured_summary
```

代码行为对（fake 跑通），但注释提到的方法名 `_full_compress` / `_create_structured_summary` 已被 IMPROVEMENT 04 删除并改名。

**修法：** 替换为新方法名 `_maybe_run_compaction` / `_run_cache_aligned_summary`。

---

## P3 边缘 / cleanup

### F7 — `internal_call` 与 `call` 可能选不同 node

**证据：**

```python
# router.py call()   — 三桶分类 + fits 预检 + failover
# router.py internal_call() — is_serving 过滤 + max(priority) + node_id 字典序
```

`call` 用 `is_passable` + `fits` 预检，**会 failover**；`internal_call` 用 `is_serving` + 取最高优先级，**无 fits 预检、无 failover**。

**异质池的边缘场景：**

- 池：节点 A（priority=100, context_window=50K）+ 节点 B（priority=80, context_window=200K）
- 主请求 messages 大 → A 不 fits → `call()` 落 B（priority 低但能装下）
- 触发 compact → `internal_call()` 选 max priority = A → A 仍然装不下 dropped + system → 抛错
- 落 `_maybe_run_compaction` 的 try/except：normal 路径 defer，forced 路径走 deterministic fallback

**不会崩，但：**
- "summary call 与上次主请求共享 prefix → cache hit" 的性能假设失效
- spec §6.14 声明 "failover 时 pricing 错向少 compact 是安全方向"，但**没显式讨论 `internal_call` 节点漂移**

**修法（可选，cache 优化向）：**

```python
async def internal_call(self, messages, tools=None):
    candidates = [n for n in self.pool.enabled() if self.breaker.is_serving(n.node_id)]
    if not candidates:
        raise NoAvailableNodeError(...)

    # NEW: 优先复用上次 call() 落到的节点（如果还 serving）
    last = getattr(self.last_routing_decision, "selected_node_id", None)
    if last is not None:
        preferred = next((n for n in candidates if n.node_id == last), None)
        if preferred is not None:
            node = preferred
        else:
            node = self._pick_by_priority(candidates)
    else:
        node = self._pick_by_priority(candidates)
    ...
```

低优；今天的 pool 大多同构，问题不显。

---

### F8 — `_content_truncate_large_tool_results` 仍跳过 `[Previous ` 前缀

**证据：**

```python
# mini_agent/agent.py:430-434
for msg in self.live_messages:
    if msg.role != "tool":
        continue
    if isinstance(msg.content, str) and msg.content.startswith("[Previous "):
        continue
```

- v0 L1/L2 占位是 `"[Previous read_file: foo.py]"` / `"[Previous bash: ...]"`
- IMPROVEMENT 04 删了占位**写入**路径，但保留了 emergency truncate 对它的 skip
- 因为 ingest 截断在 `_add_tool_message` 阶段就把 50K 以上截了，到 emergency 这一层不会有 50K 以上未截断的 tool result
- 所以 skip 分支事实上**变成死代码**（除非有遗留 jsonl 加载老 session）

**裁定：** 不是 bug，但可以删（IMPROVEMENT 04 删占位写入路径后已经不会再生成这种前缀）。如果坚持兼容旧 session，可改成更严格的判定（`startswith("[Previous ") and endswith("]") and len < 100`）以防误伤。

---

## 不予采纳的意见

记录一下被其他 reviewer 提出但**经验证不成立**的项，避免被后续 review 重复提：

**MCP unreachable/timeout 路径泄 `CancelledError`（不可复现）**

- 声明：`tests/test_mcp.py::test_connection_timeout_on_unreachable_server` 和 `test_per_server_timeout_override_in_config` 单独运行均失败
- 本环境验证：两条测试通过；`pytest tests/test_mcp.py -q` → 29 passed, 1 skipped
- 代码已显式处理 `BaseException`（[mcp_loader.py:284-291](../mini_agent/tools/mcp_loader.py#L284-L291)），注释明确说就是为了覆盖 `asyncio.CancelledError`
- 可能是对方环境 anyio/mcp 版本不同；当前仓库 + uv 锁定下不构成 bug
- **保留观察项**：升级 anyio 或 mcp 包后重新跑这两条

---

## 已验证通过的 IMPROVEMENT 04 不变量

按设计文档 §10 + §12 全部核对，全部通过，记录如下供后续 review 复用：

| 不变量 | 状态 | 证据 |
|---|---|---|
| `live_messages` Message 永不字段级修改 | ✓ | 仅 `_content_truncate_large_tool_results` 在 emergency 路径改 `msg.content`；其余路径只 append / slice |
| `Message` schema 不加 `cache_control` 字段 | ✓ | `schema.py:29-37` 未新增字段 |
| `_rebuild_system_prompt` / `self.system_prompt` / `cold_summaries` / `_skip_next_token_check` / `_truncate_old_tool_results` / `_truncate_old_readfile_results` 全删 | ✓ | grep 无残留 |
| DeepSeek 节点不注入 `cache_control` | ✓ | router 透传 `enable_cache_control=node.supports_explicit_cache_control`；client `enable=False` 时 strip system/messages/tools |
| Summary call 不在 dropped 上打 BP | ✓ | `router.internal_call` 传 `attach_message_bp=False` |
| DP 切分点仅 `role == "user"` | ✓ | `_user_round_boundaries` filter on role |
| Forced compact 至少保留 1 个 user-round | ✓ | `_force_compact` 用 `boundaries[-1]` |
| `LLMClientBase.generate` 抽象签名含 `attach_message_bp` / `enable_cache_control` | ✓ | Anthropic / OpenAI / 所有 test fake 都接受 |
| `_convert_tools` 挂 cache_control 时 shallow-copy | ✓ | `result[-1] = {**last, ...}` |
| BP #4 三个边界（str → block list / 全 thinking 放弃 / 跳 thinking 落 text） | ✓ | 三分支齐全且有专门单测 |
| OpenAI flatten `system: list[dict]` → `str` | ✓ | `openai_client.py:156-164` |
| `TokenUsage.prompt_tokens` 保持"总 input"语义 | ✓ | `prompt_tokens = uncached + cache_read + cache_creation` |
| DP `P_summary_input = P_input` （非 `cache_write`） | ✓ | `policy.py:91-93` |
| Pricing 退化仅在 explicit=false **且** automatic=false 时触发 | ✓ | `agent.py:715-727` |
| `tool_list` 在 `_maybe_run_compaction` 之前定义 | ✓ | `agent.py:938-942` |
| user_goals deterministic merge 同时写回字段 + raw_text | ✓ | 合并在 `_parse_structured_summary` 内部，字段与 raw_text 同源 |
| `config-example.yaml` 展示 DeepSeek explicit=false + automatic=true | ✓ | 配置文件第 98-117 行 |
| `/clear` 重置 7 个 compaction 相关字段 | ✓ | `messages.setter` 完整 |
| 测试不依赖真实 config.yaml / 真实 API key | ✓ | 全部用 `tempfile.TemporaryDirectory` + fake router |

---

## 建议合并顺序

1. **先修 F1 + F3**（两个 P0 阻塞项；F1 删 README 引用、F3 改 logger lazy init）
2. **再修 F2 + F4 + F5**（任意顺序；都是单文件修改，可在一个 commit 内做完）
3. **F6 + F3-补**：全局 sed 替换术语，单独 commit 方便 review
4. **F7 + F8**：可选优化，留到下个 PR
5. 合并

完整 IMPROVEMENT 04 主体代码 + 配套测试质量过硬，只是周边卫生没擦完。处理完上述项后，这套改造就可以放心进 main。
