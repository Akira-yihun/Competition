# v0.8 多 agent 分工、自进化任务循环与审查实现

日期：2026-09-20。基线：Mixture 工程（源自 ChatGPT `574235a` v0.7 的拷贝）。状态：设计 + 实现 + 本地验证。

本文是实现合同：先写需求、证据与方案，再改代码，最后补实际验证结果（见第 7 节）。所有写入限于 `CoreGeek/Mixture/`；官方规则只读，引用 `docs/任务书.md`。

## 1. 用户需求（2026-09-20）

1. 全局观察 agent：记录、总结全局状态，协助其他 agent 决策；**本轮暂缓实现**，只在本文第 3 节完成设计。
2. 防御 agent：总结夜间对抗的机器人火力与基地火力对比、围墙／基地／武器损伤状况、当前经济状况，据此给出下一个白天的防御工作规划（修补哪些围墙、买什么道具、回到什么位置使用什么单位的道具、是否需要补石头储备、按可见单位位置估算所需回合数）；记录日志；按规划给出当前任务，中途切换任务要有明确规则和日志；按当前任务规划路径（例如修墙时列出所有待修位置并计算修复路径，避免绕路）；石头储备不必过多，有闲暇可采矿售卖，但首要目标是修缮工事、夜间攻击敌人、及时升级基地；并实现一个**攻击子 agent**，按“仅攻击我方基地的机器人距基地距离、攻击力、攻击距离、对周边敌人的溅射伤害”等计算攻击策略与收益，决定夜间攻击目标。
3. 经济 agent：记录地图上所有矿山位置、矿石价格、采集工人位置，计算采集性价比，指导采集与出售；把握售卖时机（用世界新闻与必要的 LLM 调用预判价格，同时不能因为囤货导致防御买不起升级道具）；结合矿山位置、价格、工人位置、当前时间（白天／夜间、距夜晚还有多久）制定任务计划（采集某处矿石／去小贩处售卖）；记录日志；按当前任务行进与操作，临时切换任务要有规则与日志；采集工人可全图移动，但要按机器人攻击距离与位置避让，不要死亡。
4. 任务 agent：依据任务书 5.3 自进化类章节制定开拓者任务计划；完成任务时避免被机器人攻击（机器人行进方向朝向基地，`req` 中 `targetTeam` 表明它攻击我方还是敌方基地），同时尽量不离开任务点一格以免任务中断；所有自进化任务结束后，除非 LLM 结果表示可以召唤宝藏，否则协助防御 agent 购买围墙修复包并在夜间（围墙将要被攻陷时）使用，并购买各种升级券后使用。
5. 自进化任务目前仍未成功：需要按 [11-自进化任务agent实现](11-自进化任务agent实现.md) 形成“记录上下文 → 调用 LLM → executeCmd → 观察结果 → 再决策 → 提交答案”的循环；无需过度复杂，优化 prompt，必要时停止调用并让开拓者提交答案；这是任务 agent 的子 agent；过程中不得抛异常，LLM 响应无法处理时忽略并记录日志。
6. 增加审查 agent：检查响应格式，避免出错。
7. 为代码补充注释，便于修改和检视。

## 2. 证据与现状

- 现有实现已按角色分工：`policies/roles.py` 固定 defender／miner，开拓者独立；`policies/economy.py` 是防御工的 gather→service→home 日程，`policies/mining.py` 是采矿与售卖，`policies/pioneer.py` 是任务点绑定与躲避，`tasks/workflow.py` 是“模型调用／命令执行”状态机。
- 缺口（对照第 1 节）：
  - 没有把夜间火力对比、损伤、经济汇总成结构化态势；`defense.plan` 只在回防时描述“距离／路径”，没有白天工作计划与回合估算。
  - 攻击目标选择集中在 `policies/combat.py`，按几何与最近威胁排序，没有独立的收益评估，也不区分机器人是否攻击我方基地（虽然 `_attack_targets` 已过滤 `targetTeam`）。
  - 采矿价值排序内嵌在 `mining.candidates`，没有价格历史、没有“为了防御采购必须在第几回合前卖出”的售卖时机判断。
  - `policies/pioneer.py:risk` 把**所有**机器人当作威胁，既包括攻击我方的，也包括攻击敌方的——后者靠近任务点时会触发不必要的躲避甚至退出任务。
  - 任务流程没有步数预算、重复命令检测、无进展停止条件；`tasks/prompts.py` 是一段长指令，没有 11 号文档要求的“原始任务／当前实际任务／已确认事实／最近执行记录”结构。
  - 没有独立的格式审查环节：`guard.legal` 只做**合法性**过滤，不做“响应格式是否正确”的报告。
  - LLM 响应解析失败时只累加 `parse_rejections`，没有面向操作者的结构化审查记录。
  - 任务全部结束、且无宝藏时，开拓者只等待（`pioneer.plan` 的 `task_refresh_wait`），不协助防御采购与用券。

