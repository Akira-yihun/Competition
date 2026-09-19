"""Final legality gate: the last line of defence against the five-exception rule.

Design rule: **the guard only ever removes commands.**  It never invents a
better action, because that would be a second, hidden strategy layer.  Every
rejection carries a reason code so a systematic mistake shows up in telemetry
instead of repeating silently every turn (the baseline's worst property: it
dropped the judge's feedback on the floor and re-sent the same illegal command
until the team was disqualified).

The rejection list is derived from the "指令错误" definition in 任务书 §8:
missing fields, unknown action codes, and wrong JSON types all count as
exceptions, so all of them are checked here.  Rule-level impossibilities that
the task book explicitly calls 执行失败 (collisions, no target at the aim point)
are *not* filtered -- rejecting them would discard legal, useful actions.
"""
from __future__ import annotations

from typing import Any, Mapping

from .config import DEFAULT, Config
from .model import Observation, Pos, Unit, distance
from .world import WorldView
from . import rules as R


class GuardResult:
    __slots__ = ("commands", "rejections")

    def __init__(self, commands: dict[str, dict], rejections: list[tuple[str, str]]):
        self.commands = commands
        self.rejections = rejections

    @property
    def ok(self) -> bool:
        return not self.rejections


def _as_units(obs: Observation) -> dict[int, Unit]:
    return {u.unit_id: u for u in obs.roles}


def _targets(command: Mapping[str, Any]) -> list[dict]:
    raw = command.get("targetPos")
    return raw if isinstance(raw, list) else []


def validate(command: Any, actor: Unit | None, obs: Observation, world: WorldView,
             cfg: Config = DEFAULT) -> tuple[bool, str]:
    """Return ``(is_legal, reason)`` for one command."""
    if not isinstance(command, dict):
        return False, "command:not_object"
    action = command.get("action")
    if not isinstance(action, str) or action not in R.ACTIONS:
        return False, "action:unknown"

    # ---- field presence -------------------------------------------------
    targets = _targets(command)
    if action in R.ACTIONS_NEEDING_TARGET and not targets:
        return False, "targetPos:missing"
    if action in R.ACTIONS_NEEDING_NAME:
        name = command.get("name")
        if not isinstance(name, str) or not name:
            return False, "name:missing"
    if action == "attack" and not command.get("controllerId"):
        return False, "controllerId:missing"
    if action == "submitAnswer" and not isinstance(command.get("taskAnswer"), str):
        return False, "taskAnswer:missing"
    if action == "summonTreasure" and not isinstance(command.get("item"), list):
        return False, "item:missing"

    # ---- field types (wrong types count as exceptions too) --------------
    for field in ("name", "taskAnswer", "controllerId"):
        if field in command and not isinstance(command[field], str):
            return False, f"{field}:not_string"
    if "num" in command and not isinstance(command["num"], int):
        return False, "num:not_int"
    if "item" in command:
        items = command["item"]
        if not isinstance(items, list) or any(not isinstance(i, str) for i in items):
            return False, "item:not_string_list"
    if not isinstance(targets, list):
        return False, "targetPos:not_list"
    for point in targets:
        if not isinstance(point, dict):
            return False, "targetPos:not_object"
        if type(point.get("x")) is not int or type(point.get("y")) is not int:
            return False, "targetPos:not_int_pair"

    # ---- target count ---------------------------------------------------
    if action == "attack":
        expected = _expected_targets(command, obs)
        if expected is None:
            return False, "attack:tower_unknown"
        if len(targets) != expected:
            return False, f"attack:want_{expected}_got_{len(targets)}"
    elif len(targets) > 1:
        return False, "targetPos:too_many"

    # ---- bounds ---------------------------------------------------------
    for point in targets:
        pos = Pos(point["x"], point["y"])
        if not obs.in_bounds(pos):
            return False, "targetPos:out_of_bounds"

    # ---- phase / role constraints ---------------------------------------
    if actor is None:
        return False, "actor:unknown"
    if action in R.DAY_ONLY_ACTIONS:
        if not obs.is_day:
            return False, "build:at_night"
        if obs.phase_round > 68:      # +-1 buffer around the day/night edge
            return False, "build:phase_edge"
    if action in R.NIGHT_ONLY_ACTIONS:
        if obs.is_day:
            return False, "attack:at_day"
        if obs.phase_round < 72:
            return False, "attack:phase_edge"
    if action in R.WORKER_ONLY_ACTIONS and actor.kind != R.WORKER:
        return False, f"{action}:worker_only"
    if action in R.PIONEER_ONLY_ACTIONS and actor.kind != R.PIONEER:
        return False, f"{action}:pioneer_only"

    # ---- per-action preconditions ---------------------------------------
    if action == "move":
        return _check_move(Pos(targets[0]["x"], targets[0]["y"]), actor, obs, world)
    if action == "attack":
        return _check_attack(command, targets, actor, obs, world, cfg)
    if action == "build":
        return _check_build(command, Pos(targets[0]["x"], targets[0]["y"]),
                            actor, obs, cfg)
    if action == "collect":
        return _check_collect(Pos(targets[0]["x"], targets[0]["y"]), actor, obs)
    if action == "remove":
        return _check_remove(Pos(targets[0]["x"], targets[0]["y"]), actor, obs)
    if action == "use":
        return _check_use(command, targets, actor, obs)
    if action == "summonTreasure":
        return _check_summon(command, targets, actor, obs)
    return True, "ok"


