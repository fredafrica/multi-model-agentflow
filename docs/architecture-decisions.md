# 架构决策与待确认事项

状态：MVP 实现后的决策记录
规则：`已接受` 表示来自用户已确认要求；`暂定` 表示可用于设计但可在实现前调整；`待确认` 不得擅自定案。

## 1. 已接受决策

### AD-01：通用核心与项目策略分离

- 状态：已接受
- 决策：通用核心管理任务合同、路由、状态、审核、费用、隐私、恢复和统计；领域规则通过项目配置或项目 `AGENTS.md` 提供。
- 影响：股票投研可作为首个试验，但不能向核心引入金融专属字段或判断。

### AD-02：平台无关内核与可插拔适配器

- 状态：已接受
- 决策：核心不绑定模型、供应商、Agent Engine 或本地服务；Codex Skill 为首个入口，OpenCode、LM Studio、DeepSeek API 等通过边界适配。
- 影响：候选模型是运行数据；新模型/版本单独注册和建立信任。

### AD-03：确定性、可选的本地控制平面

- 状态：已接受
- 决策：状态机、队列、预算、锁、检查点、重试、事件和通知由普通程序负责；模型只参与需要生成或判断的节点。控制平面是可停用边车。
- 影响：所有结果必须以开放的本地状态和 Git 产物保存，不能锁在聊天或某一前端中。

### AD-04：共享状态源

- 状态：已接受
- 决策：Codex Skill 与 Terminal 读取和操作同一套计划、授权与运行状态，不建立 Skill 私有状态机。
- 影响：自动发现 Skill 只产生建议；显式激活和授权由共享控制边界验证。

### AD-05：SQLite 当前状态 + 追加式事件日志

- 状态：已接受
- 决策：同一个 SQLite 数据库同时保存追加式 `events` 表和可查询的当前状态表，并在同一事务中更新；所有计划、任务、调用和尝试有唯一编号。
- 影响：MVP 只有一个权威写入源，避免数据库与独立日志文件双写不一致；以后可以从事件表导出 JSONL 供外部审计。

### AD-06：授权绑定计划内容

- 状态：已接受
- 决策：授权绑定带 schema 版本的规范化 JSON 计划，并使用 SHA-256 计算内容哈希；包含任务、模型、文件、预算、隐私、模式、重试、停止/升级条件和有效期。增量授权必须生成新的完整有效授权快照，不累积补丁。
- 影响：执行前和恢复时都要重新验证哈希。授权在计划变化、运行结束或 24 小时后失效，以最先发生者为准。

### AD-07：双维风险模型

- 状态：已接受
- 决策：业务重要度与操作安全等级正交保存，分别使用 `B0`~`B3` 与 `S0`~`S3`。用户决定业务等级，系统建议并记录分歧；系统根据数据、权限和行为决定安全等级，构成不可被降级的硬门禁。
- 影响：风险升级若未越过有效授权，可加强模型/测试/审核继续；越权则暂停。

### AD-08：分级隐私与最小远程披露

- 状态：已接受
- 决策：采用 D0-D3 分类。D2 远程发送必须同时具备本次计划的明确授权并通过自动脱敏检查；D3 永不远程发送。源代码远程处理要求项目授权、自动脱敏和最小差异；所有远程发送可审计。
- 影响：密钥、Token、个人路径及可组合隐私必须在适配器调用前检查，密钥只来自环境变量或本机密钥管理器。

### AD-09：预算是执行门禁

- 状态：已接受
- 决策：支持本地免费、固定预算、效率优先。即使效率优先也受重试、去重、异常提醒和熔断约束。
- 影响：预算耗尽默认本地降级；质量不足时停在等待更强审核，不能虚假批准。

### AD-10：实施与审核职责隔离

- 状态：已接受
- 决策：审核使用独立上下文且默认只读。普通任务可以使用同一家族，但审核上下文不得接收实施方解释；重要或关键任务必须使用不同模型家族。同一模型不得包办实施到批准的全部闭环。
- 影响：重要任务的验收与边界测试需在实施前由独立方参与；没有合格审核组合时进入等待更强审核，不得批准。

### AD-11：Git 隔离并按冲突串行化

- 状态：已接受
- 决策：每项实施工作使用独立分支或 worktree；文件/接口重叠或有依赖的任务串行，无重叠且无依赖的任务才可并行；冲突不自动覆盖。
- 影响：MVP 为降低变量只运行串行任务，仍验证 worktree 与文件白名单。

### AD-12：事件优先等待

- 状态：已接受
- 决策：优先事件通知，其次非 LLM 状态检查，最后才用历史估时与指数退避。只在完成、失败、需授权、无进展或超时唤醒顶层 Agent。
- 影响：状态观察和日志跟随不能产生模型调用，且只读取增量。

