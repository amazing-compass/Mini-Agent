# Coding Agent 项目详细对照

## 文档目的

这份文档用来系统对照四个对象：

1. `mini-swe-agent`
2. 你本地当前这个 `Mini-Agent` 仓库
3. `learn-claude-code`
4. `Claude Code`

目标不是简单排出“谁更强”，而是回答下面几个更有价值的问题：

- 它们各自到底想解决什么问题
- 它们分别站在 coding agent 的哪一个层级上
- 哪些是“最小闭环”，哪些是“实用 harness”，哪些是“教学拆解”，哪些是“产品”
- 如果你的主目标是学懂本地这个仓库，外部项目应该怎么辅助阅读

---

## 事实边界

这份文档里的判断分两类：

- `mini-swe-agent`、本地 `Mini-Agent`、`learn-claude-code`：基于 README 和关键源码直接阅读后的结论
- `Claude Code`：由于不是完全开源代码仓库，这里的比较只能基于公开行为、公开讨论、以及相关教学仓库的对照来做高层判断，其中涉及推断的地方会明确说明

文档撰写时点：`2026-03-31`

使用到的参考对象包括：

- 本地仓库 `/Users/repeater/Documents/Code/study/Mini-Agent`
- `SWE-agent/mini-swe-agent` 的浅克隆
- `shareAI-lab/learn-claude-code` 的浅克隆
- 各仓库公开 README 与关键实现文件

---

## 一句话结论

如果只看一句话，可以这样理解：

- `mini-swe-agent`：最小可运行的 coding agent baseline
- 本地 `Mini-Agent`：更实用的单 agent coding harness
- `learn-claude-code`：把 Claude Code 风格 harness 拆成课程的教学仓库
- `Claude Code`：产品级 coding agent 系统

如果只看你当前最关心的问题：

- 你的主学习对象应该仍然是本地 `Mini-Agent`
- 最值得作为“配套解释材料”的外部仓库是 `learn-claude-code`
- 最值得作为“极简对照组”的外部仓库是 `mini-swe-agent`

推荐优先级：

1. 本地 `Mini-Agent`
2. `learn-claude-code`
3. `mini-swe-agent`

---

## 一、四者的角色定位

## 1. `mini-swe-agent`

它最准确的定位不是“功能少一点的 coding agent”，而是：

- 一个极简主义的 AI software engineering agent
- 一个 bash-first 的 coding agent baseline
- 一个非常适合 benchmark、sandbox、研究实验、RL/FT 数据收集的最小 scaffold

它最核心的设计追求是：

- 控制流尽量小
- 让模型承担更多“智能”本身
- agent scaffold 不要抢戏
- 消息轨迹尽量线性、透明、便于调试

它不是在追求“把产品功能做全”，而是在追求：

- 少而必要
- 简而稳定
- 极易作为 baseline

这和很多人以为的“只是功能少一点”不一样。它其实是有明确方法论立场的。

## 2. 本地 `Mini-Agent`

本地这个仓库最准确的定位是：

- 一个实用型、可阅读、单 agent 的 coding assistant 框架或 demo
- 一个比纯 baseline 更接近实际使用体验的 harness
- 一个把多种实用能力打包进单体应用的入门级工程样板

它已经不只是“最小 agent loop”了，而是包含了很多现实中很有价值的增强层：

- 文件工具
- Bash 工具
- session note
- 长上下文摘要
- skills
- MCP
- CLI
- ACP 接入

它的核心目标不是研究最小性，而是：

- 代码仍然可读
- 功能已经足够像一个“能干活的 agent”

所以它是一个很典型的“实用型单 agent harness”。

## 3. `learn-claude-code`

这个项目最准确的定位不是“另一个 coding agent 项目”，而是：

- 一个关于 harness engineering 的教学仓库
- 一个把 Claude Code 风格机制按层拆开的课程仓库
- 一个强调“模型是 agent，代码是 harness”的思维训练材料

它的重点不是把所有能力都统一封装成一个成熟应用，而是把问题拆成 session：

- s01: 最小 loop
- s02: tool dispatch
- s03: todo / planning
- s04: subagent
- s05: skills
- s06: context compact
- s07: task system
- s08: background tasks
- s09-s11: teams / protocol / autonomous agents
- s12: worktree isolation

它最强的地方不是“这个仓库直接最好用”，而是：

- 它把 coding agent harness 为什么会逐步长成现在这样，讲得非常清楚

## 4. `Claude Code`

`Claude Code` 的定位显然是产品，而不是教学仓库或最小 baseline。

对它最合理的理解是：

- 一个产品级 coding agent 系统

这意味着它关心的东西一定不只包括：

- loop 能不能跑起来

还包括：

- 权限
- 安全
- 工作流
- 交互体验
- 稳定性
- 生命周期控制
- 用户信任边界
- 复杂工程环境下的表现

这里需要强调：

- 我们没有它的完整开源实现
- 所以不能把任何开源仓库说成“Claude Code 的真实源码简化版”

更准确的说法是：

- 有些开源仓库在讲解或复现 Claude Code 背后的 harness 思路
- 但它们并不等于 Claude Code 本体

---

## 二、最关键的概念区分：Agent 和 Harness

这是 `learn-claude-code` 反复强调、也最值得你吸收的一个思想。

很多人说“我在做 agent”，实际在做的往往是两种完全不同的事情中的一种：

1. 训练模型本身
2. 给模型搭工作外壳，也就是 harness

如果是 coding agent 语境，大部分工程师做的是第 2 种。

也就是说：

- 真正的“agent 智能”主要在模型
- 你在代码里搭的是：
  - 工具
  - 环境
  - 观察界面
  - 行动界面
  - 知识加载
  - 权限边界
  - 上下文管理
  - 协作机制

