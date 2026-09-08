# 第一版 MVP

状态：本地 MVP 已验收；OpenCode 远程只读 Reviewer 与远程实现/修订 Worker 扩展已获授权并进入实现（2026-09-04）

## 1. MVP 目标

用一个可完全停用的本地控制平面，证明一条三任务串行链能够在有效授权内完成“计划 → 实施 → 自测 → 确定性门禁 → 独立只读审核 → 修复/复审 → 批准”，并能在暂停、人工修改或进程重启后安全恢复。

MVP 的重点是验证授权、安全和恢复闭环，不是覆盖所有模型、并发形态或用户界面。

## 2. 范围内

### 2.1 单一纵向切片

- 一次只运行一个已授权执行计划。
- 一个计划包含 3 个串行任务；数据结构允许第 4 个任务，但验收不依赖它。
- 每个实施任务使用独立 Git worktree，并受 `allowed_files` 限制。
- 计划内未跟踪新文件同样进入范围、Review Packet、expected-output 和补充空白错误检查；`git diff --check` 只按 Git 本身语义报告。
- 实施模型完成工作与自测；确定性程序运行门禁；全新上下文的审核模型只读审核；重要或关键任务必须跨模型家族，修复后由原审核方复审。
- 阻断问题清零后才能批准，不能以模型自述代替证据。

### 2.2 入口与控制

- Terminal 提供计划查看、启动、状态观察、日志、费用、暂停、立即冻结、接管、恢复和取消能力。
- Codex Skill 作为第一版入口，负责识别适用场景、展示计划/建议并调用同一控制接口；自动发现 Skill 不会自动启动模型。
- 控制平面为本地普通 Python 程序、可选边车，不要求开机自启。
- `status --watch` 和等待机制只读取本地持久化状态，不调用模型。

### 2.3 模型与路由

- 通过可插拔接口调用模型；OpenCode → LM Studio 继续是本地实现路径。
- 提供无费用测试替身验证授权、预算、失败和收费幂等场景；新增 OpenCode remote-provider 通用 Reviewer 路径，但不增加供应商专属直连核心。
- 模型和版本来自注册表与运行时发现，不在 Skill 中写死。
- 最小路由支持用户直接指定角色模型，以及本地优先策略。
- 注册表实现 MVP 所需的可用性、版本、信任、成本/速度和最高风险字段；未接入候选只展示，不执行。
- 若没有满足独立审核要求的可用模型组合，任务进入等待更强审核，不能降级为“已批准”。

### 2.4 授权、风险、费用与隐私

- 显式激活后才能调度；授权绑定计划版本和内容哈希。
- 托管、监督和自适应三种模式都必须形成端到端执行闭环。自适应模式只按明确的 B/S 风险阈值和关键节点标记决定确认点，不做学习型路由或自动优化。
- 验证业务重要度与操作安全等级分离，且操作安全硬门禁不可被业务等级覆盖。
- 支持本地免费和固定预算的端到端执行；效率优先模式至少能被配置并仍受重试/熔断约束，完整自动优化不在 MVP。
- 实现 D0-D3 发送门禁、最小必要上下文、远程发送审计和密钥/敏感字段检查；D2 必须同时具备本次计划授权和通过自动脱敏检查。
- 预算耗尽时本地降级；无法达到门禁时停在等待更强审核。

### 2.5 状态、事件与恢复

