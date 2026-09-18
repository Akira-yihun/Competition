# DeepSeek 交付包 ·《未来战争》参赛 Agent 目标/策略/Loop/日志

> 归档目录：`Demo/CoreGeek/docs/DeepSeek/`
> 依据版本：`docs/任务书.md` v1.0（2026-09-09）、`docs/接口文档.md` v1.0、`docs/request.txt`、`docs/response.txt`、`Demo/CoreGeek/src/**`
> 生成方式：规则逐条推导 + 报文实测 + 数值建模 + 多 Agent 对抗式评审（见文末"评审记录"）

---

## 1. 文件清单

| 文件 | 作用 | 读者 |
|---|---|---|
| `01-游戏目标与策略分析.md` | 目标函数、得分线量化模型、空间/火力/人力/经济策略、优化清单（P0~P3）、**16 个必须确认的问题** | 人 + Agent（选假设来源） |
| `02-Loop工程计划.md` | 闭环迭代系统设计：三层架构、状态机、`loopctl` CLI 契约、**预留平台接口清单**、多局统计判定、护栏、落地阶段 | 人 + 实现者 |
| `03-日志格式与加密设计.md` | fwlog v1：事件模型、帧格式、AEAD 方案、密钥管理、KAT 向量、工具链、性能实测 | 实现者 |
| `04-执行Agent操作手册.md` | **可直接作为另一个 Agent CLI 的系统提示词**：路径、硬规则、逐条命令、决策表、故障处置 | Agent |
| `templates/loop.config.json` | ★ **唯一需要人工填充的配置**（所有平台接口/凭据/阈值都在这里） | 人 |
| `templates/endpoints.reserved.json` | ★ 15 个预留接口的完整骨架（方法/路径/字段映射/状态词表/幂等键） | 人 |
| `templates/hypothesis.md` | 假设卡模板（四项必填：观察/改动/预期/反例） | Agent |
| `templates/iteration-report.md` | 迭代报告模板（结论/合规/KPI/统计/归因/未解释现象/下一轮建议） | Agent |
| `schemas/fwlog.event.schema.json` | 日志事件 JSON Schema（含 16 类事件的条件校验） | 实现者 |
| `schemas/loop.state.schema.json` | loop 状态文件 JSON Schema（状态机/幂等 receipt/预算/噪声地板） | 实现者 |
| `reference/fwlog_crypto_ref.py` | **可执行规范**：fwlog v1.1 加密参考实现（仅标准库）+ **15 项自测（含对抗用例）** + KAT + 基准 + **可用的 `--verify`/`--dump`** | 实现者 |
| `reference/fwlog_kat.json` | 已知答案向量（KAT），跨语言实现必须逐字节复现 | 实现者 |
| `reference/make_sample.py` | 生成端到端样例（含真实加密的样例日志） | 实现者 |
| `reference/selfcheck.py` | **交付包自检**：校验文档/模板/schema/参考实现/样例互相一致（**78 项**）：含"样例每帧必须通过 event schema"、"样例可复现"、"锚能检出截断" | 人 + Agent |
| `samples/sample-match.fwl.jsonl` | **14 帧真实加密的样例日志**（含 boot/cfg/round/cmd/ack/guard/decision/outcome/task/sandbox/econ/anomaly/stat/bye） | 实现者 |
| `samples/sample-match.meta.json` | 解样例所需的上下文（**演示密钥，非真实密钥**，由固定公开种子派生 ⇒ 样例可复现） | 实现者 |
| `samples/sample-match.anchor.json` | 终结锚（验证尾部截断；无锚则截断不可检测） | 实现者 |
| `samples/sample-match.readme.txt` | 解密样例日志的一行命令 | 实现者 |
| `REVIEW.md` | 多轮对抗式评审记录与修订台账 | 人 |

---

## 2. ★ 需要你（人）确认后手动填充的部分

全部集中在 **两个文件**，填充后 `loopctl doctor` 会变绿，其余代码无需改动：

### 2.1 `templates/loop.config.json`（拷到 `loop/config/loop.config.json`）