## 3. 全局观察 agent（本轮只设计，不实现）

定位：只观察、只总结、只建议，**不直接下发动作**，避免与角色 agent 争抢同回合的单动作通道（每回合每个单位只有一个动作，见任务书 4.4）。

输入（全部来自已有数据，不新增平台调用）：

| 来源 | 内容 |
| --- | --- |
| `req` | roundNo、昼夜、goldNum、我方单位、敌方可见单位、机器人（全图可见）、世界新闻、`lastRoundRoleActionResults`、`errors` |
| Session 状态 | `role_plans`／`goal_history`、`economy_jobs`、`mine_targets`、`defense_assignments`、`task`／`memory`、`intelligence` |
| 黑板（本轮新增） | 防御／攻击／经济／任务／审查 agent 每回合写入的结构化摘要 |

输出（拟）：`state['blackboard']['global']`，含 `phase`（白天建设期／白天服务期／夜前回防／夜间防守）、`threat_level`、`economy_health`、`task_progress`、`conflicts`（例如“防御需要现金但经济在囤矿”“开拓者被机器人与任务点夹击”）、`advice`（每条带 target_role、priority、reason、expires_round）。

与其他 agent 的协作方式（拟三种，按风险从低到高）：
1. **只读摘要**：其他 agent 通过 `blackboard.read()` 读取全局摘要作为排序的次要因子，不改变既定优先级。
2. **冲突仲裁**：全局 agent 检测到同一资源（金币、同一工人动作）被两个角色抢占时，写 `conflicts`，由现有 `scheduler.arbitrate` 保持唯一动作所有权，全局 agent 只解释原因。
3. **建议队列**：向角色 agent 下发带过期回合的建议；角色 agent 可拒绝并在日志说明。

暂缓理由：当前回合预算（默认 3s 决策、4.5s 请求）与得分瓶颈在任务时延、路线争格与防御日程，多一层全局推理没有可验证收益；先让各角色 agent 的黑板数据稳定积累一个版本，再据此实现 observer，避免凭空设计。

实现落点（后续）：`src/agent/agents/observer.py`，读 `blackboard.snapshot(state)`，纯函数、无副作用、不产生平台调用；先离线（回放日志）验证建议质量，再接入在线决策。

## 4. 角色 agent 设计

新增包 `src/agent/agents/`，每个模块一个 agent，纯标准库、无外部调用、无副作用（只写传入的 state 与 commands）：

| 模块 | 职责 | 关键函数 |
| --- | --- | --- |
| `blackboard.py` | 共享黑板：各 agent 写入结构化摘要，供其他 agent 与后续 observer 读取 | `write/read/snapshot` |
| `defense_agent.py` | 防御态势评估、白天工作计划、修缮路径、石头储备上限 | `assess/work_plan/stone_reserve/repair_order/next_site` |
| `attack_agent.py` | 夜间攻击目标与收益评估（子 agent） | `threat/evaluate/choose` |
| `economy_agent.py` | 矿山勘察、性价比、售卖时机、工人任务计划、机器人避让路径 | `survey/sell_timing/plan/route_to` |
| `task_agent.py` | 开拓者任务计划与任务结束后的防御协助 | `plan/support/use_wall_fixer` |
| `self_evolve.py` | 自进化子 agent：上下文、动作解析、循环守卫、停止条件（11 号文档） | `AgentState/build_context/parse_action/guard/stop_reason/budget_note` |
| `review_agent.py` | 审查 agent：命令与响应格式审查 | `review_command/review_response/review_action` |

