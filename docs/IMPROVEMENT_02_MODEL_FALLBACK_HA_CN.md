# 改进设计 02：模型回退与高可用

> 作者：Codex（GPT-5）
> 日期：2026-03-31
> 适用仓库：`Mini-Agent`
> 本文聚焦范围：模型池、故障切换、健康检测、熔断、降级与可用性治理

---

## 1. 这份文档要解决什么问题

当前 `Mini-Agent` 的模型调用链路已经具备“基础重试”能力，但还不具备真正的高可用能力。

从代码上看，当前的调用路径大致是：

- `cli.py` 读取单组 `llm` 配置
- 创建一个 [`LLMClient`](../mini_agent/llm/llm_wrapper.py)
- `LLMClient` 内部只实例化一个具体 provider client
- provider client 在本节点内做指数退避重试
- 如果重试耗尽，整个 Agent 直接报错退出

相关核心位置包括：

- [`mini_agent/cli.py`](../mini_agent/cli.py)
- [`mini_agent/config.py`](../mini_agent/config.py)
- [`mini_agent/llm/llm_wrapper.py`](../mini_agent/llm/llm_wrapper.py)
- [`mini_agent/llm/base.py`](../mini_agent/llm/base.py)
- [`mini_agent/llm/openai_client.py`](../mini_agent/llm/openai_client.py)
- [`mini_agent/llm/anthropic_client.py`](../mini_agent/llm/anthropic_client.py)
- [`mini_agent/retry.py`](../mini_agent/retry.py)

这说明当前系统是：

- **单模型节点**
- **单 provider 实例**
- **节点内重试**
- **无多节点回退**
- **无健康状态记忆**
- **无熔断**
- **无降级策略**

这对 Demo 来说已经合理，但如果目标是把 `Mini-Agent` 升级为更像“可持续使用的 Agent Runtime”，这一层必须补齐。

本文的目标不是简单写一个“失败后换模型”的 if/else，而是设计一套**模型访问层的可靠性架构**，使系统具备：

- 更高的请求成功率
- 更好的故障恢复能力
- 更清晰的错误边界
- 更合理的资源与成本控制
- 后续接入模型池、多账户、跨 provider 路由的演进空间

---

## 2. 当前实现评估

### 2.1 当前实现有什么

当前实现已经有三块基础能力：

1. **统一 LLM 包装层**
   - `LLMClient` 把 provider 差异收敛成统一接口

2. **Provider-specific client**
   - `OpenAIClient`
   - `AnthropicClient`

3. **指数退避重试**
   - `RetryConfig`
   - `async_retry()`
   - `RetryExhaustedError`

这意味着当前系统已经有一个不错的起点：

- provider 抽象已经存在
- retry 机制已经存在
- CLI 配置入口已经存在

所以这项改进不是从零开始，而是在现有抽象上继续往“可用性治理”方向推进。

### 2.2 当前实现的优点

现有设计有几个优点，不应该被否定：

- `LLMClient` 已经把协议差异包起来了，后续扩展高可用层有落点。
- provider client 已经支持工具调用、thinking、usage 解析，说明模型访问不是“裸 HTTP 调一下”。
- retry 逻辑是解耦的，不直接污染业务层。
- CLI 的配置入口已经能承载进一步扩展。

### 2.3 当前实现的主要问题

#### 问题 1：只有“重试”，没有“回退”

当前如果同一个模型节点连续失败，只会在同一个节点上做重试。  
一旦重试耗尽，请求就失败，不会自动切换到备用模型、备用账号或备用 provider。

这意味着当前系统只能处理：

- 短暂网络抖动
- 短暂接口失败

但无法处理：

- 单账号限流
- 单 provider 持续性异常
- 区域性 API 故障
- 模型级别不可用

#### 问题 2：没有“节点健康状态”

当前系统对模型节点没有记忆。

也就是说：

- 刚失败过的节点，下一次请求还会被当成正常节点使用
- 没有失败计数
- 没有冷却时间
- 没有“最近 5 分钟明显不稳定”的判断

这会导致系统在故障期间不断命中坏节点。

#### 问题 3：没有错误分类

当前 retry 配置默认把 `(Exception,)` 当成可重试异常，这对 Demo 简单直接，但不适合高可用系统。

因为不同错误的处理方式应该不同：

