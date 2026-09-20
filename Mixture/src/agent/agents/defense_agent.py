"""Defense agent: night-fire assessment, next-day work plan and repair routing.

The agent answers the questions the strategy request asks for, every round:

* how our firepower compares with the robot wave (damage per round, breach ETA);
* what is damaged (station / towers / walls) and how urgent each wall is;
* what the economy can afford right now;
* what the defender should do next, in priority order, with a round estimate;
* in which order to visit repair sites so the trip does not zig-zag.

Everything is derived from the observation plus session state; no platform call,
no model call, no mutation outside the passed ``state``.
"""
from dataclasses import replace
from math import ceil

from ..model import Pos, distance, station_footprint
from ..navigation import route
from ..policies.construction import facing, operator_hub, priority_wall_sites, wall_sites
from ..rules import SHOP_PRICES, TOWER_TYPES
from ..world import _neighbours
from . import attack_agent, blackboard

# Stone is only useful up to the number of walls we still intend to build; more
# than a handful blocks service trips, so the reserve is deliberately small.
MAX_STONE_RESERVE = 5
DAMAGED_RATIO = 0.8
CRITICAL_RATIO = 0.5


def _max_health(unit):
    """Documented maxima (§4.5.1): station 1500/level, tower and wall 1000+500*(level-1)."""
    if unit.kind == 'station':
        return 1500 * max(1, unit.level)
    return 1000 + 500 * (max(1, unit.level) - 1)


def maintenance_sites(turn):
    """Priority wall cells early, full ring later; mirrors the construction plan."""
    return priority_wall_sites(turn) if turn.round_no <= 130 else wall_sites(turn)


def shop_prices(turn):
    """Shop catalogue with the observation's live prices overriding the documented ones."""
    prices = dict(SHOP_PRICES)
    for item in turn.raw.get('weaponShopList', []) or []:
        if isinstance(item, dict) and 'name' in item and 'price' in item:
            prices[item['name']] = item['price']
    return prices


def vendor_prices(turn):
    return {i['name']: i['price'] for i in turn.raw.get('vendorShopList', []) or []
            if isinstance(i, dict) and 'name' in i and 'price' in i}


def wall_pressure(turn, wall):
    """Incoming damage per round against one wall (robots hit what blocks them)."""
    return sum(attack_agent.robot_damage(r) for r in attack_agent.threats(turn)
               if distance(r.pos, wall.pos) <= attack_agent.ROBOT_RANGE)


def wall_rows(turn):
    """Damage table for every standing wall, with breach estimate."""
    primary = set(priority_wall_sites(turn))
    rows = []
    for wall in turn.walls():
        maximum = _max_health(wall)
        pressure = wall_pressure(turn, wall)
        rows.append({'id': wall.unit_id, 'pos': wall.pos.dump(), 'health': wall.health, 'max': maximum,
                     'level': max(1, wall.level), 'ratio': round(wall.health / maximum, 3),
                     'primary': wall.pos in primary, 'incoming': pressure,
                     'breach_rounds': ceil(wall.health / pressure) if pressure else None})
    rows.sort(key=lambda row: (row['breach_rounds'] is None, row['breach_rounds'] or 0, row['ratio']))
    return rows


def assess(turn, state=None, round_no=None):
    """Structured situation report; stored in state and published on the blackboard."""
    station = turn.station()
    footprint = station_footprint(station.pos) if station else ()
    towers = list(turn.weapons())
    threats = list(attack_agent.threats(turn))
    incoming = [r for r in threats if footprint and min(distance(r.pos, c) for c in footprint) <= attack_agent.ROBOT_RANGE]
    walls = wall_rows(turn)
    damaged = [row for row in walls if row['ratio'] < DAMAGED_RATIO]
    ready_damage = sum(attack_agent.weapon_damage(t) for t in towers if not t.cooldown)
    robot_hp = sum(r.health for r in threats)
    report = {
        'round': turn.round_no,
        'day': turn.is_day,
        'firepower': {
            'towers': [{'id': t.unit_id, 'kind': t.kind, 'level': t.level, 'health': t.health,
                        'range': t.range_of_attack(), 'cooldown': t.cooldown,
                        'damage': attack_agent.weapon_damage(t)} for t in towers],
            'round_damage': ready_damage,
            'robots': {'total': len(threats), 'hp': robot_hp,
                       'in_base_range': len(incoming),
                       'incoming_damage': sum(attack_agent.robot_damage(r) for r in incoming),
                       'by_kind': {kind: sum(1 for r in threats if r.kind == kind)
                                   for kind in sorted({r.kind for r in threats})}},
            'clear_rounds': ceil(robot_hp / ready_damage) if ready_damage else None,
        },
        'damage': {
            'station': ({'health': station.health, 'max': _max_health(station), 'level': station.level}
                        if station else None),
            'towers_damaged': [t.unit_id for t in towers if t.health < _max_health(t)],
            'walls': walls,
            'walls_damaged': [row['pos'] for row in damaged],
            'next_breach': next((row for row in walls if row['breach_rounds']), None),
        },
        'economy': {
            'gold': turn.gold,
            'prices': vendor_prices(turn),
            'fixers': sum(u.backpack.count('WallFixer') for u in turn.controllable()),
            'vouchers': {u.unit_id: [i for i in u.backpack if 'Voucher' in i] for u in turn.controllable()
                         if any('Voucher' in i for i in u.backpack)},
            'stones': sum(u.backpack.count('stone') for u in turn.controllable()),
        },
    }
    report['summary'] = (
        f"第{turn.round_no}回合{'白天' if turn.is_day else '夜间'}："
        f"我方{turn.gold}金/{len(towers)}塔每回合{ready_damage}伤害，"
        f"机器人{len(threats)}台({robot_hp}血)，基地3格内{len(incoming)}台每回合{report['firepower']['robots']['incoming_damage']}伤害；"
        f"围墙受损{len(damaged)}段" + (f"，最快{report['damage']['next_breach']['breach_rounds']}回合被攻破"
                                       if report['damage']['next_breach'] else ''))
    if state is not None:
        state['defense_assessment'] = report
        blackboard.write(state, 'defense', report, round_no)
    return report


