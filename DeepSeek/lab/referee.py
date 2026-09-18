"""Approximate local referee for offline regression only.

**This is not the official judge.**  The task book does not specify several
things this file has to invent, and each invention is listed here so no result
from this file is mistaken for evidence about the real game:

===================  ====================================================
invented here        why it is a guess
===================  ====================================================
robot count/spawn    §4.7.3 says only "数量随天数增加"; no formula or spawn
robot AI             §4.7.3 lists three behaviours and no pathing rule
mine regeneration    §4.1 says mines vanish after 10 collects and reappear
                     "随机刷新", with no distribution
task oracle          real tasks are the platform's; this one is synthetic and
                     gives BOTH teams the same information and latency
wave ownership       "每队各有一波" is inferred from 召唤令 semantics
collision resolution §4.5.4.5 specifies the outcomes but not tie-breaking
===================  ====================================================

What it *is* good for: proving the agent never emits an illegal command, never
crashes, never times out, and that its mechanics improve.  It also re-implements
the rules independently rather than importing ``agent`` code, so a wrong shared
assumption cannot make both sides green at once.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

WIDTH, HEIGHT = 41, 32
TOWERS = ("gatling", "railgun", "rocket")
MOBILE = ("worker", "pioneer")
BUILDING_HP = {
    "station": (1500, 3000, 4500),
    "gatling": (1000, 1500, 2000),
    "railgun": (1000, 1500, 2000),
    "rocket": (1000, 1500, 2000),
    "wall": (1000, 1500, 2000),
}
RANGE_BY_LEVEL = {"gatling": (3, 5, 7), "railgun": (6, 8, 10),
                  "rocket": (10, 15, None)}
ROBOT = {
    "smallRobot": (40, 5, 1),
    "middleRobot": (60, 10, 2),
    "largeRobot": (500, 20, 4),
    "bossRobot": (800, 40, 10),
}
ROLE_HP = {"worker": 220, "pioneer": 200}
ROLE_BAG = {"worker": 100, "pioneer": 40}
PRICES = {
    "WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150,
    "WallUpgradeVoucher1": 20, "WallUpgradeVoucher2": 30,
    "StationUpgradeVoucher1": 100, "StationUpgradeVoucher2": 150,
    "WallFixer": 10, "Medicine": 10, "DizzyWeapon": 100, "Bomb": 100,
    "SmallRobotSummonOrder": 20, "MiddleRobotSummonOrder": 30,
    "LargeRobotSummonOrder": 100, "BossRobotSummonOrder": 200,
    "AcientTablet": 15, "StarSand": 15, "FlameBreath": 15,
    "FrostPotion": 15, "ThornAmulet": 15, "IronWhistle": 15,
}
MINERAL_PRICE = {"stone": 1, "iron": 3, "copper": 5}
DAY_ROUNDS, NIGHT_ROUNDS = 70, 60
DAY_LEN = DAY_ROUNDS + NIGHT_ROUNDS
MAX_ROUNDS = 1300
MINE_YIELD = 10
STATION_ANCHOR = "upper_left"        # the only reading consistent with the sample


def cheb(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def footprint(anchor: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    x, y = anchor
    if STATION_ANCHOR == "upper_left":
        return ((x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1))
    return ((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1))


def cells_of(unit: dict) -> tuple[tuple[int, int], ...]:
    pos = (unit["pos"]["x"], unit["pos"]["y"])
    if unit["roleType"] == "station":
        return footprint(pos)
    return (pos,)


def on_line(origin: tuple[int, int], target: tuple[int, int],
            point: tuple[int, int]) -> bool:
    """Integer cross-product collinearity plus bounding-box containment."""
    (x0, y0), (x1, y1), (px, py) = origin, target, point
    if (x1 - x0) * (py - y0) - (y1 - y0) * (px - x0) != 0:
        return False
    return (min(x0, x1) <= px <= max(x0, x1)
            and min(y0, y1) <= py <= max(y0, y1))


@dataclass
class Metrics:
    exceptions: int = 0
    malformed: int = 0
    illegal_commands: int = 0
    failed_commands: int = 0
    commands: int = 0
    collisions: int = 0
    kills: int = 0
    attack_rejections: dict[str, int] = field(default_factory=dict)
    rejections: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.attack_rejections[reason] = self.attack_rejections.get(reason, 0) + 1
        if len(self.rejections) < 200:
            self.rejections.append(reason)


@dataclass
class Team:
    side: str
    type_name: str
    team_id: str
    gold: int = 75
    score: int = 0
    task_score: int = 0
    kill_score: int = 0
    survival_score: int = 0
    roles: list[dict] = field(default_factory=list)
    player_tasks: list[dict] = field(default_factory=list)
    results: dict[str, bool] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    llm_calls_today: int = 0
    summoned: dict[str, int] = field(default_factory=dict)
    active_task: dict | None = None
    phase_task: str = ""
    llm_resp: str = ""
    last_cmd_result: str = ""
    dead: list[dict] = field(default_factory=list)
    task_done: int = 0

    def score_total(self) -> int:
        return self.task_score + self.kill_score + self.survival_score


class Referee:
    """One full 1300-round half, both sides driven by pluggable policies."""

    def __init__(self, seed: int = 1, task_text: str = "") -> None:
        self.rng = random.Random(seed)
        self.round = 1
        self.task_text = task_text or (
            "TASK: return the value of 21 * 2 as a single integer."
        )
        self.next_robot = 30001
        self.mines: dict[tuple[int, int], int] = {}
        self.zones: list[dict] = []
        self.robots: list[dict] = []
        self.over = False
        self.log: list[dict] = []
        self._setup()

    # -- setup -------------------------------------------------------------
    def _setup(self) -> None:
        self.zones = [
            {"pos": {"x": 14, "y": 14}, "neutralType": "challengerTaskPoint1"},
            {"pos": {"x": 17, "y": 17}, "neutralType": "challengerTaskPoint2"},
            {"pos": {"x": 16, "y": 17}, "neutralType": "challengerTaskPoint2"},
            {"pos": {"x": 23, "y": 14}, "neutralType": "defenderTaskPoint1"},
            {"pos": {"x": 26, "y": 17}, "neutralType": "defenderTaskPoint2"},
            {"pos": {"x": 27, "y": 17}, "neutralType": "defenderTaskPoint2"},
            {"pos": {"x": 20, "y": 16}, "neutralType": "vendor"},
            {"pos": {"x": 25, "y": 20}, "neutralType": "weaponShop"},
        ]
        self.teams = {
            "challenger": self._make_team("challenger", (10, 24),
                                          ["10010", "10011", "10012", "10013"]),
            "defender": self._make_team("defender", (30, 8),
                                        ["20010", "20011", "20012", "20013"]),
        }
        for _ in range(6):
            self._spawn_mine()

    def _make_team(self, type_name: str, anchor: tuple[int, int],
                   ids: list[str]) -> Team:
        team = Team(side=type_name, type_name=type_name, team_id=type_name)
        team.roles = [
            {"id": int(ids[3]), "pos": {"x": anchor[0], "y": anchor[1]},
             "roleType": "station", "health": 1500, "level": 1,
             "attackPower": 0, "attackRange": 0, "backPackCapability": 0,
             "backpack": []},
            {"id": int(ids[0]), "pos": {"x": anchor[0], "y": anchor[1] - 3},
             "roleType": "worker", "health": 220, "level": 1,
             "attackPower": 0, "attackRange": 0, "backPackCapability": 100,
             "backpack": []},
            {"id": int(ids[1]), "pos": {"x": anchor[0] + 1, "y": anchor[1] - 3},
             "roleType": "pioneer", "health": 200, "level": 1,
             "attackPower": 0, "attackRange": 0, "backPackCapability": 40,
             "backpack": []},
            {"id": int(ids[2]), "pos": {"x": anchor[0] + 2, "y": anchor[1] - 3},
             "roleType": "worker", "health": 220, "level": 1,
             "attackPower": 0, "attackRange": 0, "backPackCapability": 100,
             "backpack": []},
        ]
        prefix = "自进化类"
        team.player_tasks = [
            {"taskType": f"{prefix}1", "taskPosition": {"x": 14, "y": 14}
             if type_name == "challenger" else {"x": 23, "y": 14},
             "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30,
             "isValid": True, "timeoutRounds": 60},
            {"taskType": f"{prefix}2", "taskPosition": {"x": 17, "y": 17}
             if type_name == "challenger" else {"x": 26, "y": 17},
             "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30,
             "isValid": True, "timeoutRounds": 60},
        ]
        return team

    def _spawn_mine(self) -> None:
        occupied = {(z["pos"]["x"], z["pos"]["y"]) for z in self.zones}
        occupied |= {p for t in self.teams.values() for r in t.roles
                     for p in cells_of(r)}
        occupied |= {(r["pos"]["x"], r["pos"]["y"]) for r in self.robots}
        kind = self.rng.choice(("stone", "iron", "copper"))
        for _ in range(2000):
            pos = (self.rng.randrange(WIDTH), self.rng.randrange(HEIGHT))
            if pos in occupied:
                continue
            if self._in_build_region(pos):
                continue
            self.zones.append({"pos": {"x": pos[0], "y": pos[1]},
                               "neutralType": kind})
            self.mines[pos] = MINE_YIELD
            return

    def _in_build_region(self, pos: tuple[int, int]) -> bool:
        for team in self.teams.values():
            station = next((r for r in team.roles
                            if r["roleType"] == "station"), None)
            if station is None:
                continue
            anchor = (station["pos"]["x"], station["pos"]["y"])
            if cheb(pos, anchor) <= 2:
                return True
        return False

    # -- phase -------------------------------------------------------------
    @property
    def phase(self) -> int:
        return (self.round - 1) % DAY_LEN

    @property
    def day(self) -> int:
        return (self.round - 1) // DAY_LEN + 1

    @property
    def is_day(self) -> bool:
        return self.phase < DAY_ROUNDS

    # -- observation -------------------------------------------------------
    def observation(self, side: str) -> dict:
        team = self.teams[side]
        enemy = self.teams["defender" if side == "challenger" else "challenger"]
        visible = [
            {k: v for k, v in r.items() if k != "backpack"} | {"backpack": []}
            for r in enemy.roles
            if r["roleType"] in ("station", "wall") or _within(team, r, 4)
        ]
        return {
            "roundNo": self.round,
            "mapInfo": {"width": WIDTH, "height": HEIGHT,
                        "zones": [dict(z) for z in self.zones]},
            "teamOur": {
                "type": team.type_name, "teamId": team.team_id,
                "teamName": team.team_id, "goldNum": team.gold,
                "totalScore": team.score_total(),
                "playerTasks": [dict(t) for t in team.player_tasks],
                "roles": [dict(r) for r in team.roles],
            },
            "teamEnemy": {"roles": visible},
            "robot": {"roles": [dict(r) for r in self.robots]},
            "phaseTask": team.phase_task,
            "lastRoundRoleActionResults": dict(team.results),
            "lastSummonTreasureResult": 0,
            "llmResp": team.llm_resp,
            "worldNews": {"officialNews": "今日无重大新闻", "folkLegends": ""},
            "lastCmdResult": team.last_cmd_result,
            "vendorShopList": [{"name": k, "price": v}
                               for k, v in MINERAL_PRICE.items()],
            "weaponShopList": [{"name": k, "price": v}
                               for k, v in PRICES.items()],
            "errors": list(team.errors),
        }

    # -- rules -------------------------------------------------------------
    def static_cells(self) -> set[tuple[int, int]]:
        cells = {(z["pos"]["x"], z["pos"]["y"]) for z in self.zones}
        for team in self.teams.values():
            for unit in team.roles:
                if unit["roleType"] not in MOBILE:
                    cells |= set(cells_of(unit))
        return cells

    def _tower_range(self, tower: dict) -> int:
        level = max(1, int(tower.get("level", 1)))
        table = RANGE_BY_LEVEL[tower["roleType"]]
        documented = table[min(level, len(table)) - 1]
        return WIDTH * 2 if documented is None else documented

    def validate_commands(self, side: str, response: Any,
                          zones: list[dict]) -> list[tuple[int, dict]]:
        """Check every command against the observation this side was shown.

        Real judges collect both sides' commands first and settle them together,
        so neither side may be judged against state the other side's commands
        already mutated.
        """
        team = self.teams[side]
        if not isinstance(response, dict) or not isinstance(
                response.get("roleCommandMap"), dict) \
                or not isinstance(response.get("prompt"), str) \
                or not isinstance(response.get("executeCmd"), str):
            self._malformed(team, "malformed envelope")
            return []
        by_id = {r["id"]: r for r in team.roles}
        used: set[int] = set()
        accepted: list[tuple[int, dict]] = []
        for key in sorted(response["roleCommandMap"], key=str):
            command = response["roleCommandMap"][key]
            team.metrics.commands += 1
            try:
                rid = int(key)
            except (TypeError, ValueError):
                self._malformed(team, "roleCommandMap key is not an integer")
                continue
            unit = by_id.get(rid)
            ok, why = self._legal(command, unit, team, used, zones)
            if not ok:
                if why.startswith("malformed"):
                    self._malformed(team, why)
                else:
                    team.metrics.illegal_commands += 1
                    team.metrics.reject(why)
                team.results[str(rid)] = False
                continue
            if command.get("action") == "attack":
                used.add(int(command["controllerId"]))
            accepted.append((rid, command))
        return accepted

    def apply_accepted(self, side: str,
                       accepted: list[tuple[int, dict]]) -> None:
        team = self.teams[side]
        by_id = {r["id"]: r for r in team.roles}
        for rid, command in accepted:
            unit = by_id.get(rid)
            if unit is None:
                team.results[str(rid)] = False
                continue
            applied = self._apply(side, team, unit, command)
            team.results[str(rid)] = applied
            if applied is False:
                team.metrics.failed_commands += 1

    def apply_commands(self, side: str, response: Any) -> None:
        team = self.teams[side]
        team.results = {}
        team.errors = []
        team.llm_resp = ""
        team.last_cmd_result = ""

        if not isinstance(response, dict) or not isinstance(
                response.get("roleCommandMap"), dict) \
                or not isinstance(response.get("prompt"), str) \
                or not isinstance(response.get("executeCmd"), str):
            team.metrics.exceptions += 1
            team.metrics.malformed += 1
            team.errors.append({"errorCode": 4,
                                "description": "malformed envelope"})
            return

        commands = response["roleCommandMap"]
        by_id = {r["id"]: r for r in team.roles}
        used_controllers: set[int] = set()

        for key in sorted(commands, key=str):
            command = commands[key]
            team.metrics.commands += 1
            try:
                rid = int(key)
            except (TypeError, ValueError):
                self._malformed(team, "roleCommandMap key is not an integer")
                continue
            unit = by_id.get(rid)
            ok, why = self._legal(command, unit, team, used_controllers)
            if not ok:
                if why.startswith("malformed"):
                    self._malformed(team, why)
                else:
                    team.metrics.illegal_commands += 1
                    team.metrics.reject(why)
                team.results[str(rid)] = False
                continue
            applied = self._apply(side, team, unit, command)
            team.results[str(rid)] = applied
            if command.get("action") == "attack":
                if applied:
                    used_controllers.add(int(command["controllerId"]))
                else:
                    team.metrics.failed_commands += 1
            elif applied is False:
                team.metrics.failed_commands += 1

    def _malformed(self, team: Team, why: str) -> None:
        team.metrics.exceptions += 1
        team.metrics.malformed += 1
        team.errors.append({"errorCode": 4, "description": why})

    def _legal(self, command: Any, unit: dict | None,
               team: Team, used_controllers: set[int],
               zones: list[dict] | None = None) -> tuple[bool, str]:
        zones = self.zones if zones is None else zones
        if not isinstance(command, dict):
            return False, "malformed:command not an object"
        action = command.get("action")
        if action not in ("move", "attack", "sell", "buy", "build", "remove",
                          "acceptTask", "submitAnswer", "summonTreasure",
                          "use", "drop", "collect"):
            return False, "malformed:unknown action"
        targets = command.get("targetPos", [])
        if not isinstance(targets, list):
            return False, "malformed:targetPos not a list"
        for point in targets:
            if not isinstance(point, dict) or type(point.get("x")) is not int \
                    or type(point.get("y")) is not int:
                return False, "malformed:targetPos must hold integer coords"
        if action != "attack" and len(targets) > 1:
            return False, "malformed:too many targets"
        for field_name in ("name", "taskAnswer", "controllerId"):
            if field_name in command and not isinstance(command[field_name], str):
                return False, f"malformed:{field_name} must be a string"
        if "num" in command and type(command["num"]) is not int:
            return False, "malformed:num must be an int"
        if "item" in command and (not isinstance(command["item"], list)
                                 or any(not isinstance(i, str)
                                        for i in command["item"])):
            return False, "malformed:item must be a list of strings"
        if action in ("move", "attack", "build", "remove", "collect",
                      "summonTreasure") and not targets:
            return False, "malformed:missing targetPos"
        if action in ("build", "sell", "buy", "use", "drop") \
                and not command.get("name"):
            return False, "malformed:missing name"
        if action == "attack" and not command.get("controllerId"):
            return False, "malformed:missing controllerId"
        if action == "submitAnswer" and not isinstance(
                command.get("taskAnswer"), str):
            return False, "malformed:missing taskAnswer"
        if action == "use" and command.get("name") in (
                "WallFixer", "DizzyWeapon", "Bomb") \
                and not targets:
            return False, "malformed:use requires targetPos"
        if action == "summonTreasure" and "item" not in command:
            return False, "malformed:summonTreasure requires item"
        if unit is None:
            return False, "illegal:unknown role id"

        # rule-level legality (counts as an illegal command, not malformed)
        if action == "attack":
            tower = unit
            if tower["roleType"] not in TOWERS:
                return False, "illegal:attack by a non-tower"
            if self.is_day:
                return False, "illegal:attack during the day"
            controller = next((r for r in team.roles
                               if str(r["id"]) == str(command.get("controllerId"))),
                              None)
            if controller is None or controller["roleType"] not in MOBILE:
                return False, "illegal:controller not mobile"
            if int(controller["id"]) in used_controllers:
                return False, "illegal:controller already fired"
            if cheb((controller["pos"]["x"], controller["pos"]["y"]),
                    (tower["pos"]["x"], tower["pos"]["y"])) > 1:
                return False, "illegal:controller not adjacent"
            want = 1 if tower["roleType"] == "railgun" else max(
                1, int(tower.get("level", 1)))
            if len(targets) != want:
                return False, f"illegal:target count {len(targets)} != {want}"
            reach = self._tower_range(tower)
            points = [(p["x"], p["y"]) for p in targets]
            for point in points:
                if not (0 <= point[0] < WIDTH and 0 <= point[1] < HEIGHT):
                    return False, "illegal:target out of bounds"
                if cheb((tower["pos"]["x"], tower["pos"]["y"]), point) > reach:
                    return False, "illegal:target out of range"
                if tower["roleType"] != "rocket" \
                        and point == (tower["pos"]["x"], tower["pos"]["y"]):
                    return False, "illegal:target is own cell"
            if tower["roleType"] == "gatling":
                origin = (tower["pos"]["x"], tower["pos"]["y"])
                vectors = [(p[0] - origin[0], p[1] - origin[1]) for p in points]
                for i, a in enumerate(vectors):
                    for b in vectors[i + 1:]:
                        if a[0] * b[0] + a[1] * b[1] < 0:
                            return False, "illegal:gatling cone violated"
        elif action == "move":
            if unit["roleType"] not in MOBILE:
                return False, "illegal:immobile unit cannot move"
            point = (targets[0]["x"], targets[0]["y"])
            if not (0 <= point[0] < WIDTH and 0 <= point[1] < HEIGHT):
                return False, "illegal:move out of bounds"
            if cheb((unit["pos"]["x"], unit["pos"]["y"]), point) != 1:
                return False, "illegal:move not adjacent"
        elif action == "collect":
            if unit["roleType"] != "worker":
                return False, "illegal:collect by non-worker"
            point = (targets[0]["x"], targets[0]["y"])
            if self._zone_at(point, zones) not in ("stone", "iron", "copper"):
                return False, "illegal:collect target not a mine"
            if cheb((unit["pos"]["x"], unit["pos"]["y"]), point) != 1:
                return False, "illegal:collect not adjacent"
        elif action == "build":
            if unit["roleType"] != "worker":
                return False, "illegal:build by non-worker"
            if not self.is_day:
                return False, "illegal:build at night"
            point = (targets[0]["x"], targets[0]["y"])
            name = command["name"]
            if name not in ("wall", *TOWERS):
                return False, "illegal:unknown build name"
            if cheb((unit["pos"]["x"], unit["pos"]["y"]), point) != 1:
                return False, "illegal:build not adjacent"
            if self._zone_at(point, zones) is not None:
                return False, "illegal:build on a neutral zone"
            if name == "wall" and unit["backpack"].count("stone") < 1:
                return False, "illegal:no stone"
            if name != "wall":
                if team.gold < 25:
                    return False, "illegal:no gold"
                if self._alive_towers(team) >= 3:
                    return False, "illegal:tower cap"
        elif action == "sell":
            if not self._near(team, unit, "vendor"):
                return False, "illegal:not near vendor"
        elif action == "buy":
            if not self._near(team, unit, "weaponShop"):
                return False, "illegal:not near weapon shop"
        elif action == "acceptTask":
            if unit["roleType"] != "pioneer":
                return False, "illegal:acceptTask by non-pioneer"
        return True, "ok"

    @staticmethod
    def _alive_towers(team: Team) -> int:
        return sum(1 for r in team.roles
                   if r["roleType"] in TOWERS and r["health"] > 0)

    def _near(self, team: Team, unit: dict, zone_kind: str) -> bool:
        pos = (unit["pos"]["x"], unit["pos"]["y"])
        return any(cheb((z["pos"]["x"], z["pos"]["y"]), pos) <= 1
                   for z in self.zones if z["neutralType"] == zone_kind)

    def _zone_at(self, pos: tuple[int, int],
                 zones: list[dict] | None = None) -> str | None:
        for zone in (self.zones if zones is None else zones):
            if (zone["pos"]["x"], zone["pos"]["y"]) == pos:
                return zone["neutralType"]
        return None

    # -- application -------------------------------------------------------
    def _apply(self, side: str, team: Team, unit: dict, command: dict) -> bool:
        action = command["action"]
        targets = command.get("targetPos", [])
        if action == "move":
            point = (targets[0]["x"], targets[0]["y"])
            return self._try_move(side, team, unit, point)
        if action == "attack":
            return self._fire(team, unit, command, targets)
        if action == "collect":
            point = (targets[0]["x"], targets[0]["y"])
            if unit["backpack"] and len(unit["backpack"]) >= unit.get(
                    "backPackCapability", 0):
                return False
            unit["backpack"].append(self._zone_at(point) or "stone")
            self.mines[point] = self.mines.get(point, 1) - 1
            if self.mines.get(point, 0) <= 0:
                self._remove_zone(point)
                self.mines.pop(point, None)
            return True
        if action == "build":
            point = (targets[0]["x"], targets[0]["y"])
            name = command["name"]
            if name == "wall":
                if unit["backpack"].count("stone") < 1:
                    return False
                unit["backpack"].remove("stone")
            else:
                if team.gold < 25:
                    return False
                if self._alive_towers(team) >= 3:
                    return False
                team.gold -= 25
            tower_kind = name
            new_id = self._next_unit_id(team, name)
            unit_row = {"id": new_id, "pos": {"x": point[0], "y": point[1]},
                        "roleType": tower_kind,
                        "health": BUILDING_HP[tower_kind][0], "level": 1,
                        "attackPower": 0, "attackRange": 0,
                        "backPackCapability": 0, "backpack": []}
            existing = self._unit_at(team, point)
            if existing is not None and existing["roleType"] in TOWERS:
                team.roles.remove(existing)
            team.roles.append(unit_row)
            return True
        if action == "remove":
            point = (targets[0]["x"], targets[0]["y"])
            existing = self._unit_at(team, point)
            if existing is None or existing["roleType"] != "wall":
                return False
            team.roles.remove(existing)
            return True
        if action == "sell":
            name = command["name"]
            num = int(command.get("num", 1))
            have = unit["backpack"].count(name)
            if have < 1:
                return False
            num = min(num, have)
            for _ in range(num):
                unit["backpack"].remove(name)
            team.gold += num * MINERAL_PRICE.get(name, 1)
            return True
        if action == "buy":
            name = command["name"]
            num = int(command.get("num", 1))
            price = PRICES.get(name)
            if price is None:
                return False
            cost = price * num
            if team.gold < cost:
                return False
            if len(unit["backpack"]) + num > unit.get("backPackCapability", 0):
                return False
            team.gold -= cost
            unit["backpack"].extend([name] * num)
            return True
        if action == "use":
            return self._use(team, unit, command, targets)
        if action == "drop":
            name = command["name"]
            if name not in unit["backpack"]:
                return False
            unit["backpack"].remove(name)
            return True
        if action == "acceptTask":
            for task in team.player_tasks:
                if task["isValid"] and task["coldDownRounds"] == 0 \
                        and cheb((unit["pos"]["x"], unit["pos"]["y"]),
                                 (task["taskPosition"]["x"],
                                  task["taskPosition"]["y"])) <= 1:
                    team.active_task = {
                        "task": task, "start": self.round,
                        "timeout": task.get("timeoutRounds", 60),
                        "best": 0.0, "submitted": False,
                    }
                    team.phase_task = self.task_text
                    return True
            return False
        if action == "submitAnswer":
            active = team.active_task
            if active is None:
                return False
            answer = command.get("taskAnswer", "")
            correct = self._grade(answer)
            active["submitted"] = True
            active["best"] = max(active["best"], correct)
            if correct >= 1.0:
                self._end_task(team, completed=True)
            else:
                team.errors.append({"errorCode": 2, "description": "partial"})
            return True
        return False

    def _grade(self, answer: str) -> float:
        """Synthetic oracle: both sides get the same question and the same latency."""
        cleaned = (answer or "").strip().strip("`*_ ")
        return 1.0 if cleaned.rstrip(".") == "42" else 0.0

    def _end_task(self, team: Team, *, completed: bool) -> None:
        active = team.active_task
        if active is None:
            return
        task = active["task"]
        ratio = active["best"]
        elapsed = max(1, self.round - active["start"])
        reward = int(task.get("scoreReward", 0))
        if completed:
            gained = reward + int(5 * active.get("timeout", 1) / elapsed)
        else:
            gained = int(reward * ratio)
        team.task_score += gained
        team.gold += int(task.get("goldReward", 0) * ratio)
        team.task_done += 1 if completed else 0
        task["coldDownRounds"] = 30
        task["isValid"] = True
        team.active_task = None
        team.phase_task = ""
        del completed

    def _use(self, team: Team, unit: dict, command: dict,
             targets: list[dict]) -> bool:
        name = command["name"]
        if name not in unit["backpack"]:
            return False
        point = (targets[0]["x"], targets[0]["y"]) if targets else None
        if name in ("WallFixer", "DizzyWeapon", "Bomb") and point is None:
            return False
        if name == "Medicine":
            unit["health"] = ROLE_HP.get(unit["roleType"], unit["health"])
        elif name in ("WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
                      "StationUpgradeVoucher1", "StationUpgradeVoucher2",
                      "WallUpgradeVoucher1", "WallUpgradeVoucher2"):
            target = self._unit_at(team, point) if point else None
            if target is None:
                return False
            if cheb((unit["pos"]["x"], unit["pos"]["y"]), point) > 1:
                return False
            if target["level"] >= 3:
                return False
            target["level"] += 1
            target["health"] = BUILDING_HP[target["roleType"]][
                min(target["level"], 3) - 1]
        elif name == "WallFixer":
            target = self._unit_at(team, point) if point else None
            if target is None or target["roleType"] != "wall":
                return False
            target["health"] = BUILDING_HP["wall"][
                min(target["level"], 3) - 1]
        elif name == "Bomb":
            for robot in self.robots:
                if cheb((robot["pos"]["x"], robot["pos"]["y"]), point) <= 1:
                    robot["health"] -= 100
        elif name == "DizzyWeapon":
            for robot in self.robots:
                if cheb((robot["pos"]["x"], robot["pos"]["y"]), point) <= 1:
                    robot["abnormalState"] = "dizzy"
        elif name.endswith("SummonOrder"):
            key = name.replace("SummonOrder", "")
            team.summoned[key] = team.summoned.get(key, 0) + 1
        unit["backpack"].remove(name)
        return True

    def _next_unit_id(self, team: Team, name: str) -> int:
        base = 10000 if team.type_name == "challenger" else 20000
        if name == "wall":
            base = 40000 if team.type_name == "challenger" else 41000
            used = [r["id"] for r in team.roles if r["roleType"] == "wall"]
            return (max(used) + 1) if used else base
        offset = {"gatling": 20, "railgun": 30, "rocket": 40}[name]
        used = [r["id"] for r in team.roles if r["roleType"] == name]
        if not used:
            return base + offset
        return max(used) + 1

    def _unit_at(self, team: Team, point: tuple[int, int]) -> dict | None:
        for unit in team.roles:
            if point in cells_of(unit):
                return unit
        return None

    def _remove_zone(self, point: tuple[int, int]) -> None:
        self.zones = [z for z in self.zones
                      if (z["pos"]["x"], z["pos"]["y"]) != point]

    def _try_move(self, side: str, team: Team, unit: dict,
                  point: tuple[int, int]) -> bool:
        if point in self.static_cells():
            return False
        if any(point in cells_of(r) for t in self.teams.values()
               for r in t.roles if r["id"] != unit["id"]):
            return False
        if any((r["pos"]["x"], r["pos"]["y"]) == point for r in self.robots):
            return False
        # contested target: whoever applied first holds it
        for other in team.roles:
            if other["id"] == unit["id"] or other["roleType"] not in MOBILE:
                continue
            if other.get("_pending") == point:
                other["_pending"] = None
                team.metrics.collisions += 1
                return False
        for other in team.roles:
            other["_pending"] = None
        unit["pos"] = {"x": point[0], "y": point[1]}
        del side
        return True

    def _fire(self, team: Team, tower: dict, command: dict,
              targets: list[dict]) -> bool:
        kind = tower["roleType"]
        origin = (tower["pos"]["x"], tower["pos"]["y"])
        points = [(p["x"], p["y"]) for p in targets]
        damage = {r["id"]: 0 for r in self.robots}
        hit = False
        for point in points:
            if kind == "rocket":
                for robot in self.robots:
                    d = cheb((robot["pos"]["x"], robot["pos"]["y"]), point)
                    if d <= 1:
                        damage[robot["id"]] += 20 if d == 0 else 10
                        hit = True
            else:
                along = sorted(
                    (r for r in self.robots
                     if on_line(origin, point,
                                (r["pos"]["x"], r["pos"]["y"]))),
                    key=lambda r: (cheb(origin, (r["pos"]["x"], r["pos"]["y"])),
                                   r["id"]),
                )
                energy = 10 if kind == "gatling" else 10 * max(
                    1, int(tower.get("level", 1)))
                for robot in along:
                    dealt = min(energy, max(0, robot["health"]))
                    damage[robot["id"]] += dealt
                    energy -= dealt
                    hit = True
                    if kind == "gatling" or energy <= 0:
                        break
        for robot in self.robots:
            dealt = damage.get(robot["id"], 0)
            if not dealt:
                continue
            robot["health"] -= dealt
            if dealt > robot.get("_top_damage", 0):
                robot["_top_damage"] = dealt
                robot["_top_team"] = team.type_name
        del command
        return hit

    # -- round lifecycle ---------------------------------------------------
    def step(self, policies: dict) -> None:
        """Advance one round; ``policies`` maps side -> callable(observation)."""
        responses: dict[str, Any] = {}
        observations: dict[str, dict] = {}
        for side in self.teams:
            observations[side] = self.observation(side)
            try:
                responses[side] = policies[side](observations[side])
            except Exception:
                self.teams[side].metrics.exceptions += 1
                self.teams[side].metrics.malformed += 1
                responses[side] = None

        # Collect then settle, like the real judge: every command is validated
        # against the observation its own side was shown, before any command
        # mutates shared state.
        accepted: dict[str, list[tuple[int, dict]]] = {}
        for side in self.teams:
            zones = list(observations[side]["mapInfo"]["zones"])
            accepted[side] = self.validate_commands(side, responses[side], zones)
        for side in self.teams:
            self.apply_accepted(side, accepted[side])

        # LLM / sandbox round trip (next round sees the result)
        for side, response in responses.items():
            team = self.teams[side]
            if isinstance(response, dict):
                if response.get("prompt"):
                    active = team.active_task is not None
                    if active or team.llm_calls_today < 3:
                        team.llm_resp = "42"
                        if not active:
                            team.llm_calls_today += 1
                    else:
                        team.errors.append({"errorCode": 5,
                                            "description": "daily quota"})
                if response.get("executeCmd"):
                    team.last_cmd_result = ("[exitCode:0]\n42"
                                            if active_or_none(team) else
                                            "[JUDGER_ERROR]\nno active task")

        self._robots_act()
        self._spawn_wave_if_needed()
        self._tick_tasks()
        self._end_of_round()
        self.round += 1

    def _robots_act(self) -> None:
        for robot in self.robots:
            if robot.get("abnormalState") == "dizzy":
                robot["abnormalState"] = ""
                continue
            pos = (robot["pos"]["x"], robot["pos"]["y"])
            target_team = self.teams.get(
                robot.get("targetTeam", "challenger"))
            if target_team is None:
                continue
            reachable = [
                r for r in target_team.roles
                for c in cells_of(r)
                if cheb(pos, c) <= 3
            ]
            if reachable:
                victim = min(reachable, key=lambda r: (
                    cheb(pos, (r["pos"]["x"], r["pos"]["y"])), r["id"]))
                victim["health"] -= ROBOT[robot["roleType"]][1]
                continue
            goals = [cells_of(r) for r in target_team.roles
                     if r["roleType"] == "station"]
            if not goals:
                continue
            goal = min((c for group in goals for c in group),
                       key=lambda c: (cheb(pos, c), c))
            step = _step_towards(pos, goal)
            if step is None:
                continue
            if step in self.static_cells():
                continue
            if any((r["pos"]["x"], r["pos"]["y"]) == step for r in self.robots
                   if r is not robot):
                continue
            robot["pos"] = {"x": step[0], "y": step[1]}

    def _spawn_wave_if_needed(self) -> None:
        if self.phase != DAY_ROUNDS:
            return
        day = self.day
        for side, team in self.teams.items():
            enemy_type = "defender" if side == "challenger" else "challenger"
            kinds = ["smallRobot"] * (3 + day * 2)
            kinds += ["middleRobot"] * max(0, day - 1)
            kinds += ["largeRobot"] * max(0, day - 3)
            kinds += ["bossRobot"] * max(0, day - 7)
            for key, extra in team.summoned.items():
                kinds += [f"{key[0].lower()}{key[1:]}Robot"] * extra
            team.summoned = {}
            anchor = (5, 6) if side == "challenger" else (33, 24)
            for index, kind in enumerate(kinds):
                if kind not in ROBOT:
                    continue
                spot = (anchor[0] + index % 6, anchor[1] + index // 6)
                if not (0 <= spot[0] < WIDTH and 0 <= spot[1] < HEIGHT):
                    continue
                self.robots.append({
                    "id": self.next_robot, "pos": {"x": spot[0], "y": spot[1]},
                    "roleType": kind, "health": ROBOT[kind][0],
                    "abnormalState": "", "targetTeam": enemy_type,
                })
                self.next_robot += 1

    def _tick_tasks(self) -> None:
        for team in self.teams.values():
            for task in team.player_tasks:
                task["coldDownRounds"] = max(0, task["coldDownRounds"] - 1)
            active = team.active_task
            if active is None:
                continue
            pioneer = next((r for r in team.roles
                            if r["roleType"] == "pioneer"), None)
            task = active["task"]
            if pioneer is None or pioneer["health"] <= 0:
                self._end_task(team, completed=False)
                continue
            if cheb((pioneer["pos"]["x"], pioneer["pos"]["y"]),
                    (task["taskPosition"]["x"],
                     task["taskPosition"]["y"])) > 1:
                self._end_task(team, completed=False)
                continue
            if self.round - active["start"] > active.get("timeout", 60):
                self._end_task(team, completed=False)

    def _end_of_round(self) -> None:
        for team in self.teams.values():
            # robot kills: credit the side that dealt the most damage
            for robot in list(self.robots):
                if robot["health"] <= 0:
                    winner = robot.get("_top_team")
                    if winner is not None:
                        self.teams[winner].kill_score += ROBOT[robot["roleType"]][2]
                        self.teams[winner].metrics.kills += 1
                    self.robots.remove(robot)
            for unit in list(team.roles):
                if unit["health"] > 0:
                    continue
                if unit["roleType"] in MOBILE:
                    team.roles.remove(unit)
                    team.dead.append(unit)
                elif unit["roleType"] != "station":
                    # 任务书 §4.1: a demolished building's cell becomes walkable
                    team.roles.remove(unit)
                # the station is left at 0 HP so base destruction stays visible
            if not any(r["roleType"] == "station" for r in team.roles):
                team.base_destroyed = True
        self.robots = [r for r in self.robots if r["health"] > 0]

        if self.phase == DAY_LEN - 1:
            self.robots = []
        if self.phase == 0:
            for team in self.teams.values():
                team.llm_calls_today = 0
        # survival scoring is settled by the caller at end of match
        destroyed = [t for t in self.teams.values()
                     if not any(r["roleType"] == "station" for r in t.roles)]
        if len(destroyed) == len(self.teams) or self.round >= MAX_ROUNDS:
            self.over = True
        if self.round % DAY_LEN == 0:
            day = self.day
            for team in self.teams.values():
                if any(r["roleType"] == "station" for r in team.roles):
                    team.survival_score += 10 * day

    def finalize(self) -> dict:
        result = {}
        for side, team in self.teams.items():
            result[side] = {
                "score": team.score_total(),
                "task_score": team.task_score,
                "gold": team.gold,
                "survival": team.survival_score,
                "tasks_completed": team.task_done,
                "base_alive": any(r["roleType"] == "station"
                                  for r in team.roles),
                "exceptions": team.metrics.exceptions,
                "malformed": team.metrics.malformed,
                "illegal_commands": team.metrics.illegal_commands,
                "failed_commands": team.metrics.failed_commands,
                "commands": team.metrics.commands,
                "collisions": team.metrics.collisions,
                "rejection_kinds": dict(team.metrics.attack_rejections),
                "sample_rejections": team.metrics.rejections[:20],
            }
        return result


def active_or_none(team: Team) -> bool:
    return team.active_task is not None


def _step_towards(pos: tuple[int, int], goal: tuple[int, int]):
    dx = (goal[0] > pos[0]) - (goal[0] < pos[0])
    dy = (goal[1] > pos[1]) - (goal[1] < pos[1])
    if dx == 0 and dy == 0:
        return None
    return (pos[0] + dx, pos[1] + dy)


def _within(team: Team, unit: dict, radius: int) -> bool:
    """Is ``unit`` inside the shared vision radius of ``team``? (任务书 §4.3)"""
    pos = (unit["pos"]["x"], unit["pos"]["y"])
    for ours in team.roles:
        for cell in cells_of(ours):
            if cheb(pos, cell) <= radius:
                return True
    return False
