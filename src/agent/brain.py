from itertools import permutations
from typing import Any

from .grid import next_step
from .protocol import (
    PIONEER,
    Pos,
    Turn,
    Unit,
    WALL,
    WALL_MATERIAL,
    WEAPON_BUILD_COST,
    accept_task_command,
    attack_command,
    buy_command,
    build_command,
    collect_command,
    distance,
    move_command,
    sell_command,
    station_footprint,
    submit_answer_command,
    use_command,
)

TOWER_LOADOUT = ("rocket", "gatling", "railgun")
STONE_BATCH = 5
SELL_BATCH = 8
RETURN_BUFFER = 2
MINERAL_TYPES = ("copper", "iron", "stone")
UPGRADE_VOUCHERS = (("WeaponUpgradeVoucher1", 1, 100), ("WeaponUpgradeVoucher2", 2, 150))
_NEIGHBOUR_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """Produce a complete, schema-valid response for one simultaneous turn."""
    turn = Turn.load(payload)
    commands: dict[int, dict[str, Any]] = {}
    prompt = ""
    if turn.is_day:
        prompt = _day(turn, commands)
    else:
        _night(turn, commands)
    return {
        "roleCommandMap": {str(key): value for key, value in commands.items()},
        "prompt": prompt,
        "executeCmd": "",
    }


def _day(turn: Turn, commands: dict[int, dict[str, Any]]) -> str:
    claimed: set[Pos] = set()
    prompt = _pioneer_day(turn, commands, claimed)
    sites = _tower_sites(turn)
    standing_towers = {unit.pos for unit in turn.weapons()}
    standing_walls = {unit.pos for unit in turn.walls()}
    occupied = turn.occupied_cells()
    free_towers = [pos for pos in sites if pos not in standing_towers and pos not in occupied]
    free_walls = [pos for pos in _wall_order(turn) if pos not in standing_walls and pos not in occupied]
    for worker in turn.workers():
        if _return_to_defense(turn, worker, claimed, commands):
            continue
        if _upgrade_weapon(turn, worker, claimed, commands):
            continue
        _worker_day(turn, worker, sites, free_towers, free_walls, claimed, commands)
    return prompt


def _pioneer_day(turn: Turn, commands: dict[int, dict[str, Any]], claimed: set[Pos]) -> str:
    pioneers = tuple(unit for unit in turn.controllable() if unit.kind == PIONEER)
    if not pioneers:
        return ""
    pioneer = pioneers[0]
    if turn.phase_task:
        if turn.llm_response.strip():
            commands[pioneer.unit_id] = submit_answer_command(turn.llm_response.strip())
            return ""
        return (
            "你正在完成游戏中的自进化任务。只输出最终 taskAnswer，不要解释、"
            "不要 Markdown、不要代码块。若任务要求执行命令，请根据题目和已有信息"
            "推导出可直接提交的完整答案。\n\n任务：\n" + turn.phase_task
        )
    tasks = [task for task in turn.tasks if task.valid]
    if not tasks:
        return ""
    task = max(
        tasks,
        key=lambda item: (
            (item.score_reward + item.gold_reward) / max(distance(pioneer.pos, item.position), 1),
            item.timeout_rounds,
            -item.cooldown_rounds,
        ),
    )
    if distance(pioneer.pos, task.position) <= 1:
        commands[pioneer.unit_id] = accept_task_command()
        return ""
    step = _step_toward(turn, pioneer, task.position, claimed)
    if step is not None:
        commands[pioneer.unit_id] = move_command(step)
    return ""


