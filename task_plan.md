# multi-model-agentflow 实施计划

## Goal

修复 OpenCode 本地 implementation 调用的固定总超时问题，以及超时后 Token/审计元数据丢失的问题；新增 `implementation_timeout_seconds` 计划字段，完成无费用回归验证、离线构建和 canonical/installed Skill 同步。

## Current Phase

里程碑 19 主体实现完成，正在修复 Codex 独立验收（`CHANGES_REQUIRED`）提出的 P0/P1/P2 项：最小临时沙箱、严格类型校验、真实输入快照、远程超时审计、完整主管协议、增量 `status --watch`、角色感知 fallback 路由与文档修正。当前全套 239 项测试通过；本轮不调用真实模型、不启动子 Agent、不产生 API 费用、不创建 commit。

## Next Step

完成剩余文档修正后，同步 canonical/installed Skill、离线构建 wheel 并在干净隔离环境安装验证；最终复核 `findings.md`/`progress.md` SHA-256 未变化。

## Milestones

### 里程碑 1：实现设计冻结

**Status:** complete

- [x] 定义计划、任务、授权、调用、事件、审核和费用数据合同
- [x] 定义任务生命周期与运行控制状态迁移
- [x] 定义 SQLite schema、事务边界和幂等恢复语义
- [x] 定义模型适配器与无费用测试替身协议
- [x] 将 AD-17、AD-21 更新为已接受
- [x] 运行设计一致性测试（8 项通过）

### 里程碑 2：确定性内核

**Status:** complete

- [x] 实现配置、数据库、事件和迁移
- [x] 实现计划规范化、SHA-256 授权和过期
- [x] 实现风险、隐私、预算与文件范围门禁
- [x] 实现状态机、检查点与收费调用幂等保护
- [x] 实现无费用测试替身并通过内核测试（27 项通过）

### 里程碑 3：本地执行闭环

**Status:** complete

- [x] 实现 `agentflow` CLI
- [x] 实现三种运行模式
- [x] 实现 worktree、实施、自测、只读审核、修复与复审
- [x] 实现安全暂停、立即冻结、人工接管与恢复
- [x] 通过测试替身端到端验收（40 项通过）

### 里程碑 4：真实本地模型

**Status:** complete

- [x] 探测 OpenCode 与 LM Studio 本地环境
- [x] 接入 OpenCode → LM Studio 适配器
- [x] 使用无敏感数据的临时项目验证本地调用
- [x] 记录模型版本、速度、Token、耗时和审核独立性

### 里程碑 5：Codex Skill

**Status:** complete

- [x] 创建最小 `multi-model-agentflow` Skill
- [x] 共用 `agentflow` 计划、授权和状态源
- [x] 验证自动建议不会自动启动模型
- [x] 通过 skill-creator 校验

### 里程碑 6：完整验收与收尾

**Status:** complete

- [x] 运行 MVP-A01 至 MVP-A18
- [x] 修复所有 P0/P1
- [x] 执行代码简化与文档一致性复查
- [x] 验证控制平面停用后可人工接管

### 里程碑 7：远程 Reviewer 规范与合同

**Status:** complete

- [x] 完成目录、Git 状态和允许范围检查
- [x] 完整读取规范、实现、测试及 canonical/installed Skill
- [x] 更新需求、MVP 范围和 Owner 架构决策
- [x] 冻结 provider/model 校验、发现状态、Review Packet 和费用未知合同

### 里程碑 8：远程 Reviewer 实现与自动化测试

**Status:** complete

- [x] 实现 OpenCode remote-provider 只读 Reviewer 与安全路由
- [x] 实现 Review Packet 哈希审计、隐私/预算/独立性门禁
- [x] 实现 reported cost、cost_unavailable 与汇总审计
- [x] 覆盖角色、权限、注入、发现、UNKNOWN、恢复和 CLI 替身场景

### 里程碑 9：确定性验证、构建与 Skill 安装

**Status:** complete

- [x] 运行完整测试、compileall、Skill quick_validate 和 git diff --check
- [x] 构建 wheel 并在临时隔离环境验证安装、导入和 CLI
- [x] 确认 launcher 与实际加载路径
- [x] 安装 canonical Skill、复验并逐文件比对 SHA-256
- [x] 汇总零真实模型调用、零远程费用和未运行检查

### 里程碑 10：真实故障语义与修复设计

**Status:** complete