### 4.1 防御 agent

`assess(turn, state)` 产出并写入 `state['defense_assessment']` 与黑板：

- `firepower`：我方武器（类型、等级、射程、冷却、按等级估算每回合伤害）与机器人（按类型数量、总血量、每回合对基地潜在伤害、进入基地 3 格范围的单位）；`ratio` 给出“可击杀机器人总血量／每回合我方伤害”的估算回合数。
- `damage`：基地血量与等级、每座炮台血量／等级、`walls` 列表（位置、血量、上限、比例、是否优先墙、是否在敌人来向）。
- `economy`：金币、背包矿石与估值、在途升级券、维修包数量、当前矿价（含新闻预测）。

`work_plan(turn, state, worker)` 由态势推出**有序**工作项，每项含 `kind/target/name/unit/eta/priority/reason`：

| 优先级 | 工作项 | 触发条件 |
| --- | --- | --- |
| 100 | `use` 基地升级券（target=基地） | 基地血低于阈值或两回合内会被击毁（沿用 `defense.emergency_upgrade`） |
| 90 | `use` 围墙修复包（target=某围墙） | 该墙比例 ≤ 0.5 且有机器人在其 3 格内 |
| 80 | `repair_wall`（走到墙边用修复包） | 有修复包且存在受损优先墙 |
| 70 | `defend_ready`（回操作位） | 距夜晚剩余回合 ≤ 回程 + 工作量 + 余量 |
| 60 | `use` 武器／围墙升级券（target=具体建筑） | 券在背包且建筑未满级 |
| 50 | `build_wall`（补缺失墙位，需石头） | 优先墙位缺失 |
| 40 | `buy`（修复包／券／药剂） | 金币足够且背包有空位 |
| 30 | `restock_stone` | 石头 < 所需（见 `stone_reserve`） |
| 20 | `sell` / `mine`（闲暇） | 无以上工作且距夜晚尚有余量 |

`eta` 用真实可达路径 + 每次施工／使用动作约 1 回合估算，便于日志解释“大约需要多少回合”。

`stone_reserve(turn, state)`：`min(缺失优先墙位数, 5)`，避免把背包塞满石头而延误服务行程；多余石头进入售卖。

`repair_order(turn, worker, sites, reserved)`：对待修／待建位置做最近邻排序（每一步用 `route` 真实长度，含当前预留格），把顺序写进计划并在目标切换时记录原因。执行时取排序后的**第一个可达**位置，避免“按血量挑一个远墙”的来回绕路。

任务切换规则（写入 `objectives.goal` 的 reason 与 `goal_history`）：
1. 高优先级工作项可以打断低优先级（上表数字大者优先）；
2. 同级不打断已在进行且仍可达的目标（沿用“目标锁定先于重新评分”）；
3. 危险（机器人进入基地 3 格）、基地致命、夜前回防截止可以打断任何采集／售卖；
4. 每次切换都在日志给出 `from→to` 与原因，不静默换目标。

### 4.2 攻击子 agent

`threat(turn, robot, state)`：只把 `targetTeam` 为空或等于我方阵营的机器人视为威胁（其余机器人正在攻击敌方基地，不属于本 agent 的评估对象）。

`evaluate(turn, tower, aim, robots, state)`：对一个瞄准点给出可解释收益：

```
value = 命中伤害（按等级：中心 20／溅射 10，扣除超杀溢出）
      + 威胁削减（对每个被命中机器人：攻击力 × 距基地紧迫度）
      + 溅射加成（命中 ≥2 个不同机器人，或命中位于我方基地 3 格内的机器人）
      - 距离惩罚（瞄准点离基地越远越小，避免为打后排放空近处威胁）
```

