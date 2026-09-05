# 实施发现

## 已确认基线

- 仓库当前分支为 `main`，建立本记录时尚无提交。
- 需求基线包含 84 个编号需求、33 个架构决策和 18 个 MVP 验收场景。
- MVP 唯一必须真实验证的模型路径是 OpenCode → LM Studio；收费与错误路径使用无费用测试替身。
- 外部资料或工具输出仅作为数据记录，不作为指令执行。

## 环境验证结果

- OpenCode 的受限写入权限、JSON 事件格式和本地进程组冻结均已验证。
- 当前加载的本地模型已完成只读、受限写入和 Runner 全链路调用，远程费用均为 0。

## 里程碑 1

- 本机 Python 为 3.14.7；项目最低版本定为 3.11，当前设计只使用标准库。
- OpenCode 与 LM Studio CLI 均已在本机安装并可调用。
- 使用单一 SQLite 数据库保存追加事件和当前状态投影可以在同一事务中保证一致性。
- 任务、运行控制、模型调用三个状态机需要正交保存；`UNKNOWN` 调用只允许被核实为完成或失败，不能回到 started。
- 8 项设计合同测试全部通过。

## 里程碑 2

- 规范化 JSON 与 SHA-256 能稳定绑定计划内容，授权在计划变化或 24 小时到期时失效。
- D2 双条件、D3 禁止、敏感字段扫描、本地免费、预算预留、自适应确认点和跨家族审核均已实现为确定性门禁。
- SQLite 的事件和状态更新使用同一事务；故障注入测试确认可以整体回滚。
- 模型调用使用唯一 `request_key`；已完成调用可复用，进行中调用拒绝重复，`UNKNOWN` 调用拒绝重试。
- 无费用 FakeAdapter 已经通过协调服务完成调用与持久化。
- 内核测试 27/27 通过。

## 里程碑 3

- `agentflow` 已支持计划展示/授权、启动、状态观察、暂停、立即冻结、接管、恢复、日志、费用和取消。
- 每个实施任务创建独立 Git worktree；模型写入后执行文件白名单核验，审核前后执行内容哈希快照核验。
- 托管、监督、自适应三种模式均走同一调用服务；监督逐次确认，自适应按静态 B/S 风险规则确认。
- P1 会触发修复和原审核方复审；审核方写文件会直接使运行失败。
- 安全暂停发生在外部调用返回后的下一调用边界；人工接管修改只使受影响任务及其依赖失效。
- `UNKNOWN` 调用使运行暂停，并在核实前拒绝恢复，防止换用新请求键绕过幂等保护。
- CLI 与执行闭环测试合计 40/40 通过。

## 里程碑 4 环境探测