- 单个 SQLite 数据库保存计划、任务、授权、调用、尝试、状态、测试、审核、费用和最终结论；追加 `events` 表与状态投影必须在同一事务中更新。
- 等待优先消费进程、文件或任务状态事件；无事件接口时由非 LLM 程序检查；最终后备才采用指数退避。
- 支持安全暂停、立即冻结、人工接管和恢复。一次外部进程或模型请求是安全暂停的最小不可中断单元；返回后在下一次调用前保存检查点。
- 重启恢复执行幂等检查：已完成任务和已完成收费调用不重复。
- 步骤上限耗尽作为带原因的已知失败，保留 Token、耗时和已确认费用；实施结果不进入自测/审核，Reviewer 结果不产生 review 记录，两者均安全暂停且不自动重试。无论进程退出码为 0 还是正数非零，只要 stdout 事件流包含严格步骤耗尽信号即按此处理；signal 终止或结果不可确认仍保持 `UNKNOWN`。
- 本地实施任务通过任务合同的 `implementation_max_steps` 字段携带有限步骤预算（缺省 8，允许 1–32，进入计划哈希与授权）。远程 Reviewer 继续使用独立的固定安全步骤上限，不因该字段放宽。
- 本地实施任务通过任务合同的 `implementation_timeout_seconds` 字段携带墙钟超时（缺省 900，允许 60–14400，进入计划哈希与授权）。超时后终止进程组并记为 `UNKNOWN`，保留部分输出中已确认的 Token、耗时与 OpenCode 会话 ID 等审计证据，`output_text` 保持空且不自动重试/自测/审核。
- 本地实施任务通过任务合同的 `implementation_max_continuations` 字段携带续接预算（缺省 0，允许 0–8，进入计划哈希与授权）。本地 implementation/revision 调用因步骤耗尽而已知失败时，仅在本地模型、同一模型与 worktree/文件范围、续接限额内、文件范围核验通过且非 `UNKNOWN`/超时/signal/费用未知的前提下，以同一 OpenCode 会话的新 segment 续接；每个 segment 拥有唯一 call_id/request_key/segment_index/continuation_of_call_id 并单独记录 Token/耗时/费用，续接决定以 `continuation.scheduled` 事件持久化。远程只读 Reviewer 永不续接；segment 间进程重启后 resume 只续接一次且不重复已完成 segment。多轮修订由 `max_retry_count` 驱动（impl→test→fix→retest）。
- 收费调用结果未知时先按请求 ID 查询；无法确认则进入 `UNKNOWN` 并暂停，不自动重试。
- 人工修改后基于 Git 差异和文件哈希，只失效受影响结果。

### 2.6 退出与人工降级

- 控制平面可关闭，不删除或锁死 Git worktree、差异、任务记录和审核证据。
- 生成人工接管摘要，使用户能够回到 OpenCode 或普通 Terminal 工作流继续。
- 项目仓库只跟踪非敏感策略；运行记录、调用内容和敏感数据默认不进入 Git。用户级数据路径遵循平台惯例并允许环境变量覆盖。

### 2.7 OpenCode 远程只读 Reviewer 扩展

- provider 与精确模型来自计划和授权快照，并在调用前通过 OpenCode 的无推理模型列表验证为已配置且可发现。
- 远程 provider 只允许 `review`/`rereview`，必须 `read_only=true`，并禁用编辑、Shell、外部目录、网页、任务/子 Agent、Skill 和交互升级。
- packet-only Reviewer 使用足以完成一次正常答复且仍有限的步骤预算；Reviewer prompt 在 packet 外强制只返回一个无 Markdown/散文包装的 JSON 对象。
- Reviewer 结果严格验证 `approved` boolean、`findings` array、P0-P3 严重度与必填字符串字段；非结构化输出和带 P0/P1 的 `approved=true` 均不能形成批准。
- Reviewer 不接收仓库工作区，只接收 AgentFlow 生成的最小 Review Packet；发送审计只持久化 SHA-256、大小、模型和隐私策略等元数据。
- D0-D3、密钥/路径扫描、可用预算、应急预留和跨家族独立性在进程启动前执行；当前没有完整自动 D2 脱敏，因此 D2 默认拒绝，不能伪造 `redaction_passed`。
- OpenCode 报告的远程费用按调用记录；未报告费用时记录 `cost_unavailable`，不伪造零。
- configured/discoverable 只表示接入候选，不等于 `callable_verified`。真实 smoke test 必须使用另一个明确计划和哈希批准，本次开发任务不执行。

