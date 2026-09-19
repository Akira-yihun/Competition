"""Day/night turn planning: defense, economy, construction, intelligence.

Kept as one module with four clearly separated sections rather than four modules
that must call each other: on a 41x32 board the strategy is small, and a single
place to read the whole turn is worth more than file boundaries that only serve
the directory listing.  Each section has a single responsibility and no section
mutates another's state.

Section map
-----------
1. defense    -- return to cover, controller<->tower assignment, firing
2. economy    -- mining, bulk selling, purchases and upgrades
3. construction -- tower/wall siting with the limited build-site probe
4. intelligence -- official-price calendar and treasure hypothesis

The 1800-action budget (3 roles x 60 night rounds == 3 towers x 600 firing
rounds, zero slack) is what makes night non-negotiable: nothing but defence and
mining-with-spare-capacity happens after dark.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import combat
from . import rules as R
from .config import Config
from .model import Observation, Pos, Unit, distance, footprint_distance
from . import navigation
from .navigation import distances_from, ordered_stands
from .world import (
    LEGAL, WorldView, build_attempt_preconditions_ok,
)
from .protocol import (
    accept_task, attack as attack_cmd, build as build_cmd, buy as buy_cmd,
    collect, move as move_cmd, sell as sell_cmd, submit_answer,
    summon_treasure, use as use_cmd,
)


# ---------------------------------------------------------------------------
# plan container
# ---------------------------------------------------------------------------
@dataclass
class TurnPlan:
    commands: dict[int, dict] = field(default_factory=dict)
    prompt: str = ""
    execute_cmd: str = ""
    diagnostics: list[str] = field(default_factory=list)
    reserved_steps: set[tuple[int, int]] = field(default_factory=set)
    gold_spent: int = 0
    build_attempt: tuple[int, Pos, str] | None = None
    #: soft no-go cells for this turn (v2: the map's middle band after dark)
    avoid: frozenset[Pos] = frozenset()
    #: role ids allowed to cross ``avoid`` (the defender must reach its towers)
    exempt: frozenset[int] = frozenset()

    def reserve(self, pos: Pos) -> bool:
        key = (pos.x, pos.y)
        if key in self.reserved_steps:
            return False
        self.reserved_steps.add(key)
        return True

    def take(self, role_id: int, command: dict) -> None:
        if role_id in self.commands:
            return
        self.commands[role_id] = command

    def note(self, message: str) -> None:
        self.diagnostics.append(message)
        del self.diagnostics[:-64]

    def avoid_for(self, actor: Unit) -> frozenset[Pos]:
        if not self.avoid or actor.unit_id in self.exempt:
            return frozenset()
        return self.avoid

    def available_gold(self, obs: Observation) -> int:
        """Observed gold minus everything already committed this turn.

        Two workers each reading the same un-decremented ``goldNum`` is exactly
        how the baseline could spend 50 gold when it only had 25 (audit A13).
        """
        return max(0, obs.gold - self.gold_spent)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _resolve_goal(world: WorldView, actor: Unit, goal: Pos,
                  cfg: Config) -> Pos:
    """Turn an unreachable target into a reachable one.

    Mines, the vendor, the weapon shop and the task points are all *zones*, and
    任务书 §4.1 lists zones as movement blockers: the character must stand
    **next to** them.  v1 passed the zone cell straight to the pathfinder, which
    rejects any goal inside ``obs.zones`` and returns no path -- so every
    "walk to the vendor / weapon shop" instruction silently did nothing and the
    workers never sold a single ore or bought a single upgrade.
    """
    obs = world.obs
    if obs.buildable_terrain(goal) and goal not in world.blocked_cells():
        return goal
    stands = [n for n in goal.neighbours()
              if obs.buildable_terrain(n) and n not in world.blocked_cells()]
    if not stands:
        return goal
    field = distances_from(world, actor, (actor.pos,), cfg)
    stands.sort(key=lambda p: (field.get(p, 10 ** 6), distance(actor.pos, p),
                               p.x, p.y))
    return stands[0]


def _walk(world: WorldView, plan: TurnPlan, actor: Unit, goal: Pos,
          cfg: Config, *, exclude: frozenset[Pos] = frozenset()) -> Pos | None:
    """One step towards ``goal``, honouring cells already claimed this turn.

    A single A* returns the whole path plus the distance field it explored, so a
    cell contested by another role is resolved by re-planning to that role's next
    preference **from the same search** instead of paying for one full-grid
    search per alternative (the baseline did exactly that: ~2400 searches in one
    daytime turn, peaking near a second).

    v2 adds the night-time soft no-go band (R3).  It is only ever *soft*: when
    the restricted search finds nothing at all we retry unrestricted, because
    standing still next to a robot is strictly worse than crossing the middle.
    """
    goal = _resolve_goal(world, actor, goal, cfg)
    avoid = plan.avoid_for(actor)
    path, field, _cost = navigation.search(world, actor, goal, cfg, avoid=avoid)
    if not path and avoid:
        path, field, _cost = navigation.search(world, actor, goal, cfg)
    if not path:
        return None
    step = _first_free_step(path, plan, exclude)
    if step is not None:
        return step
    # The cheapest few alternatives the same search already reached.
    ranked = sorted(
        (cell for cell in field
         if cell != actor.pos and cell not in exclude
         and (cell.x, cell.y) not in plan.reserved_steps
         and world.obs.buildable_terrain(cell)),
        key=lambda cell: (field[cell], distance(cell, goal), cell.x, cell.y),
    )
    for alternative in ranked[:4]:
        fallback, _f, _c = navigation.search(world, actor, alternative, cfg,
                                             allow_robots=True)
        if not fallback:
            continue
        step = _first_free_step(fallback, plan, exclude)
        if step is not None:
            return step
    return None


def _first_free_step(path: list[Pos], plan: TurnPlan,
                     exclude: frozenset[Pos]) -> Pos | None:
    if len(path) < 2:
        return None
    step = path[1]
    if step in exclude:
        return None
    return step if plan.reserve(step) else None


def _move_towards(world: WorldView, plan: TurnPlan, actor: Unit, goal: Pos,
                  cfg: Config) -> bool:
    if actor.pos == goal:
        return False
    step = _walk(world, plan, actor, goal, cfg)
    if step is None or step == actor.pos:
        return False
    plan.take(actor.unit_id, move_cmd(step))
    return True


def _stand_cells(world: WorldView, target: Pos,
                 actor: Unit) -> tuple[Pos, ...]:
    return tuple(n for n in target.neighbours()
                 if world.obs.buildable_terrain(n))




def _price_table(world: WorldView) -> dict[str, int]:
    """Vendor prices from the payload, falling back to the task-book baseline."""
    prices = dict(R.BASE_MINERAL_PRICE)
    for kind, price in (world.obs.vendor or {}).items():
        if kind in R.MINE_KINDS and price > 0:
            prices[kind] = price
    return prices


def _price_on(world: WorldView, state, kind: str, prices: dict[str, int]) -> float:
    """Today's price for ``kind``: observed price scaled by the news calendar."""
    base = float(prices.get(kind, R.BASE_MINERAL_PRICE.get(kind, 1)))
    calendar = getattr(state, "price_calendar", None) if state is not None else None
    if calendar is None:
        return base
    return base * calendar.multiplier(kind, world.obs.day_index)


def _mineable_on(world: WorldView, state, kind: str) -> bool:
    calendar = getattr(state, "price_calendar", None) if state is not None else None
    if calendar is None:
        return True
    return calendar.mineable(kind, world.obs.day_index)


