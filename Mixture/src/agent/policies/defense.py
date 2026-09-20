"""One worker operates the rear rocket battery, respecting observed cooldowns.

Night targeting is delegated to ``agents/attack_agent`` (threat filter + value
estimate) and the situation report to ``agents/defense_agent``; this module keeps
the movement, cooldown rotation and emergency-upgrade rules.
"""
from ..model import distance
from ..navigation import route
from ..protocol import move_command, attack_command
from ..agents import attack_agent, blackboard, defense_agent
from .construction import operator_hub, operator_stands, upgrade_order
from .roles import assign
from ..objectives import goal


def should_recall(turn,role,state=None):
    memory=state if state is not None else {}
    defender,_=assign(turn,memory)
    if role!=defender:return False
    if not turn.is_day:return True
    if not turn.weapons():return False
    day=(turn.round_no-1)//130;phase=(turn.round_no-1)%130
    sticky=memory.get('defense',{})
    if sticky.get('day')==day and sticky.get('mobilized'):return True
    hub=operator_hub(turn)
    length=route(turn,role,[hub] if hub else operator_stands(turn,turn.weapons()[0]),cautious=False)[1]
    failures=memory.get('movement_failures',{}).get(str(role.unit_id),{}).get('count',0)
    chores=sum('UpgradeVoucher' in i and not i.startswith('Station') for i in role.backpack)
    return phase>=68 or length<10**6 and 70-phase<=length+chores+3+min(failures,4)


def plan(turn,available,reserved,commands,state=None):
    state=state if state is not None else {};worker,_=assign(turn,state)
    state['defense_assignments']=[]
    if worker is None or worker not in available or not should_recall(turn,worker,state):return set()
    state['defense']={'day':(turn.round_no-1)//130,'mobilized':[str(worker.unit_id)]}
    towers=turn.weapons();hub=operator_hub(turn)
    goal(state,turn,worker,'defend',hub,'夜前回防并保持操作位，冷却就绪即攻击')
    goals=[hub] if hub and all(distance(hub,t.pos)<=1 for t in towers) else [p for t in towers for p in operator_stands(turn,t)]
    step,length=route(turn,worker,goals,reserved,cautious=False) if goals else (None,10**6)
    state['defense_assignments']=[{'role':worker.unit_id,'tower':t.unit_id,'distance':distance(worker.pos,t.pos),'path_length':length} for t in towers]
    if step is not None:
        commands[worker.unit_id]=move_command(step);reserved.add(step)
    elif length==0:
        reserved.add(worker.pos)
        for tower in sorted(towers,key=lambda t:upgrade_order(turn,t)):
            voucher=f'WeaponUpgradeVoucher{tower.level}'
            if turn.is_day and tower.level<3 and voucher in worker.backpack and distance(worker.pos,tower.pos)<=1:
                commands[worker.unit_id]={'action':'use','name':voucher,'targetPos':[tower.pos.dump()]}
                return {worker.unit_id}
        if not turn.is_day:
            report = defense_agent.assess(turn, state)
            last=state.get('last_fired',{})
            ready=sorted((t for t in towers if t.cooldown==0 and distance(worker.pos,t.pos)<=1),key=lambda t:(-t.level,last.get(str(t.unit_id),-1),t.unit_id))
            for tower in ready:
                # Attack sub-agent: only robots targeting our base are threats, and the
                # chosen aim set is scored for damage, kills and splash before firing.
                targets, analysis = attack_agent.choose(turn, tower, state)
                state['attack_assessment'] = analysis
                blackboard.write(state, 'attack', analysis, turn.round_no)
                if targets:
                    commands[tower.unit_id]=attack_command(worker.unit_id,targets)
                    state.setdefault('last_fired',{})[str(tower.unit_id)]=turn.round_no
                    goal(state,turn,worker,'defend',hub,
                         f"第{turn.round_no}回合开火：{analysis.get('reason','')}"
                         f"（预计伤害{analysis.get('damage',0)}，击杀{len(analysis.get('kills',[]))}）")
                    break
            if report.get('summary'):
                state['defense_report'] = report['summary']
    return {worker.unit_id}


def _tower_pairs(turn,roles=None):
    """Compatibility inspection: one eligible worker owns the whole battery."""
    workers=[r for r in (turn.workers() if roles is None else roles) if r.kind=='worker']
    return tuple((workers[0],tower) for tower in turn.weapons()) if workers else ()


def emergency_upgrade(turn,state,reserved,commands):
    station=turn.station();worker,_=assign(turn,state)
    if not station or not worker or station.level>=3:return False
    name=f'StationUpgradeVoucher{station.level}'
    if name not in worker.backpack:return False
    team=turn.raw.get('teamOur',{}).get('type','')
    damage={'smallRobot':5,'middleRobot':10,'largeRobot':20,'bossRobot':40}
    incoming=sum(damage.get(r.kind,40)*2 for r in turn.robots if r.health>0
                 and (not team or not r.target_team or r.target_team==team)
                 and min(distance(r.pos,p) for p in turn.footprint(station))<=5)
    if station.health>=100 and station.health>incoming:return False
    goal(state,turn,worker,'emergency_base_upgrade',station.pos,'基地低血或两回合内致命，优先用券回血')
    if min(distance(worker.pos,p) for p in turn.footprint(station))<=1:
        commands[worker.unit_id]={'action':'use','name':name,'targetPos':[station.pos.dump()]}
    else:
        from ..world import _neighbours
        goals=[q for p in turn.footprint(station) for q in _neighbours(p)]
        step,_=route(turn,worker,goals,reserved,cautious=False)
        if step is not None:commands[worker.unit_id]=move_command(step);reserved.add(step)
    return True
