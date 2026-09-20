# 自进化任务接手说明

先读 [工程交接](../../../AGENT.md)、[角色 agent 包](../agents/AGENT.md) 与 [11号设计](../../../docs/design/11-自进化任务agent实现.md)。

## 数据流

`workflow.advance`读取当前任务和待处理调用→关联req的LLM／executeCmd返回→记录证据→构造下一轮prompt或executeCmd→最终答案写入开拓者submitAnswer。`agents/self_evolve.py`是任务 agent 的子 agent：维护原始任务／当前实际任务／已确认事实／最近执行记录，容错解析模型回复（`parse_action`不抛异常），并用守卫（重复命令、连续无进展、步数预算24、剩余回合≤2）决定何时停止探索、改为只提交答案。`prompts.py`提供探索提示与收尾提示两套合同；`evidence.py`控制上下文尺寸并标记省略；`memory.py`归档方法摘要；`channel.py`保留平台身份校验；`legacy.py`保留兼容流程，不等于当前默认策略。

模型请求只有平台的一条反馈通道，新闻与任务不能同时竞争或互相消费返回。请求内包含taskKey、requestId、roundNo；结构化响应要求回传这些身份。兼容解析支持纯最终答案及旧taskUnderstanding，不能为了强制新格式无声破坏已有回放。

模型下一步必须输出executeCmd或taskAnswer，两者互斥；taskAnswer字符串严格符合题目格式。最终响应可同时携带taskSummary，摘要不混入提交答案。模型说环境事实不等于已经证实，必须看实际命令返回。可选字段`purpose`记录该命令想确认什么，进入上下文与日志便于复盘。

## 状态与故障处理

- 同文本不等于同任务实例，换任务点、刷新题目、epoch变化都需正确绑定；旧返回不得跨实例使用。
- sandbox_history必须保留每条实际发出命令及对应返回回合、结果、截断标记。不能只发最后一次错误而丢失前文；超长内容必须有明确省略提示。
- 安全换位／用药占用了开拓者动作时，保留ready_answer，不能吞掉刚返回的答案，也不能覆盖救命动作；真正离开任务范围时才结束该任务流程。
- 多格任务点的允许站位要同时兼容接取、持续任务、安全移动和guard。
- 守卫命中时（预算用尽、命令重复、无进展、剩余回合不足）丢弃该条命令、记录stopReason，并在提示中要求直接提交答案；不重复调用沙盒。
- 任务结束、消失或提交不能当官方判对。memory当前verified始终False，submitted_unconfirmed只是已提交未确认；候选SOP优先同点，但仍需复核环境，参数化复用步骤而非旧城市答案。
- 跳号、超时、空返回、格式错误、错误ID、命令失败都需要有限重试或可解释退路，不能无限重复命令；任何情况下不允许异常冒泡成整回合空响应。

## 验证

先跑test_challenge、test_state、test_v07、test_agents。再运行lab.sandbox_scenario，观察文档发现→失败API→修正→严格答案→同点下一城市的完整日志。此fixture只匹配字符串，不运行网络或shell，不能拿它证明真实LLM会正确探索。新模型适配也不能只改fixture让它返回预设答案来宣称提升。

需要真实样本时使用用户给出的平台req/rsp或沙盒输出，避免在开发主机执行日志里或模型生成的executeCmd。接口和题目内容以真实证据为准，不能根据示例虚构文件系统或API。
