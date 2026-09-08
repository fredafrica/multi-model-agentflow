# 本地 Ollama Reviewer 收尾执行提示词

> 执行者：用户手动选择的 DeepSeek V4 Pro。主管、验收方案制定者和最终独立复核者：Codex。按本文串行执行，不启动子 Agent；本文件替代旧会话交接提示词。用户已要求你完成修改、自测和交付，不必为本文范围内的常规修复反复请求确认。

**Goal:** 修复现有本地 Ollama packet-only Reviewer 实现，补齐行为测试与文档，在不回归既有路径的前提下交付给 Codex 复核。

**Architecture:** 保留 `LocalOllamaReviewerAdapter`、最小公共 Reviewer 进程执行函数、`AdapterRouter` 的 provider + role 路由和既有 Runner/Service/数据库。修正实际运行端点验证、模型元数据和调用证据；不新增控制平面、注册表、供应商直连推理通道或数据库 schema。

**Tech Stack:** Python 3.11+、标准库 unittest/mock、SQLite、Git worktree、OpenCode 子进程；零真实推理的自动化测试。

**Spec:** 项目 `AGENTS.md`、`docs/requirements.md`、`docs/mvp.md`、`docs/architecture-decisions.md` 和本提示词中的本次 Owner 委托裁定。

## 1. 工作位置、授权及终点

项目根：`/Users/fredafrica/Documents/multi-model-agentflow-ollama-reviewer`。

现有独立 worktree 分支：`feat/ollama-local-reviewer`；核查时 HEAD 为 `0545b64c0338f363c0aca862c251f350f48aa228`。留在这个 worktree 和分支，不新建或切换分支，不覆盖已有修改。

本次由用户启动的 DeepSeek V4 Pro 会话是被指定的实施者，可以读下列任务材料、修改白名单文件、运行确定性测试。旧提示词中“不得调用收费/远程模型”的开发测试限制，不解释为禁止用户已经手动选择的本实施会话；它仍禁止你另外启动真实模型、调用推理 API、自动切换模型或进行 smoke。本次不要求通过 AgentFlow 再启动你自己，也不需要为了 mock 测试给真实模型生成授权计划。

按用户最新分工：你完成实施并运行自测，Codex 已制定独立验收条件，之后再独立复核。无需另找第三个模型才能开始或运行确定性测试。不得自己代表 Codex 给出最终审核批准。

完成后输出 `READY_FOR_CODEX_REVIEW`，提供有证据的交付说明；Codex 复核通过后本工作才最终关闭。不得以措辞、格式、非实质重构或美化为由自行追加返工轮次。

当前采取保留未提交 diff 的交接方式：不 commit、不 push、不 merge，不更新全局安装、canonical/installed Skill、OpenCode 或 Ollama 全局配置。旧最初任务曾允许开发 commit，后续交接已明确不提交，本次沿用后者。

## 2. 已核实的基线：不能把历史陈述当作当前事实

Codex 于 2026-09-08 约 04:19 PDT 检查实际工作区，并用 Python 3.11.16 复跑：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m unittest discover -s tests -q
```

结果：408 tests，35.336 秒，1 failure、5 errors，退出 1。其中旧有 388 项通过；新增文件有 20 项，14 通过、1 失败、5 报错。这里的通过仅为确定性测试结果，不是独立正式审核通过。

现有改动是 4 个源码文件（404 additions / 111 deletions）以及 343 行的新测试文件。不要再把新测试文件称为只有 `placeholder`，也不要假定 4 个源码文件已经正确完成。

### 2.1 已确认、必须关闭的缺陷和缺口

| ID | 实际证据 | 本次处理 |
| --- | --- | --- |
| H01 | `LocalOllamaReviewerAdapter.discover()` 捕获 `ReviewerUnavailableError`，模块却没导入它，模型发现错误时触发 NameError | 补导入，并测试失败分类 |
| H02 | `ollama_host_is_loopback('::1')` 返回 False；`127.999.1.1`、`127.evil.example.com`、`ftp://localhost`、`[::1`、非法端口反而返回 True | 用标准库严格解析，按第 5 节验收 |
| H03 | 仅检查构造时的 `OLLAMA_HOST`；`_permission_config()` 没有 provider 端点绑定，共享执行器重新复制环境 | 实际 OpenCode 有效配置必须验证并绑定，不能以环境变量替代实际调用地址 |
| H04 | 无 planned_models 时把所有发现的模型 family 写为 `gpt-oss` | 使用计划元数据；未知 family 保持 None，不猜测 |
| H05 | `parse_opencode_json()` 成功但无 Token 事件时给出数值 0，缺少 `usage_unavailable` / `token_source` | 保持现有数值字段兼容，用审计元数据区分未知与确认零 |
| H06 | 新测试引用未定义 `_completed_process`，实际只定义了 `_CompletedProcess` | 修正 helper；此项造成当前 4 个 errors |
| H07 | 名为“所有工具禁用”的测试只断言目录不可写，没有检查捕获的 permission 配置 | 增加全局和 agent 两层的实际配置断言 |
| H08 | 新测试末尾存在无用 `_completed_config` / `_CAPTURED_CONFIG` 和未完成章节标记 | 清理半成品，并补齐后半段测试 |
| H09 | 本地 Reviewer 的失败、UNKNOWN、路由集成、哈希授权和正式 JSON 审核闭环缺乏完整新测试 | 按第 8 节测试矩阵交付 |
| H10 | requirements/MVP/AD-45/本次里程碑/模型资料尚未更新 | 按第 9 节修改；不重写无关历史 |

