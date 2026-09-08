# Codex 正式复核：本地 Ollama Reviewer

> 本文保留首次复核的历史发现；R01–R05 后续已关闭。最终结论以 [Codex 最终批准](2026-09-08-ollama-reviewer-codex-approval.md) 为准。

复核日期：2026-09-08，约 05:06 PDT。结论：**CHANGES_REQUIRED，暂不批准关闭里程碑 20**。

本轮由 Codex 独立读取实际 diff、规范、原交接验收矩阵和测试后复核。没有调用真实模型、修改产品源码/既有测试、提交、推送或更改全局配置。仅新建本报告；诊断用例、构建副本与隔离安装均位于独立临时目录。

## 1. 结论摘要

不是推倒重做：角色路由、常规端点拒绝、正常审核、步骤耗尽和全量回归已有可用成果。但五组实质缺陷尚未关闭，尤其是最终权限检查和进程启动环境没有完全落实原验收条件。没有证据表明本轮已经发生真实泄露、越权模型调用或重复收费；发现的是可以复现的安全门禁及审计缺陷。

DeepSeek 报告中的“429 项通过”属实；“H01–H10 全部关闭”“O01–O28 全覆盖”不能成立。既有测试通过与完整验收通过必须分开。

## 2. 阻断项与有限修复要求

### R01 / P1：额外工具放行项能穿过最终权限复核

位置：`src/agentflow/opencode_adapter.py:932`，`_verify_deny_permission`。

实际行为：只检查 `*` 和固定的 14 个工具名。构造最终有效配置，在 `agent.agentflow-local-ollama-reviewer.permission` 的既有 deny 项之后增加 `"lsp":"allow"`，适配器仍启动推理替身，而不是失败关闭。`lsp` 是实际存在的权限；工具名与模式规则并不限于当前硬编码列表。