从这个角度看，这四者分别在不同层面回答不同问题：

- `mini-swe-agent`：最小 harness 到底能小到什么程度
- 本地 `Mini-Agent`：实用型单 agent harness 应该长什么样
- `learn-claude-code`：Claude Code 风格 harness 是怎样一层层长出来的
- `Claude Code`：这些想法做成产品以后会是什么样

这也是为什么它们不能简单放在一条“大小型号”线上。

---

## 三、总览对照表

| 维度 | `mini-swe-agent` | 本地 `Mini-Agent` | `learn-claude-code` | `Claude Code` |
|---|---|---|---|---|
| 主要身份 | 极简 baseline | 实用型单 agent 框架 | 教学型 harness 拆解仓库 | 产品级 coding agent |
| 主要目标 | 最小可用、易 benchmark、易 sandbox | 可读、可跑、够实用 | 让你理解 harness 为什么这样设计 | 面向真实开发工作流 |
| 默认行动面 | 以 bash 为核心 | 文件工具 + bash + note + skills + MCP | 从 bash 起步，再逐步加层 | 更丰富、更产品化 |
| 架构风格 | 线性、极简、bash-first | 模块化、单体整合、实用增强 | session 递进、概念分层 | 产品系统 |
| 是否强调环境抽象 | 很强 | 中等，偏 workspace | 后期教学中强调 | 很强，且更偏治理 |
| 是否有显式 task system | 相对弱 | 没有完整内建 task graph | 明确作为课程章节 | 推测更完整 |
| 是否覆盖多 agent / teams | 不是重点 | 没有内建 | 明确讲解 | 可能以产品形态存在 |
| 是否适合作为当前主学习对象 | 适合作为极简对照 | 最适合你当前主线 | 适合作为讲解镜子 | 不是代码阅读入口 |

---

## 四、按架构层逐项对照

## 4.1 控制循环

### `mini-swe-agent`

它最宝贵的地方，就是它对“最小控制循环”的坚持。

核心逻辑大致就是：

1. 组装消息
2. 调模型
3. 解析动作
4. 在环境里执行动作
5. 把观察结果回填给模型
6. 重复直到退出

这个设计的优点非常明显：

- 轨迹简单
- 容易 debug
- 很适合拿来做 benchmark
- 很适合拿来做训练数据采集
- scaffold 对模型行为的干扰相对小

它不是“没有能力”，而是“故意把不必要层拿掉”。

### 本地 `Mini-Agent`

本地仓库同样有一个非常标准的 agent loop，但外面已经包了不少实用层：

- cancellation
- step 级消息清理
- token 估算
- 自动 summarization
- logger
- tool registry

所以本地 `Mini-Agent` 的 loop 仍然清晰，但它已经不再追求“最小”，而是在追求：

- 够简单
- 但别太简陋

### `learn-claude-code`

这个仓库最大的优点，就是把最小 loop 本身变成了第一课。

也就是说，它不是默认你已经理解：

- tool_use stop_reason
- tool_result 回填
- 为什么 loop 可以很小

而是直接从 `s01` 把这件事讲起。

接着每一章再在 loop 外面多包一层 harness 机制。

所以在教学价值上，它比“直接给你一个完整应用”更适合作为概念分解器。

### `Claude Code`

关于 `Claude Code`，最稳妥的判断是：

- 它背后的 loop 不太可能比这些开源项目复杂很多
- 真正复杂、真正体现产品价值的，是 loop 外层的 harness 与治理系统

这也是 `learn-claude-code` 想传达的核心思想。

---

## 4.2 工具哲学

### `mini-swe-agent`：bash-first

它的哲学立场很鲜明：

- shell 足够强
- 很多工具接口没必要都单独发明
- 只要模型够强，它可以自己通过 bash 调动现有环境能力

优点：

- harness 更小
- sandbox 更简单
- 轨迹更统一
- baseline 更干净

代价：

- 很多行为是“让模型自己想办法”
- 工具层结构化程度更低
- 对模型质量依赖更高

### 本地 `Mini-Agent`：结构化工具更丰富

本地仓库选择了更实用的路线。

它有：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- note 工具
- skill 工具
- MCP 加载出来的工具

这带来的变化是：

- 常见能力有更明确的接口
- 比纯 bash 更容易控制
- 比纯 bash 更容易读懂 agent 的行为
- 更像很多人心中“真正能用的 coding assistant”

所以从工具哲学上看：

- `mini-swe-agent` 更极简
- 本地 `Mini-Agent` 更实用

### `learn-claude-code`：把工具扩张变成课程

它最有教学价值的一点，是它不只是告诉你“最后有很多工具”，而是告诉你：

- 为什么开始只有 bash
- 为什么后来要有 read / write / edit
- 为什么会出现 TodoWrite
- 为什么 skill loading 需要单独成为一章
- 为什么 team mailbox 也会变成工具接口

它讲的不是工具清单，而是“工具层是如何生长的”。

### `Claude Code`

这里必须用推断语气。

合理的判断是：

- 产品级 coding agent 的工具层一定比纯 bash-only 更丰富
- 但真正高明的地方，不是“工具越多越好”，而是“工具边界是否合理”

换句话说：

- `mini-swe-agent` 在问：bash alone 能不能成立
- `Claude Code` 在问：怎样的工具面最适合真实工程工作流
- 本地 `Mini-Agent` 处在两者中间

---

## 4.3 环境模型与隔离

### `mini-swe-agent`

这是它特别强的一点。

它把“执行环境”当成核心设计轴，而不是附属实现细节。

它能很好地适配：

- local
- docker / podman
- singularity / apptainer
- 其他隔离环境

