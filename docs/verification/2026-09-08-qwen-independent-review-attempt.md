# Qwen 独立审核尝试：超时，未完成验收

## 授权与范围

- 用户批准哈希：`0ff75821eba2a292c131cfc667d2afe6d1cebb4582d231e6276106c248197408`。
- 计划：`.agentflow/independent-review-plan.json`，version 1。
- Run：`qwen-budget-independent-20260908-01`；授权 ID：`74444045-a5ad-46fd-9bfc-e68ced8f322c`。
- 本地 LM Studio `qwen/qwen3.8-27b@8bit`，一次真实 review；32 步、16000 有效输出上限、900 秒推理超时。
- 冻结 31 个文件，包括当前未提交差异、新增源码/测试、交接要求与证据。确定性程序在隔离工作树重放这些文件，不产生新实施。fake 实施阶段明确为 no-op 替身，不是独立模型实施或自测。

## 已获得证据

| 检查 | 实际结果 |
| --- | --- |
| 候选快照 | 31 文件复制前后哈希匹配 |
| 隔离全量测试 | 465 tests，39.183s，OK；整个准备/测试命令 39406 ms |
| 审核结束后的完整性 | 原目录和审核工作树分别复核 31 文件，均无哈希变化 |
| 真实 review call | `c6967b47-0af9-471a-b561-5a3813954944`，test_double=0 |
| 终止 | timeout，900019 ms，cleanup_incomplete=false |
| 已完成步骤 | 13（OpenCode 部分事件中 step_finish 计数） |
| 已报告使用量 | 累计 input=995472，output=1530，reasoning=2889；多步骤 input 含重复上下文，不能视为单次上下文长度 |
| 费用 | remote_cost=0，cost_unavailable=false；仅本地 |
| 原始部分输出证据 | 348377 bytes；SHA-256 `52de93be7742dc9c36bb528ed804b698aac8cefcfa68f40dd0b1982ca7b64f5b`，审计仅保存可用摘要，不宣称原始流已持久化 |
| 最终审核记录 | 0 条；没有合法最终结论可据以批准 |
| 控制平面状态 | run=paused，task=waiting_review，call=unknown；reason=unknown_model_call |
| 运行后 LM Studio 状态 | idle，queued=0；不能以 idle 代替 UNKNOWN 核销 |

通过当前源码的 AgentFlow CLI 执行 authorize/start/status，使用测试库 mode=ro 读取最小审计证据。原始授权、测试、调用和事件记录位于 `.agentflow/runs/agentflow.db`。未重试、续接、追加模型调用、修改待审代码、提交或合并。

## 结论与后续边界

独立审核尝试未完成，不能宣布验收通过，也没有最终 findings 可据以判定代码被否决。真实 13 步事件支持“审核不再固定两步”，但不证明所有预算边界或完整修复正确。

本次按授权的 900 秒上限停止，不能静默延长。UNKNOWN 保留，不调用 resume/resolve-call 或擅自核销；重试前需要按既有流程核对会话及已完成结果，避免重复调用。

建议后续把 BUG-RB-01、BUG-RB-02 拆为较小的审核范围，同时保留共享授权/恢复的交叉检查，减少重复大上下文。若改调用次数、范围或超时，必须形成新计划哈希并由 Owner 批准。本次没有运行第三模型独立自测；确定性测试与不同模型审核须分开记录。