def stone_reserve(turn, state=None):
    """Wanted stone stock: only the missing priority walls, capped for mobility."""
    missing = [p for p in maintenance_sites(turn) if p not in turn.occupied_cells()]
    return min(len(missing), MAX_STONE_RESERVE)


def stone_needed(turn, worker, state=None):
    have = worker.backpack.count('stone')
    return max(0, stone_reserve(turn, state) - have)


def repair_order(turn, worker, sites, reserved=(), limit=8):
    """Nearest-neighbour route over repair sites using real path lengths.

    Returns ``(order, total_length)`` where order is a list of
    ``{'site': {'x':..,'y':..}, 'length': n, 'cumulative': n}``. Unreachable sites
    are dropped instead of being silently kept in the plan. At most ``limit`` sites
    are ordered — each step costs one BFS, and the far ones are replanned later.
    """
    remaining = sorted(sites, key=lambda p: (distance(worker.pos, p), p.x, p.y))[:limit]
    here = worker
    blocked = set(reserved)
    order, total = [], 0
    while remaining:
        best = None
        for site in remaining:
            stands = [p for p in _neighbours(site) if turn.land(p)]
            # One multi-goal search instead of one search per standing cell.
            length = route(turn, here, stands, blocked, cautious=False)[1]
            if length >= 10**6:
                continue
            key = (length, site.x, site.y)
            if best is None or key < best[0]:
                best = (key, site, length)
        if best is None:
            break
        _, site, length = best
        total += length
        order.append({'site': site.dump(), 'length': length, 'cumulative': total})
        remaining.remove(site)
        stands = [p for p in _neighbours(site) if turn.land(p) and p not in blocked]
        step_choice = min(((route(turn, here, [p], blocked, cautious=False)[1], p) for p in stands),
                          key=lambda item: (item[0], item[1].x, item[1].y), default=None)
        if step_choice and step_choice[0] < 10**6:
            here = replace(here, pos=step_choice[1])
            blocked.add(step_choice[1])
    return order, total


def next_site(turn, worker, sites, reserved=()):
    """First stop of the repair route (avoids walking past a nearer damaged wall)."""
    order, _ = repair_order(turn, worker, sites, reserved)
    return Pos.load(order[0]['site']) if order else None


def _voucher_jobs(turn, worker, report):
    """Carried vouchers turned into 'walk there and use it' jobs."""
    jobs = []
    station = turn.station()
    for item in worker.backpack:
        if 'Voucher' not in item:
            continue
        if item.startswith('Station') and station and station.level < 3:
            target, unit = station.pos, station.unit_id
        elif item.startswith('Weapon'):
            tower = next((t for t in turn.weapons()
                          if t.level < 3 and item == f'WeaponUpgradeVoucher{t.level}'), None)
            if tower is None:
                continue
            target, unit = tower.pos, tower.unit_id
        elif item.startswith('Wall'):
            # Wall vouchers are tier-specific: Voucher1 upgrades level1 walls, Voucher2 level2.
            row = next((r for r in report['damage']['walls']
                        if r['level'] < 3 and item == f"WallUpgradeVoucher{r['level']}"), None)
            if row is None:
                continue
            target, unit = Pos.load(row['pos']), row['id']
        else:
            continue
        jobs.append({'kind': 'use', 'name': item, 'target': target.dump(), 'unit': unit,
                     'priority': 60, 'reason': f'{item} 已在背包，走到目标旁使用'})
    return jobs