- [x] 确认 OpenCode `steps` 的当前精确语义和有限最小预算
- [x] 审计 implementation/review 调用、调用状态、恢复和费用持久化路径
- [x] 冻结步骤耗尽、严格 Reviewer JSON 和未跟踪输出证据的通用合同

### 里程碑 11：核心实现与回归测试

**Status:** complete

- [x] 实现有限 Reviewer 步骤预算和已知不完整调用语义
- [x] 实现严格 JSON-only Reviewer 协议及阻断性 finding 一致性验证
- [x] 修正 Runner 的安全暂停、证据保留、恢复幂等和未跟踪文件证据
- [x] 补齐真实事件流 fixture 与定向回归测试

### 里程碑 12：文档同步与完整验证

**Status:** complete

- [x] 同步稳定需求编号、MVP 验收、架构决策与简洁 Skill 边界
- [x] 运行定向测试、完整测试、compileall、Skill 校验与 `git diff --check`
- [x] 审计 changed files、测试替身标记、零真实调用及通用核心边界

### 里程碑 13：离线构建与 Skill 安装

**Status:** complete

- [x] 重建 wheel 并在干净临时环境安装、导入和运行测试
- [x] 更新本机 installed Skill，不删除或清理旧文件
- [x] 验证 canonical/installed Skill 一致并记录 SHA-256
- [x] 运行安装后静态无费用自检，不执行真实 Reviewer smoke test

### 里程碑 14：非零退出步骤耗尽修复与步骤预算字段

**Status:** complete

- [x] 修复 OpenCode 本地与远程 Reviewer 非零退出时先解析 stdout 严格步骤耗尽信号再分类
- [x] 本地 signal 终止保持 `InvocationOutcomeUnknown`，不降级为已知失败
- [x] 已知失败经 `fail_call` 保存 provider/session ID、Token、耗时、费用与 `termination_source`
- [x] Runner 在 implementation/review 边界分别暂停并拒绝 resume 重复调用
- [x] 新增任务合同字段 `implementation_max_steps`（缺省 8，1–32），进入计划哈希与授权
- [x] OpenCodeAdapter 从请求元数据读取并验证预算后写入 `agentflow-sandbox` 的 `steps`
- [x] 补齐本地/远程非零退出、普通错误、signal、Runner 真实适配器与预算字段回归测试

### 里程碑 15：本地实施调用超时与审计元数据保留

**Status:** complete

- [x] 新增任务合同字段 `implementation_timeout_seconds`（缺省 900，60–14400），进入规范化 JSON 与计划哈希；布尔/字符串/浮点/越界值被拒绝
- [x] OpenCodeAdapter 从请求元数据读取并验证超时后用于本地 `communicate(timeout=...)`，取代固定 900 秒
- [x] 超时后终止进程组、在原始字节层合并 `TimeoutExpired.output` 与后续输出（UTF-8 多字节截断安全），保守解析部分 stdout 中已确认 Token/会话 ID
- [x] 以 `InvocationOutcomeUnknown(result=...)` 返回，服务层经 `mark_call_unknown` 持久化 `UNKNOWN` 终态与 Token/耗时/会话/`termination_reason=timeout`/`token_source`/`usage_unavailable`；`output_text` 保持空
- [x] 部分输出重叠或重复不重复累计 Token：先在原始字节层统一并消除 `TimeoutExpired.output` 与后续 `communicate()` 输出的重叠，合并完成后只解码一次，再按每个真实出现的已完成 step 累加 Token；不得根据事件内容推断重复；无可用 usage 时诚实记录 `usage_unavailable`
- [x] 完成 step 计数改用 `completed_step_count`，只统计 `step_finish`，不再同时统计 start/finish
- [x] 正常完成路径 `parse_opencode_json` 同步由 max 改为累加，并补充 reasoning Token 提取
- [x] 补齐契约、适配器、Runner 真实适配器与 CLI 回归测试，并同步文档与 Skill

### 里程碑 16：本地实施步骤耗尽的同会话分段续接

**Status:** REVIEW_PASSED