### AD-13：检查点恢复与幂等调用

- 状态：已接受
- 决策：重启后检查原任务，沿最近检查点恢复，不重复已完成任务或已完成收费调用，并继续遵守原授权和预算。收费请求发出但结果未知时，先使用供应商请求 ID 或幂等键查询；无法确认则标为 `UNKNOWN` 并暂停，绝不自动重试。
- 影响：预算预留不能视为重复扣费授权；每次外部调用都需要稳定请求标识和明确终态。

### AD-14：三级人工介入

- 状态：已接受
- 决策：支持只观察、安全暂停和立即冻结。安全暂停的最小不可中断单元是一次外部进程或模型请求；它结束后、下一次工具或模型调用前完整落盘并释放写锁。超过暂停超时后由用户选择继续等待或立即冻结；立即冻结不保证 Token 级续传。
- 影响：人工修改后以 Git 差异与文件哈希进行最小失效分析。

### AD-15：分层存储

- 状态：已接受
- 决策：用户层保存模型与脱敏跨项目偏好/表现，项目层保存风险、隐私、模型、预算、Git 与审核策略，运行层保存计划到最终结论的完整证据。项目仓库只跟踪 `.agentflow/project.toml` 等非敏感策略，运行记录和敏感数据默认不进入 Git；用户级数据目录可由环境变量覆盖，不能写死 macOS 路径。
- 影响：原始任务和敏感内容只留在所属项目；全局统计不得保存项目原文；具体默认目录遵守平台惯例。

### AD-16：Codex Skill 采用渐进披露

- 状态：已接受
- 决策：未来 Skill 的 `SKILL.md` 只包含准确的适用描述、必要授权边界和短入口工作流。按需将稳定且较长的模式说明放入 `references/`，将可重复确定性封装放入 `scripts/`；`agents/openai.yaml` 只承载必要界面元数据与调用策略。
- 影响：默认可让 Codex 自动发现该 Skill 以提出建议，但任何模型执行仍需明确激活。Skill 不复制控制平面，也不硬编码当前候选模型。

### AD-18：三种运行模式均形成 MVP 闭环

- 状态：已接受
- 决策：托管、监督和自适应三种模式均进入 MVP 端到端执行。自适应模式只使用显式风险阈值和节点标记决定确认点，不在 MVP 中引入学习型路由或自动优化。
- 影响：三种模式共享同一授权状态机；自适应模式保持可预测、可测试。

### AD-19：项目、Skill 与 CLI 命名

- 状态：已接受
- 决策：项目与 Codex Skill 使用 `multi-model-agentflow`，Terminal 命令使用 `agentflow`。
- 影响：后续外部接口以此命名，若需改名必须新增决策并说明迁移影响。

### AD-20：MVP 模型接入最小集合

- 状态：已接受
- 决策：OpenCode → LM Studio 是 MVP 唯一必须真实验证的模型调用路径；另提供无费用测试替身验证授权、预算、错误和收费幂等场景。DeepSeek 等远程真实适配器推迟到 MVP 后。
- 影响：适配器合同必须保持供应商无关，但 MVP 验收不需要产生远程 API 费用。

### AD-22：D2 双条件放行

- 状态：已接受
- 决策：D2 只有在本次计划明确授权且通过自动脱敏检查时才能发送远程模型；任一条件缺失都必须留在本地。

### AD-23：审核独立性下限

- 状态：已接受
- 决策：普通任务允许同一家族在全新、只读、无实施解释的上下文中审核；重要或关键任务必须跨模型家族。缺少合格组合时不得最终批准。

### AD-24：单库事务事件模型

- 状态：已接受
- 决策：MVP 在单个 SQLite 事务中追加事件并更新状态投影，不维护独立追加文件作为第二写入源；JSONL 仅作为后续可再生成的导出格式。

### AD-25：风险枚举

- 状态：已接受
- 决策：业务重要度使用 `B0` 琐碎、`B1` 普通、`B2` 重要、`B3` 关键；操作安全使用 `S0` 本地只读且无敏感数据、`S1` 可逆本地修改或授权的 D0 远程调用、`S2` D1/D2 远程处理或有边界的外部写入、`S3` 高影响或不可逆操作。D3 远程发送不因任何 S 等级而放行。
- 影响：两个等级分别参与门禁，不合并成会掩盖高安全风险的总分。

### AD-26：授权哈希规范

- 状态：已接受
- 决策：使用带 schema 版本、稳定键排序且排除易变运行字段的规范化 JSON，经 SHA-256 生成计划哈希；增量授权生成新的完整快照。MVP 保存本机用户确认记录，不引入密码学签名系统。

### AD-27：未知收费调用暂停

