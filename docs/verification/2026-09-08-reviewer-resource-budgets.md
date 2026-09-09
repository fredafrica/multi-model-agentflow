# Reviewer 资源预算修复证据

状态：实现与确定性检查完成，独立验收待完成。实施者检查不构成不同模型自测或独立审核批准。

## 基线与范围

- 分支 `codex/reviewer-budget-fixes`，HEAD `b8885939ec084f878bb9fa49ea152b1157bd6f3d`，复用 Ollama linked worktree。
- 起始唯一已有改动：未跟踪的交接计划，本轮保留原文。
- 基线：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 -m unittest discover -s tests -q`，442 tests，35.386s，OK。
- 真实模型调用 0；API 费用 0；未加载/下载模型、安装运行时、改全局配置或业务数据库。

## BUG-RB-01

根因：远程/Ollama 配置构造、Ollama 环境重建及 resolved 检查均固定为 2；LM Studio review 误用 implementation 步骤。

失败证据：配置消费替身需要三轮，显式 8/12 仍抛步骤耗尽；单轮结果返回但捕获配置为 2；LM Studio review 同样耗尽。修正替身缺失的 text part.type 后，原实现 2 个测试产生 4 failures、9 errors（均为预期预算未贯通）。

修复：任务字段贯通解析、哈希、Runner、InvocationService、三种 Reviewer 适配器及 Ollama 配置重建/复核；审核与实施步骤不再共用。请求元数据与授权计划不一致时拒绝。没有添加自动续接或提高工具权限。

通过证据：首段定向 4 tests，OK；最终全量 465 tests，OK。`tests/test_reviewer_resource_budgets.py` 中 `ReviewerStepBudgetTests` 使用消费实际进程配置的替身，覆盖 remote/Ollama review/rereview 的 2/8/12、需三轮与单轮提前返回；LM Studio 的审核预算与实施预算独立。`BudgetRunnerTests.test_exhaustion_audit_no_review_and_resume_never_redispatches` 验证耗尽后无 review、保留已发生 Token/费用、恢复不再调用。旧超时/signal/协议/费用门禁测试同时通过。

## BUG-RB-02

本机 OpenCode 1.18.29。只读检查已安装可执行文件的内嵌源码，发现 `maxOutputTokens` 取 `Math.min(model.limit.output, runtimeMax) || runtimeMax`；运行时默认值为 32000，环境入口为 `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`。因此仅覆盖 limit.output 不能解除中间层默认限制。本次同时绑定调用级配置和子进程输出上限，并验证参数源码路径。

根因：原 ModelRecord 只有 context、没有可信 output/source；任务未区分角色输出授权，计划未冻结能力，调用配置受上述默认值约束，调用审计也没有有效输出依据。

失败证据：最初的新测试确认能力快照字段缺失、未知能力请求没有在推理前停止；后续负向测试还暴露 resolved output 的浮点相等问题、未知 transport 未拒绝以及发现阶段 context 被数值转换接受的问题（该轮 3 failures、1 error）。这些分别通过严格类型校验、规范 JSON 比较、输出路径门禁和取消 context 强转修复。最后新增版本检查时旧 Ollama 集成替身 3 项失败，原因是未模拟 `--version`；补齐明确的版本替身后全量通过，并新增未知版本拒绝测试。

修复：`resource_budgets.py` 从计划和实际模型计算最小值；InvocationService 在派发前重新生成可信元数据，不接受调用者扩权。四条适配器路径均设置精确 model 的 `limit.output` 和子进程运行时上限，再检查 resolved 配置。Ollama 重建后重新验证 output、context、端点、transport、步骤和禁工具权限。provider/model 输出或思考覆盖、别名漂移、模型切换、managed 覆盖均在推理前拒绝。费用预留、实际使用量算法和恢复通道不变。

通过证据：新模块覆盖所有四条路径与 implementation/revision/review/rereview；虚构能力 65536 配角色授权 16000/100000 分别得到 16000/65536，384000 不被截为 65536；context/input/options/API 别名保持。能力增长不扩权、能力缩小拒绝，未知能力拒绝授权或安全暂停。实际 Runner fallback 使用自己的 4096 output/32768 context；UNKNOWN 保留预算及 Token 并阻止恢复重发。联合五文件 fixture 使用 steps=12、review output=16000、implementation output=8000，完整经过 review、revision、rereview，各角色记录正确。五文件 fixture 仅证明 packet/预算/流程，绝非真实模型语义审核证明。

## 合同、来源与授权规则

| 字段/角色 | 缺省、范围及含义 |
| --- | --- |
| `review_max_steps` | 默认 8；严格整数 2–32；最多循环次数，不是必须跑满。8 是为三轮以上审核留余量的工程缺省，32 是有限资源边界，不是质量承诺。 |
| `implementation_max_output_tokens` | 默认 16000；implementation/revision，包括远程 Worker。 |
| `review_max_output_tokens` | 默认 16000；review/rereview，包括 LM Studio/Ollama/remote。 |
| `ModelRecord.max_output_tokens` | 默认未知；有值时严格正整数，独立于 context；无可信来源不得冻结/授权。 |
| context/output/角色 Token 值 | 上界 2^31−1，仅为有界数值资源防御，不是模型能力；拒绝 bool、浮点、字符串、null（必填预算）、0、负数、越界。context 可未知。 |
| `PlanContract.model_capabilities` | 缺省空列表便于旧计划读取；执行授权要求所有主模型及 fallback 的精确快照。 |

来源优先级为显式 `registry_config`、明确 `opencode_catalog`、最后是显式且有安全上界的 `conservative_fallback`；每条必须有非空来源版本（至多 256 字符）。这是构建可信记录时的选择规则，`freeze_capability` 不自动抓取、猜测或合并来源。当前发现列表只证明发现状态，不会伪造 output 或升级成 callable_verified。没有默认 32K 能力回填。

每个快照保存精确 ModelRef、独立 context/output、source/source_version 与可选 api_model_id。`effective=min(冻结能力,角色授权)`，策略标识 `frozen_min_reject_decrease_v1`。运行前观察到 output/context 更低则拒绝（即使低值仍大于角色授权）；catalog 来源的 output 消失也拒绝。增长不改变冻结有效值。未知能力为 `unknown_pause`。续接从原已授权计划和实际模型计算，fallback 不借用主模型快照。

预算、能力、来源、别名均进入 canonical JSON 和 plan_hash。`plan show` 展示计划与按角色/模型派生的 resource_budgets，派生展示不构成第二授权源。模型记录通过序列化 helper 往返；仓库没有 ModelRecord 专属持久注册表表，实际能力快照沿用 plans/tasks JSON 持久化，不新增第二注册表或 schema。

调用最小证据存入 `model_calls.request_scope_json.resource_budgets`：role、实际 ModelRef、configured_steps、authorized/effective output、capability 及其哈希/来源、policy。正常、已知失败、UNKNOWN、复用均保留；不存完整 resolved 配置、认证信息或 headers。fixture 的 approved 文本只用于测试，未写入任何业务运行。

## 旧计划与恢复

旧计划可读取并补 8/16000/16000/空快照，但规范化哈希变化使旧授权失效，不自动刷新授权。未授权计划应补可信快照后展示新哈希；已保存计划不能覆盖原 ID/version，必须新版本并重新批准。旧暂停运行可读历史，恢复时拒绝旧批准；不原地改 JSON、授权、Token、费用、approved 或 UNKNOWN。临时旧库测试对比原 JSON 不变并断言没有新调用。既有业务产物重审、旧 run 重规划属于 Owner 后续独立授权，不在本轮执行。

## OpenCode 最终参数证据与限制

- 版本：1.18.29；已检查二进制 SHA-256：`2f24593f1b8e578d0b7ed7ca399440d4b6c125330eece20a69ad8d380190d669`。
- `tests/verify_installed_opencode_output.cjs` 只读提取已安装源码，断言环境入口、32000 默认、min 变换、LLM 参数传递、compatible chat SDK 的 `max_tokens` 属性；执行提取的表达式，三组 wire_max_tokens 为 16000/65536/384000，旧默认负向对照为 32000。
- 这是交接允许的“安装版本源码与配置路径”证据；没有执行完整 SDK、HTTP 请求或真实 provider，不能称真实端到端验证。Python 替身另行验证调用时的环境/config，二者共同覆盖此版本的映射路径。
- 当前仅验证 `@ai-sdk/openai-compatible`；本机只读配置摘要显示 Ollama/LM Studio/DeepSeek 均采用该 transport。其他 transport、未知版本、隐藏输出/思考参数安全停止。版本检查不是二进制真实性校验，升级/自定义构建需重新审查源码证据。
- 不推断 reasoning Token 计费语义，不把最大输出等于实际 Token 或费用。现有推理超时仍有限；新增版本/配置预检各有 15 秒界限，不能声称所有预检加推理共享一个新增总超时。

## 修改范围

- 合同与执行：`src/agentflow/{contracts,serialization,authorization,resource_budgets,runner,service,database,cli,opencode_adapter}.py`。
- 新证据：`tests/test_reviewer_resource_budgets.py`、`tests/resource_budget_fixtures.py`、`tests/verify_installed_opencode_output.cjs`。
- 既有测试适配：`tests/test_{core,cli,opencode_adapter,local_ollama_reviewer,remote_reviewer,remote_worker,runner,invocation_audit,budget_honesty,run_consistency,readonly_schema}.py`。加入显式虚构能力与配置解析替身，不删旧断言；旧非预算测试隔离输出预检，新预算测试使用实际预检函数与受控 subprocess 替身。
- 规范：requirements、mvp、architecture-decisions、task_plan，新增 MODEL-15/AUTH-10/TASK-09–11/STATE-06、AD-46/47、MVP-A34–36；同步仓库 canonical Skill，不更新 installed Skill。
- 本报告；用户交接文件保持原文且未加入提交。没有改打包依赖或安装配置，未运行联网构建/安装。

## 验证记录

| 命令 | 实际结果 |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 -m unittest discover -s tests -q` | 最终 465 tests，31.761s，OK；442 基线 + 23 新测试方法，包含多个参数子场景。前一轮 465 tests/31.549s 也通过。 |
| `node tests/verify_installed_opencode_output.cjs /Users/fredafrica/.opencode/bin/opencode` | exit 0；上述三组源码表达式断言通过，real_model_calls=0。 |
| `python3.11 -m compileall -q src tests` | exit 0。 |
| `git diff --check` | exit 0。 |
| Node 只读编号/引用/新增文件断言 | exit 0；111 个需求定义、36 个验收定义、47 个决策定义唯一；A34–36 需求引用有效；6 个未跟踪文件均无行尾空白且有末尾换行。 |
| `git branch --show-current` / `git rev-parse HEAD` | 仍为本报告基线分支与提交；未提交。 |

