好的，我已经把整个项目的所有核心代码过了一遍。下面给你列出项目中存在的**所有数据流链条**，你可以一条条对着代码追踪。

---

## Mini-Agent 项目数据流完整清单

### 链条 1：配置加载链

```
config.yaml 文件
  → Config.from_yaml() 解析
    → LLMConfig / AgentConfig / ToolsConfig / MCPConfig / RetryConfig
      → 分发给 LLMClient、Agent、各 Tool 初始化
```

**涉及文件**: `config.py`, `cli.py`（`run_agent` 函数）

---

### 链条 2：主循环 — 用户输入到 Agent 输出

```
用户终端输入 (prompt_toolkit)
  → user_input 字符串
    → agent.add_user_message() 追加到 messages
      → agent.run() 启动循环
        → llm.generate(messages, tools) 调 LLM
          → LLMResponse (content / thinking / tool_calls)
            → 有 tool_calls? → 执行工具 → 结果追回 messages → 继续循环
            → 无 tool_calls? → 返回 content 作为最终回复
```

**涉及文件**: `cli.py`（交互循环部分）, `agent.py`（`run` 方法）

---

### 链条 3：LLM 请求/响应转换链

```
内部 Message 列表
  → _convert_messages() 转成 API 特定格式
    → Anthropic: system 提取出来, tool 结果变 user+tool_result
    → OpenAI: system 留在 messages 里, reasoning_details 处理
  → _convert_tools() 把 Tool 对象转成 API schema
  → HTTP 请求发出 → API 返回原始响应
    → _parse_response() 解析回 LLMResponse(content, thinking, tool_calls, usage)
```

**涉及文件**: `llm/anthropic_client.py`, `llm/openai_client.py`

---

### 链条 4：LLM Provider 路由链

```
config.provider 字符串 ("anthropic" / "openai")
  → LLMClient.__init__() 判断
    → 拼接 api_base 后缀 (/anthropic 或 /v1，仅 MiniMax 域名)
      → 实例化 AnthropicClient 或 OpenAIClient
        → 统一暴露 generate() 接口
```

**涉及文件**: `llm/llm_wrapper.py`, `llm/base.py`

---

### 链条 5：Tool Schema 注册链

```
各 Tool 类定义 name / description / parameters 属性
  → to_schema() → Anthropic 格式 dict
  → to_openai_schema() → OpenAI 格式 dict
    → 在 LLM client 里被 _convert_tools() 消费
      → 作为 API 请求的 tools 参数发出
```

**涉及文件**: `tools/base.py`, `llm/anthropic_client.py`（`_convert_tools`）, `llm/openai_client.py`（`_convert_tools`）

---

### 链条 6：Tool 执行链

```
LLMResponse.tool_calls (list[ToolCall])
  → 遍历每个 ToolCall
    → 从 agent.tools dict 中查找 tool
      → tool.execute(**arguments) → 返回 ToolResult(success, content, error)
        → 构造 Message(role="tool", content=..., tool_call_id=...)
          → 追加到 agent.messages
```

**涉及文件**: `agent.py`（`run` 方法中 tool call 部分）

---

### 链条 7：工具加载与收集链

```
cli.py run_agent():
  ├─ initialize_base_tools(config):
  │   ├─ BashOutputTool, BashKillTool          ← config.tools.enable_bash
  │   ├─ create_skill_tools(skills_dir)        ← config.tools.enable_skills
  │   │    → GetSkillTool + SkillLoader
  │   └─ load_mcp_tools_async(mcp_config_path) ← config.tools.enable_mcp
  │        → list[MCPTool]
  │
  └─ add_workspace_tools(tools, config, workspace):
      ├─ BashTool(workspace_dir)
      ├─ ReadTool / WriteTool / EditTool(workspace_dir)
      └─ SessionNoteTool / RecallNoteTool(memory_file)
  
  → 所有 Tool 汇总为一个 list → 传给 Agent.__init__
    → Agent 内部转成 dict: {tool.name: tool}
```

**涉及文件**: `cli.py`（`initialize_base_tools`, `add_workspace_tools`）

---

### 链条 8：MCP 工具加载链

