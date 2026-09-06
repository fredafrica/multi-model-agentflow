# multi-model-agentflow

[English](README.md) | [简体中文](README.zh-CN.md) | [Español](README.es.md) | [Português](README.pt.md)

`multi-model-agentflow` 是一个平台无关、领域无关、风险感知的多模型协作控制平面，用于协调计划、授权、执行、验证、审核、费用追踪、隐私检查和恢复。

这个项目围绕一个核心想法构建：模型应当在明确边界内生成、实施和审核；授权、状态、预算、安全门禁、证据和恢复应当由确定性软件负责。

## 为什么做这个

复杂 Agent 工作流可能以难以审计的方式失败：

- 计划发生变化，但授权边界没有同步变化；
- 一个模型完成编写、测试、审核，并宣布自己的工作完成；
- 远程模型收到过多上下文或敏感数据；
- 付费调用在结果不确定后被重复重试；
- 长时间任务中断后缺少明确的恢复状态；
- 人工修改会让部分结果失效，但不一定让全部结果失效；
- 状态散落在聊天、终端、日志、模型输出和 Git diff 中。

`multi-model-agentflow` 试图让这些工作流变得明确、可检查、可恢复。

## 当前状态

本地 MVP 已实现，并通过自动化测试验证。它覆盖了核心闭环：

```text
plan -> authorize -> implement -> self-test -> deterministic gates
-> independent review -> revise/rereview -> approve
```

它还覆盖安全暂停、立即冻结、人工接管、可恢复执行、幂等调用追踪、费用证据、文件边界检查和审核证据。

当前实现范围包含基于 OpenCode 的远程角色：

- 远程 Reviewer：只读、仅接收 packet、不访问仓库工作区；
- 远程 Worker：承担 implementation/revision 角色，但必须由任务合同显式允许；
- 远程 Worker 在最小 staging sandbox 中执行；
- 远程 Worker 输入通过 `input_artifacts` 声明，并使用 SHA-256 校验；
- 远程 Worker 输出只同步回 `allowed_files`，并采用全有或全无边界；
- 远程调用受计划哈希、授权快照、预算、隐私策略、文件范围、角色权限和网络策略共同约束。

默认开发和验证使用无费用测试替身、本地模型路径、mock 进程和 stub OpenCode executable。真实付费 provider 调用和真实远程 smoke test 需要单独计划、展示哈希、明确批准和授权。

## 核心保证

- **显式激活**：自动 Skill 发现可以建议 AgentFlow，但这不等于授权启动模型。
- **计划哈希授权**：授权绑定规范化计划 JSON 和 SHA-256 内容哈希。
- **范围变更失效**：改变模型、文件、预算、数据暴露、权限或副作用范围都需要重新授权。
- **双轴风险**：业务重要度使用 B0-B3；操作安全使用 S0-S3。
- **隐私门禁**：数据按 D0-D3 分类；D3 数据和密钥永不发送远程。
- **角色隔离**：实施、自测和审核不能合并成一个自我批准的模型路径。
- **独立审核**：Reviewer 使用全新只读上下文；重要或关键工作要求跨模型家族审核。
- **远程 Reviewer 隔离**：远程 Reviewer 只接收最小 Review Packet，不接收仓库工作区。
- **远程 Worker 沙箱**：远程 Worker 在最小 staging sandbox 中运行，网络默认拒绝。
- **输入快照**：远程 Worker 输入是项目相对文件，并通过 SHA-256 校验。
- **原子输出同步**：远程 Worker 输出只复制回授权文件，且只有同步检查通过才复制。
- **审核接受策略**：任务可以使用 `block_p0_p1` 或 `zero_findings`。
- **费用诚实记录**：调用记录 Token、耗时、费用、费用不可用状态和幂等键。
- **确定性状态**：事件、状态投影、调用、测试、审核和证据保存在同一个 SQLite 状态源中。
- **主管检查点**：确定性唤醒事件可以生成有界 supervisor checkpoint，用于低 Token 监督。
- **暂停与恢复**：运行可以安全暂停、冻结、接管和恢复，不重复已完成的付费调用。
- **可选控制平面**：AgentFlow 可以停止；Git worktree、diff 和证据仍可用于人工继续。

## 快速开始

运行测试：

```bash
pytest
```

查看当前计划：

```bash
agentflow plan show
```

授权已展示的计划哈希：

```bash
agentflow plan authorize --hash <sha256>
```

启动运行：

```bash
agentflow start <plan-id>
```

查看状态：

```bash
agentflow status <run-id>
```

跟随状态和日志：

```bash
agentflow status <run-id> --watch
agentflow logs <run-id> --follow
```

查看费用证据：

```bash
agentflow cost <run-id>
```

读取或记录主管检查点：

```bash
agentflow supervisor-next <run-id>
agentflow supervisor-record <run-id> <checkpoint-id> --decision '<json>'
```

暂停、冻结、接管、处置、恢复或取消：

```bash
agentflow pause <run-id>
agentflow pause <run-id> --immediate
agentflow takeover <run-id>
agentflow handoff <run-id>
agentflow resolve-call <run-id> <call-id>
agentflow resume <run-id>
agentflow cancel <run-id>
```

针对指定项目根目录运行：

```bash
agentflow --project <root> status <run-id>
```

## 典型流程

1. 创建或生成 `.agentflow/plan.json`。
2. 运行 `agentflow plan show`。
3. 检查规范化计划、有效范围、模型、文件、预算、隐私策略、运行模式、过期时间和 SHA-256。
4. 明确批准展示出的哈希。
5. 运行 `agentflow plan authorize --hash <sha256>`。
6. 运行 `agentflow start <plan-id>`。
7. 通过 `status`、`logs`、`cost` 和 `supervisor-next` 观察运行。
8. 需要人工介入时使用安全暂停、冻结、接管或恢复。
9. 只有确定性门禁和独立审核都通过后，才批准完成。

