# multi-model-agentflow 实施计划

## Goal

修复 OpenCode 调用的有限步骤预算、不完整调用状态、严格 Reviewer JSON 协议与未跟踪输出证据边界，完成无费用回归验证、离线构建和 canonical/installed Skill 同步。

## Current Phase

里程碑 13 完成：离线构建、隔离安装验证和 canonical/installed Skill 同步均已通过。

## Next Step

真实远程 Reviewer 验证留待新的 AgentFlow plan、plan hash 展示和用户明确授权；本轮不执行收费 smoke test。

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

## Final Verification

| Command | Exit | Result |
| --- | ---: | --- |
| 带 Summary 的真实步骤耗尽 fixture、Runner 暂停路径及原误报反例定向回归 | 0 | 10 tests passed |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_opencode_adapter tests.test_remote_reviewer tests.test_runner -v` | 0 | 60 tests passed |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` | 0 | 104 tests passed |
| `PYTHONDONTWRITEBYTECODE=1 /tmp/agentflow-step-limit-summary-venv.4vvrVz/bin/python -m unittest discover -s tests -q`（隔离环境，无 `PYTHONPATH`） | 0 | 104 tests passed |
| `python3 -m compileall -q src tests` | 0 | passed |
| canonical `quick_validate.py skills/multi-model-agentflow` | 0 | `Skill is valid!` |
| installed `quick_validate.py /Users/fredafrica/.codex/skills/multi-model-agentflow` | 0 | `Skill is valid!` |
| `uv build --wheel --offline --no-python-downloads --no-build-isolation ...` | 0 | `/tmp/agentflow-step-limit-summary-build.1lhyyf/multi_model_agentflow-0.1.0-py3-none-any.whl` |
| final wheel `pip install --no-index --no-deps`, import and `agentflow --help` | 0 | installed, imported and CLI started in `/tmp/agentflow-step-limit-summary-venv.4vvrVz` |
| `git diff --check` | 0 | passed |

Final wheel SHA-256: `ee63e4a6c40a1e65a1c9a64f3c5f128677652a491414664d425836e2c73b7458`.

Canonical and installed `SKILL.md` SHA-256: `5366b16b9db0c24cdffeaedddee5ded029656f6c4883d214a1e307eb12041851`（逐字节一致）。

Canonical and installed `agents/openai.yaml` SHA-256: `3d6e327648b9db455d0f96a278c6c7be957a65f7e66c65f9d2863239dc90c0ed`（逐字节一致）。

项目未配置额外 lint 或 type-check 命令，当前环境也没有 `ruff`、`mypy` 或 `pyright`，因此这些检查未运行。