而且它的动作执行模型倾向于独立调用，而不是高度依赖长生命周期 shell state。

这非常适合：

- benchmark
- reproducibility
- sandbox portability
- 大规模实验

### 本地 `Mini-Agent`

本地仓库有很强的 workspace 意识：

- 相对路径都基于 workspace
- system prompt 注入 current workspace
- file tool 以 workspace 为边界
- bash tool 也可绑定 workspace cwd

但它的架构重点不是“环境抽象本身”。

它更像：

- 一个在本地工作目录内很好用的 coding agent 应用

而不是：

- 一个高度围绕环境抽象做 benchmark / sandbox 设计的 baseline

### `learn-claude-code`

它的处理方式很像一门课：

- 不是一上来就塞给你复杂环境抽象
- 而是先讲 loop
- 再讲 harness
- 再讲 task isolation / worktree isolation

这在教学上反而更好，因为你知道：

- 为什么后面会出现这些机制
- 它们是在解决什么问题

### `Claude Code`

在产品里，隔离和权限不再只是“工程优雅性”问题，而是用户信任问题。

所以很合理的判断是：

- 产品级系统在这方面一定比 demo 和教学仓库走得更深

---

## 4.4 记忆与上下文管理

### `mini-swe-agent`

它对这件事的态度更偏“保持线性历史的透明性”。

这很适合：

- 研究
- 调试
- benchmark
- 训练数据检查

但它不会优先追求“长期会话体验最强”。

### 本地 `Mini-Agent`

本地仓库已经引入了两个非常重要的实用机制：

1. Session Note Tool
2. Token 超阈值时自动摘要

这说明它已经不满足于：

- 把 loop 跑起来

而是在解决：

- 多轮对话如何延续
- 长上下文如何活下去

这是它相较于纯 baseline 的一个非常关键的跃迁。

### `learn-claude-code`

这个仓库在上下文管理上非常具有解释力。

它会明确讲：

- subagent 为什么能隔离上下文污染
- skills 为什么要按需加载，而不是全部塞进系统提示词
- context compact 为什么必要
- task persistence 为什么会出现

如果你读本地仓库时对“为什么要有 summary / skills / subagent 这些层”产生问题，那么 `learn-claude-code` 正好是解答器。

### `Claude Code`

产品级 coding agent 的成败，很大程度上取决于上下文治理质量。

所以在这个维度上，你可以把三者理解成：

- `mini-swe-agent`：更重视轨迹干净
- 本地 `Mini-Agent`：开始补上实用上下文治理
- `learn-claude-code`：把上下文治理讲成体系
- `Claude Code`：把这件事做成产品体验

---

## 4.5 Skills 与按需知识加载

### `mini-swe-agent`

这不是它的中心卖点。

它的重点仍然是：

- harness 尽量轻
- model 尽量自己解决更多事

### 本地 `Mini-Agent`

本地仓库把 skills 当成一等能力来做：

- 有 skills 目录
- 有 metadata / full content 的分层
- 有 `get_skill` 工具
- 有 progressive disclosure 的说明

这说明它已经很接近“实用型 coding harness”的思路了：

- 基础系统提示词只放必要信息
- 更专业的知识按需加载

### `learn-claude-code`

它的 `s05` 几乎就是把这件事拿出来单独讲。

从概念映射上说：

- 本地 `Mini-Agent` 的 skill 设计
- `learn-claude-code` 的 `s05`

两者之间是非常强的同类关系。

区别主要在于：

- 本地仓库是工程化整合后的实现
- `learn-claude-code` 是把设计原理单独剥出来教你

### `Claude Code`

这里不能做过度断言，但合理推断是：

- 产品级 coding agent 也会避免把全部领域知识预塞进初始 prompt
- 更可能采用某种按需扩展的上下文加载策略

---

## 4.6 Planning 与 Task System

### `mini-swe-agent`

planning 更多依赖：

- prompt
- 模型自己计划

而不是一个很厚的 harness task subsystem。

这符合它的哲学。

### 本地 `Mini-Agent`

本地仓库有任务执行指导，但没有一个像 `learn-claude-code` 那样完整显式的 task graph 系统。

这点很重要：

- 它很实用
- 但它还不是一个完整任务编排框架

也就是说，它更像：

- 强单 agent 执行器

而不是：

- 强任务系统驱动的 orchestration 平台

### `learn-claude-code`

这恰恰是它比本地仓库更“广”的一个地方。

它明确把下面这些做成课程章节：

- TodoWrite
- file-based task system
- dependency graph
- teammate coordination around tasks

也就是说，如果你想理解：

- coding agent 从“会用工具”走向“会管理任务”

那么 `learn-claude-code` 比本地仓库更直接。

### `Claude Code`

产品级系统大概率会比本地仓库更接近这条路。

所以这里很值得记住一个结论：

- 本地 `Mini-Agent` 在“实用型单 agent”上更完整
- `learn-claude-code` 在“高级 harness 概念覆盖面”上反而更完整

---

## 4.7 后台执行与并发

### `mini-swe-agent`

它的主强项不是“复杂后台系统”，而是：

- 执行动作独立
- 环境切换清晰

### 本地 `Mini-Agent`

本地仓库已经有很实用的后台 shell 能力：

- 背景进程管理
- 输出监控
- 可轮询结果
- 可 kill

这说明它不是一个只会同步跑小命令的玩具。

不过它和完整任务系统式的后台编排，还不是同一个层次。

### `learn-claude-code`

它把 background task 单独作为一章来讲，价值在于：

- 你会知道为什么 agent 需要“边等边思考”
- 为什么后台通知是 harness 机制

如果你已经看懂本地仓库的 background shell，再看 `s08`，会很容易产生映射感。

### `Claude Code`

产品级 coding agent 很自然会把：

