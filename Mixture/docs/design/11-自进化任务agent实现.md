# 沙盒探索 Agent：上下文工程、执行循环与经验沉淀设计

## 1. 目标

本文档用于指导实现一个能够在**未知沙盒环境**中自主探索并完成任务的
Agent。

典型初始任务可能非常简单：

> 读取 `abc.md`

但 `abc.md` 中可能包含真正的任务，例如：

> 检查当前项目，运行测试，定位失败原因并修复。

因此 Agent 应持续执行：

**任务理解 → LLM 决策 → 命令执行 → 观察结果 → 再决策 → 验证 → 最终回答**

同时，系统需要从已经完成的相似任务中沉淀经验，使未来任务能够更快解决。

------------------------------------------------------------------------

## 2. 推荐总体架构

``` text
初始任务
   ↓
检索相关历史经验
   ↓
构造当前上下文
   ↓
调用 LLM
   ↓
LLM 返回下一步 Action
   ↓
command → Controller 校验 → Sandbox 执行
final   → 输出最终答案
   ↓
Observation
   ↓
更新 Agent State
   ↓
继续下一轮
   ↓
任务完成
   ↓
LLM 提炼可复用经验
   ↓
Agent 程序保存 / 去重 / 索引经验
```

### 职责划分

  Agent 程序                LLM
  ------------------------- ---------------------------
  保存原始任务              理解任务
  构造上下文                判断下一步动作
  执行命令                  根据 Observation 更新判断
  控制 timeout 和输出大小   判断何时结束
  检测重复/循环             给出最终答案
  检索历史经验              使用历史经验辅助当前任务
  保存、去重、更新经验      从完成轨迹中提炼经验

核心原则：

> **LLM 是经验内容的作者；Agent 程序是经验系统的管理员。**

------------------------------------------------------------------------

## 3. Agent 主循环

最小可用循环：

``` text
Task
 ↓
Build Context
 ↓
LLM
 ↓
Action
 ↓
Sandbox
 ↓
Observation
 ↓
Build Context
 ↓
LLM
 ↓
...
 ↓
Final
```

每轮只要求 LLM 回答：

> 基于当前任务和已有证据，下一条最有价值的命令是什么？如果任务已经完成，则给出最终答案。

LLM 不直接控制 Shell，而只是提出动作。真正执行由 Controller 负责。

``` text
LLM
 ↓
Action Proposal
 ↓
Controller
 ├── 格式校验
 ├── 权限/安全检查
 ├── timeout
 ├── 重复动作检测
 └── 执行
 ↓
Sandbox
 ↓
Observation
```

------------------------------------------------------------------------

## 4. 中文 System Prompt

下面的 Prompt 可以直接作为主 Agent 的固定 System Prompt。

``` text
你是一个运行在未知沙盒环境中的自主任务解决 Agent。

你的目标是通过执行命令、观察结果、逐步探索环境，最终完成给定任务。

你无法直接操作环境。你只能根据当前任务、已有信息和命令执行结果，
决定下一步应该执行的动作。外部程序会负责实际执行命令，并将结果返回给你。

## 基本原则

1. 不要假设可以通过命令低成本验证的事实。
2. 每一轮只选择当前最有价值的下一步动作。
3. 优先选择能够最大程度减少当前不确定性的低成本操作。
4. 根据已有观察不断更新判断，不要重复已经确认过的检查。
5. 如果某个假设看起来很可能正确，在条件允许时尝试验证或反证它。
6. 不要为了探索而探索，每个命令都应该服务于当前任务。
7. 尽量使用精确命令，避免产生大量无关输出。
8. 优先使用 grep、find、head、tail、sed 等方式获取目标信息，而不是直接输出大型文件或目录。
9. 命令失败也是有效信息，应根据错误原因调整下一步动作。
10. 当已有证据足以完成任务时，应停止探索并给出最终答案。
11. 如果任务涉及修改、修复、构建或运行，在条件允许时，完成修改后应进行验证。
12. 不要仅因为某个修改“看起来正确”就认为任务已经完成，应尽量通过实际结果验证。
13. 历史经验仅作为参考。当前环境中的实际观察结果具有更高优先级。
14. 如果历史经验与当前环境冲突，应以当前环境的证据为准。

## 关于任务说明

最初收到的任务可能只是一个入口，例如：

“读取 abc.md”

这并不一定意味着读取文件就是最终任务。

如果读取的文件中包含进一步的任务说明、规则、目标或约束，应将其视为当前任务的一部分，并继续完成其中要求的工作。

始终区分：

- 原始任务：最开始收到的指令；
- 当前实际任务：经过探索后已经明确需要完成的目标。

## 决策要求

每一轮首先判断：

1. 当前真正需要完成的任务是什么？
2. 已经确认了哪些事实？
3. 当前最重要的未知问题是什么？
4. 哪个命令能够以最低成本获取最有价值的信息？
5. 是否已经满足完成任务所需的条件？

然后只选择一个动作。

## 输出要求

只能输出规定的 JSON，不要输出 Markdown，不要输出 JSON 之外的解释。

需要执行命令时：

{
  "type": "command",
  "command": "需要执行的命令",
  "purpose": "一句话说明这个命令准备确认什么"
}

认为任务已经完成时：

{
  "type": "final",
  "answer": "最终答案"
}

purpose 只需要描述当前动作的直接目的，不需要展示详细推理过程。
```

