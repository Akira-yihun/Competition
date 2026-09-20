"""Economy agent: mine survey, cost-performance, selling timing and the miner plan.

The agent keeps a per-round picture of every visible mine (ore, price, distance to
the worker, to the base and to the vendor, batch size and value), a short price
history, and one plan for the gathering worker: collect at X, sell at the vendor,
or wait. Selling is timed against two opposing forces:

* news/LLM forecasts say a price will rise, so holding pays;
* the defence plan needs gold soon (vouchers, repair packs), so holding is harmful.

Robot avoidance is expressed as extra blocked cells around robots that target our
base: the first route attempt avoids their 3-cell attack ring, and a second, plain
route is used when avoidance would make the target unreachable (never freeze).
"""
from ..model import Pos, distance
from ..navigation import route, night_caution, safe_cell
from ..world import _neighbours
from ..intelligence.news import should_hold
from . import attack_agent, blackboard, defense_agent

FUNDING_HORIZON = 6        # rounds of defence spending the miner must keep liquid
PRE_NIGHT_WINDOW = 8       # sell before dusk instead of hauling ore through the night
ROBOT_MARGIN = 1           # keep one cell outside the documented 3-cell attack range
PRICE_HISTORY_LIMIT = 400  # one sample per ore per round, bounded
MEASURED_MINES = 6         # mines whose distance is measured with a real path search


def ore_prices(turn):
    """Vendor buy prices from the observation; unknown ores fall back to the base value."""
    prices = {i['name']: i['price'] for i in turn.raw.get('vendorShopList', []) or []
              if isinstance(i, dict) and 'name' in i and 'price' in i}
    return {'stone': prices.get('stone', 1), 'iron': prices.get('iron', 2), 'copper': prices.get('copper', 3)}


def cargo(worker, keep_stone=0):
    """Sellable ore stacks, optionally reserving stone for wall building."""
    rows = []
    for name in ('copper', 'iron', 'stone'):
        count = worker.backpack.count(name)
        if name == 'stone':
            count = max(0, count - keep_stone)
        if count > 0:
            rows.append((name, count))
    return rows


def cargo_value(turn, worker, keep_stone=0):
    prices = ore_prices(turn)
    rows = cargo(worker, keep_stone)
    return sum(prices[name] * count for name, count in rows), rows


def record_prices(turn, state):
    """Keep a bounded per-round price series for trend explanations."""
    history = state.setdefault('price_history', [])
    sample = {'round': turn.round_no, **ore_prices(turn)}
    if history and history[-1].get('round') == turn.round_no:
        history[-1] = sample
    else:
        history.append(sample)
    del history[:-PRICE_HISTORY_LIMIT]
    return history[-1]


def danger_cells(turn, margin=ROBOT_MARGIN):
    """Cells inside (attack range + margin) of robots that may attack our base."""
    reach = attack_agent.ROBOT_RANGE + margin
    return {Pos(x, y) for r in attack_agent.threats(turn)
            for x in range(max(0, r.pos.x - reach), min(turn.width, r.pos.x + reach + 1))
            for y in range(max(0, r.pos.y - reach), min(turn.height, r.pos.y + reach + 1))}


def route_to(turn, worker, target, reserved=(), cautious=True):
    """Robot-aware routing with a plain-route fallback so the worker never stalls."""
    goals = _neighbours(target)
    danger = danger_cells(turn)
    if danger:
        step, length = route(turn, worker, goals, set(reserved) | danger, cautious=cautious)
        if step is not None or length < 10**6:
            return step, length
    return route(turn, worker, goals, reserved, cautious=cautious)


def survey(turn, state, worker=None):
    """Per-mine cost-performance rows plus the current recommendation.

    Path length is the expensive part, so it is measured with BFS only for the mines
    that are close by (Chebyshev) and estimated for the rest; the mining policy still
    uses exact routing for whatever it actually picks.
    """
    prices = ore_prices(turn)
    base = turn.station()
    vendor = turn.vendor()
    worker = worker or (turn.workers()[0] if turn.workers() else None)
    rows = []
    mines = sorted(turn.mines(), key=lambda mine: (distance(worker.pos, mine) if worker else 0, mine.x, mine.y))
    for rank, mine in enumerate(mines):
        ore = turn.zones.get(mine, '')
        count = (worker.capacity or 100) - len(worker.backpack) if worker else 10
        batch = max(1, min(10, count))
        if worker is None:
            length, estimated = 10**6, True
        elif rank < MEASURED_MINES:
            length = route(turn, worker, [p for p in _neighbours(mine) if turn.land(p)], set())[1]
            estimated = False
        else:
            length, estimated = distance(worker.pos, mine), True
        home = distance(base.pos, mine) if base else 0
        to_vendor = distance(vendor, mine) if vendor else home
        value = prices.get(ore, 1) * batch / (batch + 2 * length + 2 * home + .5 * to_vendor + 2)
        rows.append({'pos': mine.dump(), 'ore': ore, 'price': prices.get(ore, 1),
                     'worker_steps': None if length >= 10**6 else length, 'estimated': estimated,
                     'base_distance': home, 'vendor_distance': to_vendor, 'batch': batch,
                     'value': round(value, 3), 'night_safe': safe_cell(turn, mine),
                     'local': not night_caution(turn) or safe_cell(turn, mine)})
    # Ranking is by value; the exact distance is only a tie breaker.
    rows.sort(key=lambda row: (-row['value'], row['worker_steps'] or 10**6, row['pos']['x'], row['pos']['y']))
    payload = {'round': turn.round_no, 'prices': prices, 'mines': rows[:12],
               'measured': sum(1 for row in rows if not row['estimated']),
               'best': rows[0] if rows else None,
               'history': record_prices(turn, state)}
    state['economy_survey'] = payload
    blackboard.write(state, 'economy', {k: payload[k] for k in ('round', 'prices', 'best')}
                     | {'mines': len(rows), 'measured': payload['measured']}, turn.round_no)
    return payload


