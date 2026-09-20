# 运行时代码导航与修改约束

先读 [工程交接](../../AGENT.md)。这里路径相对当前目录；所有比赛记忆都在Session状态中。

## 调用链

`main3.py → server.py / runtime.py → Session.decide → engine.compute → guard.validate → Session提交 → telemetry`。入口具体加载方式以main3.py为准，不修改入口适配自己的模块设计。

- `model.py`：Turn、Unit、Pos、基地占地、两格任务点；`protocol.py`：decode及命令形状；`rules.py`：常量。
- `state.py`：锁、输入摘要、重复回合缓存、版本提交；`runtime.py`：可终止子进程；`config.py`：期限和上限。`brain.decide`只是单帧兼容，连续回合测试用Session。
- `engine.py`：顺序协调用药／基地应急、开拓者安全与任务反馈、防御回召、工人避险和经济、新闻、开拓者出行，最后审查与guard。不要在这里堆积矿价或武器评分算法。
- `agents/`：角色 agent 包（见 [agents/AGENT.md](agents/AGENT.md)）。`defense_agent`评估夜间火力/损伤/经济并给出白天工作计划与修复路径；`attack_agent`是攻击子 agent（只计算攻击我方的机器人，按距离/攻击力/射程/溅射算收益）；`economy_agent`做矿山勘察、性价比、售卖时机与机器人避让路径；`task_agent`管开拓者任务优先与任务结束后的防御协助；`self_evolve`是任务子 agent 的循环（上下文、解析、守卫、停止条件）；`review_agent`审查响应格式；`blackboard`是共享摘要（全局观察 agent 的数据面，暂缓实现）。
- `objectives.py`：持久角色目标、阶段、历史、移动失败短期避让；`policies/roles.py`：稳定分工。
- `policies/construction.py`：三炮、固定操作位、前侧优先墙、后侧通道；`combat.py`：目标与溅射几何（attack_agent的合法性与回退来源）；`defense.py`：回防、轮换开火、应急基地升级。
- `policies/economy.py`：防御工 gather→service→home 日程、采石、采购、施工和用券；`mining.py`：附近筛选、价值排序、锁定矿点、采集和集中售卖。
- `policies/pioneer.py`：任务点选择、绑定、允许站位内躲避（威胁只算攻击我方的机器人）；`tasks/`：异步命令与模型流程，见 [任务交接](tasks/AGENT.md)。
- `navigation.py`、`world.py`：八方向可达性、实体占地、预留格；`guard.py`：最后的动作合法性约束。
- `intelligence/news.py`：新闻通道、限额、矿价预测；`telemetry/writer.py`和`events.py`：日志（角色 agent 摘要追加在既有行之后）。游戏LLM调用通过rsp.prompt交给平台，不能给策略新增外部模型SDK调用。

## 必须保留的不变量

1. 同回合同输入返回相同缓存；冲突输入不能推进状态。版本过期、计算超时和异常不能提交半成品状态。跳号不得把旧executeCmd结果接到新任务。
2. 协议无match_id，不能编造隔离键；同队同侧新比赛用新进程或新Session。模型和命令反馈关联到具体待处理调用与任务实例。
3. roleCommandMap键为字符串，攻击用controllerId指定操作者；操作塔和工人本体动作占用冲突必须协调。只在最后校验后提交有效状态，guard丢掉输出时清掉相应pending。
4. Path搜索要排除正在推演的角色自身身份；不能只排除它的假想新坐标而让原坐标阻挡返程。位置预留对自己和其他角色意义不同。
5. 阵型由基地锚点推导，验证两侧；坐标增长方向以协议为准。后侧保留通道，不能只检查可造就封路。
6. 第70回合前返岗要算真实可达路径、施工、升级和争格余量；夜间常规升级不抢开火。危险与基地应急可以打断低优先级采矿。
7. 目标锁定先于重新评分。切换需记录原因；满包、矿耗尽、无安全路可等待，不能伪造动作维持“活跃率”。防御工作计划的任务切换写入 `defense_task_switch` 与 `goal_history`，不静默换目标。
8. 日志原始req/rsp保留完整值，正文额外字段不重复堆diagnostics。stdout默认日志是既有合同，不要自行改成完全静默。角色 agent 摘要（防御评估／防御计划／攻击评估／经济计划／任务计划／自进化循环／审查结果）只能追加在既有行之后。
9. 自进化循环不得抛异常：解析失败、重复命令、超预算都记录原因并改变下一步提示（必要时改为只提交答案），不能让 Session 回退成空响应。

## 常用回归映射

采矿／布局／角色日程：test_v07、test_intelligence、test_defense_revision、test_v05。路径：test_submission、test_simulator。会话与并发：test_state、test_architecture、test_http。任务：test_challenge、test_state、test_v07。角色 agent（评估/攻击收益/修复路径/售卖时机/循环守卫/审查/黑板）：test_agents。完整命令见 [测试交接](../../tests/AGENT.md)。