`choose(turn, tower, state)`：以 `combat._attack_targets` 的几何结果（等级对应数量、射程、加特林锥形约束、火箭最近锚点）为合法候选，用 `evaluate` 选最优并把评估写进黑板与 `goal` 原因；若评估结果为空则退回几何结果。这样保持既有规则不变量（每级恰好等于目标数、不越射程、锥形非钝角），同时把“为什么打这里”变成可核查的数据。

### 4.3 经济 agent

`survey(turn, state, worker)`：对每个矿山记录 `pos/ore/price/距工人步数/距基地距离/距小贩距离/单次批量/性价比/是否夜安全/是否本地`，写入 `state['economy_survey']` 与黑板；同时维护 `state['price_history']`（按回合记录小贩价格，供趋势判断）。

`sell_timing(turn, state, worker)` 决定“现在卖／继续囤”，并给出理由：

1. **防御用钱优先**：若防御计划显示下一件必需品（修复包／武器券／基地救命券）在 `N=6` 回合内需要金币，而当前金币不足，则立即卖出价值最高的矿石（理由 `defense_funding`）。
2. **价格预测**：`news.should_hold` 为真且金币充足时持有到 `holdUntil`（理由 `hold_until_<round>`）。
3. **夜间窗口**：入夜前 `N=8` 回合内、且商路仍安全时清空高价矿石，避免夜间跨区赶路（理由 `before_night`）。
4. **满包**：背包满时必须卖（理由 `backpack_full`）。
5. 其余情况继续采集（理由 `keep_mining`）。

`plan(turn, worker, state)` 输出 `{'kind':'collect'|'sell'|'wait','target','eta','switch_rule'}` 写入 `state['economy_plan']` 与黑板；切换规则同防御（锁定优先、危险可打断、日志记录）。

`route_to(turn, worker, target, reserved)`：先用“机器人 3 格攻击圈”作为附加避让格做一次路径，若不可达再退回普通路径——既避免走进攻击范围，也不会因为避让而完全不动。

### 4.4 任务 agent

`plan(turn, worker, state, reserved, commands)`：

1. 有任务（`phaseTask` 或有效任务点）→ 交给 `policies/pioneer.py` 与自进化子 agent，本模块只补充“留在任务点一格内”的站位约束与日志；
2. 无任务但新闻给出宝藏 → 由 `policies/treasure.py` 执行（引擎已保证优先级）；
3. 无任务无宝藏（自进化任务全部结束）→ **协助防御**：按防御计划购买围墙修复包与升级券；夜间若某围墙“将要被攻陷”且开拓者在其一格内，直接用修复包；否则把券带到对应建筑旁使用，或回到任务点等待下一轮任务刷新。

避险：`policies/pioneer.py:risk` 改为只统计攻击我方的机器人（`targetTeam` 匹配或为空），机器人攻击距离 3 由此进入 4 格风险半径，与任务书 4.7.2 一致。

### 4.5 自进化子 agent

按 11 号文档实现最小闭环，落在 `agents/self_evolve.py`，由 `tasks/workflow.py` 驱动（保持平台单通道异步语义）：

- `AgentState`：`original_task / effective_task / facts / steps / attempts / rejections / no_progress`。
- `build_context`：输出 11 号文档的【原始任务】【当前实际任务】【已确认事实】【最近执行记录】结构，同时保留平台需要的 `taskKey/requestId/roundNo/stage/understanding/sandboxHistory/candidateSOPs/remainingRounds` 等字段（既有测试依赖这些键）。
- `parse_action`：包装 `tasks/channel.parse_with_reason`，**不抛异常**；无法解析时返回原因字符串（`malformed_json`、`ambiguous_or_empty_action`、`missing_command` 等）并计入 `rejections`。
- 循环守卫 `guard`：同一命令重复 ≥3 次、连续 2 轮无新证据、命令长度超限、`remainingRounds` 不足时，禁止再次下命令，改为要求给出最终答案或等待。
- 停止条件 `stop_reason`：步数预算（默认 24 条命令）用尽、剩余回合 ≤2、重复命令、连续解析失败 ≥3 —— 命中后不再发起新的命令调用，提示模型直接提交当前可验证的部分答案。
- prompt：`SYSTEM` 采用 11 号文档的中文系统提示（保留“沙盒环境一无所知”“文档绝对路径未知”等既有措辞要求），追加阶段行与 JSON 上下文；`purpose` 字段进入日志便于复盘。
- 经验沉淀：任务结束时把最近一次 `taskSummary` 归档进 `memory`（沿用 `tasks/memory.py`），不把旧答案当事实。

