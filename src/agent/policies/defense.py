"""One worker operates the rear rocket battery, respecting observed cooldowns."""
from ..model import distance
from ..navigation import route
from ..protocol import move_command, attack_command
from .construction import operator_hub, operator_stands
from .combat import _attack_targets
from .roles import assign


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
    length=route(turn,role,[hub] if hub else operator_stands(turn,turn.weapons()[0]))[1]
    return phase>=65 or length<10**6 and 70-phase<=length+3


def plan(turn,available,reserved,commands,state=None):
    state=state if state is not None else {};worker,_=assign(turn,state)
    state['defense_assignments']=[]
    if worker is None or worker not in available or not should_recall(turn,worker,state):return set()
    state['defense']={'day':(turn.round_no-1)//130,'mobilized':[str(worker.unit_id)]}
    towers=turn.weapons();hub=operator_hub(turn)
    goals=[hub] if hub and all(distance(hub,t.pos)<=1 for t in towers) else [p for t in towers for p in operator_stands(turn,t)]
    step,length=route(turn,worker,goals,reserved) if goals else (None,10**6)
    state['defense_assignments']=[{'role':worker.unit_id,'tower':t.unit_id,'distance':distance(worker.pos,t.pos),'path_length':length} for t in towers]
    if step is not None:
        commands[worker.unit_id]=move_command(step);reserved.add(step)
    elif length==0:
        reserved.add(worker.pos)
        for tower in sorted(towers,key=lambda t:(t.level,t.unit_id)):
            voucher=f'WeaponUpgradeVoucher{tower.level}'
            if tower.level<3 and voucher in worker.backpack and distance(worker.pos,tower.pos)<=1:
                commands[worker.unit_id]={'action':'use','name':voucher,'targetPos':[tower.pos.dump()]}
                return {worker.unit_id}
        if not turn.is_day:
            last=state.get('last_fired',{})
            ready=sorted((t for t in towers if t.cooldown==0 and distance(worker.pos,t.pos)<=1),key=lambda t:(last.get(str(t.unit_id),-1),-t.level,t.unit_id))
            for tower in ready:
                targets=_attack_targets(turn,tower,{r.robot_id:r.health for r in turn.robots})
                if targets:
                    commands[tower.unit_id]=attack_command(worker.unit_id,targets)
                    state.setdefault('last_fired',{})[str(tower.unit_id)]=turn.round_no
                    break
    return {worker.unit_id}


def _tower_pairs(turn,roles=None):
    """Compatibility inspection: one eligible worker owns the whole battery."""
    workers=[r for r in (turn.workers() if roles is None else roles) if r.kind=='worker']
    return tuple((workers[0],tower) for tower in turn.weapons()) if workers else ()
