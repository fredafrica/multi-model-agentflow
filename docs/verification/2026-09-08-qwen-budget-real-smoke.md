# Qwen 真实 Reviewer 冒烟测试

结论：一次真实本地 Reviewer 调用成功；不是资源预算修复的独立验收批准。

## 授权与环境

- 用户明确批准计划 SHA-256：`67035299fdefd6f2c22777b6baaeb2704b4cd3d5c83b668c5ed10f6e964dd681`。
- 计划：`.agentflow/plan.json`，ID `reviewer-budget-qwen-smoke-20260908`，version 1。
- 授权 ID：`aee922c2-bacc-4f20-a31c-eeafd353528a`；有效期一小时。
- Run：`qwen-budget-smoke-20260908-01`。
- 通过当前分支源码的 `PYTHONPATH=src python3.11 -m agentflow.cli` 执行 authorize/start/status/logs/cost；没有绕过控制平面调用模型。
- OpenCode 1.18.29；LM Studio 模型 `qwen/qwen3.8-27b`，已加载版本 `qwen/qwen3.8-27b@8bit`。
- 输出能力 32768、context 262144 来自显式 OpenCode 模型配置，不是模型自述或本轮测出的能力。选定配置摘要 SHA-256：`856601c3ce467055a00a4ff72fd24d2dad4178b60b85a41633c7a22051e44060`。
- B0/S1/D1，仅本地；新建隔离工作树及运行证据，无提交、合并、加载/下载模型、全局配置更新或业务数据库修改。

## 实际结果

| 项目 | 结果 |
| --- | --- |
| Run 状态 | completed，start 命令 exit 0 |
| 真实调用 | 1 次 review，test_double=0 |
| 实施阶段 | 1 次明确标记的 no-op fake，test_double=1；没有真实实施或文件写入 |
| Review call ID | `6fc60835-5e14-4521-9925-fc70108b176e` |
| 审核对象 | 计划内 `def add(a, b): return a + b` 小样本，不是预算修复代码 |
| 返回 | `{"approved":true,"findings":[]}`，只批准该 smoke 样本 |
| 耗时 | 43687 ms，低于 180 秒推理超时 |
| configured steps / 实际完成步骤 | 12 / 1；terminal_reason=stop |
| 授权输出 / effective output | 16000 / 16000；能力快照 ID `e37fa239d6167bb6ca6307fbfce8d3dfeff13926ecc3ea8a85177b979f1aa747` |
| OpenCode 报告 Token | input=4952，output=12，reasoning=489；保留各字段，不自行合并计费语义 |
| 工具调用 | 0 |
| 远程费用 | 0 USD，cost_unavailable=false |
| 隔离工作树变化 | `git status --short` 为空 |
| 重试、续接、复审 | 均为 0 |

原始证据保存在 `.agentflow/runs/agentflow.db` 的授权、调用、review、事件及费用记录。系统 sqlite3 只读打开失败后，使用 Python sqlite3 的 mode=ro 成功读取；没有更改数据库状态来取得结果。

## 证明范围与剩余工作

真实证明：当前源码能经 AgentFlow → OpenCode → LM Studio 调用 Qwen，严格配置预检通过，返回合法协议并保留真实预算/使用量审计；步骤上限 12 不会强迫跑满。

未证明：本轮没有捕获 HTTP 层最终请求参数；输出 12 小于 16000 本身不证明硬上限。参数映射仍依据既有 OpenCode 1.18.29 离线源码路径证据及本轮成功通过的运行时配置复核。没有真实触发多轮步骤耗尽、输出截断、rereview、Ollama 或远程调用。未实施真实代码自测或独立审核，不能把数据库中该样本的 approved 扩展为里程碑 21 的批准。

按批准范围本轮结束，不自动追加调用。下一步的多轮/复审或独立验收须另列计划及取得授权。