不建议要求模型输出完整 Chain-of-Thought。`purpose`
只需要保留简短、可记录的决策依据。

------------------------------------------------------------------------

## 5. 每轮动态上下文模板

### 第一轮

``` text
【原始任务】
读取 abc.md

【当前实际任务】
读取 abc.md，尚未发现进一步任务。

【相关历史经验】
暂无。

【已确认事实】
暂无。

【最近执行记录】
暂无。

请决定下一步动作。
```

LLM：

``` json
{
  "type": "command",
  "command": "cat abc.md",
  "purpose": "读取任务明确指定的文件内容"
}
```

如果 Sandbox 返回：

``` text
请检查当前项目。
项目运行 pytest 时有测试失败，请定位原因并修复。
```

下一轮上下文：

``` text
【原始任务】
读取 abc.md

【当前实际任务】
检查当前项目，运行 pytest，定位测试失败原因并修复。

【相关历史经验】
暂无。

【已确认事实】
1. abc.md 中包含进一步任务。
2. 需要检查当前项目并解决 pytest 测试失败问题。

【最近执行记录】

# 1
命令：
cat abc.md

目的：
读取任务明确指定的文件内容

退出码：
0

标准输出：
请检查当前项目。
项目运行 pytest 时有测试失败，请定位原因并修复。

标准错误：
无

请决定下一步动作。
```

------------------------------------------------------------------------

## 6. LLM 响应格式

第一版建议只允许两种 Action。

### 6.1 command

``` json
{
  "type": "command",
  "command": "pytest -q",
  "purpose": "运行测试并获取具体失败信息"
}
```

字段：

-   `type`：固定为 `command`
-   `command`：准备在沙盒执行的 Shell 命令
-   `purpose`：一句话说明该命令要确认什么

### 6.2 final

``` json
{
  "type": "final",
  "answer": "已经定位到问题，根因是……"
}
```

第一版不建议加入大量 `thought`、`plan`、`confidence`、`hypothesis`
等字段。先保证：

``` text
Task → command → observation → command → observation → verification → final
```

------------------------------------------------------------------------

## 7. Python MVP 示例

