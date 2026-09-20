"""Self-evolution sub-agent (docs/design/11): context, tolerant parsing, loop guards.

The platform owns the transport: our prompt is answered by the model in a later
request, and one command result arrives in ``lastCmdResult``. This module owns
everything around it:

* the rolling context — original task, current effective task, confirmed facts and
  the most recent command/observation steps;
* tolerant action parsing that **never raises**; an unreadable model reply is
  recorded as a reason and the loop simply tries again;
* loop guards — repeated command, missing progress, step budget, low remaining
  rounds — which switch the agent to "stop exploring and answer" mode;
* the rolling ``taskSummary`` extraction used as experience for the next task at
  the same task point.

State lives in ``task['se']`` as plain JSON data so session snapshots stay
deep-copyable and loggable.
"""
from ..tasks.channel import parse_with_reason

MAX_STEPS = 24             # commands per task instance before we force an answer
RECENT_STEPS = 6           # raw observations kept verbatim in the prompt
REPEAT_LIMIT = 3           # identical command sent this many times => stop exploring
REJECT_LIMIT = 3           # unreadable model replies in a row => stop exploring
MIN_REMAINING_FOR_COMMAND = 2
FACT_LIMIT = 8


def new_state(task_text):
    """Fresh per-task agent state; the original task is never overwritten."""
    return {'original_task': str(task_text)[:20000], 'effective_task': '', 'facts': [],
            'steps': [], 'commands_issued': 0, 'rejections': 0, 'repeats': {},
            'stop_reason': '', 'findings': []}


def state_of(task):
    """Return the agent state of a task instance, creating it when missing."""
    state = task.get('se')
    if not isinstance(state, dict):
        state = new_state(task.get('text') or '')
        task['se'] = state
    state.setdefault('steps', [])
    state.setdefault('repeats', {})
    state.setdefault('facts', [])
    return state


def facts(turn, task):
    """Confirmed facts. Only evidence counts: model prose is a hypothesis, not a fact."""
    state = state_of(task)
    out = []
    history = task.get('sandbox_history', [])
    returned = [entry for entry in history if entry.get('result')]
    if returned:
        out.append(f"已收到{len(returned)}/{len(history)}条命令返回，最近返回回合{returned[-1].get('result_round')}")
    understanding = task.get('understanding')
    if isinstance(understanding, dict) and understanding.get('objective'):
        out.append('当前任务目标（模型推断）：' + str(understanding['objective'])[:160])
    if task.get('summary'):
        out.append('同任务点已有方法摘要，可参数化复用')
    rows = task.get('sandbox_history') or []
    if rows and rows[-1].get('result'):
        last = str(rows[-1]['result']).strip().splitlines()[:1]
        if last:
            out.append('最近一条返回首行：' + last[0][:120])
    state['facts'] = out[:FACT_LIMIT]
    return state['facts']


def note_command(task, command, purpose='', round_no=None):
    """Record an emitted command so repeats and progress can be judged later."""
    state = state_of(task)
    state['repeats'][command] = state['repeats'].get(command, 0) + 1
    # ``steps`` is only the rolling prompt window, so the budget needs its own counter.
    state['commands_issued'] = state.get('commands_issued', 0) + 1
    state['steps'].append({'round': round_no, 'command': command, 'purpose': purpose, 'result': ''})
    del state['steps'][:-RECENT_STEPS]  # keep only the rolling window
    return state['steps'][-1]


def note_result(task, result, round_no=None):
    """Attach the platform's command result to the step that produced it."""
    state = state_of(task)
    for step in reversed(state['steps']):
        if not step.get('result'):
            step['result'] = str(result)[:4000]
            step['result_round'] = round_no
            return step
    return None


def parse_action(text, pending, instance):
    """Tolerant parse of one model reply.

    Returns ``(parsed, reason, purpose, findings)``. ``parsed`` is the channel's
    ``(kind, value)`` or ``None``; this function never raises.
    """
    findings = []
    try:
        parsed, reason = parse_with_reason(text, pending, instance)
    except Exception as exc:  # Defensive: a malformed reply must not kill the round.
        return None, 'parse_exception_' + type(exc).__name__, '', [{'code': 'parse_exception',
                                                                  'detail': str(exc)[:200]}]
    purpose = ''
    try:
        import json
        raw = (text or '').strip()
        if raw.startswith('```'):
            lines = raw.splitlines()
            raw = '\n'.join(lines[1:-1]) if lines and lines[-1].strip() == '```' else '\n'.join(lines[1:])
        value = json.loads(raw)
        if isinstance(value, dict):
            purpose = str(value.get('purpose') or '')[:300]
            if isinstance(value.get('type'), str) and value['type'] not in ('command', 'final'):
                findings.append({'code': 'unknown_action_type', 'detail': value['type'][:60]})
    except (ValueError, TypeError):
        purpose = ''
    return parsed, reason, purpose, findings


