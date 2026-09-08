# 本地 Ollama Reviewer 自测交付报告

日期：2026-09-08。执行者：DeepSeek V4 Pro（实施 + 确定性自测）。独立复核：Codex（待进行）。

> 第二轮：Codex 正式复核结论为 CHANGES_REQUIRED，五组阻断 R01–R05 已按最小修复关闭，反例与 Runner/数据库/恢复测试已纳入正式测试，详见第 1.5 节。第三轮：Codex 复现的 R04 残余（有效事件后再出现缺失/非法 input/output 仍被误标完整）已用粘性 `usage_incomplete` 关闭，混合/顺序/缺字段/明确零反例覆盖成功、失败、部分三条路径。本轮未修改 Codex 报告或交接提示词，未放宽任何断言。

## 1. 工作单元与缺陷修复结论

| 工作单元 | 结论 | 关键位置与证据 |
| --- | --- | --- |
| A（端点解析 + 实际配置绑定） | 完成 | `src/agentflow/opencode_adapter.py`：`ollama_host_is_loopback` + `_normalize_ollama_endpoint`/`_normalize_loopback_host`/`_parse_decimal_port`/`_parse_bare_ollama_endpoint`（标准库 `urlsplit` + `ipaddress`）；shared `_run_opencode_packet_review` 新增 `prepare_environment` 回调；`LocalOllamaReviewerAdapter._prepare_local_environment` 每次调用重新验证 resolved 配置、绑定端点、去代理并二次复核 |
| B（发现/路由/调用分类/审计） | 完成 | `discover`/`require_model`/`invoke` 及 `parse_opencode_json` 的 usage 审计字段；`adapters.py` 的 provider+role 失败关闭；`cli.py` 的 ollama 仅 review 装配与前置拒绝；`runner.py` 的 `ReviewerUnavailableError` 路由衔接 |
| C（真实 Runner 无费用集成） | 完成 | `tests/test_local_ollama_reviewer.py::LocalOllamaIntegrationTests`（FakeAdapter 实现 + stub subprocess 本地 Reviewer + 真实 Runner） |
| D（文档与历史资料） | 完成 | `docs/requirements.md`、`docs/mvp.md`、`docs/architecture-decisions.md`、`task_plan.md`、`docs/models/ollama-gpt-oss-120b.md`、本报告 |

缺陷 H01–H10 修复位置与证据：

| ID | 修复位置 | 简短证据 |
| --- | --- | --- |
| H01 | `opencode_adapter.py` 顶部 import `ReviewerUnavailableError` | `discover()` 捕获该异常不再 NameError，见 `test_remote_resolved_endpoint_is_not_swallowed_by_discovery` |
| H02 | `_normalize_ollama_endpoint` + `ollama_host_is_loopback` | `OllamaLoopbackTests.test_loopback_boundary`、`test_determination_is_lexical_and_never_resolves_dns` |
| H03 | `_prepare_local_environment` + `prepare_environment` 回调 | `test_proxy_variables_are_removed_and_endpoint_bound` 断言 `OPENCODE_CONFIG_CONTENT` 端点、代理移除、`NO_PROXY='*'` |
| H04 | `discover()` 无计划时 `family=None` | `test_unplanned_discovery_reports_unknown_family_not_gpt_oss` |
| H05 | `parse_opencode_json` 增加 `usage_unavailable`/`token_source` | `UsageParseTests` 四项 |
| H06 | `_completed_process` helper | 全部 invoke/integration 测试可运行 |
| H07 | `test_packet_isolation_and_all_tools_denied` 断言两层 permission 配置 | 全局 + agent 两层 `*` 及全部工具 deny |
| H08 | 删除 `_completed_config`/`_CAPTURED_CONFIG` 半成品 | 测试文件无残留占位 |
| H09 | 补齐失败/UNKNOWN/路由/授权/JSON 闭环测试 | 见第 3 节矩阵 |
| H10 | 四份规范 + 模型资料 + 本报告 | 见第 2 节 |