### 4.6 审查 agent

`review_command(turn, rid, cmd)` → findings：未知 action、`targetPos` 结构／坐标类型错误、越界、`attack` 缺 `controllerId`、目标数与武器等级不符、`use` 缺名字、`buy/sell` 数量非法、字符串超长。

`review_response(turn, response)` → findings：键集合错误、`prompt` 与 `executeCmd` 互斥被破坏、响应超过平台长度上限、同一单位多条命令、`submitAnswer` 无任务或答案为空。

`review_action(raw_text, pending)` → findings：模型回复不是 JSON、动作类型未知、command 与 answer 同时出现或都缺失、`purpose` 缺失。

审查 agent 只报告，不改变合法性判定（`guard.validate` 仍是唯一过滤器）；结果写入 `state['review']`、黑板与日志行 `审查结果`，供复盘与后续自动修复。

## 5. 修改范围

- 新增：`src/agent/agents/`（8 个模块 + `AGENT.md`）、`tests/test_agents.py`。
- 修改：`engine.py`（接入任务／审查 agent 与黑板）、`policies/defense.py`（态势 + 攻击子 agent）、`policies/economy.py`（防御工作计划与石头上限）、`policies/mining.py`（避让路径与售卖时机）、`policies/pioneer.py`（威胁过滤）、`tasks/workflow.py`、`tasks/prompts.py`（自进化循环）、`telemetry/events.py` 与 `writer.py`（agent 摘要日志行）。
- 不改：`main3.py`（入口哈希受 `fixtures/entry-identity.json` 保护）、`tools/baseline_agent`、`guard.legal` 的合法性规则、官方 `docs/`。

## 6. 验收场景与风险

验收场景（新增测试 + 既有回归）：

1. 夜间评估：给定机器人波次，`assess` 给出的我方每回合伤害、机器人威胁与损伤清单与手工计算一致。
2. 攻击收益：只有攻击我方的机器人进入威胁评估；同一瞄准点命中 2 个敌人时收益高于命中 1 个；超杀不重复计分；加特林锥形、火箭锚点等既有几何不变量不变。
3. 修缮路径：三面受损墙时 `repair_order` 给出按真实路径最短的最近邻顺序，且不含不可达点。
4. 石头储备：所需石头不超过 5，已有足够石头时不再采集。
5. 售卖时机：防御 6 回合内需要金币而金币不足时立即卖出；新闻给出上涨且金币充足时持有。
6. 任务避险：靠近任务点的机器人若攻击敌方基地（`targetTeam` 为敌方），开拓者不再躲避或退出任务。
7. 自进化循环：模型返回损坏 JSON、重复命令、超预算时不抛异常，日志有原因，且能在剩余回合不足时提交答案；命令与结果继续完整进入下一轮 prompt。
8. 审查 agent：非法命令与冲突响应产生 finding，但合法响应不产生 finding、命令不被误删。

风险与局限：本地裁判与 fixture 不能证明真实平台得分；攻击收益中的伤害换算基于任务书表格（机器人 HP／攻击力）与我方武器等级的近似，官方若给出更精确公式需重新校准；`targetTeam` 为空时按“可能攻击我方”保守处理；黑板与审查记录会增加每回合状态体积，已按条数与字符数设上限。

## 7. 实施记录

实施：2026-09-20 完成第 4 节全部模块（observer 除外，仅设计）。

### 7.1 新增文件