- 网络超时：可重试、可切节点
- 429 限流：可重试、可切同 provider 其他账号
- 5xx 服务错误：可重试、可切节点
- 401/403 鉴权错误：通常不应重复重试
- 参数错误 / schema 错误：不应切换节点掩盖问题
- 上下文过长：应先压缩上下文，再决定是否换模型

没有错误分类，就很难做正确的路由决策。

#### 问题 4：配置模型是“单实例”，不是“池”

当前 [`mini_agent/config.py`](../mini_agent/config.py) 里的 `LLMConfig` 是单个：

- `api_key`
- `api_base`
- `model`
- `provider`

这意味着配置模型的基本单位还是“一个活跃节点”，而不是“一个可调度池”。

#### 问题 5：跨 provider 回退的复杂度被低估

这点非常关键。

如果你只是在 OpenAI-compatible 模型之间切换，很多事情相对简单。  
但如果要在 `anthropic` 和 `openai` 两个协议族之间做真实回退，会碰到以下问题：

- 工具 schema 格式不同
- message 格式不同
- thinking / reasoning 的保留方式不同
- usage 统计字段不同
- max_tokens / stop reason / content block 结构不同

虽然当前 `LLMClient` 已经统一了很多内容，但“切换协议族时的兼容性治理”仍然是高可用设计里最容易被低估的难点之一。

---

## 3. 改进目标

建议把目标明确拆成四个层次。

### 3.1 目标一：从“单节点重试”升级为“节点池调度”

系统需要能管理多个候选模型节点，而不是只管理一个活跃 client。

这里的“节点”建议理解为：

- `provider + api_base + api_key + model` 的组合

而不是单纯“模型名字”。

### 3.2 目标二：引入可解释的高可用策略

至少应包括：

- 被动健康检测
- 故障节点切换
- 熔断
- 冷却恢复
- 降级路线

### 3.3 目标三：建立错误分类与路由决策

不是所有错误都应该：

- 重试
- 切模型
- 切 provider

系统必须知道不同错误应该怎么处理。

### 3.4 目标四：兼顾能力、成本与稳定性

高可用不只是“尽量成功”，还要考虑：

- 备用模型是否支持工具调用
- 是否支持足够上下文窗口
- 是否保留 thinking 能力
- 是否成本激增
- 是否会因为降级导致输出质量明显变差

也就是说，路由标准不能只看“活着没活着”，还要看“适不适合当前任务”。

---

## 4. 总体设计建议

### 4.1 设计原则

建议遵守以下原则：

1. **优先支持“节点池”，再逐步支持“跨协议族池”**
   第一阶段先把单协议族内回退做稳，再扩展到跨 provider。

2. **优先做被动健康检测**
   先根据真实请求结果维护健康状态，暂时不必上后台主动探测。

3. **把重试和切换分层**
   节点内 retry 与节点间 failover 是两层逻辑，不应混成一层。

4. **把错误分类作为一等能力**
   没有错误分类，就无法做正确的高可用。

5. **日志必须可解释**
   每次回退都要能回答：
   - 为什么切换
   - 切到了谁
   - 原节点状态如何

### 4.2 推荐的新模块边界

建议新增目录：

```text
mini_agent/llm/ha/
  __init__.py
  pool.py
  router.py
  health.py
  breaker.py
  errors.py
  models.py
```

各模块职责建议如下：

- `pool.py`
  - 管理模型节点池
  - 节点注册、筛选、排序

- `router.py`
  - 为每次请求选择节点
  - 实现 failover、degrade、fallback 策略

- `health.py`
  - 维护节点健康分数、失败计数、最近状态

- `breaker.py`
  - 熔断器状态机
  - `closed / open / half-open`

- `errors.py`
  - 错误分类
  - 定义可重试、可切换、不可恢复等错误类型

- `models.py`
  - 定义节点元数据、能力描述、路由结果、健康快照等结构

### 4.3 推荐的数据模型

建议至少定义以下核心结构：

```python
ModelNode:
  node_id: str
  provider: str
  api_base: str
  api_key: str
  model: str
  protocol_family: str
  priority: int
  weight: int
  context_window: int
  supports_tools: bool
  supports_thinking: bool
  enabled: bool

NodeHealth:
  node_id: str
  consecutive_failures: int
  consecutive_successes: int
  last_failure_at: datetime | None
  last_success_at: datetime | None
  circuit_state: str
  cooldown_until: datetime | None
  health_score: float

RoutingRequest:
  required_tools: bool
  required_context_window: int
  prefers_thinking: bool
  task_tier: str

RoutingDecision:
  selected_node_id: str
  candidate_node_ids: list[str]
  reason: str
  fallback_level: int
```

