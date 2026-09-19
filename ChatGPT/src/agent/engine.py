"""Pure turn orchestration, stable duties and serialized model channels."""
from copy import deepcopy
from dataclasses import dataclass, replace
from time import monotonic
from .protocol import decode, empty_response
from .model import distance
from .world import _walk
from .navigation import evacuate, night_caution, safe_cell
from .policies import defense, economy, treasure, pioneer as pioneer_policy
from . import objectives
from .policies.roles import assign
from .policies.construction import operator_hub
from .intelligence import news
from .tasks.workflow import advance
from .guard import validate

@dataclass
class Decision:
    response: dict
    state: dict
    based_on_version: int


def compute(payload, state=None, deadline=None):
    turn=decode(payload)
    snapshot=deepcopy(state) if state is not None else {'version':0,'last_round':0,'epoch':0,'task':None,'memory':[]}
    response=empty_response();commands={};reserved=set()
    pioneer=next((r for r in turn.controllable() if r.kind=='pioneer'),None)
    assign(turn,snapshot)
    turn=replace(turn,navigation_avoid=objectives.begin(turn,snapshot))
    consumed=news.ingest(turn,snapshot)
    economy.healing(turn,commands)
    defense.emergency_upgrade(turn,snapshot,reserved,commands)
    # General-news feedback cannot satisfy a task call. A task waits for the
    # single news channel to drain, then all subsequent calls are task-exempt.
    exiting=pioneer_policy.safety(turn,pioneer,snapshot,reserved,commands) if pioneer else False
    if not exiting and not snapshot['intelligence'].get('pending'):
        advance(replace(turn,llm_response='') if consumed else turn,pioneer,snapshot,commands,response)
    if deadline is not None and monotonic()>=deadline:raise TimeoutError('decision deadline')
    available=[r for r in turn.workers() if r.unit_id not in commands]
    recalled=defense.plan(turn,available,reserved,commands,snapshot)
    hub=operator_hub(turn)
    if hub:reserved.add(hub)
    # Mining and trade may continue at night; building remains day-only in policy/guard.
    for role in turn.workers():
        if role.unit_id not in commands and role.unit_id not in recalled:
            evacuate(turn,role,reserved,commands)
    economy.plan(turn,recalled,reserved,commands,snapshot)
    news.schedule(turn,snapshot,response)
    if pioneer and pioneer.unit_id not in commands:
        pursuing=False
        if not turn.phase_task:
            pursuing=treasure.plan(turn,pioneer,snapshot,reserved,commands)
        if not pursuing:pioneer_policy.plan(turn,pioneer,snapshot,reserved,commands,response)
    response['roleCommandMap']={str(k):v for k,v in commands.items()}
    draft=deepcopy(response);response=validate(response,turn)
    if deadline is not None and monotonic()>=deadline:raise TimeoutError('decision deadline')
    objectives.finish(turn,snapshot,response)
    snapshot['last_actions']=deepcopy(response['roleCommandMap'])
    snapshot['guard_dropped']={k:v for k,v in draft['roleCommandMap'].items() if k not in response['roleCommandMap']}
    if snapshot.get('task') and snapshot['task'].get('pending') and snapshot['task']['pending']['round']==turn.round_no:
        kind=snapshot['task']['pending']['kind']
        emitted=response['prompt'] if kind=='model' else response['executeCmd'] if kind=='command' else any(c.get('action')=='submitAnswer' for c in response['roleCommandMap'].values())
        if not emitted:snapshot['task']['pending']=None;snapshot['task']['last_parse_reason']='guard_rejected_output'
    mem=snapshot['intelligence']
    if mem.get('pending') and mem['pending']['round']==turn.round_no and not response['prompt']:
        mem['pending']=None;mem['used']-=1;mem['analyzed_revision']=None
    if mem.get('treasure_attempt') and mem['treasure_attempt']['round']==turn.round_no and not any(c.get('action')=='summonTreasure' for c in response['roleCommandMap'].values()):mem['treasure_attempt']=None
    return Decision(response,snapshot,(state or {}).get('version',0))


def decide(payload):return compute(payload).response