- [x] 适配器识别真实 OpenCode 步骤耗尽文本 `Maximum steps for this agent have been reached.`（并保留既有变体与误报护栏），读取 `continuation_session_id` 元数据并追加 `opencode run --session <id>`
- [x] 新增任务合同字段 `implementation_max_continuations`（缺省 0，0–8），进入规范化 JSON、计划哈希、授权与 `plan show`
- [x] schema/model_calls 增加 `segment_index`、`continuation_of_call_id`、`continuation_session_id`，provider-request 唯一索引改为 `(provider, provider_request_id, segment_index)` 并在 `initialize()` 中幂等迁移
- [x] Runner 以 `_drive_local_role` 在同一会话内分段续接本地 implementation/revision，`_continuation_allowed` 门禁（本地模型、实施/修订角色、续接限额内、有效会话 ID、文件范围）；越界文件在续接前终止任务
- [x] 续接 segment 拥有唯一 call_id/request_key（`...:segment:{index}`）/segment_index/continuation_of_call_id，并单独记录 Token/耗时/费用；续接决定持久化为 `continuation.scheduled` 事件
- [x] `max_retry_count` 驱动多轮修订（impl→test→fix→retest），修订提示词携带返回码、stdout/stderr 尾部、缺失输出、未跟踪空白错误、changed files 与剩余验收条件的有界证据
- [x] resume 以 `_has_blocking_incomplete_call` 区分可续接与不可续接的步骤耗尽调用；segment 间进程重启后只续接一次且不重复已完成 segment
- [x] 补齐契约、适配器、Runner、迁移回归测试，并同步 requirements/mvp/architecture-decisions 与 Skill
- [x] 以 `BEGIN IMMEDIATE` 关闭暂停门禁与 continuation STARTED 之间的 TOCTOU 竞态
- [x] 区分并安全恢复 base/continuation PLANNED；非法元数据在 adapter 调用前暂停
- [x] Codex 独立复跑 167 项测试、compileall 与 `git diff --check`，Milestone 16 `REVIEW_PASSED`

### 里程碑 17：真实步骤耗尽检测遗漏修复

**Status:** complete

- [x] 根因：真实 Qwen implementation/revision 输出把 `Maximum steps for this agent have been reached.` 作为“前缀推理文本 + `</think>` + 独立终止行 + 长 Markdown Summary”中的独立行，旧 `_text_reports_step_limit` 只检查首行或整段 `fullmatch`，导致两调用都被误记 `completed` 并错误进入 revision
- [x] 改为逐行结构化扫描：跟踪 fenced code、跳过 blockquote/diff/缩进内容，仅匹配规范化后整行等于已接受终止标记（`maximum steps…have been reached` / `the maximum number of steps…` / `critical maximum steps reached`）的行，保留全部误报护栏
- [x] 检测到真实终止后 `parse_opencode_json` 抛 `InvocationIncompleteError`（`failure_kind=step_limit_reached`、`termination_source=final_text`），保留 session ID、Token、费用、耗时与输出证据；退出码 0 与非 0 均正确分类
- [x] Runner 经既有同会话 continuation 路径续接而非运行 deterministic tests 或开启新 revision；新增真实适配器替身回归证明 base 记步骤耗尽、产生 `continuation.scheduled`、同 session 递增 segment_index，第二段完成后才进入确定性测试；review/rereview 仍不续接
- [x] 新增最小脱敏 fixture `opencode_max_steps_long.jsonl`，补齐 5 项回归测试（含负向）并复跑全套 172 项通过
- [x] 修复 Codex 二轮 P1：`_line_is_step_limit_marker` 原先先 `re.sub` 剥离全部非字母数字再匹配，会把标题/加粗/斜体/行内代码/引号/列表包裹的终止短语误判为真实终止；改为对原始行做严格锚定的 `re.fullmatch`（`re.IGNORECASE`，仅容忍末尾句号与 CRITICAL 变体的 `-`/`–`/`—`），并新增 Markdown/引号包裹负向测试；全套 173 项通过

### 里程碑 18：同行摘要引导语与疑似步骤耗尽分类

**Status:** REVIEW_PASSED

- [x] 只对两个真实观测到的同行摘要引导语做严格整行白名单匹配，不使用宽泛 `startswith` 或任意 summary 后缀
- [x] 步数与 `step_finish.reason` 仅作诊断元数据，不单独判定终止；高置信命中仍走同 session continuation
- [x] 所有已接受的裸标记变体共用一套语法，非白名单后缀进入 `suspected_step_limit` 安全暂停，恢复前不重复调用 adapter
- [x] 修复 `reachedness`/`reached123`/`reached_value` 词边界误报，并补充句点已构成边界但后缀无空格的疑似路径
- [x] `terminal_reason` 明确为最后一个带 reason 的 `step_finish`；新增真实脱敏 fixture 与 parser/Runner 回归
- [x] Codex 独立运行 193 项测试、compileall、修改文件 Ruff 与 `git diff --check`，未发现剩余 P0/P1

