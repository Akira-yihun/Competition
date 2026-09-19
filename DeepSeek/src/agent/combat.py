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
from .model import (
    Observation, Pos, Robot, Unit, distance, footprint_distance,
)
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


def _priority(robot: Robot, base: tuple[Pos, ...] = ()) -> tuple:
    """Target order: **closest to the base first** (R2), then threat, then value.

    The user's rule is explicit: 优先攻击离基地最近的机器人.  The v1 ordering was
    score-per-HP, which happily left a small robot chewing the wall while the
    agent shelled a medium robot two ranks further back.

    Tie-breaks, in order: higher attack power (the boss hurts most), higher
    score-per-HP, then id for determinism.
    """
    proximity = footprint_distance(robot.pos, base) if base else 0
    stats = R.ROBOT_STATS.get(robot.kind, R.ROBOT_STATS[R.DEFAULT_ROBOT])
    return (proximity, -stats["attack"], -robot.value_per_hp(), robot.robot_id)


def base_cells(world_or_obs) -> tuple[Pos, ...]:
    """The station footprint, or a single fallback cell, for distance ranking."""
    cells = getattr(world_or_obs, "station_cells", None)
    if callable(cells):
        got = cells()
        if got:
            return tuple(got)
    station = getattr(world_or_obs, "station", None)
    if callable(station):
        unit = station()
        if unit is not None:
            return (unit.pos,)
    return ()


# ---------------------------------------------------------------------------
# per-weapon target search
# ---------------------------------------------------------------------------
def gatling_targets(tower: Unit, robots: tuple[Robot, ...],
                    ledger: DamageLedger, cfg: Config,
                    base: tuple[Pos, ...] = ()) -> list[Pos] | None:
    """One aim cell per bullet, all pairwise within a 90-degree cone."""
    want = max(1, tower.level)
    reach = tower.effective_range()
    in_range = [r for r in robots
                if distance(tower.pos, r.pos) <= reach and r.pos != tower.pos]
    if not in_range:
        return None
    in_range.sort(key=lambda r: _priority(r, base))

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
        value = _score_gatling(tower, stack, robots, ledger, base)
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
                   ledger: DamageLedger,
                   base: tuple[Pos, ...] = ()) -> float:
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
        total += _value_of_hit(first, 10, ledger, base)
    return total


def railgun_target(tower: Unit, robots: tuple[Robot, ...],
                   ledger: DamageLedger, cfg: Config,
                   base: tuple[Pos, ...] = ()) -> Pos | None:
    """Single aim cell; energy pierces along the line with chain depletion.

    Piercing means the *front* robot always eats the first chunk of energy, so a
    railgun naturally implements the "let the cheap front rank block the strong
    rear rank" idea from R2 without any extra rule.
    """
    reach = tower.effective_range()
    energy = 10 * max(1, tower.level)
    in_range = [r for r in robots
                if distance(tower.pos, r.pos) <= reach and r.pos != tower.pos]
    if not in_range:
        return None
    in_range.sort(key=lambda r: _priority(r, base))

    best: tuple[float, Pos] | None = None
    for robot in in_range[:cfg.aim_k]:
        value = _score_railgun(tower.pos, robot.pos, robots, energy, ledger, base)
        if best is None or value > best[0]:
            best = (value, robot.pos)
    if best is None or best[0] <= 0:
        return None
    return best[1]


def _score_railgun(origin: Pos, aim: Pos, robots: tuple[Robot, ...],
                   energy: int, ledger: DamageLedger,
                   base: tuple[Pos, ...] = ()) -> float:
    total = 0.0
    remaining = energy
    for robot in robots_on_line(origin, aim, robots):
        if remaining <= 0:
            break
        dealt = min(remaining, robot.health)
        total += _value_of_hit(robot, dealt, ledger, base)
        remaining -= dealt
    return total


