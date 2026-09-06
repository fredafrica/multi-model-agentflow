# 实施进度

## 2026-09-05

- TASK-011 真实重跑模型门禁已通过：`lms ps --json` 仅有 `qwen/qwen3.8-27b`，实际变体 `qwen/qwen3.8-27b@8bit`、量化 `bits=8`、上下文 262144；未加载 GPT-OSS 或其他 LLM。
- 两次按变体/仓库路径直接加载均因 LM Studio CLI 无法解析该名称而失败，未发生自动回退；改用已安装模型键 `qwen/qwen3.8-27b` 并设置同名 identifier 后成功，随后以实际进程状态复核量化变体。
- 里程碑 18 完成：将两种真实 OpenCode 同行摘要引导语加入严格白名单，未知后缀改为 `suspected_step_limit` 安全暂停。
- Codex 独立发现并关闭三个边界：疑似识别漏掉其他合法裸标记变体、`reachedness` 词边界误报、resume 重复暂停。
- 最终复核又补充“句点后无空格的未知后缀”回归。定向 3/3、全量 193/193、compileall、修改文件 Ruff 和 `git diff --check` 通过。
- 全仓 Ruff 0.14.1 仍有 4 项历史错误，mypy 仍有 17 项历史错误；本轮未修改无关问题。真实 Qwen 长 Markdown 续接运行待核对已批准 AgentFlow 计划哈希。

- 修复真实步骤耗尽检测遗漏：真实 Qwen implementation/revision 输出把终止标记作为“前缀推理文本 + `</think>` + 独立终止行 + 长 Markdown Summary”中的独立行，旧 `_text_reports_step_limit` 只检查首行或整段 `fullmatch`，导致两个调用被误记 `completed` 并错误进入 revision。
- 将 `_text_reports_step_limit` 改为逐行结构化扫描：跟踪 fenced code、跳过 blockquote/diff/缩进，仅匹配规范化后整行等于已接受终止标记的行；检测到后抛 `InvocationIncompleteError`（`step_limit_reached`、`termination_source=final_text`）并保留 session ID、Token、费用、耗时与输出证据；退出码 0 与非 0 均正确分类。
- Runner 经既有同会话 continuation 续接而非运行 deterministic tests 或开启新 revision；新增真实 OpenCodeAdapter 替身回归证明 base 记步骤耗尽、产生 `continuation.scheduled`、同 session 递增 segment_index，第二段完成后才进入确定性测试；review/rereview 仍不续接。
- 新增最小脱敏 fixture `opencode_max_steps_long.jsonl` 与 5 项回归测试；全套 172 项测试、compileall、`git diff --check` 均通过。
- 修复二轮 P1：`_line_is_step_limit_marker` 不再先剥离标点，改为对原始行严格锚定 `re.fullmatch`，避免把标题/加粗/斜体/行内代码/引号/列表包裹的终止短语误判为真实终止；新增负向测试，全套 173 项通过。

## 2026-09-04

- 启动真实 OpenCode 故障修复；明确本轮不启动子 Agent、不调用真实远程模型、不产生 API 费用、不创建 commit。
- 已完整读取项目规范、实施计划、需求、MVP、架构决策及 `multi-model-agentflow`、`skill-creator`、`planning-with-files` Skill 指令。
- 已检查 Git 状态：工作树存在 23 个已修改文件和 1 个未跟踪测试文件，全部视为现有用户改动并在其上工作。
- 已审阅 OpenCode 本地/远程适配器、Runner、调用服务、调用状态、数据库持久化、Git worktree 证据与相关测试路径。
- 已根据 OpenCode 官方文档确认 `steps` 是 agentic iteration 上限，最后允许步骤是强制文本收尾；无工具 Reviewer 将采用有语义的有限预算 `2`。
- 定向测试在沙箱外运行 53 项：52 通过，1 失败。唯一失败为未跟踪空白错误用例沿用了默认重试数，导致额外 revision；已将该用例限定为零重试后继续验证。
- 修正回归用例后定向测试 53/53 通过；里程碑 10 和 11 完成，进入文档与 Skill 同步。
- 增加步骤耗尽大小写/标点变体覆盖后，定向测试 54/54 通过；完整测试 98/98 通过。
- 需求、MVP、架构决策与简洁 Skill 已同步；compileall、canonical Skill 校验和 `git diff --check` 均以退出码 0 通过。
- 静态审计未发现 `steps=1` 回退或业务项目词汇；测试中的远程适配器均由 mock/stub/FakeAdapter 隔离并保留 `test_double` 标记。
- 里程碑 12 完成，进入离线 wheel 构建、隔离安装和 installed Skill 同步。
- wheel 以离线、无构建隔离方式成功生成；SHA-256 为 `3e4061333d96992bba78e730e115ce5da162ebfcf37280df8b475681f7900903`。
- wheel 在新建临时 Python 3.11 环境中以 `--no-index --no-deps` 成功安装，导入与 CLI 帮助正常，隔离环境完整测试 98/98 通过。
- installed Skill 已从 canonical 两个文件更新；canonical/installed `SKILL.md` SHA-256 均为 `5366b16b9db0c24cdffeaedddee5ded029656f6c4883d214a1e307eb12041851`，`openai.yaml` 也逐字节一致。
- installed Skill 官方校验与静态无费用自检通过；里程碑 13 完成，真实远程 Reviewer smoke test 明确保留为后续需重新计划与授权的事项。