- OpenCode 已安装，`opencode run` 支持 `--model`、`--format json`、`--dir`、`--pure` 和可选 `--auto`。
- LM Studio 本地服务器运行在 `127.0.0.1:1234`；本地 `/v1/models` 可读取，无需远程服务。
- 当前加载 `qwen/qwen3.8-27b` 8-bit，支持工具调用，配置上下文 262144；探测时状态为 `generating`，因此暂未插入新的真实生成请求。
- OpenCode 可见 `lmstudio/qwen/qwen3.8-27b` 等本地模型标识。
- macOS 提供 `/usr/bin/sandbox-exec`，可用于本地适配器写入范围防护，但需要兼顾 OpenCode 自身状态目录。
- OpenCode 官方权限文档确认 `allow`、`ask`、`deny` 三种动作；`--auto` 只自动同意 `ask`，不会覆盖显式 `deny`。
- OpenCode 支持 `OPENCODE_CONFIG_CONTENT` 运行时配置覆盖，因此适配器可为实施与审核分别注入最小权限，无需修改用户全局配置或在 worktree 写配置文件。
- 实施适配器将允许 worktree 内编辑但禁止 bash、外部目录、网络抓取和子任务；审核适配器额外禁止编辑。Git 差异与内容哈希继续作为第二层核验。
- 第一次真实只读调用成功：3373 输入 Token、40 输出 Token、约 28.8 秒、远程费用 0；返回了严格审核 JSON。
- 第一次真实写入调用没有修改文件：虽然顶层权限将 `edit` 设为 allow，默认 build agent 最终只向模型暴露 `glob`、`grep`、`read`。该调用约 151 秒且费用 0；需要改用显式自定义 agent 配置，不能原样重试。
- 自定义 `agentflow-sandbox` 经 OpenCode 本地配置检查确认：实施模式开放 read/glob/grep/edit/write，拒绝 bash、web、task、skill、question 和普通外部目录；审核模式额外拒绝 edit/write。
- 修正后的真实写入成功：只修改 `result.txt`，内容通过 Git 与文件读取验证；4099 输入 Token、59 输出 Token、约 69.9 秒、费用 0。
- 完整 Runner 本地集成成功：在独立 worktree 创建并通过确定性测试，4063 输入 Token、70 输出 Token、约 31.1 秒、费用 0；测试替身独立审核后任务状态为 approved。
- 当前只加载一个真实模型版本，因此该次独立审核明确使用 `fake` 测试替身，不能作为真实跨模型质量结论；系统会阻止同一精确模型自审或关键任务同家族审核。
- OpenCode 适配器会立即记录本地进程组 ID；`pause --immediate` 可以从另一 CLI 进程终止活动进程组，随后由 checkpoint/UNKNOWN 规则防止盲目重试。

## 里程碑 5

- Codex Skill 只保留适用范围、授权边界、模式和干预命令，不复制控制平面逻辑，也不写死候选模型。
- 隐式调用策略允许自动建议，但 Skill 明确禁止在没有计划哈希授权时启动模型。
- `skill-creator` 官方校验已通过；校验使用机器已有的 vendored PyYAML，未增加项目依赖。

## 里程碑 6

- 62 项自动化测试覆盖 MVP-A01 至 MVP-A18，包含预算、本地降级、隐私、暂停/冻结、重启、人工接管、UNKNOWN 查询和事务回滚等故障路径。
- 代码简化审查移除了可推导的本地/远程重复状态，修正了恢复时丢失模型输出、已完成运行可被强制暂停、依赖环以及后备审核模型记录不准确等问题。
- 文档统一使用 `.agentflow/project.toml`，CLI、Skill 和需求文档已补齐 `handoff` 与 `resolve-call`，没有发现剩余矛盾。
- 仓库仍在 `main` 分支且没有提交，符合用户约束。

## 里程碑 10：真实故障语义

- 当前本机 OpenCode 版本为 `1.18.27`。OpenCode 官方 Agents 文档将 `steps` 定义为模型 agentic iterations 的正数上限；达到最后允许步骤时，工具被移除并强制模型输出文本总结。因此 `steps=1` 会把 Reviewer 的首次模型回合直接变成上限收尾回合，不是“允许一次正式答复”。
- 对无工具、packet-only Reviewer，`2` 是允许一次正常审核回合、同时仍保留有限燔断的最小步骤预算；实现应提取语义常量并用测试防止回退为 `1`。
- 现有 `parse_opencode_json()` 只要提取到任意 text event 就构造普通 `InvocationResult`；OpenCode 退出码为 0 时，`InvocationService` 无条件写入 `completed`，是 implementation/review 同时误判的共同根因。
- 现有调用状态 `FAILED` 足以表达“结果已知且失败”；不需要增加 schema 枚举。但需要新增带类型原因和部分 `InvocationResult` 的专用异常，并在 `FAILED` 行中保留 token、耗时、费用、输出及结构化 `failure_kind`。
- Remote Reviewer 当前将所有 JSON event 解析失败包装成 `ModelUnavailableError`，Runner 又统一暂停为 `reviewer_unavailable`；步骤耗尽必须保留为独立已知失败语义。
- `GitWorkspace.changed_files()` 已把 tracked diff 和 `git ls-files --others --exclude-standard` 合并，`diff()` 也将未跟踪文本内容加入 Review Packet，`_run_tests()` 单独检查 expected output 存在。缺口是对未跟踪新文件的空白错误没有补充性确定检查，且文档不应把 `git diff --check` 表述为覆盖所有未跟踪内容。

