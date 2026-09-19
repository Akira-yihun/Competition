"""Pathfinding and stand-off logic.

The baseline issued one full-grid A* per candidate stand cell per candidate
target (audit finding A18: 60 unreachable mines cost 1.87 s in a single turn,
37 % of the whole 5 s budget).  This implementation therefore:

* bounds every search with an expansion budget (``config.path_node_limit``),
* exposes a single ``distances_from`` multi-source BFS distance field so callers
  can rank many candidate targets from one search,
* supports a limited "soft step" onto cells currently held by robots
  (audit finding D7): when a character is boxed in by the wave it is usually
  better to contest a robot's cell -- both parties stop, which the rules treat as
  a mere 执行失败 -- than to stand still and never reach its tower.
"""
from __future__ import annotations

from collections import deque
from heapq import heappop, heappush
from itertools import count
from typing import Iterable

from .config import DEFAULT, Config
from .model import STEPS, Pos, Unit, distance
from .world import WorldView


def passable(world: WorldView, moving: Unit, pos: Pos,
             *, allow_robots: bool, avoid: frozenset[Pos] = frozenset()) -> bool:
    obs = world.obs
    if not obs.in_bounds(pos):
        return False
    if pos in obs.zones:                 # mine / vendor / shop / task point
        return False
    if pos == moving.pos:
        return True
    if pos in avoid:                     # soft no-go (e.g. the night-time middle)
        return False
    if pos in world.blocked_cells():
        return False
    if not allow_robots and pos in world.robot_cells():
        return False
    return True


def distances_from(world: WorldView, moving: Unit, sources: Iterable[Pos],
                   cfg: Config = DEFAULT,
                   *, allow_robots: bool = False,
                   avoid: frozenset[Pos] = frozenset()) -> dict[Pos, int]:
    """8-connected BFS distance field from ``sources``.

    Chebyshev movement is uniform-cost, so plain BFS is exact and much cheaper
    than one A* per candidate target.  Unreachable sources simply never appear.
    """
    origin = moving.pos
    out: dict[Pos, int] = {}
    budget = cfg.path_node_limit
    expansions = 0

    queue: deque[Pos] = deque()
    for source in sources:
        if source in out:
            continue
        # a source that is itself blocked is only usable if it is our own cell
        if source != origin and not passable(world, moving, source,
                                             allow_robots=allow_robots,
                                             avoid=avoid):
            continue
        out[source] = 0
        queue.append(source)

    while queue and expansions < budget:
        current = queue.popleft()
        expansions += 1
        depth = out[current]
        for dx, dy in STEPS:
            step = Pos(current.x + dx, current.y + dy)
            if step in out:
                continue
            if not passable(world, moving, step, allow_robots=allow_robots,
                            avoid=avoid):
                continue
            out[step] = depth + 1
            queue.append(step)
    return out


def next_step(world: WorldView, moving: Unit, goal: Pos,
              cfg: Config = DEFAULT) -> Pos | None:
    """First step of a shortest path towards ``goal`` (None if unreachable).

    The robot-avoiding search runs first; a robot-tolerant search is attempted
    only if the strict one fails.
    """
    if goal == moving.pos:
        return None                      # the baseline raised KeyError here
    path, _explored, _cost = search(world, moving, goal, cfg, allow_robots=False)
    if not path and cfg.path_soft_step_limit > 0:
        path, _explored, _cost = search(world, moving, goal, cfg,
                                       allow_robots=True)
    return path[1] if len(path) > 1 else None


def search(world: WorldView, moving: Unit, goal: Pos, cfg: Config,
           *, allow_robots: bool = False,
           avoid: frozenset[Pos] = frozenset(),
           branch_limit: int = 1) -> tuple[Pos | None, list[Pos], int]:
    """A* towards ``goal``.

    Returns ``(path, explored, cost)`` where ``path`` starts at the actor's cell
    and ends at the goal (empty when unreachable).  ``explored`` is the set of
    cells the search actually settled, which lets a caller pick a different goal
    and re-plan *once*, instead of paying for a fresh full-grid A* per candidate
    -- the baseline's behaviour, which cost ~2400 searches in a single turn.
    """
    obs = world.obs
    if goal == moving.pos:
        return [moving.pos], {moving.pos: 0}, 0
    if not obs.in_bounds(goal) or goal in obs.zones:
        return [], {moving.pos: 0}, -1
    if goal in avoid:
        return [], {moving.pos: 0}, -1
    if goal in world.blocked_cells():
        return [], {moving.pos: 0}, -1
    if not allow_robots and goal in world.robot_cells():
        return [], {moving.pos: 0}, -1

    order = count()
    frontier = [(distance(moving.pos, goal), 0, next(order), moving.pos)]
    came_from: dict[Pos, Pos] = {}
    best = {moving.pos: 0}
    seen: set[Pos] = set()
    expansions = 0

    while frontier and expansions < cfg.path_node_limit:
        _, cost, _, current = heappop(frontier)
        if current in seen:
            continue
        seen.add(current)
        expansions += 1
        if current == goal or expansions >= branch_limit and current == goal:
            return _rebuild(came_from, moving.pos, goal), best, cost
        for dx, dy in STEPS:
            step = Pos(current.x + dx, current.y + dy)
            if step in seen:
                continue
            if not passable(world, moving, step, allow_robots=allow_robots,
                            avoid=avoid):
                continue
            new_cost = cost + 1
            if new_cost >= best.get(step, new_cost + 1):
                continue
            best[step] = new_cost
            came_from[step] = current
            heappush(frontier,
                     (new_cost + distance(step, goal), new_cost, next(order), step))
    return [], best, -1


def _rebuild(came_from: dict[Pos, Pos], start: Pos, goal: Pos) -> list[Pos]:
    path = [goal]
    current = goal
    guard = 0
    while current != start:
        nxt = came_from.get(current)
        if nxt is None or guard > 8192:
            return []
        current = nxt
        path.append(current)
        guard += 1
    path.reverse()
    return path


def _first_step(came_from: dict[Pos, Pos], start: Pos, goal: Pos) -> Pos:
    current = goal
    guard = 0
    while came_from.get(current) != start:
        nxt = came_from.get(current)
        if nxt is None or guard > 4096:  # defensive: never loop on bad input
            return goal
        current = nxt
        guard += 1
    return current


def reachable_stands(world: WorldView, moving: Unit,
                     cells: Iterable[Pos]) -> tuple[Pos, ...]:
    """Stand cells next to ``cells`` that ``moving`` could actually occupy."""
    out: list[Pos] = []
    seen: set[Pos] = set()
    for cell in cells:
        for nb in cell.neighbours():
            if nb in seen:
                continue
            if nb != moving.pos and not passable(world, moving, nb,
                                                 allow_robots=False):
                continue
            seen.add(nb)
            out.append(nb)
    return tuple(out)


def ordered_stands(world: WorldView, moving: Unit, cells: Iterable[Pos],
                   *, exclude: frozenset[Pos] = frozenset(),
                   cfg: Config = DEFAULT) -> list[Pos]:
    """Stand cells ordered by travel distance (one BFS, not N searches)."""
    stands = [s for s in reachable_stands(world, moving, cells) if s not in exclude]
    if not stands:
        return []
    field = distances_from(world, moving, (moving.pos,), cfg)
    return sorted(
        stands,
        key=lambda p: (field.get(p, 10 ** 6), distance(moving.pos, p), p.x, p.y),
    )
