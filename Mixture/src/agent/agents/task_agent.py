"""Task agent: pioneer commitments plus defence support once tasks are exhausted.

The pioneer's first duty is the self-evolution task (task book §5.3): stay within
one cell of the task point, keep the task channel free, and finish through the
sub-agent loop in ``self_evolve``. When no task is available (all task points on
cooldown or exhausted) and the news channel has no confirmed treasure, the pioneer
switches to support work for the defence agent:

* use a carried ``WallFixer`` on a wall that is about to be breached;
* otherwise buy repair packs / upgrade vouchers with gold the defence does not need;
* otherwise drift back towards the task point so the next refresh is taken fast,
  while keeping out of the robots' 3-cell attack ring.

Robot avoidance only counts robots whose ``targetTeam`` is ours: robots marching on
the enemy base are not a reason to abandon a task point.
"""
from ..model import Pos, distance
from ..navigation import evacuate, night_caution, safe_cell
from ..protocol import move_command
from ..objectives import goal
from . import blackboard, defense_agent, economy_agent

SUPPORT_GOLD_RESERVE = 40      # never spend the defender's own next purchase
SUPPORT_SURPLUS = 100          # only spend clearly surplus gold on upgrades
CRITICAL_RATIO = 0.5           # "about to be breached" wall health share
MIN_TOWERS_BEFORE_SUPPORT = 3  # early gold belongs to the tower/wall build-up


def has_task_work(turn):
    """True while the pioneer should stay on task duty."""
    return bool(turn.phase_task) or any(task.valid for task in turn.tasks)


def nearest_task_point(turn, worker):
    return min((task.position for task in turn.tasks), key=lambda p: distance(worker.pos, p), default=None)


def critical_walls(turn, worker=None):
    """Walls the defence agent considers about to fall, nearest first when a worker is given."""
    rows = [row for row in defense_agent.wall_rows(turn)
            if row['ratio'] <= CRITICAL_RATIO and (row['incoming'] > 0 or row['ratio'] <= 0.35)]
    if worker is not None:
        rows.sort(key=lambda row: (distance(worker.pos, Pos.load(row['pos'])), row['ratio']))
    return rows


def use_wall_fixer(turn, worker, state, reserved, commands):
    """Spend a carried repair pack on the wall that needs it most (adjacent or by walking)."""
    if 'WallFixer' not in worker.backpack:
        return False
    rows = critical_walls(turn, worker)
    if not rows:
        return False
    target = Pos.load(rows[0]['pos'])
    if distance(worker.pos, target) <= 1:
        commands[worker.unit_id] = {'action': 'use', 'name': 'WallFixer', 'targetPos': [target.dump()]}
        goal(state, turn, worker, 'support_repair_wall', target,
             f"围墙{rows[0]['health']}/{rows[0]['max']}血，夜间随时可能被攻破")
        return True
    step, length = economy_agent.route_to(turn, worker, target, reserved)
    if step is not None:
        commands[worker.unit_id] = move_command(step)
        reserved.add(step)
        goal(state, turn, worker, 'support_move_wall', target,
             f"携带修复包前往可能被攻破的围墙（{length}回合）")
        return True
    return False


def voucher_target(turn, item):
    """Where a carried voucher must be used, or ``None`` when it has no valid target.

    Mirrors the legality rules in ``guard``: station -> station, weapon -> a tower of
    the matching level, wall -> a wall of the matching level.
    """
    station = turn.station()
    if item.startswith('Station'):
        return station.pos if station and station.level < 3 and item == f'StationUpgradeVoucher{station.level}' else None
    if item.startswith('Weapon'):
        tower = next((t for t in turn.weapons()
                      if t.level < 3 and item == f'WeaponUpgradeVoucher{t.level}'), None)
        return tower.pos if tower else None
    if item.startswith('Wall'):
        row = next((r for r in defense_agent.wall_rows(turn)
                    if r['level'] < 3 and item == f"WallUpgradeVoucher{r['level']}"), None)
        return Pos.load(row['pos']) if row else None
    return None


def use_held(turn, worker, state, reserved, commands):
    """Deliver a carried upgrade voucher: walk to the building, then use it.

    A voucher bought by the pioneer is invisible to the defender's own emergency
    logic (``defense.emergency_upgrade`` only checks the defender's backpack) and
    blocks the defender's purchase plan ("one already in transit"), so delivering it
    is mandatory — otherwise the gold is simply lost.
    """
    for item in worker.backpack:
        if 'Voucher' not in item:
            continue
        target = voucher_target(turn, item)
        if target is None:
            continue
        if distance(worker.pos, target) <= 1:
            commands[worker.unit_id] = {'action': 'use', 'name': item, 'targetPos': [target.dump()]}
            goal(state, turn, worker, 'support_use_voucher', target, f'把{item}用在目标建筑上')
            return True
        if not turn.is_day and not safe_cell(turn, target):
            # Do not walk into the robots' zone at night just to deliver a voucher.
            continue
        step, length = economy_agent.route_to(turn, worker, target, reserved)
        if step is not None:
            commands[worker.unit_id] = move_command(step)
            reserved.add(step)
            goal(state, turn, worker, 'support_deliver_voucher', target,
                 f'把{item}送到目标建筑并使用（{length}回合）')
            return True
    return False