``` python
import json
import subprocess
from dataclasses import dataclass, field
from typing import Optional

MAX_OUTPUT = 12000
MAX_STDERR = 4000
MAX_STEPS = 50
COMMAND_TIMEOUT = 30
RECENT_STEPS = 6

@dataclass
class Step:
    command: str
    purpose: str
    exit_code: int
    stdout: str
    stderr: str

@dataclass
class AgentState:
    original_task: str
    effective_task: Optional[str] = None
    facts: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

def trim_output(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half]
        + "\n\n... OUTPUT TRUNCATED ...\n\n"
        + text[-half:]
    )

def run_command(command: str) -> Step:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        return Step(
            command=command,
            purpose="",
            exit_code=result.returncode,
            stdout=trim_output(result.stdout, MAX_OUTPUT),
            stderr=trim_output(result.stderr, MAX_STDERR),
        )
    except subprocess.TimeoutExpired:
        return Step(
            command=command,
            purpose="",
            exit_code=-1,
            stdout="",
            stderr=f"Command timed out after {COMMAND_TIMEOUT}s",
        )

def build_context(state: AgentState, memories: list[str]) -> str:
    memory_text = "\n".join(f"- {m}" for m in memories) or "暂无"
    fact_text = "\n".join(f"- {f}" for f in state.facts) or "暂无"
    history_parts = []

    for i, step in enumerate(state.steps[-RECENT_STEPS:], 1):
        history_parts.append(f"""
# 执行记录 {i}
命令：{step.command}
目的：{step.purpose}
退出码：{step.exit_code}
标准输出：
{step.stdout or "无"}
标准错误：
{step.stderr or "无"}
""")

    history_text = "\n".join(history_parts) or "暂无"
    effective_task = state.effective_task or state.original_task

    return f"""
【原始任务】
{state.original_task}

【当前实际任务】
{effective_task}

【相关历史经验】
{memory_text}

【已确认事实】
{fact_text}

【最近执行记录】
{history_text}

请根据以上信息决定下一步动作。
"""

def parse_action(raw: str) -> dict:
    action = json.loads(raw)
    action_type = action.get("type")

    if action_type not in ("command", "final"):
        raise ValueError(f"Unknown action type: {action_type}")

    if action_type == "command":
        if not action.get("command"):
            raise ValueError("Missing command")
        action.setdefault("purpose", "")

    if action_type == "final" and not action.get("answer"):
        raise ValueError("Missing final answer")

    return action

def call_llm(system_prompt: str, user_prompt: str) -> str:
    # 替换为实际 LLM API 调用。
    raise NotImplementedError

def retrieve_memories(task: str) -> list[str]:
    # 第一版可返回 []，后续接 SQLite / embedding。
    return []

def run_agent(task: str, system_prompt: str):
    state = AgentState(original_task=task)
    memories = retrieve_memories(task)

    for _ in range(MAX_STEPS):
        context = build_context(state, memories)
        raw = call_llm(system_prompt, context)
        action = parse_action(raw)

        if action["type"] == "final":
            return action["answer"], state

        result = run_command(action["command"])
        result.purpose = action["purpose"]
        state.steps.append(result)

    raise RuntimeError(f"Agent exceeded {MAX_STEPS} steps")
```

实际系统还应增加危险命令限制、权限控制、重复命令检测、总执行时间限制、总
Token/成本限制和真正的沙盒隔离。

------------------------------------------------------------------------

## 8. Agent State 维护

第一版可以从以下结构开始：

``` python
state = {
    "original_task": "...",
    "effective_task": "...",
    "facts": [],
    "recent_steps": []
}
```

-   `original_task`：永远保存最初任务。
-   `effective_task`：探索后明确的实际任务。
-   `facts`：已经确认、未来仍有价值的事实。
-   `recent_steps`：最近若干轮原始命令及结果。

第一版甚至可以暂时不维护复杂 Facts，只保留原始任务、历史经验和最近 5～8
次 Observation。等真实任务证明有必要后，再让 LLM 返回结构化
`state_update`。

------------------------------------------------------------------------

## 9. 经验由谁沉淀？

推荐：

> **LLM 负责提炼经验，Agent
> 程序负责保存、检索、去重、更新和生命周期管理。**

LLM
擅长抽象根因、有效诊断方法、失败原因和通用方法；程序擅长持久化、索引、Top-K、embedding、版本和使用统计。

不要直接把完整 trajectory 当作长期
Memory，而是在任务完成后提取可复用知识。

------------------------------------------------------------------------

## 10. 经验提炼 Prompt

