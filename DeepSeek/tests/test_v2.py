"""Regression tests for the v2 optimisation round (R1-R5).

Each test names the user requirement it protects, so a future change that
silently reverts a behaviour fails here instead of on the competition platform.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DS_AGENT_LOG", "off")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent import journal                              # noqa: E402
from agent.combat import DamageLedger, aim_for         # noqa: E402
from agent.config import DEFAULT, Config               # noqa: E402
from agent.engine import compute                       # noqa: E402
from agent.model import Pos, Robot, Unit, parse        # noqa: E402
from agent.state import SessionState                   # noqa: E402
from agent import strategy                             # noqa: E402
from agent.world import WorldView                      # noqa: E402

from test_contract import _minimal_payload, sample_payload   # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _payload(round_no: int = 1, **overrides) -> dict:
    payload = _minimal_payload(round_no=round_no)
    payload.update(overrides)
    return payload


def _night_payload(round_no: int = 71, gold: int = 75) -> dict:
    """Round 71 is the first night round (70 daylight rounds per day)."""
    payload = _payload(round_no=round_no)
    payload["teamOur"]["goldNum"] = gold
    return payload


def _world(payload: dict, state=None, cfg: Config = DEFAULT) -> WorldView:
    obs = parse(payload)
    return WorldView(obs, cfg, state.knowledge if state is not None else None)


def _robots(*specs) -> list[dict]:
    out = []
    for index, (kind, x, y) in enumerate(specs, start=1):
        hp = {"smallRobot": 40, "middleRobot": 60, "largeRobot": 500,
              "bossRobot": 800}[kind]
        out.append({"id": 30000 + index, "pos": {"x": x, "y": y},
                    "roleType": kind, "health": hp, "abnormalState": "",
                    "targetTeam": "challenger"})
    return out


def _tower(kind: str, pos: Pos, level: int = 1) -> Unit:
    power = {"gatling": 10, "railgun": 10, "rocket": 20}[kind]
    return Unit(unit_id={"gatling": 10020, "railgun": 10030,
                         "rocket": 10040}[kind], pos=pos, kind=kind,
                health=1000, level=level, cooldown=0, attack_power=power,
                attack_range=2 ** 31 - 1, capacity=0, backpack=())


def _robots_typed(specs) -> tuple:
    return tuple(Robot(robot_id=30000 + i, pos=Pos(x, y), kind=kind,
                       health={"smallRobot": 40, "middleRobot": 60,
                               "largeRobot": 500, "bossRobot": 800}[kind],
                       target_team="challenger")
                 for i, (kind, x, y) in enumerate(specs, start=1))


# ---------------------------------------------------------------------------
# R1 -- per-round journal
# ---------------------------------------------------------------------------
class TestJournal:
    def test_block_layout_and_order(self):
        req = _payload(round_no=85)
        req["worldNews"] = {"officialNews": "官方A", "folkLegends": "传闻B"}
        req["phaseTask"] = "任务C"
        req["llmResp"] = "答案D"
        req["lastCmdResult"] = "[exitCode:0]\nE"
        rsp = {"roleCommandMap": {}, "prompt": "P", "executeCmd": "Q"}
        block = journal.format_turn(req, rsp, {"notes": ["n"]})
        lines = block.splitlines()
        assert lines[0] == "ROUND 85"
        assert lines[1].startswith("req {")
        assert lines[2].startswith("rsp {")
        labels = [line.split(" ", 1)[0] for line in lines[3:10]]
        assert labels == ["官方新闻", "民间传闻", "自进化类任务要求",
                          "req的llm调用结果", "rsp的llm调用prompt",
                          "req的executecmd结果", "rsp的executecmd命令"]
        assert '"官方A"' in lines[3]
        assert '"传闻B"' in lines[4]
        assert '"任务C"' in lines[5]
        assert '"答案D"' in lines[6]
        assert '"P"' in lines[7]
        assert "E" in lines[8]
        assert '"Q"' in lines[9]

    def test_every_item_stays_on_one_line(self):
        req = _payload(round_no=1)
        req["worldNews"] = {"officialNews": "第一行\n第二行", "folkLegends": ""}
        block = journal.format_turn(req, {"prompt": "a\nb"}, {"notes": ["x"]})
        for line in block.splitlines():
            assert "\n" not in line
        assert block.count("\n") == 10   # ROUND + req + rsp + 7 items + diagnostics
        assert block.splitlines()[-1].startswith("diagnostics ")

    def test_off_switch_writes_nothing(self, monkeypatch=None):
        os.environ["DS_AGENT_LOG"] = "off"
        try:
            assert journal.record({"roundNo": 1}, {"prompt": "x"}) is False
        finally:
            os.environ["DS_AGENT_LOG"] = "off"

    def test_engine_records_one_block_per_turn(self):
        path = ROOT / "lab" / "artifacts" / "journal-test.log"
        if path.exists():
            path.unlink()
        os.environ["DS_AGENT_LOG"] = str(path)
        try:
            state = SessionState(DEFAULT)
            compute(_payload(round_no=1), state, time.monotonic() + 5)
            compute(_payload(round_no=2), state, time.monotonic() + 5)
            journal.flush()
            text = path.read_text(encoding="utf-8")
        finally:
            os.environ["DS_AGENT_LOG"] = "off"
            if path.exists():
                path.unlink()
        assert text.count("ROUND ") == 2
        assert "ROUND 1" in text and "ROUND 2" in text
        assert text.count("rsp {") == 2


# ---------------------------------------------------------------------------
# R2 -- base footprint, orientation, layout and targeting
# ---------------------------------------------------------------------------
class TestBaseGeometry:
    def test_footprint_is_the_documented_top_left_corner(self):
        world = _world(_payload())
        cells = set(world.station_cells())
        assert cells == {Pos(10, 24), Pos(11, 24), Pos(10, 23), Pos(11, 23)}

    def test_sample_towers_all_sit_on_footprint_ring_one(self):
        """The real sample's three towers must be legal, or the reading is wrong."""
        world = _world(sample_payload())
        footprint = world.station_cells()
        for tower in parse(sample_payload()).towers():
            gap = min(max(abs(tower.pos.x - c.x), abs(tower.pos.y - c.y))
                      for c in footprint)
            assert gap == 1, (tower.kind, tower.pos, gap)

    def test_front_direction_points_at_the_map_interior(self):
        world = _world(_payload())                 # left-hand base
        assert world.front_direction()[0] == 1
        right = _payload()
        right["teamOur"]["roles"][0]["pos"] = {"x": 30, "y": 8}
        assert _world(right).front_direction()[0] == -1

    def test_tower_sites_share_one_operator_hub(self):
        payload = _payload()
        for role in payload["teamOur"]["roles"]:
            if role["roleType"] in ("worker", "pioneer"):
                role["pos"] = {"x": 4, "y": 12}
        world = _world(payload)
        sites = world.tower_sites()
        assert len(sites) == 3, sites
        hub = world.operator_hub()
        assert hub is not None
        for site in sites:
            assert max(abs(site.x - hub.x), abs(site.y - hub.y)) == 1, site
            gap = min(max(abs(site.x - c.x), abs(site.y - c.y))
                      for c in world.station_cells())
            assert gap == 1, site

    def test_wall_sites_are_ring_two_and_never_the_hub(self):
        world = _world(_payload())
        hub = world.operator_hub()
        sites = world.wall_sites()
        assert sites
        for site in sites:
            assert site != hub
            gap = min(max(abs(site.x - c.x), abs(site.y - c.y))
                      for c in world.station_cells())
            assert gap == 2, (site, gap)


