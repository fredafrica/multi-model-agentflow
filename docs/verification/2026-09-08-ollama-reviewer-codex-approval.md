# 本地 Ollama Reviewer：Codex 最终批准

日期：2026-09-08，06:10 PDT。状态：**REVIEW_PASSED**。

批准范围：里程碑 20 的本地 Ollama packet-only Reviewer 实现、确定性验收和离线构建交付。原五组阻断 R01–R05（含 R04 混合有效/无效使用量的残余）已关闭；本范围内没有未解决的 P0/P1。此前 `2026-09-08-ollama-reviewer-codex-review.md` 保留为历史发现，本文件是最终结论。

```json
{"approved":true,"findings":[]}
```

这是用户委托的人工开发会话之独立源码审核结论，不是伪造的 AgentFlow 模型运行记录，也不表示已经执行真实服务 smoke、部署或 Git 整合。

## 关闭证据

| 项目 | 最终确认 |
| --- | --- |
| R01 权限 | 有效配置的全部权限项逐项检查；额外工具/模式中的 allow、ask 和无法判定的规则失败关闭 |
| R02 目录 | 推理 Popen 明确设置 cwd=review_root，与 --dir 相同，临时目录只读且不是仓库 |
| R03 中断 | signal、KeyboardInterrupt、通信 OSError 保留可确认 Token、会话、耗时与输出哈希；输出仍为空，UNKNOWN 保留，恢复不能重复派发 |
| R04 Token | 拒绝 bool、负数、错误类型；reasoning 不冒充 input/output；明确零与缺失区分。混合事件只累计确认部分，任何使用量事件不完整后，后续合法事件不能把全局标记恢复成完整可用；成功、失败、部分输出语义一致 |
| R05 配置 | 实际有效地址为空时拒绝，显式空端口和 query/fragment 标记拒绝，配置执行 OSError 转为受控不可用异常；普通合法回环路径继续可用 |

R04 最后一次针对性复核包含：有效事件先/后顺序、非法 input、缺单字段、全缺、bool/负数、reasoning-only、step_finish 无 tokens、明确零及普通非使用量事件。修复前独立反例出现 33 个失败子情境，修复后全部通过。没有通过放宽断言或丢弃合法 Token 让测试通过。

## 最终独立验证

- 全量：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 -m unittest discover -s tests -q`，**442 tests，34.754 秒，OK，退出 0**。
- 独立检查：原五组反例 8 项、正向检查 3 项、最后 Token 完整性检查 3 项，共 **14 tests，OK，退出 0**；其中包含参数化边界情境。它们与项目 442 项分别统计，不混算。
- compileall 与 tracked diff 空白检查：退出 0。
- 从本次最终未提交 src 重新离线构建 wheel、创建临时 venv 并离线安装；在仓库外清除 PYTHONPATH 后导入 LocalOllamaReviewerAdapter、执行 agentflow --help 均成功。
- 最新源码 SHA 在测试与构建前后相同；findings.md / progress.md 未改变。实际源码修改仍限原白名单，未提交、推送、合并或更新全局配置/安装。

独立诊断用例暂存于 `/tmp/ollama-formal-review.r1voOg/`；核心反例已由实施者纳入项目正式测试。本轮独立构建产物：`/tmp/ollama-approved-build.0C4lZX/wheels/multi_model_agentflow-0.1.0-py3-none-any.whl`，SHA-256：`32829abeff6e53ff861823d9e49d6a1418519d29e51c561f45c98c81d6eed9e4`。

审核快照：分支 `feat/ollama-local-reviewer`；HEAD `0545b64c0338f363c0aca862c251f350f48aa228`；审核包含未提交修改。

```text
src/agentflow/opencode_adapter.py b1c7a2ed709912665723ea32f0bb66c36c962a44e55c991ffdb8a81da61a99ae
tests/test_local_ollama_reviewer.py 31a4a185c06c45c38dd1bd15eec34ad0a61eff5a6309571204cf54e18bb6e903
tests/test_remote_reviewer.py 980acd69481479a14a649023822a107e8807e659394db546f13ba3147ac3fd91
```

## 保留边界，不追加返工

- 推理默认 900 秒，发现默认每次 15 秒，清理宽限 5+2 秒；不是整个流程严格 900 秒的墙钟承诺。当前 invoke 仍有三次配置读取，不以去除这次冗余作为阻断。
- packet-only 是应用权限、最小输入和目录隔离，不是 OS 级绝对隔离；可信本机 Ollama 是前提。
- 历史 smoke 不等于当前真实可调用，本轮没有发起真实模型 smoke。批准不扩张为真实服务可用性认证。
- 实施者报告中“仅 int ≥ 0 或布尔 False”是笔误：实际代码和测试都拒绝 bool；“每次至多两次配置解析”应限定为 prepare 阶段，整个 invoke 仍三次。此类报告措辞以本次源码核实为准，不另开返工。
- 本批准只关闭里程碑 20，不替历史里程碑 19 补造独立批准，不授权提交、发布、全局安装或额外模型调用。

Codex 仅更新批准记录与里程碑/MVP 状态，没有代替 DeepSeek 修改本轮产品源码。最终决定基于独立反例、实际代码与新运行的验证证据，不以实施模型自述代替验收。
