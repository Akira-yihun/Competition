"""Pure turn orchestration; state changes are returned, never committed here."""
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from .protocol import decode, empty_response
from .model import distance
from .world import _walk
from .policies import defense, economy
from .tasks.legacy import _task
from .guard import validate

@dataclass
class Decision:
    response: dict
    state: dict
    based_on_version: int


def compute(payload, state=None, deadline=None):
    turn=decode(payload)
    snapshot=deepcopy(state) if state is not None else None
    response=empty_response(); commands={}; reserved=set()
    pioneer=next((r for r in turn.controllable() if r.kind=='pioneer'),None)
    economy.healing(turn,commands)
    recall_pioneer=bool(pioneer and defense.should_recall(turn,pioneer,snapshot))
    if snapshot is not None:
        from .tasks.workflow import advance
        if not recall_pioneer and (not pioneer or pioneer.unit_id not in commands):
            advance(turn,pioneer,snapshot,commands,response)
        elif snapshot.get('task'):
            snapshot['task']['pending']=None
            snapshot['task']['status']='ABANDON_PENDING'
            snapshot['task']['last_parse_reason']='return_to_turret_before_night'
    elif turn.phase_task and pioneer and not recall_pioneer and pioneer.unit_id not in commands:
        _task(turn,pioneer,commands,response)
    if deadline is not None and monotonic()>=deadline:
        raise TimeoutError('decision deadline')
    available=[r for r in turn.controllable() if r.unit_id not in commands and not (r.kind=='pioneer' and turn.phase_task and not recall_pioneer)]
    recalled=defense.plan(turn,available,reserved,commands,snapshot)
    if turn.is_day:
        economy.plan(turn,recalled,reserved,commands,snapshot)
        if pioneer and not turn.phase_task and pioneer.unit_id not in recalled and pioneer.unit_id not in commands and (turn.round_no-1)%130<42:
            tasks=[t for t in turn.tasks if t.valid]
            if tasks:
                task=max(tasks,key=lambda t:((t.score_reward+0.5*t.gold_reward)/(4+distance(pioneer.pos,t.position)),-t.position.x,-t.position.y))
                if distance(pioneer.pos,task.position)<=1:
                    commands[pioneer.unit_id]={'action':'acceptTask'}
                else:
                    _walk(turn,pioneer,task.position,reserved,commands)
    response['roleCommandMap']={str(k):v for k,v in commands.items()}
    draft=deepcopy(response)
    response=validate(response,turn)
    if deadline is not None and monotonic()>=deadline:
        raise TimeoutError('decision deadline')
    if snapshot is not None:
        snapshot['last_actions']=deepcopy(response['roleCommandMap'])
        snapshot['guard_dropped']={k:v for k,v in draft['roleCommandMap'].items() if k not in response['roleCommandMap']}
        if snapshot.get('task') and snapshot['task'].get('pending') and snapshot['task']['pending']['round']==turn.round_no:
            kind=snapshot['task']['pending']['kind']
            emitted=response['prompt'] if kind=='model' else response['executeCmd'] if kind=='command' else any(c.get('action')=='submitAnswer' for c in response['roleCommandMap'].values())
            if not emitted:
                snapshot['task']['pending']=None
                snapshot['task']['last_parse_reason']='guard_rejected_output'
    return Decision(response,snapshot or {},(state or {}).get('version',0))


def decide(payload):
    return compute(payload).response
