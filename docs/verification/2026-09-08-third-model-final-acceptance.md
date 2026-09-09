# 第三模型独立测试最终验收（BUG-RB-01 / BUG-RB-02 / 输出终止审计 / 审核隔离）

状态：PASS（本轮独立验收最终通过）

## 1. 本报告测试模型身份

- 测试模型/审阅者身份：第三模型（独立测试员），运行于 OpenCode 会话，模型 `deepseek/deepseek-chat`。
- 模型版本：确切的二进制/快照版本未在环境中可核查，如实标注为**未知**；不编造版本号。
- 测试模式：完全本地、确定性、无 API 费用、未启动子 Agent、未调用任何其他模型。
- 历史真实 Qwen 会话证据（`2026-09-08-qwen-review-gates-passed.md`、会话 `ses_f7b9c1873ffe4wUftZP9SDeH1N`、审核 `831bb174-...`）仅作为**先行的独立审核证据引用**，本报告 PASS 不以该证据作为自身通过依据，而基于下列可复核的本地执行与本报告作者对生产调用链的独立阅读。

## 2. 工作目录 / 分支 / HEAD / 候选内容校验

- 工作目录：`/Users/fredafrica/Documents/multi-model-agentflow-ollama-reviewer`
- 分支：`codex/reviewer-budget-fixes`
- HEAD：`b8885939ec084f878bb9fa49ea152b1157bd6f3d`（git rev-parse 实测）
- 阶段暂存（staged）：0；存在未提交修改与未跟踪文件（正是本轮候选）。
- 候选完整工作树（未提交+未跟踪）为本报告验收对象；它包含 `src/agentflow/resource_budgets.py` 与新增测试 `test_reviewer_resource_budgets.py` / `test_output_termination.py` / `test_lmstudio_review_isolation.py` / `resource_budget_fixtures.py`（均当前为准）。
- 内容不变性证据：测试开始前对 `src/` 与 `tests/` 共 43 个 py 文件生成 SHA-256 快照（`.sha` 文件自身 SHA-256 `f12575bc…8276429`）；全部回归与新增测试完成后重算并 `diff`，结果 **IDENTICAL: candidate src+tests unchanged during verification**。因此可确认本轮运行中生产代码与既有测试未被改动；非阻断诊断仅新增于临时目录并已清理，未污染候选。

## 3. 实际执行命令与结果