### 里程碑 19：远程 Worker、显式输入快照、审核接受策略与低 Token 主管协议

**Status:** in progress

- [x] 合同新增 `RemoteNetworkMode`/`ReviewAcceptancePolicy`/`SupervisorReasoningEffort` 枚举、`InputArtifact`、`SupervisorPolicy`，以及 `TaskContract`（`allow_remote_implementation`、`remote_worker_network_mode`、`remote_worker_allowed_hosts`、`remote_worker_max_steps`、`remote_worker_timeout_seconds`、`input_artifacts`、`review_acceptance_policy`）与 `PlanContract.supervisor_policy` 字段，全部进入规范化 JSON 与计划哈希并严格校验（拒绝字符串布尔、布尔/浮点整数、非字符串 host/path/hash）
- [x] 实现 `RemoteOpenCodeWorkerAdapter`（远程 implementation/revision，写权限 + 全网络/子代理禁用，步骤耗尽暂停且不续接，超时 `UNKNOWN` 并复用本地字节合并/partial usage，远程成本记 unavailable），与 Reviewer 共享 `_discover_remote_model_ids` 发现助手
- [x] Runner 将远程 Worker 路由到最小临时沙箱（仅含哈希校验只读输入、允许文件与生成简报），输出全有或全无同步回工作树，并记录 `input_artifact.snapshotted` 事件；Reviewer 保持 packet-only 只读隔离
- [x] `invocation_decision` 放行已授权的远程 implementation/revision，网络 `ALLOWLIST` 模式失败关闭；纯函数 `network_decision` 单独承载白名单语义
- [x] `_parse_review` 按 `review_acceptance_policy`（`block_p0_p1`/`zero_findings`）确定性判定批准；P0/P1 finding 写入 checkpoint + 安全暂停且不修订，P2/P3 在 `zero_findings` 下自动修订、重试耗尽前不唤醒
- [x] `supervisor_checkpoints` 表与完整主管协议：强制唤醒事件不可被空 `wake_events` 静默、`reasoning_effort`/`plan_hash`/`event_sequence`/`terminal` 列、终局 checkpoint 恰好一次且幂等、`max_supervisor_checkpoints` 同事务强制执行、有界 digest 保留 reason/预算/范围/plan hash/cursor 且 `original_bytes` 为真实 UTF-8 字节数
- [x] CLI 新增 `supervisor-next`（`--after-sequence`/`--wait-seconds`，仅轮询本地库）与 `supervisor-record`（校验 plan hash/cursor/决策 schema、幂等重复、冲突拒绝、不得扩大授权/恢复 UNKNOWN/更改 plan）；`status --watch` 首次全量、后续仅在状态或事件序列变化时输出；`_runner` 按 provider+role 注册 Worker/Reviewer 适配器（不再误报同 provider 双角色）
- [x] 远程 Worker 超时/中断持久化部分 stdout 字节与 SHA、时长、session ID、`termination_reason=timeout`
- [x] 新增/修订回归测试（严格类型、沙箱隔离、全有或全无、输入快照事件、终局幂等、上限强制、supervisor-record 校验、digest 截断），全套 239 项通过；`git diff --check` 与 compileall 通过

## Decisions Made

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-09-04 | 使用 Python 标准库优先实现 | 符合已确认技术基线并降低依赖 |
| 2026-09-04 | 先完成测试替身闭环，再连接本地模型 | 隔离模型环境不确定性并保证零意外费用 |
| 2026-09-04 | 本任务不调用收费模型、不启动子 Agent | 遵守用户授权边界 |
| 2026-09-04 | Python 最低版本为 3.11，核心优先标准库 | 保持跨平台且减少依赖 |
| 2026-09-04 | 任务、控制和调用状态使用三个正交状态机 | 避免组合状态爆炸并隔离 UNKNOWN 调用 |
| 2026-09-04 | 通过 OpenCode 增加计划限定的远程只读 Reviewer | Owner 明确扩大该项范围，但不放宽其他安全边界 |
| 2026-09-04 | 远程 Reviewer 仅允许 review/rereview，实施与修改继续由本地主模型承担 | 保持职责隔离并限制远程副作用 |
| 2026-09-04 | 本地实施超时改为计划字段控制，超时记为 `UNKNOWN` 并保留审计证据 | 避免固定 900 秒超时与超时后元数据丢失，且不把未确认结果当已知失败 |
| 2026-09-05 | 本地实施/修订步骤耗尽时在同一 OpenCode 会话内以新 segment 分段续接，续接预算由计划字段 `implementation_max_continuations` 控制 | 修复固定 `steps` 预算耗尽即中止、无法在一个任务合同内继续的问题；续接非“自动重试”，仍在授权、文件范围与预算门禁内 |

