"""Role agents: one module per responsibility, no platform or model calls.

| module | owner | publishes on the blackboard |
| --- | --- | --- |
| `blackboard.py` | shared store | - (the store itself) |
| `defense_agent.py` | defender | `defense`, `defense_plan` |
| `attack_agent.py` | defender (sub-agent) | `attack` |
| `economy_agent.py` | miner | `economy`, `economy_plan` |
| `task_agent.py` | pioneer | `task` |
| `self_evolve.py` | pioneer (sub-agent) | `self_evolve` |
| `review_agent.py` | independent | `review` |

Rules for anything added here:

1. pure functions over `Turn` + session state: no I/O, no timers, no model calls;
2. only the passed `state`/`commands` are mutated, and every payload published to
   the blackboard stays JSON-serializable (it is deep-copied and logged);
3. legality stays in `guard.validate`; agents may only order, score and explain;
4. exceptions are not a control flow: parsing helpers return reasons, never raise.

The global observer (docs/design/12 §3) is intentionally **not implemented yet**:
it will read `blackboard.snapshot(state)` once a version of role-agent summaries
has accumulated real match data.
"""