### 2.2 已确认的环境事实及未证实的说法

- 本机 `opencode debug config --pure` 帮助明确支持查看 resolved configuration，`--pure` 表示不加载外部插件。
- Codex 只在内存过滤该输出后，核实当前 `provider.ollama.npm` 为 `@ai-sdk/openai-compatible`，`options.baseURL` 为 `http://127.0.0.1:11434/v1`；其中配置了 `gpt-oss:120b`，没看到该模型的 baseURL override。这是一次配置快照，不能证明模型当前已加载或可推理。
- Qwen 两个会话反复产生空参数工具调用，最终本项目进程被中止。上下文很长是观察到的伴随现象；“一定由于上下文耗尽导致截断”“一定是 OpenCode 环境坏了”均未有充分根因证据。本次不修 OpenCode、Qwen 模板或模型运行时。
- `task_plan.md` 的“当前 239 项”和旧 Goal/Next Step 落后于当前代码。388 项通过不能单独证明里程碑 19 的所有历史独立审核都已结束，不要自行改成 REVIEW_PASSED。
- 项目内目前没有现成模型资料文件，也未找到可直接交付的两次 smoke 原始产物。第 9 节给出历史用户提供的资料，按来源如实记录即可，不要跨项目翻找数据库或重跑 smoke 补证。

## 3. 输入材料、文件权限与运行条件

先完整读 `AGENTS.md`、requirements、MVP、architecture-decisions、`task_plan.md`。然后读当前 diff 和下列直接相关函数；不要把全部会话历史、整个数据库或无关源码反复读入上下文。

| 允许修改/新增的精确路径 | 职责 |
| --- | --- |
| `src/agentflow/opencode_adapter.py` | 端点解析、本地 Reviewer、最小公共 Reviewer 执行、相关 usage 解析 |
| `src/agentflow/adapters.py` | 保留通用 provider + role 拒绝语义 |
| `src/agentflow/cli.py` | 本地 Ollama Reviewer 装配和角色前置拒绝 |
| `src/agentflow/runner.py` | 必要的路由异常衔接；维持既有审核、暂停、fallback 规则 |
| `tests/test_local_ollama_reviewer.py` | 完成本次端点、适配器、集成验收 |
| `tests/test_opencode_adapter.py` | 共享 usage 解析的必要回归 |
| `tests/test_remote_reviewer.py` | 共享执行重构的必要回归 |
| `tests/test_adapters.py`、`tests/test_cli.py`、`tests/test_runner.py` | 如在既有对应文件落测试更清楚，可增加直接相关用例；无需为了分文件迁移测试 |
| `docs/requirements.md`、`docs/mvp.md`、`docs/architecture-decisions.md` | 同步规范、验收编号、AD-45 |
| `task_plan.md` | 当前任务、里程碑 20、真实检查结果和短交接记录 |
| `docs/models/ollama-gpt-oss-120b.md`（新建） | 唯一的人工可读候选模型资料，不作为运行配置源 |
| `docs/verification/2026-09-08-ollama-reviewer.md`（新建） | 自测交付报告和验收矩阵映射 |

以上目录如不存在，可以创建。`src/agentflow/contracts.py`、`serialization.py`、`authorization.py`、`policies.py`、`service.py`、`database.py`、`states.py`、`workspace.py` 以及所有现有 tests 可以只读查看和执行；当前方案不需要修改这些文件或增加表/计划字段。

`findings.md` 和 `progress.md` 保持原样。本次核查 SHA-256：

```text
findings.md 221c1070de0d29cb3724df2b308b39c5124886fad5d47a37a034543d63be8e5a
progress.md 001d24adaf7269ba384b3b621cf606010b6a8caf6e171a624d5f0530326e3e68
```

本提示词是主管输入文件，保持只读。`AGENTS.md`、README 系列、Skill 文件、其他 worktree、用户级 AgentFlow 工作区、TASK-011 业务实现与历史验收记录均不修改。

可以在 `tempfile.TemporaryDirectory()` / `mktemp -d` 创建的独立临时目录中生成 mock/stub、测试仓库、SQLite、pycache、构建副本、wheel 和临时 venv。测试为了验证 GitWorkspace 可以在这些新建测试仓库内 init/commit；“不 commit”针对实际项目分支，不禁止测试夹具需要的临时 Git 操作。

所有模型发现、配置解析、Popen 调用在普通自动化测试中都必须 mock 或指向本次创建的 stub。测试不得使用真实 OpenCode run、Ollama generate/chat、模型下载或加载。本机已存在 Python 3.11.16、setuptools 82.0.1 和 uv；无需增加依赖。

## 4. 已裁定的兼容性与字段语义

