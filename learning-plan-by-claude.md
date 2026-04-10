# Mini-Agent 学习计划

> 作者：Claude Sonnet 4.6
> 日期：2026-03-31
> 适用对象：具备 Java RAG 项目（Paismart）+ Go 微服务项目（GuGoTik）背景的开发者

---

## 你的项目背景分析

### Paismart（Java RAG 知识库系统）
- **技术栈**：Spring Boot 3.4.2、MySQL、Redis、Elasticsearch、Kafka、MinIO、DeepSeek API、Ollama
- **核心能力**：RAG 全链路（文档分块 → 向量化 → 语义检索 → LLM 生成答案）、WebSocket 实时交互、多租户权限设计

### GuGoTik（Go 微服务平台）
- **技术栈**：Go、PostgreSQL、Redis Cluster、Docker/K8s、Consul（服务发现）、OpenTelemetry（可观测性）、FFmpeg
- **核心能力**：微服务架构设计、服务注册发现、容器化部署、链路追踪

### 你的优势与迁移点

| 你已有的能力 | 在 Mini-Agent 中的对应 |
|---|---|
| RAG 管道（Paismart）| Agent 的工具调用 + LLM 推理循环 |
| LLM API 集成（DeepSeek/Ollama）| LLM Wrapper 层（`mini_agent/llm/`）|
| 微服务模块化设计（GuGoTik）| Agent 工具系统模块化（`mini_agent/tools/`）|
| 异步/并发处理（Go goroutine）| Python asyncio 异步编程 |
| JSON Schema / API 设计 | 工具 Schema 定义、Function Calling |
| 服务间通信（RPC/HTTP）| MCP 协议（Model Context Protocol）|

---

## 学习路线图（4 周计划）

### 第一周：Python 快速上手（面向 Java/Go 开发者）

> 目标：用你已有的编程经验快速补齐 Python 语法差异

#### 1.1 Python vs Java/Go 关键差异速查

```
类型系统：Python 动态类型 → 类比 Go interface{} / Java Object
包管理：uv/pip → 类比 Maven/Go Modules
类定义：class Foo → 类比 Java class，但无需显式接口
装饰器：@decorator → 类比 Java 注解 @Annotation
上下文管理器：with ... → 类比 Go defer
```

#### 1.2 重点学习内容

- **类型注解**：`def func(x: int) -> str`（项目大量使用）
- **dataclass / Pydantic**：类比 Java 的 Lombok + 参数校验（Mini-Agent 用 Pydantic 定义工具 Schema）
- **异步编程（asyncio）**：
  - `async def` / `await` → 类比 Go 的 goroutine + channel
  - `asyncio.gather()` → 类比 Go 的 `sync.WaitGroup`
- **生成器与迭代器**：`yield` 关键字（用于流式输出 streaming）

#### 1.3 推荐资源