def funding_need(turn, state):
    """Gold the defence wants within FUNDING_HORIZON rounds; drives the sell decision."""
    prices = defense_agent.shop_prices(turn)
    held = {item for unit in turn.controllable() for item in unit.backpack}
    need = 0
    station = turn.station()
    if station and station.level < 3:
        name = f'StationUpgradeVoucher{station.level}'
        if name not in held:
            need += prices.get(name, 100)
    for tower in turn.weapons():
        name = f'WeaponUpgradeVoucher{tower.level}'
        if tower.level < 3 and name not in held:
            need += prices.get(name, 100)
            break
    damaged = any(wall.health < defense_agent._max_health(wall) for wall in turn.walls())
    if damaged and 'WallFixer' not in held:
        need += prices.get('WallFixer', 10)
    return need


def _held_reason(state, turn, ores):
    """Explain a hold decision using the news forecast window, when there is one.

    Uses the same ``should_hold`` contract as the selling policy, so a forecast that
    has matured (``holdUntil`` reached) no longer blocks the sale.
    """
    windows = []
    for name, _ in ores:
        if not should_hold(state, name, turn.round_no):
            return None
        forecast = next((f for f in (state.get('intelligence') or {}).get('market', [])
                         if f.get('ore') == name), None)
        if not forecast or not forecast.get('holdUntil'):
            return None
        windows.append(forecast['holdUntil'])
    if not windows:
        return None
    return f"新闻预测持有到第{max(windows)}回合再卖"


def sell_timing(turn, state, worker, keep_stone=0):
    """Decide whether to sell now, and say why. Returns a plan fragment."""
    vendor = turn.vendor()
    value, rows = cargo_value(turn, worker, keep_stone)
    if not rows or value <= 0:
        return {'sell': False, 'force': False, 'reason': 'no_ore', 'value': 0}
    gold = turn.gold
    need = funding_need(turn, state)
    missing = max(0, need - gold)
    sticky = (state.get('economy_jobs', {}).get(str(worker.unit_id), {}) or {}).get('kind') == 'sell'
    at_vendor = bool(vendor and distance(worker.pos, vendor) <= 1)
    # 1. Night: hauling across the map is not worth it unless the backpack is full.
    if not turn.is_day and not worker.backpack_full:
        return {'sell': False, 'force': False, 'reason': 'night_keep_mining', 'value': value,
                'keep_stone': keep_stone}
    # 2. A price forecast that has not matured beats the convenience of standing
    #    next to the vendor: holding is the whole point of the news channel.
    hold = _held_reason(state, turn, rows)
    if hold and not worker.backpack_full:
        return {'sell': False, 'force': False, 'reason': hold, 'value': value, 'keep_stone': keep_stone}
    # 3. Standing next to the vendor with nothing to wait for: selling costs no tempo.
    if at_vendor:
        return {'sell': True, 'force': True, 'reason': 'at_vendor', 'value': value, 'keep_stone': keep_stone}
    # 4. Defence funding (day only: routing to the vendor at night is unsafe):
    #    cash on hand blocks the next purchase, so realise ore now.
    if turn.is_day and missing > 0 and value >= missing:
        return {'sell': True, 'force': True, 'reason': f'defense_funding({missing}金缺口)',
                'value': value, 'keep_stone': keep_stone}
    # 5. Before dusk: clear the pack while the lanes are still open.
    phase = (turn.round_no - 1) % 130
    if turn.is_day and 70 - phase <= PRE_NIGHT_WINDOW and not night_caution(turn):
        return {'sell': True, 'force': False, 'reason': f'before_night(剩{70 - phase}回合)',
                'value': value, 'keep_stone': keep_stone}
    if worker.backpack_full:
        return {'sell': True, 'force': False, 'reason': 'backpack_full', 'value': value,
                'keep_stone': keep_stone}
    if sticky:
        return {'sell': True, 'force': False, 'reason': 'sell_trip_in_progress', 'value': value,
                'keep_stone': keep_stone}
    return {'sell': False, 'force': False, 'reason': 'keep_mining', 'value': value, 'keep_stone': keep_stone}


def plan(turn, worker, state, reserved=()):
    """One decision for the gathering worker: sell / collect / wait, with a reason.

    The miner never reserves stone (walls are the defender's job), so ``keep_stone``
    stays 0 here and surplus stone is sold rather than hoarded.
    """
    keep_stone = 0
    timing = sell_timing(turn, state, worker, keep_stone)
    result = {'round': turn.round_no, 'kind': 'sell' if timing['sell'] else 'collect',
              'force': timing['force'], 'reason': timing['reason'], 'value': timing['value'],
              'keep_stone': keep_stone, 'night': not turn.is_day,
              'switch_rule': '锁定矿点优先；满包、防御缺钱、夜前窗口或危险才切换'}
    state['economy_plan'] = result
    blackboard.write(state, 'economy_plan', result, turn.round_no)
    return result


def eta_rounds(turn, worker, target):
    """Reachable round estimate for the current plan target (None when unreachable)."""
    step, length = route_to(turn, worker, target, set())
    return None if length >= 10**6 else length + 1