class TestTargetPriority:
    def test_nearest_to_base_is_engaged_first(self):
        tower = _tower("rocket", Pos(9, 22))
        # (12,22) is one cell from the base footprint; (2,22) is eight away.
        robots = _robots_typed([("middleRobot", 2, 22), ("smallRobot", 12, 22)])
        aims = aim_for(tower, robots, DamageLedger(), DEFAULT,
                       (Pos(10, 24), Pos(11, 24), Pos(10, 23), Pos(11, 23)),
                       (1, -1))
        assert aims
        assert max(abs(aims[0].x - 12), abs(aims[0].y - 22)) <= 2, aims

    def test_rocket_shifts_to_the_rear_rank_to_keep_the_blocker(self):
        """R2: aim one cell towards the interior when a strong second rank exists."""
        base = (Pos(10, 24), Pos(11, 24), Pos(10, 23), Pos(11, 23))
        tower = _tower("rocket", Pos(9, 22))
        # front rank small robot, second rank (one cell further inland) a boss
        robots = _robots_typed([("smallRobot", 6, 22), ("bossRobot", 7, 22)])
        aims = aim_for(tower, robots, DamageLedger(), DEFAULT, base, (1, -1))
        assert aims and aims[0].x >= 6, aims
        # ...and the front blocker is only splashed, so it survives to block.
        assert aims[0] != Pos(6, 22)

    def test_rocket_hits_directly_when_the_base_is_being_eaten(self):
        base = (Pos(10, 24), Pos(11, 24), Pos(10, 23), Pos(11, 23))
        tower = _tower("rocket", Pos(9, 22))
        robots = _robots_typed([("smallRobot", 12, 24), ("bossRobot", 13, 24)])
        aims = aim_for(tower, robots, DamageLedger(), DEFAULT, base, (1, -1))
        # footprint distance of (12,24) is 1 -> inside rocket_urgent_radius
        assert aims and aims[0] == Pos(12, 24), aims

    def test_level_two_rocket_may_stack_on_one_cell(self):
        base = (Pos(10, 24), Pos(11, 24), Pos(10, 23), Pos(11, 23))
        tower = _tower("rocket", Pos(9, 22), level=2)
        robots = _robots_typed([("bossRobot", 11, 22)])
        aims = aim_for(tower, robots, DamageLedger(), DEFAULT, base, (1, -1))
        assert aims is not None and len(aims) == 2
        assert aims[0] == aims[1]          # overlapping impacts stack (任务书 §4.5.4.4)


