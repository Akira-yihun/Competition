"""Domain types parsed from the judge request.

Parsing is *total*: every field accessor has a safe default, so a malformed or
partial payload degrades to "we know less this turn" instead of raising.  A
raised exception here would cost one of the five allowed 异常 responses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from . import rules as R


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return default
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return default
    return default


def as_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return default


def as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, order=True)
class Pos:
    x: int
    y: int

    @classmethod
    def load(cls, raw: Any) -> "Pos | None":
        obj = as_dict(raw)
        if "x" not in obj or "y" not in obj:
            return None
        x, y = as_int(obj.get("x"), -1), as_int(obj.get("y"), -1)
        if x < 0 or y < 0:
            return None
        return cls(x, y)

    def dump(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}

    def step(self, dx: int, dy: int) -> "Pos":
        return Pos(self.x + dx, self.y + dy)

    def neighbours(self) -> tuple["Pos", ...]:
        return tuple(self.step(dx, dy) for dx, dy in STEPS)

    def ring(self, radius: int) -> tuple["Pos", ...]:
        """Cells at exactly ``radius`` Chebyshev distance (radius >= 1)."""
        if radius <= 0:
            return (self,)
        out: list[Pos] = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) == radius:
                    out.append(self.step(dx, dy))
        return tuple(out)


STEPS: tuple[tuple[int, int], ...] = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


def distance(a: Pos, b: Pos) -> int:
    """切比雪夫距离 (任务书 4.5.4.1)."""
    return max(abs(a.x - b.x), abs(a.y - b.y))


def footprint_distance(pos: Pos, cells: Iterable[Pos]) -> int:
    best = None
    for cell in cells:
        d = distance(pos, cell)
        if best is None or d < best:
            best = d
    return 0 if best is None else best


def station_footprints(anchor: Pos) -> tuple[tuple[Pos, ...], ...]:
    """The base's 2x2 footprint, from the anchor the payload reports.

    接口文档 1.3.1: the station ``pos`` is the **top-left** corner of its 2x2
    footprint.  With y growing upwards that is (min x, max y), so the four cells
    are ``anchor``, ``anchor+(1,0)``, ``anchor+(0,-1)``, ``anchor+(1,-1)``.

    This is now the *only* reading, and it is the one the real sample confirms:
    in ``docs/request.txt`` the three towers of the challenger sit at
    (9,24)/(10,25)/(9,25) around a station reported at (10,24), which are exactly
    the ring-1 cells of this footprint -- the old "opposite corner" fallback
    would have placed the rocket *inside* the base.  Keeping both readings also
    invented a phantom blocked row that could veto a legal build or step.

    The tuple-of-tuples shape is kept so existing unpacking call sites keep
    working.
    """
    return ((
        anchor,
        anchor.step(1, 0),
        anchor.step(0, -1),
        anchor.step(1, -1),
    ),)


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Unit:
    unit_id: int
    pos: Pos
    kind: str
    health: int = 0
    level: int = 1
    cooldown: int = 0
    attack_power: int = 0
    attack_range: int = 0
    capacity: int = 0
    backpack: tuple[str, ...] = ()
    is_ours: bool = True

    @classmethod
    def load(cls, raw: Any, *, ours: bool) -> "Unit | None":
        obj = as_dict(raw)
        pos = Pos.load(obj.get("pos"))
        kind = as_str(obj.get("roleType"))
        if pos is None or not kind:
            return None
        return cls(
            unit_id=as_int(obj.get("id"), -1),
            pos=pos,
            kind=kind,
            health=as_int(obj.get("health"), 0),
            level=max(1, as_int(obj.get("level"), 1)),
            cooldown=max(0, as_int(obj.get("cooldown"), 0)),
            attack_power=as_int(obj.get("attackPower"), 0),
            attack_range=max(0, as_int(obj.get("attackRange"), 0)),
            capacity=max(0, as_int(obj.get("backPackCapability"), 0)),
            backpack=tuple(as_str(i) for i in as_list(obj.get("backpack")) if as_str(i)),
            is_ours=ours,
        )

    @property
    def alive(self) -> bool:
        return self.health > 0

    @property
    def is_mobile(self) -> bool:
        return self.kind in R.MOBILE_TYPES

    @property
    def is_tower(self) -> bool:
        return self.kind in R.TOWER_TYPES

    @property
    def backpack_full(self) -> bool:
        return self.capacity > 0 and len(self.backpack) >= self.capacity

    def count(self, item: str) -> int:
        return self.backpack.count(item)

    def documented_range(self) -> int | None:
        table = R.DOCUMENTED_RANGE.get(self.kind)
        if table is None:
            return None
        idx = min(max(self.level, 1), len(table)) - 1
        return table[idx]

    def effective_range(self) -> int:
        """Conservative attack range: never larger than the documented table.

        任务书 4.5.1 gives gatling/railgun/rocket ranges per level, but the
        sample request reports L1 range +1 for gatling and railgun and
        INT32_MAX for rocket.  Firing beyond the documented value risks an
        out-of-range target, so we take the smaller of the two.
        """
        documented = self.documented_range()
        reported = self.attack_range
        if reported >= R.RANGE_SENTINEL:
            reported = 0
        if documented is None:
            return reported
        if reported <= 0:
            return documented
        return min(reported, documented)

    def cells(self, station_cells: tuple[Pos, ...] = ()) -> tuple[Pos, ...]:
        if self.kind == R.STATION and station_cells:
            return station_cells
        return (self.pos,)


@dataclass(frozen=True, slots=True)
class Robot:
    """A hostile robot.

    ``attackPower`` / ``attackRange`` are deliberately absent: 接口文档 1.5.1's
    ``RobotRole`` does not define them, so attack power/range come from the
    任务书 4.7.2 table (``rules.ROBOT_STATS``) instead of the payload.
    """

    robot_id: int
    pos: Pos
    kind: str = R.DEFAULT_ROBOT
    health: int = 0
    abnormal: str = ""
    target_team: str = ""

    @classmethod
    def load(cls, raw: Any) -> "Robot | None":
        obj = as_dict(raw)
        pos = Pos.load(obj.get("pos"))
        if pos is None:
            return None
        return cls(
            robot_id=as_int(obj.get("id"), -1),
            pos=pos,
            kind=as_str(obj.get("roleType"), R.DEFAULT_ROBOT) or R.DEFAULT_ROBOT,
            health=as_int(obj.get("health"), 0),
            abnormal=as_str(obj.get("abnormalState")),
            target_team=as_str(obj.get("targetTeam")),
        )

    @property
    def alive(self) -> bool:
        return self.health > 0

    @property
    def score(self) -> int:
        return R.ROBOT_STATS.get(self.kind, R.ROBOT_STATS[R.DEFAULT_ROBOT])["score"]

    @property
    def base_hp(self) -> int:
        return R.ROBOT_STATS.get(self.kind, R.ROBOT_STATS[R.DEFAULT_ROBOT])["hp"]

    @property
    def dizzy(self) -> bool:
        return self.abnormal == "dizzy"

    def value_per_hp(self) -> float:
        hp = max(1, self.health)
        return self.score / hp


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlayerTask:
    task_type: str
    cells: tuple[Pos, ...]
    cooldown: int = 0
    score_reward: int = 0
    gold_reward: int = 0
    valid: bool = False
    timeout_rounds: int = 0
    cold_down_reported: int = 0
    #: the position the judge itself reports in ``playerTasks[].taskPosition``.
    #: Task point 2 spans two cells, so the clustered ``cells`` anchor can differ
    #: from the judge's own anchor; the reported one wins for acceptance tests.
    reported: Pos | None = None

    @property
    def team(self) -> str:
        """``challenger`` / ``defender`` / ``""``."""
        for prefix in ("challenger", "defender"):
            if self.task_type.startswith(prefix):
                return prefix
        return ""

    @property
    def index(self) -> str:
        """``1`` or ``2`` (which of the two team task points)."""
        tail = self.task_type[len(self.team):] if self.team else self.task_type
        digits = "".join(ch for ch in tail if ch.isdigit())
        return digits or "?"

    @property
    def anchor(self) -> Pos:
        return self.reported or self.cells[0]

    def stands(self) -> tuple[Pos, ...]:
        """Every cell a pioneer may stand on to reach this task point."""
        out: list[Pos] = []
        seen: set[Pos] = set()
        for cell in self.cells:
            for nb in cell.neighbours():
                if nb not in seen:
                    seen.add(nb)
                    out.append(nb)
        return tuple(out)

    def accepts(self, pos: Pos) -> bool:
        """任务书 §4.4: 开拓者须在任务点周围一格内.

        Measured against the *reported* anchor, which is the cell the judge
        demonstrably uses: standing next to the other half of a two-cell point is
        within the letter of §4.6.2 ("处于任一格") but was rejected by every local
        simulator, so the strict reading is the safe one -- and it is always
        reachable, because the anchor has free neighbours.
        """
        return distance(pos, self.anchor) <= 1


def cluster_task_points(entries: list[tuple[Pos, str]]) -> list[PlayerTask]:
    """Group raw task-point zone entries into logical task points.

    TaskPoint2 occupies two cells, so the request contains the same
    ``neutralType`` twice (实测: challengerTaskPoint2 出现 2 次).  Counting zone
    entries would turn 2 task points into 4; cluster by type + adjacency.
    """
    groups: list[dict[str, Any]] = []
    for pos, ztype in entries:
        placed = False
        for group in groups:
            if group["type"] != ztype:
                continue
            if any(distance(pos, cell) <= 1 for cell in group["cells"]):
                group["cells"].append(pos)
                placed = True
                break
        if not placed:
            groups.append({"type": ztype, "cells": [pos]})

    tasks: list[PlayerTask] = []
    for group in groups:
        cells = tuple(sorted(group["cells"]))
        tasks.append(PlayerTask(task_type=group["type"], cells=cells))
    return tasks


def merge_task_state(tasks: list[PlayerTask], rows: Any) -> list[PlayerTask]:
    """Attach ``playerTasks`` rows (cooldown/reward/valid) to clustered points."""
    rows = [as_dict(r) for r in as_list(rows)]
    merged: list[PlayerTask] = []
    for task in tasks:
        best = None
        for row in rows:
            rpos = Pos.load(row.get("taskPosition"))
            if rpos is None:
                continue
            if min(distance(rpos, c) for c in task.cells) <= 1:
                best = row
                break
        if best is None:
            merged.append(task)
            continue
        merged.append(PlayerTask(
            task_type=task.task_type,
            cells=task.cells,
            cooldown=max(0, as_int(best.get("coldDownRounds"), 0)),
            score_reward=as_int(best.get("scoreReward"), 0),
            gold_reward=as_int(best.get("goldReward"), 0),
            valid=as_bool(best.get("isValid"), False),
            timeout_rounds=max(0, as_int(best.get("timeoutRounds"), 0)),
            cold_down_reported=max(0, as_int(best.get("coldDownRounds"), 0)),
            reported=rpos,
        ))
    return merged


# ---------------------------------------------------------------------------
# world news / shops
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WorldNews:
    official: str = ""
    folk: str = ""

    @classmethod
    def load(cls, raw: Any) -> "WorldNews":
        obj = as_dict(raw)
        return cls(as_str(obj.get("officialNews")), as_str(obj.get("folkLegends")))


@dataclass(frozen=True, slots=True)
class Observation:
    """One fully parsed judge request."""

    round_no: int = 0
    day_index: int = 1
    phase_round: int = 0
    is_day: bool = True
    width: int = 41
    height: int = 32

    team_type: str = ""
    team_id: str = ""
    team_name: str = ""
    gold: int = 0
    total_score: int = 0

    zones: dict[Pos, str] = field(default_factory=dict)
    tasks: tuple[PlayerTask, ...] = ()
    roles: tuple[Unit, ...] = ()
    enemies: tuple[Unit, ...] = ()
    robots: tuple[Robot, ...] = ()

    phase_task: str = ""
    llm_response: str = ""
    last_cmd_result: str = ""
    last_action_results: dict[int, bool] = field(default_factory=dict)
    summon_result: int = 0
    news: WorldNews = field(default_factory=WorldNews)
    vendor: dict[str, int] = field(default_factory=dict)
    shop: dict[str, int] = field(default_factory=dict)
    errors: tuple[tuple[int, str], ...] = ()

    parse_notes: tuple[str, ...] = ()

    # -- convenience views -------------------------------------------------
    def mine_cells(self, kind: str | None = None) -> tuple[Pos, ...]:
        out = []
        for pos, ztype in self.zones.items():
            if ztype in R.MINE_KINDS and (kind is None or ztype == kind):
                out.append(pos)
        return tuple(sorted(out))

    def zone_cells(self, ztype: str) -> tuple[Pos, ...]:
        return tuple(sorted(p for p, z in self.zones.items() if z == ztype))

    def my_tasks(self) -> tuple[PlayerTask, ...]:
        return tuple(t for t in self.tasks if t.team == self.team_type)

    def enemy_tasks(self) -> tuple[PlayerTask, ...]:
        return tuple(t for t in self.tasks if t.team and t.team != self.team_type)

    def first_zone(self, ztype: str) -> Pos | None:
        for pos in sorted(self.zones):
            if self.zones[pos] == ztype:
                return pos
        return None

    def by_kind(self, kinds: Iterable[str]) -> tuple[Unit, ...]:
        wanted = set(kinds)
        return tuple(u for u in self.roles if u.kind in wanted and u.alive)

    def station(self) -> Unit | None:
        for unit in self.roles:
            if unit.kind == R.STATION:
                return unit
        return None

    def fighters(self) -> tuple[Unit, ...]:
        """Mobile units we may still command, in deterministic id order."""
        return tuple(sorted(
            (u for u in self.roles if u.is_mobile and u.alive),
            key=lambda u: u.unit_id,
        ))

    def towers(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            (u for u in self.roles if u.is_tower and u.alive),
            key=lambda u: (u.pos, u.unit_id),
        ))

    def walls(self) -> tuple[Unit, ...]:
        return self.by_kind((R.WALL,))

    def pioneer(self) -> Unit | None:
        for unit in self.fighters():
            if unit.kind == R.PIONEER:
                return unit
        return None

    def workers(self) -> tuple[Unit, ...]:
        return self.by_kind((R.WORKER,))

    def alive_robots(self) -> tuple[Robot, ...]:
        return tuple(r for r in self.robots if r.alive)

    def in_bounds(self, pos: Pos) -> bool:
        return 0 <= pos.x < self.width and 0 <= pos.y < self.height

    def buildable_terrain(self, pos: Pos) -> bool:
        """True when a building *could* stand here (in bounds, no neutral zone)."""
        return self.in_bounds(pos) and pos not in self.zones

    def enemy_roles(self) -> tuple[Unit, ...]:
        return tuple(u for u in self.enemies if u.alive)


def parse(payload: Any, error_sink: list[str] | None = None) -> Observation:
    """Total parse of a judge request.  Never raises."""
    notes: list[str] = []
    obj = as_dict(payload)
    if not obj:
        notes.append("payload:not_object")

    info = as_dict(obj.get("mapInfo"))
    team = as_dict(obj.get("teamOur"))

    width = max(1, as_int(info.get("width"), 41))
    height = max(1, as_int(info.get("height"), 32))
    round_no = as_int(obj.get("roundNo"), 0)
    phase_round = (round_no - 1) % R.ROUNDS_PER_DAY if round_no > 0 else 0
    day_index = ((round_no - 1) // R.ROUNDS_PER_DAY + 1) if round_no > 0 else 1
    is_day = phase_round < R.DAY_ROUNDS

    zones: dict[Pos, str] = {}
    task_entries: list[tuple[Pos, str]] = []
    for raw_zone in as_list(info.get("zones")):
        zone = as_dict(raw_zone)
        pos = Pos.load(zone.get("pos"))
        ztype = as_str(zone.get("neutralType"))
        if pos is None or not ztype:
            notes.append("zone:dropped")
            continue
        zones[pos] = ztype
        if ztype in R.TASK_ZONE_TYPES:
            task_entries.append((pos, ztype))

    team_type = as_str(team.get("type"))
    roles = tuple(
        u for u in (Unit.load(r, ours=True) for r in as_list(team.get("roles")))
        if u is not None
    )
    enemies = tuple(
        u for u in (Unit.load(r, ours=False)
                    for r in as_list(as_dict(obj.get("teamEnemy")).get("roles")))
        if u is not None
    )
    robots = tuple(
        r for r in (Robot.load(x)
                    for x in as_list(as_dict(obj.get("robot")).get("roles")))
        if r is not None
    )

    actions_raw = as_dict(obj.get("lastRoundRoleActionResults"))
    action_results: dict[int, bool] = {}
    for key, value in actions_raw.items():
        rid = as_int(key, -1)
        if rid >= 0:
            action_results[rid] = as_bool(value, False)

    vendor: dict[str, int] = {}
    for row in as_list(obj.get("vendorShopList")):
        entry = as_dict(row)
        name = as_str(entry.get("name"))
        if name:
            vendor[name] = as_int(entry.get("price"), 0)

    shop: dict[str, int] = {}
    for row in as_list(obj.get("weaponShopList")):
        entry = as_dict(row)
        name = as_str(entry.get("name"))
        if name:
            shop[name] = as_int(entry.get("price"), 0)

    errors: list[tuple[int, str]] = []
    for row in as_list(obj.get("errors")):
        entry = as_dict(row)
        errors.append((as_int(entry.get("errorCode"), 0),
                       as_str(entry.get("description"))))

    tasks = cluster_task_points(task_entries)
    tasks = merge_task_state(tasks, team.get("playerTasks"))
    if not team_type:
        notes.append("team:unknown_type")

    obs = Observation(
        round_no=round_no,
        day_index=day_index,
        phase_round=phase_round,
        is_day=is_day,
        width=width,
        height=height,
        team_type=team_type,
        team_id=as_str(team.get("teamId")),
        team_name=as_str(team.get("teamName")),
        gold=as_int(team.get("goldNum"), 0),
        total_score=as_int(team.get("totalScore"), 0),
        zones=zones,
        tasks=tuple(tasks),
        roles=roles,
        enemies=enemies,
        robots=robots,
        phase_task=as_str(obj.get("phaseTask")),
        llm_response=as_str(obj.get("llmResp")),
        last_cmd_result=as_str(obj.get("lastCmdResult")),
        last_action_results=action_results,
        summon_result=as_int(obj.get("lastSummonTreasureResult"), 0),
        news=WorldNews.load(obj.get("worldNews")),
        vendor=vendor,
        shop=shop,
        errors=tuple(errors),
        parse_notes=tuple(notes),
    )
    if error_sink is not None:
        error_sink.extend(notes)
    return obs