- 后台执行
- 状态通知
- 中断
- 恢复

做得更完整。

本地仓库可以视为这个方向上的一个简化实用版本。

---

## 4.8 多 agent、团队协作、worktree 隔离

### `mini-swe-agent`

这不是它的重点问题域。

它的重点是：

- 单 agent 最小闭环能多简单

### 本地 `Mini-Agent`

这是一个明显偏单 agent 的仓库。

这不是缺点，而是明确的范围选择。

优点：

- 更容易读懂
- 更容易改
- 结构更集中

局限：

- 不直接教你更复杂的多 agent harness 模式

### `learn-claude-code`

这个仓库在教学覆盖范围上最强的一点，就在这里。

它明确讲：

- subagent
- mailbox 协议
- team protocol
- auto-claim
- worktree isolation

这使它特别适合作为你之后的“架构进阶材料”。

### `Claude Code`

不能把任何外部项目直接当成它的源码替代物。

但如果你想理解：

- 为什么产品级 coding agent 迟早会走向更强的 task / team / isolation 机制

那么 `learn-claude-code` 是非常好的解释性参照。

---

## 4.9 权限、治理与安全边界

### `mini-swe-agent`

它更偏 baseline 风格治理：

- 依靠更简单的环境模型
- interactive 模式提供基本人类确认

### 本地 `Mini-Agent`

本地仓库有实用控制，但它的主要定位并不是权限治理框架。

它的重点更多是：

- 单 agent 功能组合
- 使用体验
- 代码可读性

### `learn-claude-code`

它会在概念层强调 permissions 是 harness 的一部分，但它自己也明确说省略了不少 production 机制。

这反而是优点，因为它没有过度吹成“完全等价产品实现”。

### `Claude Code`

权限治理、审批、信任边界，几乎一定是产品和 demo 之间最大的差异之一。

所以任何“开源项目 = Claude Code 简化版”的说法，都应该谨慎。

---

## 五、这四者不是单纯的“大小版本关系”

很多人会下意识这样排：

- `mini-swe-agent` < 本地 `Mini-Agent` < `learn-claude-code` < `Claude Code`

这种排法有一定直觉性，但不准确。

更准确的理解应该是多轴的。

## 5.1 按“极简程度”看

从最极简到最不极简，大致是：

1. `mini-swe-agent`
2. `learn-claude-code` 的早期 session
3. 本地 `Mini-Agent`
4. `Claude Code`

## 5.2 按“教学清晰度”看

从最适合讲概念到最不适合作为课程，大致是：

1. `learn-claude-code`
2. `mini-swe-agent`
3. 本地 `Mini-Agent`
4. `Claude Code`

原因很简单：

- 产品不是课程
- 完整应用也不如拆章节讲得清楚

## 5.3 按“单仓库的实用整合度”看

在开源仓库里，如果只看你现在讨论的这三个开源项目：

- 本地 `Mini-Agent` 是更偏“整合好的单 agent 应用”
- `learn-claude-code` 概念覆盖更广，但很多实现是教学代码，不是单一完整产品封装
- `mini-swe-agent` 则是有意识地保持 baseline 风格

所以它们不是简单的“大中小型号”关系，而是：

- `mini-swe-agent`：最小 baseline
- 本地 `Mini-Agent`：实用单 agent app
- `learn-claude-code`：教学拆解平台
- `Claude Code`：产品系统

---

## 六、本地 `Mini-Agent` 和 `learn-claude-code` 的关系

这组关系对你当前最重要。

## 6.1 相同点

两者都不满足于：

- 只有一个最小 loop

它们都关心 harness 里的更高层机制，例如：

- skills
- context 管理
- agent 在真实任务中的持续执行能力

## 6.2 不同点

本地 `Mini-Agent` 更像：

- 一个已经整合成单体应用的实用仓库

`learn-claude-code` 更像：

- 一个把这些机制拆成课程章节的教学仓库

换句话说：

- 本地仓库回答“一个能用的单 agent app 长什么样”
- `learn-claude-code` 回答“这些能力为什么会一层层长出来”

## 6.3 谁更接近你当前需求

如果你的目标是：

- 学会改你手头这份代码
- 学会 trace 它的模块关系
- 学会基于它继续开发

那本地 `Mini-Agent` 必须是主线。

如果你的目标是：

- 学懂这些设计背后的原因
- 把本地仓库放进更大的 coding agent 设计图景里

那 `learn-claude-code` 是最好的对照材料。

所以它们不是替代关系，而是：

- 主代码库
- 理论镜子

---

## 七、本地 `Mini-Agent` 和 `mini-swe-agent` 的关系

## 7.1 相同点

它们都属于：

- 代码可读
- 相对轻量
- 不是纯前端包装壳

## 7.2 不同点

`mini-swe-agent` 更强调：

- minimal control flow
- bash-only 或 bash-first
- baseline / benchmark / sandbox 友好

本地 `Mini-Agent` 更强调：

- 更丰富的 tool surface
- session note
- summarization
- skills
- MCP
- CLI / ACP

换句话说：

- `mini-swe-agent` 是把 scaffold 压薄
- 本地 `Mini-Agent` 是把单 agent 做得更像“能长期用”的实用工具

## 7.3 对你有什么意义

如果你一开始就钻进 `mini-swe-agent`，你会很快理解：

- 最小 loop 到底长什么样

但你不会自动理解：

- 为什么本地仓库有 summary
- 为什么有 skills
- MCP 为什么值得集成
- 为什么要有 background shell

所以对你当前目标来说，它更适合作为：

- 极简对照组

而不是：

- 第一主线教材

---

## 八、`learn-claude-code` 和 `mini-swe-agent` 的关系