def _locked_mine(world: WorldView, state, actor: Unit,
                 kinds: tuple[str, ...]) -> Pos | None:
    """The mine this worker committed to, if it is still there and usable.

    R4: a mine survives 10 collections and only then respawns elsewhere, so the
    target is worth keeping across the walk/collect/sell cycle.  The lock is
    dropped the moment the ore cell disappears from ``zones``, changes type, or
    becomes a place we refuse to stand at night.
    """
    if state is None or not getattr(state, "mine_targets", None):
        return None
    entry = state.mine_targets.get(actor.unit_id)
    if not entry:
        return None
    pos = Pos(int(entry.get("x", -1)), int(entry.get("y", -1)))
    kind = world.obs.zones.get(pos)
    if kind is None or kind not in R.MINE_KINDS:
        state.mine_targets.pop(actor.unit_id, None)
        return None
    if kinds and kind not in kinds:
        return None
    if world.night_now() and not world.safe_cell(pos):
        return None
    if entry.get("ore") != kind:
        entry["ore"] = kind
    return pos


def _lock_mine(state, actor: Unit, mine: Pos, kind: str) -> None:
    if state is None or not getattr(state, "mine_lock", True):
        return
    if getattr(state, "mine_targets", None) is None:
        return
    state.mine_targets[actor.unit_id] = {"x": mine.x, "y": mine.y, "ore": kind}


def _heal_step(world: WorldView, plan: TurnPlan, actor: Unit) -> bool:
    """Drink a held Medicine / patch a wall we are standing next to.

    v1 *bought* these consumables and then never used them, so the gold was a
    pure donation.  Both are free actions in the sense that they need no walk:
    Medicine has no target, and WallFixer only applies to an adjacent wall.
    """
    if actor.unit_id in plan.commands:
        return False
    maximum = 200 if actor.kind == R.PIONEER else 220
    if actor.health < maximum * 0.65 and actor.count(R.MEDICINE) >= 1:
        plan.take(actor.unit_id, use_cmd(R.MEDICINE))
        plan.note("use:Medicine")
        return True
    if actor.count(R.WALL_FIXER) >= 1:
        for wall in sorted(world.obs.walls(), key=lambda w: (w.health, w.unit_id)):
            top = R.BUILDING_HP[R.WALL][min(max(wall.level, 1), 3) - 1]
            if wall.health >= top or distance(actor.pos, wall.pos) > 1:
                continue
            plan.take(actor.unit_id, use_cmd(R.WALL_FIXER, wall.pos))
            plan.note("use:WallFixer")
            return True
    return False


def choose_mine(world: WorldView, state, actor: Unit, cfg: Config,
                kinds: tuple[str, ...],
                taken: frozenset[Pos] = frozenset()) -> tuple[Pos, Pos] | None:
    """Pick a mine and a stand cell, balancing price, distance and sale trip (R4).

    ``score = price * yield / (1 + walk_to_mine + walk_mine_to_vendor)``

    which is the gold-per-round rate of the whole loop, not just of the walk.
    A locked mine always wins if it is still valid: switching targets every round
    is what made v1's collection "比较混乱".
    """
    obs = world.obs
    locked = _locked_mine(world, state, actor, kinds)
    if locked is not None:
        stands = _stand_cells(world, locked, actor)
        if stands:
            field = distances_from(world, actor, (actor.pos,), cfg)
            reachable = [(field[s], s) for s in stands if s in field]
            if reachable:
                reachable.sort(key=lambda item: (item[0], item[1].x, item[1].y))
                return locked, reachable[0][1]
        return locked, locked          # adjacent collect handled by the caller

    mines = [p for p in obs.mine_cells()
             if (not kinds or obs.zones.get(p) in kinds)
             and _mineable_on(world, state, obs.zones.get(p, ""))]
    if not mines:
        return None
    if world.night_now():
        mines = [p for p in mines if world.safe_cell(p)]
        if not mines:
            return None
    prices = _price_table(world)
    vendor = obs.first_zone(R.ZONE_VENDOR)
    field = distances_from(world, actor, (actor.pos,), cfg)

    best: tuple[float, int, Pos, Pos] | None = None
    for mine in mines:
        kind = obs.zones.get(mine, "")
        price = _price_on(world, state, kind, prices)
        to_vendor = distance(mine, vendor) if vendor is not None else 24
        for stand in _stand_cells(world, mine, actor):
            reach = field.get(stand)
            if reach is None:
                continue
            loop = 1.0 + reach + cfg.mine_sell_weight * to_vendor
            score = price * cfg.mine_yield / loop
            if mine in taken:
                score *= 0.35             # two workers on one 10-use mine waste yield
            key = (score, -reach, mine, stand)
            if best is None or key > best:
                best = key
    if best is None:
        return None
    _score, _reach, mine, stand = best
    _lock_mine(state, actor, mine, obs.zones.get(mine, ""))
    return mine, stand


def _mine_step(world: WorldView, plan: TurnPlan, actor: Unit, cfg: Config,
               kinds: tuple[str, ...], state=None,
               taken: frozenset[Pos] = frozenset()) -> bool:
    """Collect from the committed mine, or take one step towards it."""
    obs = world.obs
    adjacent = [p for p in obs.mine_cells()
                if distance(actor.pos, p) == 1
                and (not kinds or obs.zones.get(p) in kinds)
                and _mineable_on(world, state, obs.zones.get(p, ""))]
    if adjacent and not actor.backpack_full:
        locked = _locked_mine(world, state, actor, kinds)
        if locked in adjacent:
            adjacent = [locked]
        else:
            adjacent.sort(key=lambda p: (obs.zones.get(p, ""), p.x, p.y))
        _lock_mine(state, actor, adjacent[0], obs.zones.get(adjacent[0], ""))
        plan.take(actor.unit_id, collect(adjacent[0]))
        return True
    if actor.backpack_full:
        return False
    pick = choose_mine(world, state, actor, cfg, kinds, taken)
    if pick is None:
        return False
    mine, stand = pick
    if distance(actor.pos, mine) == 1:
        plan.take(actor.unit_id, collect(mine))
        return True
    if stand == actor.pos:
        plan.take(actor.unit_id, collect(mine))
        return True
    step = _walk(world, plan, actor, stand, cfg)
    if step is None:
        return False
    plan.take(actor.unit_id, move_cmd(step))
    return True


# ---------------------------------------------------------------------------
# 1. defense
# ---------------------------------------------------------------------------
def return_deadline(world: WorldView, actor: Unit, goal: Pos, cfg: Config) -> int:
    """Phase round by which ``actor`` must start heading for ``goal``."""
    field = distances_from(world, actor, (actor.pos,), cfg)
    travel = field.get(goal)
    if travel is None:
        travel = distance(actor.pos, goal)
    return R.DAY_ROUNDS - travel - cfg.return_margin


def must_return(world: WorldView, actor: Unit, goal: Pos | None,
                cfg: Config) -> bool:
    if goal is None:
        return False
    obs = world.obs
    if not obs.is_day:
        return True
    return obs.phase_round >= return_deadline(world, actor, goal, cfg)