## Errors Encountered

| Error | Attempt | Resolution |
| --- | --- | --- |
| `unittest.mock` 未由 `import unittest` 暴露 | 里程碑 2 首次测试 | 改为显式 `from unittest import mock` |
| 计划经 JSON 往返后整数预算变为浮点数，导致授权哈希变化 | 里程碑 3 CLI 测试 | 在冻结数据合同构造时规范化数值类型 |
| OpenCode 本地写入烟雾测试只暴露读取工具，未创建允许文件 | 里程碑 4 首次真实写入 | 调查自定义 agent 的工具暴露与权限合并方式；不重复原命令 |
| skill-creator 的 `quick_validate.py` 缺少 PyYAML | 里程碑 5 首次校验 | 复用已安装 Skill 的 vendored PyYAML 运行官方校验；项目未增加依赖，校验通过 |
| 沙箱内首次 `git status` 无法创建 Xcode 临时缓存 | 里程碑 7 启动检查 | 经用户批准在沙箱外只读运行 `/usr/bin/git status`，确认工作树干净 |
| 用户指定的原始 unittest 命令首次无法导入 `src/` 布局包 | 里程碑 7 基线测试 | 使用 `PYTHONPATH=src` 重跑，62 项全部通过；最终在 wheel 隔离安装后再运行原始命令 |
| 文档批量补丁因 AD-33 行文本不匹配未应用 | 里程碑 7 文档同步 | 拆分为小补丁并以实际行内容定位，避免重复失败 |
| authorization 首次补丁因字段顺序与预期上下文不一致未应用 | 里程碑 8 合同实现 | 读取实际构造顺序后使用小范围补丁更新 |
| fallback 测试替身重复使用 provider request ID | 里程碑 8 扩展测试 | 改为由 request key 生成唯一测试 ID，保留数据库幂等约束 |
| 合并测试补丁因目标文件缺少预期 import 行未应用 | 里程碑 8 覆盖补强 | 拆成 import/测试两个按实际上下文定位的小补丁 |
| 数据库迁移定向测试漏导入 `sqlite3` | 里程碑 8 迁移验证 | 补充标准库导入并仅重跑迁移场景 |
| 修复迁移测试的首个补丁误把 task_plan 上下文用于测试文件 | 里程碑 8 错误记录 | 改为两个明确的 Update File 段后应用 |
| 定向 CLI stub 测试在受限沙箱内无可用临时目录 | 里程碑 8 定向验证 | 使用已批准的沙箱外无网络测试权限重跑，1 项通过 |
| 首次 `compileall` 验证被用户中止 | 里程碑 9 确定性验证 | 确认无遗留进程后重跑同一命令，退出码 0 |
| 首次 wheel 命令的换行被转义为普通文本 | 里程碑 9 离线构建 | 未执行构建、未产生 wheel；改用真实换行重跑 |
| `uv build --offline` 的隔离环境未缓存 `setuptools` | 里程碑 9 离线构建 | 使用系统已有 Python 3.11 与 setuptools 82.0.1 执行 `--no-build-isolation`，仍保持离线并成功构建 |
| pause/cancel 子测试先创建暂停 run，触发单活动 run 保护 | 里程碑 9 最终覆盖补强 | 改为先验证 cancel、再验证 pause；未降低数据库约束 |
| 定向测试在受限沙箱无可用临时目录 | 里程碑 11 首次测试 | 按既有项目做法获批在沙箱外运行无网络测试；真实断言结果为 52 通过、1 失败 |
| 未跟踪空白错误用例允许 1 次 revision，比预期多一次调用 | 里程碑 11 定向测试 | 将该边界用例的重试数设为 0，保持“门禁失败后不进入 Reviewer”的单一验证目标 |
| 合并的 `apply_patch` 对 `task_plan.md` 含两个 Update File 段，校验器拒绝 | 里程碑 11 进度更新 | 将源码/测试和单一计划更新拆开后成功应用，没有部分写入 |
| Runner 的真实步骤耗尽 fixture 回归仍断言旧手工费用 `0.25` | 本轮定向测试 | 改为断言 fixture 实际报告费用 `0.125`；生产检测与费用持久化逻辑无需修改 |
| 独立验收发现超时路径 `TimeoutExpired.output` 为 `bytes`、后续 `communicate()` 返回 `str`，直接相加触发 TypeError 落入普通 FAILED | 独立验收 P1 | 新增 `_decode_partial_stdout` 统一解码，并用 `_merge_overlapping_output` 按重叠前缀合并两段输出 |
| 独立验收发现 `max` 聚合会系统性少记多步骤 Token（真实会话 input 120156 vs max 34362） | 独立验收 P1 | 改为按每个真实出现的已完成 step 累加 Token，`parse_opencode_json` 与 `parse_opencode_partial_usage` 一并修复，并补充 reasoning 提取与 bytes/多步/重叠回归测试 |
| 独立验收发现按 canonical JSON 内容去重会误删内容相同的独立 step，且 `step_count` 同时统计 start/finish | 独立验收 P1/P2 | 改为原始字节层重叠合并（`_merge_overlapping_output_bytes`）、去除事件内容去重，`step_count` 改为只统计 `step_finish` 的 `completed_step_count` |
| 真实运行 `task011-agentflow-v2-20260905` 中 Qwen implementation/revision 的长输出（前缀文本 + `</think>` + 独立终止行 + Markdown Summary）被误记 `completed` | 本轮真实缺陷 | 改为逐行状态扫描检测独立未引用终止行，触发 `InvocationIncompleteError` 并走同会话 continuation；新增长摘要 fixture 与 5 项回归测试 |
| Codex 二轮发现 `_line_is_step_limit_marker` 先剥离标点导致标题/加粗/斜体/行内代码/引号/列表包裹的终止短语被误判为真实终止 | 本轮 P1 | 改为对原始行严格锚定 `re.fullmatch`，仅容忍末尾句号与 CRITICAL 变体连字符；新增 Markdown/引号包裹负向测试 |