1. Ollama 在本扩展中只支持 `review` / `rereview`，`ModelRef.provider='ollama'`、`is_local=True`。远程端点失败关闭；不在本次自动转为远程模型。
2. `gpt-oss:120b` 只是资料和测试示例。通用核心不得硬编码其名称、family、版本或量化。planned_models 里的 ModelRef 原样保留；无计划发现的模型 family 为 None，version 继续沿用既有“model_id 作为未验证候选标识”的惯例，不能声称是精确已验证权重快照。
3. 使用现有 ModelRef / ModelRecord / PlanContract；本次不增加 `ollama_host` 计划字段或修改 schema。provider/model_id/version/family/is_local 原本就进入 canonical JSON；改变它们必须让 plan_hash 变化并使旧授权失效。
4. 端点是适配器运行配置，不是“因为没有进计划就可随意改为远程”。每次实际调用重新验证 resolved configuration，本次只放行确定性本地端点；远程/无法证明/冲突都拒绝。
5. 发现到配置模型不等于服务在线、更不等于已加载。`opencode models` 只给 `DISCOVERABLE`，`available=False`、`trust_level=UNVERIFIED`；不自动提升为 `CALLABLE_VERIFIED`。没有服务加载证明，不增加加载状态承诺或新的探测 API。
6. 不创建自动 fallback。既有 Runner 仅在计划授权的 `fallback_model` 上继续按现有策略工作；本次测试该既有路径不会错用审核专用模型，也不会绕过独立性。未配置 fallback 时不挑替代模型。
7. Reviewer 固定 steps=2，默认 timeout_seconds=900，继续独立于 implementation 的 steps/timeout/continuations。审核角色不续接。
8. InvocationResult / 数据库 Token 数值字段保持现有类型，不改为 None。无证据时零只是兼容存储值，必须伴随 `usage_unavailable=True`、`token_source='unavailable'`，报告不得把它称为确认消耗 0 Token。明确报告合法 input=0/output=0 时才是确认零。
9. 本地已验证调用 `remote_cost=0.0`、`cost_unavailable=False`；缺失 Token 不等于费用未知。本地零远程费用不包含机器、电力或当前人工启动的 DeepSeek 实施会话成本。
10. packet-only 表示应用权限禁用、空只读工作目录和最小 prompt；不是 OS 级“进程绝对无法读取任意文件”的保证，不新增容器或操作系统沙箱。可信本机 Ollama 服务是运行前提；本次不设计防恶意本地代理转发的网络隔离系统，也不把 Ollama Cloud 注册为本地候选。

## 5. 工作单元 A：端点解析和实际运行配置绑定

文件：`src/agentflow/opencode_adapter.py`、`tests/test_local_ollama_reviewer.py`。

- [ ] 先增加 H02/H03 的失败测试，再修复。

保留 `ollama_host_is_loopback(raw: object) -> bool` 的对外接口；可新增模块私有 `_normalize_ollama_endpoint(raw: object) -> str`，非法输入抛 ValueError，布尔接口捕获并返回 False。用 `urllib.parse.urlsplit` 与 `ipaddress.ip_address`，不要自行数点或仅看第一段 127。

解析合同：

- None、空字符串或纯空白：纯函数默认值为 `http://127.0.0.1:11434/v1`；但这不代表真实 OpenCode provider 可缺省或未配置，实际配置门禁另外检查。
- 仅接受字符串或上述 None；布尔、数字、容器拒绝，不能先 `str(raw)`。
- 支持 http/https URL、裸 IPv4/localhost、host:port、裸 IPv6、方括号 IPv6 + 可选端口。裸 IPv6 必须先整体用 ipaddress 识别，不能 rsplit 冒号当端口。
- host 仅接受大小写无关的精确 localhost、合法 IPv4 127.0.0.0/8、合法 IPv6 ::1；localhost 规范化为数值 127.0.0.1，以免判定后又依赖 DNS。expanded IPv6 ::1 等价形式可按 ipaddress 规范化。
- 拒绝其他域名、localhost.、子域、IPv4-mapped IPv6、IPv6 zone id、缺括号/多余括号、非 http(s) scheme、userinfo、query、fragment、反斜杠、内部空白/控制字符、非法/空端口；显式端口必须十进制 1–65535。
- 缺 scheme 默认 http，缺端口默认 11434；缺路径或根路径补 /v1，显式非空路径保持其语义（不要只验证时删除、请求时又发往另一个地址）。支持本地 URL 的 /v1/api 等自定义路径，但不承诺这种路径一定有可推理服务。
- URL 解析错误返回 False 或转换为受控配置异常，不能泄漏异常堆栈或把含凭据的 raw 地址插入报错。

代表性行为测试：

```python
def test_loopback_boundary(self):
    positives = (None, '', 'localhost', 'LOCALHOST:11434',
                 '127.255.255.254', '::1', '0:0:0:0:0:0:0:1',
                 '[::1]:11434', 'https://[::1]:11434/v1',
                 'http://127.0.0.1:11434/v1/api')
    negatives = (True, 127, [], '127.999.1.1', '127.evil.example.com',
                 'localhost.evil.example', 'localhost.', '0.0.0.0',
                 '192.168.1.5', '[::ffff:127.0.0.1]:11434', '[::1',
                 'ftp://localhost', 'http://localhost:abc',
                 'http://localhost:0', 'http://localhost:65536',
                 'http://localhost@evil.example',
                 'http://user:pass@localhost', 'http://localhost/?x=1',
                 'http://localhost/#x')
    for value in positives:
        with self.subTest(value=value):
            self.assertTrue(ollama_host_is_loopback(value))
    for value in negatives:
        with self.subTest(value=value):
            self.assertFalse(ollama_host_is_loopback(value))
```