## 1.5 Codex 正式复核发现 (R01–R05) 的修复

| ID | 严重度 | 问题 | 最小修复 | 反例/回归测试 |
| --- | --- | --- | --- | --- |
| R01 | P1 | `_verify_deny_permission` 只核对 `*` 与 14 个工具名，额外工具 `allow` 可漏过 | 逐 key/value 严格核对：任何非 `"deny"`（含 `allow`/`ask`/嵌套规则）失败关闭；额外全 deny 工具接受 | `test_extra_tool_allow_in_global_permission_is_rejected`、`test_extra_tool_allow_in_agent_permission_is_rejected`、`test_ask_or_nested_permission_value_is_rejected`、`test_extra_full_deny_tool_is_accepted` |
| R02 | P1 | shared `_run_opencode_packet_review` 的 Popen 缺 `cwd`，仅 `--dir` 指向 review_root | Popen 增加 `cwd=str(review_root)` | `test_packet_isolation_and_all_tools_denied` 捕获 Popen kwargs，断言 `cwd == --dir == review_root`、非仓库、不可写 |
| R03 | P1 | 中断/信号终止丢失 usage/session 证据（前版 `(b"", b"")` 丢弃） | `KeyboardInterrupt`/`OSError` 分支 `_bounded_drain` 返回 `(drained, cleanup_incomplete)`，`parse_opencode_partial_usage` 携带 `termination_reason=interrupted/communication_error`；负 returncode 路径解析 stdout 并标记 `signal_terminated` | `test_signal_terminated_preserves_usage_evidence`、`test_communication_oserror_preserves_evidence`、`test_interrupt_without_usage_is_marked_unavailable`、集成 `test_signal_terminated_review_pauses_with_evidence_and_blocks_resume`；`test_interrupted_remote_process_is_unknown_and_not_retried` 适配 `(b"", False)` |
| R04 | P1 | 非法/仅 reasoning token 被误记为有效；混合有效/无效事件把缺失/非法 input/output 冒充完整可用 | 新增 `_valid_token_value`（仅 int ≥ 0 或布尔 False），删除 `_token_total`；`_scan_usage_events`/`parse_opencode_json` 跟踪 `saw_input`/`saw_output` 并持续记录粘性 `usage_incomplete`：任何携带 tokens 的事件（或应报告使用量的 `step_finish`）若 input/output 缺失或非法即置位、后续有效事件不清除；`saw_usage = (saw_input and saw_output) and not usage_incomplete` | `test_invalid_token_values_are_unknown_not_confirmed_zero`、`test_reasoning_only_is_recorded_but_input_output_unknown`、`test_mixed_valid_and_invalid_events_preserve_confirmed_only`（已更正期望为 unavailable）、`test_usage_availability_is_sticky_across_all_paths`（成功/失败/部分三路径 × 10 反例：有效后无效、反向顺序、缺单字段/全缺/非法、明确零、多步合法、非 usage 事件） |
| R05 | P1 | 空端点/空端口、空 baseURL、配置执行异常分类遗漏 | `_normalize_ollama_endpoint` 拒绝空端口（`netloc` 以 `:` 结尾）与 `?`/`#`；`_parse_resolved_ollama_config`/`_check_endpoint_override` 拒绝空/纯空白 baseURL；`_read_resolved_config` 捕获 `PermissionError`/`OSError`、`_discover_remote_model_ids` 捕获 `OSError` → `UnsupportedProviderError` | `test_loopback_boundary` 增补 `http://localhost:/v1`、`http://localhost/?`、`http://localhost/#`；`test_missing_provider_and_missing_base_url_are_rejected` 增补 `""`/`"   "`；`test_config_discovery_permission_error_is_unsupported_provider` |

修复文件：`src/agentflow/opencode_adapter.py`（R01–R05）、`tests/test_local_ollama_reviewer.py`（新增 13 项 + 增强）、`tests/test_remote_reviewer.py`（`_bounded_drain` mock 适配）。未修改四份规范文档与 Codex 报告/交接提示词。

## 2. 修改文件清单

