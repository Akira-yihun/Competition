# 02 · Loop Engineering 计划（供另一个 Agent CLI 驱动）

> 上游依据：`01-游戏目标与策略分析.md`（目标函数、假设队列 P0~P3、待判定问题 Q1~Q22）
> 本文定义：一套**可由任意 Agent CLI 驱动**的闭环迭代系统——改码 → 提交 → 打包 → 上传 → 等编译 → 对战(×N) → 取日志 → 分析 → 决策 → 下一轮。
> 平台侧接口**全部预留占位**，用户确认后只需填**一个 JSON**（`loop/config/loop.config.json` 的 `platform` 段）。
>
> **v1.1 说明**：v1.0 经对抗式评审发现 40+ 处缺陷（业务码误判、包身份与幂等、need_more 死循环、统计三处根本性错误、
> 模板硬错误、`.gitignore` 缺失、手册引用不存在项等）。本版逐条修正，修订台账见 `REVIEW.md`。

---

## 0. 设计原则

| 原则 | 做法 | 原因 |
|---|---|---|
| **Agent 只对话，不碰细节** | 平台交互全部封装进确定性 CLI `loopctl`；Agent 只调 `loopctl <verb> --json` | Agent 不必理解 Cookie/分页/重试/业务码；平台改版只改适配器 |
| **一次调用 = 一个 JSON** | 每个 verb 的 stdout **有且只有一个 JSON 对象**，人读信息走 stderr | Agent 可 `json.loads(stdout)` |
| **成功判定看业务码** | 平台返回 `{code:0,data:...}` 才算成功；HTTP 200 + `code!=0` 一律失败 | v1.0 只看 HTTP 状态 ⇒ 空 `matchIds` 也会推进状态机 |
| **关键字段 fail-closed** | DTO 的必需字段缺失 ⇒ 立即 `blocked`，**不允许**带 None 继续 | 防止"格式正确但语义为空"的静默错误 |
| **可断点续跑** | 两阶段 receipt（intent/done）+ 服务端幂等键 + `reconcile` 对账 | 覆盖"请求已发出但响应丢失" |
| **显式下一步** | 每个 JSON 带结构化 `next_actions` 与 `blocked_reason` | Agent 不需要猜 |
| **一切可回滚** | 每轮一个分支 + 一个 tag | 失败零成本 |
| **单假设单变量** | 一轮一个假设 + diff 预算 | 否则无法归因 |
| **判定实验与迭代分开** | `--profile experiment` 允许刻意违规且**不进 scoreboard** | 否则"验协议"必然与"异常数=0"门禁冲突 |

---

