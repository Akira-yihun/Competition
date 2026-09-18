"""World model: derived map facts, threat assessment and build-site knowledge.

Everything here is derived from one :class:`Observation`.  The only stateful
piece is :class:`SiteKnowledge`, which remembers which build cells the judge has
actually accepted -- see the note on build zones below.

**Why build sites are learned, not computed.**  任务书 §4.1 says the blue
(weapons-only) and yellow (walls-only) build regions are assigned by the system,
but 接口文档 §1.2.1's ``neutralType`` is a closed 9-value enum with no buildable
marker, and the sample request confirms none is transmitted.  The 8x8 diagram in
the task book yields no coordinate formula, and the walls in the only real sample
sit at Chebyshev distance 5 from the base, which contradicts every reading of
"weapons at distance 1 / walls at distance 2".  So the ring geometry below is a
**calculated guess**, used only to order candidates; the ground truth comes from
observing whether a build was accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import DEFAULT, Config
from .model import (
    Observation, Pos, PlayerTask, Robot, Unit, distance, footprint_distance,
    station_footprints,
)
from . import rules as R

UNKNOWN = "unknown"
LEGAL = "legal"
ILLEGAL = "illegal"


@dataclass
class SiteKnowledge:
    """Per-match record of which build cells the judge accepted.

    ``false`` in ``lastRoundRoleActionResults`` is the *union* of every failure
    mode, so a cell is only marked ILLEGAL after a completed attempt in which
    every precondition was independently verified (see ``world.attempt_ok``).
    """

    status: dict[tuple[int, int, str], str] = field(default_factory=dict)
    failures: dict[tuple[int, int, str], int] = field(default_factory=dict)
    legal_sites: set[tuple[int, int]] = field(default_factory=set)
    probes_used: int = 0
    pattern: frozenset[tuple[int, int]] | None = None

    def key(self, pos: Pos, name: str) -> tuple[int, int, str]:
        return (pos.x, pos.y, name)

    def status_of(self, pos: Pos, name: str) -> str:
        return self.status.get(self.key(pos, name), UNKNOWN)

    def mark_legal(self, pos: Pos, name: str) -> None:
        self.status[self.key(pos, name)] = LEGAL
        self.legal_sites.add((pos.x, pos.y))

    def note_failure(self, pos: Pos, name: str, limit: int) -> bool:
        """Record a verified failed attempt; True when the cell is now ILLEGAL."""
        key = self.key(pos, name)
        count = self.failures.get(key, 0) + 1
        self.failures[key] = count
        if count >= limit:
            self.status[key] = ILLEGAL
            return True
        return False

    def note_probe(self) -> None:
        self.probes_used += 1

    def probe_available(self, quota: int) -> bool:
        return self.probes_used < quota

    def infer_pattern(self, anchor: Pos) -> None:
        """Derive the accepted offset set from >= 2 confirmed cells."""
        if len(self.legal_sites) < 2:
            return
        self.pattern = frozenset(
            (x - anchor.x, y - anchor.y) for x, y in self.legal_sites
        )

    def is_illegal(self, pos: Pos, name: str) -> bool:
        return self.status_of(pos, name) == ILLEGAL

    def reset(self) -> None:
        self.status.clear()
        self.failures.clear()
        self.legal_sites.clear()
        self.pattern = None
        self.probes_used = 0


@dataclass
class WorldView:
    obs: Observation
    cfg: Config = DEFAULT
    knowledge: SiteKnowledge = field(default_factory=SiteKnowledge)
    #: tower id -> round from which it may fire again.  The payload's ``cooldown``
    #: field is absent from the sample request, so the rocket's 3-round window has
    #: to be tracked locally or it fires through its own cooldown.
    cooling: dict[int, int] = field(default_factory=dict)
    #: tower cells we issued a build for but have not yet observed standing
    pending_tower_sites: list[Pos] = field(default_factory=list)
    #: what the world model learned this turn (e.g. a refused build site)
    notes: list[str] = field(default_factory=list)
    _blocked: frozenset[Pos] | None = field(default=None, repr=False)
    _robot_cells: frozenset[Pos] | None = field(default=None, repr=False)

    # -- station geometry --------------------------------------------------
    def station(self) -> Unit | None:
        return self.obs.station()

    def station_anchor(self) -> Pos | None:
        station = self.station()
        return station.pos if station is not None else None

    def station_cells(self) -> tuple[Pos, ...]:
        """Documented footprint first, other reading appended.

        Only the upper-left reading is consistent with the sample's three
        towers; the lower-left reading is kept as a secondary candidate so that
        blocking stays conservative (we never path *into* our own base).
        """
        anchor = self.station_anchor()
        if anchor is None:
            return ()
        variants = station_footprints(anchor)
        primary = tuple(p for p in variants[0] if self.obs.in_bounds(p))
        extra = tuple(p for p in variants[1]
                      if self.obs.in_bounds(p) and p not in primary)
        return primary + extra

    def blocked_cells(self) -> frozenset[Pos]:
        """Buildings (both sides) and our own units.  Cached per WorldView."""
        cached = self._blocked
        if cached is None:
            cells: set[Pos] = set()
            for unit in self.obs.roles:
                cells.update(self.unit_cells(unit))
            for unit in self.obs.enemies:
                cells.update(self.unit_cells(unit))
            cached = frozenset(cells)
            self._blocked = cached
        return cached

    def robot_cells(self) -> frozenset[Pos]:
        cached = self._robot_cells
        if cached is None:
            cached = frozenset(r.pos for r in self.obs.robots if r.alive)
            self._robot_cells = cached
        return cached

    def unit_cells(self, unit: Unit) -> tuple[Pos, ...]:
        if unit.kind == R.STATION:
            anchor = unit.pos
            primary = tuple(p for p in station_footprints(anchor)[0]
                            if self.obs.in_bounds(p))
            return primary or (anchor,)
        return (unit.pos,)

    # -- build sites -------------------------------------------------------
    def build_candidates(self, kind: str) -> tuple[Pos, ...]:
        """Ordered candidate cells for building ``kind`` ('wall' or a tower)."""
        anchor = self.station_anchor()
        if anchor is None:
            return ()
        name = R.BUILD_NAME.get(kind, kind)
        footprint = self.station_cells()
        blocked = self.blocked_cells()
        robot_cells = {r.pos for r in self.obs.robots if r.alive}

        rings = (1, 2) if kind != R.WALL else (2, 3)
        candidates: list[Pos] = []
        for radius in rings:
            for cell in self._ring(anchor, radius):
                if not self.obs.buildable_terrain(cell):
                    continue
                if cell in blocked or cell in robot_cells:
                    continue
                if self.knowledge.is_illegal(cell, name):
                    continue
                candidates.append(cell)

        def rank(cell: Pos) -> tuple:
            status = self.knowledge.status_of(cell, name)
            hint = 0 if status == LEGAL else 1
            if self.knowledge.pattern is not None:
                offset = (cell.x - anchor.x, cell.y - anchor.y)
                if offset in self.knowledge.pattern:
                    hint = 0 if status == LEGAL else 1
                elif status != LEGAL:
                    hint = 2
            # towers hug the base, walls form the outer ring
            return (hint, footprint_distance(cell, footprint), cell.x, cell.y)

        candidates.sort(key=rank)
        return tuple(candidates)

    def _ring(self, anchor: Pos, radius: int) -> tuple[Pos, ...]:
        return anchor.ring(radius)

    def wall_plan(self, cfg: Config = DEFAULT) -> tuple[Pos, ...]:
        """Cells that should eventually hold a wall (outer ring, one gate)."""
        anchor = self.station_anchor()
        if anchor is None or not cfg.wall_ring_enabled:
            return ()
        footprint = self.station_cells()
        cells = [p for p in self._ring(anchor, 2) if self.obs.buildable_terrain(p)]
        cells.sort(key=lambda p: (footprint_distance(p, footprint), p.x, p.y))
        cells = cells[:cfg.wall_ring_limit]
        if cfg.wall_keep_entrance and cells:
            # leave one gate on the side pointing at the map interior so our
            # own characters are never sealed in
            gate = min(cells, key=lambda p: (abs(p.x - self.obs.width / 2)
                                             + abs(p.y - self.obs.height / 2)))
            cells = [c for c in cells if c != gate]
        return tuple(cells)

    # -- task points -------------------------------------------------------
    def my_task_points(self) -> tuple[PlayerTask, ...]:
        return self.obs.my_tasks()

    def open_task_points(self) -> tuple[PlayerTask, ...]:
        return tuple(t for t in self.my_task_points() if t.valid and t.cooldown <= 0)

    def nearest_task_point(self, pos: Pos) -> PlayerTask | None:
        tasks = self.my_task_points()
        if not tasks:
            return None
        return min(tasks, key=lambda t: (footprint_distance(pos, t.cells),
                                         t.task_type))

    # -- threats -----------------------------------------------------------
    def threat_robots(self) -> tuple[Robot, ...]:
        """Robots that matter, filtered by ``targetTeam`` when it is sent.

        ``targetTeam`` is defined by 接口文档 §1.5.1 but absent from the sample
        request, so it is used only as a *filter when present* -- never assumed.
        """
        base = self.station_cells() or ((self.station_anchor() or Pos(0, 0)),)
        robots = [r for r in self.obs.robots if r.alive]
        tagged = [r for r in robots if r.target_team]
        if tagged:
            mine = [r for r in tagged if r.target_team == self.obs.team_type]
            if mine:
                return tuple(sorted(mine, key=lambda r: self.threat_key(r, base)))
        return tuple(sorted(robots, key=lambda r: self.threat_key(r, base)))

    def threat_key(self, robot: Robot, base: tuple[Pos, ...]) -> tuple:
        return (
            footprint_distance(robot.pos, base),
            -R.ROBOT_STATS.get(robot.kind, R.ROBOT_STATS[R.DEFAULT_ROBOT])["attack"],
            robot.robot_id,
        )

    def base_under_pressure(self) -> bool:
        base = self.station_cells()
        if not base:
            return False
        for robot in self.threat_robots():
            if footprint_distance(robot.pos, base) <= 3:
                return True
        return False

    def mines(self, kind: str | None = None) -> tuple[Pos, ...]:
        return self.obs.mine_cells(kind)

    def mine_stands(self, pos: Pos) -> tuple[Pos, ...]:
        return tuple(n for n in pos.neighbours()
                     if self.obs.buildable_terrain(n))

    def free_stands(self, pos: Pos) -> tuple[Pos, ...]:
        blocked = self.blocked_cells()
        robots = {r.pos for r in self.obs.robots if r.alive}
        return tuple(n for n in pos.neighbours()
                     if self.obs.buildable_terrain(n)
                     and n not in blocked and n not in robots)


def build_attempt_preconditions_ok(obs: Observation, worker: Unit, pos: Pos,
                                   kind: str) -> bool:
    """All preconditions that make a failed build *informative*.

    Only when every one of these holds may a ``false`` in
    ``lastRoundRoleActionResults`` be treated as evidence about the cell itself;
    otherwise the failure could be a collision, missing material or missing gold.
    """
    if obs.is_day is False:
        return False
    if worker.kind != R.WORKER or not worker.alive:
        return False
    if distance(worker.pos, pos) != 1:
        return False
    if pos in obs.zones:
        return False
    name = R.BUILD_NAME.get(kind, kind)
    if kind == R.WALL and worker.count(R.WALL_MATERIAL) < 1:
        return False
    if kind != R.WALL and obs.gold < R.WEAPON_BUILD_COST:
        return False
    return True