| 文件 | 内容 |
| --- | --- |
| `src/agent/agents/__init__.py` | 包说明与新增规则（纯函数、只写传入 state、合法性仍在 guard、不抛异常） |
| `src/agent/agents/blackboard.py` | 共享摘要：`write/read/snapshot/log_summary`，单条 6000 字符上限 |
| `src/agent/agents/defense_agent.py` | `assess`（火力对比/损伤/经济/破墙回合）、`work_plan`（优先级 100→20 的有序工作项，含 ETA 与理由）、`stone_reserve`（上限 5）、`repair_order`（真实路径最近邻）、`log_reason`（任务切换记录） |
| `src/agent/agents/attack_agent.py` | `threats`（只算 `targetTeam` 为空或我方的机器人）、`aim_value`（中心 20／溅射 10、超杀按剩余血量计、威胁削减、溅射加成）、`choose`（火箭候选按收益重排，几何结果作回退） |
| `src/agent/agents/economy_agent.py` | `survey`（矿山/价格/距离/批量/性价比，含价格历史）、`sell_timing`（夜间、预测持有、身边小贩、防御资金缺口、夜前窗口、满包六条规则与理由）、`plan`、`route_to`（机器人 3 格攻击圈避让 + 普通路径回退）、`funding_need` |
| `src/agent/agents/task_agent.py` | `has_task_work`、`use_wall_fixer`、`use_held`（把已购升级券送到目标建筑并使用）、`buy_for_defense`（只在明显盈余时购买，且不买仅防御工自己会用的基地券）、`standby`、`support` 阶梯、`voucher_target` |
| `src/agent/agents/self_evolve.py` | `new_state/facts/note_command/note_result/parse_action/guard_reason/stop_reason/answer_only/budget_note/build_context/summary_from` |
| `src/agent/agents/review_agent.py` | `review_command/review_response/review_action/audit` |
| `src/agent/agents/AGENT.md` | 包内数据流、不变量、验证命令 |
| `tests/test_agents.py` | 29 项新测试（见 8.1） |

### 7.2 既有文件的接入点

- `engine.py`：每回合先 `defense_agent.assess` 产出态势；开拓者按 `task_agent.has_task_work` 决定走任务还是防御协助；提交前 `review_agent.audit`，之后仍由 `guard.validate` 过滤。
- `policies/defense.py`：夜间开火改为 `attack_agent.choose`，把评估写入黑板与 `goal` 理由；`should_recall`、`emergency_upgrade`、`defense_assignments` 未改。
- `policies/economy.py`：防御工日程保留 gather→service→home，接入 `defense_agent.work_plan`（工作计划、石头上限、修复顺序、任务切换记录）。
- `policies/mining.py`：`plan_miner` 由 `economy_agent.plan` 决定采集/售卖与理由；`move_to` 走机器人避让路径；矿点锁定、价值排序、集中售卖未改。
- `policies/pioneer.py`：`risk` 只统计攻击我方的机器人（`targetTeam`），任务点一格内约束与退出原因未改。
- `tasks/workflow.py`：任务状态机保留，解析改用 `self_evolve.parse_action`（不抛异常），命令/结果进入 `self_evolve` 的滚动记录，守卫命中时丢弃命令并要求直接作答；`tasks/prompts.py` 改为探索提示 + 收尾提示两套。
- `telemetry/writer.py`、`events.py`：新增 `防御评估／防御计划／防御任务切换／攻击评估／经济计划／任务计划／自进化循环／审查结果` 行，**追加在既有行之后**，不改变既有行序。

### 7.3 一处既有测试预期的变更（含原因）

`tests/test_intelligence.py::test_task_calls_do_not_consume_general_quota` 原先连续四轮发送**完全相同**的 `printf 42` 并期望每次都下发。新的循环守卫把“同一命令重复 3 次”或“连续三次返回相同结果”视为无进展并停止探索（11 号文档第 17 节），因此该场景在第四轮不再下发命令。该测试的本意是验证“任务调用不消耗新闻配额”，与循环守卫无关，故改成每轮不同命令/不同返回（更贴近真实探索），原有断言（下发内容、`intelligence.used==0`、`stage=='solve'`）全部保留；守卫本身由 `test_agents.py` 的两个用例覆盖。没有为通过测试而放宽任何策略保护。

## 8. 验证记录

### 8.1 单元与回归