- 状态：已接受
- 决策：收费调用结果未知时先按请求 ID 查询，无法确认就进入 `UNKNOWN` 并暂停人工处理，不自动重试。

### AD-28：配置与运行数据分离

- 状态：已接受
- 决策：项目仓库只跟踪非敏感策略；运行记录、调用内容和敏感数据默认不进入 Git。用户级配置和数据使用跨平台惯例路径，并允许环境变量覆盖，不写死 macOS 路径。

### AD-29：双层文件边界

- 状态：已接受
- 决策：尽可能通过工具权限或 OS 沙箱事前限制文件范围，并始终使用 Git 差异进行事后核验；任一层发现越界都阻断整合。

### AD-30：安全暂停原子边界

- 状态：已接受
- 决策：一次外部进程或模型请求是 MVP 的最小不可中断单元；返回后必须在下一次工具或模型调用前落检查点。暂停超时不会自动升级为冻结。

### AD-31：问题严重度

- 状态：已接受
- 决策：P0 包括越权、隐私泄露、数据破坏、重复收费和关键结论错误；P1 包括未满足验收标准、明显回归和恢复失败。P0/P1 均阻断批准；P2/P3 记录为非阻断改进。

### AD-32：模型信任升级门槛

- 状态：已接受
- 决策：统计按供应商、精确模型版本和任务类型隔离。至少完成 20 个经独立审核的任务后才可以提示提升信任；系统只能建议，最终由用户确认。

### AD-33：macOS 通知延后

- 状态：已接受
- 决策：macOS 通知不属于 MVP 验收条件，在核心闭环完成后再作为增强实现；只通知完成、失败、需要授权和暂停超时。

### AD-34：OpenCode 远程只读 Reviewer

- 状态：已接受（Owner 于 2026-09-04 明确授权）
- 决策：在保持模型无关核心的前提下，AgentFlow 可以通过 OpenCode 调用计划和授权快照指定的远程 provider/model；第一版远程角色仅限 `review`/`rereview`。本地主模型继续负责 implementation/revision，Codex 继续担任 Supervisor。
- 安全边界：远程 Reviewer 强制只读、无仓库工作区、无编辑/Shell/外部目录/网页/任务/子 Agent/Skill/交互工具，只接收经过隐私与预算门禁的最小 Review Packet。重要或关键任务必须与实施模型跨 family；不可用时停在 `waiting_review`，不得降级为自审。
- 发现与费用：OpenCode 无推理模型列表只产生 configured/discoverable 或 callable_unverified 证据，不能产生 callable_verified；真实 smoke test 需要独立计划与哈希批准。远程费用使用 provider 报告值，缺失时记录 `cost_unavailable`，不伪造零。
- 影响：本决定仅覆盖 AD-20 和原 MVP “不实现远程真实适配器”的范围限制，不放宽既有授权、D0-D3、预算、UNKNOWN、幂等、费用、文件或副作用规则，也不授权本次开发任务进行真实模型调用。

### AD-35：已知不完整调用的终态

- 状态：已接受（Owner 于 2026-09-04 明确要求）
- 决策：步骤上限耗尽等已确认不完整结果使用现有非成功 `failed` 调用终态，并在原始元数据中保存稳定 `failure_kind`；它不是 `completed`，也不是结果不确定的 `UNKNOWN`。适配器优先检查结构化终止事件，无专用字段时才保守匹配最终文本的规范标记。
- 影响：失败记录保留已确认 Token、耗时、费用和部分输出，Runner 在 implementation/review 各自边界使用不同暂停原因，恢复不得猜测性重试可能产生费用的调用。非零退出时先解析 stdout 的严格步骤耗尽信号再决定错误分类：正数非零退出且存在严格信号时进入已知不完整路径，否则保持普通执行错误；signal 终止或结果不可确认仍保持 `UNKNOWN`。

### AD-36：Reviewer 严格 JSON-only 协议

- 状态：已接受（Owner 于 2026-09-04 明确要求）
- 决策：Reviewer prompt 在最小 Review Packet 之外声明唯一输出协议，核心只接受单一 JSON 对象并严格验证 `approved`、`findings`、P0-P3 与 finding 字段类型。不从 Markdown fence 或散文中提取结论，P0/P1 总是阻断批准。
- 影响：非合规回答使运行停在 review 边界并记录 `reviewer_output_invalid`，不生成正式 review row。

### AD-37：未跟踪输出的补充证据

- 状态：已接受（Owner 于 2026-09-04 明确要求）
- 决策：文件范围和 Reviewer diff 合并 tracked diff 与未跟踪文件，expected output 另行检查存在性。因 `git diff --check` 单独不完整覆盖未跟踪内容，AgentFlow 对未跟踪文本附加明确空白错误检查并单独记录证据。