- 用户明确授权进入 MVP 实现阶段，并要求每个里程碑完成后反馈。
- 已完整读取 planning-with-files 与 karpathy-guidelines。
- 已建立六个里程碑的持续实施计划。
- 里程碑 1 完成：新增 JSON 友好的数据合同、三个正交状态机、SQLite v1 schema 和供应商无关适配器协议。
- 设计合同测试 8/8 通过；AD-17 与 AD-21 已转为“已接受”。
- 里程碑 2 完成：实现配置路径、SQLite 事务存储、计划授权、策略门禁、调用幂等保护和无费用测试替身。
- 首次测试发现 `unittest.mock` 需要显式导入，已修复；最终内核测试 27/27 通过。
- 里程碑 3 完成：实现 CLI、三种运行模式、独立 worktree、质量门禁、暂停/冻结、接管与恢复。
- CLI 授权哈希往返测试发现数值类型会影响哈希，已在数据合同入口统一规范化。
- 测试替身闭环 40/40 通过，包括三任务串行、P1 修复复审、审核只读、文件越界、暂停恢复、人工修改最小失效和 UNKNOWN 禁止重试。
- 当前进行里程碑 4：真实本地模型。
- 里程碑 4 只读烟雾测试成功；首次写入测试因 OpenCode 默认 agent 未暴露写工具而未修改文件，已记录并转向自定义 agent 权限方案。
- 里程碑 4 完成：OpenCode → LM Studio 本地适配器完成模型发现、权限隔离、JSON 事件解析、Token/耗时记录与本地进程冻结支持。
- 使用 Qwen 3.8 27B 8-bit 完成真实只读、受限写入和 Runner 全链路烟雾测试；所有调用费用均为 0，真实写入只涉及授权文件。
- 当前进行里程碑 5：Codex Skill。
- 已创建最小 Skill 与 UI 元数据；首次官方校验因校验器运行环境缺少 PyYAML 而未启动，正在使用本机已有依赖环境解决。
- 里程碑 5 完成：Skill 只编排 `agentflow` 的统一计划、授权和状态源；隐式触发仅允许建议、检查和草拟，禁止自动启动模型。
- 已复用机器上的 vendored PyYAML 完成 skill-creator 官方校验；项目本身未增加 YAML 或其他运行依赖。
- 当前进行里程碑 6：完整验收、缺口修复、代码简化与文档一致性复查。
- 里程碑 6 完成：MVP-A01 至 MVP-A18 全部通过，自动化测试 62/62 通过，Skill 官方校验通过，CLI 帮助入口可运行。
- 最终故障注入覆盖固定预算、本地后备、D0-D3、进程组冻结、UNKNOWN 查询、已完成调用复用、数据库重开与事件事务回滚。
- 已按 simplify 规则完成代码审查并复跑全套检查；没有未解决的 P0/P1。
- 文档冲突检查完成：项目策略扩展名统一为 `.agentflow/project.toml`，需求阶段的过期限制已移除，验收证据已写入 `docs/mvp.md`。
- Git 保持 `main`、无提交；本轮未启动子 Agent、未调用收费模型、未产生 API 费用。