- [ ] 实际端点绑定按以下路径实现，不留成“检查 OLLAMA_HOST 就完成”。

给共享 `_run_opencode_packet_review` 增加可选的准备环境回调，例如 `prepare_environment: Callable[[Path, dict[str, str]], None] | None = None`。先创建空临时 review_root、设置只读权限、建立 environment，再在 Popen 前调用回调。本地适配器传入该回调；远程 Reviewer 不传，保持原有环境与路径。移除共享函数中没有使用的 `opencode_command` 形参属于允许的局部清理。

本地回调的确定流程：

1. 在同一个 review_root 和准备给推理进程的 environment 下，以参数数组执行 `opencode debug config --pure`，`cwd=review_root`、capture_output=True、timeout=discovery_timeout_seconds；不把返回的完整配置写入日志或模型上下文。
2. 只在程序内解析配置，读取 `provider.ollama`、npm、options.baseURL 和所选 model 条目。适配器支持已验证的 `@ai-sdk/openai-compatible` 路径；缺 provider/npm、未知 transport、非法 JSON、解析不出确定端点都以 `ProviderNotConfiguredError` 拒绝。
3. 确认 provider options.baseURL 是严格回环。所选模型的 `options.baseURL` 或 `api.url` 若存在也必须验证：与规范化 provider 地址相同才允许；不同则拒绝，避免 endpoint precedence 歧义。不要把远程 override 覆盖掉后默默继续。
4. 显式构造参数 `ollama_host` 或非空 `OLLAMA_HOST` 若存在，必须也是严格回环，并与有效配置在规范化后相同；冲突拒绝。未设置环境变量时，以真实 resolved baseURL 为准，不凭空断言默认端点被 OpenCode 使用。
5. 将已经验证的所选 provider/model 端点显式写入该子进程的 `OPENCODE_CONFIG_CONTENT`（保留所选模型已配置的必要元数据）；同时写入完整全局/agent deny 配置、enabled_providers=['ollama']。localhost 改为数值回环。仅改子进程环境，不修改磁盘全局配置。
6. 对本地子进程删除大小写 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY，设置 NO_PROXY/no_proxy='*'；这只影响本地 Reviewer 子进程，远程适配器不受影响。
7. 用同样 cwd/environment 再做一次 bounded resolved-config 检查，只比较所选 transport、端点、enabled_providers、agent steps 和两层 permissions；如果高优先级配置覆写导致不一致或多出 allow 权限，拒绝。每次 invocation 至多这两次配置解析，不循环重试。
8. Popen 使用这份已验证 environment 和 `cwd=review_root`，--dir 同样指向 review_root。测试同时断言 cwd 和 --dir，不能依然让进程 cwd 留在仓库。

单独 `discover()` / `require_model()` 也需在临时目录中使用同一类配置验证；不要把模型发现列表当成端点验证。可以提取 `_prepare_local_environment` 复用，不复制两套校验。实现时通过 mock/stub 为 debug config、models 和 run 分别提供输出，避免全局 mock subprocess.run 把所有命令都误伪造成模型列表。

错误返回：无真实推理开始前的配置冲突/非法端点 → ProviderNotConfiguredError；OpenCode executable 缺失或配置发现命令不可执行 → UnsupportedProviderError；配置解析超时 → ProviderNotConfiguredError。错误只含稳定原因，不含完整配置、凭据或原始 URL。它们均属于 ReviewerUnavailableError 家族，推理 Popen 次数必须为 0。

参考（已由 Codex 核对）：OpenCode 使用 `provider.options.baseURL`，Ollama 示例采用 `@ai-sdk/openai-compatible`；配置是合并而非整体替换，managed 配置可高于 inline。因此最后的有效配置复核不能省略。

- https://opencode.ai/docs/providers/#ollama
- https://opencode.ai/docs/config/#precedence-order

## 6. 工作单元 B：发现、路由、调用结果和审计