这两个项目表面上都很欣赏“简单”，但它们服务的是不同问题。

## 8.1 `mini-swe-agent` 的问题意识

它在问：

- coding agent 的 scaffold 能不能极简到几乎只剩 loop + bash + environment

## 8.2 `learn-claude-code` 的问题意识

它在问：

- 如果从最小 loop 开始，Claude Code 风格 harness 是怎样逐层长出来的

所以两者差异不是“一个简单，一个复杂”这么粗糙，而是：

- 一个是在做 baseline
- 一个是在做课程

---

## 九、本地 `Mini-Agent` 能不能被叫做“只实现了核心功能的 Claude Code”？

这个说法有一点直觉上的合理性，但如果要精确，应该改写。

### 为什么这个说法有一定道理

因为本地仓库确实已经拥有一些更接近 Claude Code 类产品的特征：

- 不只是一个最小 loop
- 有 richer tools
- 有 memory / summary
- 有 skills
- 有 MCP
- 有更完整的 CLI 使用面

所以它确实比 `mini-swe-agent` 更像“实用 coding harness”。

### 为什么这个说法还不够准确

因为它容易让人误以为：

- 本地仓库是 Claude Code 的直接简化复刻版

这就超出了证据边界。

更准确的说法应该是：

- 本地 `Mini-Agent` 是一个处在“极简 baseline”和“产品级 coding agent”之间的实用型单 agent harness

如果再稍微口语化一点：

- 它比 `mini-swe-agent` 更接近 Claude Code 这类产品的形态
- 但它不是 Claude Code 的直接精简实现

这个表述会更稳妥。

---

## 十、对你最有用的学习顺序

既然你的明确目标是：

- 学本地这个项目

那么最好的顺序不是先转去看最火的仓库，而是：

## 10.1 第一阶段：把本地 `Mini-Agent` 主线吃透

建议阅读顺序：

1. `README.md`
2. `examples/01_basic_tools.py`
3. `examples/02_simple_agent.py`
4. `mini_agent/tools/base.py`
5. `mini_agent/tools/file_tools.py`
6. `mini_agent/tools/bash_tool.py`
7. `mini_agent/agent.py`
8. `mini_agent/cli.py`
9. `mini_agent/tools/note_tool.py`
10. `mini_agent/tools/skill_loader.py`
11. `mini_agent/tools/skill_tool.py`
12. `mini_agent/tools/mcp_loader.py`
13. `mini_agent/config.py`
14. `mini_agent/config/system_prompt.md`

这样读的好处是：

- 先看表面能力
- 再看工具
- 再看 loop
- 再看扩展层

## 10.2 第二阶段：用 `learn-claude-code` 做“架构解释镜子”

本地仓库读到相应概念时，再配对看这些 session：

1. `s01_agent_loop`
2. `s05_skill_loading`
3. `s06_context_compact`
4. `s08_background_tasks`
5. `s07_task_system`
6. `s09_agent_teams`
7. `s12_worktree_task_isolation`

为什么是这个顺序：

- 前四个和本地仓库最容易建立映射
- 后三个帮助你看见本地仓库还没有覆盖的更高层机制

## 10.3 第三阶段：再读 `mini-swe-agent` 做“减法思考”

等你已经看懂本地仓库之后，再去读：

- `README.md`
- `src/minisweagent/agents/default.py`
- `src/minisweagent/environments/local.py`
- `src/minisweagent/models/litellm_model.py`
- `src/minisweagent/run/mini.py`

你会得到一个非常宝贵的能力：

- 分辨哪些层是真核心
- 哪些层是 practical harness richness
- 哪些层是可以拿掉的，哪些不能

---

## 十一、本地 `Mini-Agent` 和 `learn-claude-code` 的最佳映射表

| 本地 `Mini-Agent` 的点 | `learn-claude-code` 最对应的 session | 说明 |
|---|---|---|
| agent loop | `s01_agent_loop` | 最基础的循环模式 |
| 增加 structured tools | `s02_tool_use` | 本地仓库是整合版 |
| task guidance / 简单计划意识 | `s03_todo_write` | 本地仓库没有完整 Todo 子系统，但有执行指导 |
| skills | `s05_skill_loading` | 概念映射非常强 |
| summarization / context 管理 | `s06_context_compact` | 本地仓库已经有实用实现 |
| background shell | `s08_background_tasks` | 机制不完全一样，但是同一类问题 |
| task graph | `s07_task_system` | 本地仓库没有完整内建 |
| subagent / teams / mailbox | `s09-s11` | 本地仓库未覆盖 |
| worktree isolation | `s12_worktree_task_isolation` | 本地仓库未覆盖 |

这个映射表的价值在于：

- 你不会因为看到 `learn-claude-code` 覆盖面更广，就误以为本地仓库“不完整”
- 也不会因为本地仓库更像一个可运行应用，就忽略了背后的设计原理

---

## 十二、它们分别最适合教你什么

## 12.1 如果你想学“最小 loop 到底长什么样”

最适合的是：

- `mini-swe-agent`

原因：

- scaffold 足够薄
- loop 足够干净
- 环境模型足够清晰

## 12.2 如果你想学“一个实用单 agent coding harness 怎么搭”

最适合的是：

- 本地 `Mini-Agent`

原因：

- 单体整合度高
- 实用功能多
- 代码仍然可读
- 很适合上手改

## 12.3 如果你想学“为什么这些高级机制会出现”

最适合的是：

- `learn-claude-code`

原因：

- session 拆解
- 讲的是机制生长逻辑
- 对 Claude Code 风格 harness 的解释力很强

## 12.4 如果你想学“产品级 coding agent 最终是什么体验”

参照对象是：

- `Claude Code`

但要注意：