### AD-38：实现步骤预算字段

- 状态：已接受（Owner 于 2026-09-04 明确要求）
- 决策：任务合同新增 `implementation_max_steps` 字段，为有限正整数并设置 1–32 硬上限；旧计划未提供时默认 8，保持向后兼容。字段进入 canonical 计划 JSON 与 SHA-256，改变字段使旧授权失效；Runner 将授权值写入本地 implementation 请求元数据，OpenCodeAdapter 从元数据读取并验证后写入 `agentflow-sandbox` 的 `steps`。远程 Reviewer 当时使用独立固定上限；此运行规则已由 AD-46 替代。
- 影响：模型 context 与步骤预算是两个独立参数；本次不修改任何模型的 context。不得从环境变量、未授权 project config 或模型输出覆盖该值，也不得自动选择无限步骤。

### AD-39：本地实施调用超时

- 状态：已接受（Owner 于 2026-09-04 明确要求）
- 决策：任务合同新增 `implementation_timeout_seconds` 字段（默认 900，范围 60–14400），进入 canonical 计划 JSON 与 SHA-256，改变字段使旧授权失效；布尔、字符串、浮点数及越界值被拒绝。Runner 将授权值写入本地 implementation 请求元数据，OpenCodeAdapter 从元数据读取并验证后作为 `communicate(timeout=...)` 的墙钟超时。超时后终止进程组、收集部分 stdout，保守解析已确认 Token/会话 ID，并以 `InvocationOutcomeUnknown(result=...)` 返回；服务层调用 `mark_call_unknown` 把 `UNKNOWN` 终态与 Token/耗时/会话/`termination_reason=timeout`/`token_source`/`usage_unavailable` 一起持久化，`output_text` 保持空以明确未确认语义，部分 stdout 只以字节数与 SHA-256 记录。
- 影响：超时是 `UNKNOWN`，不是已知不完整 `failed`，也不自动重试/自测/审核；本地 `remote_cost=0.0` 且 `cost_unavailable=false`。远程 Reviewer 继续使用独立的 `timeout_seconds`，不得意外复用 `implementation_timeout_seconds`。`TimeoutExpired.output` 在真实运行时可能是 `bytes` 而后续 `communicate()` 返回 `str`，必须先在原始字节层统一并消除两段输出的重叠（按重叠前缀合并，UTF-8 多字节截断也安全），合并完成后只解码一次，再按每个真实出现的已完成 step 累加 Token；不得根据事件内容推断重复，因此两个内容完全相同但独立发生的 step 仍各计一次。无可用 usage 时诚实记录 `usage_unavailable`，不得把字段缺失伪造为确认零使用。

### AD-40：本地实施步骤耗尽的同会话分段续接

- 状态：已接受（Owner 于 2026-09-05 明确要求）
- 决策：本地 implementation/revision 角色因步骤上限耗尽而已知失败时，在全部安全条件满足的前提下，可在同一 OpenCode 会话内以新 segment 续接，而非重启一个全新会话或重试。每个 segment 拥有唯一 `call_id`、幂等 `request_key`（`...:segment:{index}`）、`segment_index` 与 `continuation_of_call_id`，并单独记录 Token、耗时与费用；模型调用表 provider-request 唯一索引改为 `(provider, provider_request_id, segment_index)`，使复用同一会话 ID 的多个 segment 不冲突。续接决定以 `continuation.scheduled` 事件持久化，segment 间进程重启后 resume 只续接一次且不重复已完成 segment。
- 续接门禁：仅本地模型、implementation/revision 角色、完全相同的模型与 worktree/文件范围、`implementation_max_continuations` 限额内、文件范围核验通过、无暂停/取消/接管，且原调用不是 `UNKNOWN`/超时/signal/费用未知时允许。任一门禁不满足即保持安全暂停，不得续接或重试。远程只读 `review`/`rereview` 永不续接。
- 影响：续接不是“自动重试”，而是同一会话内延续未完成工作；QA-08 的“不得自动重试”语义不变。多轮修订由 `max_retry_count` 驱动（impl→test→fix→retest），修订提示词携带返回码、stdout/stderr 尾部、缺失输出、未跟踪空白错误、changed files 与剩余验收条件的有界证据；修订角色同样在限额内续接。
- 实现硬化（P1）：续接子调用必须确定性复用产生父 segment 的实际模型（含回退模型），而非默认 implementation_model；OpenCode 返回的会话 ID 必须与请求复用的 `--session` 完全一致，不一致视为协议错误，记录 `failure_kind=session_mismatch` 并安全暂停，绝不续接测试或审核。`continuation.scheduled` 事件与子调用登记在同一事务内原子写入；进程在“登记后、启动前”崩溃时遗留的 `PLANNED` 子调用按幂等 `request_key` 复用启动，而非死锁或重复登记。每个续接 segment 之前重新检查控制状态，非 `RUNNING` 时在安全边界暂停。

