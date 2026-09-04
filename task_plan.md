# multi-model-agentflow MVP 实施计划

## Goal

实现并验证 `docs/mvp.md` 定义的本地、可停用、无意外费用的多模型协作 MVP，完成全部 18 个验收场景，并在每个里程碑完成后向用户反馈。

## Current Phase

全部里程碑完成。

## Next Step

无待执行步骤；等待用户决定是否创建首次 Git 提交或进入下一阶段。

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

## Decisions Made

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-09-04 | 使用 Python 标准库优先实现 | 符合已确认技术基线并降低依赖 |
| 2026-09-04 | 先完成测试替身闭环，再连接本地模型 | 隔离模型环境不确定性并保证零意外费用 |
| 2026-09-04 | 本任务不调用收费模型、不启动子 Agent | 遵守用户授权边界 |
| 2026-09-04 | Python 最低版本为 3.11，核心优先标准库 | 保持跨平台且减少依赖 |
| 2026-09-04 | 任务、控制和调用状态使用三个正交状态机 | 避免组合状态爆炸并隔离 UNKNOWN 调用 |

## Errors Encountered

| Error | Attempt | Resolution |
| --- | --- | --- |
| `unittest.mock` 未由 `import unittest` 暴露 | 里程碑 2 首次测试 | 改为显式 `from unittest import mock` |
| 计划经 JSON 往返后整数预算变为浮点数，导致授权哈希变化 | 里程碑 3 CLI 测试 | 在冻结数据合同构造时规范化数值类型 |
| OpenCode 本地写入烟雾测试只暴露读取工具，未创建允许文件 | 里程碑 4 首次真实写入 | 调查自定义 agent 的工具暴露与权限合并方式；不重复原命令 |
| skill-creator 的 `quick_validate.py` 缺少 PyYAML | 里程碑 5 首次校验 | 复用已安装 Skill 的 vendored PyYAML 运行官方校验；项目未增加依赖，校验通过 |