def rocket_targets(tower: Unit, robots: tuple[Robot, ...],
                   ledger: DamageLedger, cfg: Config,
                   base: tuple[Pos, ...] = (),
                   front: tuple[int, int] = (0, 0)) -> list[Pos] | None:
    """One aim cell per missile, aimed at the *second* rank when that is better.

    R2, verbatim: 火箭炮对周围八格都有溅射伤害 … 如果后面还有大批敌人（例如后面
    还有两排）则可以把火力中心稍微向后移 … 前排的机器人通常是较低级别，后面是较
    高级别，可以从第二排开始攻击，这样前面的低级机器人能挡住后面攻击力更强的机
    器人的路.

    So the anchor is the live robot closest to the base; candidate impact cells
    are the anchor and its eight neighbours; a candidate that sits one cell
    further towards the map interior earns ``rocket_rear_bonus`` whenever the
    rear rank is crowded or holds a non-small robot.  The anchor keeps taking
    splash (10), which is exactly the intent: it survives and keeps blocking.
    When the anchor is already at the base's doorstep the direct hit wins
    outright -- blocking value is worthless once the wall is being chewed.

    Missiles are re-aimed against a local damage ledger, so a level-2/3 rocket
    stacks onto a boss instead of wasting a second missile on a corpse; the task
    book explicitly allows overlapping impacts and adds their damage.
    """
    want = max(1, tower.level)
    reach = tower.effective_range()
    in_range = [r for r in robots
                if distance(tower.pos, r.pos) <= reach and r.pos != tower.pos]
    if not in_range:
        return None
    in_range.sort(key=lambda r: _priority(r, base))

    local: dict[int, int] = {}
    chosen: list[Pos] = []
    for _ in range(want):
        live = [r for r in in_range
                if r.health - local.get(r.robot_id, 0) > 0]
        anchor = live[0] if live else None
        if anchor is None:
            break
        impact = _rocket_impact(tower, anchor, live, local, cfg, base, front)
        if impact is None:
            break
        chosen.append(impact)
        for robot in live:
            d = distance(robot.pos, impact)
            if d <= 1:
                dealt = 20 if d == 0 else 10
                local[robot.robot_id] = local.get(robot.robot_id, 0) + dealt
    if not chosen:
        return None
    if len(chosen) < want:
        # Fewer distinct anchors than missiles: repeat the last choice (overlap
        # damage stacks per 任务书 §4.5.4.4) rather than under-sending the
        # target count the protocol mandates.
        chosen.extend([chosen[-1]] * (want - len(chosen)))
    return chosen


def _rocket_impact(tower: Unit, anchor: Robot, live: list[Robot],
                   local: dict[int, int], cfg: Config,
                   base: tuple[Pos, ...],
                   front: tuple[int, int]) -> Pos | None:
    reach = tower.effective_range()
    candidates: list[Pos] = []
    seen: set[tuple[int, int]] = set()
    for cell in (anchor.pos, *anchor.pos.neighbours()):
        if not tower_ok_aim(tower.pos, cell, reach):
            continue
        key = (cell.x, cell.y)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(cell)
    if not candidates:
        return None

    anchor_prox = footprint_distance(anchor.pos, base) if base else 99
    urgent = anchor_prox <= cfg.rocket_urgent_radius
    best_key: tuple[float, int, int, int] | None = None
    best_cell: Pos | None = None
    for cell in candidates:
        score = _score_rocket(cell, live, local)
        if cell == anchor.pos:
            if urgent:
                score += 1000.0            # the wall is being eaten: hit it now
        else:
            rear = ((cell.x - anchor.pos.x) * front[0]
                    + (cell.y - anchor.pos.y) * front[1]) > 0
            if rear:
                hit = [r for r in live if distance(r.pos, cell) <= 1]
                strong = any(r.kind != R.SMALL_ROBOT for r in hit)
                if strong or len(hit) >= cfg.rocket_rear_min_cluster:
                    score += cfg.rocket_rear_bonus
        # Deterministic: score first, then whichever impact sits closest to the
        # base, then coordinates.
        key = (score, -_proximity(cell, base), -cell.x, -cell.y)
        if best_key is None or key > best_key:
            best_key = key
            best_cell = cell
    return best_cell


def _proximity(pos: Pos, base: tuple[Pos, ...]) -> int:
    if not base:
        return 0
    return footprint_distance(pos, base)