### AD-41：远程 implementation/revision Worker

- 状态：已接受（Owner 于 2026-09-05 明确要求）
- 决策：在计划与授权快照明确允许时，AgentFlow 可以通过 OpenCode 调用计划指定的远程 provider/model 承担 `implementation`/`revision` 角色，由独立的 `RemoteOpenCodeWorkerAdapter` 执行；该角色仅在任务合同的 `allow_remote_implementation=true` 且 `remote_worker_network_mode` 可安全执行时放行。远程 Worker 拥有受限写权限（在授权 worktree 内编辑/写入），但 `bash`/`shell`/`external_directory`/`webfetch`/`websearch`/`task`/`subagent`/`skill`/`question` 全部禁用。
- 网络门禁：OpenCode 权限层无法表达按主机白名单的 `webfetch`/`websearch`，因此 Worker 的权限配置始终拒绝网络，`ALLOWLIST` 模式在调用门禁处失败关闭（fail-closed），`DENY` 是唯一安全模式；纯函数 `network_decision` 单独承载白名单语义并独立测试。
- 步骤预算与超时：远程 Worker 使用任务合同的 `remote_worker_max_steps`（缺省 32，1–128）与 `remote_worker_timeout_seconds`（缺省 900，60–14400），进入计划哈希与授权。远程 Worker 因步骤耗尽而失败时安全暂停，绝不续接（同会话续接仅限本地模型）；超时或结果不可确认保持 `UNKNOWN`。
- 影响：该决定覆盖 AD-34 的“第一版远程仅 review/rereview”范围限制（Owner 明确扩大），但不放宽授权、D0-D3、预算、UNKNOWN、幂等、费用、文件或副作用规则，也不授权本次开发任务进行真实模型调用。远程只读 Reviewer 的权限与角色限制保持不变。

### AD-42：显式输入快照

- 状态：已接受（Owner 于 2026-09-05 明确要求）
- 决策：任务合同新增 `input_artifacts`，为 `(path, sha256)` 元组列表，声明远程 Worker 执行前必须存在且内容哈希匹配的只读输入文件。每个 path 必须是项目相对路径且不得与 `allowed_files` 重叠，sha256 必须是 64 位十六进制；路径重复被拒绝。Runner 在驱动远程 Worker 前从项目根按哈希校验并原子复制输入到最小临时沙箱，记录 `input_artifact.snapshotted` 事件；执行后核验输入未被改动，并仅将 `allowed_files` 内的产物全有或全无同步回工作树。
- 影响：远程 Worker 的输入状态被显式冻结为可审计快照；该字段进入计划哈希与授权，变化使旧授权失效。

### AD-43：审核接受策略

- 状态：已接受（Owner 于 2026-09-05 明确要求）
- 决策：任务合同新增 `review_acceptance_policy` 枚举，`block_p0_p1`（缺省）沿用 P0/P1 阻断批准；`zero_findings` 要求审核零 findings（含 P2/P3）才批准。策略在解析审核结果时确定性应用，不能由审核输出覆盖。
- 影响：为需要“无任何发现”的高质量门槛任务提供更严格的批准条件；缺省值保持向后兼容。

### AD-44：低 Token 主管协议

- 状态：已接受（Owner 于 2026-09-05 明确要求）
- 决策：计划可新增可选 `supervisor_policy`（缺省 `wake_events` 为空），声明主管唤醒事件白名单、缺省/升级推理强度、检查点数量上限与单条内容字符上限，以及可选的主管模型提示 `supervisor_model_hint`（注册表数据，不硬编码模型名）；MVP 强制 `continuous_llm_monitoring=false`（持续 LLM 监控不支持）。控制平面在唤醒事件发生时写入独立的 `supervisor_checkpoints` 表，内容受 `max_checkpoint_chars` 约束截断为合法 JSON；`wake_events` 只能额外增加可选唤醒原因，一组强制唤醒事件（与 `SUPERVISOR_MANDATORY_WAKE_EVENTS` 一致）不可被空或窄 `wake_events` 静默。主管经 `agentflow supervisor-next --after-sequence N --wait-seconds S` 读取有界 digest（仅轮询本地库、无新事件超时输出 `{changed:false,wake_required:false,cursor}`），经 `agentflow supervisor-record` 记录并校验决策（plan hash、cursor、schema、幂等重复、冲突拒绝、不得扩大授权/恢复 UNKNOWN/更改 plan）；`supervisor_digest` 提供低 Token 摘要。唤醒事件均为确定性状态/审核/测试派生的事件，不使用高频模型调用轮询。
- 影响：主管只被确定性事件唤醒，读取的是有界摘要而非完整日志；`checkpoint_json` 仍保留原语义，不承担主管摘要。强制唤醒事件清单与 `SUPERVISOR_MANDATORY_WAKE_EVENTS` 一致，均为业务无关的通用编码。