```bash
CORE_GEEK_DEBUG_LOG=off PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

结果：**140 项通过**（既有 109 项 + 新增 `test_agents.py` 31 项），无跳过。新增覆盖：

- 防御评估数值（我方每回合伤害 60=3×20、机器人过滤、破墙回合 300/10=30）、石头上限、最近邻修复路径（顺序与累计长度单调）、工作计划把“将被攻破的墙”排在首位、任务切换记录。
- 攻击收益：`targetTeam` 过滤、溅射加成、超杀不重复计分、瞄准点在射程内、无威胁时返回 `no_threat`。
- 经济：夜间持有、身边小贩即卖、新闻预测持有到 `holdUntil`、资金缺口强制卖出、价格历史、机器人攻击圈避让、防御资金需求。
- 任务：修复包对将破围墙使用、携带券的送达与使用、盈余门槛（不抢占防御金币）、不购买只有防御工会用的基地券、攻击敌方的机器人不再触发躲避。
- 自进化：重复命令、无进展、步数预算、剩余回合、任意损坏输入不抛异常、摘要提取的尺寸与容错。
- 审查与黑板：命令格式问题、通道冲突、干净响应零 findings、黑板截断与日志摘要。
- 整轮回归：持有基地升级券时工作计划必须正常返回；连续 140 回合（跨一整天）不得出现回退空响应。

### 8.2 自进化任务沙盒流程

```bash
PYTHONPATH=src python3 -B -m lab.sandbox_scenario --output artifacts/v08-sandbox
```

结果：三个连续城市任务分别在**第 21、56、91 回合**提交（与 v0.7 相同），说明“记录上下文→调用 LLM→executeCmd→观察→提交”闭环在改写后未退化。180 回合日志中新增行齐全（`防御评估/防御计划/防御任务切换/攻击评估/经济计划/任务计划/自进化循环/审查结果` 各 180 行，任务切换 156 行），`审查结果` 全部为 `clean`（0 条格式问题）。计划切换统计：`build_wall→defend_ready` 65 次（夜前回防）、`build_wall→build_wall` 41 次（换到下一个最近墙位）、`defend_ready→build_wall` 26 次（次日复工）、`build_wall→use` 20 次（券到位），均为规则驱动的切换而非抖动。

### 8.3 本地换边对局：两次回归与根因（2 种子 × 双向，1300 回合）

第一次运行（`artifacts/v08-matches`）候选 4601:5164、4619:5135，明显低于 v0.7 的 4992:5182、4991:5168。逐半场定位到**两个**根因，它们在两次运行中先后暴露。

**根因 A：开拓者买了升级券却不送达，反而挡住防御工自己的采购（首次运行暴露）。**
`agents/task_agent` 的协助采购最初只实现了“购买”。而 `policies/economy.purchase_options` 有一条既有保护：同类券只要在任一单位背包里就不再购买。于是开拓者买下的 `WeaponUpgradeVoucher1/StationUpgradeVoucher1` 永久躺在背包里（该半场日志显示开拓者 200 金买券后从未 `use`），防御工再也买不到武器券，武器全程 level1（射程 10），只能打进入射程的少数机器人：换边半场 306 次瞄准仅 7 次击杀，而 v0.7 为 168 次击杀、三塔 level3。
修复：新增 `task_agent.use_held`，把已持有的券送到对应建筑并使用；`support` 阶梯改为“修复包 → 送达已持券 → 购买 → 待命”，保证不会先买后忘；采购只在**明显盈余**时进行（`gold ≥ 价格×数量 + 防御储备 + 100`），且不再购买只有防御工自身紧急逻辑会读取的基地升级券。

**根因 B（致命）：防御工作计划抛 KeyError，整回合回退为空响应（修复 A 后的第二次运行暴露）。**
`defense_agent.work_plan` 的基地应急分支写成 `report['firepower']['incoming_damage']`，而该字段实际位于 `firepower.robots` 之下。只要**防御工自己**背包里有一张基地升级券且基地等级<3，此后每个“正常工作”的白天回合都会抛异常；`Session.decide` 按设计回退为空响应且不提交状态，于是整个白天无人行动（矿工、开拓者也一并停摆）。修复 A 之后开拓者不再替防御工买基地券，防御工自己买下了它，这个隐藏错误随即全面触发。
证据（`artifacts/v08b-matches`）：日志 `日志状态 {"error":{"type":"KeyError","message":"'incoming_damage'"}}` 出现 **1496** 次（v0.7 与修复后均为 0 次）；防御工白天 455/560 回合无动作（v0.7 为 130/700），矿工背包涨到 100 却不再售卖，三塔停在 level1，最终两名工人在第 1000—1040 回合阵亡。该次运行在定位后即中止，未写入 summary，不作为成绩证据。
修复：改为 `report['firepower']['robots']['incoming_damage']`，并新增两项回归——`work_plan` 在持有基地券时必须正常返回工作计划；以及**跑满一整天 140 回合的整轮回归**，断言 `session.state['fallbacks']` 为空、裁判无错误、角色计划存在。

修复后复测（`artifacts/v08c-matches` 定位问题，`artifacts/v08d-matches` 为优化后的最终复测，两者分数完全一致、日志中 `日志状态` 错误 0 次）：

| 半场 | 候选 | 基线 | 任务分 | 击杀分（击杀数） | 生存分 | 三塔最终等级 |
| --- | --- | --- | --- | --- | --- | --- |
| 种子1 正向 | 2506 | 2558 | 1600 | 356（208） | 550 | 3/3/3 |
| 种子1 换边 | 2497 | 2596 | 1600 | 347（182） | 550 | 3/3/3 |
| 种子3 正向 | 2506 | 2552 | 1600 | 356（208） | 550 | 3/3/3 |
| 种子3 换边 | 2492 | 2607 | 1600 | 342（174） | 550 | 3/3/3 |
| **合计** | **5003 / 4998** | 5154 / 5159 | 3200 | 1401（772） | 2200 | — |

对照 v0.7（4992 / 4991，击杀 379+368=747）：总分与击杀均**不再回退并略有提高**；任务分 1600 对基线 2000 仍是已知的本地 fixture 通道偏差（候选要经命令反馈往返，基线直接得到答案），与 v0.7 相同。最终复测的 `source_unchanged=true`、`comparison_eligible=true`，即源码在运行期间未被改动，结果可用作同版本对比。

### 8.4 决策时延

新增 agent 最初把 p95 从 v0.7 的 13—16ms 推到 250—330ms（`max` 535—602ms）。cProfile 定位到两处重复路径搜索：

- `economy_agent.survey` 对**每个**矿山的每个站位各做一次 BFS（单次约 1 秒）；
- `defense_agent.repair_order` / `work_plan.walk_eta` 对每个目标站位各做一次 BFS。

修复：survey 只对最近的 6 个矿山实测距离，其余用切比雪夫距离估算并标记 `estimated`（真正的采集决策仍由 `policies/mining.candidates` 用精确路径给出）；`route()` 支持多目标，因此每个目标改为**一次**多目标搜索；修复顺序按天缓存（`state['defense_route']`）；ETA 只对可能执行的前 5 个工作项计算。

结果（最终复测 `v08d-matches`）：四个半场决策 p95 为 18.9／19.1／21.7／26.9ms，max 为 41.5／48.3／44.4／80.9ms，与 v0.7 同量级；完整回归耗时从 25s 降到 6s。

### 8.5 局限

- 本地裁判与 fixture 是近似实现，分数、击杀与任务时延都不能当作官方胜率；真实 LLM 的探索质量仍未被本地 fixture 证明。
- 攻击收益换算（我方伤害、机器人伤害/血量/射程）来自任务书表格与本地裁判，若官方给出更精确公式需重新校准。
- 自进化循环的停止阈值（24 步、重复 3 次、剩余 2 回合）是第一版工程取值，需用真实任务轨迹调整；当前证据来自确定性 fixture。
- 全局观察 agent 只有数据面（黑板）；建议与冲突说明尚未实现，也没有离线回放验证其建议质量。
- 本地对局只跑了种子 1、3；任务分仍受 fixture 通道偏差影响，不能据此判断真实平台任务收益。