def tower_ok_aim(origin: Pos, aim: Pos, reach: int) -> bool:
    return distance(origin, aim) <= reach


def _score_rocket(aim: Pos, robots: tuple[Robot, ...] | list[Robot],
                  local: dict[int, int] | None = None) -> float:
    """Expected value of one missile: damage capped at remaining HP, plus a kill.

    ``local`` is the per-turn damage ledger for *this* tower, so a second missile
    re-evaluates against what the first one already destroyed.  Finishing a robot
    adds a flat bonus so a missile prefers a real kill over the same amount of
    chip damage (score_2 only counts kills, 任务书 §6).
    """
    total = 0.0
    for robot in robots:
        remaining = robot.health - (local or {}).get(robot.robot_id, 0)
        if remaining <= 0:
            continue
        d = distance(robot.pos, aim)
        damage = 20 if d == 0 else (10 if d == 1 else 0)
        if damage <= 0:
            continue
        dealt = min(damage, remaining)
        total += dealt
        if dealt >= remaining:
            total += 15.0
    return total


def _value_of_hit(robot: Robot, damage: int, ledger: DamageLedger,
                  base: tuple[Pos, ...] = ()) -> float:
    """Kill points when the hit completes a kill, otherwise discounted threat."""
    total, kill = ledger.project(robot, damage)
    proximity = footprint_distance(robot.pos, base) if base else 99
    urgency = max(0.0, 6.0 - proximity) * 0.05   # nearest-to-base bias
    if kill:
        return float(robot.score) + urgency
    if robot.dizzy:
        damage *= 0.25
    return damage / max(1, robot.health) * robot.score * 0.5 + urgency


# ---------------------------------------------------------------------------
# turn-level combat plan
# ---------------------------------------------------------------------------
def tower_cooldown(tower: Unit, round_no: int, cooling: dict[int, int]) -> int:
    """Rounds of cooldown left, from local memory (payload cooldown is unusable)."""
    local = cooling.get(tower.unit_id, 0) - round_no
    reported = tower.cooldown if tower.kind == R.ROCKET else 0
    return max(local, reported)


def aim_for(tower: Unit, robots: tuple[Robot, ...], ledger: DamageLedger,
            cfg: Config, base: tuple[Pos, ...] = (),
            front: tuple[int, int] = (0, 0)) -> list[Pos] | None:
    if tower.kind == R.GATLING:
        return gatling_targets(tower, robots, ledger, cfg, base)
    if tower.kind == R.RAILGUN:
        aim = railgun_target(tower, robots, ledger, cfg, base)
        return [aim] if aim is not None else None
    if tower.kind == R.ROCKET:
        return rocket_targets(tower, robots, ledger, cfg, base, front)
    return None


def record_cooldown(tower: Unit, round_no: int, cooling: dict[int, int]) -> None:
    if tower.kind == R.ROCKET:
        # 3-round empty window: rounds r+1, r+2, r+3 are blocked (local referee
        # models this as cooldown = 4 on the firing round)
        cooling[tower.unit_id] = round_no + R.ROCKET_COOLDOWN + 1


def bomb_cluster(obs: Observation, robots: tuple[Robot, ...], cfg: Config,
                 base: tuple[Pos, ...] = ()) -> Pos | None:
    """A 3x3 cell hitting enough robots to justify a 100-gold Bomb."""
    if not robots or obs.gold < R.SHOP_PRICE[R.BOMB]:
        return None
    best: tuple[int, int, Pos] | None = None
    for robot in robots:
        for cell in (robot.pos, *robot.pos.neighbours()):
            if not obs.in_bounds(cell):
                continue
            count = sum(1 for r in robots if distance(r.pos, cell) <= 1)
            if count < cfg.bomb_min_cluster:
                continue
            prox = footprint_distance(cell, base) if base else 0
            key = (count, -prox, cell)
            if best is None or key > best:
                best = key
    return best[2] if best else None


def dizzy_target(obs: Observation, robots: tuple[Robot, ...],
                 cfg: Config, base: tuple[Pos, ...] = ()) -> Pos | None:
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
    candidates.sort(key=lambda r: _priority(r, base))
    return candidates[0].pos