def _worker_day(
    turn: Turn,
    worker: Unit,
    sites: tuple[Pos, ...],
    free_towers: list[Pos],
    free_walls: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> None:
    if free_towers and turn.gold >= WEAPON_BUILD_COST:
        for index, site in enumerate(sites):
            if site in free_towers and site not in claimed:
                _build_or_walk(turn, worker, site, TOWER_LOADOUT[index], claimed, commands)
                return
    if _sell_if_ready(turn, worker, claimed, commands):
        return
    stones = worker.backpack.count(WALL_MATERIAL)
    if free_walls and stones:
        for site in free_walls:
            if site not in claimed:
                _build_or_walk(turn, worker, site, WALL, claimed, commands)
                return
    _mine(turn, worker, claimed, commands, stone_only=bool(free_walls and stones < STONE_BATCH))


def _sell_if_ready(turn: Turn, worker: Unit, claimed: set[Pos], commands: dict[int, dict[str, Any]]) -> bool:
    vendor = turn.vendor()
    minerals = [(name, worker.backpack.count(name)) for name in MINERAL_TYPES]
    name, count = max(
        minerals,
        key=lambda item: (turn.vendor_prices.get(item[0], 1) * item[1], item[1]),
    )
    if vendor is None or not count:
        return False
    if distance(worker.pos, vendor) <= 1:
        commands[worker.unit_id] = sell_command(name, count)
        return True
    if count < SELL_BATCH and not worker.backpack_full:
        return False
    step = _step_toward(turn, worker, vendor, claimed)
    if step is not None:
        commands[worker.unit_id] = move_command(step)
        return True
    return False


def _mine(turn: Turn, worker: Unit, claimed: set[Pos], commands: dict[int, dict[str, Any]], *, stone_only: bool) -> bool:
    if worker.backpack_full:
        return False
    mines = turn.stone_mines() if stone_only else turn.mines()
    for mine in sorted(
        mines,
        key=lambda pos: (
            -turn.vendor_prices.get(turn.zones.get(pos, ""), 1),
            distance(worker.pos, pos),
            pos.x,
            pos.y,
        ),
    ):
        if distance(worker.pos, mine) <= 1:
            commands[worker.unit_id] = collect_command(mine)
            return True
        step = _step_toward(turn, worker, mine, claimed)
        if step is not None:
            commands[worker.unit_id] = move_command(step)
            return True
    return False


def _night(turn: Turn, commands: dict[int, dict[str, Any]]) -> None:
    claimed: set[Pos] = set()
    for role, tower in _tower_pairs(turn):
        if distance(role.pos, tower.pos) <= 1:
            if tower.cooldown == 0:
                targets = _attack_targets(turn, tower)
                if targets:
                    commands[tower.unit_id] = attack_command(role.unit_id, targets)
            continue
        step = _step_toward(turn, role, tower.pos, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)


def _tower_pairs(turn: Turn) -> tuple[tuple[Unit, Unit], ...]:
    roles = turn.controllable()
    towers = turn.weapons()
    count = min(len(roles), len(towers))
    if not count:
        return ()
    best = min(
        permutations(roles, count),
        key=lambda assigned: sum(distance(role.pos, tower.pos) for role, tower in zip(assigned, towers)),
    )
    return tuple(zip(best, towers))


def _attack_targets(turn: Turn, tower: Unit) -> list[Pos]:
    reachable = [
        robot for robot in turn.robots
        if robot.health > 0
        and robot.target_team == turn.team_type
        and distance(tower.pos, robot.pos) <= tower.range_of_attack()
    ]
    if not reachable:
        return []
    station = turn.station()
    def threat(robot: Any) -> tuple[int, int, int, int]:
        station_distance = distance(robot.pos, station.pos) if station else 99
        killable = robot.health <= _tower_damage(tower)
        return (not killable, robot.abnormal_state == "dizzy", station_distance, robot.robot_id)
    if tower.kind == "rocket":
        return _rocket_targets(turn, tower, reachable)
    ordered = sorted(reachable, key=threat)
    if tower.kind == "railgun":
        return [ordered[0].pos]
    # Gatling supports one target per level.  Keeping every shot in one quadrant
    # guarantees the required <=90 degree firing cone.
    leader = ordered[0]
    dx = 0 if leader.pos.x == tower.pos.x else (1 if leader.pos.x > tower.pos.x else -1)
    dy = 0 if leader.pos.y == tower.pos.y else (1 if leader.pos.y > tower.pos.y else -1)
    same_cone = [robot for robot in ordered if (robot.pos.x - tower.pos.x) * dx >= 0 and (robot.pos.y - tower.pos.y) * dy >= 0]
    required = max(tower.level, 1)
    targets = [robot.pos for robot in same_cone[:required]]
    return targets + [leader.pos] * (required - len(targets))


def _rocket_targets(turn: Turn, tower: Unit, robots: list[Any]) -> list[Pos]:
    station = turn.station()
    def value(candidate: Any) -> tuple[int, int, int, int]:
        splash_damage = sum(20 if robot.pos == candidate.pos else 10 for robot in robots if distance(candidate.pos, robot.pos) <= 1)
        station_distance = distance(candidate.pos, station.pos) if station else 99
        kill_score = sum(1 for robot in robots if distance(candidate.pos, robot.pos) <= 1 and robot.health <= (20 if robot.pos == candidate.pos else 10))
        return (kill_score, splash_damage, -station_distance, -candidate.robot_id)
    ordered = sorted(robots, key=value, reverse=True)
    shots = max(tower.level, 1)
    return [robot.pos for robot in ordered[:shots]] + [ordered[0].pos] * max(0, shots - len(ordered))


def _tower_damage(tower: Unit) -> int:
    if tower.kind == "gatling":
        return 10
    if tower.kind == "railgun":
        return 10 * max(tower.level, 1)
    return 20


def _return_to_defense(turn: Turn, worker: Unit, claimed: set[Pos], commands: dict[int, dict[str, Any]]) -> bool:
    remaining = 70 - ((turn.round_no - 1) % 130)
    if remaining <= 0 or turn.phase_task:
        return False
    pairs = _tower_pairs(turn)
    target = next((tower for role, tower in pairs if role.unit_id == worker.unit_id), None)
    if target is None or distance(worker.pos, target.pos) + RETURN_BUFFER < remaining:
        return False
    step = _step_toward(turn, worker, target.pos, claimed)
    if step is not None:
        commands[worker.unit_id] = move_command(step)
    return True


def _upgrade_weapon(turn: Turn, worker: Unit, claimed: set[Pos], commands: dict[int, dict[str, Any]]) -> bool:
    towers = turn.weapons()
    if len(towers) < 3:
        return False
    target = next((tower for tower in towers if tower.kind == "rocket"), towers[0])
    for voucher, required_level, _ in UPGRADE_VOUCHERS:
        if voucher in worker.backpack and target.level == required_level:
            if distance(worker.pos, target.pos) <= 1:
                commands[worker.unit_id] = use_command(voucher, target.pos)
                return True
            step = _step_toward(turn, worker, target.pos, claimed)
            if step is not None:
                commands[worker.unit_id] = move_command(step)
            return True
    shop = turn.weapon_shop()
    if shop is None:
        return False
    for voucher, required_level, fallback_price in UPGRADE_VOUCHERS:
        price = turn.weapon_prices.get(voucher, fallback_price)
        if target.level == required_level and turn.gold >= price and voucher in turn.weapon_prices:
            if distance(worker.pos, shop) <= 1:
                commands[worker.unit_id] = buy_command(voucher)
                return True
            step = _step_toward(turn, worker, shop, claimed)
            if step is not None:
                commands[worker.unit_id] = move_command(step)
            return True
    return False


def _build_or_walk(turn: Turn, worker: Unit, target: Pos, name: str, claimed: set[Pos], commands: dict[int, dict[str, Any]]) -> None:
    if 0 < distance(worker.pos, target) <= 1:
        commands[worker.unit_id] = build_command(target, name)
        claimed.add(target)
        return
    step = _step_toward(turn, worker, target, claimed)
    if step is not None:
        commands[worker.unit_id] = move_command(step)


def _step_toward(turn: Turn, role: Unit, target: Pos, claimed: set[Pos]) -> Pos | None:
    for stand in _stand_cells(turn, role, target, claimed):
        if stand == role.pos:
            return None
        step = next_step(turn, role, stand)
        if step is not None and step not in claimed:
            claimed.add(step)
            return step
    return None


def _stand_cells(turn: Turn, role: Unit, target: Pos, claimed: set[Pos]) -> list[Pos]:
    blocked = turn.blocked(role)
    cells = [pos for pos in _neighbours(target) if turn.land(pos) and pos not in blocked and (pos == role.pos or pos not in claimed)]
    cells.sort(key=lambda pos: (distance(role.pos, pos), pos.x, pos.y))
    return cells


def _tower_sites(turn: Turn) -> tuple[Pos, ...]:
    station = turn.station()
    if station is None:
        return ()
    footprint = station_footprint(station.pos)
    cells = [pos for pos in _cells_at_distance(station.pos, 1) if turn.land(pos)]
    cells.sort(key=lambda pos: (_footprint_distance(pos, footprint), pos.x, pos.y))
    return tuple(cells[:3])


def _wall_order(turn: Turn) -> tuple[Pos, ...]:
    station = turn.station()
    if station is None:
        return ()
    footprint = station_footprint(station.pos)
    xs, ys = [pos.x for pos in footprint], [pos.y for pos in footprint]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    cells = [
        *(Pos(x, ymin - 2) for x in range(xmax + 2, xmin - 3, -1)),
        *(Pos(xmin - 2, y) for y in range(ymin - 1, ymax + 2)),
        *(Pos(x, ymax + 2) for x in range(xmin - 2, xmax + 3)),
        *(Pos(xmax + 2, y) for y in range(ymax + 1, ymin - 2, -1)),
    ]
    entrance = Pos(xmax + 2, ymin - 1)
    return tuple(pos for pos in cells if pos != entrance and turn.land(pos))


def _cells_at_distance(station_pos: Pos, radius: int) -> tuple[Pos, ...]:
    footprint = station_footprint(station_pos)
    cells = []
    for x in range(min(pos.x for pos in footprint) - radius, max(pos.x for pos in footprint) + radius + 1):
        for y in range(min(pos.y for pos in footprint) - radius, max(pos.y for pos in footprint) + radius + 1):
            pos = Pos(x, y)
            if pos not in footprint and _footprint_distance(pos, footprint) == radius:
                cells.append(pos)
    return tuple(cells)


def _footprint_distance(pos: Pos, footprint: tuple[Pos, ...]) -> int:
    return min(distance(pos, cell) for cell in footprint) if footprint else 0


def _neighbours(pos: Pos) -> tuple[Pos, ...]:
    return tuple(Pos(pos.x + dx, pos.y + dy) for dx, dy in _NEIGHBOUR_STEPS)