- [ ] 修正 H01/H04。`discover()` 的端点/配置安全验证在可用性异常捕获外执行，不能吞掉安全拒绝；模型列表命令的不可用结果可为 planned_models 生成 UNAVAILABLE 记录。未提供 planned_models 且没有模型列表时返回空序列，不创造虚构模型。
- [ ] `require_model()` 拒绝其他 provider 和 is_local=False；有效配置下列表缺所选模型 → ModelUnavailableError。列表命中只允许进入一次调用尝试，不声明已加载。调用实际失败按下表处理，不触发下载、启动或换模型。
- [ ] `invoke()` 在任何配置发现前拒绝 implementation/revision 或 read_only=False（沿用 ValueError）；不要因新增 preflight 提前启动发现进程。
- [ ] 保留 CLI 的 ollama 分支仅注册 role_adapters[(ollama, review/rereview)]，不注册到默认 adapters；implementation_model.provider=ollama 在装配前拒绝（UnsupportedProviderError）。CLI 装配阶段错误发生在创建正式 run 前，不要求伪造一个 PAUSED run。
- [ ] AdapterRouter 的未知 provider 继续沿用 ValueError；已知只注册特定角色的 provider 用于其他角色时抛 ReviewerUnavailableError。默认 adapter 的既有解析顺序不变。
- [ ] Runner 的候选选择错误用已增加的 try/except 处理。已有 first_denial 若是 PolicyDeniedError，最终仍保留它，暂停原因可能是 policy_denied；若首个有效拒绝是 ReviewerUnavailableError，暂停原因是 reviewer_unavailable。不要为了测试一律改成后者而抹掉原始授权/预算拒绝。两者都必须 PAUSED，且审核专用 adapter 不接收写角色。
- [ ] 修复测试 helper 并删除半成品。最小替代为 `def _completed_process(stdout, returncode=0): return _CompletedProcess(stdout, returncode)`，或把所有使用统一为 `_CompletedProcess`。补齐 communicate/cancel 测试需要的 mock 行为，不调用真实 PID 的 kill。
- [ ] `parse_opencode_json` 增加与 failed/partial 路径一致的 `usage_unavailable`、`token_source`；复用轻量 Token 有效性检查，不能重复扫描/累加同一事件。对于明确合法 input/output=0 应识别为已报告；只有缺失或非法数据不能当确认零。数值证据继续按真实事件累计，已有 bytes 重叠合并规则不变。
- [ ] 本地 signal/KeyboardInterrupt/通信 OSError 等 UNKNOWN 若已有可解析 stdout，调用现有 failed/partial usage 解析构造 output='' 的 result，保留 Token/session/duration/termination_reason 后附在 InvocationOutcomeUnknown 上；没有证据则明确 usage_unavailable。不要为了本次修改重新设计 Worker 的异常路径。

### 调用分类合同

| 情况 | 适配器/核心结果 | 正式审核及恢复 |
| --- | --- | --- |
| 配置或模型不可用，推理未开始 | 对应 ReviewerUnavailableError 子类；Popen=0 | CLI 前置返回错误；已运行 Runner 安全暂停，无成功审核 |
| 本地非只读或写角色直调 | ValueError，配置发现/Popen 均 0 | 不允许调用 |
| 正常 OpenCode 事件 + 合法 approved:true/findings:[] | InvocationResult，local cost=0；Runner 解析合法审核 | 记录 review，满足其它门禁则批准任务 |
| 进程退出 0，有 text，但 text 是散文/fence/错类型 JSON | adapter 可返回 transport completed；Runner._parse_review 拒绝 | reviewer_output_invalid，无 review row；不要把已完成传输强改为 UNKNOWN |
| 正数非零退出，无步骤耗尽标记 | ReviewerProtocolError(result=failed_usage)，termination_reason=nonzero_exit | 调用 FAILED/protocol_error，reviewer_output_invalid；不得 fallback 重发 |
| 退出 0，无可用 text | ReviewerProtocolError(result=failed_usage)，termination_reason=no_usable_result | 同上，保留已确认 usage |
| 明确步骤耗尽，退出 0 或正数 | InvocationIncompleteError，failure_kind=step_limit_reached | FAILED，review_step_limit_reached，无 review row；不续接，resume 不重发 |
| 疑似非白名单步骤耗尽后缀 | 沿用 suspected_step_limit | 安全暂停，不把它当成功 |
| communicate 超时 | cancel/drain 有界；InvocationOutcomeUnknown(result=...)，termination_reason=timeout | UNKNOWN、unknown_model_call，空 output_text；resolve 前 resume 不重发 |
| signal 退出，即使已有步骤标记 | InvocationOutcomeUnknown，signal_terminated | UNKNOWN，不被降为普通已知失败 |
| 中断/通信故障，已启动进程 | 有界取消，InvocationOutcomeUnknown，保留可确认部分证据 | UNKNOWN，不猜测重试 |

共享执行器重构不能改变远程费用规则：远程有合法费用照实累计，远程缺费用仍为 unavailable；本地费用始终是已确认零远程费用。无 Token 不等于远程 cost=0。

## 7. 工作单元 C：真实 Runner 路径的无费用集成

在临时 Git 仓库和临时 SQLite 中，用 FakeAdapter 负责实现文件，用真实 LocalOllamaReviewerAdapter + stub subprocess 负责审核。实例必须 `test_double=True`，database 调用记录也必须为替身；不要用 FakeAdapter 冒充本地适配器已经接通。

可复用 `tests/test_runner.py` 的 `make_task`、`make_plan`、`approved_response`。用标准 discover 入口执行，或模块定向时配置 `PYTHONPATH=src:tests`，不要把顶层 helper 导入失败当产品缺陷。

基础构造参考：

```python
task = replace(make_task('ollama-review'), review_model=ollama_model())
plan = make_plan(tasks=(task,))
authorization = issue_authorization(plan)
adapter = LocalOllamaReviewerAdapter(
    planned_models=(task.review_model,), opencode_command='opencode-stub',
    test_double=True,
)
router = AdapterRouter(
    {'fake': FakeAdapter(responder=approved_response)},
    {('ollama', 'review'): adapter, ('ollama', 'rereview'): adapter},
)
# 使用现有测试 setUp 创建的 database/workspace；stub 分别处理 debug config/models/run。
result = Runner(database, router, workspace).start(
    plan, authorization, run_id='local-ollama-integration'
)
self.assertEqual(RunState.COMPLETED, result.state)
```

每次真实发生的 stub 调用使用唯一 session ID，不能让无关请求复用同一 ID 后撞数据库唯一约束。测试 UNKNOWN / step limit 后再次 resume，比较 invocation 次数和审核行数；不允许悄悄重发。