## 远程角色

### Reviewer

远程 Reviewer 只能执行 `review` 或 `rereview`。

它默认只读，不接收仓库工作区，只接收 AgentFlow 生成的最小 Review Packet。Review Packet 受隐私、预算、授权和独立性门禁约束。

远程 Reviewer 不得编辑文件、写入磁盘、运行 Shell、访问外部目录、浏览网页、调用 Skill、启动任务/子 Agent，或请求交互式权限升级。

Reviewer 输出必须是一个协议有效的 JSON 对象。非 JSON 散文、fenced JSON、缺字段、类型错误、非法严重度或矛盾批准都不能批准任务。

### Worker

远程 Worker 只能在任务合同显式设置以下字段时执行 `implementation` 或 `revision`：

```yaml
allow_remote_implementation: true
```

Worker 在最小 staging sandbox 中运行，其中只包含：

- 通过哈希校验的只读 `input_artifacts`；
- 授权的 `allowed_files`；
- 生成的任务简报。

网络访问默认拒绝。当前 OpenCode 权限层不能安全表达 host allowlist，因此 allowlist 模式失败关闭。

Worker 输出只同步回 `allowed_files`。同步使用 baseline 和 manifest 检查，并遵循全有或全无边界。输入被修改、输出越界、目标冲突、manifest 损坏或 rollback 失败都会阻断整合。

## 任务合同

任务合同在任何模型运行前描述执行边界：

```yaml
task_id:
objective:
risk_level:
allowed_files:
forbidden_actions:
acceptance_criteria:
data_sensitivity:
implementation_model:
review_model:
fallback_model:
max_remote_cost:
max_retry_count:
escalation_conditions:
expected_outputs:
implementation_max_steps:
implementation_timeout_seconds:
implementation_max_continuations:
allow_remote_implementation:
remote_worker_network_mode:
remote_worker_allowed_hosts:
remote_worker_max_steps:
remote_worker_timeout_seconds:
input_artifacts:
review_acceptance_policy:
```

`risk_level` 同时包含业务重要度和操作安全等级。任务合同是规范化计划 JSON 和授权哈希的一部分。

## 运行模式

- **Managed**：已批准计划可在授权范围内运行，无需额外确认。
- **Supervised**：每次 implementation、review、revision 和 rereview 调用都需要确认。
- **Adaptive**：确认点只由计划中的静态 B/S 阈值和关键节点标记决定。

Adaptive 模式不执行学习型路由，也不会静默改变策略。

## 状态与恢复

AgentFlow 将运行状态保存到本地 SQLite 数据库。事件和当前状态投影在同一事务中更新，因此恢复时可以区分已完成工作、失败工作、未知调用、待审核和人工介入状态。

如果付费或远程调用结果不确定，AgentFlow 会将其记录为 `UNKNOWN` 并暂停等待处置。它不得进行猜测性重试。

如果本地 implementation 或 revision 调用达到已知步骤上限，它会被记录为已知不完整失败。只有在授权的同会话 segment 内，并且仍处于 `implementation_max_continuations` 范围内时，才可以继续。远程 Reviewer 和远程 Worker 不使用这种续接方式。

## 文档地图

- `docs/requirements.md`：带稳定编号和验收证据的规范产品需求。
- `docs/mvp.md`：MVP 范围、非目标、验收场景和验证状态。
- `docs/architecture-decisions.md`：已接受、暂定和待确认的架构决策。
- `task_plan.md`：实现里程碑和当前工作计划。
- `progress.md`：进度记录和验证记录。
- `skills/multi-model-agentflow/SKILL.md`：Codex Skill 入口。
- `AGENTS.md`：仓库协作规则和安全边界。

## 安全边界

默认情况下，本项目不会：

- 下载或安装模型；
- 注册 provider 账号；
- 购买额度；
- 收集或保存 provider 密钥；
- 在计划授权外调用模型；
- 将 D3 数据、密钥、Token 或私密用户数据发送给远程模型；
- 将 OpenCode configured/discoverable 模型视为 callable-verified 模型；
- 允许远程 Reviewer 编辑文件、运行 Shell、浏览网页或读取仓库工作区；
- 允许远程 Worker 使用网络、Shell、外部目录、Skill、任务、子 Agent 或交互式升级；
- 用模型散文替代测试、审核记录、数据库证据或 Git diff；
- 用 AI 模型实现状态机、锁、预算检查、等待逻辑或确定性门禁；
- 在没有明确授权时产生 API 费用。

## 开发说明

通用核心必须保持独立于模型供应商、Agent Engine、平台和应用领域。领域特定策略应放在项目配置、项目文档或调用方提供的策略中。

新增需求应写入 `docs/requirements.md`，并带有稳定编号、可观察行为和验收证据。MVP 范围变化应反映到 `docs/mvp.md`。架构决策和开放问题应记录到 `docs/architecture-decisions.md`。

实现变更应保留测试、审核、费用、授权、状态和恢复证据。测试替身必须清晰标记，不能被描述成真实模型运行。

## 免责声明

本项目是用于研究和开发工作流的实验性软件，按现状提供，不提供任何形式的担保。

本项目不提供法律、财务、安全、合规或其他专业建议。用户在真实项目或真实模型 provider 中使用前，应自行审查计划、权限、模型输出、费用、隐私边界、provider 行为和下游影响。

真实远程 provider 调用、付费模型使用、敏感数据处理和生产部署都需要单独审查和明确授权。

## 许可证

本项目基于 Apache License 2.0 授权。详情见 `LICENSE`。