``` text
你需要从一个已经完成的 Agent 任务执行轨迹中提取能够帮助未来类似任务的经验。

不要简单总结完整执行过程，也不要机械记录每一条命令。

只保留未来遇到类似问题时能够提高解决效率的信息。

重点提取：

1. 任务属于什么类型；
2. 有哪些关键现象或诊断信号；
3. 最终确认的根本原因是什么；
4. 哪些检查步骤最有价值；
5. 哪些尝试无效，以及为什么无效；
6. 最终解决方法是什么；
7. 如何验证问题已经解决；
8. 这个经验适用于什么条件；
9. 哪些环境差异可能导致这个经验不适用。

输出 JSON：

{
  "task_type": "...",
  "symptoms": ["..."],
  "root_cause": "...",
  "useful_checks": ["..."],
  "failed_approaches": [
    {
      "approach": "...",
      "reason": "..."
    }
  ],
  "solution": ["..."],
  "verification": ["..."],
  "applicability": "...",
  "keywords": ["..."]
}

如果某一项没有值得沉淀的信息，可以使用空数组或空字符串。
只输出具有复用价值的信息，不要记录与未来任务无关的临时细节。
```

------------------------------------------------------------------------

## 11. 经验示例

``` json
{
  "task_type": "python_test_failure",
  "symptoms": [
    "token expiry 测试预期 401，但实际得到 200"
  ],
  "root_cause": "过期时间使用毫秒，而验证代码按照秒进行比较",
  "useful_checks": [
    "首先运行失败测试获得断言信息",
    "单独执行失败测试缩小范围",
    "搜索 expiry 字段的生成和比较位置",
    "比较测试输入与实现中的时间单位"
  ],
  "failed_approaches": [],
  "solution": [
    "统一 token expiry 的时间戳单位"
  ],
  "verification": [
    "重新执行目标失败测试",
    "运行完整 pytest"
  ],
  "applicability": "适用于涉及 token expiry、时间戳比较和认证测试失败的问题",
  "keywords": [
    "pytest",
    "token",
    "expiry",
    "timestamp",
    "authentication"
  ]
}
```

未来遇到相似任务时，只检索最相关的 2～5
条经验，不需要重新发送完整历史轨迹。

------------------------------------------------------------------------

## 12. Memory 存储

第一版不需要直接使用 Vector DB。SQLite 足够：

``` sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT,
    content TEXT NOT NULL,
    keywords TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

早期可使用 `task_type`、`keywords`、SQLite `LIKE` 或 FTS 检索。

经验增加后再升级：

``` text
新任务
  ↓
embedding
  ↓
向量相似度检索
  ↓
Top 3～5 Memories
  ↓
加入当前 Prompt
```

------------------------------------------------------------------------

## 13. 是否需要压缩上下文？

假设：

-   任务说明：1000～2000 中文字符
-   System Prompt：约 1000～2000 字符
-   历史经验：约 1000～3000 字符
-   最近 5～8 个命令结果
-   每个命令结果经过长度限制

对于这种规模，第一版通常**没有必要做复杂自动压缩**。

过早摘要可能丢失关键错误信息。因此优先控制：

> **Observation 大小，而不是急于压缩任务上下文。**

真正容易导致 Context 爆炸的是：

``` bash
cat huge.log
find /
npm install
pytest -vv
kubectl logs ...
```

------------------------------------------------------------------------

## 14. Observation 输出控制

建议 Controller 初始设置：

``` python
MAX_STDOUT = 12000
MAX_STDERR = 4000
COMMAND_TIMEOUT = 30
RECENT_STEPS = 6
```

超长输出保留头尾：

``` python
def trim_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    half = limit // 2

    return (
        text[:half]
        + "\n... OUTPUT TRUNCATED ...\n"
        + text[-half:]
    )
```

同时要求 LLM 优先使用 `grep`、带过滤条件的
`find`、`head`、`tail`、`sed -n` 等精准命令。

------------------------------------------------------------------------

## 15. 上下文保留策略

推荐第一版：

``` text
System Prompt             永久保留
Original Task             永久保留
Effective Task            永久保留
Relevant Memory           Top 2～5
Facts                     已确认的重要事实
Recent Steps              最近 5～8 步
```

不要根据"运行多少轮"决定压缩，而应根据：

-   当前 Token 数
-   Observation 大小
-   信息密度
-   模型 Context Window
-   成本要求

决定。

示例策略：

``` text
Context < 30k tokens
    → 不压缩

30k～60k
    → 淘汰较老的原始 Observation，只保留重要 Facts