def obs_tower_for_command(command: Mapping[str, Any], obs: Observation) -> Unit | None:
    """The tower id is carried as ``__towerId`` by the combat policy.

    ``controllerId`` names the *role* that operates the weapon, not the weapon
    (接口文档 §1.3.1 assigns distinct id blocks: 10020-10022 gatling,
    10030-10032 railgun, 10040-10042 rocket), so the weapon id travels in an
    internal key that is stripped before serialisation.
    """
    tid = command.get("__towerId")
    if not isinstance(tid, int):
        return None
    for unit in obs.roles:
        if unit.unit_id == tid and unit.is_tower:
            return unit
    return None


def _expected_targets(command: Mapping[str, Any], obs: Observation) -> int | None:
    """Target count mandated by 接口文档 §2.2: ``level`` for gatling/rocket, 1 for railgun."""
    tower = obs_tower_for_command(command, obs)
    if tower is None:
        return None
    return 1 if tower.kind == R.RAILGUN else max(1, tower.level)


def _strip_internal(command: dict) -> dict:
    return {k: v for k, v in command.items() if not k.startswith("__")}


def _check_move(target: Pos, actor: Unit, obs: Observation,
                world: WorldView) -> tuple[bool, str]:
    if distance(actor.pos, target) != 1:
        return False, "move:not_adjacent"
    static = world.blocked_cells()
    if target in static and target != actor.pos:
        return False, "move:onto_building"
    if target in obs.zones:
        return False, "move:onto_zone"
    return True, "ok"


def _check_build(command: Mapping[str, Any], target: Pos, actor: Unit,
                 obs: Observation, cfg: Config) -> tuple[bool, str]:
    if distance(actor.pos, target) != 1:
        return False, "build:not_adjacent"
    if target in obs.zones:
        return False, "build:onto_zone"
    if target in WorldView(obs, cfg).blocked_cells():
        return False, "build:occupied"
    name = command.get("name")
    kind = _kind_for_name(name)
    if kind is None:
        return False, "build:unknown_name"
    if kind == R.WALL and actor.count(R.WALL_MATERIAL) < 1:
        return False, "build:no_stone"
    if kind != R.WALL:
        if obs.gold < R.WEAPON_BUILD_COST:
            return False, "build:no_gold"
        # Only *standing* weapons count: a destroyed tower's cell is free again,
        # and counting the wreck would make rebuilding impossible.
        if len(obs.towers()) >= R.MAX_TOWERS:
            return False, "build:tower_cap"
    return True, "ok"


def _kind_for_name(name: Any) -> str | None:
    for kind, build_name in R.BUILD_NAME.items():
        if name == build_name:
            return kind
    return None


def _check_collect(target: Pos, actor: Unit, obs: Observation) -> tuple[bool, str]:
    if distance(actor.pos, target) != 1:
        return False, "collect:not_adjacent"
    if obs.zones.get(target) not in R.MINE_KINDS:
        return False, "collect:not_a_mine"
    if actor.backpack_full:
        return False, "collect:backpack_full"
    return True, "ok"


def _check_remove(target: Pos, actor: Unit, obs: Observation) -> tuple[bool, str]:
    if distance(actor.pos, target) != 1:
        return False, "remove:not_adjacent"
    if not any(w.alive and w.pos == target for w in obs.walls()):
        return False, "remove:no_wall"
    return True, "ok"


def _check_use(command: Mapping[str, Any], targets: list[dict], actor: Unit,
               obs: Observation) -> tuple[bool, str]:
    name = command.get("name")
    if name not in R.SHOP_PRICE and name not in R.TASK_ITEMS:
        return False, "use:unknown_item"
    if actor.count(str(name)) < 1:
        return False, "use:not_in_backpack"
    if name in R.USE_NEEDS_TARGET:
        if not targets:
            return False, "use:target_required"
        pos = Pos(targets[0]["x"], targets[0]["y"])
        if name == R.WALL_FIXER:
            if distance(actor.pos, pos) > 1:
                return False, "use:wallfixer_distance"
            if not any(w.alive and w.pos == pos for w in obs.walls()):
                return False, "use:wallfixer_no_wall"
        elif name in (R.VOUCHER_WEAPON_1, R.VOUCHER_WEAPON_2,
                      R.VOUCHER_STATION_1, R.VOUCHER_STATION_2,
                      R.VOUCHER_WALL_1, R.VOUCHER_WALL_2):
            if distance(actor.pos, pos) > 1:
                return False, "use:voucher_distance"
        # Bomb / DizzyWeapon explicitly have no range restriction
    return True, "ok"


