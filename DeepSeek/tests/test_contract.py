"""Contract tests: protocol shape, guard legality, session idempotence, replay.

These are the tests that protect the hard metrics.  A failure here means the
agent could burn one of the five allowed exceptions, so they are written against
the *documented* protocol rather than against the implementation.
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent import guard, protocol, rules as R          # noqa: E402
from agent.config import DEFAULT, Config               # noqa: E402
from agent.engine import compute                       # noqa: E402
from agent.model import Pos, Unit, parse               # noqa: E402
from agent.state import Session, SessionState          # noqa: E402
from agent.tasks.memory import extract_answer, shape_key   # noqa: E402
from agent.tasks.workflow import parse_cmd_result      # noqa: E402
from agent.world import WorldView                      # noqa: E402

#: The official sample request, found relative to this repo so the suite keeps
#: working wherever the checkout is placed.  Tests fall back to a synthetic
#: payload when it is absent, so the suite never depends on the workspace layout.
SAMPLE = next(
    (candidate for candidate in (
        ROOT.parent.parent / "docs" / "request.txt",
        ROOT.parent / "docs" / "request.txt",
        ROOT / "docs" / "request.txt",
    ) if candidate.exists()),
    ROOT / "docs" / "request.txt",
)


def sample_payload() -> dict:
    if SAMPLE.exists():
        return json.loads(SAMPLE.read_text(encoding="utf-8"))
    return _minimal_payload()


def _minimal_payload(round_no: int = 1, gold: int = 75) -> dict:
    return {
        "roundNo": round_no,
        "mapInfo": {"width": 41, "height": 32, "zones": [
            {"neutralType": "challengerTaskPoint1", "pos": {"x": 14, "y": 14}},
            {"neutralType": "challengerTaskPoint2", "pos": {"x": 17, "y": 17}},
            {"neutralType": "vendor", "pos": {"x": 20, "y": 16}},
            {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}},
            {"neutralType": "stone", "pos": {"x": 12, "y": 22}},
        ]},
        "teamOur": {"type": "challenger", "teamId": "t1", "teamName": "t1",
                    "goldNum": gold, "totalScore": 0, "playerTasks": [],
                    "roles": [
                        {"id": 10013, "pos": {"x": 10, "y": 24},
                         "roleType": "station", "health": 1500, "level": 1,
                         "backPackCapability": 0, "backpack": []},
                        {"id": 10010, "pos": {"x": 9, "y": 22},
                         "roleType": "worker", "health": 220, "level": 1,
                         "backPackCapability": 100, "backpack": []},
                        {"id": 10011, "pos": {"x": 11, "y": 22},
                         "roleType": "pioneer", "health": 200, "level": 1,
                         "backPackCapability": 40, "backpack": []},
                        {"id": 10012, "pos": {"x": 10, "y": 22},
                         "roleType": "worker", "health": 220, "level": 1,
                         "backPackCapability": 100, "backpack": []},
                    ]},
        "teamEnemy": {"roles": []},
        "robot": {"roles": []},
        "phaseTask": "", "lastRoundRoleActionResults": {},
        "lastSummonTreasureResult": 0, "llmResp": "",
        "worldNews": {"officialNews": "", "folkLegends": ""},
        "lastCmdResult": "",
        "vendorShopList": [{"name": "stone", "price": 1}],
        "weaponShopList": [{"name": "Medicine", "price": 10}],
        "errors": [],
    }


# ---------------------------------------------------------------------------
# envelope / protocol
# ---------------------------------------------------------------------------
class TestEnvelope:
    def test_always_has_three_typed_top_level_keys(self):
        for commands in ({}, {10010: {"action": "move",
                                      "targetPos": [{"x": 1, "y": 1}]}}):
            out = protocol.envelope(commands)
            assert set(out) == {"roleCommandMap", "prompt", "executeCmd"}
            assert isinstance(out["roleCommandMap"], dict)
            assert isinstance(out["prompt"], str)
            assert isinstance(out["executeCmd"], str)

    def test_keys_are_strings(self):
        out = protocol.envelope({10010: {"action": "acceptTask"}})
        assert list(out["roleCommandMap"]) == ["10010"]

    def test_empty_commands_are_dropped(self):
        out = protocol.envelope({10010: {}})
        assert out["roleCommandMap"] == {}

    def test_engine_returns_a_complete_envelope_on_the_sample(self):
        out = compute(sample_payload(), SessionState(DEFAULT),
                      time.monotonic() + 5)
        assert isinstance(out["roleCommandMap"], dict)
        assert isinstance(out["prompt"], str)
        assert isinstance(out["executeCmd"], str)

    def test_malformed_payloads_still_return_an_envelope(self):
        for payload in ({}, None, [], {"roundNo": "x"},
                        {"roundNo": 3, "mapInfo": None, "teamOur": 5}):
            out = compute(payload, SessionState(DEFAULT), time.monotonic() + 5)
            assert set(out) == {"roleCommandMap", "prompt", "executeCmd"}


class TestParsing:
    def test_round_85_is_day_one_night(self):
        obs = parse(sample_payload())
        assert obs.round_no == 85
        assert obs.phase_round == 84
        assert obs.day_index == 1          # 85 < 130, so still day 1
        assert obs.is_day is False         # and the sample carries robots

    def test_day_night_boundaries(self):
        for round_no, expected in ((1, True), (70, True), (71, False),
                                   (130, False), (131, True), (1300, False)):
            assert parse(_minimal_payload(round_no)).is_day is expected, round_no

    def test_duplicate_task_point_two_is_clustered(self):
        obs = parse(sample_payload())
        mine = obs.my_tasks()
        assert len(mine) == 2, [t.task_type for t in mine]
        two = [t for t in mine if t.index == "2"][0]
        assert len(two.cells) == 2

    def test_enemy_task_points_are_separated(self):
        obs = parse(sample_payload())
        assert len(obs.enemy_tasks()) == 2

    def test_station_footprint_matches_the_sample(self):
        obs = parse(sample_payload())
        world = WorldView(obs)
        primary = world.station_cells()[:4]
        towers = {t.pos for t in obs.towers()}
        assert not (towers & set(primary)), "a tower sits inside the base"
        for tower in towers:
            assert min(max(abs(tower.x - c.x), abs(tower.y - c.y))
                       for c in primary) == 1

    def test_tower_range_is_capped_by_the_documented_table(self):
        obs = parse(sample_payload())
        for tower in obs.towers():
            documented = tower.documented_range()
            if documented is not None:
                assert tower.effective_range() <= documented

    def test_robot_kind_and_flags_are_parsed(self):
        obs = parse(sample_payload())
        kinds = {r.kind for r in obs.robots}
        assert kinds, "sample should carry robots"
        assert kinds <= set(R.ROBOT_STATS)


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------
class TestGuard:
    def setup_method(self):
        # The sample is round 85 = night, and build/collect are day-bound, so
        # build-focused checks run on a daytime observation instead.
        self.obs = parse(_day_payload())
        self.world = WorldView(self.obs, DEFAULT)
        self.worker = self.obs.workers()[0]

    def test_rejects_unknown_action(self):
        ok, why = guard.validate({"action": "teleport"}, self.worker,
                                 self.obs, self.world)
        assert not ok and why == "action:unknown"

    def test_rejects_missing_target(self):
        ok, why = guard.validate({"action": "move"}, self.worker,
                                 self.obs, self.world)
        assert not ok and why == "targetPos:missing"

    def test_rejects_wrong_types(self):
        cases = [
            ({"action": "sell", "name": 5}, "name:missing"),
            ({"action": "buy", "name": "Medicine", "num": "3"}, "num:not_int"),
            ({"action": "move", "targetPos": [{"x": "1", "y": 2}]},
             "targetPos:not_int_pair"),
            ({"action": "submitAnswer", "taskAnswer": 1},
             "taskAnswer:missing"),
        ]
        for command, expected in cases:
            ok, why = guard.validate(command, self.worker, self.obs, self.world)
            assert not ok and why == expected, command

    def test_rejects_attack_during_the_day(self):
        # round 1 is daylight, and 接口文档 §2.3 says attack is night-only
        obs = parse(_day_payload(gold=200, three_towers=True))
        tower = obs.towers()[0]
        # place the controller next to the tower so the *day* rule is what fires
        controller = Unit(unit_id=10010, pos=_adjacent(tower),
                          kind="worker", health=220, level=1, capacity=100,
                          backpack=())
        obs = _with_roles(obs, controller)
        world = WorldView(obs, DEFAULT)
        aim = next(p for p in Pos(tower.pos.x, tower.pos.y).neighbours()
                   if _dist_pos(p, tower) <= tower.effective_range())
        command = protocol.attack(controller.unit_id, (aim,))
        command["__towerId"] = tower.unit_id
        ok, why = guard.validate(command, controller, obs, world)
        assert not ok and why in ("attack:at_day", "attack:phase_edge"), why

    def test_attack_target_count_must_match_level(self):
        obs = parse(sample_payload())
        old_tower = obs.towers()[0]
        tower = Unit(unit_id=old_tower.unit_id, pos=old_tower.pos,
                     kind="rocket", health=1000, level=3, cooldown=0,
                     attack_power=20, attack_range=R.RANGE_SENTINEL,
                     capacity=0, backpack=())
        controller = Unit(unit_id=10010, pos=_adjacent(tower), kind="worker",
                          health=220, level=1, capacity=100, backpack=())
        obs = _with_roles(obs, tower, controller)
        world = WorldView(obs, DEFAULT)
        for count in (1, 2):
            aims = tuple(Pos(tower.pos.x + 1, tower.pos.y + i)
                         for i in range(count))
            command = protocol.attack(controller.unit_id, aims)
            command["__towerId"] = tower.unit_id
            ok, why = guard.validate(command, controller, obs, world)
            assert not ok and why.startswith("attack:want_3"), (count, why)

    def test_duplicate_aim_cells(self):
        """Rocket impacts may overlap; every other weapon must aim distinctly.

        任务书 §4.5.4.4 says 多枚导弹落点重叠时伤害叠加, so stacking a level-2/3
        rocket on one armoured robot is legal and often correct (R2).  A gatling
        bullet has no stacking rule, so its aim cells stay distinct.
        """
        obs = parse(sample_payload())
        old_tower = obs.towers()[0]
        aim = Pos(old_tower.pos.x + 1, old_tower.pos.y)
        controller = Unit(unit_id=10010, pos=_adjacent(old_tower), kind="worker",
                          health=220, level=1, capacity=100, backpack=())
        for kind, expect_ok in (("rocket", True), ("gatling", False)):
            tower = Unit(unit_id=old_tower.unit_id, pos=old_tower.pos,
                         kind=kind, health=1000, level=2, cooldown=0,
                         attack_power=20, attack_range=R.RANGE_SENTINEL,
                         capacity=0, backpack=())
            local = _with_roles(obs, tower, controller)
            world = WorldView(local, DEFAULT)
            command = protocol.attack(controller.unit_id, (aim, aim))
            command["__towerId"] = tower.unit_id
            ok, why = guard.validate(command, controller, local, world)
            if expect_ok:
                assert ok, why
            else:
                assert not ok and why == "attack:duplicate_targets", why

    def test_gatling_cone_is_checked_pairwise(self):
        origin = Pos(10, 10)
        # (1,0) . (0,1) == 0  -> exactly 90 degrees, legal
        assert guard.cone_ok(origin, [Pos(11, 10), Pos(10, 11)])
        # (1,0) . (-1,1) == -1 -> 135 degrees, illegal
        assert not guard.cone_ok(origin, [Pos(11, 10), Pos(9, 11)])
        # a single aim cell is trivially legal
        assert guard.cone_ok(origin, [Pos(11, 10)])

    def test_build_requires_stone_for_wall(self):
        worker = Unit(unit_id=10010, pos=Pos(9, 22), kind="worker",
                      health=220, level=1, capacity=100, backpack=())
        obs = _with_roles(self.obs, worker)
        command = protocol.build(Pos(9, 23), "wall")
        ok, why = guard.validate(command, worker, obs, WorldView(obs, DEFAULT))
        assert not ok and why == "build:no_stone"

    def test_tower_cap_is_enforced(self):
        obs = parse(_day_payload(gold=200, three_towers=True))
        assert len(obs.towers()) == 3
        worker = obs.workers()[0]
        target = next(p for p in Pos(worker.pos.x, worker.pos.y).neighbours()
                      if obs.buildable_terrain(p)
                      and p not in WorldView(obs, DEFAULT).blocked_cells())
        command = protocol.build(target, "gatling")
        ok, why = guard.validate(command, worker, obs, WorldView(obs, DEFAULT))
        assert not ok and why == "build:tower_cap"

    def test_pioneer_only_actions(self):
        worker = self.obs.workers()[0]
        ok, why = guard.validate({"action": "acceptTask"}, worker,
                                 self.obs, self.world)
        assert not ok and why == "acceptTask:pioneer_only"

    def test_worker_only_actions(self):
        pioneer = self.obs.pioneer()
        ok, why = guard.validate({"action": "collect",
                                  "targetPos": [{"x": pioneer.pos.x + 1,
                                                 "y": pioneer.pos.y}]},
                                 pioneer, self.obs, self.world)
        assert not ok and why == "collect:worker_only"


def _with_roles(obs, *replacements):
    """Return a copy of ``obs`` whose roles have been swapped for ``replacements``."""
    by_id = {u.unit_id: u for u in obs.roles}
    for unit in replacements:
        by_id[unit.unit_id] = unit
    import dataclasses
    return dataclasses.replace(obs, roles=tuple(sorted(
        by_id.values(), key=lambda u: u.unit_id)))


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------
class TestSession:
    def test_identical_retry_replays_the_cached_response(self):
        session = Session()
        payload = sample_payload()
        calls = []

        def compute_once(p, s, d):
            calls.append(1)
            return protocol.envelope({10010: move(p)})

        first = session.decide(payload, compute_once)
        second = session.decide(copy.deepcopy(payload), compute_once)
        assert first == second
        assert len(calls) == 1, "a retry must not re-run the strategy"

    def test_same_round_different_payload_is_a_conflict(self):
        session = Session()

        def compute_once(p, s, d):
            return protocol.envelope({10010: move(p)})

        session.decide(sample_payload(), compute_once)
        other = sample_payload()
        other["teamOur"]["goldNum"] += 1
        out = session.decide(other, compute_once)
        assert out["roleCommandMap"] == {}

    def test_earlier_round_is_treated_as_a_new_match(self):
        """A round that goes backwards means a new half/match, so it recomputes.

        What matters is that the match-scoped memory is dropped; the response
        itself may legitimately carry commands for the new half.
        """
        session = Session()
        seen = []

        def compute_once(p, s, d):
            seen.append(p["roundNo"])
            return protocol.envelope({10010: move(p)})

        payload = sample_payload()
        session.decide(payload, compute_once)
        state_before = session.state
        older = sample_payload()
        older["roundNo"] = payload["roundNo"] - 5
        session.state.knowledge.mark_legal(Pos(9, 23), "wall")
        assert session.state.knowledge.legal_sites
        session.decide(older, compute_once)
        assert session.state is state_before
        assert not session.state.knowledge.legal_sites, \
            "learned build sites must not survive into a new half"
        assert len(seen) == 2

    def test_compute_exception_yields_a_valid_envelope(self):
        session = Session()

        def boom(p, s, d):
            raise RuntimeError("nope")

        out = session.decide(sample_payload(), boom)
        assert set(out) == {"roleCommandMap", "prompt", "executeCmd"}

    def test_round_digest_is_stable(self):
        from agent.state import digest
        payload = sample_payload()
        assert digest(payload) == digest(copy.deepcopy(payload))


def move(payload):
    return protocol.move(Pos(1, 1))


def _day_payload(gold: int = 75, three_towers: bool = False) -> dict:
    """A round-1 (day) payload, optionally with the three weapons already up."""
    payload = _minimal_payload(round_no=1, gold=gold)
    payload["teamOur"]["playerTasks"] = [
        {"taskType": "自进化类1", "taskPosition": {"x": 14, "y": 14},
         "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30,
         "isValid": True, "timeoutRounds": 60},
        {"taskType": "自进化类2", "taskPosition": {"x": 17, "y": 17},
         "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30,
         "isValid": True, "timeoutRounds": 60},
    ]
    if three_towers:
        for index, (kind, pos) in enumerate((
                ("gatling", (9, 22)), ("railgun", (10, 22)),
                ("rocket", (11, 22)))):
            payload["teamOur"]["roles"].append({
                "id": 10020 + index * 10, "pos": {"x": pos[0], "y": pos[1]},
                "roleType": kind, "health": 1000, "level": 1,
                "attackPower": 10, "attackRange": 4,
                "backPackCapability": 0, "backpack": [],
            })
    return payload


# ---------------------------------------------------------------------------
# task channel helpers
# ---------------------------------------------------------------------------
class TestTaskChannel:
    def test_parses_each_cmd_result_shape(self):
        assert parse_cmd_result("[exitCode:0]\n42")["status"] == "ok"
        assert parse_cmd_result("[exitCode:0]\n42")["output"].strip() == "42"
        assert parse_cmd_result("[exitCode:2]\nboom")["status"] == "nonzero"
        assert parse_cmd_result("[TIMEOUT]\npartial")["status"] == "timeout"
        assert parse_cmd_result("[JUDGER_ERROR]\nwhy")["status"] == "judger_error"
        assert parse_cmd_result("")["status"] == "empty"

    def test_truncation_marker_is_stripped(self):
        parsed = parse_cmd_result("[exitCode:0]\nvalue\n[TRUNCATED]")
        assert parsed["status"] == "ok"
        assert "TRUNCATED" not in parsed["output"]

    def test_answer_extraction_never_returns_prose(self):
        text = "Sure! Here is the answer:\n\n**42**\n"
        assert extract_answer(text) == "42"

    def test_answer_extraction_falls_back_to_the_longest_line(self):
        assert extract_answer("first\nlast long line here") == "last long line here"

    def test_library_reuses_a_solver_across_instances(self):
        from agent.tasks.memory import SkillLibrary
        library = SkillLibrary()
        first = "给你一个天气API文档\n任务1：请查询北京天气"
        second = "给你一个天气API文档\n任务2：请查询上海天气"
        assert library.lookup(second) is None, "nothing learned yet"
        library.note_success(first, round_no=40)
        learned = library.lookup(second)
        assert learned is not None, "the second instance must reuse the first"

    def test_library_does_not_reuse_across_different_types(self):
        from agent.tasks.memory import SkillLibrary
        library = SkillLibrary()
        library.note_success("查询天气：北京", round_no=40)
        assert library.lookup("查询股票：北京") is None

    def test_shape_key_groups_one_task_type_across_instances(self):
        """The documented 自进化类 pattern: one instruction, changing entity."""
        doc = ("给你一个三方的天气查询API接口文档，让你通过该API查询天气\n"
               "任务1：请查询北京天气")
        assert shape_key(doc) == shape_key(doc.replace("北京", "广州"))

    def test_shape_key_separates_different_instructions(self):
        assert shape_key("查询天气：北京") != shape_key("查询股票：北京")
        assert shape_key("查天气\n任务1：北京") == shape_key("查天气\n任务2：上海")

    def test_numbered_task_marker_is_not_part_of_the_shape(self):
        """A leading "任务1：" enumerator is structure, not content."""
        assert shape_key("任务1：请查询北京天气") == shape_key("请查询北京天气")
        assert shape_key("1. 请查询北京天气") == shape_key("请查询北京天气")


# ---------------------------------------------------------------------------
# replay of the official sample
# ---------------------------------------------------------------------------
class TestReplay:
    def test_sample_replay_produces_only_legal_commands(self):
        payload = sample_payload()
        state = SessionState(DEFAULT)
        out = compute(payload, state, time.monotonic() + 5)
        obs = parse(payload)
        world = WorldView(obs, DEFAULT)
        units = {u.unit_id: u for u in obs.roles}
        for key, command in out["roleCommandMap"].items():
            actor = _actor_for(int(key), command, units)
            assert actor is not None, key
            ok, why = guard.validate(command, actor, obs, world)
            assert ok, (key, why, command)

    def test_sample_replay_sends_no_attack_during_day(self):
        payload = sample_payload()
        payload["roundNo"] = 10              # day 1
        out = compute(payload, SessionState(DEFAULT), time.monotonic() + 5)
        for command in out["roleCommandMap"].values():
            assert command.get("action") != "attack"


def _adjacent(tower):
    """A free cell next to ``tower`` that no other unit occupies."""
    return next(p for p in Pos(tower.pos.x, tower.pos.y).neighbours()
                if (p.x, p.y) != (tower.pos.x, tower.pos.y))


def _dist(unit, tower):
    return max(abs(unit.pos.x - tower.pos.x), abs(unit.pos.y - tower.pos.y))


def _dist_pos(pos, tower):
    return max(abs(pos.x - tower.pos.x), abs(pos.y - tower.pos.y))


def _actor_for(key: int, command: dict, units: dict):
    if command.get("action") == "attack":
        return units.get(int(command["controllerId"]))
    return units.get(key)


# ---------------------------------------------------------------------------
# performance guard
# ---------------------------------------------------------------------------
class TestPerformance:
    def test_decide_is_fast_on_the_sample(self):
        payload = sample_payload()
        state = SessionState(DEFAULT)
        started = time.perf_counter()
        for _ in range(20):
            compute(copy.deepcopy(payload), state, time.monotonic() + 5)
        per_turn = (time.perf_counter() - started) / 20
        assert per_turn < 0.25, f"{per_turn * 1000:.1f} ms per turn"

    def test_many_unreachable_mines_stay_bounded(self):
        payload = _minimal_payload(gold=0)
        payload["mapInfo"]["zones"] = [
            {"neutralType": "stone", "pos": {"x": x, "y": y}}
            for x in range(0, 40, 3) for y in range(0, 30, 3)
        ]
        started = time.perf_counter()
        compute(payload, SessionState(DEFAULT), time.monotonic() + 5)
        assert time.perf_counter() - started < 1.0


if __name__ == "__main__":
    from run_tests import run_module

    raise SystemExit(run_module(sys.modules[__name__]))
