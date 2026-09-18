# CoreGeek v0.3 调测版

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
- `policies/`：建设、经济、防御、战斗算法。
- `tasks/`：任务实例、模型/命令反馈关联、有限证据记忆。结束原因不明的记录不会冒充成功SOP。
- `lab/`：独立近似裁判、任务fixture、完整换边评测和回放。
- `loop/`：本地打包→模拟→报告的可恢复流程；未配置的官方接口返回BLOCKED_CONFIG。

`brain.decide(payload)` 是兼容单帧调用。连续比赛使用 `Session.decide` 或HTTP服务，任务记忆由主进程保存，计算子进程只返回状态提案。相同回合相同输入重放缓存；冲突输入不推进状态；跳号丢弃无法关联的待处理反馈；超时不提交子进程的中间状态。

协议没有match_id。不同队伍/阵营隔离，同一队伍同阵营的新比赛必须使用新进程或显式新Session；不能把不可区分的多场比赛混入同一个服务。当前状态在内存中，重启后重新同步，不承诺磁盘恢复游戏任务。

## 本地验收

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m lab.evaluate --seeds 1,2,3 --rounds 1300 --output artifacts/v02-simulation
python3 -m lab.replay --input artifacts/v02-simulation/seed-1-normal.ndjson --output artifacts/v02-replay.ndjson
python3 tools/package.py --output artifacts/coregeek-v0.3.tar.gz
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

`artifacts/coregeek-v0.3.tar.gz` 包含未修改的 `main3.py`、`pyproject.toml`、递归 `src/agent/**/*.py` 和MANIFEST。**不含run.sh**、实验室、Loop、对手、测试、日志或密钥。以平台提供的脚本调用入口；本地可解压后运行 `python3 main3.py PORT`。

当前按调测要求默认向 stdout 输出明文 NDJSON：新闻、任务正文、模型返回、执行结果、实际提交答案、解析原因、动作校验丢弃和夜间操纵站位。异步队列满时丢弃日志并计数；大字段截断长度见 `truncatedCharacters`。公钥遥测、可验证SOP自动复用、统一角色/机器人移动裁判、宝藏与官方平台适配尚未完成。

```bash
# 指定明文日志文件；入口不变
CORE_GEEK_DEBUG_LOG=artifacts/debug.ndjson bash run.sh 8080
# 关闭正文日志
CORE_GEEK_DEBUG_LOG=off bash run.sh 8080
# 汇总提交次数、解析原因与夜间操纵情况
python3 tools/analyze_debug.py artifacts/debug.ndjson
```

防御布局根据基地横向位置镜像：敌方来袭侧先建墙，再建上下墙，背面中段留入口；三座武器放在内圈角落，先火箭炮。黄昏召回保持到次日，开拓者停止接新任务并回防。石头优先留作围墙，其他矿石按小批次及时出售；建设三塔后购买武器/围墙升级券和低血量药剂。

参见 [总体设计](docs/design/01-总体架构与实现设计.md)、[实施分工](docs/design/02-实施阶段与模块分工.md)、[本轮实现报告](docs/design/03-v02实现与验证.md)。

本次调测变更及验证见 [防御与任务日志修订](docs/design/04-防御与任务日志修订.md)。
