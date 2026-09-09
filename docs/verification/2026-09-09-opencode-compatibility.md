# OpenCode 兼容性修复验证

## 范围与结果

精确锁定 1.18.29 导致本机 1.18.30 在推理前被拒绝。现允许稳定 1.18.x 且 patch >= 29，通过现有权限、transport、别名、步骤、能力及输出配置复核后继续。其他系列和预发布版本仍拒绝；未来同系列补丁属于兼容策略推定，不代表逐版真实验证。

源码与 Skill 由 Codex 在主目录修改。481 项确定性测试在审核工作区全部通过（33.178 秒）；compileall、diff 检查、1.18.30 离线输出路径审计通过。先前离线 wheel 构建通过；实际安装入口直接引用主目录源码。Skill 官方 quick_validate 校验通过。

## 本地真实审核

- run：`opencode-compat-120b-max-review`，AgentFlow 状态 `completed`。
- 计划 version 2，Owner 批准哈希：`0b3525bd589603c7962d31f480040d39c0705ab80eba0ac2c4afb6a2753181e9`。
- 审核者：Ollama `gpt-oss:120b`，MXFP4，digest `a951a23b46a1f6093dafee2ea481d634b4e31ac720a8a16f3f91e04f5a40ecd9`。
- 输出授权与有效上限：131072，能力依据 OpenAI 官方模型页：https://developers.openai.com/api/docs/models/gpt-oss-120b 。
- 原配置缺 context 导致首轮配置解析失败；本次只在进程环境补入本机已确认 context=131072、output=131072，通过真实无推理预检后启动。全局 provider 配置未改动。
- 返回：`{"approved":true,"findings":[]}`；`test_double=false`；input 13401、output 686；terminal reason `stop`；tool_use_count 0；远程费用 0。
- 会话：`ses_f7b0a8174ffejIqKvgoyJEm38l`。
- fake implementation 仅用于重放 Codex 补丁前的无写入占位，明确不是第三模型实施或独立模型自测。测试由确定性程序执行；独立模型负责审核。

## 审核材料一致性

冻结补丁 SHA-256：`0c8f2d2972d771ebeb01b37579b2c2da46b47eccdd0c9c19f988952f9ae4f2cd`。

主目录与审核工作区均为：

- `src/agentflow/opencode_adapter.py`：`63d750aa3ac9d6cc92f8441954b0cf10a47baf78a4136b7016848abd4a1ab5e0`。
- `skills/multi-model-agentflow/SKILL.md`：`46941a0171983eb5db410e74992581e939be43b7ff1f238b1d1268aa1a44765b`。

审核完成后仅补充本报告及计划进度，不修改已审核代码和 Skill。旧失败运行已取消并保留审计记录。本轮未推送远程仓库。