依据：PLAT-06、原交接 O06/O16。OpenCode 官方说明具体工具规则可以覆盖通配规则，最后匹配项生效，agent 权限优先于全局：[权限规则](https://opencode.ai/docs/permissions/#granular-rules-object-syntax)、[Agent 权限](https://opencode.ai/docs/permissions/#agents)。本次复现使用 agent 层额外放行，不依赖猜测全局/agent 合并次序。

最小修复：保留必要显式 deny，同时验证两层权限对象的**全部键和值**；额外工具、模式、嵌套规则不得含 allow/ask 或无法判定的值。全 deny 的额外项可接受，无需设计新的权限系统。

验收：分别在 global/agent 加入额外工具或通配模式的 allow/ask，均在推理 Popen 前抛 `ProviderNotConfiguredError`；完整 deny 配置继续成功。不要只测修改 `edit` 这一种情境。

### R02 / P1：推理进程没有使用已验证的临时工作目录

位置：`src/agentflow/opencode_adapter.py:518`，共享 `_run_opencode_packet_review` 的 Popen。

实际行为：只有 argv 的 `--dir`，Popen 没有 `cwd`，进程启动时继承父进程目录。独立捕获参数得到 `cwd=None`，而配置解析使用了临时 review_root。既有 isolation 测试只检查 `--dir` 目录的写权限，未断言 cwd；交付报告所称“Popen cwd=review_root”与代码不符。

依据：原交接工作单元 A 第 8 步和 O15。这里确认的是启动环境不一致及未满足隔离合同，不把它夸大成已经观察到仓库内容外泄；也不要求增加 OS 沙箱。

最小修复：本地 Reviewer 推理 Popen 的 cwd 与两次有效配置检查及 `--dir` 一致，均指同一个空只读临时目录。若共享 helper 会影响远程路径，保留其兼容性并运行远程回归。

验收：启动替身时同时断言 cwd/--dir/配置检查目录一致、不是仓库、不可写；从项目目录启动也必须通过。捕获 Runner 实际 prompt，确认 JSON-only 协议和最小 packet，不以手写的简化 request 替代全部隔离检查。

### R03 / P1：信号退出和中断会丢弃已取得的 Token、会话及耗时证据

位置：`src/agentflow/opencode_adapter.py:548` 和 `:556`。

实际行为：KeyboardInterrupt/OSError 分支调用 `_bounded_drain` 后丢弃返回值；负退出码分支丢弃已有 stdout。两条路径都抛 `InvocationOutcomeUnknown(result=None)`。即使 stdout 含 session ID 和 7 input / 3 output Token，异常仍无结果证据。`InvocationService` 因而以 0 Token、0 ms 和未知使用量填充，不能恢复原证据。

依据：原交接 O21、MVP-A33、STATE-05。状态仍为 UNKNOWN、没有被批准，这部分正确；问题是审计丢失，而不是自动重试已经发生。

最小修复：将信号退出已有 stdout 或中断后有界 drain 的字节解析为证据结果，保留 session、合法 Token、duration、字节数/哈希和 cleanup 状态；output 必须为空，termination_reason 区分 signal/interrupted/communication_error，不借用错误的 timeout 原因。UNKNOWN 不得转换成普通成功或已知失败，不增加自动续接。

验收：signal、KeyboardInterrupt、通信 OSError，各测有/无 usage；接真实 Runner 与临时数据库，核对 UNKNOWN、空 output、证据持久化，resume 在 resolve 前拒绝且调用次数不增加。现有“只断言抛 UNKNOWN”测试不足。

### R04 / P1：Token 字段存在被误当成有效证据

位置：`src/agentflow/opencode_adapter.py:1859`，并关联 `_scan_usage_events` / `_token_total`。

实际复现：

| 输入 tokens | 当前 input/output | 当前 usage_unavailable | 应有语义 |
| --- | --- | --- | --- |
| `{"input":"bad","output":null}` | 0 / 0 | false | 未知，不能确认零 |
| `{"reasoning":9}` | 0 / 0 | false | reasoning 可记录，但 input/output 未知 |
| `{"input":true,"output":-2}` | 1 / -2 | false | 非法计数，不可计为有效消耗 |

成功解析的新逻辑仅检测键名；辅助计数函数的宽松类型行为是已有实现，不能把全部问题说成这轮新增，但本次明确要求的 O17 已将这些边界纳入修复范围。失败/部分输出解析还把明确 input=0/output=0 误标为没有 usage，与成功路径不一致。

最小修复：共享合法 Token 判定，仅认可非布尔、非负整数及项目已经支持的合法分项结构；不改数据库数值类型。保留已确认部分，缺失/非法的 input/output 不能被总标记冒充完整可用；reasoning 独立记录。成功、失败、超时使用一致语义，远程 cost 校验不退化。

验收：合法非零、明确零、缺一个字段、全缺、reasoning-only、null/string/bool/负数、混合有效/无效事件与多步骤；核对数值和 availability 元数据，不靠凑测试数量。

### R05 / P1：配置失败关闭仍有输入与异常分类遗漏

位置：`src/agentflow/opencode_adapter.py:866`、`:443`、`:817`。

确认行为：

- 实际 resolved `provider.ollama.options.baseURL=""` 被归一为默认本地地址，而不是拒绝。纯函数允许缺省，不代表真实配置可以缺失；这是原交接特意区分的两个层次。
- `http://localhost:/v1` 显式空端口返回 True；`urlsplit().port=None` 被当成完全未写端口，违反严格解析合同。
- 配置命令不可执行产生 PermissionError 时，未转换为约定的 `UnsupportedProviderError`；原始异常外溢，无法进入预期的受控不可用分类。

最小修复：实际 provider/model endpoint 字段单独要求非空有效字符串；保留纯函数 None/空白的既定缺省行为。区分未写端口与写了空端口，并覆盖空 query/fragment 标记、括号等 URL 边界。配置启动的其它 OSError 统一转为脱敏受控不可用异常，不捕获后虚构模型发现成功。

验收：有效配置正常调用；空/空白有效地址、非法明确端口及不可执行命令，全部在推理前稳定拒绝且不泄露原 URL/配置/凭据。不要修改全局配置来让测试通过。

## 3. 已核实通过的内容与时间边界

- 本轮重新跑全量：429 tests，29.089 秒，OK，退出 0。
- 独立补充正向诊断 3 项通过：五个 ModelRef 字段逐一修改使哈希变化并令旧授权失效，序列化往返哈希稳定；timeout 重叠字节/截断 UTF-8 保留 7/3 Token、正确 SHA 和空 output；清理超时使用 5 秒 + 2 秒有界等待，并记录 cleanup_incomplete。
- 默认推理 communicate timeout=900 秒；单次配置/模型列表发现默认 15 秒；Reviewer steps=2，不使用 implementation 的续接预算。这是分阶段时限，不是整个调用严格 900 秒墙钟上限。
- 当前一次 invoke 实际有 3 次 debug config（require_model 一次 + prepare 两次），另有一次 models；在这些阶段都正常返回、清理走满宽限的条件下，配置等待预算合计约 60 秒，再加推理 900 秒和清理 7 秒。不是已测得的性能，也不是涵盖进程创建/CLI 装配的总时限承诺。交付报告“每次 invocation 至多两次解析”不准确；消除冗余属于非阻断建议，不要求为此扩展架构。
- 现有用例中的写角色/非只读拒绝、常规远程端点拒绝、授权 provider 边界、默认路由保留、步骤耗尽暂停和正常本地零远程费用通过。未经测试的真实服务可用性不在本结论内。
- compileall、tracked diff 空白检查通过。独立从当前未提交 src 构建 wheel，离线隔离安装、导入 LocalOllamaReviewerAdapter、CLI --help 均退出 0。
- 本轮 wheel SHA-256：`04690d31183dc2670c89ded1151a3968a2ce1a65478e68cc7af4ba154ad94d4d`。路径 `/tmp/ollama-formal-review.r1voOg/wheels/multi_model_agentflow-0.1.0-py3-none-any.whl`。构建产物哈希受打包时间影响，不以与实施者那次 wheel 不同判缺陷。
- findings.md / progress.md 哈希仍与原交接一致。历史 smoke 来源限制已被资料文档标明；本轮未做真实推理、下载或全局安装。

## 4. 独立诊断证据与测试覆盖结论

临时独立用例：

- `/tmp/ollama-formal-review.r1voOg/test_independent_review.py`：8 个测试方法，结果 9 个 assertion failures（含 3 个 subTest）+ 1 error；对应 R01–R05。测试均为无费用替身。
- `/tmp/ollama-formal-review.r1voOg/test_positive_review.py`：3 个测试方法全部通过，对应上述正向验证。

复跑方式（从项目根）：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 /tmp/ollama-formal-review.r1voOg/test_independent_review.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 /tmp/ollama-formal-review.r1voOg/test_positive_review.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3.11 -m unittest discover -s tests -q
```

临时目录不是永久测试源；实施者应把这里的具体输入、断言与必要 Runner 集成移入原白名单测试文件，不能只修改报告或测试名称。O06/O15/O17/O21 已实证不满足；O01/O03/O05 仍有遗漏。报告对 O20/O21 的 signal/恢复断言以及对 O26 的泛称引用不能代替具体映射；O26 已由本轮独立验证，未发现产品哈希缺陷，不因缺少同名测试再列一个功能阻断项。

审核快照：分支 `feat/ollama-local-reviewer`，HEAD `0545b64c0338f363c0aca862c251f350f48aa228`。

```text
src/agentflow/opencode_adapter.py SHA-256 57ed41853bb60d97cbc4483d51f0c5c6f5c025878c43d627bf22415f5154c25a
tests/test_local_ollama_reviewer.py SHA-256 4e503bf64e8f79c249cc358539feb1ce978d8ce0d215f29a8af27d86ac934df0
```

## 5. 修复终点

只要求关闭 R01–R05，补入能揭示这些缺陷的正式测试，更新自测报告中的事实性覆盖声明；然后跑相关定向、全量和离线构建，交付 READY_FOR_CODEX_REVIEW。仍然不提交、不推送、不全局安装、不启动其它模型或真实 smoke。该结论不是已向 OpenCode 派发返工的消息，本轮没有启动新的执行。

Codex 复核时以安全边界、证据正确性和必要测试为准。文档中的 README 数量引用、Next Step 旧措辞、方法命名等作为非阻断整理，不单独启动返工轮次；不要求重做已有正确模块，也不把真实模型 smoke 升级成完成条件。