### 2.8 本地 Ollama Reviewer

- 本地 Ollama（`provider='ollama'`、`is_local=True`）仅承担 `review`/`rereview`；`implementation`/`revision` 等写角色、`read_only=false` 或非回环端点在推理进程启动前确定性拒绝。
- 每次调用前重新读取并验证实际 OpenCode 有效配置（`opencode debug config --pure`），确认 `provider.ollama` 的 npm 为已验证 transport、`options.baseURL` 与所选模型条目端点均为严格回环；远程、冲突、非法或无法证明的端点失败关闭。
- 验证后的回环端点与全 deny 配置写入子进程 `OPENCODE_CONFIG_CONTENT`，并移除代理变量、设置 `NO_PROXY='*'`；仅影响本地 Reviewer 子进程。
- 本地 Ollama Reviewer 复用 packet-only 只读最小 prompt、全工具禁用、固定 steps=2 与 JSON-only 协议；费用为确认零远程费用。发现列表只产生 `discoverable`/`unavailable`，不提升为 `callable_verified`。

## 3. 明确不做

- 网页控制台、手机端或云端控制平面；
- 多机调度、复杂并行调度或组织级权限系统；
- 自动安装运行环境、自动下载/加载模型或建立完整市场模型目录；
- 自动注册供应商账号、购买/充值额度、申请/保存密钥或修改供应商配置；
- 绕过 OpenCode 的供应商专属远程直连适配器；
- 未经独立计划和哈希批准的真实远程 smoke test，或把 configured/discoverable 记为 callable_verified；
- 开机自动服务；
- 将金融规则写进通用核心；
- 用 AI 模型实现状态机、预算、锁、等待或确定性测试；
- Token 位置级别的生成恢复保证；

macOS 系统通知是可选增强，不得成为验收前置条件。复杂的质量优先/成本优先/均衡自动路由、跨项目自动信任升级和远程模型市场发现推迟到 MVP 后；MVP 只保留兼容这些能力的数据与接口边界。

## 4. 验收场景

所有场景使用临时测试仓库和无真实敏感数据的样例。收费调用可以用可判定的测试替身验证；若要使用真实收费模型，必须另行取得明确授权，不属于本轮工作。