## 8. 完整验收矩阵

以下每行必须映射到实际 `TestClass.test_method`。可用 subTest 合并同类值，不追求凑测试数量；已有等价用例可引用，不必复制。

| ID | 覆盖情境 | 明确预期 |
| --- | --- | --- |
| O01 | 第 5 节正常/非法 IPv4、IPv6、localhost、scheme、端口、路径、类型 | 正确分类；调用 socket.getaddrinfo/gethostbyname 的 mock 一旦被触发即失败 |
| O02 | OLLAMA_HOST 本地，但有效 provider 地址远程 | 配置拒绝；没有推理调用，不把远程费用记本地 |
| O03 | 未设置 OLLAMA_HOST，但实际 provider 本地/远程/缺失 | 本地按实际配置继续；远程/缺失拒绝 |
| O04 | 显式 host 与 resolved 地址不同；构造后配置变成远程 | 调用前复核并拒绝，不使用陈旧构造快照 |
| O05 | 所选模型有 remote options.baseURL/api.url；未知 npm；配置 JSON 非法/超时 | 受控异常，无推理、无凭据回显 |
| O06 | 有效配置验证后被 managed override 改回远程或 allow 权限 | 最后一次 resolved 检查拒绝；不因 inline 配置看上去安全就放行 |
| O07 | 本地正常运行，父进程有代理环境 | 传入 Popen 的 provider/model URL 与校验一致；代理移除、NO_PROXY='*'；父环境不变 |
| O08 | 配置列表有模型/没有模型/命令失败，planned 和未 planned 发现 | discoverable/unavailable/空集合正确；available=False；不提升信任或冒充已加载 |
| O09 | planned family/无 planned 的非 GPT 模型 | family 原样保留/None；不能全变成 gpt-oss |
| O10 | review 和 rereview 正向；implementation/revision、非只读直调 | 前两者各成功；后三种在配置发现和 Popen 之前拒绝 |
| O11 | CLI 有效本地 Reviewer；ollama implementation；is_local=False；provider 未授权 | 正确 role adapter；其余前置拒绝且无推理/越权注册 |
| O12 | AdapterRouter 默认 provider、仅审核 provider、未知 provider | 既有默认路由不变；错误角色受控拒绝；未知 provider 保持 ValueError |
| O13 | primary implementation 不可用，fallback=ollama；revision 同理 | 写角色从未调用 LocalOllamaReviewerAdapter；PAUSED，原始拒绝原因可追溯 |
| O14 | 审核主模型不可用，计划内独立 Ollama fallback；未配置/不独立 fallback | 合法候选可用；其余等待审核且不换未授权模型 |
| O15 | Packet 和 process 隔离 | cwd/--dir 为临时只读非仓库目录；prompt 有 JSON-only 协议和最小 packet，metadata 中 worktree 不能泄漏到 prompt |
| O16 | 全局 + agent permissions、steps、enabled provider | 两层 '*' 及 read/glob/grep/edit/write/bash/shell/external_directory/webfetch/websearch/task/subagent/skill/question 均 deny；steps=2，仅 ollama |
| O17 | 有 Token、多 step、明确零、无 Token、只有 reasoning/字段错误 | 确认值准确累计；未知有标记；明确零不被误记未知；不改变远程 cost validation |
| O18 | 本地 cost 缺失/报告任意值；远程 cost 缺失/合法/非法 | 本地 zero/available；远程既有语义不变；数据库和成本汇总一致 |
| O19 | 正数非零退出、有 usage 无 text | FAILED/protocol_error，保留 session/Token/duration/原因，无 review row，不 fallback |
| O20 | timeout 含重叠 bytes、截断 UTF-8、无 usage、有 usage | UNKNOWN、部分哈希与 session/Token 保存，output=''，cancel/drain 有限，resume 不重发 |
| O21 | signal 与 KeyboardInterrupt/通信 OSError | UNKNOWN，已有 usage 保留；未确认的输出不能批准 |
| O22 | 明确步骤耗尽退出 0/正数；疑似标记；quoted 标记 | 前两类按已知失败/疑似暂停，不续接；引用里的普通文本不误触发终止 |
| O23 | 合法 JSON、fence、散文、数组、缺字段、错 bool、非法 severity | 仅合法协议生成审核记录；非法在 Runner 边界暂停 |
| O24 | approved=true 但 P0/P1；P2/P3 在两种接受策略 | P0/P1 总阻断；zero_findings 下 P2/P3 阻断；默认策略沿用当前行为 |
| O25 | B2/B3 同 family、缺 family；同实现者；修订贡献者试图审核 | 独立性规则拒绝，不能通过 model_id/fallback 绕过；合法跨族组合成功 |
| O26 | provider/model_id/version/family/is_local 分别变化、序列化往返 | 每项变化改变 plan_hash、旧 validate_authorization 抛 ValueError；往返哈希稳定 |
| O27 | 全量确定性测试、Supervisor、Worker、LM Studio、预算/隐私/恢复 | 原有 388 项不回归；新增验收全绿；无新增 skip 掩盖失败 |
| O28 | 构建/临时安装、文档编号、文件范围、敏感材料 | wheel/导入/CLI 成功；编号唯一引用有效；只修改白名单；无虚假 smoke/审核声明 |