class TestUpgradeOrder:
    def test_front_tower_is_upgraded_first(self):
        payload = _payload()
        payload["teamOur"]["roles"] = [
            payload["teamOur"]["roles"][0],
            {"id": 10040, "pos": {"x": 9, "y": 22}, "roleType": "rocket",
             "health": 1000, "level": 1, "backPackCapability": 0, "backpack": []},
            {"id": 10041, "pos": {"x": 9, "y": 23}, "roleType": "rocket",
             "health": 1000, "level": 1, "backPackCapability": 0, "backpack": []},
        ]
        world = _world(payload)
        towers = sorted(world.obs.towers(),
                        key=lambda t: strategy._upgrade_order(world, t))
        assert towers[0].pos.x >= towers[-1].pos.x   # left base -> front is +x


# ---------------------------------------------------------------------------
# R3 -- night safety
# ---------------------------------------------------------------------------
class TestNightSafety:
    def test_middle_of_the_map_is_not_safe(self):
        world = _world(_payload())
        assert not world.safe_cell(Pos(20, 16))
        assert world.safe_cell(Pos(2, 16))
        assert world.safe_cell(Pos(38, 16))

    def test_robots_make_a_side_cell_unsafe(self):
        payload = _payload()
        payload["robot"] = {"roles": _robots(("smallRobot", 2, 16))}
        world = _world(payload)
        assert not world.safe_cell(Pos(2, 16))
        assert world.safe_cell(Pos(2, 25))

    def test_middle_band_matches_the_safe_rule(self):
        world = _world(_payload())
        band = world.middle_band()
        assert Pos(20, 16) in band
        assert Pos(3, 16) not in band

    def test_night_turn_never_routes_a_worker_through_the_middle(self):
        payload = _night_payload()
        payload["robot"] = {"roles": _robots(("smallRobot", 6, 20))}
        state = SessionState(DEFAULT)
        world = _world(payload, state)
        plan = strategy.TurnPlan()
        plan.avoid = world.middle_band()
        worker = world.obs.workers()[0]
        step = strategy._walk(world, plan, worker, Pos(2, 10), DEFAULT)
        if step is not None:
            assert world.side_depth(step) <= DEFAULT.night_side_limit(41) + 1

    def test_pioneer_abandons_an_unsafe_task_at_night(self):
        payload = _night_payload()
        payload["phaseTask"] = "TASK: 1+1"
        payload["teamOur"]["roles"][2]["pos"] = {"x": 14, "y": 15}
        state = SessionState(DEFAULT)
        out = compute(payload, state, time.monotonic() + 5)
        command = out["roleCommandMap"].get("10011")
        assert command is None or command["action"] == "move"