文档脚本首次误把历史验收结果表的编号引用也当成定义，触发重复断言；限定到验收定义章节后检查通过，没有删除历史证据。限定范围的 simplify 检查仅补全类型标注、去掉重复模型字符串，不改变行为。没有安装额外 lint/type-check 工具，也未把未运行的检查算作通过。

无真实模型调用、无 API 费用、无子 Agent、无提交/推送/合并；无全局配置或业务数据库变更。

## 待独立验收与合并前检查

1. 由不同模型执行自测与独立审核，重点检查授权冻结、四路径最终输出、fallback/续接、UNKNOWN/旧计划恢复及端点权限不回归；当前没有独立批准。
2. 复核真实候选模型的 output 来源和版本，准备新计划版本/哈希，不把 fixture 数值带入真实注册表。
3. 如 Owner 另行授权最小 smoke：选已验证本地模型、小 packet 和明确低输出授权，确认首轮提前完成及一例多轮 review/rereview；记录真实最终请求/使用量。远程收费验证必须另获精确模型和费用授权，本轮不运行。
4. 升级 OpenCode/transport 前重新取证，验证最终 SDK/HTTP 参数与 reasoning 行为；验证完成前保持失败关闭。
5. Owner 决定后再提交/合并/部署。里程碑 20 的历史独立批准不能继承为本次修改的批准。