def work_plan(turn, state, worker, report=None):
    """Ordered next-day plan. Each job carries an ETA in rounds and a reason."""
    report = report or assess(turn, state)
    price = shop_prices(turn)
    jobs = []
    station = turn.station()
    hub = operator_hub(turn)
    walls = report['damage']['walls']
    damaged = [row for row in walls if row['ratio'] < DAMAGED_RATIO]
    fixers = worker.backpack.count('WallFixer')
    missing = [p for p in maintenance_sites(turn) if p not in turn.occupied_cells()]
    stones = worker.backpack.count('stone')

    def walk_eta(target):
        """Rounds to reach a cell next to ``target`` (None when unreachable)."""
        stands = [p for p in _neighbours(target) if turn.land(p)]
        length = route(turn, worker, stands, set(), cautious=False)[1]
        return None if length >= 10**6 else length + 1

    def job(kind, priority, reason, target=None, name=None, unit=None, eta=None):
        return {'kind': kind, 'priority': priority, 'reason': reason, 'target': target,
                'name': name, 'unit': unit, 'eta': eta}

    # 1. Base about to fall: the emergency voucher outranks every chore.
    if station and station.level < 3 and f'StationUpgradeVoucher{station.level}' in worker.backpack:
        # Two rounds of the damage that is actually inside the base's 3-cell ring.
        incoming = report['firepower']['robots']['incoming_damage'] * 2
        if station.health < 100 or incoming >= station.health:
            jobs.append(job('use', 100, '基地低血或两回合内致命，优先回血',
                            target=station.pos.dump(), name=f'StationUpgradeVoucher{station.level}',
                            unit=station.unit_id))
    # 2. Repair the wall that will actually break first.
    critical = [row for row in damaged if row['ratio'] <= CRITICAL_RATIO and row['incoming'] > 0]
    if fixers:
        for row in (critical or damaged):
            jobs.append(job('repair_wall', 90 if row in critical else 80,
                            f"围墙{row['health']}/{row['max']}血，"
                            + (f"{row['breach_rounds']}回合内可能被攻破" if row['breach_rounds'] else '夜间受损'),
                            target=row['pos'], name='WallFixer', unit=row['id']))
            break
    elif damaged and turn.gold >= price.get('WallFixer', 10):
        jobs.append(job('buy', 85, f"{len(damaged)}段围墙受损且无修复包", name='WallFixer'))
    # 3. Missing priority walls need stone before dusk.
    if missing:
        jobs.append(job('build_wall', 50, f"优先墙还缺{len(missing)}段，石头{stones}",
                        target=missing[0].dump(), name='wall'))
    # 4. Carried upgrades, then procurement.
    jobs.extend(_voucher_jobs(turn, worker, report))
    if missing and stones < stone_reserve(turn, state):
        jobs.append(job('restock_stone', 30,
                        f"石头{stones}不足所需{stone_reserve(turn, state)}"))
    # 5. Night readiness always has a veto: it is inserted as a hard deadline job.
    if hub:
        left = 70 - (turn.round_no - 1) % 130
        step_length = route(turn, worker, [hub], set(), cautious=False)[1]
        if step_length < 10**6:
            jobs.append(job('defend_ready', 95 if left <= step_length + 5 else 45,
                            f'夜前回操作位需{step_length}回合，白天还剩{left}回合',
                            target=hub.dump()))
    jobs.sort(key=lambda entry: (-entry['priority'], str(entry.get('target'))))
    # ETAs cost one path search each, so only the jobs we may actually execute get one.
    for entry in jobs[:5]:
        if entry.get('target') and entry.get('eta') is None:
            entry['eta'] = walk_eta(Pos.load(entry['target']))
    plan = {'round': turn.round_no, 'day': turn.is_day, 'stone_reserve': stone_reserve(turn, state),
            'damaged': len(damaged), 'missing_walls': len(missing), 'jobs': jobs[:8],
            'headline': jobs[0]['reason'] if jobs else '没有待办防御工作'}
    state['defense_plan'] = plan
    blackboard.write(state, 'defense_plan', plan)
    return plan


def log_reason(state, plan, previous=None):
    """Task-switch explanation used by the executing policy (kept human readable)."""
    previous = previous if previous is not None else state.get('defense_active_job')
    head = plan['jobs'][0] if plan['jobs'] else {'kind': 'idle', 'reason': plan['headline']}
    switched = previous and (previous.get('kind') != head.get('kind') or previous.get('target') != head.get('target'))
    state['defense_active_job'] = {'kind': head.get('kind'), 'target': head.get('target')}
    return {'switched': bool(switched), 'from': previous, 'to': state['defense_active_job'],
            'reason': head.get('reason', '')}
