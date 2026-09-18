from dataclasses import dataclass, field
from typing import Any
from .rules import *

@dataclass(frozen=True, slots=True)
class PlayerTask:
    position: "Pos"
    valid: bool
    score_reward: int
    gold_reward: int


@dataclass(frozen=True, slots=True)
class Pos:
    x: int
    y: int

    @classmethod
    def load(cls, raw: Any) -> "Pos":
        return cls(int(raw["x"]), int(raw["y"]))

    def dump(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}


def distance(first: Pos, second: Pos) -> int:
    return max(abs(first.x - second.x), abs(first.y - second.y))


def station_footprint(pos: Pos) -> tuple[Pos, ...]:
    return (
        pos,
        Pos(pos.x + 1, pos.y),
        Pos(pos.x, pos.y - 1),
        Pos(pos.x + 1, pos.y - 1),
    )


@dataclass(frozen=True, slots=True)
class Unit:
    unit_id: int
    pos: Pos
    kind: str
    health: int
    level: int
    cooldown: int
    attack_range: int
    capacity: int | None
    backpack: tuple[str, ...]

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Unit":
        raw_capacity = raw.get("backPackCapability")
        return cls(
            int(raw.get("id") or 0),
            Pos.load(raw["pos"]),
            str(raw["roleType"]),
            int(raw["health"]),
            int(raw.get("level") or 0),
            int(raw.get("cooldown") or 0),
            int(raw.get("attackRange") or 0),
            int(raw_capacity) if raw_capacity is not None else None,
            tuple(str(item) for item in raw.get("backpack") or ()),
        )

    @property
    def backpack_full(self) -> bool:
        if self.capacity is None:
            return False
        return len(self.backpack) >= self.capacity

    def range_of_attack(self) -> int:
        table = TOWER_RANGE_BY_LEVEL.get(self.kind)
        if table is None:
            return 0
        level = min(max(self.level, 1), len(table))
        documented = table[level - 1]
        return min(self.attack_range, documented) if 0 < self.attack_range < 2**31 - 1 else documented


@dataclass(frozen=True, slots=True)
class Robot:
    robot_id: int
    pos: Pos
    health: int
    target_team: str = ""
    kind: str = "smallRobot"
    abnormal_state: str = ""

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Robot":
        return cls(int(raw["id"]), Pos.load(raw["pos"]), int(raw["health"]),
                   str(raw.get("targetTeam") or ""), str(raw.get("roleType") or "smallRobot"),
                   str(raw.get("abnormalState") or ""))


@dataclass(frozen=True, slots=True)
class Turn:
    round_no: int
    is_day: bool
    gold: int
    width: int
    height: int
    zones: dict[Pos, str]
    ours: tuple[Unit, ...]
    enemies: tuple[Unit, ...]
    robots: tuple[Robot, ...]
    tasks: tuple[PlayerTask, ...]
    phase_task: str
    llm_response: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, payload: dict[str, Any]) -> "Turn":
        round_no = int(payload["roundNo"])
        info = payload["mapInfo"]
        team = payload["teamOur"]
        return cls(
            round_no,
            (round_no - 1) % ROUNDS_PER_DAY < DAY_ROUNDS,
            int(team.get("goldNum") or 0),
            int(info["width"]),
            int(info["height"]),
            {
                Pos.load(zone["pos"]): str(zone["neutralType"])
                for zone in info.get("zones") or ()
            },
            tuple(Unit.load(role) for role in team.get("roles") or ()),
            tuple(
                Unit.load(role)
                for role in (payload.get("teamEnemy") or {}).get("roles") or ()
            ),
            tuple(
                Robot.load(robot)
                for robot in (payload.get("robot") or {}).get("roles") or ()
            ),
            tuple(
                PlayerTask(
                    Pos.load(task["taskPosition"]),
                    bool(task.get("isValid")),
                    int(task.get("scoreReward") or 0),
                    int(task.get("goldReward") or 0),
                )
                for task in team.get("playerTasks") or ()
            ),
            str(payload.get("phaseTask") or ""),
            str(payload.get("llmResp") or ""),
            payload,
        )

    def station(self) -> Unit | None:
        for unit in self.ours:
            if unit.kind == STATION and unit.health > 0:
                return unit
        return None

    def alive(self, kinds: tuple[str, ...]) -> tuple[Unit, ...]:
        return tuple(
            unit for unit in self.ours
            if unit.kind in kinds and unit.health > 0
        )

    def controllable(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive(CONTROLLABLE_TYPES), key=lambda unit: unit.unit_id,
        ))

    def workers(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive((WORKER,)), key=lambda unit: unit.unit_id,
        ))

    def weapons(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive(TOWER_TYPES),
            key=lambda unit: (unit.pos.x, unit.pos.y),
        ))

    def walls(self) -> tuple[Unit, ...]:
        return self.alive((WALL,))

    def stone_mines(self) -> tuple[Pos, ...]:
        return tuple(
            pos for pos, kind in self.zones.items() if kind == WALL_MATERIAL
        )

    def mines(self) -> tuple[Pos, ...]:
        return tuple(
            pos for pos, kind in self.zones.items()
            if kind in {"stone", "iron", "copper"}
        )

    def vendor(self) -> Pos | None:
        return next((pos for pos, kind in self.zones.items() if kind == "vendor"), None)

    def footprint(self, unit: Unit) -> tuple[Pos, ...]:
        if unit.kind == STATION:
            return station_footprint(unit.pos)
        return (unit.pos,)

    def land(self, pos: Pos) -> bool:
        if not 0 <= pos.x < self.width or not 0 <= pos.y < self.height:
            return False
        return self.zones.get(pos, LAND) == LAND

    def occupied_cells(self) -> frozenset[Pos]:
        cells: set[Pos] = set()
        for unit in self.ours:
            if unit.health > 0:
                cells.update(self.footprint(unit))
        for unit in self.enemies:
            if unit.health > 0:
                cells.update(self.footprint(unit))
        return frozenset(cells)

    def blocked(self, moving: Unit) -> frozenset[Pos]:
        cells = {pos for pos, kind in self.zones.items() if kind != LAND}
        cells.update(self.occupied_cells())
        cells.discard(moving.pos)
        for robot in self.robots:
            if robot.health > 0:
                cells.add(robot.pos)
        return frozenset(cells)



Observation = Turn