| 编号 | 场景与操作 | 通过条件 | 对应需求 |
| --- | --- | --- | --- |
| MVP-A01 | 建立含 3 个串行任务的计划，完成授权并启动。 | 三任务按依赖顺序完成实施、自测、门禁、审核和复审；P0/P1 清零后计划获批。 | TASK-03, QA-01~06 |
| MVP-A02 | Skill 被识别为适用，但用户尚未激活。 | 只展示建议；模型调用和费用记录均为零。 | AUTH-01 |
| MVP-A03 | 尝试调用授权范围外或黑名单模型。 | 调用在适配器执行前被阻止，并留下拒绝事件。 | MODEL-01, AUTH-04~05 |
| MVP-A04 | 修改已授权计划的任务、文件、模型、预算、远程数据或副作用范围。 | 规范化 JSON 的 SHA-256 变化，旧授权失效；增量授权生成新完整快照，未重新授权不能继续。 | AUTH-04~07 |
| MVP-A05 | 在本地免费模式下请求收费 API；在固定预算模式下耗尽预算。 | 前者零远程收费调用；后者触发熔断并按策略本地降级或等待更强审核。 | COST-01~04 |
| MVP-A06 | 分别尝试发送 D0、D1、D2、D3，并注入密钥、账户字段和个人路径。 | D2 缺少本次授权或未通过脱敏时被阻止；D3 与密钥始终被阻止；允许发送时仅含最小片段且审计完整。 | PRIV-01~06 |
| MVP-A07 | 运行实施任务并尝试越界修改；让审核方尝试写文件。 | 实施在独立 worktree；事前权限和事后 Git 差异均参与门禁；越界写入与审核写入不能整合。 | GIT-01, GIT-05, QA-03 |
| MVP-A08 | 观察运行并保持一段时间无状态变化。 | 顶层 Agent 不被高频唤醒；`status --watch` 不增加模型调用；日志只读取增量。 | WAIT-01~04, CLI-02 |
| MVP-A09 | 执行中请求安全暂停。 | 不再派发任务；当前最小单元收尾；差异、测试、日志、费用和审核状态保存；写锁释放并生成接管摘要。 | HUMAN-01~03 |
| MVP-A10 | 执行中立即冻结。 | 已有文件和日志保留；系统不承诺从中断 Token 继续；状态明确。 | HUMAN-04 |
| MVP-A11 | 暂停后人工只修改一个任务涉及的文件，再恢复。 | 比较差异/哈希，只使受影响任务及其依赖结果失效，展示剩余计划并按授权规则继续。 | HUMAN-05, AUTH-05 |
| MVP-A12 | 在任务完成及模拟收费调用完成后终止控制进程并重启。 | 已完成工作和调用不重复；从最近检查点恢复；预算与授权仍生效。 | STATE-01~03 |
| MVP-A13 | 关闭控制平面并人工接管。 | Git 历史、worktree、差异、任务/测试/审核/费用记录可读，能够人工继续。 | PROD-06, CTRL-03 |
| MVP-A14 | 对全部运行记录做完整性检查。 | 计划、授权哈希、唯一编号、状态、测试、调用指标、费用、审核和最终结论均可追溯。 | MODEL-09, WAIT-04, STORE-03 |
| MVP-A15 | 用同一计划分别运行托管、监督和自适应模式。 | 托管只使用计划级授权；监督在每次模型启动前确认；自适应只按预先定义的 B/S 阈值和关键节点确认，三者都能完成闭环。 | AUTH-02~03, AUTH-08, RISK-01~05 |
| MVP-A16 | 模拟收费请求已经发出但响应丢失。 | 系统先按请求 ID 查询；无法确认时进入 `UNKNOWN` 并暂停，模型调用和费用记录中没有自动重试。 | COST-05, STATE-03 |
| MVP-A17 | 在追加事件与更新状态投影之间模拟进程故障。 | 单一 SQLite 事务全部提交或全部回滚；恢复后事件与当前状态一致。 | STATE-01, STATE-04, STORE-03 |
| MVP-A18 | 分别审核普通任务和重要/关键任务。 | 普通任务可以同家族但必须全新只读上下文且不含实施解释；重要/关键任务必须跨家族，否则等待更强审核。 | QA-03 |
| MVP-A19 | 以 stub OpenCode executable 执行远程 Reviewer。 | 参数使用数组并准确形成 `<provider>/<model-id>`；角色、只读权限、provider 配置与模型发现均在调用前验证。 | PLAT-05, MODEL-13, QA-07 |
| MVP-A20 | 对 D0-D3 Review Packet、敏感字段、预算和独立性组合执行远程门禁。 | 只有计划/授权/隐私/预算/跨家族全部满足时进入替身；D2 未通过真实脱敏标志和 D3 始终拒绝。 | AUTH-09, PRIV-02, PRIV-07, QA-03 |
| MVP-A21 | 解析有费用、无费用字段和结果未知的 OpenCode 替身响应。 | 已报告费用累计，缺失费用计为 `cost_unavailable`，UNKNOWN 不重试且恢复前必须处置。 | COST-05~06, STATE-03, STATE-05 |
| MVP-A22 | 运行 canonical Skill 与 installed Skill 校验和哈希比对。 | 两者通过 quick_validate、逐文件内容一致，并保留 plan show → hash approval → authorize → start。 | AUTH-01, AUTH-09, PLAT-02 |
| MVP-A23 | 用退出码为 0 或正数非零的 OpenCode 事件流分别模拟 implementation 和 review 步骤耗尽。 | 调用为带 `step_limit_reached` 原因的已知失败；implementation 不进入测试/审核，review 不生成 review row，两者保留使用/费用证据并暂停且不自动重试。 | QA-08, STATE-03, STATE-05 |
| MVP-A24 | 对 Reviewer 返回纯 JSON、fenced JSON、散文包装、缺字段、错类型、非法 severity 与矛盾批准。 | 只有合规 JSON 生成 review row；非合规输出暂停，P0/P1 始终阻断批准。 | QA-06~09 |
| MVP-A25 | 实施产生计划内未跟踪新文件，包括带空白错误的样例。 | 新文件进入文件范围、Review Packet 和存在性检查；补充检查可在 `git diff --check` 单独退出 0 时捕获未跟踪空白错误。 | GIT-05~06 |
| MVP-A26 | 本地实施调用超过任务合同配置的超时上限。 | 进程组被终止，调用记为 `UNKNOWN` 并暂停；数据库保存已确认 Token、耗时、会话 ID 与 `termination_reason=timeout`/`token_source`/`usage_unavailable`，`output_text` 为空，resume 被阻止且不自动重试；重叠或缺失的部分输出不重复累计 Token、不伪造零费用。 | TASK-05, QA-10, STATE-03, STATE-05 |
| MVP-A27 | 本地实施调用反复步骤耗尽，且续接预算配置为 0、1 或 2。 | 预算为 0 时安全暂停且 resume 被阻止；预算 ≥1 时以 `--session` 在同一 OpenCode 会话内续接并最终完成，每个 segment 的 call/request_key/segment_index/continuation_of_call_id 唯一且单独记录 Token/耗时/费用；到达限额仍耗尽时暂停且 resume 不重复调用；远程 Reviewer、`UNKNOWN`、超时、signal、越界文件、缺失会话 ID 的场景不续接。 | TASK-06, QA-11, STATE-03, STATE-05 |
| MVP-A28 | 以 stub OpenCode executable 执行远程 implementation/revision Worker。 | 参数使用数组并准确形成 `<provider>/<model-id>` 与授权 worktree `--dir`；角色、只读/写权限、provider/model 发现与网络禁用均在调用前验证；`bash`/`shell`/`external_directory`/`webfetch`/`websearch`/`task`/`subagent`/`skill`/`question` 全部拒绝。 | TASK-08, QA-07 |
| MVP-A29 | 远程实施任务声明 `input_artifacts`，worktree 输入文件与哈希一致或不一致。 | 一致时 Worker 正常执行；任一文件缺失或哈希不一致时任务以 `input_artifact_mismatch` 失败，不进入模型调用。 | TASK-07 |
| MVP-A30 | 同一审核输出在 `block_p0_p1` 与 `zero_findings` 两种 `review_acceptance_policy` 下分别评估。 | P2/P3 finding 在 `zero_findings` 下阻断批准，在 `block_p0_p1` 下不阻断；P0/P1 始终阻断。 | QA-12 |
| MVP-A31 | 配置与未配置 `supervisor_policy` 唤醒事件，运行产生 P0/P1 finding 的任务。 | 配置时产生有界 `supervisor_checkpoints`，`supervisor-next` 读取、`supervisor-record` 确认、`supervisor_digest` 给出摘要且不调用模型；无论是否配置，一组强制唤醒事件（P0/P1 finding、UNKNOWN、超时/步骤/续接耗尽、会话不一致、范围/隐私/授权/网络违规、Reviewer 不可用/协议错误、费用未知、预算达限、终局等）始终被记录，空或窄 `wake_events` 只能额外增加可选唤醒原因，不能静默强制唤醒。 | CTRL-04, WAIT-02 |
| MVP-A32 | 本地 Ollama 经 OpenCode 承担 `review`/`rereview`：验证回环端点、发现、角色与只读边界、packet/进程隔离、两层权限与费用。 | 回环/非法端点严格分类且不触发 DNS；远程/冲突/未知 transport 端点失败关闭且推理 `Popen`=0；无计划发现的 family 为 `None`；写角色与 `read_only=false` 在配置发现前拒绝；cwd/`--dir` 为临时只读目录；两层 `*` 及 read/glob/grep/edit/write/bash/shell/external_directory/webfetch/websearch/task/subagent/skill/question 均 deny、steps=2、仅 ollama；本地费用为确认零。 | PLAT-06, MODEL-14, MODEL-13, AUTH-04/05/07/09, COST-06, QA-03/08/09/12, STATE-05 |
| MVP-A33 | 本地 Ollama 调用的失败、协议错误、UNKNOWN、恢复、独立性与授权边界。 | 正数非零退出/无可用结果为协议错误并保留 usage 与 `termination_reason`，不 fallback；超时/signal/KeyboardInterrupt 为 `UNKNOWN` 且保留已确认证据、resume 不重发；明确步骤耗尽为已知失败且不生成 review row；写角色 fallback 到 Ollama 时 LocalOllamaReviewerAdapter 从未被调用且暂停原因可追溯；provider/model_id/version/family/is_local 变化改变 plan_hash 并使旧授权失效。 | QA-08/09/12, STATE-03/05, AUTH-04/05/07/09 |