- [Python 官方教程](https://docs.python.org/zh-cn/3/tutorial/)（重点看第 4-9 章）
- [asyncio 文档](https://docs.python.org/zh-cn/3/library/asyncio.html)
- 《Python Cookbook》第 7 章（函数）+ 第 12 章（并发）

#### 1.4 实践任务

```python
# 任务 1：用 Pydantic 定义一个工具 Schema
from pydantic import BaseModel
class SearchTool(BaseModel):
    query: str
    max_results: int = 10

# 任务 2：用 asyncio 模拟并发工具调用
import asyncio
async def call_tool(name: str):
    await asyncio.sleep(1)
    return f"{name} result"

async def main():
    results = await asyncio.gather(
        call_tool("search"),
        call_tool("read_file"),
    )
    print(results)
```

---

### 第二周：LLM Agent 核心概念

> 目标：理解 Agent 的运行原理，映射你的 RAG 经验

#### 2.1 从 RAG 到 Agent：概念升级

你在 Paismart 中实现了 RAG：
```
用户问题 → Embedding → 向量检索 → 召回文档 → LLM 生成答案
```

Agent 是 RAG 的超集：
```
用户指令 → LLM 思考 → 选择工具 → 执行工具 → 观察结果 → 再次思考 → ... → 最终答案
```

核心差异：
- RAG 是**一次性检索**；Agent 是**多轮推理循环（ReAct Loop）**
- RAG 工具固定（向量检索）；Agent 工具动态（文件、代码、网络等）
- Agent 有**状态记忆**（对话历史管理）

#### 2.2 Function Calling 机制（关键！）

这是 Agent 的核心机制，你在 Paismart 调用 DeepSeek API 时可能接触过：

```json
// 向 LLM 描述工具
{
  "name": "read_file",
  "description": "读取文件内容",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "文件路径"}
    },
    "required": ["path"]
  }
}
```

LLM 返回时会说"我要调用 read_file"，Agent 负责真正执行并把结果返回给 LLM。

#### 2.3 Token 管理（与你的 RAG 经验对应）

你在 Paismart 做文档分块是为了控制 Embedding 长度；
Mini-Agent 的 Token 管理（`mini_agent/tokens/`）是为了控制对话历史长度：

```
对话历史增长 → Token 超限 → 需要截断/压缩 → 类比 RAG 的 chunk 策略
```

#### 2.4 阅读任务

精读以下论文（摘要+实验部分即可）：
- **ReAct**：[ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)
- **Tool Use**：理解 OpenAI Function Calling 文档

---

### 第三周：Mini-Agent 源码精读

> 目标：读懂项目核心代码，建立完整的心智模型

#### 3.1 阅读顺序（按依赖关系从底到顶）

```
第 1 天：Schema 定义层
└── mini_agent/schema/ 或 mini_agent/models/
    理解工具参数、消息格式的数据结构定义

第 2 天：LLM 客户端层
└── mini_agent/llm/llm_wrapper.py
    理解如何封装 OpenAI/Claude/本地模型 API
    对比你在 Paismart 中调用 DeepSeek API 的方式

第 3 天：工具系统层
└── mini_agent/tools/
    重点读 file_tools.py、bash_tool.py
    理解工具注册机制（类比 GuGoTik 的服务注册）

第 4 天：Agent 主循环
└── mini_agent/agent/ 或核心 agent.py
    理解 ReAct 循环：think → act → observe → repeat

第 5 天：CLI 入口
└── mini_agent/__main__.py 或 cli.py
    理解命令行参数解析、交互模式 vs 非交互模式

第 6-7 天：MCP 集成
└── mini_agent/mcp/
    理解 MCP 协议（类比微服务的 RPC 通信）
```

#### 3.2 核心代码对照表

| Mini-Agent 模块 | 你的类比经验 | 关键问题 |
|---|---|---|
| `llm_wrapper.py` | Paismart 的 DeepSeek 调用 | 如何统一不同 LLM 的接口？ |
| `tools/` | GuGoTik 的 service 层 | 工具如何注册和被发现？ |
| Agent 主循环 | Paismart 的 RAG pipeline | 循环什么时候终止？ |
| Token 管理 | Paismart 的文档分块 | 对话历史如何裁剪？ |
| MCP 客户端 | GuGoTik 的 RPC/Consul | 协议格式是什么？ |

#### 3.3 调试实践

```bash
# 克隆后安装依赖
cd /Users/repeater/Documents/Code/work/Mini-Agent
pip install -e .  # 或使用 uv

# 运行一个简单的 Agent 任务，加 -v 开启详细日志
mini-agent "列出当前目录下的文件"

# 在关键位置加断点，观察消息流
# 推荐用 VS Code + Python Debugger
```

---

### 第四周：扩展与实战

> 目标：基于 Mini-Agent 做改造，深化理解

#### 4.1 实战项目建议（结合你的背景）

**项目 A：给 Mini-Agent 添加 RAG 工具**
- 利用 Paismart 的向量检索能力，为 Mini-Agent 编写一个 `rag_search` 工具
- 让 Agent 可以调用你的 Elasticsearch 知识库

**项目 B：Mini-Agent 的可观测性增强**
- 利用 GuGoTik 的 OpenTelemetry 经验，为 Agent 每次工具调用添加链路追踪
- 记录：调用了哪个工具、耗时多少、Token 消耗

**项目 C：Go 版 Mini-Agent（高难度）**
- 基于第 S250 条记忆中的可行性分析，尝试用 Go 重写核心 Agent 循环
- 重点：goroutine 实现并发工具调用 vs Python asyncio

#### 4.2 技术深度扩展

- **MCP 协议深入**：阅读 [MCP 官方文档](https://modelcontextprotocol.io/)，理解与微服务 RPC 的异同
- **Streaming 输出**：理解 SSE（Server-Sent Events）在 Agent 中的应用（类比 WebSocket in Paismart）
- **多 Agent 协作**：研究 Agent 作为工具被另一个 Agent 调用的模式

---

## 前置知识清单

### 必须掌握（学习前）

- [ ] Python 基础语法（变量、函数、类、模块）
- [ ] Python 类型注解（`typing` 模块）
- [ ] asyncio 基础（`async/await`、`gather`、`create_task`）
- [ ] Pydantic v2 基础（`BaseModel`、字段定义、校验）
- [ ] HTTP API 调用（`httpx` 或 `requests`）
- [ ] JSON 序列化/反序列化

### 建议了解（学习中）

- [ ] OpenAI API / Anthropic API 的 Function Calling 格式
- [ ] ReAct 论文核心思想
- [ ] MCP 协议基础（JSON-RPC over stdio/HTTP）
- [ ] Python 进程管理（`subprocess` 模块，类比 Go 的 `os/exec`）
- [ ] Python 包管理（`uv`、`pyproject.toml`，类比 Go Modules）

### 加分项（深入后）

- [ ] LLM 的 Temperature、Top-P 等参数含义
- [ ] Token 计算方式（tiktoken）
- [ ] 向量数据库基础（如果做 RAG 工具集成）
- [ ] Docker 化 Python 应用（已有 Go/Java 经验，快速迁移）

---

## 学习节奏建议

| 周次 | 重点 | 每日投入 | 产出 |
|---|---|---|---|
| 第 1 周 | Python 语法补全 | 1-2 小时 | 能读懂项目代码 |
| 第 2 周 | Agent 概念建模 | 1-2 小时 | 能描述 Agent 工作原理 |
| 第 3 周 | 源码精读 | 2-3 小时 | 能独立修改/扩展工具 |
| 第 4 周 | 实战改造 | 2-3 小时 | 有一个自己的贡献/改造 |

---

## 你的核心优势

1. **RAG 经验（Paismart）**：你已经理解 LLM + 检索的结合，Agent 只是把检索泛化为「任意工具调用」
2. **微服务模块化（GuGoTik）**：Mini-Agent 的工具注册机制与微服务的服务注册高度类似，概念迁移成本低
3. **Go 并发经验**：Python asyncio 对你来说不会陌生，goroutine ≈ coroutine
4. **分布式系统视角**：理解 MCP 协议会比没有微服务经验的人快很多

> 核心建议：**不要从头学 Python 再学 Agent**。带着你的 Java/Go 经验直接读源码，遇到不懂的语法再查，这样效率最高。

---

*由 Claude Sonnet 4.6 生成 · 2026-03-31*