本轮修改（未提交 diff）：

| 文件 | 性质 |
| --- | --- |
| `src/agentflow/opencode_adapter.py` | Qwen 初稿 + 本轮重写端点解析/配置绑定/usage 审计（主要实现） |
| `src/agentflow/adapters.py` | Qwen 初稿 + 本轮收敛为 provider+role 失败关闭语义 |
| `src/agentflow/cli.py` | Qwen 初稿 + 本轮确认 ollama 仅 review 装配与前置拒绝 |
| `src/agentflow/runner.py` | Qwen 初稿 + 本轮确认 `ReviewerUnavailableError` 路由衔接 |
| `tests/test_local_ollama_reviewer.py` | 本轮从半成品占位重写为 54 项（含 R01–R05 反例与 Runner 集成恢复测试） |
| `tests/test_remote_reviewer.py` | 本轮 `_bounded_drain` mock 适配 `(drained, cleanup_incomplete)` 双值返回 |
| `docs/requirements.md`、`docs/mvp.md`、`docs/architecture-decisions.md`、`task_plan.md` | 本轮 |
| `docs/models/ollama-gpt-oss-120b.md`、`docs/verification/2026-09-08-ollama-reviewer.md` | 本轮新建 |

新增规范条目：`PLAT-06`、`MODEL-14`、`MVP-A32`、`MVP-A33`、`AD-45`、MVP 2.8。这些条目在四份文档中各有且仅有一个定义，引用编号有效。

## 3. 验收矩阵 O01–O28 映射