## 5. MVP 完成定义

同时满足以下条件才算 MVP 通过：

1. MVP-A01 至 MVP-A33 全部通过，且每项有非模型自述的可核查证据。
2. 没有未解决的 P0/P1 审核问题。
3. 通用核心没有硬编码候选模型或金融规则。
4. 未包含“明确不做”列表中的能力作为隐含依赖。
5. Skill 与 Terminal 观察到的是同一计划、授权和状态源。
6. 关闭控制平面后能够人工接管，不损失已有产物。

## 6. 实现门槛（已满足）

- AD-17 技术基线和 AD-21 正交状态表示已完成最小原型验证；如需偏离现有选择，先回到架构决策流程。
- 任务合同、授权哈希、状态机和幂等调用的最小数据模型完成设计评审。
- 验收样例的数据分类、允许文件与禁止副作用预先固定。
- 测试模型/替身方案保证开发验收不会未经授权产生 API 费用。
- Codex Skill 使用 `multi-model-agentflow`，CLI 使用 `agentflow`，所有示例与接口保持一致。

## 7. 验收结果

MVP-A01 至 MVP-A31 均已通过（历史记录）。MVP-A32 与 MVP-A33 为本次新增的本地 Ollama Reviewer 验收，已完成确定性替身验收，并于 2026-09-08 获 Codex 独立复核批准；证据见 `verification/2026-09-08-ollama-reviewer-codex-approval.md`。当前自动化证据由标准库测试提供；远程扩展只使用明确标记的 FakeAdapter、mock 进程和 stub OpenCode executable，未执行真实 provider smoke test，远程费用为 0。历史本地适配器验证保持不变。

