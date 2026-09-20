"""Attack sub-agent: legal aim geometry plus an explainable night-fire value.

Data source is the task book: robots are fully visible, their attack power, HP and
3-cell attack range come from §4.7.2; rocket missiles deal 20 at the centre and 10
in the surrounding 8 cells (§4.5.4). Only robots whose ``targetTeam`` is empty or
equal to our side are treated as threats — the rest are walking towards the enemy
base and are not this agent's business.

The module never invents aim points: ``policies/combat._attack_targets`` still owns
legality (target count per level, range, gatling cone, rocket anchor). This agent
re-scores the rocket candidates and always keeps the geometric result as fallback.
"""
from ..model import Pos, distance, station_footprint
from ..navigation import STEPS, night_caution
from ..policies.combat import _attack_targets

ROBOT_POWER = {'smallRobot': 5, 'middleRobot': 10, 'largeRobot': 20, 'bossRobot': 40}
ROBOT_HEALTH = {'smallRobot': 40, 'middleRobot': 60, 'largeRobot': 500, 'bossRobot': 800}
ROBOT_RANGE = 3
MISSILE_CENTRE = 20
MISSILE_SPLASH = 10
SPLASH_BONUS = 12


def our_team(turn):
    return turn.raw.get('teamOur', {}).get('type', '')


def is_threat(turn, robot):
    """True when the robot may attack our base (empty targetTeam is treated as unknown)."""
    team = our_team(turn)
    return robot.health > 0 and (not team or not robot.target_team or robot.target_team == team)


def threats(turn):
    return tuple(r for r in turn.robots if is_threat(turn, r))


def base_footprint(turn):
    station = turn.station()
    return station_footprint(station.pos) if station else ()


def robot_damage(robot):
    return ROBOT_POWER.get(robot.kind, 20)


def robot_health(robot):
    return ROBOT_HEALTH.get(robot.kind, robot.health or 40)


def weapon_damage(tower):
    """Per-round damage estimate for one weapon at its current level (§4.5.1/§4.5.4)."""
    level = min(3, max(1, tower.level))
    if tower.kind == 'gatling':
        return 10 * level
    if tower.kind == 'railgun':
        return 10 * level
    if tower.kind == 'rocket':
        return MISSILE_CENTRE * level
    return 0


def pressure(turn, robot, footprint=None):
    """Threat weight: attack power scaled by how soon it can hit the base."""
    footprint = footprint if footprint is not None else base_footprint(turn)
    if not footprint:
        return robot_damage(robot)
    gap = min(distance(robot.pos, cell) for cell in footprint)
    # Range 3 plus two rounds of approach sets the useful horizon; closer is worse.
    urgency = max(0, ROBOT_RANGE + 2 - gap + 1)
    return robot_damage(robot) * urgency


def _live(robots, remaining):
    return [r for r in robots if remaining.get(r.robot_id, r.health) > 0]


def aim_value(turn, aim, live, remaining, footprint):
    """Expected value of one missile (or one bullet) landing on ``aim``."""
    hit = [r for r in live if distance(aim, r.pos) <= 1]
    centre = [r for r in hit if r.pos == aim]
    damage = sum(min(remaining.get(r.robot_id, r.health), MISSILE_CENTRE if r in centre else MISSILE_SPLASH)
                 for r in hit)
    kills = sum(1 for r in hit
                if remaining.get(r.robot_id, r.health) <= (MISSILE_CENTRE if r in centre else MISSILE_SPLASH))
    pressure_cut = sum(pressure(turn, r, footprint) for r in hit)
    splash = SPLASH_BONUS if len(hit) >= 2 else 0
    return {'aim': aim, 'damage': damage, 'kills': kills, 'hits': len(hit),
            'pressure': pressure_cut, 'splash': splash,
            'score': damage + pressure_cut + splash}


def _candidate_aims(turn, tower, anchor):
    """Anchor cell and neighbours that stay inside the weapon's range."""
    radius = tower.range_of_attack()
    cells = [anchor.pos] + [Pos(anchor.pos.x + dx, anchor.pos.y + dy) for dx, dy in STEPS]
    return [p for p in cells
            if 0 <= p.x < turn.width and 0 <= p.y < turn.height and 0 < distance(tower.pos, p) <= radius]