- 它不是一个能直接拿来逐文件阅读的开源课程仓库

---

## 十三、针对你当前目标的最终建议

你现在的目标不是：

- 学遍所有 coding agent 仓库

而是：

- 学懂本地这个项目

所以最优策略是：

### 主线

- 本地 `Mini-Agent`

### 最佳辅助资料

- `learn-claude-code`

### 最佳极简对照组

- `mini-swe-agent`

这三者的分工可以记成一句话：

- 学代码实现，看本地 `Mini-Agent`
- 学设计理由，看 `learn-claude-code`
- 学最小本质，看 `mini-swe-agent`

这比“先去看最火的那个”更有效率。

---

## 十四、最值得你记住的判断

### 判断 1

`learn-claude-code` 火，不代表它应该取代本地仓库成为你的第一学习对象。

它火，更多说明：

- 它是一个非常好的讲解仓库

### 判断 2

本地 `Mini-Agent` 不只是“比 `mini-swe-agent` 多点功能”。

更准确地说，它属于：

- 已经具备实用 harness 特征的单 agent 应用

### 判断 3

`mini-swe-agent` 的价值不在于“功能少”，而在于：

- 它非常清楚地告诉你最小 baseline 到底能压缩到什么程度

### 判断 4

`learn-claude-code` 和本地 `Mini-Agent` 不是竞争关系，而是：

- 一个负责“做出来”
- 一个负责“讲清楚为什么这么做”

---

## 十五、最终结论

如果问题是：

“我现在最应该先学哪一个？”

答案是：

- 本地 `Mini-Agent`

如果问题是：

“哪个项目最适合拿来解释它背后的设计思路？”

答案是：

- `learn-claude-code`

如果问题是：

“哪个项目最适合让我看清 coding agent 的最小本质？”

答案是：

- `mini-swe-agent`

因此，对你当前阶段最合理的顺序是：

1. 本地 `Mini-Agent`
2. `learn-claude-code`
3. `mini-swe-agent`

不是因为三者有简单的强弱关系，而是因为它们分别回答的是三种不同的问题。

---

## 十六、参考入口

### 本地 `Mini-Agent`

- 本地仓库根目录：`/Users/repeater/Documents/Code/study/Mini-Agent`
- README：`README.md`
- 核心 loop：`mini_agent/agent.py`
- CLI / tool 装配：`mini_agent/cli.py`
- system prompt：`mini_agent/config/system_prompt.md`
- file tools：`mini_agent/tools/file_tools.py`
- bash tools：`mini_agent/tools/bash_tool.py`
- note tools：`mini_agent/tools/note_tool.py`
- skill loader：`mini_agent/tools/skill_loader.py`
- MCP loader：`mini_agent/tools/mcp_loader.py`

### `mini-swe-agent`

- 仓库：<https://github.com/SWE-agent/mini-swe-agent>
- README：<https://github.com/SWE-agent/mini-swe-agent/blob/main/README.md>
- default agent：<https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/agents/default.py>
- local environment：<https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/environments/local.py>
- model wrapper：<https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/models/litellm_model.py>
- CLI 入口：<https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/run/mini.py>

### `learn-claude-code`

- 仓库：<https://github.com/shareAI-lab/learn-claude-code>
- README：<https://github.com/shareAI-lab/learn-claude-code/blob/main/README.md>
- `s01_agent_loop.py`：<https://github.com/shareAI-lab/learn-claude-code/blob/main/agents/s01_agent_loop.py>
- `s05_skill_loading.py`：<https://github.com/shareAI-lab/learn-claude-code/blob/main/agents/s05_skill_loading.py>
- `s09_agent_teams.py`：<https://github.com/shareAI-lab/learn-claude-code/blob/main/agents/s09_agent_teams.py>
- `s_full.py`：<https://github.com/shareAI-lab/learn-claude-code/blob/main/agents/s_full.py>

### `Claude Code`

- 本文对 `Claude Code` 的比较是高层、概念性的
- 不应把任何一个开源仓库直接等同为它的源码替代物

### `Kode-Agent`

- 仓库：<https://github.com/shareAI-lab/Kode-Agent>
- README：<https://github.com/shareAI-lab/Kode-Agent/blob/main/README.md>
- package 定义：<https://github.com/shareAI-lab/Kode-Agent/blob/main/package.json>
- CLI 入口：<https://github.com/shareAI-lab/Kode-Agent/blob/main/src/entrypoints/cli.tsx>
- 系统总览：<https://github.com/shareAI-lab/Kode-Agent/blob/main/docs/develop/overview.md>
- 系统架构：<https://github.com/shareAI-lab/Kode-Agent/blob/main/docs/develop/architecture.md>
- tools 系统：<https://github.com/shareAI-lab/Kode-Agent/blob/main/docs/develop/tools-system.md>
- TaskTool：<https://github.com/shareAI-lab/Kode-Agent/blob/main/src/tools/agent/TaskTool/TaskTool.tsx>

---

## 附录 A：`Kode-Agent` 补充对照

你后来补充的这个项目非常值得看，因为它和 `learn-claude-code` 属于同一个组织，而且两者之间不是简单重复关系。

最值得先记住的一句话是：

- `learn-claude-code` 更像“教学拆解”
- `Kode-Agent` 更像“面向落地的产品型 CLI”

也就是说，它们之间更像：

- 一个讲原理
- 一个做产品

而不是：

- 两个都在做同一层级的示例仓库

---

## 附录 B：`Kode-Agent` 的基本定位

从 README、目录结构、文档和源码来看，`Kode-Agent` 可以概括为：

- 一个明显偏产品化的 terminal-native AI coding assistant
- 一个 TypeScript/Bun/React Ink 技术栈实现的 CLI agent 系统
- 一个强调工具系统、权限系统、subagent、multi-model、plugin/skill marketplace 的大体量工程

