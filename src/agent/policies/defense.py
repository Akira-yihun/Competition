"""Sticky dusk mobilization and explicit adjacent turret operators."""
from itertools import permutations, combinations
from dataclasses import replace
from ..world import _neighbours
from ..model import distance
from ..navigation import route
from ..protocol import move_command, attack_command
from .construction import operator_stands
from .combat import _attack_targets


def _tower_pairs(turn,roles=None):
    roles=tuple(turn.controllable() if roles is None else roles)
    towers=turn.weapons();n=min(len(roles),len(towers))
    if not n:return ()
    costs={(r.unit_id,t.unit_id):(0 if distance(r.pos,t.pos)<=1 else route(turn,r,operator_stands(turn,t))[1]) for r in roles for t in towers}
    return min((tuple(zip(rs,ts)) for rs in permutations(roles,n) for ts in combinations(towers,n)),
               key=lambda pairs:(sum(costs[r.unit_id,t.unit_id] for r,t in pairs),tuple((r.unit_id,t.unit_id) for r,t in pairs)))


def should_recall(turn,role,state=None):
    towers=turn.weapons()
    if not towers:return not turn.is_day
    phase=(turn.round_no-1)%130
    sticky=(state or {}).get('defense',{})
    if not turn.is_day or sticky.get('day')==(turn.round_no-1)//130 and str(role.unit_id) in sticky.get('mobilized',[]):
        return True
    lengths=[route(turn,role,operator_stands(turn,t))[1] for t in towers]
    reachable=[n for n in lengths if n<10**6]
    # Once dusk begins everyone stays home, independent of shrinking path length.
    return phase>=50 or bool(reachable and 70-phase<=min(reachable)+8)


def _yield_for_return(turn,pairs,reserved):
    """An adjacent operator may sidestep to reopen the one-cell inner corridor."""
    for waiting,target in pairs:
        if distance(waiting.pos,target.pos)<=1:continue
        if route(turn,waiting,operator_stands(turn,target),reserved)[1]<10**6:continue
        for blocker,tower in pairs:
            if blocker==waiting or distance(blocker.pos,tower.pos)>1:continue
            for cell in _neighbours(blocker.pos):
                if distance(cell,tower.pos)>1 or not turn.land(cell) or cell in reserved or cell in turn.blocked(blocker):continue
                shifted=replace(turn,ours=tuple(replace(u,pos=cell) if u.unit_id==blocker.unit_id else u for u in turn.ours))
                if route(shifted,waiting,operator_stands(shifted,target),reserved)[1]<10**6:
                    return blocker.unit_id,cell
    return None


def plan(turn,available,reserved,commands,state=None):
    day=(turn.round_no-1)//130
    memory=(state or {}).get('defense',{})
    if memory.get('day')!=day:memory={'day':day,'mobilized':[]}
    recalled=set();remaining={r.robot_id:r.health for r in turn.robots}
    assignments=[]
    pairs=_tower_pairs(turn,available)
    mobilized_pairs=tuple((r,t) for r,t in pairs if should_recall(turn,r,state))
    yielding=_yield_for_return(turn,mobilized_pairs,reserved)
    if yielding:
        commands[yielding[0]]=move_command(yielding[1]);reserved.add(yielding[1])
    for role,tower in pairs:
        if not should_recall(turn,role,state):continue
        goals=operator_stands(turn,tower)
        adjacent=distance(role.pos,tower.pos)<=1
        step,length=route(turn,role,goals,reserved) if not adjacent else (None,0)
        recalled.add(role.unit_id)
        if str(role.unit_id) not in memory['mobilized']:memory['mobilized'].append(str(role.unit_id))
        assignments.append({'role':role.unit_id,'tower':tower.unit_id,'distance':distance(role.pos,tower.pos),'path_length':length})
        if yielding and role.unit_id==yielding[0]:continue
        if adjacent:
            reserved.add(role.pos)
            if not turn.is_day and tower.cooldown==0:
                targets=_attack_targets(turn,tower,remaining)
                if targets:
                    commands[tower.unit_id]=attack_command(role.unit_id,targets)
                    if tower.kind=='rocket':
                        for r in turn.robots:
                            remaining[r.robot_id]=max(0,remaining[r.robot_id]-sum(20 if p==r.pos else 10 if distance(p,r.pos)<=1 else 0 for p in targets))
        elif step is not None:
            commands[role.unit_id]=move_command(step);reserved.add(step)
    if state is not None:
        state['defense']=memory;state['defense_assignments']=assignments
    return recalled
