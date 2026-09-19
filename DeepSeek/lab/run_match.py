"""Run one or more full local matches and report hard + soft metrics.

Hard metrics (must hold for every run):
  * ``exceptions == 0``      -- no malformed envelope, no illegal command
  * ``max_decide_ms`` bounded -- the judge allows 5 s per response
  * the process never raises

Soft metrics (regression only, never evidence about the official game):
  scores, build order, tasks completed, base survival.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# The per-round journal is on by default for the competition platform; a local
# 1300-round batch does not want it.  ``DS_AGENT_LOG=<path>`` still works.
os.environ.setdefault("DS_AGENT_LOG", "off")

from agent.brain import make_handler           # noqa: E402
from agent.config import Config               # noqa: E402
from lab.referee import Referee               # noqa: E402


class Agent:
    """Wraps one brain handler and records per-turn latency."""

    def __init__(self, cfg: Config, name: str):
        self.name = name
        self.handle = make_handler(cfg)
        self.latencies: list[float] = []
        self.crash = 0

    def __call__(self, observation: dict) -> dict:
        started = time.perf_counter()
        try:
            response = self.handle(observation)
        except Exception:
            self.crash += 1
            raise
        finally:
            self.latencies.append((time.perf_counter() - started) * 1000)
        return response

    def stats(self) -> dict:
        if not self.latencies:
            return {"max_ms": 0.0, "p99_ms": 0.0, "mean_ms": 0.0}
        ordered = sorted(self.latencies)
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        return {
            "max_ms": round(max(ordered), 2),
            "p99_ms": round(p99, 2),
            "mean_ms": round(statistics.fmean(ordered), 3),
        }


def run(seed: int = 1, rounds: int = 260, verbose: bool = False,
        cfg: Config | None = None) -> dict:
    cfg = cfg or Config()
    referee = Referee(seed=seed)
    agents = {
        "challenger": Agent(cfg, "challenger"),
        "defender": Agent(cfg, "defender"),
    }
    while not referee.over and referee.round <= rounds:
        referee.step(agents)
        if verbose and referee.round % 50 == 0:
            print(f"  round {referee.round} day {referee.day} "
                  f"{'day' if referee.is_day else 'night'} "
                  f"robots={len(referee.robots)}")
    summary = referee.finalize()
    for side, agent in agents.items():
        summary[side]["latency"] = agent.stats()
        summary[side]["crashes"] = agent.crash
    summary["seed"] = seed
    summary["rounds_played"] = referee.round - 1
    return summary


def main() -> int:
    argv = list(sys.argv[1:])
    rounds = 1300
    if "--rounds" in argv:
        index = argv.index("--rounds")
        try:
            rounds = int(argv[index + 1])
        except (IndexError, ValueError):
            print("--rounds needs an integer")
            return 2
        del argv[index:index + 2]
    seeds = [int(a) for a in argv if a.isdigit()] or [1, 2, 3]
    failures = 0
    report = []
    for seed in seeds:
        started = time.perf_counter()
        summary = run(seed=seed, rounds=rounds)
        elapsed = time.perf_counter() - started
        report.append(summary)
        line = [f"seed={seed} rounds={summary['rounds_played']} ({elapsed:.1f}s)"]
        for side in ("challenger", "defender"):
            data = summary[side]
            line.append(
                f"  {side}: score={data['score']} task={data['task_score']} "
                f"gold={data['gold']} tasks={data['tasks_completed']} "
                f"base={'alive' if data['base_alive'] else 'DEAD'} "
                f"exceptions={data['exceptions']} illegal={data['illegal_commands']} "
                f"failed={data['failed_commands']} "
                f"max_ms={data['latency']['max_ms']}"
            )
            if data["exceptions"] or data["illegal_commands"] or data["crashes"]:
                failures += 1
            if data["sample_rejections"]:
                line.append(f"    rejections={data['sample_rejections'][:5]}")
        print("\n".join(line))

    out = ROOT / "lab" / "artifacts" / "match-summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\nhard-metric failures: {failures}")
    print(f"summary written to {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
