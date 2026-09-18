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
from .model import Observation, Pos, Unit, distance
from . import navigation
from .navigation import distances_from, ordered_stands
from .world import (
    LEGAL, WorldView, build_attempt_preconditions_ok,
)
from .protocol import (
    accept_task, attack as attack_cmd, build as build_cmd, buy as buy_cmd,
    collect, drop as drop_cmd, move as move_cmd, remove as remove_cmd,
    sell as sell_cmd, submit_answer, use as use_cmd,
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

    def available_gold(self, obs: Observation) -> int:
        """Observed gold minus everything already committed this turn.

        Two workers each reading the same un-decremented ``goldNum`` is exactly
        how the baseline could spend 50 gold when it only had 25 (audit A13).
        """
        return max(0, obs.gold - self.gold_spent)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _walk(world: WorldView, plan: TurnPlan, actor: Unit, goal: Pos,
          cfg: Config, *, exclude: frozenset[Pos] = frozenset()) -> Pos | None:
    """One step towards ``goal``, honouring cells already claimed this turn.

    A single A* returns the whole path plus the distance field it explored, so a
    cell contested by another role is resolved by re-planning to that role's next
    preference **from the same search** instead of paying for one full-grid
    search per alternative (the baseline did exactly that: ~2400 searches in one
    daytime turn, peaking near a second).
    """
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
        fallback, _f, _c = navigation.search(world, actor, alternative, cfg)
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




def _mine_step(world: WorldView, plan: TurnPlan, actor: Unit, cfg: Config,
               kinds: tuple[str, ...]) -> bool:
    """Collect from an adjacent mine, or step towards the best one."""
    obs = world.obs
    adjacent = [p for p in obs.mine_cells()
                if distance(actor.pos, p) == 1
                and (not kinds or obs.zones.get(p) in kinds)]
    if adjacent:
        adjacent.sort(key=lambda p: (obs.zones.get(p, ""), p.x, p.y))
        plan.take(actor.unit_id, collect(adjacent[0]))
        return True
    if actor.backpack_full:
        return False
    mines = [p for p in obs.mine_cells() if not kinds or obs.zones.get(p) in kinds]
    if not mines:
        return False
    field = distances_from(world, actor, (actor.pos,), cfg)
    best: tuple[int, Pos, Pos] | None = None
    for mine in mines:
        for stand in _stand_cells(world, mine, actor):
            reach = field.get(stand)
            if reach is None:
                continue
            if best is None or (reach, mine.x, mine.y) < (best[0], best[2].x,
                                                          best[2].y):
                best = (reach, stand, mine)
    if best is None:
        return False
    reach, stand, mine = best
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


def plan_night(world: WorldView, plan: TurnPlan, cfg: Config,
               *, pioneers_excluded: frozenset[int] = frozenset()) -> None:
    obs = world.obs
    robots = tuple(world.threat_robots())
    ledger = combat.DamageLedger()

    fighters = tuple(u for u in obs.fighters() if u.unit_id not in pioneers_excluded)
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
        aims = combat.aim_for(tower, robots, ledger, cfg)
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
    for tower in towers:
        controller = assignment.get(tower.unit_id)
        if controller is None:
            continue
        if controller.unit_id in claimed_roles or controller.unit_id in plan.commands:
            continue
        stands = ordered_stands(world, controller, (tower.pos,), cfg=cfg)
        stands = [s for s in stands if distance(s, tower.pos) <= 1]
        if not stands:
            stands = [s for s in _stand_cells(world, tower.pos, controller)]
        if not stands:
            continue
        goal = stands[0]
        if controller.pos == goal:
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
        spare.append(actor)
    _shelter(world, plan, tuple(spare), cfg)


def _use_consumable(world: WorldView, plan: TurnPlan, actor: Unit,
                    robots: tuple, cfg: Config) -> bool:
    """Spend a held Bomb/DizzyWeapon, buying one first only if affordable."""
    obs = world.obs
    if actor.unit_id in plan.commands:
        return False
    aim = combat.bomb_cluster(obs, robots, cfg)
    item = R.BOMB
    if aim is None:
        aim = combat.dizzy_target(obs, robots, cfg)
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
    """Idle roles fall back to the base interior; never stand in the open."""
    obs = world.obs
    cells = world.station_cells() or ()
    if not cells:
        return
    for actor in actors:
        if actor.unit_id in plan.commands:
            continue
        stands = [c for c in cells if c != actor.pos]
        if not stands:
            continue
        stands.sort(key=lambda c: (distance(actor.pos, c), c.x, c.y))
        step = _walk(world, plan, actor, stands[0], cfg)
        if step is not None:
            plan.take(actor.unit_id, move_cmd(step))


# ---------------------------------------------------------------------------
# 2. economy
# ---------------------------------------------------------------------------


def _inventory(actor: Unit, item: str) -> int:
    return actor.count(item)


def plan_economy(world: WorldView, plan: TurnPlan, actor: Unit, cfg: Config,
                 *, need_stone: bool) -> None:
    """One worker action: sell, buy, upgrade, mine, or head for a vendor."""
    obs = world.obs
    if actor.unit_id in plan.commands:
        return

    vendor = obs.first_zone(R.ZONE_VENDOR)
    shop = obs.first_zone(R.ZONE_WEAPON_SHOP)

    # --- sell what we carry once the batch is worth the trip -------------
    for kind in (R.ZONE_COPPER, R.ZONE_IRON, R.ZONE_STONE):
        held = _inventory(actor, kind)
        if kind == R.ZONE_STONE and need_stone:
            held = max(0, held - cfg.stone_target)
        if held >= cfg.sell_batch_min and vendor is not None:
            if distance(actor.pos, vendor) <= 1:
                plan.take(actor.unit_id, sell_cmd(kind, held))
                return
            if _move_towards(world, plan, actor, vendor, cfg):
                return

    # --- purchases and upgrades ------------------------------------------
    action = _purchase_plan(world, plan, actor, cfg)
    if action is not None:
        if action[0] == "walk":
            _move_towards(world, plan, actor, action[1], cfg)
            return
        plan.take(actor.unit_id, action[1])
        plan.gold_spent += action[2]
        return

    # --- mine -------------------------------------------------------------
    kinds = tuple(cfg.mineral_priority)
    if need_stone and R.ZONE_STONE in obs.zones.values():
        kinds = (R.ZONE_STONE, *[k for k in cfg.mineral_priority if k != R.ZONE_STONE])
    if _mine_step(world, plan, actor, cfg, kinds):
        return

    # --- nothing to do: rally to the next build site / base --------------
    anchor = world.station_anchor()
    if anchor is not None:
        _move_towards(world, plan, actor, anchor, cfg)


def _purchase_plan(world: WorldView, plan: TurnPlan, actor: Unit, cfg: Config):
    """Return ``("walk", pos)``, ``("cmd", command, gold)`` or ``None``."""
    obs = world.obs
    shop = obs.first_zone(R.ZONE_WEAPON_SHOP)
    if shop is None:
        return None
    spendable = max(0, plan.available_gold(obs) - cfg.gold_reserve)

    towers = obs.towers()
    station = obs.station()
    # 1) station upgrade: +1500 HP and a full heal, worth 15 survival points/100g
    if station is not None and station.level < 3:
        voucher = (R.VOUCHER_STATION_1 if station.level == 1
                   else R.VOUCHER_STATION_2)
        if spendable >= R.SHOP_PRICE[voucher]:
            return _ensure_voucher(world, plan, actor, voucher, station.pos, shop)
    # 2) weapon upgrades: range + damage, the strongest gold->power channel
    for tower in towers:
        if tower.level >= 3:
            continue
        voucher = (R.VOUCHER_WEAPON_1 if tower.level == 1 else R.VOUCHER_WEAPON_2)
        if spendable >= R.SHOP_PRICE[voucher]:
            return _ensure_voucher(world, plan, actor, voucher, tower.pos, shop)
    # 3) emergency consumables
    if station is not None and station.health < R.BUILDING_HP[R.STATION][0] * 0.5:
        walls = [w for w in obs.walls() if w.health < R.BUILDING_HP[R.WALL][0]]
        if walls and spendable >= R.SHOP_PRICE[R.WALL_FIXER]:
            return _ensure_voucher(world, plan, actor, R.WALL_FIXER, walls[0].pos, shop)
    if actor.health < 100 and spendable >= R.SHOP_PRICE[R.MEDICINE]:
        return _ensure_voucher(world, plan, actor, R.MEDICINE, actor.pos, shop)
    return None


def _ensure_voucher(world: WorldView, plan: TurnPlan, actor: Unit, item: str,
                    target: Pos, shop: Pos):
    """Buy it if missing, then walk into range and use it."""
    obs = world.obs
    if actor.count(item) < 1:
        if plan.available_gold(obs) < R.SHOP_PRICE.get(item, 0):
            return None
        if distance(actor.pos, shop) <= 1:
            return ("cmd", buy_cmd(item, 1), R.SHOP_PRICE.get(item, 0))
        return ("walk", shop)
    if distance(actor.pos, target) <= 1:
        return ("cmd", use_cmd(item, target), 0)
    return ("walk", target)


# ---------------------------------------------------------------------------
# 3. construction
# ---------------------------------------------------------------------------
def plan_construction(world: WorldView, plan: TurnPlan, actor: Unit,
                      cfg: Config) -> bool:
    """Build the next tower or wall; returns True when an action was issued."""
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
        kind = _next_tower_kind(towers)
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


def _next_tower_kind(towers: tuple[Unit, ...]) -> str:
    """Diversify: gatling for cheap multi-target, railgun for reach, rocket for splash."""
    have = {t.kind for t in towers}
    for kind in R.TOWER_TYPES:
        if kind not in have:
            return kind
    return R.GATLING


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
    """
    obs = world.obs

    plan_construction_then_economy(world, plan, cfg, state, machine)


def plan_construction_then_economy(world: WorldView, plan: TurnPlan,
                                   cfg: Config, state, machine) -> None:
    obs = world.obs
    pioneer = obs.pioneer()
    workers = obs.workers()

    # --- pioneer: tasks are the only route to score_1 ---------------------
    if pioneer is not None:
        task_plan = machine.plan(obs, pioneer)
        if task_plan.get("action"):
            plan.take(pioneer.unit_id, _task_command(task_plan))
        elif task_plan.get("prompt"):
            plan.prompt = task_plan["prompt"]
        elif task_plan.get("goto") is not None:
            _move_towards(world, plan, pioneer, task_plan["goto"], cfg)
        elif machine.in_task:
            pass                     # stay on the task point: leaving ends it
        else:
            _move_towards(world, plan, pioneer, _idle_pioneer_goal(world, cfg), cfg)

    # --- workers: build first, then mine/sell/upgrade ---------------------
    need_stone = any(
        not any(w.alive and w.pos == c for w in obs.walls())
        for c in world.wall_plan(cfg)
    )
    for worker in workers:
        if worker.unit_id in plan.commands:
            continue
        if need_stone and worker.count(R.WALL_MATERIAL) < cfg.stone_target:
            if _mine_step(world, plan, worker, cfg,
                          (R.ZONE_STONE, *[k for k in cfg.mineral_priority
                                           if k != R.ZONE_STONE])):
                continue
        if plan_construction(world, plan, worker, cfg):
            continue
        plan_economy(world, plan, worker, cfg, need_stone=need_stone)


def _task_command(task_plan: dict) -> dict:
    action = task_plan["action"]
    if action == "acceptTask":
        return accept_task()
    if action == "submitAnswer":
        return submit_answer(task_plan.get("answer", ""))
    return accept_task()


def _idle_pioneer_goal(world: WorldView, cfg: Config) -> Pos:
    """With no task available, hold station near the base (not across the map)."""
    anchor = world.station_anchor()
    if anchor is None:
        point = world.obs.first_zone(R.ZONE_VENDOR)
        return point if point is not None else Pos(0, 0)
    task = world.nearest_task_point(anchor)
    if task is not None:
        stands = sorted(task.stands(), key=lambda p: (p.x, p.y))
        if stands:
            return stands[0]
    return anchor


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