注意 O25 所谓不同模型职责，是产品的 Reviewer 与实施者独立性；本次开发的确定性测试并不需要启动真实模型来证明。

## 9. 工作单元 D：文档与历史模型资料

- [ ] 在 requirements 增加 `PLAT-06`，描述本地 Ollama 经 OpenCode 的仅审核路径；增加 `MODEL-14`，描述实际回环端点验证、错误配置失败关闭和模型家族不可猜测。引用既有 AUTH-04/05/07/09、MODEL-13、COST-06、QA-03/08/09/12、STATE-05，不复制第二套状态定义。
- [ ] MVP 增加 2.8 本地 Ollama Reviewer 范围，以及 `MVP-A32`（端点/发现/角色/隔离/费用）、`MVP-A33`（调用失败/协议/UNKNOWN/恢复/独立性/授权）。验收结果只能在对应测试通过后更新为“确定性替身验收通过，待 Codex 独立复核”。原 A01–A31 历史记录保留。
- [ ] AD-45 记录本地 Ollama Reviewer：功能方向来自原始 Owner 任务；第 4–6 节细节注明“Codex 根据 Owner 于 2026-09-08 委托裁定的本次实现方案”，不要把技术细节虚构成 2026-09-06 Owner 原话。明确无新表/字段、回环失败关闭、共享执行、元数据和角色边界。
- [ ] task_plan 更新 Goal/Current Phase/Next Step 为本次工作，新增里程碑 20 的 A–D 工作单元。说明原里程碑 19 的 239 是历史快照，本次交接基线为 408（388 旧 + 20 新，6 项失败/错误），最终填写真实总数。不要批量把所有历史数字替换；不要重启里程碑 19 的 Skill 安装任务，不代替历史独立审核宣布其通过。
- [ ] 新建 `docs/models/ollama-gpt-oss-120b.md`，作为人读资料，不读写全局注册表。以下内容来源为“Owner 原始任务提供的历史证据，Codex 从目标 OpenCode 用户消息核对；本轮未复验原始 smoke 产物”：

| 字段 | 记录值及证据限制 |
| --- | --- |
| provider / model_id / family | ollama / gpt-oss:120b / gpt-oss |
| architecture / 参数量 | gptoss / 约 116.8B，历史报告 |
| quantization / context | MXFP4 / 131072，历史报告，不是所有正式请求的固定参数 |
| capabilities | completion、tools、thinking；Reviewer 权限仍禁用工具 |
| 硬件状态 | 曾显示 100% GPU；当前加载状态未验证 |
| 历史 smoke 日期 | 标识指向 2026-09-06；原始精确时间戳/时区未提供 |
| smoke 1 | gpt-oss-ollama-reviewer-smoke-20260906 |
| smoke 2 | gpt-oss-ollama-task011-review-packet-smoke-20260906 |
| 历史结果 | 两次均报告单一 JSON `{"approved":true,"findings":[]}`、无工具、reported cost=0 |
| 历史 smoke 2 指标 | 约 2033 input / 71 output Token / 5.6 秒 |
| 第一笔精确指标、两笔原始文件哈希 | 未提供，禁止补写猜测值 |
| 当前配置观察 | 2026-09-08 Codex 只读检查发现本地 baseURL 配置及该 model 条目；仅 configured/discoverable 证据 |
| 当前可调用、正式独立审核 | 本轮未验证；不能据两次历史 smoke 宣布正式项目通过 |

记录历史 LM Studio GPT-OSS 的 peg-native 问题只属于被观察到的 LM Studio 路径，不能外推 Ollama。原始 smoke 文件缺失不阻挡本次代码交付，只作为资料限制；不得访问其它工作区或真实重跑来补齐它。

## 10. 执行和验证方式

先 A，再 B/C，最后 D。每个工作单元完成报告一次实际测试结果；中途不用反复全量跑。上下文以文件为准，避免反复打印整仓库或 4 份规范。

修改采用小范围补丁。连续两次工具出现相同缺参数错误时，只做一次无副作用的最小诊断；若仍失败，停止重试，保留文件并在 task_plan 或最终回复报告准确断点。不要花几十个模型轮次猜测“再等一下环境会恢复”。若工具正常但测试失败，应继续修复测试揭示的真实问题。

### 10.1 必须执行

