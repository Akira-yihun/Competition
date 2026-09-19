# CoreGeek v2（DeepSeek 版）

《未来战争》v1.0 云核心网编程大赛参赛程序（DeepSeek 实现）。与同工作区的
`CoreGeek/ChatGPT/`（Codex 版）互为对照实现，本目录是**唯一可写区**；官方规则以工作区
`docs/任务书.md`、`docs/接口文档.md` 为准（只读）。

```bash
# 平台入口（平台提供启动脚本，本仓库的 run.sh 只用于本地）
python3 main3.py <port>

# 本地整局对抗（近似裁判，1300 回合 = 10 天）
python3 -m lab.run_match 1 2 3 --rounds 1300

# 全部测试（无第三方依赖）
python3 tests/run_tests.py
```

## 代码结构

| 路径 | 作用 |
|---|---|
| `main3.py`、`pyproject.toml` | 与官方包**逐字节一致**，不修改 |
| `src/agent/model.py` | 领域类型 + 全量安全解析（任何字段缺失都不抛异常） |
| `src/agent/rules.py` | 规则常量，每条注明出处（任务书章节 / 接口文档章节） |
| `src/agent/protocol.py` | 12 个动作构造器 + 唯一响应出口 `envelope()` |
| `src/agent/world.py` | 世界模型：基地占地/朝向、安全区、塔位、墙位、威胁排序、建址学习 |
| `src/agent/navigation.py` | A*（带扩展上限）+ BFS 距离场 + 夜间软禁区 + 软步降级 |
| `src/agent/combat.py` | 弹道、锥、冷却、**最近敌人优先**、火箭后排溅射与叠加 |
| `src/agent/strategy.py` | 昼夜单回合编排：防御 / 经济 / 建造 / 情报四段 |
| `src/agent/guard.py` | 最终合法性闸门（只做减法，每条拒绝带原因码） |
| `src/agent/state.py` | 会话去重、LLM 额度、矿点锁、角色分工、基地券持有人 |
| `src/agent/journal.py` | 逐回合固定格式日志（有界异步，不阻塞决策） |
| `src/agent/tasks/` | 自进化任务状态机、技能库、沙盒命令通道 |
| `lab/` | 近似裁判 + 整局跑批（硬指标门禁） |
| `tests/` | 契约 41 项 + 整局 8 项 + v2 回归 36 项 |

## 日志

`DS_AGENT_LOG` 控制逐回合日志：缺省 `stdout`（平台抓取），`off` 关闭，其余当作文件路径。

```
ROUND 85
req {原始请求 JSON，单行}
rsp {实际发出的响应 JSON，单行}
官方新闻 "..."
民间传闻 "..."
自进化类任务要求 "..."
req的llm调用结果 "..."
rsp的llm调用prompt "..."
req的executecmd结果 "..."
rsp的executecmd命令 "..."
diagnostics {...}
```

## 优化归档（每轮新增，不覆盖历史）

1. [01-CoreGeekD设计与实现方案](docs/design_DS/01-CoreGeekD设计与实现方案.md) — v1 架构与策略依据
2. [02-代码改造清单](docs/design_DS/02-代码改造清单.md) — v1 文件级改动与关键不变量
3. [03-v2优化方案与实现](docs/design_DS/03-v2优化方案与实现.md) — 本轮：日志、武器、采集、夜避、采购、基地券，含 9 个根因缺陷与验证数据

## 提交包

白名单：`main3.py`、`pyproject.toml`、`src/agent/**`。`lab/`、`tests/`、`docs/`、`run.sh`
一律不入包。