def assign_controllers(world: WorldView, available: tuple[Unit, ...],
                       cfg: Config) -> dict[int, Unit]:
    """Greedy min-distance matching between towers and available roles.

    With three towers packed around a base, two towers can end up sharing the
    only reachable stand cell; the loser fires nothing and effectively loses a
    third of the night's firepower (1800 actions, no slack).
    """
    obs = world.obs
    towers = list(obs.towers())
    if not towers or not available:
        return {}
    pairs: list[tuple[int, int, int, int]] = []
    for tower in towers:
        for role in available:
            d = distance(role.pos, tower.pos)
            pairs.append((d, role.unit_id, tower.unit_id, 0))
    pairs.sort(key=lambda item: (item[0], item[1], item[2]))

    assignment: dict[int, Unit] = {}
    used_roles: set[int] = set()
    by_id = {u.unit_id: u for u in obs.roles}
    for d, role_id, tower_id, _ in pairs:
        if tower_id in assignment or role_id in used_roles:
            continue
        if d > cfg.idle_defend_radius and len(assignment) >= len(available):
            continue
        assignment[tower_id] = by_id[role_id]
        used_roles.add(role_id)
    for tower in towers:
        if tower.unit_id in assignment:
            continue
        free = [r for r in available if r.unit_id not in used_roles]
        if not free:
            break
        best = min(free, key=lambda r: (distance(r.pos, tower.pos), r.unit_id))
        assignment[tower.unit_id] = best
        used_roles.add(best.unit_id)
    return assignment


def plan_night(world: WorldView, plan: TurnPlan, cfg: Config, state=None,
               *, pioneers_excluded: frozenset[int] = frozenset()) -> None:
    """Night turn: everything is defence, plus the held station voucher (R5).

    A single role may operate only one weapon per round (接口文档 §2.2), so the
    layout puts all three towers next to one shared hub and this function puts a
    *different* body on each ready tower.  v1 assigned towers by nearest-role and
    then fired before moving, which in practice fired one tower per round.
    """
    obs = world.obs
    robots = tuple(world.threat_robots())
    ledger = combat.DamageLedger()
    base = world.station_cells()
    front = world.front_direction()

    # R3: after dark nobody wanders into the middle of the map.  Characters that
    # are already there (and the defender, who may have to cross) are exempt, so
    # the band can never trap anyone; see ``_walk``'s fallback as well.
    band = world.middle_band()
    defender, _miner = assign_duties(world, state)
    exempt = {u.unit_id for u in obs.fighters() if u.pos in band}
    if defender is not None:
        exempt.add(defender.unit_id)
    plan.avoid = band
    plan.exempt = frozenset(exempt)

    # The station voucher is a full heal; spend it before the wave finishes the
    # job, and keep the carrier within one cell of the base once it looks likely.
    _station_voucher_step(world, plan, cfg, state)

    fighters = tuple(u for u in obs.fighters()
                     if u.unit_id not in pioneers_excluded)
    towers = obs.towers()
    if not towers:
        _shelter(world, plan, fighters, cfg)
        return

    assignment = assign_controllers(world, fighters, cfg)
    claimed_roles: set[int] = set()

    # fire first: a role already in place should shoot rather than reposition
    for tower in towers:
        controller = assignment.get(tower.unit_id)
        if controller is None or controller.unit_id in claimed_roles:
            continue
        if distance(controller.pos, tower.pos) > cfg.idle_defend_radius:
            continue
        if combat.tower_cooldown(tower, obs.round_no, world.cooling) > 0:
            continue
        aims = combat.aim_for(tower, robots, ledger, cfg, base, front)
        if not aims:
            continue
        command = attack_cmd(controller.unit_id, aims)
        command["__towerId"] = tower.unit_id
        plan.take(controller.unit_id, command)
        claimed_roles.add(controller.unit_id)
        for aim in aims:
            for robot in combat.robots_on_line(tower.pos, aim, robots):
                ledger.commit(robot.robot_id, 10)
                break

    # then move everyone else into position
    hub = world.operator_hub()
    for tower in towers:
        controller = assignment.get(tower.unit_id)
        if controller is None:
            continue
        if controller.unit_id in claimed_roles or controller.unit_id in plan.commands:
            continue
        goal = _operator_stand(world, controller, tower, hub, cfg)
        if goal is None or controller.pos == goal:
            claimed_roles.add(controller.unit_id)
            continue
        step = _walk(world, plan, controller, goal, cfg)
        if step is not None:
            plan.take(controller.unit_id, move_cmd(step))
        claimed_roles.add(controller.unit_id)

    leftover = [f for f in fighters
                if f.unit_id not in claimed_roles and f.unit_id not in plan.commands]
    # A role with no tower to fire (or one whose tower is cooling) is the right
    # candidate to spend 100 gold on a 3x3 cluster: Bomb/DizzyWeapon settle
    # before robots move, have no range limit, and cost no tower action.
    spare: list[Unit] = []
    for actor in leftover:
        if _use_consumable(world, plan, actor, robots, cfg):
            continue
        if _heal_step(world, plan, actor):
            continue
        spare.append(actor)
    _shelter(world, plan, tuple(spare), cfg)


def _operator_stand(world: WorldView, controller: Unit, tower: Unit,
                    hub: Pos | None, cfg: Config) -> Pos | None:
    """Where this controller should stand: the shared hub when it can reach it."""
    if hub is not None and distance(hub, tower.pos) <= 1:
        if controller.pos == hub:
            return None
        return hub
    stands = ordered_stands(world, controller, (tower.pos,), cfg=cfg)
    stands = [s for s in stands if distance(s, tower.pos) <= 1]
    if not stands:
        stands = [s for s in _stand_cells(world, tower.pos, controller)]
    return stands[0] if stands else None


def _station_voucher_step(world: WorldView, plan: TurnPlan, cfg: Config,
                          state) -> bool:
    """R5: hold the station upgrade voucher, use it only when it saves the base.

    The upgrade sets the station to full HP, so spending it early throws the heal
    away.  Use it when
      * health < ``station_emergency_hp`` (100), or
      * the robots already in range deal lethal damage within
        ``station_predict_rounds`` rounds (attack power × rounds ≥ health),
    and until then keep the carrier loitering next to the base once health drops
    below ``station_standby_ratio`` of maximum, so the trip is never the reason
    the base falls.
    """
    if state is None:
        return False
    station = world.obs.station()
    if station is None or station.level >= 3:
        return False
    voucher = R.VOUCHER_STATION_1 if station.level == 1 else R.VOUCHER_STATION_2
    holder = _voucher_holder(world, state, voucher)
    if holder is None:
        return False
    maximum = R.BUILDING_HP[R.STATION][min(station.level, 3) - 1]
    incoming = _incoming_damage(world, tuple(world.threat_robots()), cfg)
    urgent = (station.health < cfg.station_emergency_hp
              or incoming * cfg.station_predict_rounds >= station.health)
    near = _near_base(world, holder.pos)
    if urgent:
        if near:
            target = _station_target(world, holder)
            plan.take(holder.unit_id, use_cmd(voucher, target))
            plan.note(f"station_voucher@{world.obs.round_no}")
            return True
        step = _walk(world, plan, holder, _base_stand(world, holder), cfg)
        if step is not None:
            plan.take(holder.unit_id, move_cmd(step))
            return True
        return False
    if station.health < maximum * cfg.station_standby_ratio and not near:
        step = _walk(world, plan, holder, _base_stand(world, holder), cfg)
        if step is not None:
            plan.take(holder.unit_id, move_cmd(step))
            return True
    return False


def _voucher_holder(world: WorldView, state, voucher: str) -> Unit | None:
    """The role that carries ``voucher`` (remembered so it does not wander off)."""
    remembered = (state.station_voucher or {}).get(voucher)
    for unit in world.obs.fighters():
        if unit.count(voucher) >= 1 and (remembered is None
                                         or unit.unit_id == remembered):
            return unit
    for unit in world.obs.fighters():
        if unit.count(voucher) >= 1:
            state.station_voucher = {voucher: unit.unit_id}
            return unit
    return None