# ---------------------------------------------------------------------------
# R4 -- mine selection
# ---------------------------------------------------------------------------
class TestMining:
    def _payload_with_mines(self, mines) -> dict:
        payload = _payload()
        payload["mapInfo"]["zones"] = [
            {"neutralType": kind, "pos": {"x": x, "y": y}}
            for kind, x, y in mines
        ] + [{"neutralType": "vendor", "pos": {"x": 20, "y": 16}},
             {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}}]
        payload["teamOur"]["roles"][1]["pos"] = {"x": 9, "y": 22}
        return payload

    def test_nearer_mine_wins_between_equal_prices(self):
        payload = self._payload_with_mines([("copper", 8, 22), ("copper", 30, 5)])
        state = SessionState(DEFAULT)
        world = _world(payload, state)
        worker = world.obs.workers()[0]
        pick = strategy.choose_mine(world, state, worker, DEFAULT, ("copper",))
        assert pick is not None and pick[0] == Pos(8, 22)

    def test_price_can_outweigh_a_slightly_longer_walk(self):
        payload = self._payload_with_mines([("stone", 8, 22), ("copper", 12, 22)])
        state = SessionState(DEFAULT)
        world = _world(payload, state)
        worker = world.obs.workers()[0]
        pick = strategy.choose_mine(world, state, worker, DEFAULT,
                                    ("stone", "copper"))
        assert pick is not None and pick[0] == Pos(12, 22)   # copper is 5x stone

    def test_target_is_locked_across_turns(self):
        """A worker crossing the map keeps mining the ore it committed to."""
        payload = self._payload_with_mines([("copper", 8, 22), ("copper", 2, 2)])
        state = SessionState(DEFAULT)
        world = _world(payload, state)
        worker = world.obs.workers()[0]
        first = strategy.choose_mine(world, state, worker, DEFAULT, ("copper",))
        assert first is not None and first[0] == Pos(8, 22)
        # Same two mines, but the worker is now standing next to the far one: a
        # stateless "nearest mine" rule would switch, the lock must not.
        moved = self._payload_with_mines([("copper", 8, 22), ("copper", 30, 5)])
        moved["teamOur"]["roles"][1]["pos"] = {"x": 31, "y": 5}
        world2 = _world(moved, state)
        worker2 = world2.obs.workers()[0]
        second = strategy.choose_mine(world2, state, worker2, DEFAULT, ("copper",))
        assert second is not None and second[0] == Pos(8, 22), second

    def test_lock_is_released_when_the_mine_disappears(self):
        payload = self._payload_with_mines([("copper", 8, 22)])
        state = SessionState(DEFAULT)
        world = _world(payload, state)
        worker = world.obs.workers()[0]
        assert strategy.choose_mine(world, state, worker, DEFAULT, ("copper",))
        gone = self._payload_with_mines([("copper", 30, 5)])
        gone["teamOur"]["roles"][1]["pos"] = {"x": 9, "y": 22}
        world2 = _world(gone, state)
        worker2 = world2.obs.workers()[0]
        pick = strategy.choose_mine(world2, state, worker2, DEFAULT, ("copper",))
        assert pick is not None and pick[0] == Pos(30, 5)

    def test_night_restricts_mining_to_the_sides(self):
        payload = self._payload_with_mines([("copper", 20, 16)])
        payload["roundNo"] = 71
        state = SessionState(DEFAULT)
        world = _world(payload, state)
        worker = world.obs.workers()[0]
        assert strategy.choose_mine(world, state, worker, DEFAULT, ("copper",)) is None