## 1. 系统架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│  L3  Driver Agent（另一个 Agent CLI）                                     │
│      读：01 策略文档、04 操作手册、runs/*/analysis/report.md、scoreboard.csv │
│      做：改代码、写假设、调 loopctl、读 JSON 决定下一步                      │
└───────────────▲─────────────────────────────────────┬───────────────────┘
                │ 单个 JSON（stdout）                   │ shell
┌───────────────┴─────────────────────────────────────▼───────────────────┐
│  L2  loopctl（Python 3.11+，仅标准库，无 LLM，确定性）                     │
│      状态机 · 幂等/对账 · 重试 · 超时 · 打包 · 护栏 · 统计 · 日志解密分析    │
└───────────────▲─────────────────────────────────────┬───────────────────┘
                │ 归一化 DTO                           │
┌───────────────┴─────────────────────────────────────▼───────────────────┐
│  L1  Platform Adapter：http（真实）/ fixture（离线回放）/ mock             │
│      全部端点在 loop.config.json → platform.endpoints 一处配置            │
└─────────────────────────────────────────────────────────────────────────┘
```

**信任边界（诚实声明）**：Agent 与 `loopctl` 同用户同权限，`guard`/审批是**防误操作的护栏，不是安全边界**。
若需要真正的隔离，应把平台凭据放在另一个用户/容器下，只暴露 `loopctl`。

---

## 2. 目录结构与归档边界

> **归档边界**：`docs/DeepSeek/` 存放设计、契约、模板、参考实现、样例（本次交付的全部内容）。
> `loop/`、`runs/`、`secrets/` 是**运行期产物**。若希望全部集中在一处，把下表的 `loop/` 前缀换成
> `docs/DeepSeek/loop/` 并同步改 `repo.root` 与 `guard.checks` 的脚本路径即可，**没有其它耦合**。

```
Demo/CoreGeek/
├── docs/DeepSeek/                     # 本次交付：设计 + 模板 + 参考实现 + 样例
├── loop/                              # 实现代码（Phase 0 交付物）
│   ├── loopctl.py                     # 唯一入口
│   ├── config/loop.config.json        # ★ 唯一需要人工填充
│   ├── lib/{state,adapter_http,adapter_fixture,packager,guard,stats,kpi,report}.py
│   ├── lib/fwlog/                     # 照抄 docs/DeepSeek/reference/fwlog_crypto_ref.py
│   ├── fixtures/                      # 录制的平台响应（离线开发）
│   ├── templates/                     # 从 docs/DeepSeek/templates/ 拷入
│   └── state/{loop_state.json, journal.jsonl, scoreboard.csv, noise.json, backlog.json}
├── runs/<iterId>/                     # 每轮迭代产物
├── secrets/                           # ★ 必须 gitignore
└── tests/                             # 单测 + 黄金用例（Phase 0）
```

**必需的 `.gitignore`**（当前仓库**没有**，实测 `git check-ignore` 失败 —— 这是 P0-0 级的安全项）：

```gitignore
secrets/
src/agent/_fwsecret.py
src/agent/__pycache__/_fwsecret*
runs/
loop/state/
logs/
*.fwl.jsonl
*.anchor.json
```

**受保护路径**：`loop/**`（除 `loop/state/**`）、`docs/**`、`secrets/**`、`.git/**`。
**例外（bootstrap 必需，否则死锁）**：Phase 0 允许 Agent 创建 `loop/**`、`tests/**` 与 `run.sh`；
进入 Phase 1 之后它们才转为受保护。该例外由 `guard.bootstrap_phase: true` 显式开启。

---

## 3. 状态机

### 3.1 状态与迁移（**与 `schemas/loop.state.schema.json` 的 enum 完全一致**）

```
IDLE
 └─ iter.open ──► NEW
      └─ (Agent 改代码) ──► EDITED ──iter.guard──► GUARDED
           ▲                   │(不通过)
           └───────────────────┘
      GUARDED ──iter.commit──► COMMITTED ──iter.build──► PACKAGED
           PACKAGED ──iter.upload──► UPLOADED ──await-build──► BUILD_PENDING
                ├─► BUILD_OK ──iter.battle──► BATTLE_PENDING ──► BATTLE_RUNNING
                │        └─► MATCH_DONE ──fetch-logs──► LOGS_FETCHED ──analyze──► ANALYZED
                │                                                                   │
                │                                                          iter.decide
                │                                                                   ▼
                │                                                    DECIDED(accept|revert|need_more|inconclusive)
                │                                                       │        │            │
                │                                        iter.close ────┘        │      (补样)└──► BATTLE_PENDING
                │                                                ▼               │            （新 batch_index）
                │                                            CLOSED ◄────────────┘
                ├─► BUILD_FAILED ──(Agent 修码)──► EDITED（同一迭代，≤max_build_retries）
                │        └─(超过重试上限)──► FAILED
                ├─► BUILD_STUCK / MATCH_STUCK ──► BLOCKED
                └─► NEEDS_HUMAN（任何 needs_human=true）

任何状态 ──(不可恢复错误/预算耗尽)──► FAILED | BLOCKED
```

- **`CLOSED` 是唯一正常出口**：`accept` / `revert` / `inconclusive` / `need_more 补样后达成结论` 都收敛到它。
  `inconclusive` **也是正常收尾**（保留分支、写回 backlog、换下一条假设），不会卡死下一轮 `iter.open`。
- **`FAILED`（不可恢复）** / **`BLOCKED`（可恢复但需外部条件）** / **`NEEDS_HUMAN`（等人）** 三者语义分离：
  - `BLOCKED` / `NEEDS_HUMAN` 带 `resume_from`（从哪个 verb 续跑）与 `attempts`；
  - 人处理后 `loopctl resume` 会从 `resume_from` 继续，**不会重头再来**。
- **`EDITED` 可达性**：`iter.guard` 会先校验 `git diff <base_tag>` **非空**；
  若 diff 为空 ⇒ 直接 `blocked_reason="no_change"`，**不允许**拿字节等价的包去打 3 场（v1.0 的隐患）。
- **单一真相源**：`loop/state/loop_state.json` 是唯一状态；`runs/<iter>/iter.json` 是**只读快照**；
  `journal.jsonl` 是**追加日志**。三者冲突时以 `loop_state.json` 为准。

### 3.2 每步契约

> **命令写法约定**：`next_actions[].verb` 用**点号**（`iter.await-build`），对应的命令行写法是把点号换成**空格**
> （`python3 loop/loopctl.py iter await-build`）。两者一一对应：`verb.replace(".", " ")`。
> 下表的"verb"列用命令行写法。

所有 verb 通用参数：`--json`（默认开）、`--iter <id>`（缺省 = 活跃迭代）、`--timeout <sec>`、`--dry-run`（**不写 receipt、不推进状态**）、`--adapter http|fixture|mock`、`--profile standard|experiment`、`--tier smoke|standard|confirm`。

| step | verb | 关键输入 | 输出 `data` 关键字段 | 退出码 | 重试 |
|---|---|---|---|---|---|
| 配置自检 | `doctor` | `--probe` | `{unfilled:[], missing_files:[], reachable:{}, logs_probe:{ok,frames}}` | 0/2 | — |
| 开迭代 | `iter open` | `--hypothesis-file` | `{iter_id, branch, base_commit, base_tag, baseline:{iter_id\|null}}` | 0/3 | — |
| **改码** | *（Agent 直接 edit）* | — | — | — | — |
| 本地护栏 | `iter guard` | — | `{pass, checks:[{name,pass,detail}], diff:{files,added,deleted}}`；diff 为空 ⇒ `blocked_reason="no_change"` | 0/5 | 0 |
| 提交 | `iter commit` | `-m`, `--reason`（超预算时必填） | `{commit, rollback_tag, files_changed}` | 0/3 | — |
| 打包 | `iter build` | — | `{artifact, artifact_sha256, bytes, version, injected:{bid}}` | 0/3 | — |
| 上传 | `iter upload` | — | `{upload_id, build_id}`（幂等键 = `artifact_sha256`） | 0/4 | 3 |
| 等编译 | `iter await-build` | `--timeout` | `{build_id, status, duration_s, log_url, build_log_path}` | 0/4/6 | 轮询 |
| 修编译 | *(由 Agent 改码后重跑 build/upload)* | — | 同一迭代内 `retry_no ≤ max_build_retries` | — | ≤3 |
| 开对战 | `iter battle` | `--matches N`, `--opponent`, `--seed`, `--batch-index` | `{match_ids, batch_id, batch_index}` | 0/4 | 2 |
| 等对战 | `iter await-match` | `--timeout` | `{matches:[{match_id,status,result}]}` | 0/4/6 | 轮询 + stuck |
| 取日志 | `iter fetch-logs` | — | `{files:[{name,path,sha256,anchor_path}]}` | 0/4/6 | 3 |
| 分析 | `iter analyze` | `--tier` | `{kpi, baseline, deltas, stats, attribution, report_path}`；**带 `inputs_digest`** | 0/6 | — |
| 判定 | `iter decide` | — | `{decision, reason, stats:{n,delta_ci,mde}, extra_matches}` | 0/3 | — |
| 收尾 | `iter close` | `--decision` | `{closed, tag, board_row}` | 0/3 | — |
| 审批 | `approve` | `--step upload\|match`, `--artifact-sha` | `{approved, bound_to}` | 0/3 | — |
| **一键** | `run` | open 参数 + `--profile`, `--auto` | 串起全流程，返回最终 JSON | 同上 | — |
| 续跑 | `resume` | — | 从 `BLOCKED/NEEDS_HUMAN` 的 `resume_from` 继续 | 同上 | — |
| 面板 | `board` | `--last N` | `{rows, best_iter, trend}` | 0 | — |
| 队列 | `backlog next` / `backlog done <id>` / `backlog add` | — | `{hypothesis:{id,title,metric,expected,falsify,files}}` | 0/3 | — |
| 判定实验 | `experiment run` | `--id Q1 --probe <file> --profile experiment` | `{question, verdict, evidence}`；**不进 scoreboard** | 0/3/6 | — |
| 录制/回放 | `fixture record` / `fixture replay` | `--name` | 离线跑通全链 | 0/2 | — |
| 校准 | `calibrate` | `--matches N` | `{metric, n, mean, std, icc, floor, enough:bool}` | 0/4 | — |
| 鉴权 | `auth refresh` | — | `{ok, expires_at}` | 0/4 | — |

### 3.3 编译失败的自愈回路

```
BUILD_PENDING ──poll──► BUILD_FAILED(build_id, log_url)
    ├─ 拉取编译日志 → runs/<id>/platform/build_log.txt（只落盘，不进报告正文）
    ├─ 把日志尾部 ≤4KB 交给 Driver Agent（不是让人看）
    ├─ Agent 在同一迭代内修复 → 新 commit → 重新 build（新 artifact_sha256）→ 重新 upload → 回到 BUILD_PENDING
    └─ retry_no > max_build_retries(默认 3) ⇒ FAILED + needs_human
```
- **每次重试都是新的上传**（新 `artifact_sha256`），因此**不会命中旧 receipt**（v1.0 的 J2 事故）。
- 重试不消耗"单假设"配额，但消耗上传额度。

### 3.4 统一 JSON 信封（**每个 verb 都必须遵守**）

```jsonc
{
  "ok": true,                       // 语义成功（不代表实验成功）
  "verb": "iter.await-build",
  "iter_id": "i0007",               // ★ 恒定存在（v1.0 有些 verb 没有）
  "state": "BUILD_OK",              // = schemas/loop.state.schema.json 的 enum
  "elapsed_s": 41.2,
  "data": { ... },
  "blocked_reason": null,           // 取值见下枚举
  "human_action": null,             // blocked 时给出**给人看的**一句话
  "needs_human": false,
  "retryable": false,
  "next_actions": [{"verb": "iter.battle", "args": {"--iter": "i0007"}}],   // ★ 结构化，不是裸字符串
  "artifacts": ["runs/i0007/platform/build_status.jsonl"],
  "log_tail": ["..."]
}
```

**`next_actions` 是结构化数组**（v1.0 只有字符串，Agent 漏传 `--iter` 会写错迭代）：
```jsonc
[{"verb":"iter.battle","args":{"--iter":"i0007","--matches":3}}]
```
白名单：`doctor / iter.open / iter.guard / iter.commit / iter.build / iter.upload / iter.await-build /
iter.battle / iter.await-match / iter.fetch-logs / iter.analyze / iter.decide / iter.close / approve /
resume / board / backlog.next / backlog.add / experiment.run / fixture.replay / calibrate / auth.refresh`，
特殊值：`human.report`、`abort`。

**`blocked_reason` 枚举**（每个都对应"找谁"）：

| 值 | 含义 | 找谁 |
|---|---|---|
| `config.unfilled` | 配置未填全 | 人（附 `doctor.unfilled`） |
| `config.missing_file` | 模板/脚本缺失 | Agent（Phase 0） |
| `auth.failed` / `auth.expired` | 鉴权失败/过期 | 先 `auth refresh`，再失败找人 |
| `guard.failed` | 本地护栏不过 | Agent 自己改 |
| `no_change` | 与基线无差异 | Agent 自己改 |
| `build.failed` | 编译失败 | Agent 读日志自己修（≤3 次） |
| `build.stuck` / `match.stuck` | 平台卡住 | 人（附 id） |
| `platform.rejected` | 业务码非 0 | 看 `human_action`（多为参数/额度问题） |
| `platform.empty` | 返回成功但关键字段为空 | 人（fail-closed 触发） |
| `quota.exceeded` / `budget.exceeded` | 额度/预算用尽 | 人 |
| `log.channel_missing` | 日志回流通道未定义 | 人（见 §4.4） |
| `log.verify_failed` | 日志完整性失败 | 人（该样本不参与统计） |
| `stats.insufficient` | 样本不足 | 自动加打 |
| `protected_path.touched` | 改了受保护路径 | Agent 自己回退 |
| `experiment.violation` | 实验profile 下出现异常 | 预期内，仅记录 |

**退出码**（Agent **不要**用它分支，只看 JSON；退出码仅供 `set -e` 之外的工具）：
`0` 正常结束（**含 `needs_human=true` 的情况**，避免 Agent 在最该行动时被 `set -e` 打断）、
`2` 配置错误、`3` 前置条件不满足、`4` 平台错误、`5` 护栏失败、`6` 分析失败。

---

## 4. 平台接口预留清单（★ 待用户确认后填充）

### 4.1 端点表（与 `templates/endpoints.reserved.json` 一一对应）

| id | 用途 | method | path | 关键请求 | 映射到 DTO |
|---|---|---|---|---|---|
| `auth.login` | 登录换 token | POST | `__FILL__` | `{username,password}` | `token, expires_at` |
| `auth.refresh` | 刷新凭据 | POST | `__FILL__` | `{refresh_token}` | `token, expires_at` |
| `team.info` | 队伍信息 + **额度** | GET | `__FILL__` | — | `team_id, team_name, quota` |
| `opponent.list` | 可选对手 | GET | `__FILL__` | `{page,size}` | `items[]` |
| `artifact.upload` | 上传源码包 | POST(multipart) | `__FILL__` | `file, teamId, version, sha256` | `upload_id, build_id` |
| `build.status` | 编译状态 + **产物 sha** | GET | `__FILL__` | `{buildId}` | `status, log_url, duration_s, artifact_sha256` |
| `build.log` | 编译日志正文 | GET | `__FILL__` | `{buildId}` | `text` |
| `build.list` | 最近编译（**对账用**） | GET | `__FILL__` | `{teamId,size,page}` | `items[]`（含 `artifact_sha256`） |
| `match.create` | 创建对战（可批量） | POST | `__FILL__` | `{buildId,opponentId,count,seed,mode}` | `match_ids[]` |
| `match.status` | 对战状态 | GET | `__FILL__` | `{matchId}` | `status, progress, started_at, ended_at` |
| `match.result` | 比分（含 seed/opponent 回显） | GET | `__FILL__` | `{matchId}` | `winner, halves[], seed, opponent` |
| `match.list` | 历史对战（对账用） | GET | `__FILL__` | `{teamId,size,page}` | `items[]` |
| `match.cancel` | 取消卡死的对战 | POST | `__FILL__` | `{matchId}` | `ok` |
| `logs.list` | 日志文件清单/下载地址 | GET | `__FILL__` | `{matchId,half}` | `files[]` |
| `logs.download` | 下载日志正文（预签名 URL/鉴权） | GET | `__FILL__` | `{fileId}` | `bytes/path` |
| `logs.upload` | 日志上报（`sink:http`，双方共用） | POST(ndjson) | `__FILL__` | `X-Fwlog-*` 头 + 帧流 | `accepted` |
| `leaderboard` | 榜单 | GET | `__FILL__` | `{size}` | `items[]` |
| `probe.ping` | 连通性/鉴权探活 | GET | `__FILL__` | — | `ok, server_time` |

### 4.2 三个"接平台必翻车"的点（v1.1 已内置防护）

1. **业务码 ≠ HTTP 码**：所有端点用 `success_when: {code_path:"$.code", ok_values:[0]}` 判成功；
   HTTP 200 + `code!=0` ⇒ `blocked_reason="platform.rejected"`，**绝不推进状态机**。
2. **关键字段 fail-closed**：DTO 里标了 `required:true` 的字段（`build_id`/`match_ids`/`status`/`artifact_sha256`）取不到 ⇒ `platform.empty`。
   ⇒ v1.0 的"空 match_ids 照推 BATTLE_RUNNING → 0 样本 → need_more → 死循环烧额度"不会发生。
3. **列表端点必须有 `items_map` + 分页**：`build.list`/`match.list`/`opponent.list` 三个端点在模板里都带 `items_map` 与 `pagination`；
   否则 `resume` 的"用 `build.list` 反查崩溃前是否已上传"物理上不可实现 ⇒ 必然重复上传。

### 4.3 归一化 DTO

```jsonc
// BuildInfo
{ "build_id":"", "status":"pending|compiling|success|failed", "artifact_sha256":"",
  "created_at":"", "duration_s":0, "log_url":"", "raw":{} }
// MatchInfo
{ "match_id":"", "build_id":"", "status":"pending|running|finished|failed",
  "opponent":{"id":"","name":""}, "seed":null, "progress":0.0, "halves":[] }
// HalfInfo
{ "half_no":1, "side":"challenger|defender", "our_score":0, "enemy_score":0,
  "s1":0,"s2":0,"s3":0, "winner":"us|them|draw",
  "ended_reason":"base_destroyed|round_limit|abnormal", "rounds":0, "base_destroyed":false }
// ScoreRow（scoreboard.csv 一行 = 一个半场样本）
{ "iter_id":"","build_id":"","match_id":"","half_no":1,"side":"",
  "opponent":"","seed":"","ts":"2026-09-17T00:00:00Z",
  "our_score":0,"s1":0,"s2":0,"s3":0,"enemy_score":0,"win":1,
  "anomalies_local":0,"errors_reported":0,"p99_decision_ms":0,
  "idle_tower_rounds":0,"tasks_done":0,"tasks_failed":0,"task_avg_rounds":0,
  "hypothesis_id":"","profile":"standard" }
```

### 4.4 日志回流通道（**Phase 0 必须打通，否则第 7 环没有输入**）

三选一，`doctor --probe` 必须**真取到 ≥1 帧**才算通过：

| 方案 | 做法 | 前置条件 |
|---|---|---|
| A 平台日志接口 | `logs.list` + `logs.download` 下载密文文件 | 平台提供（待确认） |
| B 容器文件通道 | 日志写在容器内固定目录，由平台/人工取出（挂载卷 / 制品回传 / 平台文件浏览） | 需与平台约定路径，**必须写进部署说明** |
| C 双写 | 本地文件 + `sink:http` 上报，任一可用即可 | 需要网络出口 |

---

## 5. 多局对战与统计判定

### 5.1 采样单元与档位（**v1.1 修正：独立单元是"场"而不是"半场"**）

| 档位 | 场次 | 半场 | 用途 |
|---|---|---|---|
| `smoke` | 1 | 2 | **只验证"能编译、能跑通、不异常"，不参与统计判定** |
| `standard` | 3 | 6 | 主要指标的方向性判断 |
| `confirm` | 6 | 12 | 接近门槛时的确认 |

- ⚠️ **同一场的上下半场不独立**（同对手、同地图、换边互为镜像）⇒ **独立单元 = 场**。
- 需要更大样本时用**配对**（以"场"为配对单位），**绝不当独立样本**直接跑 t 检验。
- 单场内部的相关性由 `calibrate` 估计并写入 `noise.json` 的 `icc`（组内相关系数）；`icc` 越高，有效样本量越小。

### 5.2 主指标与判据（**v1.1 修正：比较 Δ 而不是绝对水平**）

```
主指标来源优先级：
  ① hypothesis.md 里声明的主指标（每轮可以不同：score_2 / score_1 / idle_tower_rounds …）
  ② 未声明时用 acceptance.primary_metric（默认 win_rate）

判据（对"候选 − 基线"的差值 Δ 做区间估计）：
  · Δ 的 (1−α) 区间下界 > max(min_effect, 噪声地板)  ⇒ accept
  · Δ 的区间上界 < −max(min_effect, 噪声地板)        ⇒ revert
  · 区间跨越阈值                                      ⇒ need_more（自动追加，最多到 confirm 档）
  · confirm 档仍跨越                                  ⇒ inconclusive（正常收尾）

统计方法：
  · 主指标是连续量（score_total 等）⇒ 对 Δ 做 bootstrap 区间 / 配对符号检验
  · 主指标是 0/1（win_rate）⇒ 对**配对**的二值差做精确符号检验
  · 报告里必须打印 MDE（最小可检测效应），让 Agent 知道"这一轮能不能测出这个改动"
```

⚠️ **v1.0 的三处统计错误（已修）**：
1. 把半场当独立样本 ⇒ 高估显著性；**现以"场"为单位，并报告 ICC**。
2. 判据写成"区间下界 > 噪声地板"——比较的是**绝对水平**而非 Δ，n=6 时"赢 2/6"的 Wilson 下界就已超过地板，等价于"赢 2 场就 accept"；**现改为对 Δ 的区间**。
3. **"指标持平但方差下降就 accept"已删除**：它会把"每场以同样方式输"（方差 0）判成"更稳"，反向激励更差策略。

### 5.3 统计功效的现实约束（**必须承认**）

| 事实 | 后果 | 对策 |
|---|---|---|
| n=3 场（3 个独立样本） | **win_rate 的 MDE 高达 ~40~58 个百分点**，检出 10pp 需约 100 场 | **不要用 win_rate 做小样本判据**；改用**低方差的连续过程指标**（如 `idle_tower_rounds`、`overkill`、`task_avg_rounds`）作为"机制是否生效"的证据，把终局指标留给大样本 |
| alpha=0.1 且每天 30 轮 × 多指标 | 每天约 3 次假 accept，且 accept 后永久固化 | 每轮只判**一个**主指标；secondary 只做描述不做判定；`need_more` 的"偷看"用**序贯**阈值（每多看一次，α 收紧） |
| 对手可能变化 | "变好了"可能是对手变弱 | 固定 `opponent_id` + 交替 A/B + 记录时间戳与对手指纹；跨对手结果只做参考 |
| 不支持固定 seed | 无法配对 | `use_paired_when_seeded=false`，退化为独立样本并**放大 MDE 报告** |

### 5.4 决策表

| 条件 | 决策 | 动作 |
|---|---|---|
| `needs_human` 或 `blocked_reason` ∈ {config.*, auth.*, quota.*, budget.*, build.stuck, match.stuck, log.*, platform.empty} | — | 停下，按 `human_action` 汇报 |
| guard 失败 / `no_change` | — | Agent 自己改代码 |
| **本地异常计数 > 0**（`profile=standard`） | `revert` | 回滚到 `loop/best`；实验 profile 例外 |
| 主指标 Δ 显著改善 | `accept` | `git tag loop/best`，更新 scoreboard |
| 主指标 Δ 显著恶化 | `revert` | 回滚并记录 |
| 区间跨越阈值且未到 confirm 档 | `need_more` | **用新的 `batch_index` 追加对战**（幂等键含 batch_index ⇒ 不会命中旧 receipt） |
| confirm 档仍跨越 | `inconclusive` | 保留分支、写回 backlog、**正常收尾** |

### 5.5 首轮基线（bootstrap）

- 第一轮迭代没有 `loop/best`：`iter open` 会把 `baseline` 标为 `null`，`iter analyze` 只产出**描述性 KPI**（不判定），
  `iter close --decision baseline` 把该 build 设为 `loop/best`。**这是唯一允许"无基线直接 accept"的情形。**

### 5.6 每轮产物

```
runs/<iterId>/
  iter.json              hypothesis.md         diff.patch            guard.json
  artifact/{build.tar.gz, .sha256, manifest.json}
  platform/{upload.json, build_status.jsonl, build_log.txt, matches.json, <matchId>/status.jsonl}
  logs/<matchId>/{<run>.jsonl, <run>.anchor.json, decoded/}
  kpi.json               analysis/{attribution.json, report.md}
  decision.json
```

---

## 6. 分析与归因

### 6.1 三层分析

1. **合规层**：**本地异常计数**（不认 `errors[]`）、`errors[]` 分布（仅作参考）、guard 拦截率、决策 p50/p99/max、兜底触发次数、日志完整性。
2. **KPI 层**：`score_1/2/3` 分项、击杀数按类型、金币曲线、任务完成率/平均耗时、墙/塔存活、基地掉血速率、**每回合有效开火率**、`idle_tower_rounds`、溢出伤害占比。
3. **归因层**：
   - **后悔值（regret）**：`decision` 的 `cands[].ev` 与下回合 `outcome` 的差值，按模块聚合；
   - **反事实对比**：与基线在同一局面（回合 + 局面摘要哈希）上的决策差异；
   - **丢分清单**：按"理论上限 − 实际"排序的 top-10 → 下一轮假设的来源。

### 6.2 报告结构（模板见 `templates/iteration-report.md`）

结论 / 合规检查 / KPI 对比 / **统计证据（含 MDE）** / 归因 Top-5（带日志事件 id）/ **未解释现象** / 下一轮建议假设。
**纪律：报告里每条结论都必须带可追溯的日志事件 id 或 KPI 字段名；没有证据的结论不允许进报告。**

### 6.3 假设回写（v1.1 新增，否则 Phase 3"自主优化"不可达）

`report.md` §7 的 3 条建议由 `loopctl backlog add --from-report <iter>` 结构化写回 `backlog.json`；
**这是唯一能让 backlog 自我补充的入口**，没有它 backlog 一空就"正常结束"。

---

## 7. 护栏

| 类别 | 规则 | 强制点 |
|---|---|---|
| 路径 | 受保护路径见 §2；`bootstrap_phase` 期间允许创建 `loop/**`、`tests/**`、`run.sh` | `guard.protected_paths` |
| 密钥 | 每次 `iter.commit` 前跑 `guard scan-secrets`：**禁止提交 64 位 hex、`secrets/`、`_fwsecret*`、`*.fwl.jsonl`** | `guard.checks` |
| Git | 白名单式：只允许 `add/commit/tag/checkout/branch/diff/log/status`；**其它 git 子命令一律禁止**（v1.0 的字符串黑名单挡不住 `amend`/`reset --hard HEAD~1`/`force-with-lease`） | `guard.git_allowlist` |
| 变更规模 | diff ≤ `max_diff_lines`（默认 400）；超出需 `--reason "<理由>"` 并单独成轮 | `guard.diff_budget` |
| 单变量 | 一轮一个 `hypothesis_id`；跨模块改动需 `--multi-module` | `state.py` |
| 额度 | 上传/对战/迭代/时长上限（读 `team.info.quota` 的真实值优先） | `platform.limits` |
| **人工闸门** | `require_approval: ["upload"]`（默认）；`approve --step upload --artifact-sha <sha>` **绑定到产物哈希** ⇒ 改码换包后审批自动失效 | `loopctl approve` |
| 失败熔断 | 同一假设连续失败 3 次 ⇒ `backlog.quarantine` | `state.py` |
| 实验隔离 | `--profile experiment`：允许刻意违规、**不进 scoreboard、不参与判定**、日志标 `profile=experiment` | `state.py` |

> ⚠️ **诚实声明**：以上都是**防误操作**护栏。Agent 与 loopctl 同权限，理论上可绕过。
> 需要真正的强制，应把凭据放到 Agent 无法读取的位置（另一个用户/容器），只暴露 `loopctl`。

---

## 8. Driver Agent 的使用方式

```bash
# 0) 首次：填配置 → 自检（含日志通道探针）
$EDITOR loop/config/loop.config.json
python3 loop/loopctl.py doctor --json --probe      # unfilled / missing_files / reachable / logs_probe

# 0.1) 平台接口未定时：用 fixture 离线跑通（Phase 0 验收）
python3 loop/loopctl.py doctor --json --adapter fixture
python3 loop/loopctl.py run --json --adapter fixture

# 0.2) 首次接通平台后：测噪声地板与 ICC
python3 loop/loopctl.py calibrate --matches 3 --json

# 1) 取假设 → 2) 开迭代 → 3) 改代码 → 4) 一键跑完
python3 loop/loopctl.py backlog next --json
python3 loop/loopctl.py iter open --hypothesis-file /tmp/hyp.md --json
#   … Agent 改代码 …
python3 loop/loopctl.py run --json

# 5) 读结论
cat runs/i0007/analysis/report.md
python3 loop/loopctl.py board --last 5 --json
```

---

## 9. 落地阶段与验收标准

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| **Phase 0 · 可离线跑通**（最高优先） | `loopctl` 骨架 + 状态机 + fixture 适配器 + guard + 打包 + 日志解密 + 报表 + `tests/` + `run.sh` + **`.gitignore` 补齐** | ① `loopctl run --adapter fixture` 在**没有平台**时走完全部状态并产出 `report.md`/`kpi.json`/`decision.json`；② `doctor --json` 的 `missing_files` 为空；③ `.gitignore` 覆盖 secrets/`_fwsecret`/runs/loop-state |
| **Phase 1 · 接通平台** | 填 endpoints → `doctor --probe` 全绿（**含 logs_probe 真取到 ≥1 帧**）→ 一次真实 upload/build/battle/取日志 | 一次真实迭代端到端成功 |
| **Phase 2 · 可自动判定** | `calibrate`（含 ICC）+ Δ 判据 + 自动 revert + 审批绑定 | 连续 3 轮无需人工干预给出 accept/revert |
| **Phase 3 · 可自我优化** | 归因报告 → `backlog add --from-report` 自动补充 → 无人值守跑批 | 一夜（≥8 轮）后 scoreboard 主指标单调改善 |

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 平台接口形态未知 | 无法开工 | fixture 适配器 + `doctor` 占位清单，先把 L2/L3 做完 |
| **业务码被当成 HTTP 状态** | 空数据照推状态机、烧额度 | §4.2 的 `success_when` + fail-closed |
| **包身份与幂等** | 用旧 artifact 打分、重复上传 | `artifact_sha256` 进幂等键 + 两阶段 receipt + `build.list` 对账 |
| **need_more 死循环** | 样本 0 增量、无限追加 | 幂等键含 `batch_index`；`analyze` 的 `inputs_digest` 含样本摘要 |
| 平台限流/风控 | 迭代停摆 | 退避 + `Retry-After` + 每日额度 + 只允许 loopctl 发请求 |
| 对战噪声大 | 误判"变好了" | 以"场"为单位 + Δ 判据 + MDE + 固定对手 + 交替 A/B |
| 编译/对战卡死 | 浪费时间预算 | stuck 检测 + 超时 + `match.cancel` + 现场保全 |
| 密钥泄漏 | 日志被对手读 | `.gitignore` + 打包白名单 + 产物扫描 + 每 build 轮换（见 `03` §6） |
| **日志回流通道缺失** | 第 7 环没有输入 | §4.4 三选一，列入 Phase 0/1 验收 |
| Agent 自嗨式总结 | 优化跑偏 | 报告强制"证据可追溯" |
| 一次性大改动 | 无法归因 | diff 预算 + 单假设 + 自动 revert |

---

## 11. 与其它文档的接口

- `01-游戏目标与策略分析.md` §7 → `backlog.json` 初始内容；§8 的 Q1~Q22 → `experiment run` 清单（按 §8.4 的调度表执行，**注意异常额度预算**）。
- `03-日志格式与加密设计.md` → §6 的分析输入格式、`fetch-logs` 的解密与 `verify --anchor` 流程、§4.4 的日志通道。
- `04-执行Agent操作手册.md` → 给 Driver Agent 的逐条命令手册（可直接作为系统提示词）。
- `templates/endpoints.reserved.json` → §4 的可复制骨架。