它和前面几个项目最大的不同不是“能做更多事”这么简单，而是：

- 它已经明显站在“产品工程”这一层了

一些很关键的信号：

- npm 包名是 `@shareai-lab/kode`
- 有独立的 `cli.js`、`cli-acp.js`
- 有大量 `commands/`
- 有 `ui/`、`services/`、`tools/`、`context/`、`core/`
- 有完整的 `docs/develop/*` 架构文档
- 有权限模式、TaskTool、subagent、plugin marketplace、AGENTS.md 标准兼容

这说明它不是“教程代码慢慢长大了一点”，而是：

- 已经进入可发布、可安装、可配置、可治理的产品化阶段

---

## 附录 C：`Kode-Agent` 和 `learn-claude-code` 的关系

这是最值得先澄清的关系。

### 1. 它们不是重复项目

虽然都来自 `shareAI-lab`，但定位非常不同：

- `learn-claude-code`：课程化、session 化、强调 mental model
- `Kode-Agent`：面向实际使用的 CLI 工程

### 2. 它们更像“教程”和“成品”的关系

`learn-claude-code` 的 README 在后面其实就已经给了一个很强的暗示：

- 学完 12 个 session 之后，接下来可以去用 `Kode Agent CLI` 或 `Kode Agent SDK`

这说明它们在同一个叙事里大致是：

- 先通过 `learn-claude-code` 学会 harness engineering
- 再通过 `Kode-Agent` 看这些想法如何进入更完整的 CLI 形态

所以你完全可以把两者理解成：

- `learn-claude-code` 是课程
- `Kode-Agent` 是同体系里的产品化实现

### 3. 这和本地 `Mini-Agent` 的关系不同

本地 `Mini-Agent` 和 `learn-claude-code` 的关系更像：

- 一个实用型开源单 agent 仓库
- 一个教学镜子

而 `Kode-Agent` 和 `learn-claude-code` 的关系更像：

- 一个教材
- 一个落地产品线

这也是为什么你发现 `Kode-Agent` 之后，对整个地图的理解会更完整。

---

## 附录 D：`Kode-Agent` 和本地 `Mini-Agent` 的核心差异

这是你当前最关心的部分。

如果要一句话总结：

- 本地 `Mini-Agent` 是“实用型单 agent harness”
- `Kode-Agent` 是“产品级 terminal agent 平台”

这个差异不是局部功能差异，而是系统层级差异。

### 1. 代码规模差异

做了一个快速规模对比：

- `Kode-Agent` 的 `src + tests + docs` 文件数大约 `604`
- `Kode-Agent` 的 `src/tests` TS/JS 代码行数大约 `102k`
- 本地 `Mini-Agent` 的核心 Python 代码量（不含 skills）大约 `5.1k`

这组数字不能直接等于“质量高低”，但非常能说明定位差异：

- 本地 `Mini-Agent` 是一个小而整合的单 agent 仓库
- `Kode-Agent` 是一个重工程化的大系统

### 2. 技术栈差异

本地 `Mini-Agent`：

- Python
- `prompt-toolkit`
- 以单仓库 CLI + tools + config 为核心

`Kode-Agent`：

- TypeScript
- Node/Bun
- React + Ink 终端 UI
- 更明显的前台交互层和产品 UI 层

也就是说：

- 本地仓库更像“工程师友好的 Python 实现”
- `Kode-Agent` 更像“专门打磨终端产品体验的 TS CLI 系统”

### 3. 架构层级差异

本地 `Mini-Agent` 的主结构相对集中：

- `agent`
- `cli`
- `llm`
- `tools`
- `config`
- `acp`

`Kode-Agent` 的层级明显更厚：

- `entrypoints`
- `app`
- `commands`
- `context`
- `core`
- `services`
- `tools`
- `ui`
- `acp`

这意味着它不是简单地把一个 agent loop 包起来，而是已经拆出了：

- 命令系统
- UI 层
- 服务层
- 权限上下文
- 多模型管理
- 插件与 marketplace

所以和本地仓库相比，`Kode-Agent` 已经不只是“更多功能”，而是：

- 更多系统分层
- 更多产品边界
- 更多工程治理

### 4. 工具哲学差异

本地 `Mini-Agent` 的工具哲学是：

- 明确文件工具
- 明确 bash
- note
- skill
- MCP

它已经比 `mini-swe-agent` 丰富很多，但总体仍然是：

- 单 agent 视角下的可用工具集

`Kode-Agent` 的工具哲学更接近“tool-first architecture”：

- 一切能力都抽象成 Tool
- Tool 带 schema、权限、异步执行、结果格式化
- Tool 既服务 AI，也服务整个 CLI 系统

这个层级是更高的。

本地仓库更像：

- “agent 需要一组工具”

`Kode-Agent` 更像：

- “整个产品能力都由工具系统统一建模”

### 5. 权限与安全模型差异

这是两者非常大的分水岭。

本地 `Mini-Agent`：

- 有基本的工具边界
- 但不是一个完整权限治理系统

`Kode-Agent`：

- README 明确区分默认 YOLO / `--safe`
- 文档里有完整的 permission architecture
- Tool 级、session 级、persistent 级、mode 级权限分层
- 有显式安全模型文档

这意味着：

- 本地仓库更偏“开发样板”
- `Kode-Agent` 更偏“可交付给用户的工具”

### 6. 多模型能力差异

本地 `Mini-Agent`：

- 支持 Anthropic/OpenAI 风格 provider
- 有较清晰的 provider wrapper
- 更偏“把模型调用统一起来”

`Kode-Agent`：