def _rocket_plan(turn, tower, live, remaining, footprint):
    """Greedy per-missile plan scored by aim_value; returns (aims, total, detail)."""
    level = min(3, max(1, tower.level))
    local = dict(remaining)
    aims, detail, total = [], [], 0
    for _ in range(level):
        pool = _live(live, local)
        if not pool:
            break
        anchor = min(pool, key=lambda r: (min(distance(r.pos, c) for c in footprint) if footprint else 0,
                                          local.get(r.robot_id, r.health), r.robot_id))
        candidates = _candidate_aims(turn, tower, anchor)
        if not candidates:
            break
        scored = sorted((aim_value(turn, p, pool, local, footprint) for p in candidates),
                        key=lambda item: (-item['score'], item['aim'].x, item['aim'].y))
        best = scored[0]
        aims.append(best['aim'])
        total += best['score']
        detail.append(best)
        for robot in pool:
            if distance(best['aim'], robot.pos) <= 1:
                local[robot.robot_id] = max(0, local.get(robot.robot_id, robot.health)
                                            - (MISSILE_CENTRE if best['aim'] == robot.pos else MISSILE_SPLASH))
    if not aims:
        return [], 0, []
    aims += [aims[-1]] * (level - len(aims))  # protocol wants exactly one aim per level
    return aims, total, detail


def evaluate(turn, tower, aims):
    """Explain a finished aim set: what it hits, for how much, and who dies."""
    remaining = {r.robot_id: r.health for r in turn.robots}
    dealt_total, per_robot = 0, {}
    footprint = base_footprint(turn)
    for aim in aims:
        for robot in _live(list(turn.robots), remaining):
            if distance(aim, robot.pos) <= 1:
                dealt = min(remaining[robot.robot_id], MISSILE_CENTRE if aim == robot.pos else MISSILE_SPLASH)
                remaining[robot.robot_id] -= dealt
                dealt_total += dealt
                per_robot[robot.robot_id] = per_robot.get(robot.robot_id, 0) + dealt
    hits = [{'id': robot.robot_id, 'kind': robot.kind, 'damage': per_robot[robot.robot_id],
             'to_base': min(distance(robot.pos, c) for c in footprint) if footprint else None}
            for robot in turn.robots if robot.robot_id in per_robot]
    killed = [r for r in turn.robots if r.health > 0 and remaining.get(r.robot_id, 0) <= 0]
    return {'damage': dealt_total, 'hits': hits, 'kills': [r.robot_id for r in killed],
            'threat_cut': sum(pressure(turn, r, footprint) for r in killed)}


def choose(turn, tower, state=None):
    """Return (aims, analysis). ``analysis`` is safe to log on every night round."""
    live = [r for r in threats(turn) if r.health > 0]
    remaining = {r.robot_id: r.health for r in turn.robots}
    geometry = _attack_targets(turn, tower, remaining)
    if not live:
        return geometry, {'mode': 'no_threat', 'reason': '没有攻击我方的可见机器人',
                          'aims': [p.dump() for p in geometry]}
    if tower.kind == 'rocket':
        scored_aims, score, detail = _rocket_plan(turn, tower, live, remaining, base_footprint(turn))
        base = evaluate(turn, tower, geometry) if geometry else {'damage': 0, 'kills': [], 'threat_cut': 0}
        base_score = base['damage'] + base.get('threat_cut', 0)
        # Our plan is built from the same anchor neighbourhood, so range and target
        # count always match the protocol; keep the geometric result when it scores higher.
        if scored_aims and len(scored_aims) == min(3, max(1, tower.level)) and score >= base_score:
            analysis = evaluate(turn, tower, scored_aims)
            analysis.update({'mode': 'rocket_value', 'score': score,
                             'aims': [p.dump() for p in scored_aims],
                             'reason': '按基地距离、攻击力与溅射收益选点'})
            return scored_aims, analysis
        analysis = dict(base)
        analysis.update({'mode': 'geometry', 'score': base_score,
                         'aims': [p.dump() for p in geometry], 'reason': '收益评估未超过既有几何选点'})
        return geometry, analysis
    analysis = evaluate(turn, tower, geometry)
    analysis.update({'mode': 'direct', 'score': analysis['damage'],
                     'aims': [p.dump() for p in geometry], 'reason': '直射武器按最近威胁与穿透/锥形约束'})
    return geometry, analysis
