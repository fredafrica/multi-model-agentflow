# 当前候选真实 Qwen 审核结果：未通过

## 授权与范围

- Plan: `reviewer-budgets-qwen-current-review-20260908`
- SHA-256: `e915d5f98563da76cdd73e7266ef50b8daeb7c796141237ba6b7106955f10ced`
- Authorization: `6c8ebca1-7671-43b7-a20a-eff9b9cb486d`
- Run: `qwen-budget-current-20260908-01`
- Branch: `codex/reviewer-budget-fixes`；未创建提交或合并。
- 本地 Qwen `qwen/qwen3.8-27b@8bit`，每次 8 步、输出上限 16000 tokens，零重试、零远程费用。
- 当前代码绑定 30 个文件的内容哈希；实际代码由 Codex 实施，Runner implementation 是明确标记的 fake no-op，不是真实实施模型。确定性测试不是第三模型独立自测。

## 本轮结果

| 项目 | 证据与结论 |
| --- | --- |
| 自动化测试 | 474 项，38.216 秒，OK；控制器总测试耗时 38350 ms，返回码 0 |
| 冻结内容 | 测试前后哈希校验通过 |
| BUG-RB-01 | 真实审核调用完成，返回协议有效 JSON，但含两条 P3；zero_findings 判定不通过 |
| BUG-RB-02 | 未执行；第一项失败后流水线停止 |
| A+B | 未执行独立审核；不能以第一项调用正常结束证明失败路径验收通过 |
| 总体 | run_state=failed；非 REVIEW_PASSED；第三模型独立自测仍缺失 |

## 真实调用审计

- Call: `98e1046f-b55b-43fe-b88c-e28dffb8033c`
- Session: `ses_f7bea1eaaffe3mOsJA3PnlegUX`
- 开始：2026-09-09 02:53:39 UTC；结束：03:06:18 UTC（当地 9 月 8 日）。
- 持续 757519 ms，6 个完成步骤，terminal_reason=stop，classifier_version=3。
- 累计输入 202806，输出 1306，推理 8302 tokens；累计输入包含多步重复上下文，不是单次 prompt 长度。
- 成本 0，test_double=false，调用状态 completed；审核任务 failed。调用完成不等于验收通过。
- 模型原始 `approved=true`，但含两条 finding，控制器按 zero_findings 保存 `approved=0`，未改写策略。
- 完整会话只读导出：182816 bytes，SHA-256 `a094788f8753a6b6be37d1c80e2f3bdd2969304d019d68b457dd1436d960489a`。采用临时文件捕获 stdout，避免管道截断；没有修改原始会话或历史数据库。
- 原始审核、调用元数据和测试输出已保存在 `.agentflow/runs/agentflow.db`；本轮摘录另存 `.agentflow/current-review-evidence.json`。

## 审核意见核对（不是独立复审）

1. P3：OpenCode 版本限制的报错可更明确。当前代码确实只接受已验证的 1.18.29，但已有 `OpenCode output-control version is unverified` 提示，并非完全无版本说明。可以改善版本诊断；不应未经验证放开新版本。
2. P3：`thinking` 子串拦截可能过宽，报错缺乏具体键。当前代码确有此行为，出于未知输出/推理覆盖项的 fail-closed 设计。改用很短的精确 denylist 可能漏掉别名；不能直接照搬建议而降低边界。若修订，优先改善安全诊断，兼容性放行须另有可验证规则。

## 额外发现：审核输入隔离不足

模型未遵守计划中的“只审核内联补丁，不调用工具、不读旧基线”要求。完整会话确认 10 次已完成工具调用：6 次 read、2 次 glob、2 次 grep；读取了审核工作树中的旧版 contracts.py、opencode_adapter.py、runner.py，并搜索到 resource_budgets 不存在。

该工作树是旧 HEAD，不含当前未提交补丁；内联补丁虽然最新，但允许读取旧基线造成输入混杂。没有模型写文件、shell 或外部网络工具调用记录。**此问题来自本次验收编排：仅用文字禁止读取，实际工具权限仍允许。不能把这次审核作为当前候选的干净独立验收证据。**

建议下一步先修正审核输入隔离：使审核只能接触经哈希确认的当前候选快照，或以确定性权限完全禁用读取工具；同时核对两条 P3 的处理，不降低 zero_findings、不手工标通过。此举改变当前授权范围/内容，须形成新方案并重新批准；本轮不自动修代码、不重试、不启动后续调用。