| 场景 | 结果 | 主要证据 |
| --- | --- | --- |
| MVP-A01 | 通过 | 三任务串行、修复与复审测试 |
| MVP-A02 | 通过 | Skill 隐式触发边界与无固定模型测试 |
| MVP-A03 | 通过 | 越权/黑名单模型在适配器前拒绝并写入事件 |
| MVP-A04 | 通过 | 计划哈希稳定性、变化失效与 24 小时授权测试 |
| MVP-A05 | 通过 | 本地免费拒绝远程、固定预算熔断及本地后备测试 |
| MVP-A06 | 通过 | D0-D3、D1/D2 授权、脱敏与密钥/路径扫描测试 |
| MVP-A07 | 通过 | 独立 worktree、越界文件与审核写入阻断测试 |
| MVP-A08 | 通过 | `status --watch`/增量日志前后模型调用数不变 |
| MVP-A09 | 通过 | 安全暂停、检查点、恢复与接管摘要测试 |
| MVP-A10 | 通过 | 立即冻结状态及本地进程组终止测试 |
| MVP-A11 | 通过 | 人工修改后的文件哈希与依赖最小失效测试 |
| MVP-A12 | 通过 | 完成调用后模拟进程退出、重开数据库并无重复恢复 |
| MVP-A13 | 通过 | 控制平面关闭后重开数据库，worktree 与摘要仍可读 |
| MVP-A14 | 通过 | 计划/授权/调用/Token/耗时/测试/审核/费用完整性测试 |
| MVP-A15 | 通过 | 托管、监督、自适应及关键节点确定性确认测试 |
| MVP-A16 | 通过 | 未知响应禁止重试、按请求 ID 查询并复用结果测试 |
| MVP-A17 | 通过 | 状态投影与事件同事务回滚故障注入测试 |
| MVP-A18 | 通过 | 普通/重要任务审核独立性与等待更强审核测试 |
| MVP-A19 | 通过 | OpenCode 参数数组、provider/model 发现、角色与只读权限替身测试 |
| MVP-A20 | 通过 | D0-D3、敏感扫描、预算、授权、跨 family 与 fallback 门禁测试 |
| MVP-A21 | 通过 | reported cost、`cost_unavailable`、UNKNOWN、恢复与汇总测试 |
| MVP-A22 | 通过 | canonical/installed `quick_validate` 与逐文件 SHA-256/内容比对 |
| MVP-A23 | 通过 | 近真实 JSONL fixture、结构化/文本终止标记、implementation/review 暂停及费用/恢复测试 |
| MVP-A24 | 通过 | JSON-only prompt、严格字段/类型/severity 解析、非合规暂停与 P0/P1 阻断测试 |
| MVP-A25 | 通过 | 未跟踪文件范围、Review Packet、expected output 与补充空白错误测试 |
| MVP-A26 | 通过 | 本地实施超时的进程组终止、`UNKNOWN` 持久化（Token/耗时/会话/termination_reason/token_source/usage_unavailable）、空 `output_text`、resume 阻止与重叠输出不重复计数测试 |
| MVP-A27 | 通过 | 本地实施步骤耗尽的同会话分段续接、限额暂停、session 复用、越界/缺失会话/远程/UNKNOWN 不续接、segment 间进程重启只续接一次与多轮修订测试 |
| MVP-A28 | 通过 | 远程 Worker 参数数组、`<provider>/<model-id>` 与 `--dir`、写权限 + 全网络/子代理禁用、角色/只读在发现前拒绝、步骤耗尽与超时分类测试 |
| MVP-A29 | 通过 | 显式输入快照哈希一致放行、缺失或哈希不一致以 `input_artifact_mismatch` 失败且不进入模型调用测试 |
| MVP-A30 | 通过 | `block_p0_p1` 与 `zero_findings` 两种策略下 P2/P3 阻断差异与 P0/P1 始终阻断测试 |
| MVP-A31 | 通过 | `supervisor_checkpoints` 记录/读取/确认/摘要、内容截断、空/窄 `wake_events` 仍记录强制唤醒与 CLI `supervisor-next`/`supervisor-record` 测试 |
| MVP-A32 | 确定性替身验收及 Codex 独立复核通过 | 回环/非法端点严格分类（无 DNS）、远程/冲突/未知 transport 失败关闭且 `Popen`=0、无计划 family=None、写角色/非只读前置拒绝、临时只读 cwd/`--dir`、两层全 deny + steps=2 + 仅 ollama、本地费用确认零 |
| MVP-A33 | 确定性替身验收及 Codex 独立复核通过 | 非零退出/无结果协议错误保留 usage 且不 fallback、超时/signal 为 UNKNOWN 且 resume 不重发、步骤耗尽无 review row、写角色 fallback 到 Ollama 不调用本地 Reviewer 且暂停原因可追溯、计划字段变化使 plan_hash 失效；混合有效/无效使用量保留确认部分但标记不完整 |