# ---------------------------------------------------------------------------
# R5 -- purchases and the held station voucher
# ---------------------------------------------------------------------------
class TestPurchasePolicy:
    def _shop_payload(self, gold: int, station_health: int = 1500,
                      level: int = 1, backpack=(), round_no: int = 20,
                      robots=(), towers: bool = True) -> dict:
        payload = _payload(round_no=round_no)
        payload["teamOur"]["goldNum"] = gold
        roles = [
            {"id": 10013, "pos": {"x": 10, "y": 24}, "roleType": "station",
             "health": station_health, "level": level,
             "backPackCapability": 0, "backpack": []},
            {"id": 10010, "pos": {"x": 25, "y": 20}, "roleType": "worker",
             "health": 220, "level": 1, "backPackCapability": 100,
             "backpack": list(backpack)},
            {"id": 10040, "pos": {"x": 9, "y": 22}, "roleType": "rocket",
             "health": 1000, "level": 1, "backPackCapability": 0, "backpack": []},
        ]
        if towers:
            roles.extend([
                {"id": 10041, "pos": {"x": 9, "y": 23}, "roleType": "rocket",
                 "health": 1000, "level": 1, "backPackCapability": 0,
                 "backpack": []},
                {"id": 10042, "pos": {"x": 9, "y": 24}, "roleType": "rocket",
                 "health": 1000, "level": 1, "backPackCapability": 0,
                 "backpack": []},
            ])
        payload["teamOur"]["roles"] = roles
        payload["robot"] = {"roles": list(robots)}
        return payload

    def test_station_voucher_is_bought_but_not_used(self):
        """With no weapon left to upgrade, the spare 100 gold buys the base券."""
        state = SessionState(DEFAULT)
        payload = self._shop_payload(gold=120)
        for role in payload["teamOur"]["roles"]:
            if role["roleType"] == "rocket":
                role["level"] = 3
        out = compute(payload, state, time.monotonic() + 5)
        command = out["roleCommandMap"].get("10010")
        assert command is not None
        assert command["action"] == "buy", command
        assert command["name"] == "StationUpgradeVoucher1"

    def test_weapon_voucher_comes_before_the_station_voucher(self):
        payload = self._shop_payload(gold=120)
        state = SessionState(DEFAULT)
        out = compute(payload, state, time.monotonic() + 5)
        command = out["roleCommandMap"].get("10010")
        assert command is not None and command.get("name") == \
            "WeaponUpgradeVoucher1", command

    def test_held_voucher_is_used_when_the_base_is_about_to_fall(self):
        state = SessionState(DEFAULT)
        payload = self._shop_payload(gold=0, station_health=90,
                                     backpack=("StationUpgradeVoucher1",))
        payload["teamOur"]["roles"][1]["pos"] = {"x": 10, "y": 22}
        out = compute(payload, state, time.monotonic() + 5)
        command = out["roleCommandMap"].get("10010")
        assert command is not None and command["action"] == "use", command
        assert command["name"] == "StationUpgradeVoucher1"

    def test_held_voucher_is_kept_while_the_base_is_healthy(self):
        state = SessionState(DEFAULT)
        payload = self._shop_payload(gold=0, station_health=1500,
                                     backpack=("StationUpgradeVoucher1",))
        payload["teamOur"]["roles"][1]["pos"] = {"x": 9, "y": 22}
        out = compute(payload, state, time.monotonic() + 5)
        command = out["roleCommandMap"].get("10010")
        assert command is None or command["action"] != "use"

    def test_predicted_lethal_damage_triggers_the_voucher(self):
        """R5's second trigger: robots whose two rounds of damage would kill it."""
        state = SessionState(DEFAULT)
        payload = self._shop_payload(
            gold=0, station_health=200, backpack=("StationUpgradeVoucher1",),
            round_no=71,
            robots=_robots(("bossRobot", 13, 24), ("bossRobot", 14, 24),
                           ("bossRobot", 13, 23)))
        payload["teamOur"]["roles"][1]["pos"] = {"x": 10, "y": 22}
        out = compute(payload, state, time.monotonic() + 5)
        command = out["roleCommandMap"].get("10010")
        assert command is not None
        assert command["action"] in ("use", "move"), command