使用本机 `python3.11`；如环境更新导致该命令不存在，使用已安装的其它 Python >=3.11 并记录版本，不安装运行时。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_local_ollama_reviewer.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_opencode_adapter.py' -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_remote_reviewer.py' -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m unittest discover -s tests -q
```

最终一次全量测试必须针对最终源码和测试版本；失败后改了代码，重跑相关测试及最后全量。记录真实退出码，不通过管道 tail 的成功码覆盖 unittest 失败。

```sh
ollama_check_tmp=$(mktemp -d)
PYTHONPYCACHEPREFIX="$ollama_check_tmp/pycache" python3.11 -m compileall -q src tests
git diff --check
git diff --no-index --check /dev/null tests/test_local_ollama_reviewer.py
git status --short
```

新增 Markdown 文件也做未跟踪空白检查；`git diff --check` 单独不覆盖它们。明确区分 no-index 普通 diff 的“有差异”状态与 --check 的空白问题，保存检查输出。

在已核查环境中没有项目 lint/type-check 配置，也没有 Ruff/mypy/pyright 可用；因此它们是“不适用/未配置”，不是通过，也不是交付阻断。不要联网安装、新增 lint 体系或全仓格式化。Skill 未改动，本次不要求安装、同步或另行 Skill quick_validate；既有 test_skill 由全量回归覆盖。

### 10.2 离线 wheel 构建与隔离安装

允许在临时目录创建构建副本，避免把 egg-info/build 产物留在 worktree。使用当前修改后的 src，不要仅 `git archive HEAD` 而丢失未提交实现。已安装 setuptools 82.0.1 自带构建支持。

示例命令按依赖顺序运行；第一步失败不能继续假定后续已完成：

```sh
ollama_build_tmp=$(mktemp -d)
mkdir -p "$ollama_build_tmp/source" "$ollama_build_tmp/wheels"
cp -R src "$ollama_build_tmp/source/src"
cp pyproject.toml LICENSE "$ollama_build_tmp/source/"
(cd "$ollama_build_tmp/source" && PYTHONDONTWRITEBYTECODE=1 python3.11 -c 'from setuptools.build_meta import build_wheel; print(build_wheel("../wheels"))')
uv venv --python python3.11 "$ollama_build_tmp/venv"
uv pip install --offline --no-index --python "$ollama_build_tmp/venv/bin/python" "$ollama_build_tmp"/wheels/*.whl
(cd "$ollama_build_tmp" && env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 "$ollama_build_tmp/venv/bin/python" -c 'import agentflow; from agentflow.opencode_adapter import LocalOllamaReviewerAdapter; print(agentflow.__file__)')
"$ollama_build_tmp/venv/bin/agentflow" --help
shasum -a 256 "$ollama_build_tmp"/wheels/*.whl
```

临时 venv 安装是允许的验证步骤，不是更新全局 AgentFlow；不修改 pyproject 依赖、不联网下载。若本机离线构建依赖实际不可用，记录具体缺项和已执行命令，交付为 BLOCKED，不伪造构建通过，也不为了完成自动安装。

最终核对 docs 的 PLAT-06/MODEL-14/AD-45/A32/A33 只有一个定义，互相引用有效；scope、非目标和终点一致；findings/progress 哈希未变；工作区无新增密钥、运行数据库、原始全局配置或超范围产物。

## 11. 交付格式与 Codex 复核门槛

将正式交付报告放到允许的 `docs/verification/2026-09-08-ollama-reviewer.md`，包含：

1. A–D 完成情况；H01–H10 的修复位置和简短证据。
2. 修改文件清单，注明哪些是沿用 Qwen 的修改、哪些由本轮修复，但不把前任自述当验收依据。
3. O01–O28 到实际测试方法的映射，以及最终定向/全量数量、失败数、错误数、skip、退出码。
4. 配置/端点绑定的实现方式；仅放脱敏示例，完整 resolved config 不写入报告。
5. compileall、tracked/新增文件空白检查、离线构建/安装/导入/CLI 结果、wheel 路径与 SHA-256。
6. 历史 smoke 来源限制、未执行真实模型 smoke、未验证当前加载/正式审核、lint/type-check 未配置等说明。
7. 当前分支、HEAD、未提交文件；剩余风险和 Codex 应重点看的位置。

报告保持简短、可核查。不要为了报告更新后的 Markdown 再无限重复所有测试；只改文档且不改变可执行内容时，核对文档和 diff 即可。

全部功能验收、定向/全量测试和构建通过后，最终回复 `READY_FOR_CODEX_REVIEW` 并链接报告。真实 smoke 和全局安装不属于本次完成条件；正确记录证据缺口已满足资料交付要求。

只有实质行为错误、授权/隐私/费用/角色边界问题、必要验收缺失、明确回归或无法复现的“通过”才阻断。非影响结果的措辞、排版、方法名、可以但非必要的重构只记录非阻断建议；不得因此启动新的无限修订。若确有白名单以外文件必须修改或无法执行的必要检查，交付精确 BLOCKED/CHANGES_REQUIRED 说明，不扩大范围、不自称完成。

## 12. 主管的方案一致性检查（已完成）

- 实施者、验收制定者和最终 Reviewer 分工清楚；用户指定 DeepSeek 自测与旧限制的解释已写明。
- 所有要求修改的源码、测试、文档和交付报告都在写权限表内；模型资料缺失已给出新文件位置与可用历史材料。
- 测试和构建需要的临时文件、Git 夹具、venv 安装有明确权限，实际项目仍禁止提交和安装。
- “未跟踪文件检查”“不要求 smoke”“不要求更新 Skill”和旧任务的过时步骤已消除冲突。
- 产品角色禁止 Shell 与实施者运行确定性测试是两个不同层次；后者在本次工作中允许。
- 不新增 ModelRef/schema/数据库字段；Token 缺失通过既有 raw_metadata 表示，哈希授权用现有合同验证。
- 安全配置错误、传输失败、协议错误、UNKNOWN 和计划内 fallback 的预期分类分开，未要求所有错误都返回同一暂停原因。
- 端点验证依据真实 resolved configuration；未把历史可调用或本地 provider 标签当作当前运行证明。
- 最终交付和阻断门槛固定；审查不因纯格式或非实质建议反复退回。