- 明显有更完整的 multi-model manager
- README 里明确区分 `main`、`task`、`compact`、`quick`
- 可以动态切模型
- 更强调不同子任务使用不同模型

这和本地仓库的差异在于：

- 本地仓库的模型抽象是“provider 统一”
- `Kode-Agent` 的模型抽象已经进化到“多模型协作调度”

### 7. Subagent / TaskTool 差异

本地 `Mini-Agent`：

- 是单 agent 仓库
- 没有完整内建的 subagent / task tool 架构

`Kode-Agent`：

- 有明确的 `TaskTool`
- 支持 `subagent_type`
- 有 agent template
- 有 `@run-agent-*` 风格 mention
- 文档中直接把它定位为复杂任务 delegation 的核心机制

所以这里的差异不是“本地仓库少几个功能”这么简单，而是：

- 本地仓库主要还是单 agent 执行器
- `Kode-Agent` 已经进入 agent orchestration 层

### 8. Skills / Plugins / Marketplace 差异

本地 `Mini-Agent`：

- 已经有 skills
- skill loading 设计很不错
- 也有 MCP

但整体上仍然更像：

- 仓库内置技能 + MCP 工具集成

`Kode-Agent`：

- 支持 skills
- 支持 plugins
- 支持 marketplace
- 支持 AGENTS.md / `.claude` 兼容
- 支持安装、启停、作用域（user/project）

这让它更像一个平台，而不是一个只给自己用的 harness。

### 9. 终端体验差异

本地 `Mini-Agent`：

- 终端体验是实用型 CLI
- 已经有颜色、history、交互式输入
- 对学习很友好

`Kode-Agent`：

- 明显更强调终端产品体验
- Ink UI
- 命令菜单
- 状态线
- 更强交互界面

这对学习来说反而有双刃剑效应：

- 用起来可能更强
- 但读代码不一定更容易

---

## 附录 E：`Kode-Agent` 在整体地图中的位置

如果把我们现在讨论的几个对象重新摆一遍，大概会更像这样：

### 1. 按“教学性”排序

1. `learn-claude-code`
2. 本地 `Mini-Agent`
3. `mini-swe-agent`
4. `Kode-Agent`
5. `Claude Code`

这里不是说后面的不好，而是说：

- 越偏产品，越不适合作为第一阅读入口

### 2. 按“极简性”排序

1. `mini-swe-agent`
2. `learn-claude-code` 早期 session
3. 本地 `Mini-Agent`
4. `Kode-Agent`
5. `Claude Code`

### 3. 按“产品化程度”排序

1. `Claude Code`
2. `Kode-Agent`
3. 本地 `Mini-Agent`
4. `mini-swe-agent`
5. `learn-claude-code`

这里 `mini-swe-agent` 和 `learn-claude-code` 的先后要看你定义：

- 如果按 CLI 完整度，`mini-swe-agent` 可能更像产品
- 如果按教学意图，`learn-claude-code` 明显更像课程

### 4. 按“和本地 `Mini-Agent` 的相似度”看

这点很容易误判。

很多人会觉得：

- `Kode-Agent` 功能更丰富，所以更像本地仓库的“升级版”

但从学习与代码结构角度看，不完全是。

更准确地说：

- 本地 `Mini-Agent` 和 `learn-claude-code` 在“解释 harness 机制”上更容易互相映射
- 本地 `Mini-Agent` 和 `Kode-Agent` 在“都想做可用 coding assistant”上更接近

所以：

- 如果你想学设计原理，先看 `learn-claude-code`
- 如果你想看成熟 CLI 产品形态，去看 `Kode-Agent`

---

## 附录 F：`Kode-Agent` 对你当前学习路径的意义

对你当前最重要的不是“要不要改学 `Kode-Agent`”，而是要知道它应该放在什么位置。

### 不建议现在立刻切主线去学 `Kode-Agent`

原因：

- 它太大
- 层次太厚
- 产品层代码很多
- 对你当前“学懂本地仓库”的目标并不是最短路径

### 但它非常值得作为“远端参照物”

它能帮助你理解：

- 如果一个 harness 继续产品化，会长成什么样
- 权限系统、多模型、subagent、插件市场、终端 UI 如何进入一个成熟 CLI

### 最合理的位置

你可以把它放在当前学习路径的第四步：

1. 本地 `Mini-Agent`
2. `learn-claude-code`
3. `mini-swe-agent`
4. `Kode-Agent`

这个顺序的好处是：

- 先学你真正要掌握的本地代码
- 再学背后的设计解释
- 再看极简本质
- 最后再看产品级 CLI 怎么做

这样不会被大项目的信息量淹没。

---

## 附录 G：加入 `Kode-Agent` 之后的最终判断

把 `Kode-Agent` 也加进来之后，最准确的整体结论可以写成：

- `mini-swe-agent`：最小 baseline
- 本地 `Mini-Agent`：实用型单 agent harness
- `learn-claude-code`：Claude Code 风格 harness 教程
- `Kode-Agent`：更成熟、更产品化的开源 terminal agent CLI
- `Claude Code`：产品级参考点

如果只问“和本地这个项目最应该怎么比”，答案是：

- 本地 `Mini-Agent` 不是 `Kode-Agent` 的简化版
- `Kode-Agent` 也不是本地仓库的直接升级版
- 二者更像处在不同产品阶段的两个开源实现

更精确地说：

- 本地 `Mini-Agent` 偏“小而清晰、适合学习和改造的实用单 agent”
- `Kode-Agent` 偏“大而完整、接近真实终端产品的 agent 平台”

所以对你来说：

- 想学本地代码，继续以本地仓库为主
- 想理解更大的设计图景，看 `learn-claude-code`
- 想看产品化开源 CLI 长什么样，看 `Kode-Agent`