def guard_reason(turn, task):
    """Why the agent must stop issuing commands, or ``None`` when exploring is fine."""
    state = state_of(task)
    remaining = max(0, task.get('timeout', 30) - (turn.round_no - task.get('accepted_round', turn.round_no)))
    issued = state.get('commands_issued', 0)
    if issued >= MAX_STEPS:
        return 'budget_exhausted', f'已执行{issued}条命令，达到步数预算{MAX_STEPS}'
    if remaining <= MIN_REMAINING_FOR_COMMAND:
        return 'low_remaining_rounds', f'任务仅剩{remaining}回合，必须直接提交可验证结果'
    repeated = [cmd for cmd, count in state['repeats'].items() if count >= REPEAT_LIMIT]
    if repeated:
        return 'repeated_command', f'命令重复{REPEAT_LIMIT}次：{repeated[-1][:120]}'
    if state.get('rejections', 0) >= REJECT_LIMIT:
        return 'too_many_rejections', f'连续{state["rejections"]}次无法解析模型响应'
    results = [step.get('result') for step in state['steps'] if step.get('result')]
    if len(results) >= 3 and len(set(results[-3:])) == 1:
        return 'no_progress', '连续三次命令返回相同内容，继续探索没有新信息'
    return None


def stop_reason(turn, task):
    guard = guard_reason(turn, task)
    if guard:
        state_of(task)['stop_reason'] = guard[0] + ':' + guard[1]
        return state_of(task)['stop_reason']
    return ''


def answer_only(turn, task):
    """True when the next model call may only submit an answer."""
    return guard_reason(turn, task) is not None


def budget_note(turn, task):
    """Short human-readable progress line embedded in the prompt context."""
    state = state_of(task)
    remaining = max(0, task.get('timeout', 30) - (turn.round_no - task.get('accepted_round', turn.round_no)))
    return (f"已用命令{state.get('commands_issued', 0)}/{MAX_STEPS}，解析失败{state.get('rejections', 0)}次，"
            f"剩余回合{remaining}")


def build_context(context, turn, task, state):
    """Add the doc-11 context sections to the platform context (no key removed)."""
    se = state_of(task)
    context.update({
        'originalTask': se['original_task'],
        'effectiveTask': se.get('effective_task') or turn.phase_task,
        'facts': facts(turn, task),
        'recentSteps': [{'round': step.get('round'), 'command': step.get('command'),
                         'purpose': step.get('purpose'), 'result': (step.get('result') or '')[:600]}
                        for step in se['steps'][-RECENT_STEPS:]],
        'progress': budget_note(turn, task),
        'stopReason': stop_reason(turn, task),
    })
    return context


def note_rejection(task, reason, findings=()):
    """Count an unusable model reply; keeps the loop from spinning silently."""
    state = state_of(task)
    state['rejections'] = state.get('rejections', 0) + 1
    if findings:
        state['findings'] = (state.get('findings', []) + list(findings))[-8:]
    return state['rejections']


def note_accepted(task, reason):
    """A readable reply resets the consecutive-rejection counter."""
    state_of(task)['rejections'] = 0
    return reason


def summary_from(text):
    """Extract the rolling ``taskSummary`` experience block; never raises."""
    import json
    try:
        raw = (text or '').strip()
        if raw.startswith('```'):
            lines = raw.splitlines()
            raw = '\n'.join(lines[1:-1]) if lines and lines[-1].strip() == '```' else '\n'.join(lines[1:])
        envelope = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(envelope, dict):
        return None
    summary = envelope.get('taskSummary')
    if not isinstance(summary, dict):
        return None
    try:
        if len(json.dumps(summary, ensure_ascii=False)) > 12000:
            return None
    except (TypeError, ValueError):
        return None
    return summary