更大
    → 对旧轨迹进行一次结构化摘要
```

具体阈值应根据实际模型的 Context Window 和成本调整。

------------------------------------------------------------------------

## 16. 长任务滚动窗口

长任务可以演进为：

``` text
                   Context
                      │
        ┌─────────────┼─────────────┐
        ↓             ↓             ↓
     固定信息       长期状态       短期原始信息

 System Prompt       Facts         最近5～8轮
 Original Task       Findings      stdout/stderr
 Effective Task      Summary
 Relevant Memory
```

旧步骤逐渐转化为 Summary/Facts，最近步骤继续保留原始输出，即：

> **滚动原始窗口 + 长期结构化摘要**

但建议在真实任务证明有必要以后再加入。

------------------------------------------------------------------------

## 17. 重复和无进展检测

Controller 可以维护：

``` text
recent_commands
same_command_count
failed_command_count
last_progress_step
```

如果连续多步没有新信息，可以：

1.  向 LLM 提示当前探索可能陷入循环；
2.  要求重新评估假设和探索方向；
3.  仍无法推进时终止并报告已有证据。

------------------------------------------------------------------------

## 18. 第一版不建议加入

暂时不需要：

-   多 Agent
-   独立 Planner Agent
-   Critic Agent
-   Reflector Agent
-   知识图谱
-   每轮自动摘要
-   复杂 Vector DB
-   大量 confidence score
-   完整 Chain-of-Thought 持久化

先根据真实失败数据判断瓶颈。

------------------------------------------------------------------------

## 19. 推荐项目结构

``` text
agent_project/
│
├── agent.py
│   └── 主循环、Agent State
│
├── llm.py
│   └── LLM API 调用、JSON 响应解析
│
├── sandbox.py
│   └── Shell 执行、timeout、输出限制、安全策略
│
├── context.py
│   └── Prompt / Context 构造
│
├── memory.py
│   └── SQLite、经验保存和检索
│
├── prompts/
│   ├── agent_system.md
│   └── experience_extraction.md
│
└── data/
    └── memory.db
```

------------------------------------------------------------------------

## 20. 推荐 MVP 实现顺序

### Phase 1：跑通自主探索

``` text
Task
→ LLM
→ command
→ Sandbox
→ Observation
→ LLM
→ ...
→ final
```

重点验证：

-   能否正确读取任务入口；
-   能否根据结果选择下一步；
-   能否避免明显重复；
-   能否正确判断完成；
-   修改后是否主动验证。

### Phase 2：加入 Experience Memory

``` text
Trajectory
→ Experience Extraction Prompt
→ Structured Experience
→ SQLite
```

新任务：

``` text
Task
→ Retrieve Related Experiences
→ Prompt
```

### Phase 3：加入结构化 State

逐渐加入：

``` text
effective_task
facts
open_questions
important_findings
```

并减少旧 Observation 对 Context 的占用。

### Phase 4：根据真实任务优化

收集失败任务并分类：

``` text
任务理解错误？
探索策略错误？
命令执行错误？
上下文丢失？
历史经验误导？
过早 final？
陷入循环？
验证不足？
```

再决定是否需要 Planner、Reflection、Critic、Vector Search 或更复杂的
Context Compression。

------------------------------------------------------------------------

## 21. 最终推荐

对于任务说明通常只有 1000～2000 中文字符的场景，可以从以下简单架构开始：

``` text
固定 System Prompt
        +
原始 / 当前任务
        +
Top-K 历史经验
        +
重要 Facts
        +
最近 5～8 轮原始 Observation
        ↓
       LLM
        ↓
 command / final
```

任务结束后：

``` text
完整任务轨迹
      ↓
LLM 提炼经验
      ↓
Agent 程序负责
保存 / 去重 / 检索 / 更新
```

最重要的三个原则：

1.  **LLM 决定下一步，程序掌握执行权。**
2.  **LLM 提炼经验，程序管理经验。**
3.  **优先控制命令回显大小，不要过早压缩上下文。**

先让单 Agent 的"探索 → 观察 → 再探索 → 验证 → 完成 →
经验沉淀"闭环稳定运行，再根据真实失败案例逐步增加复杂度。
