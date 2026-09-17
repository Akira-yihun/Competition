from ..model import *
from ..protocol import move_command, build_command, attack_command, sell_command
from ..navigation import route, STEPS
from ..world import _neighbours, _footprint_distance, _walk, _cells_at_distance
from itertools import permutations, combinations
from .combat import _attack_targets

def _tower_pairs(turn, roles=None):
    roles = tuple(turn.controllable() if roles is None else roles)
    towers = turn.weapons()
    n = min(len(roles),len(towers))
    if not n:
        return ()
    costs = {(r.unit_id,t.unit_id):route(turn,r,_neighbours(t.pos))[1] for r in roles for t in towers}
    best = min((tuple(zip(rs,ts)) for rs in permutations(roles,n) for ts in combinations(towers,n)),
               key=lambda pairs: (sum(costs[r.unit_id,t.unit_id] for r,t in pairs),
                                  tuple((r.unit_id,t.unit_id) for r,t in pairs)))
    return best

def plan(turn, available, reserved, commands):
    pairs=_tower_pairs(turn,available)
    phase=(turn.round_no-1)%130
    recalled=set()
    remaining={r.robot_id:r.health for r in turn.robots}
    for role,tower in pairs:
        path_length=route(turn,role,_neighbours(tower.pos),reserved)[1]
        if path_length>=10**6:
            continue
        if turn.is_day and 70-phase>path_length+4:
            continue
        recalled.add(role.unit_id)
        if distance(role.pos,tower.pos)<=1:
            if not turn.is_day and tower.cooldown==0:
                targets=_attack_targets(turn,tower,remaining)
                if targets:
                    commands[tower.unit_id]=attack_command(role.unit_id,targets)
                    if tower.kind=='rocket':
                        for r in turn.robots:
                            remaining[r.robot_id]=max(0,remaining[r.robot_id]-sum(20 if p==r.pos else 10 if distance(p,r.pos)<=1 else 0 for p in targets))
        else:
            _walk(turn,role,tower.pos,reserved,commands)
    return recalled