def _incoming_damage(world: WorldView, robots: tuple, cfg: Config) -> int:
    """Per-round damage the robots in range of the base can currently apply."""
    base = world.station_cells()
    if not base:
        return 0
    total = 0
    for robot in robots:
        if footprint_distance(robot.pos, base) > 5:
            continue
        stats = R.ROBOT_STATS.get(robot.kind, R.ROBOT_STATS[R.DEFAULT_ROBOT])
        total += int(stats["attack"])
    return total


def _near_base(world: WorldView, pos: Pos) -> bool:
    base = world.station_cells()
    return bool(base) and footprint_distance(pos, base) <= 1


def _station_target(world: WorldView, actor: Unit) -> Pos:
    """The base cell to name in ``use``: the footprint cell nearest the actor.

    任务书 §4.6.3 requires the voucher to be used 在目标建筑周围一格内, and the
    strictest reading is that the *named* cell must be within reach.  Sending the
    nearest of the four footprint cells (rather than blindly the reported anchor)
    keeps that true for an operator standing behind the base.
    """
    base = world.station_cells()
    if not base:
        return world.station_anchor() or actor.pos
    return min(base, key=lambda c: (distance(actor.pos, c), -world.front_ness(c),
                                    c.x, c.y))


def _base_stand(world: WorldView, actor: Unit) -> Pos:
    """A free cell within one step of a base cell, preferring the rear.

    Robots arrive from the front, so the rear cell keeps the voucher carrier out
    of the fight while still being able to use the voucher (R5).
    """
    base = world.station_cells()
    if not base:
        return actor.pos
    stands: list[Pos] = []
    seen: set[Pos] = set()
    for cell in base:
        for nb in cell.neighbours():
            if nb in seen or not world.obs.buildable_terrain(nb):
                continue
            seen.add(nb)
            if nb in world.blocked_cells():
                continue
            stands.append(nb)
    if not stands:
        return actor.pos
    stands.sort(key=lambda p: (-world.front_ness(p), distance(actor.pos, p), p.x))
    return stands[0]


def _use_consumable(world: WorldView, plan: TurnPlan, actor: Unit,
                    robots: tuple, cfg: Config) -> bool:
    """Spend a held Bomb/DizzyWeapon, buying one first only if affordable."""
    obs = world.obs
    if actor.unit_id in plan.commands:
        return False
    base = world.station_cells()
    aim = combat.bomb_cluster(obs, robots, cfg, base)
    item = R.BOMB
    if aim is None:
        aim = combat.dizzy_target(obs, robots, cfg, base)
        item = R.DIZZY_WEAPON
    if aim is None:
        return False
    price = R.SHOP_PRICE.get(item, 0)
    if actor.count(item) < 1:
        # only buy when the gold is genuinely spare: a station upgrade is worth
        # more than a single bomb
        if plan.available_gold(obs) < price + R.SHOP_PRICE[R.VOUCHER_WEAPON_2]:
            return False
        shop = obs.first_zone(R.ZONE_WEAPON_SHOP)
        if shop is None:
            return False
        if distance(actor.pos, shop) <= 1:
            plan.take(actor.unit_id, buy_cmd(item, 1))
            plan.gold_spent += price
            return True
        return False
    plan.take(actor.unit_id, use_cmd(item, aim))
    plan.note(f"use:{item}@{aim.x},{aim.y}")
    return True


def _shelter(world: WorldView, plan: TurnPlan, actors: tuple[Unit, ...],
             cfg: Config) -> None:
    """Idle roles wait at the shared tower hub, or on a safe edge cell (R3).

    v1 pushed them *inside* the base footprint, which is the worst place to be:
    a robot that reaches the base then finds a 220 HP worker standing closer than
    the 1500 HP station and kills it, costing a whole next-day respawn.
    """
    obs = world.obs
    hub = world.operator_hub()
    towers = obs.towers()
    if hub is not None and towers and distance(hub, towers[0].pos) <= 1:
        goal = hub
        goal_ok = True
    else:
        goal = _safe_rally_cell(world, actors[0].pos if actors else None)
        goal_ok = goal is not None
    for actor in actors:
        if actor.unit_id in plan.commands:
            continue
        if not goal_ok or goal is None:
            continue
        if actor.pos == goal:
            continue
        step = _walk(world, plan, actor, goal, cfg)
        if step is not None:
            plan.take(actor.unit_id, move_cmd(step))


def _safe_rally_cell(world: WorldView, near: Pos | None) -> Pos | None:
    """Nearest *safe* cell to our base, i.e. an edge cell away from the wave.

    R3 says to leave the middle of the map; it does not say to run to the far
    corner.  Ranking by distance to the base first keeps the evacuated character
    close enough to walk back to a tower the moment it is needed.
    """
    cells = [c for c in world.safe_cells()
             if world.obs.buildable_terrain(c) and c not in world.blocked_cells()]
    if not cells:
        return None
    anchor = world.station_anchor() or (near or Pos(0, 0))
    cells.sort(key=lambda p: (distance(p, anchor), world.side_depth(p),
                              distance(p, near) if near else 0, p.x, p.y))
    return cells[0]


# ---------------------------------------------------------------------------
# 2. economy
# ---------------------------------------------------------------------------


def _inventory(actor: Unit, item: str) -> int:
    return actor.count(item)


def plan_economy(world: WorldView, plan: TurnPlan, actor: Unit, cfg: Config,
                 *, need_stone: bool, state=None) -> None:
    """One worker action: sell, buy, upgrade, mine, or head for a vendor."""
    obs = world.obs
    if actor.unit_id in plan.commands:
        return

    vendor = obs.first_zone(R.ZONE_VENDOR)
    shop = obs.first_zone(R.ZONE_WEAPON_SHOP)

    # --- emergency station voucher outranks every other errand (R5) -------
    if _station_voucher_step(world, plan, cfg, state):
        return

    # --- sell what we carry once the batch is worth the trip -------------
    cargo = _sellable_kinds(world, state, actor, cfg, need_stone)
    if cargo and vendor is not None:
        kind, held = cargo[0]
        # The vendor sits at the map centre in every observed map: at night a
        # sale trip is not worth dying for (R3), so only sell if already there.
        if distance(actor.pos, vendor) <= 1:
            plan.take(actor.unit_id, sell_cmd(kind, held))
            plan.note(f"sell:{kind}x{held}")
            return
        if not world.night_now():
            if _move_towards(world, plan, actor, vendor, cfg):
                return

    # --- purchases and upgrades ------------------------------------------
    action = _purchase_plan(world, plan, actor, cfg, state)
    if action is not None:
        if action[0] == "walk":
            _move_towards(world, plan, actor, action[1], cfg)
            return
        plan.take(actor.unit_id, action[1])
        plan.gold_spent += action[2]
        return

    # --- spend the stone we are carrying on the next wall cell -------------
    if need_stone and actor.count(R.WALL_MATERIAL) >= 1:
        if plan_construction(world, plan, actor, cfg):
            return

    # --- mine -------------------------------------------------------------
    kinds = _mining_kinds(world, state, cfg, need_stone)
    taken = _other_locks(world, state, actor)
    if _mine_step(world, plan, actor, cfg, kinds, state, taken):
        return

    # --- nothing to do: hold the safe rally point / the tower hub ---------
    goal = world.operator_hub() or world.station_anchor()
    if goal is not None:
        _move_towards(world, plan, actor, goal, cfg)