# ---------------------------------------------------------------------------
# task channel: acceptance anchor, cooldown, executeCmd
# ---------------------------------------------------------------------------
class TestTaskChannel:
    def _task_payload(self, round_no: int, pioneer_pos=(17, 16),
                      phase_task: str = "", llm: str = "") -> dict:
        payload = _payload(round_no=round_no)
        payload["teamOur"]["roles"] = [
            {"id": 10013, "pos": {"x": 10, "y": 24}, "roleType": "station",
             "health": 1500, "level": 1, "backPackCapability": 0, "backpack": []},
            {"id": 10011, "pos": {"x": pioneer_pos[0], "y": pioneer_pos[1]},
             "roleType": "pioneer", "health": 200, "level": 1,
             "backPackCapability": 40, "backpack": []},
        ]
        payload["teamOur"]["playerTasks"] = [
            {"taskType": "自进化类1", "taskPosition": {"x": 14, "y": 14},
             "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30,
             "isValid": True, "timeoutRounds": 60},
            {"taskType": "自进化类2", "taskPosition": {"x": 17, "y": 17},
             "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30,
             "isValid": True, "timeoutRounds": 60},
        ]
        payload["phaseTask"] = phase_task
        payload["llmResp"] = llm
        return payload

    def test_two_cell_point_accepts_next_to_the_reported_cell(self):
        """任务点2 spans (16,17)+(17,17); the payload reports (17,17).

        Standing next to the *other* half used to be accepted by our own check
        and rejected by the judge, so the pioneer spun on a rejected acceptTask
        for 186 rounds in one local match.
        """
        payload = self._task_payload(round_no=10, pioneer_pos=(17, 16))
        parsed = parse(payload)
        task = [t for t in parsed.my_tasks() if t.index == "2"][0]
        assert task.anchor == Pos(17, 17)
        assert task.accepts(Pos(17, 16))
        assert not task.accepts(Pos(15, 16))

    def test_accept_task_is_issued_next_to_the_anchor(self):
        payload = self._task_payload(round_no=10, pioneer_pos=(14, 15))
        state = SessionState(DEFAULT)
        out = compute(payload, state, time.monotonic() + 5)
        command = out["roleCommandMap"].get("10011")
        assert command is not None and command["action"] == "acceptTask", command

    def test_cooling_point_is_not_re_accepted_in_a_loop(self):
        """A failed accept must not be retried forever (the 186-round bug)."""
        from agent.tasks.memory import SkillLibrary
        from agent.tasks.workflow import TaskMachine
        payload = self._task_payload(round_no=10, pioneer_pos=(14, 15))
        obs = parse(payload)
        machine = TaskMachine(SkillLibrary(), DEFAULT)
        for task in obs.my_tasks():
            machine.cooldown_until[machine._key(task)] = 10 ** 6
        assert machine.plan(obs, obs.pioneer()) == {}
        # ...and a single free point is still usable
        machine.cooldown_until.clear()
        plan = machine.plan(obs, obs.pioneer())
        assert plan.get("action") == "acceptTask" or plan.get("goto") is not None

    def test_model_command_is_run_through_execute_cmd(self):
        """接口文档 §2.1 executeCmd: use the sandbox channel when asked to."""
        state = SessionState(DEFAULT)
        first = compute(self._task_payload(round_no=10, pioneer_pos=(14, 15)),
                        state, time.monotonic() + 5)
        assert first["roleCommandMap"]["10011"]["action"] == "acceptTask"
        # round 11: the judge now reports the task text and a model reply that
        # asks for a command to be executed
        second = compute(self._task_payload(round_no=11, pioneer_pos=(14, 15),
                                            phase_task="TASK: query the API",
                                            llm="CMD: python3 -c 'print(42)'"),
                         state, time.monotonic() + 5)
        assert second["executeCmd"] == "python3 -c 'print(42)'", second

    def test_exec_extraction_is_conservative(self):
        from agent.tasks.workflow import TaskMachine
        assert TaskMachine._extract_exec("CMD: ls -la") == "ls -la"
        assert TaskMachine._extract_exec("```\necho hi\n```") == "echo hi"
        assert TaskMachine._extract_exec("I think the answer is 42.") == ""
        assert TaskMachine._extract_exec("```\nls\ncd /\n```") == ""


# ---------------------------------------------------------------------------
# opening build order
# ---------------------------------------------------------------------------
class TestOpening:
    def test_first_round_starts_a_tower_not_a_stone_trip(self):
        state = SessionState(DEFAULT)
        out = compute(_payload(round_no=1), state, time.monotonic() + 5)
        commands = list(out["roleCommandMap"].values())
        assert any(c["action"] in ("build", "move") for c in commands)
        assert all(c.get("name") != "stone" for c in commands)

    def test_three_rounds_are_enough_for_three_rockets(self):
        payload = _payload(round_no=1)
        payload["teamOur"]["goldNum"] = 75
        state = SessionState(DEFAULT)
        log = []
        for offset in range(4):
            turn = json.loads(json.dumps(payload))
            turn["roundNo"] = 1 + offset
            turn["teamOur"]["roles"] = list(payload["teamOur"]["roles"])
            out = compute(turn, state, time.monotonic() + 5)
            log.append(out["roleCommandMap"])
        names = [cmd.get("name") for turn in log for cmd in turn.values()]
        assert "rocket" in names
