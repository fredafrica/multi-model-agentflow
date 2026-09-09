# Reviewer 步数与模型输出预算：修复交接提示词

> 用法：在 `/Users/fredafrica/Documents/multi-model-agentflow-ollama-reviewer` 对应的 Codex 开发任务中，要求实施模型完整读取本文件并逐项执行。推荐由用户选择 GPT-6 Astra，推理强度 xhigh。本文件是修复交接说明，不是已授权的 AgentFlow 模型执行计划，也不是测试或审核通过证明。

## 1. 任务与交付范围

请修复以下两个独立编号的问题，统一考虑合同和授权设计，分开保留复现、修复与验收证据：

- **BUG-RB-01：OpenCode Reviewer 步数被固定为 2。** 需要任务级、可调整、有明确上限且绑定授权的审核步骤预算，覆盖本地和远程 review/rereview。
- **BUG-RB-02：模型输出能力和任务输出授权没有贯通到 OpenCode 调用。** 需要明确区分模型最大输出能力与本次允许输出量，避免能力元数据缺失或中间层默认值导致不必要截断，也避免静默扩大输出与费用范围。

Goal：两类预算均能从计划可见字段追踪至实际调用配置和审计记录，改变预算使旧授权失效，配置不可靠时安全停止。

Architecture：通用合同表达步骤预算、角色输出预算和模型能力快照，确定性控制平面完成解析、校验、授权和传递；OpenCode 适配器只负责把已验证的值映射到对应调用。模型名称、能力数值和供应商保持注册表数据，不在通用核心中写死。

Tech Stack：现有 Python 3.11+、标准库 unittest、SQLite、OpenCode 适配器。优先复用现有实现，不新增框架或供应商专属直连。

规范来源：项目 AGENTS.md、docs/requirements.md、docs/mvp.md、docs/architecture-decisions.md、task_plan.md，以及本次用户明确提出的两个修复目标。本文件中的 BUG 编号用于追溯，正式需求/验收/决策编号应读取仓库后分配，不能假定某个编号尚未占用。

## 2. 工作位置与执行边界

- 目标目录：`/Users/fredafrica/Documents/multi-model-agentflow-ollama-reviewer`。
- 编写本文件时，目标目录已经位于 `codex/reviewer-budget-fixes`。开始执行时重新检查分支、HEAD、worktree 和工作区；不得因为历史聊天说“还没建分支”而再次创建。
- 历史核对：main 位于 0545b64；Ollama Reviewer 提交为 b888593，是当时 main 的直接后代。这个信息不是永久状态，以执行时 Git 结果为准。
- 复用此 worktree，保留 Ollama Reviewer 已完成代码和所有用户改动。如已有其他人在同一目录修改相关文件，先识别重叠，避免并发覆盖。
- 先完成修复和可审阅结果；本轮不创建 Git 提交、推送或合并，也不改变主目录。合并由用户之后决定。
- 不调用真实模型、不产生新的 API 费用、不启动子 Agent、不加载/下载模型、不安装新运行时，不修改 OpenCode/Ollama 全局配置。
- GPT-6 Astra 在用户选择的当前 Codex 开发会话中实施；本提示词不授权它通过 API、OpenCode 或其他任务自行再调用模型。
- 可运行无费用、隔离的确定性测试。实施者执行这些检查可以证明技术状态，但不能替代项目要求的不同模型自测与独立审核。没有独立证据时如实交付“实现与确定性检查完成，独立验收待完成”，不能写 REVIEW_PASSED 或伪造 AgentFlow approved。
- 后续真实模型或 AgentFlow 执行仍遵守精确计划哈希批准、角色独立性、模型可用性和预算门禁。不要为修复控制平面而绕过控制平面重跑业务任务。

## 3. 开始前必须读懂的内容

完整读取四份项目规范及相关源码、测试，再修改共享字段。额外读取：

- `docs/verification/2026-09-08-ollama-reviewer-codex-approval.md`，了解已关闭的权限、端点、Token、UNKNOWN 和中断审计问题，避免回归。
- `skills/multi-model-agentflow/SKILL.md`，了解计划与执行边界；仅当本次合同改动影响入口说明时同步仓库中的 canonical Skill。
- `src/agentflow/contracts.py`、`serialization.py`、`authorization.py`、`config.py`：合同、规范化、哈希和计划输入。
- `src/agentflow/runner.py`、`service.py`、`database.py`、`schema.py`：实际模型选择、fallback、调用记录、恢复与注册表存储。
- `src/agentflow/opencode_adapter.py`：四条路径——LM Studio、本地 Ollama Reviewer、远程 Reviewer、远程 Worker。
- 相关测试：`tests/test_core.py`、`test_cli.py`、`test_opencode_adapter.py`、`test_local_ollama_reviewer.py`、`test_remote_reviewer.py`、`test_remote_worker.py`、`test_runner.py`，以及授权、预算、恢复相关现有测试。