这里的关键设计点有两个：

- “模型节点”不是只看模型名，而是带完整接入信息和能力描述
- 路由不是只看可用性，还看任务需求与节点能力是否匹配

---

## 5. 推荐的高可用策略

### 5.1 三层调用策略

建议把模型调用分成三层：

#### 第一层：节点内重试

仍保留当前 `RetryConfig` 的意义，但仅用于**单节点内的瞬时错误恢复**。

适合处理：

- 临时超时
- 短暂 5xx
- 短暂网络闪断

不适合无限放大：

- 限流
- 鉴权错误
- 持续性 provider 故障

#### 第二层：节点间切换

当节点内重试失败，或错误类型表明当前节点不适合继续尝试时，切换到备用节点。

示例：

- 主节点 `MiniMax-M2.5` 失败
- 切到同协议族的备用 OpenAI-compatible 节点
- 如果同协议族都失败，再考虑跨协议族降级

#### 第三层：策略性降级

当高能力节点都不可用时，允许降级到更便宜或能力稍弱的模型，但前提是满足最低任务要求。

例如：

- 必须支持 tool calling
- 必须具备至少 `X` 的上下文窗口

如果这些最低条件都不满足，就应直接失败，而不是“假装降级成功”。

### 5.2 推荐的熔断策略

建议采用标准的三态熔断器：

- `closed`
  - 正常状态
  - 节点可用

- `open`
  - 节点最近连续失败
  - 在冷却时间内不再参与路由

- `half-open`
  - 冷却期结束后，允许少量试探请求
  - 若成功则恢复 `closed`
  - 若失败则回到 `open`

推荐触发条件示例：

- 连续失败 `3` 次进入 `open`
- 冷却 `30-120s`
- 半开状态下允许 `1` 次探测请求

### 5.3 推荐的健康评分

第一阶段建议做简单版，不要一开始上复杂评分模型。

可用一个线性或分段评分：

- 初始分数 `100`
- 成功恢复 +小幅加分
- 短期失败 -中幅扣分
- 连续失败 -大幅扣分
- 超时 / 429 / 5xx 的扣分权重不同

核心目标不是绝对精确，而是让“最近明显不稳定的节点”排序自然后移。

---

## 6. 错误分类建议

### 6.1 难点判断

技术难度：**高**

真正难的是“切换条件设计”，而不是“多写几个 except”。

### 6.2 推荐分类

建议最少分成以下几类：

#### A. 瞬时可重试错误

例如：

- 网络超时
- 临时连接失败
- 部分 5xx

处理建议：

- 节点内 retry
- retry 耗尽后可切换节点

#### B. 限流与容量错误

例如：

- 429
- provider 明确返回 capacity exceeded

处理建议：

- 当前节点短时间降权
- 优先切同 provider 其他账号或同协议族节点

#### C. 节点配置错误

例如：

- 401/403
- API key 失效
- base_url 错误

处理建议：

- 不应在当前节点内反复重试
- 节点直接标记为不可用或强降权
- 路由到其他节点
- 输出明确配置错误日志

#### D. 请求构造错误

例如：

- 工具 schema 不兼容
- 参数格式错误
- 协议转换 bug

处理建议：

- 不应简单切模型掩盖问题
- 应优先作为程序错误暴露

#### E. 任务约束错误

例如：

- 上下文超长
- 模型不支持所需工具能力
- 输出 token 上限不够

处理建议：

- 先尝试压缩上下文或调整预算
- 再根据能力约束选更合适的节点

### 6.3 与上下文管理的耦合

这点必须明确：

“上下文过长”不是单纯的模型故障。  
它本质上是**上下文治理和模型路由的交叉问题**。

因此高可用层需要和“高级上下文管理”联动：

- 如果主节点窗口不够，先尝试压缩
- 如果压缩后仍不够，再路由到更大窗口模型

---

## 7. 跨 provider 回退的现实复杂度

这部分必须写透，因为它决定你第一阶段该做多大。

### 7.1 为什么跨 provider 回退更难

当前仓库支持两大协议族：