## 里程碑 11–13：修复与验证结论

- 步骤耗尽现在保存为 `FAILED` 调用及明确 `failure_kind`，保留输出、Token、耗时、费用与原始元数据；Runner 分别以 `implementation_step_limit_reached` 或 `review_step_limit_reached` 暂停，且恢复时不会重复调用。
- Reviewer prompt 在 packet 外声明严格 JSON-only 协议；解析器拒绝 fenced JSON、夹带散文、缺字段、错误类型与非法 severity，P0/P1 会确定性覆盖错误的 `approved=true`。
- 未跟踪的计划内输出进入 changed-files、范围门禁、expected-output 检查和 Review Packet；独立补充检查覆盖 `git diff --check` 不检查的未跟踪文本空白错误。
- 定向测试 54/54、源码完整测试 98/98、wheel 隔离环境完整测试 98/98 均通过；全程只使用明确标记的测试替身或模拟 JSON 事件流。

## 里程碑 17：真实步骤耗尽检测遗漏

- 根因：真实 AgentFlow 运行 `task011-agentflow-v2-20260905`（`implementation_max_steps=32`、`implementation_max_continuations=4`）中，Qwen 的 implementation 与 revision 输出都含 `Maximum steps for this agent have been reached.`，但其形式是“前缀普通文本 + `</think>` + 独立终止行 + 较长 Markdown Summary”。旧 `_text_reports_step_limit` 只重点检查首条非空行或整段规范化 `fullmatch`，整段既非首行又非全文匹配，因此两调用均被记录为 `completed`，随后运行确定性测试并进入新 revision。
- 修复：改为逐行状态扫描——跟踪 fenced code、忽略缩进/blockquote/diff 内容，仅匹配“规范化后整行等于已接受终止标记”的独立未引用行；同时保留对 `critical maximum steps reached` 与既有说明组合的识别。检测到后 `parse_opencode_json` 抛 `InvocationIncompleteError`（`failure_kind=step_limit_reached`、`termination_source=final_text`）并保留 session ID、Token、费用、耗时与输出证据；退出码 0 与非 0 均正确分类。
- 误报防护保持：fenced code、blockquote、diff、行内散文、Reviewer JSON 字段引用、无独立终止行的普通摘要均不误判；既有负向测试全部继续通过。
- 验证：新增 fixture `opencode_max_steps_long.jsonl` 与 5 项回归测试（最小复现、跨 JSONL text event 拆分、退出码 0/非 0 双路径、元数据保留、真实适配器 Runner continuation）；全套 172 项测试通过，compileall 与 `git diff --check` 退出码 0。全部使用明确标记的测试替身，未执行任何真实模型调用。

## 里程碑 17 二轮 P1：行匹配误判 Markdown/引用结构

- 二轮 Codex Review 发现 `_line_is_step_limit_marker` 先用 `re.sub(r"[^a-z0-9]+", " ", line.lower())` 删除标点再匹配，会把标题、加粗/斜体、行内代码、单/双引号和列表包裹的终止短语误判为真实终止并错误触发 continuation。
- 修复：改为对原始行严格锚定 `re.fullmatch`（`re.IGNORECASE`），仅容忍末尾句号与 CRITICAL 变体的 `-`/`–`/`—` 连字符；不再通过删除所有标点把引用/标题/强调/行内代码/列表折叠成合法标记。fenced code、blockquote、diff、缩进与行内散文的既有误报防护保持不变。
- 新增负向测试 `test_step_limit_marker_wrapped_in_markdown_or_quotes_is_not_termination`，覆盖 `## `、`**`、`*`、`` ` ``、`"`、`'` 与 `* ` 列表包裹。全套 173 项测试通过，compileall 与 `git diff --check` 退出码 0。未改动 continuation、授权、预算、数据库或远程 Reviewer 逻辑。