| 键 | 现在 | 需要你提供 |
|---|---|---|
| `platform.base_url` | `__FILL__` | 比赛平台地址 |
| `platform.auth.mode` | `__FILL__` | `none / bearer / cookie / basic / signed` |
| `platform.auth.*_env` | 已预设 | 账号口令/Token 的**环境变量名**（不要把明文写进文件） |
| `platform.identity.team_id` | `__FILL__` | 我方队伍 ID |
| `platform.endpoints` | 指向模板 | 见 2.2 |
| `platform.match_defaults.opponent_id` | `__FILL__` | 默认对手（建议固定对手做 A/B） |
| `platform.match_defaults.count` | `3` | 每轮迭代打几场（1 场 = 2 个半场样本） |
| `platform.match_defaults.seed` | `null` | 平台是否支持固定随机种子（**支持的话样本效率翻倍**） |
| `platform.log_sink.mode` | `file` | 日志是本地文件还是要往平台传（`file / http`） |
| `guardrails.require_approval` | `["upload","match"]` | 是否要人工确认再上传/开战；全自动就改成 `[]` |
| `acceptance.primary_metric` | `win_rate` | 主指标（建议先 `win_rate`，稳定后切 `score_total`） |

### 2.2 `templates/endpoints.reserved.json`（15 个端点，逐个填 `__FILL__`）

优先级从高到低：`auth.login` → `artifact.upload` → `build.status` → `match.create` → `match.status` → `match.result` → `logs.list` → `probe.ping`，其余（`opponent.list / build.list / match.list / leaderboard / logs.upload / team.info / auth.refresh`）可后补。

每个端点只需填三样：**method / path / response_map**（极简 JSONPath）。若平台返回字段名不同，改 `response_map` 即可，**不用改代码**；状态字面量不同（如 `"0/1/2/3"` 或 `"PENDING/COMPILING/..."`）改 `status_vocab`。

> **接口没确定也能开工**：先用 `--adapter fixture` 把整条链路（含状态机、护栏、打包、日志解密、分析、统计判定）全部离线跑通；平台一填立刻切 `--adapter http`。

### 2.3 与游戏规则相关的待确认项

见 `01-游戏目标与策略分析.md` §8 的 **Q1~Q16**。其中"猜错会直接掉分/出局"的六个：
**Q3（武器能否打敌方基地 / 火箭 L1 是否全图）、Q1（机器人能否隔墙打基地）、Q5（`build` 的 name 取值）、
Q11（`errors[]` 与异常次数的口径）、Q13（冷却期发 attack 算不算异常）、Q15（规则性非法算不算异常）**。
建议**第一场正式对局就用来跑这些判定实验**（用 `loopctl experiment run` 驱动，不要用临时脚本）。

---

## 3. 术语澄清（避免两个"LLM/Agent"混淆）

本交付里出现两类完全不同的 Agent/LLM，**不要混为一谈**：

| | **开发期 Agent（Dev Agent）** | **运行期 LLM（In-game LLM）** |
|---|---|---|
| 是什么 | 你用来写代码的 Agent CLI（Claude Code / Codex / DSH…） | 比赛平台提供的 LLM 接口：`Response.prompt` 提问、下一回合 `Request.llmResp` 拿回答 |
| 谁调用 | 人 + `loopctl` | 参赛代码自己调用（用于**自进化类任务**） |
| 约束 | 无比赛限制，但要遵守本文档的护栏 | 每游戏日 **3 次**；**任务执行期间不计入、不受限**（接口文档 errorCode=5 注） |
| 相关文档 | `02`、`04` | `01` §2.4 / §3.3，`03` 的 `task`、`sandbox` 事件 |

- **Dev Agent 的产物是代码**（含"如何调用 In-game LLM"的策略）；**In-game LLM 的产物是任务答案**。
- loop 的 KPI 里 `score_1` 同时受两者影响：Dev Agent 决定"SOP 怎么设计"，In-game LLM 决定"这一次答得对不对"。
- 这也是为什么日志必须记录 `task`（状态机）与 `sandbox`（`executeCmd` 往返）两类事件——**否则无法区分"是策略错了"还是"LLM 答错了"**。

---

## 4. 三条最重要的结论（如果只读三句话）