| 命令（workdir 均为项目根） | 退出码 | 结果 |
| --- | --- | --- |
| `git branch --show-current` / `git rev-parse HEAD` | 0 | `codex/reviewer-budget-fixes` / `b8885939…` |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 -m unittest discover -s tests -q` | 0 | Ran 480 tests, OK（本项目既有基线 480 项） |
| `python3.11 -m unittest tests.test_reviewer_resource_budgets tests.test_output_termination tests.test_lmstudio_review_isolation -v`…（隔离侧重配四范围，含 BUG-RB-01/02、A+B、审核隔离） | 0 | 38 tests, OK |
| 我的独立边界脚本（新增临时文件 `/var/folders/3r/…256n/T/opencode/test_third_model_scratch.py`） | 0 | 8 tests, OK（真实生产函数/解析路径） |
| `python3.11 -m compileall -q src tests` | 0 | 通过 |
| `git diff --check` | 0 | 通过 |
| 候选前后 `sha` 快照 diff | 0 | IDENTICAL |

输出细节：回归第 2 次重跑确认 `Ran 480 tests in 36.738s OK`（同 Qwen 记录的 480/35.4s 数量一致，未引用旧结果）。新增独立测试 8 项逐一运行并二次记录 exit 0。

## 4. 四项范围各自的测试与结论

### 4.1 BUG-RB-01 审核步骤预算
- 阅读与直接验证点：
  - `src/agentflow/contracts.py`：`DEFAULT_REVIEW_MAX_STEPS=8`、`REVIEW_MAX_STEPS_LIMIT=32`；`TaskContract.__post_init__` 中 `review_max_steps` 经 `_coerce_range(.,2,32,…)` 检查，缺省 8、合法边界 2..32（与先前 `implementation_max_steps` 独立字段，两者哈希进入 canonical 与授权）。
  - 数据类型：`_coerce_range` 严格 `isinstance(value,bool) or not isinstance(value,int)` → 拒绝；禁止 bool/float/str/null/0/负值，因 Python `True==1`/`8.0==8` 不产生隐式 int 转换。
  - 传递与二次一致：`opencode_adapter.py` `invoke(OpenCodeAdapter)`(LM Studio 路径 `_review_steps`)、`RemoteOpenCodeReviewerAdapter`/`LocalOllamaReviewerAdapter._permission_config(...steps=DEFAULT_REVIEW_MAX_STEPS)`，及 `_verify_local_config_consistency(..., steps=DEFAULT_REVIEW_MAX_STEPS)`、`_prepare_output_environment` 里对 resolved agent steps 严格 `type==int` 且与期望相等；请求局部传参（`service.py` 把 `{**request.metadata,"review_max_steps":…}` replace），不污染共享 adapter 实例。
  - Runner/service 边界：`service.py` 拒绝 metadata 里 `review_max_steps` 不是 int 或与任务不符；越界在推理前抛 `ValueError`/`review_max_steps must be an integer`。
- 行为测试：新增 module `test_reviewer_resource_budgets.py` 的 ReviewerStepBudgetTests：配置消耗替身的 3 轮/单轮步骤 stub、2/8/12 合法传到实际配置、预算 2<3 耗尽提前确认、耗尽不生成 review row 且 resume 不重发。全部通过。
- 结论：独立 review 预算、默认/边界、类型、传递与耗尽语义满足 TASK-09/AD-46。

### 4.2 BUG-RB-02 输出能力与授权
- 阅读并直接验证：
  - `ModelRecord` 追加 `max_output_tokens/capability_source/capability_source_version/api_model_id`（`serialization` 通过新增字段往返、`__post_init__` 严格 `_coerce_range(1..2^31-1)` 与 `_validate_capability_source`），snapshot `ModelCapabilitySnapshot.ref/context_length/max_output_tokens/source/source_version/api_model_id` 进入 canonical/plan_hash（PlanContract dataclass 序列化，稳定排序）。
  - `resource_budgets.freeze_capability`/`invocation_budgets`/`validate_invocation_budgets`：`effective=min(ability_snapshot, role_authorization)`；role 输出授权 `implementation_max_output_tokens`(impl/revision) 与 `review_max_output_tokens`(review/rereview) 独立，缺省均 16000，其中能力/来源/别名/configured steps/role 在授权/审计中成对绑定，任何一种变化都改变计划哈希。
  - authorization 门禁 `issue_authorization` 不要求带 capability 快照的模型（`model_capabilities`）缺项时拒绝——AUTH-10 等守护在计划缺快照时禁止授权。
  - 能力拒绝：能力下降/来源失效（catalog 无输出现查/`current<freeze`）在推理前抛 `ProviderNotConfiguredError`；能力增长不自动放大 effective（修复后同值）。大于 65536 但有授权的 fixture（384000）不会因硬编码 65536 截断。
- 我用虚构 fixture（显式测试替身，不加载真实大模型）：标称 context=262144/output=65536 配角色授权 16000→effective=16000，context 仍独立；同模型配 100000→65536 cap 上限，不会超越 capability。断言如 4.6 记录。
- 未知能力/只有 context 或 model 列表 → 保守暂停不 inference（未通过 `debug config` 也验证不了）。真实输出覆盖带 alias 精确模型，并写子进程 `limit.output=effective` + 同时写 `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`；解析后二次核对 model output limit 与 alias/agent steps/permission，任何权限/端点/transport/未知输出/thinking 覆盖在推理前失败；且不打印值（仅名）。结论满足 MODEL-15/TASK-10/11/AD-47。

### 4.3 A+B 输出终止与失败审计
- 直接阅读（最关键不是解析器，而是 service/DB 链路）：
  - `parse_opencode_json` （`opencode_adapter.py`: 2169-2248）先提取 usage/session + terminal_reason；`terminal_reason=="length"` → `failure_kind="output_limit_reached"` 无条件抛 `InvocationIncompleteError`（即使文本是合法 approval JSON）；无文本且无其他 failure_kind → 抛 `InvocationProtocolError(result=result)`（保留结果）。中间 `length` 后确有最终 `stop` 且有效文本流不按最终耗尽处理。
  - `service.InvocationService._invoke`：`InvocationIncompleteError` → `fail_call(call_id,result,run_id,failure_kind=error.failure_kind)`（保留 usage/session）；`InvocationProtocolError` → `fail_call(... failure_kind='protocol_error')` 并 raise。
  - `Database.fail_call`：把 `state→FAILED` 并把 result 写入行、留存 output_text/usage/session/cost、记 call.failed 事件；`output_limit_reached`/`protocol_error` among FAILED 在 `nonretryable_output_failures(run_id)` 中被计数。
  - resume 阻断：`runner.resume` 在 UNKNOWN 检测后先查 `nonretryable_output_failures` → 有任一即抛 ValueError 禁止自动重发/续接/fallback（`runner._execute` 对 output_limit 分支也单独暂停不继续）。
- 关键确认点（QA-13 + my scope）：
  - model_calls.state=FAILED；output_text 保留（合法 JSON 文本仍在 result）；直接调用该链路可观察计数。
  - 缺失 token field 与显式零 token 的区分：事件解析把“输出 token 显式 0”与“缺失 input/output key”区分——缺失则未确认 → `token_source="unavailable"` + `usage_unavailable=True`；显式 0（如 reasoning=0 已确认），令牌不过度累计。对失败路径 retain committed evidence；明确零与未知不做掩盖。
- 执行的可观察 result：`test_output_termination.OutputTerminationTests`：`length` 对 `'', 'partial', '{"approved":true,...}'` 均抛 `InvocationIncompleteError(failure_kind='output_limit_reached')`，usage 保留在 result；`test_local_exit_paths...` 对不同退出码+reason 保保留 usage；OutputTerminationRunnerTests 检查实际临时 DB `model_calls` 行为（state=FAILED、input_tokens=19802 保留、remote_cost=0、raw reasoning 保留、request_scope 的 effective=16000）；resume 抛 ValueError，调用数不再增加。全部通过。
- 另：`nonretryable_output_failures` 返回**正确计数**在 runner/pause 流程中作为 observable（DB 层计数含 protocol_error 与 output_limit）。结合 `test_length_pause...`/`test_no_text_pause...`、`test_implementation_*_does_not_retry_or_fallback`（本会话重跑通过）为证据。

### 4.4 LM Studio / 审核隔离
- 直接阅读：
  - `OpenCodeAdapter.invoke` (LM Studio) instance门禁：non-`lmstudio` provider、`is_local`位、review/rereview 无 `read_only` → 拒绝（prov先）；prompt packet-only。
  - `_permission_config(read_only,…,steps=…)`：写进子进程 `OPENCODE_CONFIG_CONTENT` 的 overlay——先设全局 `*: deny` 默认与（review 时）`_reviewer_deny_permission()`（一条独立 deny 规则）合并，构造两层 deny；LM Studio review 启动子进程前会写入此 config。
  - 解析后二层复核：LMStudio 经 `_prepare_output_environment`（`src/agentflow/opencode_adapter.py` 1588-1666）调用 `opencode debug config --pure` 解析**实际** effective config，对 review/rereview 检查 resolved global `permission` 全 deny（`_verify_deny_permission`），再逐条检查 resolved agents 中 permission 与授权 overlay 完全相等、steps 为严格 int 且等于授权；任何非 deny / allow / 含糊 / read-grant 被覆盖都在推理进程（Popen）前抛 `ProviderNotConfiguredError`。（本地 Ollama 使用另一处 `_verify_local_config_consistency` 对 endpoint/transport/enabled_providers/steps/permission 执行同样前置一致检查。）
  - LMStudio review 必须 `read_only=True`，同 Ollama `invoke` 前 `request.role in review/rereview and not request.read_only` → 拒绝。
- 行为 test（LMStudioReviewIsolationTests）：review/rereview 在两层不能给 stale 文件读权；写使 review 请求在 discovery 前被拒；managed 全局或 agent read grant 在推理前拒；implementation/revision 既有文件权限保持（未被 review 的无工具化误伤）；版本诊断输出 identifier 而 `_reject_output_overrides` 只将“名字(≤64char)不打印值”；该 test 全通过。source 阅读确认实现确实走该链（未测 `--version` 非 1.18.29 时先行拒绝= `_read_output_config`）。版本为 AD-47 记录的二进制 SHA-256 未重复读二进制（尊重前报告；此项不影响结论）。

## 5. 独立新增边界测试（新增于临时目录，将候选保持不变）
我在仓库外 `/var/folders/…/opencode/test_third_model_scratch.py` 写了 8 个测试，检查真实生产函数而非源码字符串或 mock 自身：
1. `ParsingPathTypeParityTests`：JSON 解析路径 与 构造函数 对 review_max_steps/review/impl output 参数在 bool/float/str/null/0/负/越界时都以同样严格失败而不隐式 int()（含 True/False/8.0/`16000.0`/`'8'`/None）；合法边界 2/32/缺省 8/16000 正常解析。
2. `CapabilitySnapshotParsePathsTests`：capability max_output_tokens 对 bool/float/str/0/负/越界 拒绝；context 与 output 独立（None context 允许）；source version 非空且合法、非法 souce 拒绝。
3. `OldPlanAndAuthorizationGuardTests.test_old_fieldless_plan_reads_with_defaults_but_cannot_authorize`：去掉新字段的旧计划仍可规范化读取并给出 hash（符合读取语义），但 `issue_authorization` 因 capability 快照缺失而抛 ValueError（新守卫 AUTH-10/AD-47 生效），旧计划无法沿旧授权自动升级预算。
上述 8 项运行 exit 0。测试只临时存放，不改仓库。

## 6. 未执行项及其对结论的影响
- 没有对真实模型 provider 做任何推理或收费调用；也不需要（本轮已存在缺省替身证据与文档化真实 Qwen 会话做 A/B 佐证）。有缺失项均明确不影响三模型独立结论：未读取/验证历史二进制时，OpenCode `1.18.29` 精确版本门由 `_read_output_config` 执行（被本地替身覆盖为 1.18.29 验证）；不启动子 Agent（符合项目边界）；不改全局安装/配置/不提交/推送（符合任务与 AGENTS）。
- 没执行对 `.agentflow` 业务数据库（含真实 run/authorization 与 token 历史）的任何写迁移，也未核销历史 UNKNOWN；只读临时实例/解析验证旧暂停恢复是被拒绝的行 140；不rewrite历史数据符合不可破坏边界。

## 7. 结论
- 现有回归（480）通过，四范围各自定向 module（38 tests）通过，独立补充边界测试（8 tests）通过与源码调用链按需求逐点关阅读。
- 四项范围与 QA-08/09/10/13/14/TASK-09/10/11、MODEL-15、AUTH-10 已满足；无未解决真实阻断缺陷。
- 候选内容在测试前后逐文件 SHA-256 校验不变。

**第三模型独立测试通过。结合已保存的 Qwen 独立审核证据，本轮 BUG-RB-01、BUG-RB-02、输出终止审计及审核隔离修复工作验收完成，无需追加模型或重复验收。提交与合并由 Owner 另行决定。**

## 8. 非阻断建议（不影响结论）
- 无需修正阻断项；下列为未来可选改进，不构成新增门槛：
  1) 独立安全审查可再增加一条 query 断言 `nonretryable_output_failures` 的计数与 `nonretryable_output_failures` 在合并暂停/UNKNOWN 特殊流程中的边界（当前 runner 用阻塞即止，已在可用场景覆盖，为保持三层最小差异的建议）。
  2) 可选把 OpenCode 版本门控改为从已验证 release 字符串列表（当实际多次升级时由 Owner/验证按需升级支持范围），当前固定 `1.18.29` 满足 AD-47 且未放宽。
- 建议属 P3 级、不影响 PASS。

报告完毕。
