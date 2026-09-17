# 假设卡 <ID>：<一句话标题>

> 由 `loopctl backlog next` 取出后填写；**四项必填，缺一项 `iter open` 会拒绝**。

## 1. 观察（来自哪次分析的哪一条证据）
- 来源：`runs/<iterId>/analysis/report.md` 第 <N> 节 / `scoreboard.csv` 第 <N> 行
- 证据（带日志事件 id 或 KPI 字段名，例如 `combat.idle_tower_rounds = 11.3/晚`）：

## 2. 改动（只改声明过的文件）
- 文件：
- 改法：
- 涉及配置项（写入 `config.py` 的键）：

## 3. 预期指标（必须可度量、必须写数值）
| 指标 | 基线 | 预期 | 判定窗口 |
|---|---|---|---|
| 主指标（`acceptance.primary_metric`） |  |  |  |
| 支撑指标（直接因果） |  |  |  |

## 4. 反例条件（什么情况下判定假设被推翻）
- 若 <指标A> 未改善，或 <指标B> 恶化超过噪声地板，或 异常数 > 0 ⇒ 假设不成立。

## 5. 风险与回滚
- 风险：
- 回滚：`loopctl iter close --decision revert`（自动回到 `loop/best`）

## 6. 关联
- 上游假设/问题：`01-游戏目标与策略分析.md` §7 <P?-?> / §8 <Q?>
- 若本假设需要先做判定实验：`loopctl experiment run --id <Q?>`