```
mcp.json 配置文件
  → load_mcp_tools_async() 读取 + 解析
    → 遍历每个 server 配置
      → _determine_connection_type() 判断 stdio/sse/http
        → MCPServerConnection.connect()
          → stdio_client / sse_client / streamablehttp_client 建立传输层
            → ClientSession(read_stream, write_stream) 建立会话层
              → session.initialize() + session.list_tools()
                → 每个 tool 包装为 MCPTool(name, desc, params, session)
  → 所有 MCPTool 汇总返回
```

**涉及文件**: `tools/mcp_loader.py`

---

### 链条 9：MCP 工具执行链

```
MCPTool.execute(**kwargs)
  → asyncio.timeout(execute_timeout) 包裹
    → session.call_tool(tool_name, arguments=kwargs)
      → MCP 协议发送请求到 server → server 执行 → 返回结果
        → 解析 result.content 列表
          → 拼接为 content_str → 返回 ToolResult
```

**涉及文件**: `tools/mcp_loader.py`（`MCPTool.execute`）

---

### 链条 10：Skill 加载链（Progressive Disclosure）

```
Level 1 (启动时):
  skills 目录 → rglob("SKILL.md") 递归查找
    → load_skill() 解析每个 SKILL.md
      → YAML frontmatter → name, description
      → 正文内容 → _process_skill_paths() 相对路径→绝对路径
        → Skill 对象存入 loaded_skills dict
    → get_skills_metadata_prompt() 生成元数据摘要
      → 注入 system prompt 的 {SKILLS_METADATA} 占位符

Level 2 (运行时):
  LLM 决定调用 get_skill tool
    → GetSkillTool.execute(skill_name)
      → skill_loader.get_skill(name) → Skill 对象
        → skill.to_prompt() → 完整 skill 内容作为 ToolResult 返回给 LLM
```

**涉及文件**: `tools/skill_loader.py`, `tools/skill_tool.py`, `cli.py`（prompt 注入部分）

---

### 链条 11：System Prompt 组装链

```
system_prompt.md 文件 (通过 find_config_file 优先级查找)
  → 读取为字符串
    → 替换 {SKILLS_METADATA} 占位符 (注入 skill 元数据)
      → Agent.__init__ 追加 workspace 信息
        → 存为 agent.system_prompt
          → 作为 messages[0] (role="system")
```

**涉及文件**: `cli.py`（`run_agent` 步骤 5-6）, `agent.py`（`__init__`）

---

### 链条 12：消息历史与 Token 摘要链

```
agent.messages 不断增长
  → 每步循环开头: _summarize_messages() 检查
    → _estimate_tokens() (tiktoken 计算) 或 api_total_tokens (API 返回)
      → 超限? → 找所有 user 消息位置 → 每轮执行消息提取
        → _create_summary() 调 LLM 生成摘要
          → 替换原消息列表: system + user1 + summary1 + user2 + summary2 + ...
```

**涉及文件**: `agent.py`（`_summarize_messages`, `_create_summary`, `_estimate_tokens`）

---

### 链条 13：Token Usage 追踪链

```
API 响应 → response.usage (input_tokens, output_tokens, cache 等)
  → _parse_response() 组装 TokenUsage 对象
    → LLMResponse.usage 返回
      → agent.run() 中: agent.api_total_tokens = response.usage.total_tokens
        → 作为 _summarize_messages() 的触发条件之一
        → 作为 print_stats() 的统计数据
```

**涉及文件**: `llm/anthropic_client.py`, `llm/openai_client.py`, `agent.py`

---

### 链条 14：重试机制链

```
LLM API 调用失败 → 抛出异常
  → async_retry 装饰器拦截
    → 检查是否是 retryable_exception
      → 是 → calculate_delay() 指数退避 → asyncio.sleep → 重试
      → 达到 max_retries → 抛出 RetryExhaustedError
  → on_retry 回调 → cli 中打印重试信息
```

**涉及文件**: `retry.py`, `llm/anthropic_client.py`（`generate`）, `llm/openai_client.py`（`generate`）, `cli.py`（`on_retry` 回调）

---

### 链条 15：取消机制链