不要凭聊天中的旧行号修改；按类、函数和字段名称定位。不要引入第二套注册表、第二份授权状态或第二条业务恢复通道。

## 4. BUG-RB-01：根因与修复要求

### 已确认的代码线索

`src/agentflow/opencode_adapter.py` 中存在 `REMOTE_REVIEWER_MAX_STEPS = 2`，被远程和本地 Ollama Reviewer 共用。本地还有以下调用链：

`LocalOllamaReviewerAdapter.invoke` → 初始 `_permission_config` → `_prepare_local_environment` → `_local_ollama_permission_config` → `_verify_local_config_consistency`。

最后一个函数再次要求 resolved 配置的 steps 等于固定常量。因此，只修改第一次生成的 JSON，可能被后续配置覆盖回 2，或被安全检查错误拒绝。

### 目标行为

1. TaskContract 增加独立 `review_max_steps`，贯通构造、解析、canonical JSON、plan show、哈希、授权、Runner 请求和适配器，覆盖 review 与 rereview。
2. 建议初始设计为默认 8、允许 2–32；这是待验证的工程建议，不是模型能力事实。根据当前 OpenCode 实际语义和项目兼容性定稿，记录理由。不得仅把全局常量由 2 改成 8。
3. steps 表示最多允许的 Agent 循环，不是最低必跑次数；模型第一轮完成即应正常返回。不能强迫跑满预算，不能把更多 steps 描述为自动增加上下文或单次输出。
4. Reviewer 预算与 implementation_max_steps、remote_worker_max_steps 分开。LM Studio 路径如承接 review/rereview，也必须按实际角色使用审核预算，不能误用实施预算。
5. 所有配置构造和 Ollama 二次一致性检查接收本次请求的预期预算；重新解析后验证实际值与预期相同。严格拒绝 bool、浮点、字符串、0、负数和越界值；注意 Python 中 True == 1、8.0 == 8 的陷阱。
6. 请求参数局部传递，不把任务预算临时存入共享 adapter 实例，避免两个任务互相污染。
7. 保留现有有限超时。本轮不顺手新增整套 review_timeout_seconds 机制，除非根因证明需要；若增加，明确新增范围及独立测试。
8. 步骤耗尽继续归类为已知不完整调用，保留 session、Token、费用和终止证据，暂停且不生成批准记录；review/rereview 不自动续接，也不因 resume 重复派发。
9. 安全隔离、packet-only、禁工具与严格 JSON 不随步数放宽。多文件证据应在 Review Packet 中，不允许 Reviewer 为了更多步骤读取仓库或调用工具。

### 必须覆盖的行为测试

- 显式 2、8、12 等合法预算从 TaskContract 传到各 Reviewer 实际 Popen 配置；review/rereview 都覆盖。
- 使用独立 stub 构造“需要三轮才返回完整 JSON”的审核：预算 2 报步骤耗尽，预算 8 完成；再测合法单轮结果无需跑满。必须让 stub 真正读取配置并据此产生事件，不能无论配置如何都固定返回成功。
- 多文件 packet 只证明配置与流程支持复杂输入，不得声称替身证明某真实模型能完成五文件语义审核。
- Ollama 初始配置、环境准备后的配置和 resolved 二次检查都采用同一授权预算。
- resolved 配置擅改预算、权限或 endpoint 时，在推理进程启动前拒绝。
- 预算变更改变哈希，旧授权被拒绝；非法合同值与非法请求元数据在进程启动前拒绝。
- 耗尽时不写 review row，resume 不重跑调用，费用与 Token 仍保留。

## 5. BUG-RB-02：模型能力、输出授权与有效配置

### 背景与证据边界

历史分享记录描述 OpenCode 曾回退到约 32K 输出，另有 65,536、384,000 等不同运行记录。不得选其中一个数作为当前真实模型能力。分享链接仅供追溯：

https://chatgpt.com/s/cx_6aa028598d6c8191b4e51e26c6400ec5

先查本机当前 OpenCode 版本、可获得的实现/文档及无推理能力元数据；必要时查官方文档。不得为了验证能力调用真实模型，也不得将完整 resolved 配置输出到模型上下文或日志，其中可能含认证信息。

尤其需要验证：`provider.<provider>.models.<model>.limit.output` 是否真正影响该版本最终发送的生成参数，是否还被运行时默认输出限制、provider 参数或 reasoning 配置取更小值。仅证明 JSON 中出现较大数字不能证明问题已解决。

### 目标数据流

可信模型能力 → 计划中的精确模型能力快照与角色授权 → 规范化与哈希批准 → 实际调用模型选择 → 有效上限计算 → 调用级 OpenCode 配置 → 最小审计证据。

