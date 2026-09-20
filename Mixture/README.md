# CoreGeek v0.7 角色策略版

按新设计完成参赛服务、策略模块、任务状态和本地评测的重构。`main3.py` 保持原样，正式启动脚本由平台提供；仓库 `run.sh` 仅用于本地测试，不进入提交包。

```bash
# 本地测试启动，Python >=3.11，仅标准库
bash run.sh 8080
```

## 代码结构

- `src/agent/model.py`、`protocol.py`、`rules.py`：领域模型、协议编解码、规则。
- `server.py`、`runtime.py`、`state.py`：HTTP、可终止计算进程、主进程会话状态及重试缓存。
- `engine.py`、`scheduler.py`、`guard.py`：单回合编排、动作/位置/金币预留、最终校验。
- `world.py`、`navigation.py`：基地占地、站位、多目标最短路径与失败重选。
- `objectives.py`：三个角色的持久目标、行动历史、等待原因与移动失败反馈。
- `agents/`：角色 agent 包（设计见 docs/design/12）。`defense_agent` 输出夜间火力对比、损伤与经济态势和次日工作计划（修复/采购/用券/补石，含回合估算与最近邻修复路径）；`attack_agent` 是防御子 agent，只对攻击我方的机器人按基地距离、攻击力、射程与溅射计算攻击收益；`economy_agent` 记录矿山、价格、工人位置与性价比，决定采集或售卖时机并做机器人攻击圈避让；`task_agent` 保证开拓者任务优先、留在任务点一格内，任务结束后协助防御购买修复包与升级券；`self_evolve` 是任务子 agent 的“上下文→LLM→executeCmd→观察”循环与停止守卫；`review_agent` 审查响应格式；`blackboard` 是各 agent 的共享摘要（全局观察 agent 暂缓实现）。
- `policies/`：`economy.py` 防御工日程、`mining.py` 采矿与集中售卖、`pioneer.py` 任务站位，以及建设、防御、战斗算法。
- `tasks/`：任务实例、模型/命令反馈关联、有限证据记忆。结束原因不明的记录不会冒充成功SOP。
- `lab/`：独立近似裁判、任务fixture、完整换边评测和回放。
- `loop/`：本地打包→模拟→报告的可恢复流程；正式平台流程由外层执行 Agent 调用已安装的比赛平台 skill，见 docs/02-Loop工程计划.md；现有 CLI 尚无 skill 桥接。

`brain.decide(payload)` 是兼容单帧调用。连续比赛使用 `Session.decide` 或HTTP服务，任务记忆由主进程保存，计算子进程只返回状态提案。相同回合相同输入重放缓存；冲突输入不推进状态；跳号丢弃无法关联的待处理反馈；超时不提交子进程的中间状态。

协议没有match_id。不同队伍/阵营隔离，同一队伍同阵营的新比赛必须使用新进程或显式新Session；不能把不可区分的多场比赛混入同一个服务。当前状态在内存中，重启后重新同步，不承诺磁盘恢复游戏任务。