| ID | 测试方法（`tests/test_local_ollama_reviewer.py` 内为主） |
| --- | --- |
| O01 | `OllamaLoopbackTests.test_loopback_boundary`、`test_determination_is_lexical_and_never_resolves_dns` |
| O02 | `LocalOllamaConfigBindingTests.test_remote_resolved_provider_endpoint_is_rejected_without_inference` |
| O03 | `test_missing_provider_and_missing_base_url_are_rejected` |
| O04 | `test_explicit_host_conflicting_with_resolved_endpoint_is_rejected`、`test_managed_override_back_to_remote_is_rejected_on_recheck` |
| O05 | `test_model_endpoint_override_is_rejected`、`test_unknown_transport_is_rejected`、`test_invalid_config_json_is_rejected`、`test_config_discovery_timeout_is_controlled_error`、`test_missing_executable_is_unsupported_provider` |
| O06 | `test_managed_override_readding_allow_permission_is_rejected`、`test_managed_override_back_to_remote_is_rejected_on_recheck`、`test_extra_tool_allow_in_global_permission_is_rejected`、`test_extra_tool_allow_in_agent_permission_is_rejected`、`test_ask_or_nested_permission_value_is_rejected`、`test_extra_full_deny_tool_is_accepted` |
| O07 | `test_proxy_variables_are_removed_and_endpoint_bound` |
| O08 | `LocalOllamaDiscoveryTests.test_discovered_model_is_discoverable_with_zero_cost_not_callable`、`test_model_list_unavailable_reports_planned_models_unavailable`、`test_require_model_raises_when_model_not_discovered`、`test_require_model_passes_for_discovered_loopback_model` |
| O09 | `test_unplanned_discovery_reports_unknown_family_not_gpt_oss` |
| O10 | `LocalOllamaGuardTests.test_rereview_role_is_accepted`、`test_write_roles_and_non_read_only_are_rejected_before_discovery`、`test_constructor_rejects_remote_or_foreign_planned_model` |
| O11 | `LocalOllamaRoutingTests.test_cli_builds_review_only_role_adapter`、`test_cli_rejects_ollama_implementation_before_assembly`、`test_cli_rejects_non_local_ollama_review_model`、`test_cli_rejects_provider_outside_authorization` |
| O12 | `test_router_keeps_default_route_and_denies_unknown_provider` |
| O13 | `LocalOllamaIntegrationTests.test_ollama_fallback_for_write_role_is_never_invoked` |
| O14 | 既有等价用例 `tests/test_remote_reviewer.py::test_unavailable_primary_reviewer_uses_independent_fallback`、`test_same_family_fallback_is_not_used_and_task_waits_for_review`、`test_independent_fallback_replaces_same_family_primary_without_calling_it` |
| O15 | `test_packet_isolation_and_all_tools_denied`（cwd/`--dir` 只读非仓库目录、JSON-only prompt） |
| O16 | `test_packet_isolation_and_all_tools_denied`（两层 deny、steps=2、仅 ollama） |
| O17 | `UsageParseTests.test_multi_step_tokens_accumulate`、`test_explicit_zero_tokens_is_confirmed_not_unknown`、`test_missing_tokens_is_marked_unavailable_not_zero`、`test_invalid_token_values_are_unknown_not_confirmed_zero`、`test_reasoning_only_is_recorded_but_input_output_unknown`、`test_mixed_valid_and_invalid_events_preserve_confirmed_only`、`test_usage_availability_is_sticky_across_all_paths` |
| O18 | `UsageParseTests.test_local_cost_is_confirmed_zero_and_remote_cost_validation_unchanged`、`LocalOllamaInvokeTests.test_local_call_reports_confirmed_zero_cost` |
| O19 | `test_positive_nonzero_exit_is_protocol_error_without_fallback` |
| O20 | `test_timeout_is_unknown_with_local_zero_cost`（复用既有 `_bounded_drain`/`_merge_overlapping_output_bytes` 字节合并） |
| O21 | `test_interrupted_process_is_unknown`、`test_signal_terminated_preserves_usage_evidence`、`test_communication_oserror_preserves_evidence`、`test_interrupt_without_usage_is_marked_unavailable`、集成 `test_signal_terminated_review_pauses_with_evidence_and_blocks_resume` |
| O22 | `test_step_limit_stream_is_incomplete_not_unavailable`、`LocalOllamaIntegrationTests.test_step_limit_review_pauses_without_review_row_and_resume_is_blocked`；疑似/quoted 标记复用既有 `tests/test_opencode_adapter.py` 步骤耗尽分类用例 |
| O23 | 既有 `tests/test_opencode_adapter.py`/`test_runner.py` 的 JSON-only 协议解析用例（fence/散文/缺字段/错类型） |
| O24 | 既有 `tests/test_remote_worker.py::test_zero_findings_policy_blocks_non_p0_p1_finding` 及审核接受策略用例 |
| O25 | 既有 `tests/test_review_independence.py`（`test_fallback_model_cannot_self_review`、`test_important_task_rejects_same_family_contributor`） |
| O26 | 既有 plan_hash/序列化往返用例（`test_core.py`、`test_cli.py`、`test_remote_reviewer.py`） |
| O27 | 全量回归 442 项通过，无 skip |
| O28 | 见第 5 节构建/安装结果与第 6 节文件范围说明 |

## 4. 端点/配置绑定实现方式（脱敏）

1. `_normalize_ollama_endpoint` 用 `urlsplit` + `ipaddress` 严格归一；仅接受大小写无关 `localhost`（规范化为 `127.0.0.1`）、IPv4 `127.0.0.0/8`、IPv6 `::1`；拒绝子域/`localhost.`/IPv4-mapped/zone id/userinfo/query/fragment/非 http(s)/非法端口/内部空白。布尔接口捕获异常返回 False。
2. 每次 `invoke` 前，`_prepare_local_environment` 在临时只读 `review_root` 以参数数组执行 `opencode debug config --pure`，程序内解析 `provider.ollama` 的 npm、`options.baseURL` 与所选 model 条目；缺 provider/npm、未知 transport、非法 JSON、非回环端点、model 级 baseURL/api.url override 与 provider 端点不一致均以 `ProviderNotConfiguredError` 拒绝。
3. 显式 `ollama_host`/`OLLAMA_HOST` 若存在，必须严格回环且与有效配置归一后一致，冲突拒绝；未设置时以真实 resolved baseURL 为准。
4. 已验证端点 + 全局/agent 两层 deny 配置 + `enabled_providers=["ollama"]` 写入子进程 `OPENCODE_CONFIG_CONTENT`；移除大小写 HTTP/HTTPS/ALL_PROXY，设置 `NO_PROXY`/`no_proxy='*'`。
5. 用同一 cwd/environment 再执行一次 bounded resolved-config 检查，比较 transport/端点/enabled_providers/agent steps/两层 permissions；不一致或多出 allow 权限拒绝。每次调用至多两次解析。
6. Popen 使用已验证 environment 与 `cwd=review_root`，`--dir` 同指 review_root。完整 resolved config 不写入日志、上下文或本报告。