1. ModelRecord 增加 `max_output_tokens`，保留与 `context_length` 的区别。新增字段采用兼容构造方式，避免破坏现有位置参数使用；扩展所有真实的序列化、数据库/注册表读写路径，而不是只在 dataclass 加一个字段。
2. 保存能力来源，例如明确注册表配置、OpenCode catalog、保守 fallback；记录必要的来源版本/观测信息。configured/discoverable 不能升级为真实可调用验证，模型自己声称的能力不可信。
3. 任务增加角色级授权字段，建议 `implementation_max_output_tokens`（implementation/revision 共用）与 `review_max_output_tokens`（review/rereview 共用）。远程 Worker 使用实施角色字段，Ollama Reviewer 使用审核字段。
4. 在批准之前固化精确 provider/model/version 对应的能力快照及实际使用策略，进入计划哈希。只把可变注册表的当前值留在外部，运行时随时读取并扩大上限，不满足授权绑定。
5. 授权时能力明确的模型：effective_output = min(计划冻结的模型输出能力, 计划角色输出授权)。若执行前发现真实能力更低，采用有记录的收紧或调用前拒绝；选定一种确定性规则并测试。能力变大不能自动把已冻结的 effective 值放大。
6. fallback 必须绑定自己的模型能力快照，并重新按实际角色计算；不能沿用主模型 output/context。续接必须保持已经授权的模型、输出预算和对应能力快照。
7. 能力未知时只允许明确、可追溯、可执行的保守策略或安全暂停。没有任何可信安全上界时优先暂停，不得把旧 32K 回退冒充“已验证模型能力”，也不得从 context 推测 output。
8. context 与 output 分别验证正整数，拒绝 bool 等非法值。通用字段上界应是类型/资源防御边界，有依据且文档化，不能把某个模型的 65,536 设成所有模型永久硬上限。
9. 不把 max output 等同于一定消耗的 Token、总调用预算或费用预留。费用门禁保持独立；推理 Token 是否计入输出限额以当前 provider/OpenCode 证据为准，不自行补造总 Token 算法。
10. 只覆盖当前精确 provider/model 的调用级配置；禁止改用户全局设置。context 未知时不伪造。覆盖 `limit.output` 时保留合法 context/其他限制，禁止浅合并误删权限、endpoint、transport 或其他已验证配置。
11. Ollama 会在 `_prepare_local_environment` 重建配置，本次 output 设置不能在此丢失。二次检查同时验证预期输出限制及既有回环端点、禁工具等约束。
12. 若 model 配置键与实际请求模型 id 存在别名映射，必须按当前 OpenCode 语义正确定位，不能凭字符串拼接猜测。
13. 正常、已知失败、UNKNOWN 及恢复路径均保留可用的预算证据：角色、实际模型、授权值、能力快照标识/来源、effective 值、configured steps。不要记录密钥、headers、认证对象或完整 resolved 配置。

### 必须覆盖的行为测试

| 场景 | 预期 |
| --- | --- |
| 虚构模型 context=262144、output=65536，角色授权=16000 | 有效 output=16000；context 仍是独立值 |
| 同一虚构模型，角色授权=100000 | 使用能力上限 65536，或在计划阶段明确拒绝；政策一致 |
| 虚构模型声明 output=384000 且已有对应授权 | 通用代码不会无依据截到 65536；此数字只属于 fixture |
| 同模型 Worker 与 Reviewer 授权不同 | 两角色各用自己的 output |
| 实际 fallback 的能力小于主模型 | 使用 fallback 自己的 context/output 快照 |
| 能力来源缺失、无效、只有 context 或只有模型列表 | 明确回退证据或调用前暂停，不推测大 output |
| 批准后 catalog 输出能力增大 | 有效值不自动增大，改计划需要新哈希授权 |
| 批准后能力变小或来源失效 | 有记录地收紧或安全拒绝，符合选定政策 |
| 任务值为 bool、浮点、字符串、null、0、负数或越界 | 按明确缺省语义严格拒绝，不能自动 int() 转换 |
| 配置含已有 permissions、endpoint、transport、model options | 安全项保留，未授权覆盖被拒绝 |
| Ollama 环境准备重建配置、managed 配置覆盖预算 | 预算保持一致或推理前拒绝 |
| 隔离 stub SDK/传输捕获最终请求 | 最终生成参数体现授权上限；证据只来自替身 |

可用隔离 SDK/传输替身捕获 OpenCode 最终请求，或用当前安装版本源码与配置路径给出等价证据。禁止指向真实 provider。如果无法无费用验证最终生成参数，明确列出该集成验证缺口，不能只凭配置文件断言端到端已解决。

