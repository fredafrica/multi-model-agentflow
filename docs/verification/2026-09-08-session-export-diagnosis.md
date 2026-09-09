# 超时会话导出截断：诊断与处理

结论：已解决本次诊断工具读取不完整的问题；没有发现会话 JSON 数据损坏，也没有可复用的最终审核结论。未追加模型调用。

## 对照实验

对同一个 OpenCode 1.18.29 会话 `ses_f7cacce6fffeEGj3Sw79DD3dN1` 执行只读 export，仅改变标准输出接收方式：

| 接收方式 | 字节数 | JSON |
| --- | ---: | --- |
| subprocess.PIPE | 65536 | 字符串被截断，解析失败；进程退出码仍为 0 |
| tempfile.TemporaryFile | 574231 | 完整解析，15 条消息 |

文件方式连续复核两次，均为 574231 字节，SHA-256 均为 `26ec882c8fab7b40fe9c4f8ee8cc4d5f6c02422637cc672d1eb557a574a7a81a`。

故障定位在当前 OpenCode export 的管道输出路径，而不是预算修复、AgentFlow 数据库或 JSON 解析器。缓冲输出未完整送达是与实验一致的机制解释；本轮未修改或重编译 OpenCode，不把该解释冒充上游源码确认。

处理方式：让 `opencode export` 的 stdout 直接接到 `tempfile.TemporaryFile()`，等待进程结束后 seek(0)、读取并解析；核对 session ID。临时文件关闭后自动清理，不打印或持久化整份可能包含上下文的原始会话。此处是修正诊断调用方式，不新增生产恢复通道，也无需改动预算修复源码。

## 上次会话的真实终点

- 完整导出包含 15 条消息。
- 倒数第二条 assistant 完成于 tool-calls，最后一次 read 成功；内容仍在检查输出限制函数。
- 最后一条 assistant 没有 finish 标记，只有 step-start 与 reasoning，没有最终审核 JSON。
- 因此没有最终结论可以恢复为 approved。旧 run 及调用 UNKNOWN、累计 Token/费用全部保留，不调用 resume 或擅自核销。
- 先前观察的 LM Studio idle 仅证明当时没有活动推理，不等同于业务调用成功。

## 后续审核安排

新计划 `.agentflow/split-review-plan.json` 已由 AgentFlow plan show 解析，哈希为 `c5774837e8702223ff720d197c7ed7f8570f0ad49a6866c83a4aaef005701ab0`，尚未批准/执行。

- 两个任务分别检查 BUG-RB-01、BUG-RB-02；共享的合同、授权及恢复差异仍随包提供。
- 冻结补丁及相关测试摘录直接进入计划哈希，正文分别为 45431、59660 字符；不是依靠旧 baseline 工作树猜测当前代码。
- 禁止模型重读旧工作树，要求直接返回最终 JSON。工具禁用要求属于任务指令，LM Studio 现有只读权限仍允许 read/glob/grep；不能声称新增了硬性禁工具适配器。
- 每个任务开始前，确定性程序核对原目录 31 文件哈希并运行全量测试，之后再次核对；这些是控制程序检查，不冒充第三模型独立自测。
- 最多两次真实本地 Qwen review，各 8 步、8000 输出 Token、900 秒。无重试/续接；前一任务出现 finding、失败或 UNKNOWN 会暂停，不能承诺两次必定都执行。
- 这是新的较小审核范围，不恢复旧会话。仍需 Owner 批准新哈希；本轮没有收费调用、源代码变更、提交、合并或全局配置变更。