## 5. 构建与验证结果

定向测试（均退出码 0，无失败/错误/skip）：

- `tests/test_local_ollama_reviewer.py`：54 通过（含 R01–R05 反例与集成恢复）
- `tests/test_opencode_adapter.py`：57 通过
- `tests/test_remote_reviewer.py`：34 通过

全量测试（最终源码与测试版本）：

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 -m unittest discover -s tests -q
Ran 442 tests in 34.089s / OK，退出码 0，无失败/错误/skip
```

静态与构建：

- `compileall -q src tests`：退出码 0
- `git diff --check`（tracked 文件）：退出码 0，无空白错误
- `git diff --no-index --check /dev/null tests/test_local_ollama_reviewer.py` 与新建 Markdown：仅“有差异”退出码 1，无空白错误行输出
- 离线 wheel 构建：`multi_model_agentflow-0.1.0-py3-none-any.whl` 构建成功
- 隔离安装：`uv venv` + `uv pip install --offline --no-index` 成功；`import agentflow`、`from agentflow.opencode_adapter import LocalOllamaReviewerAdapter`、`ollama_host_is_loopback` 冒烟（空端口 `http://localhost:/v1` 被拒）、`agentflow --help` 均成功
- wheel SHA-256：`0ce5b325af56945c9d607339141b27295c9163e727c1415661424cfa01cf26dc`（哈希随打包时间/内容变化，非固定期望值）

## 6. 证据缺口与未执行项

- 未执行真实模型 smoke、未验证当前模型加载或正式审核；`configured/discoverable` 不等于 `callable_verified`。
- 历史 smoke 仅来源于 Owner 原始任务提供、Codex 从 OpenCode 用户消息核对的证据；第一笔精确指标与两笔原始文件哈希未提供，未补写猜测值。
- lint/type-check 未配置（无 Ruff/mypy/pyright），属于“不适用/未配置”，非通过亦非阻断。
- `findings.md` 与 `progress.md` SHA-256 未变，与计划核对值一致。
- 未 commit/push/merge，未更新全局安装、canonical/installed Skill 或 OpenCode/Ollama 全局配置。

## 7. 当前状态与剩余风险

- 分支：`feat/ollama-local-reviewer`；HEAD：`0545b64c0338f363c0aca862c251f350f48aa228`。
- 未提交文件：`src/agentflow/{adapters,cli,opencode_adapter,runner}.py`（修改）、`docs/{requirements,mvp,architecture-decisions}.md` 与 `task_plan.md`（修改）、`tests/test_local_ollama_reviewer.py`、`docs/models/`、`docs/verification/`、`docs/superpowers/`（未跟踪）。
- 建议 Codex 重点核查：`opencode_adapter.py` 的 `_normalize_ollama_endpoint`（H02 严格解析边界 + R05 空端口/`?`/`#`）、`_prepare_local_environment`（H03 二次复核与端点绑定 + R05 空 baseURL/异常分类）、`_verify_deny_permission`（R01 逐 key/value 失败关闭）、`parse_opencode_json`/`_scan_usage_events` 的 `saw_input`/`saw_output`/`_valid_token_value`/粘性 `usage_incomplete` 语义（H05/R04），以及 `_run_opencode_packet_review` 的 `cwd`/`--dir` 一致性与中断/信号终止证据保留（R02/R03）。

READY_FOR_CODEX_REVIEW