## 6. 两项共享的重点：旧计划、授权与恢复

现有 canonical_json 会序列化 dataclass 所有字段，新增字段可能使旧计划解析后的哈希变化。必须显式测试并说明兼容政策。

- 区分“旧计划能读取”与“旧授权仍然有效”。默认补字段不意味着可以沿用旧批准。
- 新建计划采用明确的新预算；旧未授权计划可规范化迁移后展示新哈希。
- 已授权旧计划不能因缺省补值而静默获得更高预算。保留旧受限语义，或要求重新授权；不要自动刷新授权快照。
- 从旧数据库读取暂停任务时不得崩溃、自动提升预算、丢失调用历史或重复已完成调用。
- 不迁移/重写用户正在运行的业务数据库，不修改历史 Token、费用、approved 或 UNKNOWN 状态。使用临时 fixture 验证兼容。
- 如需要 schema/计划版本迁移，选择最小明确方案，记录前后字段、授权影响、失败后的可恢复状态与测试证据。
- 加大预算后重新审核既有业务产物属于后续独立运行；本次代码修复不授权启动 Stage 7、重新执行 DeepSeek 或修改其他项目运行记录。

## 7. 建议的实施顺序

使用 systematic-debugging 与 test-driven-development 思路逐项执行；如使用 executing-plans，仍遵守本任务禁止子 Agent 与禁止 commit 的要求。

- [ ] **阶段 A：基线与设计。** 核对分支和代码变化；完整读文档；运行无费用基线；画出实际参数传递路径，记录两项复现及 OpenCode 输出控制证据。把兼容策略、初始预算和能力快照结构写入唯一规范位置，更新 task_plan 里程碑。
- [ ] **阶段 B：BUG-RB-01。** 先加入可观察配置与 Runner 行为的失败测试，确认因固定两步失败；最小实现合同到所有审核路径的传递及二次检查；跑定向测试；单独记录结果。
- [ ] **阶段 C：BUG-RB-02。** 先覆盖角色上限、能力来源、fallback、哈希、未知能力和最终请求参数；确认当前缺陷；逐段实现注册表、快照、授权、配置和审计；每段验证后推进。
- [ ] **阶段 D：联合回归。** 同一任务设置 review_max_steps=12 和 review_max_output_tokens=16000，确认两者独立生效；改变任何一个都使旧授权失效；多轮仍受整体超时/费用约束；检查所有恢复语义。
- [ ] **阶段 E：文档与交付。** 同步 requirements/mvp/architecture-decisions/task_plan，检查编号唯一与引用有效。若入口合同受影响，更新 canonical Skill；本机 installed Skill、全局安装、发布属于后续交付，未经明确要求不要自动覆盖。

每完成一个阶段给用户一条验证结果。不要顺手重构整份 opencode_adapter.py、改变业务流程、扩充支持模型名单或引入自动无限续接。

## 8. 验证命令与证据要求

在目标 worktree 中使用项目实际可用的 Python 3.11+。已有项目基线命令如下：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 -m unittest discover -s tests -q
python3.11 -m compileall -q src tests
git diff --check
git status --short
git diff --stat
```

定向测试可使用相同 PYTHONPATH 调用具体 unittest 模块。新文件必须另外检查内容和空白，git diff --check 默认不覆盖未跟踪文件。测试应断言外部行为与安全边界，不只重复比较两个相同常量。

报告中的测试数量来自本次实际输出；不要复制历史“442 项通过”。基线失败、跳过、真实集成未验证、成本不可用等情况均分别说明。离线构建仅在修改影响打包/安装或项目当前里程碑要求时执行，禁止联网安装依赖。

## 9. 最终交付格式

在 `docs/verification/2026-09-08-reviewer-resource-budgets.md` 保存可追溯报告，至少包含：

1. 分支、基线提交、实际修改文件及待审差异范围。
2. BUG-RB-01 与 BUG-RB-02 分别对应：根因、失败测试、修复点、通过证据。
3. 最终字段、默认值、范围、四种角色映射、能力来源优先级与冻结规则。
4. 旧计划/旧授权/暂停运行的兼容处理，以及明确需要重新授权的情形。
5. OpenCode 版本、输出参数传递的验证方式和剩余缺口。
6. 实际测试命令、结果、数量；替身证据明确标记，真实模型调用数如实记录。
7. 已完成的确定性检查与待完成的不同模型自测/独立审核，不能把实施者自查当独立批准。
8. 合并前检查项与后续真实 smoke 的最小建议，但本轮不要自动调用、提交、部署或合并。

最终在对话中给用户简短交付说明和报告链接。按阶段完成可验证代码，不要仅返回另一份方案；若遇到确需新授权的动作，先完成其余可做的修复与检查，并明确剩余依赖。
