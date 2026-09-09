# 拆分独立审核结果：输出额度耗尽，无最终结论

- 批准哈希：`c5774837e8702223ff720d197c7ed7f8570f0ad49a6866c83a4aaef005701ab0`。
- Run：`qwen-budget-split-20260908-01`，最终 failed。
- 旧运行阻止新运行启动，因此通过 AgentFlow cancel 结束旧暂停运行；旧 UNKNOWN 与使用量保留，未重发。
- 新运行第一项 BUG-RB-01：31 文件哈希检查通过，465 tests / 32.986s / OK。
- 真实调用一次，模型 LM Studio `qwen/qwen3.8-27b@8bit`；call ID `1754b7b9-121f-464b-8d66-85648a17230f`。
- AgentFlow 调用耗时 571067 ms，state=failed，运行 checkpoint 为 execution_error；不是 900 秒超时。
- 第二项 BUG-RB-02 未启动；没有重试或续接，没有最终 review 行。

## 上游会话证据

通过 session list 的工作树路径确认会话 `ses_f7c6ebc8effeFP9vBRfBHOGkBc`。使用临时文件接收 export，完整 JSON SHA-256：`c97f801d810397fd8d5026387a5a3d1fbeb1f20729b5b7a7a78b16fd0731752c`。

唯一 assistant 消息 finish=`length`，无 error；parts 为 step-start/reasoning/step-finish，没有 text。

OpenCode tokens：input=19802，output=0，reasoning=7999，total=27801。计划 effective output=8000，观察结果说明思考输出消耗了几乎全部本次额度，未留下最终审核文本；不能把此次 length 结束解释成代码审核否决，也不应继续宣称“多加步骤”能解决该单次输出问题。

## 额外发现的审计缺口

AgentFlow model_calls 记录 input_tokens=0、output_tokens=0、raw_metadata_json=null、remote_cost=null；OpenCode 导出则有上述使用量。LM Studio 的这条无文本/length 失败路径未保留已有 Token 证据。这里的 0 不是实际零消耗；上游数据不能静默回填为历史调用成功。

本轮只执行已批准计划和只读诊断，没有修订生产代码或改写失败调用记录。此差异需要后续修复与回归测试，属于独立验收前应处理的问题。

## 结论

确定性测试通过；独立模型审核未完成，两个 BUG 都不能据此宣布独立验收通过。实施阶段仍为明确的 no-op 替身，不能冒充第三模型独立自测。没有远程 provider 调用；失败调用的数据库费用字段为空，不把它伪称为数据库已确认零费用。

后续建议先修复 LM Studio length/无文本失败的使用量审计，再评估模型的思考/最终输出预算策略或更适合代码审核的独立模型。任何增大输出预算、修改模型参数或换模型均须新计划批准；不要自动重跑当前计划。