### AD-45：本地 Ollama 只读 Reviewer

- 状态：已接受（Owner 原始任务要求本地 Ollama 经 OpenCode 承担仅审核路径；第 4–6 节实现细节来自 Codex 根据 Owner 于 2026-09-08 委托裁定的本次实现方案）
- 决策：本地 Ollama（`provider='ollama'`、`is_local=True`）经 OpenCode 仅承担 `review`/`rereview`，复用 packet-only 只读最小 prompt、全工具禁用、固定 `steps=2`（已由 AD-46 替代）与 JSON-only 协议，费用为确认零远程费用。写角色、`read_only=false` 或非回环端点在推理进程启动前确定性拒绝。
- 端点与配置绑定：每次调用前重新读取实际 OpenCode 有效配置（`opencode debug config --pure`），验证 `provider.ollama` 的 npm 为已验证 transport、`options.baseURL` 与所选模型条目端点均为严格回环；远程、冲突、非法或无法证明的端点失败关闭（fail-closed）。验证后的回环端点与全 deny 配置写入子进程 `OPENCODE_CONFIG_CONTENT`，并移除代理变量、设置 `NO_PROXY='*'`。不新增表/字段，`ollama_host` 属于适配器运行配置而非计划字段。
- 模型元数据与角色边界：计划内 `ModelRef` 原样保留；无计划发现时 family 保持 `None`，不猜测为 `gpt-oss`。发现只产生 `discoverable`/`unavailable`，不提升为 `callable_verified`。`AdapterRouter` 对仅注册特定角色的 provider 在其他角色上失败关闭（`ReviewerUnavailableError`），Runner 保留原始拒绝原因并安全暂停。
- 影响：该决定覆盖 AD-34/AD-41 的远程扩展范围限制，但不放宽授权、D0-D3、预算、UNKNOWN、幂等、费用、文件或副作用规则，也不授权本次开发任务进行真实模型调用。

### AD-46：任务级审核步骤预算（BUG-RB-01）

- 状态：已接受（Owner 于 2026-09-08 要求按资源预算交接计划修复；默认值与边界经本轮确定性验证定稿）。
- 决策：`review_max_steps` 默认 8、范围 2–32。OpenCode 当前 steps 是最多 Agent 循环数，存在终止收尾轮；2 保留原受限选项，8 给 packet-only 审核有限余量，32 是工程防御上界而非模型事实。首轮完成可立即退出。
- 传递：任务合同 → canonical JSON/hash → Runner/InvocationService → 各 Reviewer 配置与 Ollama 二次验证，均按请求局部传参；LM Studio review/rereview 同样使用审核预算。
- 影响：明确替代 AD-38、AD-45 的“固定审核 steps=2”运行规则；实施与远程 Worker 步骤字段独立。审核不续接，步骤耗尽仍保持 AD-35/40 的失败、审计与恢复语义。既有有限超时保持，不新增 review timeout 合同。

### AD-47：冻结输出能力、角色授权与调用配置（BUG-RB-02）