def _check_attack(command: Mapping[str, Any], targets: list[dict], actor: Unit,
                  obs: Observation, world: WorldView,
                  cfg: Config) -> tuple[bool, str]:
    tower = obs_tower_for_command(command, obs)
    if tower is None or not tower.alive:
        return False, "attack:tower_unknown"
    if distance(actor.pos, tower.pos) > 1:
        return False, "attack:controller_too_far"
    reach = tower.effective_range()
    positions = [Pos(t["x"], t["y"]) for t in targets]
    for pos in positions:
        if distance(tower.pos, pos) > reach:
            return False, "attack:out_of_range"
        if tower.kind != R.ROCKET and pos == tower.pos:
            return False, "attack:self_cell"
    # Rocket impacts may legally overlap (任务书 §4.5.4.4: 多枚导弹落点重叠时伤害
    # 叠加), and stacking all missiles on one armoured robot is often the right
    # play.  Every other weapon must aim at distinct cells.
    if tower.kind != R.ROCKET and \
            len({(p.x, p.y) for p in positions}) != len(positions):
        return False, "attack:duplicate_targets"
    if tower.kind == R.GATLING and len(positions) > 1 and not cone_ok(tower.pos, positions):
        return False, "attack:cone_violated"
    return True, "ok"


def cone_ok(origin: Pos, positions: list[Pos]) -> bool:
    """任务书 §4.5.4.4: every pair of aim directions must be <= 90 degrees apart.

    On integer grids "angle <= 90" is exactly "dot product >= 0".  Quadrant or
    45-degree-grid approximations are wrong: from (10,10) aiming at (11,10) and
    (11,13) spans 56.3 degrees but a 45-degree grid would split them.
    """
    vectors = [(p.x - origin.x, p.y - origin.y) for p in positions]
    for i, a in enumerate(vectors):
        for b in vectors[i + 1:]:
            if a[0] * b[0] + a[1] * b[1] < 0:
                return False
    return True


def _check_summon(command: Mapping[str, Any], targets: list[dict], actor: Unit,
                  obs: Observation) -> tuple[bool, str]:
    pos = Pos(targets[0]["x"], targets[0]["y"])
    if distance(actor.pos, pos) > 1:
        return False, "summon:not_adjacent"
    items = command.get("item") or []
    if not items:
        return False, "summon:no_items"
    need: dict[str, int] = {}
    for item in items:
        need[item] = need.get(item, 0) + 1
    for item, count in need.items():
        if actor.count(item) < count:
            return False, "summon:item_missing"
    return True, "ok"


def apply(plan: Mapping[int, dict], obs: Observation, world: WorldView,
          controllers: Mapping[int, int] | None = None,
          cfg: Config = DEFAULT) -> GuardResult:
    """Validate a whole turn plan.

    ``plan`` maps role id -> command.  ``controllers`` maps tower id ->
    controller role id and is used to enforce "one role controls one tower".
    """
    units = _as_units(obs)
    accepted: dict[str, dict] = {}
    rejected: list[tuple[str, str]] = []
    used_controllers: set[int] = set()
    controlled_towers: set[int] = set()

    for role_id in sorted(plan):
        command = plan[role_id]
        actor = units.get(role_id)
        ok, reason = validate(command, actor, obs, world, cfg)
        key = str(role_id)
        if ok and command.get("action") == "attack":
            tower_id = command.get("__towerId")
            if tower_id in controlled_towers:
                ok, reason = False, "attack:tower_taken"
            elif role_id in used_controllers:
                ok, reason = False, "attack:controller_busy"
            else:
                used_controllers.add(role_id)
                controlled_towers.add(tower_id)
                # The response key is the *weapon's* id, not the controller's:
                # ``controllerId`` names the role that operates it (接口文档 §2.2),
                # while the map key addresses the unit being commanded.  The
                # official sample keys the attack as "10020" (the gatling) with
                # ``controllerId: "10010"`` (the worker).
                key = str(tower_id)
        if ok:
            if key in accepted:
                rejected.append((key, "duplicate_key"))
                continue
            accepted[key] = _strip_internal(dict(command))
        else:
            rejected.append((str(role_id), reason))
    return GuardResult(accepted, rejected)
