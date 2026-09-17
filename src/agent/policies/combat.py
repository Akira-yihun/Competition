from ..model import *
from ..protocol import move_command, build_command, attack_command, sell_command
from ..navigation import route, STEPS
from ..world import _neighbours, _footprint_distance, _walk, _cells_at_distance

def _attack_targets(turn, tower, remaining=None):
    team = turn.raw.get('teamOur',{}).get('type','')
    robots = [r for r in turn.robots if r.health>0 and (not team or not r.target_team or r.target_team==team)]
    hp = remaining if remaining is not None else {r.robot_id:r.health for r in robots}
    robots = [r for r in robots if hp.get(r.robot_id,r.health)>0]
    station = turn.station()
    footprint = station_footprint(station.pos) if station else (tower.pos,)
    ordered = sorted(robots,key=lambda r:(_footprint_distance(r.pos,footprint),hp.get(r.robot_id,r.health),r.robot_id))
    reachable = [r for r in ordered if 0<distance(tower.pos,r.pos)<=tower.range_of_attack()]
    level = min(3,max(1,tower.level))
    if tower.kind == 'rocket':
        candidates = {p for r in robots for p in (r.pos,)+_neighbours(r.pos)
                      if 0<=p.x<turn.width and 0<=p.y<turn.height and distance(p,tower.pos)<=tower.range_of_attack()}
        if not candidates:
            return []
        targets=[]
        local=dict(hp)
        for _ in range(level):
            def value(p):
                return sum(min(local.get(r.robot_id,r.health),20 if p==r.pos else 10)
                           * (2 if _footprint_distance(r.pos,footprint)<=3 else 1)
                           for r in robots if distance(p,r.pos)<=1)
            target=max(candidates,key=lambda p:(value(p),-_footprint_distance(p,footprint),-p.x,-p.y))
            targets.append(target)
            for r in robots:
                if distance(target,r.pos)<=1:
                    local[r.robot_id]=max(0,local.get(r.robot_id,r.health)-(20 if target==r.pos else 10))
        return targets
    if not reachable:
        return []
    if tower.kind == 'railgun':
        return [reachable[0].pos]
    def compatible(a,b):
        return (a.x-tower.pos.x)*(b.x-tower.pos.x)+(a.y-tower.pos.y)*(b.y-tower.pos.y)>=0
    for leader in reachable:
        targets=[leader.pos]
        for robot in reachable:
            if robot.pos not in targets and all(compatible(robot.pos,p) for p in targets):
                targets.append(robot.pos)
                if len(targets)==level:
                    break
        if len(targets)>level:
            targets=targets[:level]
        # Interface requires exactly level positions. Use distinct legal aim cells;
        # never rely on undocumented duplicate Gatling target semantics.
        radius=tower.range_of_attack()
        for dx,dy in STEPS:
            if len(targets)==level:
                break
            p=Pos(leader.pos.x+dx,leader.pos.y+dy)
            if p!=tower.pos and 0<=p.x<turn.width and 0<=p.y<turn.height and distance(p,tower.pos)<=radius and p not in targets and all(compatible(p,q) for q in targets):
                targets.append(p)
        if len(targets)==level:
            return targets
    return []