- OpenAI-compatible
- Anthropic-compatible

虽然 `LLMClient` 统一了对外接口，但内部仍然存在显著差异：

- tools schema 转换不同
- message 序列化方式不同
- assistant thinking 的保留格式不同
- tool result 回填格式不同
- token usage 统计方式不同

这意味着“切换 provider”不是简单换一个 URL。

### 7.2 推荐策略

建议按阶段推进：

#### 第一阶段

只做**同协议族回退**：

- OpenAI-compatible 节点池
- Anthropic-compatible 节点池

好处：

- 请求和消息格式兼容性问题最少
- 工具调用的一致性更容易保证

#### 第二阶段

再做**跨协议族降级**，但要加明确能力门槛：

- 是否支持工具调用
- 是否支持当前 thinking/消息格式要求
- 是否支持足够上下文窗口

如果这些条件不满足，就不允许跨族切换。

### 7.3 推荐的术语

建议在实现里明确区分：

- `provider`
- `protocol_family`

因为未来可能出现：

- 不同厂商但同 OpenAI-compatible 协议
- 不同 Anthropic-compatible 代理节点

这两个概念不应该混用。

---

## 8. 推荐的配置设计

### 8.1 当前配置的问题

当前配置是：

```yaml
api_key: ...
api_base: ...
model: ...
provider: ...
retry: ...
```

这是单节点配置，不适合模型池。

### 8.2 推荐的新配置结构

建议把 `llm` 改成：

```yaml
llm:
  routing:
    strategy: "priority"
    cross_family_fallback: false
    reserved_large_context_margin: 8000

  retry:
    enabled: true
    max_retries: 2
    initial_delay: 1.0
    max_delay: 10.0
    exponential_base: 2.0

  breaker:
    failure_threshold: 3
    cooldown_seconds: 60
    half_open_max_requests: 1

  pool:
    - node_id: "minimax-primary"
      provider: "anthropic"
      protocol_family: "anthropic"
      api_key_env: "MINIMAX_API_KEY"
      api_base: "https://api.minimax.io"
      model: "MiniMax-M2.5"
      priority: 100
      weight: 10
      context_window: 128000
      supports_tools: true
      supports_thinking: true
      enabled: true

    - node_id: "openai-backup"
      provider: "openai"
      protocol_family: "openai"
      api_key_env: "OPENAI_API_KEY"
      api_base: "https://api.openai.com/v1"
      model: "gpt-5"
      priority: 80
      weight: 5
      context_window: 128000
      supports_tools: true
      supports_thinking: true
      enabled: true
```

这里的重点是：

- 节点池配置要显式化
- 支持从环境变量读取不同账号
- 节点能力要显式声明

### 8.3 路由策略建议

第一阶段推荐只支持两种即可：

- `priority`
  - 高优先级节点优先，失败才切换

- `weighted`
  - 在健康节点中做加权分流

对当前仓库而言，我更推荐先做 `priority`，因为更可控、更容易调试。

---

## 9. 推荐的实现方案

### 9.1 推荐分三期做

#### Phase 1：单协议族模型池 + 被动故障切换

目标：

- 引入 `ModelNode`
- 支持多个候选节点
- 保留当前节点内 retry
- retry 耗尽后切到下一个健康节点
- 被动维护失败计数

这期完成后，系统已经从“单节点”升级为“基础可回退”。

#### Phase 2：熔断 + 健康评分 + 降级策略

目标：

- 引入熔断器
- 记录节点健康状态
- 支持 cooldown / half-open
- 根据任务需求做节点筛选

这期完成后，系统才真正具备“高可用策略”。

#### Phase 3：跨协议族回退 + 能力感知路由

目标：

- 支持跨 OpenAI / Anthropic 协议族切换
- 能根据工具支持、thinking、上下文窗口做能力筛选
- 加入更完整的日志和指标

这期完成后，系统才开始接近“可扩展模型网关”的形态。

### 9.2 推荐修改的文件范围

建议的变更集合大致如下：

- 新增：
  - `mini_agent/llm/ha/pool.py`
  - `mini_agent/llm/ha/router.py`
  - `mini_agent/llm/ha/health.py`
  - `mini_agent/llm/ha/breaker.py`
  - `mini_agent/llm/ha/errors.py`
  - `mini_agent/llm/ha/models.py`