def _mining_kinds(world: WorldView, state, cfg: Config,
                  need_stone: bool) -> tuple[str, ...]:
    """Ore kinds worth mining right now, best first."""
    order = list(cfg.mineral_priority)
    if need_stone and R.ZONE_STONE in order:
        order.remove(R.ZONE_STONE)
        order.insert(0, R.ZONE_STONE)
    available = {k for k in order if any(
        world.obs.zones.get(p) == k for p in world.obs.mine_cells())}
    live = tuple(k for k in order
                 if k in available and _mineable_on(world, state, k))
    return live or tuple(order)


def _other_locks(world: WorldView, state, actor: Unit) -> frozenset[Pos]:
    """Cells already claimed by the other worker, to avoid double-teaming a mine."""
    if state is None or not getattr(state, "mine_targets", None):
        return frozenset()
    other = [Pos(int(e["x"]), int(e["y"]))
             for rid, e in state.mine_targets.items() if rid != actor.unit_id]
    return frozenset(other)


def _sellable_kinds(world: WorldView, state, actor: Unit, cfg: Config,
                    need_stone: bool) -> list[tuple[str, int]]:
    """``[(ore, count)]`` worth a trip to the vendor, richest first.

    Two rules keep the worker mining instead of walking (R4):

    * a batch must be worth the journey -- ``count * price`` has to cover
      ``sell_trip_weight`` gold per cell of travel to the vendor -- unless the
      backpack is full;
    * stone the wall plan still needs is kept back, and ore the news says is
      about to rise in price is held.
    """
    obs = world.obs
    prices = _price_table(world)
    calendar = getattr(state, "price_calendar", None) if state is not None else None
    vendor = obs.first_zone(R.ZONE_VENDOR)
    travel = distance(actor.pos, vendor) if vendor is not None else 20
    out: list[tuple[str, int]] = []
    for kind in (R.ZONE_COPPER, R.ZONE_IRON, R.ZONE_STONE):
        held = actor.count(kind)
        if held <= 0:
            continue
        if kind == R.ZONE_STONE and need_stone:
            held = max(0, held - cfg.stone_target)
        # A count floor alone is not enough: 5 stone and 5 copper both pass a
        # ">= 5" test but only one of them pays for the walk.
        worth_it = (held * _price_on(world, state, kind, prices)
                    >= cfg.sell_trip_weight * max(1, travel))
        if not (actor.backpack_full or (held >= cfg.sell_batch_min and worth_it)):
            continue
        if calendar is not None and held < actor.capacity:
            today = _price_on(world, state, kind, prices)
            tomorrow = prices.get(kind, 1) * calendar.multiplier(
                kind, obs.day_index + 1)
            if tomorrow > today * 1.2:
                continue                  # the rumour says wait: sell dearer later
        out.append((kind, held))
    out.sort(key=lambda item: (-_price_on(world, state, item[0], prices)
                               * item[1], item[0]))
    return out


def _upgrade_order(world: WorldView, tower: Unit) -> tuple:
    """R2: upgrade the weapon closest to the front line first.

    ``front_ness`` is the projection of the tower onto the base→map-interior
    axis, so with the planned layout the tower nearest the robots is upgraded
    before the two behind it.  Distance from the nearest live robot is the
    tie-break, then level and id for determinism.
    """
    robots = [r for r in world.obs.robots if r.alive]
    nearest = min((distance(tower.pos, r.pos) for r in robots), default=99)
    return (-world.front_ness(tower.pos), nearest, tower.level, tower.unit_id)


def _purchase_plan(world: WorldView, plan: TurnPlan, actor: Unit, cfg: Config,
                   state=None):
    """Return ``("walk", pos)``, ``("cmd", command, gold)`` or ``None``.

    Priority (R5): 前期尽量先购买武器升级券和基地升级券, and the station voucher
    is *bought and held* -- it is a full heal, not a stat bump, so it is only
    spent by :func:`_station_voucher_step` when the base is about to fall.
    """
    obs = world.obs
    shop = obs.first_zone(R.ZONE_WEAPON_SHOP)
    if shop is None:
        return None
    spendable = max(0, plan.available_gold(obs) - cfg.gold_reserve)

    towers = sorted(obs.towers(), key=lambda t: _upgrade_order(world, t))
    station = obs.station()

    # 1) weapon upgrades: range + damage, the strongest gold->power channel and
    #    the thing that actually keeps the base alive on nights 1-3.
    for tower in towers:
        if tower.level >= 3:
            continue
        voucher = (R.VOUCHER_WEAPON_1 if tower.level == 1
                   else R.VOUCHER_WEAPON_2)
        if spendable >= R.SHOP_PRICE[voucher]:
            return _ensure_voucher(world, plan, actor, voucher, tower.pos, shop)

    # 2) one station voucher, bought early and kept in the backpack.
    if cfg.station_voucher_reserve and station is not None and station.level < 3:
        voucher = (R.VOUCHER_STATION_1 if station.level == 1
                   else R.VOUCHER_STATION_2)
        if not any(u.count(voucher) >= 1 for u in obs.fighters()):
            if spendable >= R.SHOP_PRICE[voucher]:
                return _ensure_voucher(world, plan, actor, voucher, station.pos,
                                       shop, use_now=False)

    # 3) emergency consumables
    if station is not None and station.health < R.BUILDING_HP[R.STATION][0] * 0.5:
        walls = [w for w in obs.walls() if w.health < R.BUILDING_HP[R.WALL][0]]
        if walls and spendable >= R.SHOP_PRICE[R.WALL_FIXER]:
            return _ensure_voucher(world, plan, actor, R.WALL_FIXER, walls[0].pos, shop)
    if actor.health < 132 and spendable >= R.SHOP_PRICE[R.MEDICINE]:
        return _ensure_voucher(world, plan, actor, R.MEDICINE, actor.pos, shop)

    # 4) leftover gold: another weapon level before any wall polish.  Sorting by
    #    price alone let cheap 20-gold wall vouchers drain the bank while the
    #    rocket battery stayed at level 2 (R2: 升级优先升级靠近机器人一侧的武器).
    candidates = _spare_upgrades(world, station)
    candidates.sort(key=lambda item: (item[0], item[1], item[2].y, item[2].x))
    for price, voucher, target in candidates:
        if spendable >= price:
            return _ensure_voucher(world, plan, actor, voucher, target, shop)
    return None


def _spare_upgrades(world: WorldView,
                    station: Unit | None) -> list[tuple[int, str, Pos]]:
    """``(price, voucher, target)`` still worth buying, weapons ranked first."""
    weapons: list[tuple[int, str, Pos]] = []
    walls: list[tuple[int, str, Pos]] = []
    for tower in world.obs.towers():
        if tower.level == 1:
            weapons.append((0, R.VOUCHER_WEAPON_1, tower.pos))
        elif tower.level == 2:
            weapons.append((0, R.VOUCHER_WEAPON_2, tower.pos))
    for wall in world.obs.walls():
        if wall.level == 1:
            walls.append((1, R.VOUCHER_WALL_1, wall.pos))
        elif wall.level == 2:
            walls.append((1, R.VOUCHER_WALL_2, wall.pos))
    priced = [(R.SHOP_PRICE[v], v, p) for _rank, v, p in weapons + walls]
    order = {v: rank for rank, v, _p in weapons + walls}
    return sorted(priced, key=lambda item: (order.get(item[1], 9), item[0]))