```
用户按 Esc → esc_key_listener 线程检测
  → cancel_event.set() (asyncio.Event)
    → agent.run() 循环中多个检查点: _check_cancelled()
      → 为 True → _cleanup_incomplete_messages() 清理未完成消息
        → 返回 "Task cancelled by user."
  → 主循环 finally: 清理 cancel_event + 停止 esc 线程
```

**涉及文件**: `cli.py`（Esc 监听 + agent_task 部分）, `agent.py`（`_check_cancelled`, `_cleanup_incomplete_messages`）

---

### 链条 16：日志记录链

```
AgentLogger.start_new_run() → 创建 .log 文件
  → 每次 LLM 调用:
    ├─ log_request(messages, tools) → 记录请求
    ├─ log_response(content, thinking, tool_calls) → 记录响应
    └─ log_tool_result(tool_name, args, result) → 记录工具执行
  → 文件写入 ~/.mini-agent/log/agent_run_TIMESTAMP.log
```

**涉及文件**: `logger.py`, `agent.py`（调用处）

---

### 链条 17：Session Notes 持久化链

```
写入: LLM 调用 record_note(content, category)
  → SessionNoteTool.execute() → JSON 追加到 .agent_memory.json

读取: LLM 调用 recall_notes(category?)
  → RecallNoteTool.execute() → 从 .agent_memory.json 读取 → 格式化返回
```

**涉及文件**: `tools/note_tool.py`

---

### 链条 18：后台进程管理链

```
BashTool.execute(run_in_background=True)
  → asyncio.create_subprocess_shell → BackgroundShell 对象
    → BackgroundShellManager.add() 注册 + start_monitor() 持续读输出
      → BashOutputTool: get_new_output() 获取增量输出
      → BashKillTool: terminate() + 清理 monitor task + 从 manager 移除
```

**涉及文件**: `tools/bash_tool.py`（`BackgroundShell`, `BackgroundShellManager`, 三个 Tool 类）

---

### 链条 19：File Tools 路径解析链

```
用户给的 path 参数 (可能是相对路径)
  → Path(path)
    → 不是绝对路径? → workspace_dir / file_path 拼接
      → ReadTool: 读文件 + 行号格式化 + token 截断
      → WriteTool: 创建父目录 + 写入
      → EditTool: 读取 → old_str 查找替换 → 写回
```

**涉及文件**: `tools/file_tools.py`

---

### 链条 20：ACP 协议桥接链

```
外部 ACP 客户端 → stdio → AgentSideConnection
  → MiniMaxACPAgent 处理:
    ├─ initialize() → 返回 agent 能力声明
    ├─ newSession() → 创建 Agent 实例 + workspace 工具
    ├─ prompt() → 用户消息 → _run_turn() 执行 agent 循环
    │   → LLM 调用 → 通过 session_notification 推送:
    │     ├─ update_agent_thought (thinking)
    │     ├─ update_agent_message (content)
    │     ├─ start_tool_call + update_tool_call (工具执行)
    │   → 返回 PromptResponse(stopReason)
    └─ cancel() → state.cancelled = True
```

**涉及文件**: `acp/__init__.py`

---

### 链条 21：配置文件优先级查找链

```
Config.find_config_file(filename):
  1. mini_agent/config/{filename}   ← 开发模式
  2. ~/.mini-agent/config/{filename} ← 用户目录
  3. {package}/config/{filename}     ← 安装包目录
  → 返回第一个存在的路径

同样逻辑用于: config.yaml, system_prompt.md, mcp.json
```

**涉及文件**: `config.py`（`find_config_file`, `get_default_config_path`）

---

## 建议追踪顺序

如果你想从最核心到最外围理解，建议按这个顺序：

1. **链条 2** (主循环) — 这是骨架
2. **链条 3** (LLM 转换) — 理解数据怎么跟 API 对话
3. **链条 6** (Tool 执行) — 理解 Agent 怎么"动手"
4. **链条 7** (工具收集) — 工具从哪来
5. **链条 1** (配置) — 一切的起点
6. **链条 12** (Token 摘要) — 长对话怎么不爆
7. 其余链条按兴趣选看

每条链条你追的时候，建议在纸上画出 `数据A → 经过函数X → 变成数据B` 的箭头图，画不出来的地方就是还没完全理解的地方。