def buy_for_defense(turn, worker, state, reserved, commands):
    """Spend *surplus* gold on what the defence plan is missing and can be delivered."""
    shop = next((pos for pos, kind in turn.zones.items() if kind == 'weaponShop'), None)
    if shop is None or len(turn.weapons()) < MIN_TOWERS_BEFORE_SUPPORT or not turn.is_day:
        return False
    free = (worker.capacity or 100) - len(worker.backpack)
    if free <= 0:
        return False
    prices = defense_agent.shop_prices(turn)
    held = {item for unit in turn.controllable() for item in unit.backpack}
    reserve = economy_agent.funding_need(turn, state) + SUPPORT_GOLD_RESERVE
    rows = [row for row in defense_agent.wall_rows(turn) if row['ratio'] < defense_agent.DAMAGED_RATIO]
    want = None
    if rows and 'WallFixer' not in held:
        want = ('WallFixer', 1, '围墙受损且队内没有修复包')
    else:
        # Upgrades the pioneer can actually deliver; a station voucher stays with the
        # defender because only its own emergency check looks at its backpack.
        for tower in turn.weapons():
            name = f'WeaponUpgradeVoucher{tower.level}'
            if tower.level < 3 and name not in held:
                count = min(free, sum(1 for t in turn.weapons() if t.level == tower.level))
                want = (name, max(1, count), '协助购买武器升级券并送达')
                break
        if want is None:
            for row in [r for r in defense_agent.wall_rows(turn) if r['level'] < 3]:
                name = f"WallUpgradeVoucher{row['level']}"
                if name in held:
                    continue
                want = (name, 1, '协助购买围墙升级券并送达')
                break
    if want is None:
        return False
    name, count, reason = want
    price = prices.get(name)
    # Only clearly surplus gold is spent: the defender keeps its own shopping power.
    if type(price) is not int or turn.gold < price * count + reserve + SUPPORT_SURPLUS:
        return False
    if distance(worker.pos, shop) <= 1:
        commands[worker.unit_id] = {'action': 'buy', 'name': name, 'num': count}
        goal(state, turn, worker, 'support_buy', shop, f'{reason}（留{reserve}金给防御）')
        return True
    step, length = economy_agent.route_to(turn, worker, shop, reserved)
    if step is not None:
        commands[worker.unit_id] = move_command(step)
        reserved.add(step)
        goal(state, turn, worker, 'support_travel_shop', shop, f'{reason}，前往武器商店（{length}回合）')
        return True
    return False


def standby(turn, worker, state, reserved, commands):
    """No task, nothing to buy: stay useful and alive until the next refresh."""
    if not turn.is_day:
        # Robots are out; the pioneer has no weapon duty, so leave their attack ring.
        if not safe_cell(turn, worker.pos) and evacuate(turn, worker, reserved, commands):
            goal(state, turn, worker, 'support_evade', worker.pos, '夜间无任务，离开机器人威胁范围')
            return True
        goal(state, turn, worker, 'support_wait', worker.pos, '夜间无任务，保持后排待命')
        return False
    point = nearest_task_point(turn, worker)
    if point is not None and distance(worker.pos, point) > 1:
        step, length = economy_agent.route_to(turn, worker, point, reserved)
        if step is not None:
            commands[worker.unit_id] = move_command(step)
            reserved.add(step)
            goal(state, turn, worker, 'support_return_task', point,
                 f'任务点等待刷新，提前回到任务点（{length}回合）')
            return True
    goal(state, turn, worker, 'support_idle', point or worker.pos, '暂无任务，保持任务点附近待命')
    return False


def support(turn, worker, state, reserved, commands):
    """Post-task duty ladder. Returns True when an action was issued.

    Order matters: repair the wall that is about to fall, deliver whatever we already
    carry, and only then spend gold — so the pioneer never strands a purchase.
    """
    for step in (use_wall_fixer, use_held, buy_for_defense, standby):
        if step(turn, worker, state, reserved, commands):
            return True
    return False


def publish(state, mode, reason, action=None, round_no=None):
    """Blackboard entry so the (future) global observer can see pioneer intent."""
    payload = {'mode': mode, 'reason': reason, 'action': action}
    state['task_plan'] = payload
    blackboard.write(state, 'task', payload, round_no)
    return payload