1. **真正的稀缺资源有排序**：**① 基地血量**（唯一"输"的通道）→ **② 夜间有效开火回合数**（硬上限 3 塔 × 600 = 1800，**买不到扩容**）→ **③ 金币**（唯一能放大伤害的货币，但通道窄且一次性：3 座塔买满约 825 金）→ **④ 动作数**（白天有 28%~35% 余量）→ **⑤ 石头**（**永远不是瓶颈**）。一切优化都要问"这个动作能否换来更多有效开火回合或更高单发伤害"。
2. **任务线（`score_1`）是第一与第二名的分水岭，而且几乎没有机会成本**：`attack` 仅黑夜、`build` 仅白天 ⇒ **白天做任务对火力零成本**；`prompt`/`executeCmd` 是响应顶层字段、**不占角色动作**，一个任务只花约 2 个角色动作，而奖励 + 速度加成（`5×timeout/耗时`）可达 100~350 分、可重复（30 回合冷却），半场量级 3000~8000，远超 `score_3` 的 550 与 `score_2` 的 1000~2000；且**任务期间 LLM 调用不计入每日 3 次额度**。⇒ 开拓者白天常驻任务点，并遵守"天黑前 `距离+2` 回合停止接新任务"。
3. **围墙不能"外扩"，而且塔位是个匹配问题**：可建造区域固定为**距离 1 的 12 格（武器）**与**距离 2 的 20 格（围墙）**，距离 ≥3 根本不可建造（修正了 `Demo代码优化分析.md` §3.1 的"外扩到 d≥3"建议）。同时 **220 种三塔布局里有 12 种无法让三塔同时各占一个操控位**（实测，判据 = 二分图完美匹配；另有 4 种更极端）——**基线 Demo 恰好选中"直线三塔"，满环后其中两座塔抢同一个操控位，每回合最多 2 塔开火，等效丢掉约 1/3 火力**。

---

## 5. 建议的推进顺序

```
① 人：填 §2.1 / §2.2 的平台接口（或先跳过，用 fixture 适配器）
② Agent：实现 loopctl 骨架 + guard + 打包 + fwlog（Phase 0，离线可跑通）
③ 人 + Agent：第 1 场真实对局，跑 Q1/Q3/Q5/Q11/Q13/Q15 判定实验
④ Agent：按 01 文档 §7 的 P0 → P1 → P2 → P3 顺序迭代（每轮一个假设）
⑤ 人：定期看 runs/<iter>/analysis/report.md 与 scoreboard.csv
```

---

## 6. 目录使用约定

- `docs/`、`loop/`、`secrets/` 是**受保护路径**，执行 Agent 不得修改。
- `secrets/` 永久 gitignore：日志主密钥、平台凭据、Cookie 都在这里。
- `runs/<iterId>/` 是每轮迭代的完整产物（可复现、可审计、可回滚）。
- 本目录（`docs/DeepSeek/`）是**人工归档区**，Agent 只读。

---

## 7. 评审记录

| 轮次 | 方式 | 参与 | 主要产出 |
|---|---|---|---|
| **第 1 轮 A/B** | 独立推导（互不可见） | 规则/协议审计 Agent、策略分析 Agent | 22 项字段契约、35 条开放问题、**修正"墙环外扩 d≥3"的错误建议**、确认 `attackRange` 三方冲突、12 条反直觉洞察 |
| **第 1 轮 C** | **对抗式评审 ×4** | 策略 / Loop 计划 / 日志与加密 / 需求覆盖 | 策略 4 严重 + 20 中等；Loop 40+ 条（含 3 个"接平台必翻车"点）；**加密 5 个可复现 PoC**（含 1 个明文泄漏）；覆盖矩阵 7/10 分 + 30+ 处不一致 |
| **第 2 轮 D** | **本机脚本复算** | 脚本（不依赖 Agent 判断） | 9 项验证；**推翻 2 处 Agent 说法**（"12 种死布局"的措辞、"外扩 d≥3"的建议） |
| **第 3 轮** | 按清单逐条修订 + 自检 | 本文档 | 5 严重 + 34 中等 + 27 轻微全部处理；**78 项归档自检全绿** |

> 完整的发现、证据与处理结果见 **`REVIEW.md`**（含每个缺陷的"来源 / 问题 / 结局 / 落点"四列台账）。

---

## 8. 归档自检（交付前请先跑这个）

```bash
cd Demo/CoreGeek/docs/DeepSeek
python3 reference/selfcheck.py            # 78 项：JSON 合法性 / 密码学自测 / KAT 逐字节 / 样例加解密 /
                                          # schema 校验 / 锚检出截断 / 端点一致性 / 常量一致性 /
                                          # 文件清单 / 密钥卫生
python3 reference/fwlog_crypto_ref.py --selftest    # 15 项（含跨运行拼接、截断、nonce、算法绑定等对抗用例）
python3 reference/make_sample.py --check            # 样例可复现性
```

**当前状态：全部通过**（selfcheck 78/78、selftest 15/15、样例可复现）。
