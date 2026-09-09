# 本轮修复的真实 Qwen 审核全部通过

本报告仅声明资源预算、输出终止处理与审核隔离修复的确定性测试和 Qwen 审核门禁通过。**项目要求的第三模型独立自测尚未完成，不能据此宣布整个项目独立验收完成或允许合并。**

## 分项批准

| 范围 | 有效批准记录 | 结果 |
| --- | --- | --- |
| BUG-RB-01 审核步骤预算 | qwen-budget-isolated-20260908-01 / review-rb-01 | approved，零 findings |
| BUG-RB-02 输出能力与角色授权 | qwen-budget-isolated-20260908-01 / review-rb-02 | approved，零 findings |
| A+B 输出终止、审核隔离及补充证据 | qwen-budget-final-evidence-20260908-01 / rereview-output-evidence | approved，零 findings |

上述批准针对同一份 31 文件冻结候选，复审期间未修改源码。最后一次复审补齐了上次材料遗漏的完整 fail_call、service 调用路径、ModelRecord/ModelCapabilitySnapshot 类型验证与针对性检查；上次 P1/P2 已由原 Qwen Reviewer 重新评估为零 findings，不是实施方手工关闭。

旧运行按用户批准通过 CLI 取消以释放单活动运行限制；保留其两个 approved 任务、原始拒绝 findings、调用、测试和授权记录，不覆盖历史，也未重复执行两项已批准审核。

## 最后一次复审授权

- Plan: `reviewer-budgets-qwen-final-evidence-review-20260908`
- SHA-256: `79411f17f9d062b0cadbba11217af4aa282ccc62b12aa6d70c2a4f4f7f79ed7f`
- Authorization: `32c55cb4-0ed2-4394-9070-6981a322c47d`
- Run: `qwen-budget-final-evidence-20260908-01`
- 2026-09-09 04:18:17–04:23:10 UTC（当地 9 月 8 日）。
- 最终 run_state=completed，task state=approved；无重试、无新增修订。
- 本地 `qwen/qwen3.8-27b@8bit`，8 步、输出上限 16000、墙钟上限 1800 秒、授权 TTL 3600 秒。

## 新鲜测试与真实会话证据

- 本轮全量回归：480 tests，35.396 秒，OK。
- 两项补充检查：协议失败持久化及禁止重发计数、非法 context 类型前置拒绝；2 tests，0.214 秒，OK。测试前后冻结哈希校验通过。
- 收尾再次校验 31 文件，无漂移；复审 worktree 的 git status 为空；git diff --check 通过。
- Call: `c66edb84-8336-4ab7-90a7-761980505b2e`
- Session: `ses_f7b9c1873ffe4wUftZP9SDeH1N`
- Review: `831bb174-d898-4e8b-95cb-43ee5e8fd9c3`
- 原始回复：`{"approved":true,"findings":[]}`。
- 1 个完成步骤，terminal_reason=stop，tool_use_count=0；完整会话导出亦无工具调用。
- 输入 25827、输出 12、推理 2817 tokens，耗时 255456 ms，确认远程费用 0，费用未知调用数 0，test_double=false。
- 完整会话只读导出 113669 bytes，SHA-256 `15016d0243a4e4013cec2b8d089b9d23273d68131e3278d690816757abab0478`。

最终证据按 verification-before-completion 核对实际测试输出、数据库批准、会话及文件状态，不以模型自述单独作为完成依据。

## 留存与限制

权威数据保存在 `.agentflow/runs/agentflow.db`；最后一轮最小摘录为 `.agentflow/final-evidence-review-result.json`，前轮明细见 `2026-09-08-qwen-isolated-review-result.md`。

所有 fake implementation 都是明确标记的 no-op 占位；控制器执行的确定性测试不是第三模型自测。不同模型独立自测仍需另行安排并按模型/预算/材料范围授权。未提交、推送、合并、修改用户全局配置或更新安装；历史 UNKNOWN 未核销、未重试。
