# 角色 agent 包接手说明

先读 [工程交接](../../AGENT.md)、[运行时代码导航](../AGENT.md) 与设计归档
[12-多agent分工与自进化任务实现](../../../docs/design/12-多agent分工与自进化任务实现.md)。

## 分工与数据流

```
req ──► engine.compute
          ├── defense_agent.assess ──► 黑板 defense（火力/损伤/经济）──► 日志 防御评估
          ├── defense.plan（夜）────► attack_agent.choose ───────────► 黑板 attack ──► 日志 攻击评估
          ├── economy.plan_defender ─► defense_agent.work_plan ──────► 黑板 defense_plan ──► 日志 防御计划
          │                              └─ repair_order / stone_reserve / log_reason（任务切换）
          ├── mining.plan_miner ─────► economy_agent.survey/sell_timing/route_to ──► 黑板 economy(_plan)
          ├── task_agent.plan ───────► pioneer 任务优先；无任务时 support（修复包/升级券/返岗）
          │      └── self_evolve ────► 任务循环上下文、解析、守卫、停止条件 ──► 黑板 self_evolve
          └── review_agent.audit ────► 格式审查 findings ─────────────────► 黑板 review ──► 日志 审查结果
```

## 不变量

1. **纯函数、无副作用**：这里不调用模型、不读写文件、不起线程；只读 `Turn`，只写传入的 `state`／`commands`。
2. **合法性仍在 guard**：agent 只负责排序、评分与解释；`guard.validate` 是唯一过滤器，审查 agent 只报告不删命令。
3. **不抛异常**：模型/命令响应不可解析时返回原因字符串并记录，绝不让异常冒泡到 Session（否则整回合回退为空响应）。
4. **黑板可序列化**：写入黑板的必须是 JSON 可序列化且长度受限的数据，日志与状态快照都依赖这一点。
5. **优先级不被侵蚀**：`policies` 里已验证的规则（塔位、墙位、用券、回防、开火数量与锥形/射程约束）不得为了新评分而放宽；攻击子 agent 始终保留几何结果作为回退。
6. **省上下文**：新模块不引入大对象；每次写入黑板都要有上限（见 `blackboard.CHAR_LIMIT`）。

## 尚未实现

全局观察 agent（设计见 12 号文档第 3 节）目前只有数据面：`blackboard.snapshot(state)`。实现时应新建
`observer.py`，只读黑板与 `Turn`，产出 `blackboard['global']` 的建议与冲突说明，不直接下发动作。

## 验证

```bash
CORE_GEEK_DEBUG_LOG=off PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_agents.py' -v
CORE_GEEK_DEBUG_LOG=off PYTHONPATH=src python3 -B -m unittest discover -s tests -v
PYTHONPATH=src python3 -B -m lab.sandbox_scenario --output artifacts/my-sandbox
```

单测覆盖评估数值（伤害/破墙回合）、溅射与超杀计分、最近邻修复路径、石头上限、售卖时机四分支、
机器人攻击圈避让、`targetTeam` 威胁过滤、重复命令与预算守卫、审查 findings 与黑板截断。
本地 fixture 不能证明真实平台得分，也不能替代真实 LLM 的探索质量。
