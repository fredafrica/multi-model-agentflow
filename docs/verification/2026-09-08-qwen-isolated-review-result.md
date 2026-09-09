# 隔离修复后的真实 Qwen 复验：两项批准，一项待复审

## 授权与状态

- Plan: `reviewer-budgets-qwen-isolated-review-20260908`
- SHA-256: `51b2d07faad73ff6ebbab0a9506085e6e746c3386b42cc88aae36146e2f74feb`
- Authorization: `3abdc06b-5593-4158-b8de-b24c38849e47`
- Run: `qwen-budget-isolated-20260908-01`
- 2026-09-09 03:16:03–03:51:55 UTC（当地 9 月 8 日）。
- 本地 `qwen/qwen3.8-27b@8bit`，8 步、每步输出上限 16000、每次墙钟上限 1800 秒，零自动重试。
- 最终 `run_state=paused`、`control_state=PAUSED`；checkpoint 原因 `blocking_finding`，任务 `review-output-termination`。不恢复、不手工修改批准状态。

## 分项结果

| 项目 | 确定性测试 | Qwen 审核 | 工具调用 | 模型耗时 |
| --- | --- | --- | ---: | ---: |
| BUG-RB-01 | 480 项通过，33.129 秒 | approved=true，findings=[]；任务 approved | 0 | 600226 ms |
| BUG-RB-02 | 480 项通过，34.103 秒 | approved=true，findings=[]；任务 approved | 0 | 734872 ms |
| A+B 与隔离修复相关检查 | 480 项通过，43.605 秒 | approved=false；一条 P1、一条 P2；待复审 | 0 | 700381 ms |

三次调用全部单步正常 `stop`，没有 length/timeout/UNKNOWN，没有重试。三个完整会话均核实无 tool part，与审计中的 tool_use_count=0 一致。**旧工作树读取问题在本轮三次真实调用中均未复现。**

每项测试前后校验冻结文件；结束后再次核对 31 个文件，无内容漂移。三个运行工作树 `git status --porcelain` 均为空。未改源码、全局配置，未提交、推送或合并。

## 使用量与原始证据

| task | call_id | session_id | 输入 | 输出 | 推理 |
| --- | --- | --- | ---: | ---: | ---: |
| review-rb-01 | 2796a490-5190-4a38-81b3-810957d42bfc | ses_f7bd51a64ffe3isfxEz8WM0sns | 29210 | 12 | 6858 |
| review-rb-02 | 8abb88d2-3d9a-45d2-bd63-6f3a95134a63 | ses_f7bcb6667ffeR6WIkTyXdl1cws | 29517 | 12 | 9025 |
| review-output-termination | ed66503b-0629-4418-93ae-91be508fc587 | ses_f7bbf7e92ffeNC50hIp8V9XO2k | 21017 | 485 | 9751 |

全部远程费用确认 0；真实 Reviewer test_double=false。三个 fake implementation 是明确标记的 no-op 占位，不是实施或第三模型自测证据。使用量来源 opencode_json_events，分类器版本 3。

完整会话只读导出哈希（使用临时文件捕获，避免 stdout 管道截断）：

- RB-01：143979 bytes，`0da780bb63eecd1847a66400d5e2ec90e5a48b685fc36ebe6ebd88d656f61b58`
- RB-02：155394 bytes，`1ad9afbb636a2fb3de1acc960bf1b894680f2699526bfc6296813959c0132a8f`
- A+B：124228 bytes，`e52250452dfffd5a77d0547503fe952e6da4755ddc8148dae2e2de9a04b6c25d`

权威记录为 `.agentflow/runs/agentflow.db`；最小调用/测试/审核摘录保存于 `.agentflow/isolated-review-evidence.json`，完整测试命令保留在数据库与冻结计划中。

## 最后一项发现及本地核查

以下是实施方核查，不是独立复审；原 P1/P2 保持未关闭。

### P1：protocol_error 没有写入者，恢复 guard 可能无效

Reviewer 指出内联 diff 中未看到 failure_kind 写入者，且明确保留“除非未展示的 fail_call 内部注入该字段”的条件。

完整当前代码：`service.py` 在协议异常路径调用 `fail_call(..., failure_kind="protocol_error")`（约 213–217 行）；`database.py` 的 `fail_call` 在 2175–2176 行复制元数据并写入该字段，再保存 raw_metadata_json。因此写入者存在，只是未修改的函数正文不在审核 diff 中。

额外无模型诊断复用了 `OutputTerminationRunnerTests` 的临时仓库/数据库，真实执行无文本 parser→service→database→resume 路径，并直接断言：

- 持久化 failure_kind 为 protocol_error；
- `nonretryable_output_failures(run_id) == 1`；
- resume 拒绝，调用数未增加。

结果通过。没有修改业务数据库或生产代码。

### P2：context 非整数可能进入预算比较造成 TypeError

完整当前 `ModelRecord.__post_init__`（contracts.py 310–314 行）对非空 context_length 调用严格整数验证；ModelCapabilitySnapshot 也有同类校验，不会把非法字符串留到预算比较。该类型约束未包含在第三项材料中。

重新运行现有 `OutputBudgetAdapterTests.test_discovery_rejects_boolean_and_float_context`，覆盖 True、262144.0、'262144'、0；均在发现阶段以 ValueError 拒绝。与上述 P1 诊断合计 2 项检查，0.214 秒，OK。Reviewer “测试只提供正确整数”的假设不符合完整测试代码。

## 结论与边界

两个原始资源预算问题已获得本轮 Qwen 零 findings 批准；审核读取旧文件的问题已有三次真实零工具调用证据。**整体独立验收仍未全部完成**：第三项须补充未修改的 fail_call、完整类型校验及上述原始诊断证据，再由原 Reviewer 复审，不能以实施方分析自行关闭 P1/P2。

当前计划遇到 finding 必须停止，不能原地更改 packet 或借 resume 重发；补充材料须新哈希批准。不同模型独立自测仍缺失，不把 fake no-op 和控制器确定性测试当作该角色。历史失败、授权与 UNKNOWN 全部保留。