## Final Verification

### 里程碑 17（本轮）

| Command | Exit | Result |
| --- | ---: | --- |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_opencode_adapter.py' -v` | 0 | 39 tests passed |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_runner.py' -v` | 0 | 42 tests passed |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` | 0 | 173 tests passed |
| `PYTHONPYCACHEPREFIX=/private/tmp/agentflow-pyc python3 -m compileall -q src tests` | 0 | passed |
| `git diff --check` | 0 | passed |

### 里程碑 16（本轮）

| Command | Exit | Result |
| --- | ---: | --- |
| P1 定向回归（pause TOCTOU、base/continuation PLANNED、非法元数据、fallback 精确模型/session、session mismatch、STARTED/COMPLETED 崩溃恢复） | 0 | 8 项通过 |
| `test_opencode_adapter` | 0 | 34 tests passed |
| `test_runner` | 0 | 41 tests passed |
| `test_core` | 0 | 46 tests passed |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` | 0 | 167 tests passed |
| `PYTHONPYCACHEPREFIX=/private/tmp/agentflow-pyc python3 -m compileall -q src tests` | 0 | passed |
| `git diff --check` | 0 | passed |

### 里程碑 15（历史）

| Command | Exit | Result |
| --- | ---: | --- |
| 超时定向回归（bytes 输出、多步累计、重叠去重、相同独立 step 计数、UTF-8 截断合并、completed_step_count） | 0 | 新增 4 项，更新 2 项 |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` | 0 | 136 tests passed |
| `python3 -m compileall -q src tests` | 0 | passed |
| `git diff --check` | 0 | passed |

Final wheel SHA-256: `41a28e72f8f51bddb04d997cb3c5953705ebe816083198050d0e255e658167c3`.

Canonical and installed `SKILL.md` SHA-256: `b0ac3274673d7439accdaaae4088e4eec526716d13128ea7564451430788d855`（逐字节一致）。

Canonical and installed `agents/openai.yaml` SHA-256: `3d6e327648b9db455d0f96a278c6c7be957a65f7e66c65f9d2863239dc90c0ed`（逐字节一致）。

项目未配置额外 lint 或 type-check 命令，当前环境也没有 `ruff`、`mypy` 或 `pyright`，因此这些检查未运行。