def _ensure_voucher(world: WorldView, plan: TurnPlan, actor: Unit, item: str,
                    target: Pos, shop: Pos, *, use_now: bool = True):
    """Buy it if missing, then walk into range and use it."""
    obs = world.obs
    if actor.count(item) < 1:
        if plan.available_gold(obs) < R.SHOP_PRICE.get(item, 0):
            return None
        if distance(actor.pos, shop) <= 1:
            return ("cmd", buy_cmd(item, 1), R.SHOP_PRICE.get(item, 0))
        return ("walk", shop)
    if not use_now:
        return None                      # hold it: _station_voucher_step decides
    if item in (R.VOUCHER_STATION_1, R.VOUCHER_STATION_2):
        target = _station_target(world, actor)
    if distance(actor.pos, target) <= 1:
        return ("cmd", use_cmd(item, target), 0)
    return ("walk", target)


# ---------------------------------------------------------------------------
# 3. construction
# ---------------------------------------------------------------------------
def plan_construction(world: WorldView, plan: TurnPlan, actor: Unit,
                      cfg: Config) -> bool:
    """Build the next tower or wall; returns True when an action was issued.

    Towers come first and are chosen from ``cfg.tower_loadout`` by *slot index*
    rather than by "which kind is missing": the slot also decides which of the
    three planned cells is used, so the loadout stays in the intended order even
    after a tower is destroyed and rebuilt.
    """
    obs = world.obs
    if actor.unit_id in plan.commands:
        return False

    towers = obs.towers()
    # A build issued last round may not show up in this observation yet, so count
    # in-flight sites: otherwise two workers race for the 3rd and 4th tower and
    # the 4th is rejected -- a genuine illegal command, since 任务书 §4.5.1 caps
    # weapons at 3 globally.
    pending = _pending_towers(world, towers)
    if len(towers) + pending < R.MAX_TOWERS and obs.gold >= R.WEAPON_BUILD_COST:
        kind = _next_tower_kind(towers, pending, cfg)
        if _build_at(world, plan, actor, kind, cfg):
            return True

    walls = obs.walls()
    plan_cells = world.wall_plan(cfg)
    missing = [c for c in plan_cells
               if not any(w.alive and w.pos == c for w in walls)]
    if missing and actor.count(R.WALL_MATERIAL) >= 1:
        if _build_at(world, plan, actor, R.WALL, cfg, targets=missing):
            return True
    return False


def _next_tower_kind(towers: tuple[Unit, ...], pending: int,
                     cfg: Config) -> str:
    """The loadout slot for the next tower.

    任务书 §4.5.1 allows any mix of the three weapons; the default loadout is
    three rockets, because at level 1 a rocket already reaches 10 cells and
    splashes eight neighbours while a gatling reaches 3 for the same 10 damage.
    """
    loadout = tuple(cfg.tower_loadout) or R.TOWER_TYPES
    index = len(towers) + max(0, pending)
    if index >= len(loadout):
        have = {t.kind for t in towers}
        for kind in R.TOWER_TYPES:
            if kind not in have:
                return kind
        index = len(loadout) - 1
    kind = loadout[max(0, min(index, len(loadout) - 1))]
    return kind if kind in R.TOWER_TYPES else R.ROCKET


def _pending_towers(world: WorldView, towers: tuple[Unit, ...]) -> int:
    """Tower sites we tried to build last round that are not standing yet."""
    sites = getattr(world, "pending_tower_sites", None)
    if not sites:
        return 0
    return sum(1 for pos in sites
               if not any(t.alive and t.pos == pos for t in towers))


def _build_at(world: WorldView, plan: TurnPlan, actor: Unit, kind: str,
              cfg: Config, targets: tuple[Pos, ...] | None = None) -> bool:
    obs = world.obs
    name = R.BUILD_NAME[kind]
    candidates = targets if targets is not None else world.build_candidates(kind)
    if not candidates:
        return False

    # One BFS from the actor ranks every candidate's stand cells at once; the
    # previous version ran a full A* for each candidate in turn.
    field = distances_from(world, actor, (actor.pos,), cfg)
    best: tuple[int, Pos, Pos] | None = None        # (distance, stand, target)
    for target in candidates:
        if plan.reserved_steps and (target.x, target.y) in plan.reserved_steps:
            continue
        if distance(actor.pos, target) == 1:
            best = (0, target, target)
            break
        for stand in _stand_cells(world, target, actor):
            if stand == target:
                continue
            if stand in plan.reserved_steps and stand != actor.pos:
                continue
            reach = field.get(stand)
            if reach is None:
                continue
            key = (reach, stand.x, stand.y)
            if best is None or key < (best[0], best[1].x, best[1].y):
                best = (reach, stand, target)
    if best is None:
        return False
    reach, stand, target = best
    if reach == 0 and stand == target:
        if not plan.reserve(target):
            return False
        plan.take(actor.unit_id, build_cmd(target, name))
        plan.build_attempt = (actor.unit_id, target, kind)
        if kind != R.WALL and target not in world.pending_tower_sites:
            world.pending_tower_sites.append(target)
            del world.pending_tower_sites[:-4]
        plan.note(f"build:{name}@{target.x},{target.y}")
        return True
    step = _walk(world, plan, actor, stand, cfg)
    if step is None:
        return False
    plan.take(actor.unit_id, move_cmd(step))
    return True


def consume_build_feedback(world: WorldView, state) -> None:
    """Learn from last turn's build attempt (guarded by full preconditions).

    ``false`` is the union of every failure mode, so a cell may only be marked
    ILLEGAL when the attempt happened with every precondition independently
    verified; otherwise the failure is not evidence about the cell.
    """
    attempt = getattr(state, "last_build_attempt", None)
    if not attempt:
        return
    obs = world.obs
    role_id, pos, kind, _round_no, preconditions_ok = attempt
    state.last_build_attempt = None
    if not preconditions_ok:
        return
    accepted = obs.last_action_results.get(role_id)
    name = R.BUILD_NAME.get(kind, kind)
    if accepted is True:
        world.knowledge.mark_legal(pos, name)
        anchor = world.station_anchor()
        if anchor is not None:
            world.knowledge.infer_pattern(anchor)
    elif world.knowledge.note_failure(pos, name, world.cfg.build_fail_limit):
        world.notes.append(f"site_illegal:{pos.x},{pos.y}:{name}")


def note_build_attempt(state, world: WorldView, actor: Unit, pos: Pos,
                       kind: str) -> None:
    state.last_build_attempt = (
        actor.unit_id, pos, kind, world.obs.round_no,
        build_attempt_preconditions_ok(world.obs, actor, pos, kind),
    )


# ---------------------------------------------------------------------------
# day planning (entry point used by the engine)
# ---------------------------------------------------------------------------
def plan_day(world: WorldView, plan: TurnPlan, cfg: Config, state,
             machine) -> None:
    """Sequence one daylight turn.

    Daylight is the only time that can be *invested*: 2100 actions against a
    rigid demand of roughly 28-35 %.  Night has an 1800-action budget and zero
    slack, so everything non-combat belongs here.

    v2 changes the order of business at the start of a match: three towers in the
    first three rounds beat a stone hoard, because night 1 arrives at round 71
    whether or not the wall exists.
    """
    plan_construction_then_economy(world, plan, cfg, state, machine)