- 状态：已接受（Owner 于 2026-09-08 授权本修复范围；当前 OpenCode 1.18.29 的配置/内嵌源码和无费用替身验证支持此方案）。
- 合同：`ModelRecord` 末尾追加 `max_output_tokens`、`capability_source`、`capability_source_version`、可选 `api_model_id`，保留旧位置参数。`ModelCapabilitySnapshot` 保存精确 `ref`、独立的 `context_length`/`max_output_tokens`、`source`/`source_version`、可选 API 别名；`PlanContract.model_capabilities` 保存唯一快照列表，计划缺快照可读取和展示，但不能签发新授权。
- 来源：优先显式注册表配置（registry_config），其次明确的 OpenCode catalog 资料（opencode_catalog），最后是用户显式提供可执行上界的保守策略（conservative_fallback）。`freeze_capability` 只冻结已有可信记录，不自动推断/查询/合并来源；模型列表、模型自述和 context 都不构成 output 证据。来源版本必须非空、至多 256 字符。发现状态不会因此变成 callable_verified。
- 角色：implementation/revision 共用 `implementation_max_output_tokens`；review/rereview 共用 `review_max_output_tokens`；默认均为 16000，这是授权缺省值而非模型能力。所有 Token/context 字段严格正整数，最大 2^31−1 是跨 JSON/SQLite/运行时的有界资源防御值，不是永久模型输出上限。未知 context 保持未知；不从 context 推测 output。
- 冻结规则：`effective=min(frozen capability, role authorization)`；fallback 使用自己的精确快照；续接重新从同一已授权计划与实际模型计算。能力、来源、别名、步骤或输出授权变化均改变哈希。执行时观察到更低 output/context 时拒绝（即便低值仍大于 role authorization）；catalog 来源在配置中消失也拒绝。更高能力不放大 effective。
- OpenCode：同时写精确 provider/model 条目的 `limit.output` 与子进程 `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`，避免内置 32000 运行时默认截断。深度保留 model options、其他 limits 和已验证端点/权限；存在 API 别名时必须在快照显式绑定。解析后的 model 配置按规范 JSON 比较，避免浮点/布尔相等陷阱；检查 agent steps 严格整数与权限。未验证的 provider 输出/思考覆盖在进程前拒绝。当前最终参数路径只验证了 `@ai-sdk/openai-compatible`，其他 transport 安全停止，不擅自声称其 SDK 行为已验证。本机当前 Ollama/LM Studio/DeepSeek 都使用该 transport。
- 审计与存储：复用 plans canonical JSON、tasks contract JSON 和 model_calls.request_scope_json.resource_budgets；不建第二注册表或新业务恢复通道。当前仓库没有 ModelRecord 专属持久注册表表，发现记录经 canonical 序列化与 `model_record_from_mapping` 往返；实际执行证据由计划内快照持久化。费用预留、实际 Token 和输出上限继续分离，不补造总 Token/推理 Token 算法。
- 运行时兼容门禁：本轮历史实现曾将 OpenCode 精确锁定为 1.18.29；该规则已由 AD-50 的稳定补丁系列策略替代。版本号不是二进制真实性证明；对应二进制 SHA-256 记录在各轮验证报告中。
- 旧计划：缺字段的计划可规范化读取（8/16000/16000/空快照），哈希因此变化，旧批准必然失效；不得原地改写历史 JSON/授权/调用。已保存的旧 ID/version 不能覆盖，重订计划需新 version 并重新展示哈希/授权。旧暂停 run 保留可读历史并拒绝旧授权恢复；后续业务重规划和既有产物重新审核由 Owner 独立安排，不自动迁移运行或重复调用。
- 验收：实现者确定性检查不替代不同模型自测和独立审核；本轮无真实模型调用、无 API 费用、无提交/全局配置改动。证据见 `verification/2026-09-08-reviewer-resource-budgets.md`。

### AD-48：输出额度耗尽与无文本失败保留证据

- 状态：已接受（Owner 批准 A＋B 范围：失败分类、审计修复与确定性测试，不包含新的模型执行或预算调整）。
- 共享解析器先提取使用量、会话与最终结束原因再判断结果。最终 `length` 优先分类为 `output_limit_reached`，用已有 `InvocationIncompleteError` 携带结果；有部分文本或合法审核 JSON 也不能成功。中间步骤 `length` 后确有最终 `stop` 与有效文本的流不按最终长度耗尽处理。
- 无文本使用带结果的 `InvocationProtocolError`；LM Studio 非零退出也保存失败使用量。复用 `fail_call` 和原有表，不新增授权、存储或恢复通道。分类器版本为 3，不改历史记录。
- Runner 区分 `review_output_limit_reached` / `implementation_output_limit_reached` / `model_output_invalid`。已落库的 `output_limit_reached` 或 `protocol_error` 在 resume 前阻断重发；既有协议错误一并遵守此保守规则。超时等 UNKNOWN 规则不变，不把不确定结果强行归类为已知失败。
- 本轮不推断 provider 通用的 reasoning 计费算法、不改默认输出额度或 thinking 参数、不回填历史 0 Token、不核销旧 UNKNOWN。确定性检查不构成不同模型自测或独立审核批准。

### AD-49：LM Studio 审核材料隔离与配置诊断

- 状态：已接受（Owner 确认局部修复方案）。
- LM Studio review/rereview 强制只读，复用 Reviewer 全工具 deny 规则；实际全局与所选 Agent 权限在推理前验证，不能依赖提示词禁止读取旧代码。实施/修订继续保留原文件权限，不新增合同字段、权限放行选项或存储通道。
- 审核只以冻结材料为证据；缺少上下文应报告，不得读取工作树补充。工具权限约束不是 OS 沙箱承诺，真实复验须检查工具调用记录。
- 两条 P3 按诊断改善处理：不兼容版本提示支持系列与实际版本；输出/思考覆盖提示配置项名称但不打印值。版本放行范围后由 AD-50 调整；thinking 选项和 zero_findings 不放宽，也不据此宣称 P3 已获独立关闭。
- 新代码与审核材料重新冻结后须重新展示哈希并获得批准；历史运行/授权/UNKNOWN 不变。不同模型独立自测仍为整体验收前置条件。