- 修改：
  - `mini_agent/config.py`
  - `mini_agent/config/config.yaml`
  - `mini_agent/llm/llm_wrapper.py`
  - `mini_agent/cli.py`
  - `mini_agent/retry.py`

- 新增测试：
  - `tests/test_llm_pool.py`
  - `tests/test_llm_router.py`
  - `tests/test_llm_breaker.py`
  - `tests/test_llm_error_classification.py`

---

## 10. 技术难度评估

### 10.1 综合难度

综合难度：**高**

原因是这项改进不是单纯补功能，而是在重构模型访问层的控制逻辑。

### 10.2 难点拆分

- 单节点重试保留：低
- 节点池抽象：中等
- 被动故障切换：中等
- 熔断器状态机：中等偏上
- 错误分类：高
- 跨协议族回退：高
- 能力感知路由：高

### 10.3 为什么难

核心难点在于“正确降级”：

- 切换太积极，成本会飙升
- 切换太保守，可用性会下降
- 错误分类不准，会导致错误节点反复被选中
- 降级模型能力不够，会让任务 silently degrade

这项工作真正考验的是系统设计，而不是 SDK 调用能力。

---

## 11. 风险与注意事项

### 风险 1：一开始就做“跨所有 provider 的智能路由”

这非常容易失控。

建议先做：

- 单协议族池
- priority failover
- 被动健康检测

先把这三件事做稳。

### 风险 2：把“错误都当成可切换”

如果 schema bug、程序错误、工具格式错误也触发切模型，会掩盖真实问题。

高可用系统不应该把程序 bug 包装成 provider 故障。

### 风险 3：忽略成本与速率限制

有些备用模型：

- 更贵
- 更慢
- 速率限制更紧

所以路由规则不应只有“可用优先”，还要预留成本和时延策略位。

### 风险 4：与上下文窗口不联动

不同模型窗口大小不同。  
如果不把上下文预算与模型选择联动起来，就会出现：

- 小窗口模型不断因为 context overflow 失败
- 系统却还在重复重试或错误切换

所以这项改进和“高级上下文管理”必须联动设计。

---

## 12. 验证与测试建议

### 12.1 必测场景

至少要覆盖以下场景：

1. 主节点瞬时超时，节点内 retry 成功
2. 主节点持续失败，自动切到备用节点
3. 主节点 429，短时降权并切到备用节点
4. 主节点 401/403，不在本节点内重复重试
5. 连续失败触发熔断
6. cooldown 后半开探测成功，节点恢复
7. 不支持工具调用的节点不会被选为 tool task 目标
8. 小窗口节点在长上下文任务中会被过滤或降级
9. 所有候选节点都不满足最低能力约束时，系统明确失败

### 12.2 推荐新增日志

建议至少输出：

- 本次请求的候选节点列表
- 最终选中的节点
- 节点失败原因分类
- 是否发生 retry
- 是否发生 failover
- 熔断状态变化
- fallback 层级
- 最终成功节点

这些日志对后续调优和排障非常关键。

---

## 13. 与另外两项改进的关系

### 对高级上下文管理的依赖

两者是强耦合的：

- 模型切换意味着上下文窗口可能变化
- 上下文预算应影响模型路由
- context overflow 应先走压缩，再决定是否切大窗口节点

因此：

- 高可用层不能独立假设“消息一定能塞进去”
- 上下文层不能独立假设“模型窗口永远固定”

### 对 planner/todo/progress 的影响

planner 会让某些任务更容易分层降级：

- 简单步骤可以允许较便宜模型执行
- 关键总结或复杂分析可以优先使用高能力节点

也就是说，后续 planner/todo/progress 可以反过来增强模型路由策略。

---

## 14. 最终建议

如果只给一个判断：

**这项改进非常值得做，但第一阶段一定要收敛，不要试图一步做成“全能模型网关”。**

我建议的最优起步路线是：

1. 先把当前单节点配置升级为节点池配置
2. 保留当前 retry 机制
3. 在 retry 耗尽后做 priority failover
4. 再补被动健康状态和熔断
5. 最后再考虑跨协议族回退

这条路径既能明显提升可用性，又不会过早把系统复杂度抬得过高。

---

## 15. 一句话总结

这项改进的本质，不是“加一个备用模型”，而是：

**把 `Mini-Agent` 从“会重试的单模型 Demo”升级为“具备模型访问治理能力的 Agent Runtime”。**
