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
        """The base's 2x2 footprint (接口文档 1.3.1: pos is the top-left corner)."""
        anchor = self.station_anchor()
        if anchor is None:
            return ()
        return tuple(p for p in station_footprints(anchor)[0]
                     if self.obs.in_bounds(p))

    # -- orientation -------------------------------------------------------
    def front_direction(self) -> tuple[int, int]:
        """Unit step from the base towards the map interior.

        R2 fact (user-confirmed and matching every observed wave): robots always
        attack from the side of the base that faces the map interior.  The base
        sits in a corner region, so the interior direction is the sign of
        ``map_centre - base`` on each axis; x is the dominant axis (bases are
        left/right), y is the secondary one.
        """
        anchor = self.station_anchor()
        if anchor is None:
            return (0, 0)
        dx = 1 if anchor.x < self.obs.width / 2 else -1
        dy = 1 if anchor.y < self.obs.height / 2 else -1
        return (dx, dy)

    def front_ness(self, pos: Pos) -> int:
        """How far ``pos`` lies towards the front (bigger = closer to the enemy).

        Weighted 2:1 in favour of x so that "the tower facing the robots" is
        decided by the dominant axis, with y breaking ties.
        """
        anchor = self.station_anchor()
        if anchor is None:
            return 0
        dx, dy = self.front_direction()
        return 2 * (pos.x - anchor.x) * dx + (pos.y - anchor.y) * dy

    def rear_direction(self) -> tuple[int, int]:
        dx, dy = self.front_direction()
        return (-dx, -dy)

    def operator_hub(self) -> Pos | None:
        """One cell that touches all three planned towers, on the rear side.

        A single role may operate only one weapon per round (接口文档 §2.2), so
        three towers need three operators -- and a shared hub means any
        surviving role can take over any tower.  It is also the *rear* cell, so
        operators are never the closest unit for an attacking robot.
        """
        anchor = self.station_anchor()
        if anchor is None:
            return None
        dx, _dy = self.front_direction()
        hub = Pos(anchor.x - 2, anchor.y - 1) if dx > 0 else Pos(anchor.x + 3, anchor.y)
        return hub if self.obs.in_bounds(hub) else None

    def tower_sites(self) -> tuple[Pos, ...]:
        """The three planned tower cells, in build order (front-most first).

        Chosen as *footprint distance 1 AND hub distance 1*: that is exactly the
        set of legal weapon cells a single operator can serve from one cell, and
        it is what makes the hub layout work.  Building "any ring-1 cell" instead
        (v2's first attempt) scattered the three towers along the base's south
        face, where no shared hub exists and the operator spends the night
        walking between them.
        """
        anchor = self.station_anchor()
        hub = self.operator_hub()
        if anchor is None or hub is None:
            return ()
        footprint = self.station_cells()
        cells = [c for c in hub.ring(1)
                 if self.obs.in_bounds(c)
                 and footprint_distance(c, footprint) == 1
                 and self.obs.buildable_terrain(c)
                 and c not in self.blocked_cells()]
        # Front-most first: that tower gets the first upgrade voucher (R2).
        cells.sort(key=lambda p: (-self.front_ness(p), p.x, p.y))
        return tuple(cells[:3])

    def wall_sites(self) -> tuple[Pos, ...]:
        """U-shaped ring at footprint distance 2, open at the rear for our own use.

        Front column first (that is where the robots arrive), then the two
        lateral rows from front to rear.  The rear column is deliberately left
        open so the operator hub always stays reachable.
        """
        anchor = self.station_anchor()
        if anchor is None or not self.cfg.wall_ring_enabled:
            return ()
        dx, _dy = self.front_direction()
        x, y = anchor.x, anchor.y
        # The anchor is always the *minimum-x* cell of the 2x2 footprint, so the
        # ring-2 columns are [x-2, x+3] for either orientation; only which of the
        # two extreme columns counts as "front" flips.
        columns = range(x - 2, x + 4)
        front = x + 3 if dx > 0 else x - 2
        cells: list[Pos] = [Pos(front, yy) for yy in range(y - 3, y + 3)]
        cells.sort(key=lambda p: (abs(p.y - (y - 0.5)), p.y))
        rows = (y - 3, y + 2)
        lateral = [Pos(xx, yy) for yy in rows for xx in columns if xx != front]
        lateral.sort(key=lambda p: (abs(p.x - front), p.y))
        planned = cells + lateral
        hub = self.operator_hub()
        out = [p for p in planned
               if p != hub and self.obs.buildable_terrain(p)]
        return tuple(out[:self.cfg.wall_ring_limit])

    # -- safety ------------------------------------------------------------
    def side_depth(self, pos: Pos) -> int:
        return min(pos.x, self.obs.width - 1 - pos.x)

    def safe_cell(self, pos: Pos) -> bool:
        """R3: close to a map edge and not inside a robot's strike radius.

        The middle of the map is where the wave walks through, so at night a
        character caught there trades its life for a whole next-day respawn.
        """
        if self.side_depth(pos) > self.cfg.night_side_limit(self.obs.width):
            return False
        for robot in self.obs.robots:
            if robot.alive and distance(robot.pos, pos) <= self.cfg.night_robot_radius:
                return False
        return True

    def safe_cells(self) -> tuple[Pos, ...]:
        return tuple(Pos(x, y)
                     for x in range(self.obs.width)
                     for y in range(self.obs.height)
                     if self.safe_cell(Pos(x, y)))

    def middle_band(self) -> frozenset[Pos]:
        """Cells a night-time character should not route through (soft block)."""
        limit = self.cfg.night_side_limit(self.obs.width)
        return frozenset(
            Pos(x, y)
            for x in range(self.obs.width)
            for y in range(self.obs.height)
            if self.side_depth(Pos(x, y)) > limit
        )

    def danger_cells(self) -> frozenset[Pos]:
        """Cells within the strike radius of a live robot."""
        cells: set[Pos] = set()
        for robot in self.obs.robots:
            if not robot.alive:
                continue
            for dx in range(-self.cfg.night_robot_radius,
                            self.cfg.night_robot_radius + 1):
                for dy in range(-self.cfg.night_robot_radius,
                                self.cfg.night_robot_radius + 1):
                    cell = Pos(robot.pos.x + dx, robot.pos.y + dy)
                    if self.obs.in_bounds(cell):
                        cells.add(cell)
        return frozenset(cells)

    def night_now(self) -> bool:
        """True once caution should apply: after dark, or just before it."""
        if not self.obs.is_day:
            return True
        return self.obs.phase_round >= R.DAY_ROUNDS - self.cfg.night_margin

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
        """Ordered candidate cells for building ``kind`` ('wall' or a tower).

        Order is *planned layout first, geometry second*: the three rear tower
        cells / the U-shaped wall ring are what the strategy actually wants, and
        the ring fallback keeps the agent functional on a map whose build region
        differs from 任务书 §4.1's 8x8 diagram (the judge's own accept/reject
        feedback is what ultimately confirms a cell -- see ``SiteKnowledge``).
        """
        anchor = self.station_anchor()
        if anchor is None:
            return ()
        name = R.BUILD_NAME.get(kind, kind)
        footprint = self.station_cells()
        blocked = self.blocked_cells()
        robot_cells = {r.pos for r in self.obs.robots if r.alive}

        planned = list(self.tower_sites() if kind != R.WALL else self.wall_sites())
        # Fallback: the rest of the *same* ring.  任务书 §4.1's diagram is explicit
        # that weapons live at footprint distance 1 and walls at distance 2, so a
        # wider ring is never offered -- building there would be an illegal
        # command (a wasted action at best, an 异常 at worst), and the judge's
        # accept/reject feedback in ``SiteKnowledge`` is what confirms a cell.
        wanted = 1 if kind != R.WALL else 2
        for cell in self._footprint_ring(footprint, wanted):
            if cell not in planned:
                planned.append(cell)

        candidates: list[Pos] = []
        for cell in planned:
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
            if kind == R.WALL:
                # Front first: that is the side the wave arrives from.
                order = self.front_ness(cell)
                return (hint, -order, footprint_distance(cell, footprint),
                        cell.x, cell.y)
            return (hint, 0, footprint_distance(cell, footprint), cell.x, cell.y)

        candidates.sort(key=rank)
        return tuple(candidates)

    def _footprint_ring(self, footprint: tuple[Pos, ...],
                        radius: int) -> tuple[Pos, ...]:
        """Cells at exactly ``radius`` Chebyshev distance from a footprint."""
        if not footprint:
            return ()
        seen: list[Pos] = []
        known: set[Pos] = set()
        for cell in footprint:
            for ring_cell in cell.ring(radius):
                if ring_cell in known or not self.obs.in_bounds(ring_cell):
                    continue
                known.add(ring_cell)
                if footprint_distance(ring_cell, footprint) == radius:
                    seen.append(ring_cell)
        seen.sort(key=lambda p: (p.x, p.y))
        return tuple(seen)

    def _ring(self, anchor: Pos, radius: int) -> tuple[Pos, ...]:
        return anchor.ring(radius)

    def wall_plan(self, cfg: Config = DEFAULT) -> tuple[Pos, ...]:
        """Cells that should eventually hold a wall (footprint ring 2, rear open).

        Superseded by :meth:`wall_sites`; kept as the documented entry point so
        older call sites and tests keep working.
        """
        return self.wall_sites()

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
