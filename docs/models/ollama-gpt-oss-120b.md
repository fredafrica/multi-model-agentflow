# 候选模型资料：Ollama GPT-OSS 120B

> 本文件是人工可读候选模型资料，不是运行配置源，也不写入任何全局注册表。通用核心不得硬编码其中的名称、family、版本或量化；这些字段只在计划授权显式指定时才进入运行。
>
> 内容来源为 Owner 原始任务提供的历史证据，Codex 从目标 OpenCode 用户消息核对；本轮未复验原始 smoke 产物。

| 字段 | 记录值及证据限制 |
| --- | --- |
| provider / model_id / family | `ollama` / `gpt-oss:120b` / `gpt-oss` |
| architecture / 参数量 | gptoss / 约 116.8B，历史报告 |
| quantization / context | MXFP4 / 131072，历史报告，不是所有正式请求的固定参数 |
| capabilities | completion、tools、thinking；Reviewer 权限仍禁用工具 |
| 硬件状态 | 曾显示 100% GPU；当前加载状态未验证 |
| 历史 smoke 日期 | 标识指向 2026-09-06；原始精确时间戳/时区未提供 |
| smoke 1 | gpt-oss-ollama-reviewer-smoke-20260906 |
| smoke 2 | gpt-oss-ollama-task011-review-packet-smoke-20260906 |
| 历史结果 | 两次均报告单一 JSON `{"approved":true,"findings":[]}`、无工具、reported cost=0 |
| 历史 smoke 2 指标 | 约 2033 input / 71 output Token / 5.6 秒 |
| 第一笔精确指标、两笔原始文件哈希 | 未提供，禁止补写猜测值 |
| 当前配置观察 | 2026-09-08 Codex 只读检查发现本地 baseURL 配置及该 model 条目；仅 configured/discoverable 证据 |
| 当前可调用、正式独立审核 | 本轮未验证；不能据两次历史 smoke 宣布正式项目通过 |

说明：

- 历史 LM Studio GPT-OSS 的 peg-native 问题只属于被观察到的 LM Studio 路径，不能外推 Ollama。
- 原始 smoke 文件缺失不阻挡代码交付，只作为资料限制；不访问其它工作区或真实重跑来补齐它。
