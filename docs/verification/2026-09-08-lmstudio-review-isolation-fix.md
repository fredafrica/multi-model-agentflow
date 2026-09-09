# LM Studio 审核隔离修复：确定性检查通过，真实复验待授权

## 根因与修复

上轮真实会话 `ses_f7bea1eaaffe3mOsJA3PnlegUX` 在内联当前补丁之外读取了旧 worktree。LM Studio 的 read_only 分支原来只禁写，不禁 read/glob/grep；提示词不足以实现 packet-only 约束。

本轮按 Owner 确认方案：

- LM Studio review/rereview 在配置发现前拒绝 read_only=false；只读配置复用既有全工具 deny 规则，同时验证实际全局权限及所选 Agent 权限。实施/修订保留现有文件权限。
- 提示词明确只审核提供材料，缺证据应报告；权限而不是提示词承担读取约束。
- P3 诊断改进：版本错误包含已验证与实际有效版本，异常版本输出不原样打印；输出/思考覆盖错误指出有界清理后的键名，不打印配置值。未放开未验证版本、thinking 选项或 zero_findings。
- 规范新增 QA-14 / MVP-A38 / AD-49。无新合同字段、无数据库迁移、无历史授权或调用修正。

## 测试证据

`tests/test_lmstudio_review_isolation.py` 新增 6 个测试方法，覆盖 review/rereview 两角色、全局/Agent 8 组权限覆盖、implementation/revision 兼容性、非只读审核请求前置拒绝及版本/配置项诊断。

- 修复前：6 个方法出现 8 个预期子用例断言失败，确认读取仍为 allow、非只读请求未拒绝、诊断缺版本/键；两个既有安全/兼容性测试通过。
- 修复后定向：6 tests，0.008 秒，OK。
- 最终全量：480 tests，34.898 秒，OK。
- 测试开发中修正过测试自身 `output_text` 属性误用（实际为 `output`）；第一次全量因此失败，不计为通过，以上为修正后的重新完整执行结果。
- `git diff --check` 通过。测试是确定性进程替身，不是真实模型审核，也不冒充 OS 沙箱验证或第三模型独立自测。

## 本机真实配置检查（无推理）

使用本机 `/Users/fredafrica/.opencode/bin/opencode` 的版本与 `debug config --pure`，通过实际 `_prepare_output_environment` 路径两次解析/复核。只输出最小权限/预算证据，不记录完整配置。

- 全局与 `agentflow-sandbox` 两层：`*`, read, glob, grep, edit, write, bash, shell, external_directory, webfetch, websearch, task, subagent, skill, question 均为 deny。
- steps=8，所选 Qwen output=16000，配置检查成功。
- 未启动推理进程，没有新增模型调用或费用；用户全局配置未修改。

## 限制与下一步

本轮已按 simplify 做范围内自查，复用权限 helper，没有另造权限机制。此自查不是独立审核。

真实 Qwen 复验仍须新冻结计划与哈希批准。必须检查真实会话不再成功调用工具、返回严格 JSON 且 zero_findings 通过；不能凭自动化测试宣布总体独立验收完成。两条 P3 仅完成诊断改善，独立关闭状态仍待 Reviewer 复验。第三模型独立自测仍缺失。历史失败与 UNKNOWN 保留，未提交、推送、合并或全局安装。
