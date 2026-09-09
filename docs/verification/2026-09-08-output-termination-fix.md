# A＋B：输出耗尽/无文本失败分类与审计修复

状态：实现与确定性检查完成，独立验收待完成。只执行 Owner 批准的 A＋B，没有执行真实模型预算调整或重新审核（C）。

## 根因与复现

真实 Qwen 会话曾以 `length` 结束，仅有 reasoning=7999、input=19802，没有最终文本。原解析器在构造 InvocationResult 之前因空文本抛普通 RuntimeError，服务层只转换失败状态，已解析的使用量丢失。相反，有文本时原解析器未阻断 length，合法批准 JSON 存在被误接受风险。

本轮新测试初次运行 6 个方法，8 failures / 2 errors，复现缺结果、错误成功和 Runner failed 而非安全暂停；其中第二个 error 是子场景异常后读取未设置的测试上下文，随后修正测试结构。首次全量仅剩测试夹具共用数据库造成的活动运行冲突，拆成独立测试实例后解决，没有放宽生产门禁。

## 修改

- `src/agentflow/opencode_adapter.py`：先解析完整事件与使用量再分类；最终 length 抛带结果的 InvocationIncompleteError，failure_kind=output_limit_reached。即便文本为合法批准 JSON 也阻断；有实际后续 stop 与文本时，以最终结束原因判定。无文本抛带结果的 InvocationProtocolError。LM Studio 非零退出保留失败使用量。分类器版本升为 3。
- `src/agentflow/adapters.py`：添加不属于“模型不可用”异常族的 InvocationProtocolError，避免协议失败触发 fallback。
- `src/agentflow/service.py`：复用已有 fail_call 持久化协议失败结果；本地费用确认为零，远程未知费用不补零。
- `src/agentflow/runner.py`：准确记录 review_output_limit_reached / implementation_output_limit_reached / model_output_invalid；不自动续接或 fallback。
- `src/agentflow/database.py`：只读查询已知 output_limit_reached/protocol_error 失败，resume 前阻断重发；不新增 schema、注册表或恢复通道。该规则同样覆盖原有带证据的协议失败。
- 同步 QA-13、AD-48、MVP-A37 和 task_plan。没有改动角色输出预算、thinking 配置、真实模型调用计划或历史数据库。

## 测试覆盖

新增 `tests/test_output_termination.py`，9 个测试方法，全部是合成事件与确定性替身；不冒充真实调用：

1. length＋无文本、部分文本、合法 approved JSON 都不能成功，且保留 input/reasoning/文本。
2. stop＋无文本作为带使用量的协议失败。
3. 失败时缺失 Token 与明确零值可区分。
4. 中间 length 后有最终 stop/有效文本不误判最终输出耗尽。
5. 实际 LM Studio 适配器消费进程替身，覆盖零/非零退出、无文本和部分文本的使用量保留。
6. review 输出耗尽：失败落库、准确暂停、无 review、resume 不新增调用。
7. review 无文本：同样保留审计并阻断重发。
8. implementation 输出耗尽：即使配置续接额度、重试和 fallback，仍不自动派发。
9. implementation 无文本：不重试、不 fallback、不由 resume 重发。

新增 Runner 测试使用真实数据库、授权、状态机与解析器，只替换模型响应；断言保存 input=19802、reasoning=7999、费用和原有效输出预算。既有多路径步骤耗尽、超时/UNKNOWN、权限、费用与恢复测试纳入全量回归。

## 验证与边界

- 定向：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 -m unittest test_output_termination -q`，9 tests / 0.692s / OK。
- 全量：相同环境运行 `python3.11 -m unittest discover -s tests -q`，474 tests / 36.438s / OK；分类器版本更新后最终复跑为 474 tests / 35.475s / OK。
- `python3.11 -m compileall -q src tests`、`git diff --check` 均 exit 0。
- 无真实模型调用、费用、子 Agent、提交或合并。既有业务运行/UNKNOWN/失败 Token 不回填、不重分类。
- 未将失败调用“修复成成功”，也没有完成不同模型自测/独立审核。源码变化后，旧审核 packet 不再代表当前候选；后续审核必须重新冻结输入和授权。
- simplify 检查仅整理本轮测试结构，保持原架构与边界；完成声明依据本轮实际测试输出。
