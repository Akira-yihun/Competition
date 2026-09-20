"""Persistent per-role objectives and feedback; no additional model calls."""
from .model import Pos


def begin(turn, state):
    failures = state.setdefault('movement_failures', {})
    feedback = turn.raw.get('lastRoundRoleActionResults') or {}
    previous = state.get('last_actions', {})
    for role in turn.controllable():
        key = str(role.unit_id)
        command = previous.get(key, {})
        if state.get('last_round') != turn.round_no - 1:
            failures.pop(key, None)
            continue
        if command.get('action') != 'move':
            continue
        target = command['targetPos'][0]
        failed = feedback.get(key, feedback.get(role.unit_id)) is False
        if failed:
            old = failures.get(key, {})
            count = old.get('count', 0) + 1 if old.get('target') == target else 1
            failures[key] = {'target': target, 'count': count, 'until': turn.round_no + 2}
        else:
            failures.pop(key, None)
    return {int(key): (Pos.load(item['target']),) for key, item in failures.items()
            if item['count'] >= 2 and item['until'] >= turn.round_no}


def goal(state, turn, role, kind, target=None, reason='', phase=None):
    plans = state.setdefault('role_plans', {})
    key = str(role.unit_id)
    destination = target.dump() if isinstance(target, Pos) else target
    old = plans.get(key, {})
    same = old.get('goal') == kind and old.get('target') == destination
    plans[key] = {'duty': state.get('role_duties', {}).get('defender') == role.unit_id and 'defender'
                   or ('pioneer' if role.kind == 'pioneer' else 'miner'),
                  'goal': kind, 'target': destination, 'phase': phase or kind,
                  'since': old.get('since', turn.round_no) if same else turn.round_no,
                  'reason': reason, 'round': turn.round_no}
    if old and not same:
        history = state.setdefault('goal_history', {}).setdefault(key, [])
        history.append({'round': turn.round_no, 'from': old.get('goal'), 'to': kind, 'reason': reason})
        del history[:-12]


def finish(turn, state, response):
    commands = response['roleCommandMap']
    controllers = {c.get('controllerId') for c in commands.values() if c.get('action') == 'attack'}
    for role in turn.controllable():
        plan = state.setdefault('role_plans', {}).get(str(role.unit_id))
        if not plan or plan.get('round') != turn.round_no:
            command = commands.get(str(role.unit_id))
            if str(role.unit_id) in controllers:
                goal(state, turn, role, 'defend', reason='操纵就绪武器')
            elif command:
                goal(state, turn, role, command['action'], reason='执行更高优先级动作')
            else:
                goal(state, turn, role, 'wait', role.pos, '当前没有安全且可执行的工作')
