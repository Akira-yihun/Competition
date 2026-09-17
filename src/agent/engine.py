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
    if snapshot is not None:
        from .tasks.workflow import advance
        advance(turn,pioneer,snapshot,commands,response)
    elif turn.phase_task and pioneer:
        _task(turn,pioneer,commands,response)
    if deadline is not None and monotonic()>=deadline:
        raise TimeoutError('decision deadline')
    available=[r for r in turn.controllable() if not (r.kind=='pioneer' and turn.phase_task)]
    recalled=defense.plan(turn,available,reserved,commands)
    if turn.is_day:
        if pioneer and not turn.phase_task and pioneer.unit_id not in recalled:
            tasks=[t for t in turn.tasks if t.valid]
            if tasks:
                task=max(tasks,key=lambda t:((t.score_reward+0.5*t.gold_reward)/(4+distance(pioneer.pos,t.position)),-t.position.x,-t.position.y))
                if distance(pioneer.pos,task.position)<=1:
                    commands[pioneer.unit_id]={'action':'acceptTask'}
                else:
                    _walk(turn,pioneer,task.position,reserved,commands)
        economy.plan(turn,recalled,reserved,commands)
    response['roleCommandMap']={str(k):v for k,v in commands.items()}
    response=validate(response,turn)
    if deadline is not None and monotonic()>=deadline:
        raise TimeoutError('decision deadline')
    if snapshot is not None:
        snapshot['last_actions']=deepcopy(response['roleCommandMap'])
    return Decision(response,snapshot or {},(state or {}).get('version',0))


def decide(payload):
    return compute(payload).response
