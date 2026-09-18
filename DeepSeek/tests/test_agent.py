"""End-to-end regression: a full local match driven by the local referee.

The referee is an *approximation* (see ``lab/referee.py`` for the list of things
it invents), so the scores here are not evidence about the official game.  What
this file does assert is the set of hard invariants that protect the team from
the five-exception disqualification:

* zero malformed envelopes,
* zero illegal commands,
* zero crashes,
* bounded response latency,
* the agent actually plays (it builds, mines and completes tasks).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent.config import Config                      # noqa: E402
from lab.referee import TOWERS, Referee              # noqa: E402
from lab.run_match import Agent, run                 # noqa: E402


def _simple_match(seed: int = 1, rounds: int = 400) -> dict:
    return run(seed=seed, rounds=rounds, cfg=Config())


class TestHardInvariants:
    def test_no_exceptions_and_no_illegal_commands(self):
        summary = _simple_match(seed=5, rounds=400)
        for side in ("challenger", "defender"):
            data = summary[side]
            assert data["exceptions"] == 0, (side, data)
            assert data["illegal_commands"] == 0, (side, data["sample_rejections"])
            assert data["crashes"] == 0, side

    def test_response_latency_stays_inside_the_budget(self):
        summary = _simple_match(seed=6, rounds=400)
        for side in ("challenger", "defender"):
            latency = summary[side]["latency"]
            # 5 s is the official cap; keep a wide margin so CI noise is safe
            assert latency["max_ms"] < 1500, (side, latency)
            assert latency["p99_ms"] < 200, (side, latency)

    def test_the_agent_actually_plays(self):
        summary = _simple_match(seed=8, rounds=400)
        challenger = summary["challenger"]
        assert challenger["commands"] > 300, challenger
        assert challenger["tasks_completed"] >= 1, challenger
        assert challenger["gold"] > 75, "the economy must produce income"

    def test_both_bases_survive_the_first_three_days(self):
        summary = _simple_match(seed=9, rounds=390)
        assert summary["challenger"]["base_alive"]
        assert summary["defender"]["base_alive"]


class TestRefereeContract:
    """Guards against the referee itself becoming the source of false failures."""

    def test_no_stale_illegal_collect_from_shared_state(self):
        """Both sides are validated against their own observation.

        Before this was enforced, whichever side settled second was judged
        against mines the first side had already consumed, manufacturing illegal
        commands that cannot happen online.
        """
        summary = _simple_match(seed=2, rounds=300)
        for side in ("challenger", "defender"):
            kinds = summary[side].get("rejection_kinds") or {}
            assert "illegal:collect target not a mine" not in kinds, (side, kinds)

    def test_buildings_are_removed_when_destroyed(self):
        referee = Referee(seed=3)
        team = referee.teams["challenger"]
        team.roles.append({"id": 10020, "pos": {"x": 9, "y": 22},
                           "roleType": "gatling", "health": 1, "level": 1,
                           "attackPower": 10, "attackRange": 3,
                           "backPackCapability": 0, "backpack": []})
        for unit in team.roles:
            if unit["roleType"] == "gatling":
                unit["health"] = 0
        referee._end_of_round()
        assert not any(r["roleType"] in TOWERS and r["health"] <= 0
                       for r in team.roles), \
            "a destroyed weapon must leave the board so it can be rebuilt"


class TestDeterminism:
    def test_same_seed_same_result(self):
        first = _simple_match(seed=4, rounds=200)
        second = _simple_match(seed=4, rounds=200)
        for side in ("challenger", "defender"):
            assert first[side]["score"] == second[side]["score"]
            assert first[side]["commands"] == second[side]["commands"]


class TestAgentLatencyBudget:
    def test_full_match_turn_budget_respected(self):
        """Worst single turn across a truncated match stays well under 5 s."""
        agent = Agent(Config(), "probe")
        referee = Referee(seed=12)
        worst = 0.0
        while not referee.over and referee.round <= 200:
            for side in list(referee.teams):
                started = time.perf_counter()
                agent(referee.observation(side))
                worst = max(worst, (time.perf_counter() - started) * 1000)
            referee.step({"challenger": agent, "defender": agent})
        assert worst < 1500, f"worst turn {worst:.1f} ms"


if __name__ == "__main__":
    from run_tests import run_module

    raise SystemExit(run_module(sys.modules[__name__]))
