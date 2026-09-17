"""Combat: ballistics, target selection and firing legality.

Three things the baseline got wrong and that this module fixes:

* **Target count.**  接口文档 §2.2 requires ``len(targetPos)`` to equal the
  weapon level for gatling/rocket and 1 for railgun.  The baseline always sent
  exactly one, which a strict judge reads as 指令错误.
* **Range.**  The sample reports L1 ranges of 4 / 7 / INT32_MAX while 任务书
  §4.5.1 tabulates 3 / 6 / 10, so the effective range is the *smaller* of the
  two (see ``Unit.effective_range``); firing beyond the documented value risks
  an out-of-range target.
* **Cooldown.**  The sample carries no ``cooldown`` field at all, so the payload
  value is always 0 and the baseline fired the rocket through its 3-round
  window.  Cooldown is tracked locally in ``SessionState.cooling``.

Policy on "not enough targets": we emit exactly ``level`` **distinct** aim cells,
preferring real robot cells and then extensions along the same ballistic line.
If fewer are available we **do not fire at all** -- sending too few is a
potential 指令错误 (five of those end the match) whereas not firing merely costs
one action.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .model import Observation, Pos, Robot, Unit, distance
from . import rules as R


# ---------------------------------------------------------------------------
# ballistics
# ---------------------------------------------------------------------------
def line_cells(origin: Pos, target: Pos,
               limit: int = 64) -> tuple[Pos, ...]:
    """Cells on the segment from ``origin`` to ``target`` (Bresenham).

    任务书 §4.5.4.3: the attack path is the straight line between the two cell
    centres; a bullet consumes itself on the nearest robot on that path.  Exact
    tie-breaking on ambiguous diagonals is not specified, so callers treat the
    ordering as an estimate rather than ground truth.
    """
    x0, y0 = origin.x, origin.y
    x1, y1 = target.x, target.y
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x1 > x0 else (-1 if x1 < x0 else 0)
    sy = 1 if y1 > y0 else (-1 if y1 < y0 else 0)
    err = dx - dy
    cells: list[Pos] = []
    x, y = x0, y0
    while True:
        cells.append(Pos(x, y))
        if (x, y) == (x1, y1) or len(cells) >= limit:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy
    # drop the origin cell itself: the weapon does not shoot its own cell
    return tuple(cells[1:])


def robots_on_line(origin: Pos, target: Pos, robots: tuple[Robot, ...]) -> list[Robot]:
    path = line_cells(origin, target)
    index = {pos: i for i, pos in enumerate(path)}
    hits = [r for r in robots if r.pos in index]
    return sorted(hits, key=lambda r: (index[r.pos], r.robot_id))


@dataclass
class DamageLedger:
    """Shared expected-damage accounting for one turn.

    Damage settles at end of turn, so several weapons may combine to kill one
    robot; the ledger lets each shot know whether it completes a kill.
    """

    incoming: dict[int, int] = field(default_factory=dict)

    def project(self, robot: Robot, damage: int) -> tuple[int, bool]:
        already = self.incoming.get(robot.robot_id, 0)
        total = already + damage
        kill = total >= robot.health
        return total, kill

    def commit(self, robot_id: int, damage: int) -> None:
        self.incoming[robot_id] = self.incoming.get(robot_id, 0) + damage


def _priority(robot: Robot) -> tuple:
    """任务书 §4.7.2 score: medium 0.0333 > small 0.0250 > boss 0.0125 > large 0.0080.

    Ordered by score-per-HP so the cheapest kill is taken first, with the raw
    score as a tie-break and id for determinism.
    """
    return (-robot.value_per_hp(), -robot.score, robot.robot_id)


# ---------------------------------------------------------------------------
# per-weapon target search
# ---------------------------------------------------------------------------
def gatling_targets(tower: Unit, robots: tuple[Robot, ...],
                    ledger: DamageLedger, cfg: Config) -> list[Pos] | None:
    """One aim cell per bullet, all pairwise within a 90-degree cone."""
    want = max(1, tower.level)
    reach = tower.effective_range()
    in_range = [r for r in robots
                if distance(tower.pos, r.pos) <= reach and r.pos != tower.pos]
    if not in_range:
        return None
    in_range.sort(key=_priority)

    # candidate aim cells: robot cells first, then extension cells along the
    # same ballistic line (a bullet still hits a robot further down that line)
    first_choices: list[list[Pos]] = []
    for robot in in_range[:cfg.aim_k]:
        cells = [robot.pos]
        for extension in line_cells(tower.pos, robot.pos)[1:3]:
            if extension != tower.pos and distance(tower.pos, extension) <= reach:
                cells.append(extension)
        first_choices.append(cells)
    if not first_choices:
        return None

    best: tuple[float, list[Pos]] | None = None
    stack: list[Pos] = []

    def consider() -> None:
        nonlocal best
        if not stack:
            return
        if len({(p.x, p.y) for p in stack}) != len(stack):
            return
        if not _cone_ok(tower.pos, stack):
            return
        value = _score_gatling(tower, stack, robots, ledger)
        if best is None or value > best[0]:
            best = (value, list(stack))

    def walk(idx: int) -> None:
        if len(stack) == want:
            consider()
            return
        if idx >= len(first_choices):
            return
        for cell in first_choices[idx]:
            stack.append(cell)
            walk(idx + 1)
            stack.pop()
        walk(idx + 1)                      # skip this robot entirely

    walk(0)
    if best is None:
        return None
    return best[1]


def _cone_ok(origin: Pos, positions: list[Pos]) -> bool:
    vectors = [(p.x - origin.x, p.y - origin.y) for p in positions]
    for i, a in enumerate(vectors):
        for b in vectors[i + 1:]:
            if a[0] * b[0] + a[1] * b[1] < 0:
                return False
    return True


def _score_gatling(tower: Unit, aims: list[Pos], robots: tuple[Robot, ...],
                   ledger: DamageLedger) -> float:
    total = 0.0
    seen_robots: dict[int, int] = {}
    for aim in aims:
        hits = robots_on_line(tower.pos, aim, robots)
        if not hits:
            continue
        first = hits[0]
        if first.robot_id in seen_robots:
            continue                        # the same robot cannot be hit twice
        seen_robots[first.robot_id] = 10
        total += _value_of_hit(first, 10, ledger)
    return total


def railgun_target(tower: Unit, robots: tuple[Robot, ...],
                   ledger: DamageLedger, cfg: Config) -> Pos | None:
    """Single aim cell; energy pierces along the line with chain depletion."""
    reach = tower.effective_range()
    energy = 10 * max(1, tower.level)
    in_range = [r for r in robots
                if distance(tower.pos, r.pos) <= reach and r.pos != tower.pos]
    if not in_range:
        return None
    in_range.sort(key=_priority)

    best: tuple[float, Pos] | None = None
    for robot in in_range[:cfg.aim_k]:
        value = _score_railgun(tower.pos, robot.pos, robots, energy, ledger)
        if best is None or value > best[0]:
            best = (value, robot.pos)
    if best is None or best[0] <= 0:
        return None
    return best[1]


def _score_railgun(origin: Pos, aim: Pos, robots: tuple[Robot, ...],
                   energy: int, ledger: DamageLedger) -> float:
    total = 0.0
    remaining = energy
    for robot in robots_on_line(origin, aim, robots):
        if remaining <= 0:
            break
        dealt = min(remaining, robot.health)
        total += _value_of_hit(robot, dealt, ledger)
        remaining -= dealt
    return total


def rocket_targets(tower: Unit, robots: tuple[Robot, ...],
                   ledger: DamageLedger, cfg: Config) -> list[Pos] | None:
    """One aim cell per missile; distinct cells (duplicate-aim semantics are undefined)."""
    want = max(1, tower.level)
    reach = tower.effective_range()
    in_range = [r for r in robots if distance(tower.pos, r.pos) <= reach]
    if not in_range:
        return None
    in_range.sort(key=_priority)

    candidates: list[Pos] = []
    seen: set[tuple[int, int]] = set()
    for robot in in_range[: cfg.aim_k]:
        for cell in (robot.pos, *robot.pos.neighbours()):
            if not tower_ok_aim(tower.pos, cell, reach):
                continue
            key = (cell.x, cell.y)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(cell)
    if not candidates:
        return None

    scored = sorted(
        ((_score_rocket(cell, robots, ledger), cell.x, cell.y, cell)
         for cell in candidates),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    if not scored or scored[0][0] <= 0:
        return None
    chosen = [item[3] for item in scored[:want]]
    if len(chosen) < want:
        return None
    return chosen


def tower_ok_aim(origin: Pos, aim: Pos, reach: int) -> bool:
    return distance(origin, aim) <= reach


def _score_rocket(aim: Pos, robots: tuple[Robot, ...],
                  ledger: DamageLedger) -> float:
    total = 0.0
    for robot in robots:
        d = distance(robot.pos, aim)
        if d == 0:
            total += _value_of_hit(robot, 20, ledger)
        elif d == 1:
            total += _value_of_hit(robot, 10, ledger)
    return total


def _value_of_hit(robot: Robot, damage: int, ledger: DamageLedger) -> float:
    """Kill points when the hit completes a kill, otherwise discounted threat."""
    total, kill = ledger.project(robot, damage)
    if kill:
        return float(robot.score)
    if robot.dizzy:
        damage *= 0.25
    return damage / max(1, robot.health) * robot.score * 0.5


# ---------------------------------------------------------------------------
# turn-level combat plan
# ---------------------------------------------------------------------------
def tower_cooldown(tower: Unit, round_no: int, cooling: dict[int, int]) -> int:
    """Rounds of cooldown left, from local memory (payload cooldown is unusable)."""
    local = cooling.get(tower.unit_id, 0) - round_no
    reported = tower.cooldown if tower.kind == R.ROCKET else 0
    return max(local, reported)


def aim_for(tower: Unit, robots: tuple[Robot, ...], ledger: DamageLedger,
            cfg: Config) -> list[Pos] | None:
    if tower.kind == R.GATLING:
        return gatling_targets(tower, robots, ledger, cfg)
    if tower.kind == R.RAILGUN:
        aim = railgun_target(tower, robots, ledger, cfg)
        return [aim] if aim is not None else None
    if tower.kind == R.ROCKET:
        return rocket_targets(tower, robots, ledger, cfg)
    return None


def record_cooldown(tower: Unit, round_no: int, cooling: dict[int, int]) -> None:
    if tower.kind == R.ROCKET:
        # 3-round empty window: rounds r+1, r+2, r+3 are blocked (local referee
        # models this as cooldown = 4 on the firing round)
        cooling[tower.unit_id] = round_no + R.ROCKET_COOLDOWN + 1


def bomb_cluster(obs: Observation, robots: tuple[Robot, ...], cfg: Config) -> Pos | None:
    """A 3x3 cell hitting enough robots to justify a 100-gold Bomb."""
    if not robots or obs.gold < R.SHOP_PRICE[R.BOMB]:
        return None
    best: tuple[int, Pos] | None = None
    for robot in robots:
        for cell in (robot.pos, *robot.pos.neighbours()):
            if not obs.in_bounds(cell):
                continue
            count = sum(1 for r in robots if distance(r.pos, cell) <= 1)
            if count < cfg.bomb_min_cluster:
                continue
            if best is None or count > best[0]:
                best = (count, cell)
    return best[1] if best else None


def dizzy_target(obs: Observation, robots: tuple[Robot, ...],
                 cfg: Config) -> Pos | None:
    """Dizzy the scariest non-dizzy robot when it is healthy enough to matter."""
    if obs.gold < R.SHOP_PRICE[R.DIZZY_WEAPON]:
        return None
    candidates = [
        r for r in robots
        if not r.dizzy and r.kind in (R.LARGE_ROBOT, R.BOSS_ROBOT)
        and r.health >= r.base_hp * cfg.boss_dizzy_hp_ratio
    ]
    if not candidates:
        return None
    candidates.sort(key=_priority)
    return candidates[0].pos