def assign_duties(world: WorldView, state) -> tuple[Unit | None, Unit | None]:
    """``(defender, miner)`` -- stable worker duties (v2).

    接口文档 §1.3.1 fixes worker1/worker2 ids for the whole match, so a duty
    assigned once survives death and respawn.  The defender owns the towers and
    the station voucher; the miner ranges further for ore.
    """
    workers = sorted(world.obs.workers(), key=lambda u: u.unit_id)
    if not workers:
        return None, None
    if state is None or not hasattr(state, "duties"):
        return workers[0], (workers[1] if len(workers) > 1 else None)
    remembered = state.duties.get("defender")
    defender = next((w for w in workers if w.unit_id == remembered), None)
    if defender is None:
        defender = workers[0]
        state.duties["defender"] = defender.unit_id
    miner = next((w for w in workers if w.unit_id != defender.unit_id), None)
    if miner is not None:
        state.duties["miner"] = miner.unit_id
    return defender, miner


def plan_construction_then_economy(world: WorldView, plan: TurnPlan,
                                   cfg: Config, state, machine) -> None:
    obs = world.obs
    pioneer = obs.pioneer()
    workers = obs.workers()
    defender, _miner = assign_duties(world, state)

    # --- pioneer: tasks are the only route to score_1 ---------------------
    if pioneer is not None:
        _pioneer_turn(world, plan, cfg, state, machine, pioneer)

    # --- workers: build first, then mine/sell/upgrade ---------------------
    need_stone = any(
        not any(w.alive and w.pos == c for w in obs.walls())
        for c in world.wall_plan(cfg)
    )
    towers = obs.towers()
    pending = _pending_towers(world, towers)
    opening = (cfg.towers_before_stone
               and len(towers) + pending < R.MAX_TOWERS)
    for worker in workers:
        if worker.unit_id in plan.commands:
            continue
        # R5 first: a held station voucher that is due, or a purchase the worker
        # can complete *this round from where it stands*, outranks every errand.
        # Walking detours for shopping stay at the bottom of the list.
        if _station_voucher_step(world, plan, cfg, state):
            continue
        if _heal_step(world, plan, worker):
            continue
        if opening:
            # R2/opening: the first 75 gold is three towers; a stone trip now
            # would delay the first night's only source of damage.
            if _pending_towers(world, obs.towers()) + len(obs.towers()) >= R.MAX_TOWERS:
                opening = False
            elif plan_construction(world, plan, worker, cfg):
                continue
        if _immediate_purchase(world, plan, worker, cfg, state):
            continue
        # Everything else (sell -> shop trip -> wall -> mine) is sequenced inside
        # plan_economy.  v1 ran wall construction *before* economy, so a worker
        # with a stone in its pack built walls forever and never walked to the
        # weapon shop: three towers stayed level 1 with 1200 gold in the bank.
        plan_economy(world, plan, worker, cfg, need_stone=need_stone,
                     state=state)


def _immediate_purchase(world: WorldView, plan: TurnPlan, actor: Unit,
                        cfg: Config, state) -> bool:
    """Complete a purchase/upgrade this round without leaving the spot."""
    action = _purchase_plan(world, plan, actor, cfg, state)
    if action is None or action[0] != "cmd":
        return False
    plan.take(actor.unit_id, action[1])
    plan.gold_spent += action[2]
    return True


def _pioneer_turn(world: WorldView, plan: TurnPlan, cfg: Config, state,
                  machine, pioneer: Unit) -> None:
    """Tasks while it is safe and profitable, the tower hub when it is not.

    R3 is explicit: 夜晚来临后…做任务和采集矿石都应该去地图两侧…以免被机器人打死
    （这样要到第二天第20回合才能复活，得不偿失）.  So once caution applies and the
    pioneer is not standing somewhere safe, the task is dropped (leaving the
    1-cell ring ends it, keeping whatever partial credit was already submitted)
    and the pioneer becomes a third tower operator for the night.
    """
    obs = world.obs
    if machine.in_task and world.night_now() and not world.safe_cell(pioneer.pos):
        plan.note("abandon_task_for_night")
        goal = _safe_rally_cell(world, pioneer.pos) or world.operator_hub()
        if goal is not None:
            _move_towards(world, plan, pioneer, goal, cfg)
        return

    # 民间传闻 宝藏: only ever attempted once the location *and* the exact
    # sacrifice are both known, because a legal-but-wrong sacrifice still
    # consumes the items (任务书 §4.6.3 note).  No item purchases are made on
    # speculation -- that would trade certain gold for an inferred altar.
    if not machine.in_task and _summon_step(world, plan, cfg, state, pioneer):
        return

    accept_ok = (not world.night_now()) or world.safe_cell(pioneer.pos)
    if not accept_ok and not machine.in_task:
        goal = _safe_rally_cell(world, pioneer.pos) or world.operator_hub()
        if goal is not None:
            _move_towards(world, plan, pioneer, goal, cfg)
        return

    task_plan = machine.plan(obs, pioneer)
    if task_plan.get("action"):
        plan.take(pioneer.unit_id, _task_command(task_plan))
    elif task_plan.get("exec"):
        # 接口文档 §2.1: executeCmd is the sandbox channel, legal only while a
        # task is live -- which is exactly when the machine emits it.
        plan.execute_cmd = str(task_plan["exec"])[:12000]
    elif task_plan.get("prompt"):
        plan.prompt = task_plan["prompt"]
    elif task_plan.get("goto") is not None:
        _move_towards(world, plan, pioneer, task_plan["goto"], cfg)
    elif machine.in_task:
        pass                         # stay on the task point: leaving ends it
    else:
        _move_towards(world, plan, pioneer, _idle_pioneer_goal(world, cfg), cfg)


def _summon_step(world: WorldView, plan: TurnPlan, cfg: Config, state,
                 pioneer: Unit) -> bool:
    """走向/开启祭坛宝藏 when the rumour hypothesis has converged.

    Deliberately conservative: ``should_summon`` demands a located altar, an open
    day window and every sacrifice item already in the backpack, so this can
    never fritter away gold on a guess.
    """
    hypothesis = getattr(state, "treasure", None) if state is not None else None
    if hypothesis is None or hypothesis.pos is None or hypothesis.confidence < 0.8:
        return False
    if not hypothesis.items:
        return False
    target = should_summon(world, hypothesis, pioneer)
    if target is not None:
        plan.take(pioneer.unit_id, summon_treasure(target, hypothesis.items))
        plan.note("summon_treasure")
        return True
    if distance(pioneer.pos, hypothesis.pos) <= 1:
        return False                     # nothing to sacrifice yet
    if world.night_now() or not world.safe_cell(hypothesis.pos):
        return False
    return _move_towards(world, plan, pioneer, hypothesis.pos, cfg)


def _task_command(task_plan: dict) -> dict:
    action = task_plan["action"]
    if action == "acceptTask":
        return accept_task()
    if action == "submitAnswer":
        return submit_answer(task_plan.get("answer", ""))
    return accept_task()


def _idle_pioneer_goal(world: WorldView, cfg: Config) -> Pos:
    """With no task available, hold station near the base (not across the map)."""
    hub = world.operator_hub()
    anchor = world.station_anchor()
    if anchor is None:
        point = world.obs.first_zone(R.ZONE_VENDOR)
        return point if point is not None else Pos(0, 0)
    if world.night_now() and hub is not None:
        return hub
    task = world.nearest_task_point(anchor)
    if task is not None:
        stands = sorted(task.stands(), key=lambda p: (p.x, p.y))
        if stands:
            return stands[0]
    return hub or anchor