## 本地验收

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -B -m lab.sandbox_scenario --output artifacts/my-sandbox
python3 -m lab.evaluate --seeds 1,2,3 --rounds 1300 --output artifacts/v02-simulation
python3 -m lab.replay --input artifacts/v02-simulation/seed-1-normal.ndjson --output artifacts/v02-replay.ndjson
python3 tools/package.py --output artifacts/coregeek-v0.7.tar.gz
```

HTTP测试需要本机端口权限。源码服务经本地shell启动；解压包直接通过原 `main3.py` 启动，验证平台入口兼容性。测试包每次重新构建，避免误测旧包。

默认 `reasoning` fixture为双方提供相同的答案和延迟。`--task-profile tools` 专门验证命令反馈流程，标记为不可用于双方分数比较。所有fixture命令仅匹配字符串并返回模拟结果，绝不在开发主机执行。真实LLM、PvP、联合角色/机器人碰撞等仍有未实现或近似部分，完整限制在summary内。

原版对手保留在 `tools/baseline_agent/`，未改变文件字节。旧 `tools/simulate.py` 兼容转发至lab。历史初版报告和回放保留，不与当前数据混算。

## 可恢复本地流程

```bash
python3 -m loop.cli run --iteration artifacts/my-iteration --seeds 1 --rounds 130
python3 -m loop.cli resume --iteration artifacts/my-iteration
python3 -m loop.cli status --iteration artifacts/my-iteration
```

该流程有单写锁、原子状态、操作意图、包和汇总摘要核验。重复恢复已归档迭代不会重新模拟；源码或计划变化须新建目录。本地结果固定为 `INCONCLUSIVE_LOCAL_ONLY`，不自动晋升，不产生平台请求。它不是设计中完整的开发Agent/上传/编译/对战runner。

## 提交包与实现边界

`artifacts/coregeek-v0.7.tar.gz` 包含未修改的 `main3.py`、`pyproject.toml`、递归 `src/agent/**/*.py` 和MANIFEST。**不含run.sh**、实验室、Loop、对手、测试、日志或密钥。以平台提供的脚本调用入口；本地可解压后运行 `python3 main3.py PORT`。

当前默认按回合分段向 stdout 输出明文日志：第一行 `ROUND n`，第二行 `req {原始字段 JSON}`，第三行 `rsp {原始字段 JSON}`，随后逐行输出官方新闻、民间传闻、任务要求、LLM 结果及 prompt、executeCmd 结果及命令、`submitAnswer`。原始字段和值完整保留，换行转义为单行。已移除重复的 diagnostics 行；仅额外记录角色目标、等待原因及错误。设置 `CORE_GEEK_LOG_FORMAT=ndjson` 可使用结构化格式。异步队列满时丢弃日志并计数。

```bash
# 指定明文日志文件；入口不变
CORE_GEEK_DEBUG_LOG=artifacts/rounds.log bash run.sh 8080
# 关闭正文日志
CORE_GEEK_DEBUG_LOG=off bash run.sh 8080
# 汇总提交次数、解析原因与夜间操纵情况
python3 tools/analyze_debug.py artifacts/rounds.log
```

三个角色分别保存目标与阶段。防御工独自采石，按固定镜像阵型建设三炮和前侧优先墙，集中采购武器券、基地救命券及墙券，再回基地施工升级；根据路线和施工时间提前回防，第70回合就位，第71回合有射程内目标且武器就绪时开火。资源或道路不足时记录未完成工作。经济工从第一回合采矿，夜间优先基地背侧安全矿，商路不安全时继续采集。开拓者留在任务点持续工作，附近机器人出现时优先在任务范围内避让，必要时保命退出。

重新选矿先按工人路程和基地距离筛选附近矿，再按 `价格×批量/(批量+2×到矿步数+2×基地距离+0.5×商贩距离+2)` 排序；批量不超过10及背包余量。附近无矿才逐级扩大搜索，始终限定己方附近区域。矿点目标不会随单轮价格变化重选，直到耗尽、危险、持续不可达或更高优先级工作打断。

自进化任务提示词明确沙盒目录、文档路径和 API 初始未知，每轮选择执行命令或提交严格格式答案，并携带累积命令与返回结果。超长证据会明确标记截断；任务摘要作为同任务点后续题目的待验证步骤参考，不复制旧答案。新闻模型调用每天最多三次，自进化任务独立调度。

本轮需求、实现过程和验证见 [v0.7 实施归档](docs/design/08-v07角色目标与沙盒任务实施.md)。

```bash
python3 -m lab.evaluate --seeds 1,2,3 --rounds 1300 --output artifacts/v04-new-run
python3 -m lab.intelligence_scenario --output artifacts/v04-news-new-run
```

参见 [总体设计](docs/design/01-总体架构与实现设计.md)、[实施分工](docs/design/02-实施阶段与模块分工.md)、[本轮实现报告](docs/design/03-v02实现与验证.md)。

本次调测变更及验证见 [防御与任务日志修订](docs/design/04-防御与任务日志修订.md)。

当前工作区为 `2026 HW Comp/`，Git 根为 `CoreGeek/`，本工程为 `CoreGeek/Mixture/`，官方规则为工作区 `docs/`。本页命令均在本工程目录运行。后续每次优化都在 `docs/design/` 新增需求、方案、验证和局限记录，不覆盖历史结论。

v0.5 策略及验证以 [目录迁移与策略优化归档](docs/design/06-v05目录迁移与策略优化.md) 为准。

## 本轮可查看日志

```bash
PYTHONPATH=src python3 -B -m lab.evaluate --seeds 1,3 --rounds 1300 --output artifacts/my-v07-matches
PYTHONPATH=src python3 -B -m lab.sandbox_scenario --output artifacts/my-v07-sandbox
```

对局日志见 `artifacts/v07-final-matches/rounds.log`；沙盒多步任务日志见 `artifacts/v07-sandbox/rounds.log`。后者是确定性流程样本，模拟未知文档、损坏 API 和城市参数更换，不是真实 API 或真实大模型能力评测。