### AD-50：OpenCode 稳定补丁版本兼容策略

- 状态：已接受（Owner 于 2026-09-08 要求非实质兼容问题不要阻断后续运行）。
- 决策：用“已核验 major/minor 系列 + 最低补丁基线 + 每次调用的运行时不变量复核”替代精确补丁号锁定。当前范围为稳定版 `1.18.29+` 且仍在 `1.18.x`；`1.18.30` 已通过本机无推理源码路径审计。更老版本、带预发布后缀的版本、无法解析的输出以及不同 major/minor 系列继续在推理前失败关闭。
- 运行时条件：兼容补丁版只有在 AD-47/49 已有的最终 provider/model 配置、已验证 `@ai-sdk/openai-compatible` transport、API 别名、输出/思考覆盖、模型能力、两层权限、agent steps、enabled provider 与双重输出额度复核全部通过时才能启动推理。任何一项变化仍阻断，不能用版本兼容策略绕过。
- 证据与影响：离线审计脚本改用语义模式而非压缩变量名，确认 `1.18.30` 仍保留 32000 默认、`OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`、通用 output 限制变换与 compatible SDK `max_tokens` 映射；未产生模型调用或 API 费用。未来同系列补丁不再因补丁号本身要求改代码，但新的 minor/major 系列仍需独立审计后显式更新范围。本决定只替代 AD-47 的精确 `1.18.29` 版本门，不修改授权、预算、隐私、UNKNOWN、费用或审核门禁。

## 2. 原暂定、经实现验证后接受的决策

### AD-17：实现技术基线

- 状态：已接受
- 选择：Python 3.11+、Terminal CLI、SQLite 事务事件表、可插拔适配器并优先使用标准库；首发在 macOS 验证。
- 理由：设计合同和内存 SQLite schema 已通过测试，且核心数据使用 JSON 友好类型，不依赖 macOS 专属 API。

### AD-21：状态表示

- 状态：已接受
- 决策：把任务生命周期、运行控制状态和模型调用状态分成三个正交字段。任务生命周期表达草拟至批准/失败；运行控制状态表达 `RUNNING`、`PAUSE_REQUESTED`、`QUIESCING`、`PAUSED`、`USER_TAKEOVER`、`RESUMING`；模型调用状态单独表达 `UNKNOWN`。
- 理由：状态迁移测试证明三个状态机能够独立校验，避免状态组合爆炸，并阻止 `UNKNOWN` 收费调用被重新启动。

## 3. 冲突检查与解释

目前没有发现不可调和的需求矛盾。AD-34 是 Owner 对 AD-20 范围的明确后续扩展，不视为隐式冲突。以下表面张力按保守方式解释：

| 表面张力 | 一致解释 | 状态 |
| --- | --- | --- |
| “用户不在线也继续”与“必须明确激活/授权” | 只在已经有效授权的模型、预算、数据、文件和副作用范围内继续；越界立即暂停。 | 已由 AUTH-06 固化 |
| Skill 可自动建议与不得自动启动模型 | 保留默认自动发现，只把发现结果作为建议；执行激活是独立、可审计动作。 | 已由 AD-04/16 固化 |
| 预算耗尽后本地继续与关键门禁必须通过 | 可以继续非越权工作，但没有足够独立审核时状态只能是等待更强审核。 | 已由 COST-04 固化 |
| 审核独立性与当前可能只有少量本地模型 | 普通任务允许同一家族的独立只读上下文；重要/关键任务必须跨家族，可用组合不足时不批准。 | 已由 AD-23 固化 |
| SQLite 当前状态与追加式事件日志同时存在 | 事件表和状态投影由单个 SQLite 事务更新，JSONL 只是可再生成导出。 | 已由 AD-24 固化 |
| 平台无关与首发 macOS | 核心合同、存储与适配器跨平台；macOS 通知等能力隔离为可选增强。 | 已由 PLAT-03 固化 |
| 任务卡只有 `risk_level` 字段与双维风险模型 | `risk_level` 以对象形式保存独立的 `business_importance` 与 `operational_safety`。 | 已由 AD-25 固化 |
| AD-20 推迟远程适配器与新增远程 Reviewer | AD-34 仅覆盖经 OpenCode、计划限定、只读的 review/rereview；其他远程直连和真实 smoke test 仍不在本次范围。 | 已由最新 Owner 决策固化 |

## 4. 待确认事项

用户已于 2026-09-04 接受 AD-18 至 AD-20 及 AD-22 至 AD-33，原待确认清单已清零。

AD-17 与 AD-21 已在里程碑 1 通过可执行设计测试并转为“已接受”。当前没有待确认事项；只有拟偏离现有选择时才需要重新提交用户确认。