def consume_feedback(world: WorldView, state) -> None:
    """Apply last round's judge feedback to long-lived memory."""
    obs = world.obs
    # errors[] carries the quota signal; never rely on it to stop ourselves,
    # but honour it when it arrives.
    for code, _description in obs.errors:
        if code == 5:
            state.llm.note_quota_error()
    consume_build_feedback(world, state)
    if state.price_calendar is None:
        state.price_calendar = PriceCalendar()
    if obs.day_index not in state.price_calendar.seen_days and obs.is_day:
        state.price_calendar.seen_days.add(obs.day_index)
        for event in parse_official_news(obs.news.official, obs.day_index):
            state.price_calendar.events.append(event)
    if state.treasure is None:
        state.treasure = TreasureHypothesis()
    state.treasure = update_treasure(state.treasure, obs.news.folk, obs.day_index)

# ---------------------------------------------------------------------------
# 4. intelligence -- official price/embargo calendar and treasure hypothesis
# ---------------------------------------------------------------------------
@dataclass
class PriceEvent:
    mineral: str
    start_day: int
    end_day: int
    mineable: bool = True
    price_multiplier: float = 1.0
    confidence: float = 0.3
    source: str = "news"


@dataclass
class TreasureHypothesis:
    pos: Pos | None = None
    day_window: tuple[int, int] | None = None
    items: tuple[str, ...] = ()
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)


@dataclass
class PriceCalendar:
    events: list[PriceEvent] = field(default_factory=list)
    seen_days: set[int] = field(default_factory=set)

    def mineable(self, mineral: str, day: int) -> bool:
        for event in self.events:
            if event.mineral != mineral:
                continue
            if event.start_day <= day <= event.end_day and not event.mineable:
                return False
        return True

    def multiplier(self, mineral: str, day: int) -> float:
        best = 1.0
        for event in self.events:
            if event.mineral == mineral and event.start_day <= day <= event.end_day:
                best = max(best, event.price_multiplier)
        return best

    def embargoed(self, day: int) -> tuple[str, ...]:
        return tuple(k for k in R.MINE_KINDS if not self.mineable(k, day))


_MINERAL_WORDS = {
    "石": R.ZONE_STONE, "stone": R.ZONE_STONE,
    "铁": R.ZONE_IRON, "iron": R.ZONE_IRON,
    "铜": R.ZONE_COPPER, "copper": R.ZONE_COPPER,
}
_DAY_WORDS = {"明天": 1, "明日": 1, "后天": 2, "大后天": 3, "今日": 0, "今天": 0}
_STOP_WORDS = ("停工", "停产", "无法采集", "塌方", "关闭", "停产", "禁止")
_RISE_WORDS = ("上涨", "涨价", "稀缺", "紧缺", "飙升")


def parse_official_news(text: str, day_index: int) -> list[PriceEvent]:
    """Best-effort keyword extraction; never guesses when nothing matches.

    Task book §5.1 only gives a worked example, so this extracts the pieces the
    example demonstrates (mineral, day window, mineable, direction) and leaves
    everything else to the model channel.  Confidence stays below 1.0 by design.
    """
    if not text or text.strip() in ("", "今日无重大新闻"):
        return []
    minerals: list[str] = []
    for word, kind in _MINERAL_WORDS.items():
        if word in text and kind not in minerals:
            minerals.append(kind)
    if not minerals:
        return []
    offset = 0
    for word, days in _DAY_WORDS.items():
        if word in text:
            offset = days
            break
    window = 1
    if "两天" in text or "2天" in text or "两天左右" in text:
        window = 2
    elif "三天" in text or "3天" in text:
        window = 3
    stopped = any(word in text for word in _STOP_WORDS)
    rising = any(word in text for word in _RISE_WORDS)
    events: list[PriceEvent] = []
    for kind in minerals:
        if stopped:
            events.append(PriceEvent(
                mineral=kind,
                start_day=day_index + max(1, offset),
                end_day=day_index + max(1, offset) + window - 1,
                mineable=False,
                price_multiplier=2.0 if rising else 1.0,
                confidence=0.4,
            ))
        elif rising:
            events.append(PriceEvent(
                mineral=kind,
                start_day=day_index,
                end_day=day_index + window,
                mineable=True,
                price_multiplier=1.5,
                confidence=0.3,
            ))
    return events


_TREASURE_ITEM_WORDS = {
    "石板": "AcientTablet", "石碑": "AcientTablet", "tablet": "AcientTablet",
    "沙": "StarSand", "sand": "StarSand",
    "烈焰": "FlameBreath", "焰": "FlameBreath", "flame": "FlameBreath",
    "寒霜": "FrostPotion", "霜": "FrostPotion", "frost": "FrostPotion",
    "荆棘": "ThornAmulet", "护符": "ThornAmulet", "thorn": "ThornAmulet",
    "铁哨": "IronWhistle", "哨": "IronWhistle", "whistle": "IronWhistle",
}


def update_treasure(hypothesis: TreasureHypothesis, folk: str,
                    day_index: int) -> TreasureHypothesis:
    """Accumulate folk-legend clues into a treasure hypothesis.

    The altar location, opening time and required items must all be inferred
    from multi-day folk legends; a wrong sacrifice still consumes the items, so
    the summon is only ever attempted once the item set is complete and the
    location has converged.
    """
    if not folk:
        return hypothesis
    if folk not in hypothesis.evidence:
        hypothesis.evidence.append(folk[:300])
    items = set(hypothesis.items)
    for word, item in _TREASURE_ITEM_WORDS.items():
        if word in folk:
            items.add(item)
    hypothesis.items = tuple(sorted(items))
    numbers = [int(n) for n in _numbers(folk)]
    if numbers and hypothesis.day_window is None and "天" in folk:
        hypothesis.day_window = (day_index, day_index + max(numbers))
    evidence_weight = min(len(hypothesis.evidence), 5) / 5.0
    item_weight = min(len(hypothesis.items), 6) / 6.0
    hypothesis.confidence = 0.5 * evidence_weight + 0.5 * item_weight
    if hypothesis.pos is None:
        hypothesis.pos = _coords_from(folk)
    return hypothesis


def _numbers(text: str) -> list[int]:
    out: list[int] = []
    current = ""
    for ch in text:
        if ch.isdigit():
            current += ch
        elif current:
            out.append(int(current))
            current = ""
    if current:
        out.append(int(current))
    return out


def _coords_from(text: str) -> Pos | None:
    """Only accept an explicit ``(x,y)`` style coordinate in the rumour."""
    import re
    match = re.search(r"\(\s*(\d{1,2})\s*[,，]\s*(\d{1,2})\s*\)", text)
    if not match:
        return None
    return Pos(int(match.group(1)), int(match.group(2)))


def should_summon(world: WorldView, hypothesis: TreasureHypothesis,
                  pioneer: Unit | None) -> Pos | None:
    """Only summon with a converged location, an open window and every item held."""
    if pioneer is None or hypothesis.pos is None or hypothesis.confidence < 0.8:
        return None
    if not hypothesis.items:
        return None
    obs = world.obs
    if hypothesis.day_window and not (
            hypothesis.day_window[0] <= obs.day_index <= hypothesis.day_window[1]):
        return None
    if distance(pioneer.pos, hypothesis.pos) > 1:
        return None
    for item in hypothesis.items:
        if pioneer.count(item) < 1:
            return None
    return hypothesis.pos


def intelligence_prompt(obs: Observation) -> str:
    """One high-value out-of-task model call: interpret the day's news."""
    return (
        "You are advising a tower-defense agent. Reply with at most 3 short "
        "lines, no preamble.\n"
        f"OFFICIAL NEWS: {obs.news.official[:800]}\n"
        f"FOLK LEGEND: {obs.news.folk[:800]}\n"
        "State: (1) which mineral is affected and for how many days, "
        "(2) whether it becomes unminable or its price rises, "
        "(3) any treasure location/time/item requirement you can infer."
    )

