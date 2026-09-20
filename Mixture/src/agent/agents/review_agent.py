"""Review agent: independent format audit for commands, responses and model actions.

The reviewer only *reports*. ``guard.validate`` stays the single authority that
drops illegal commands, so a finding never silently removes an action; it makes
the reason visible in the session state and the round log.

Findings are dicts: ``{'code': str, 'detail': str, 'role': int | None}``.
"""
import json

from . import blackboard
from ..model import Pos

KNOWN_ACTIONS = ('move', 'attack', 'build', 'remove', 'collect', 'buy', 'sell', 'use',
                 'drop', 'acceptTask', 'submitAnswer', 'summonTreasure')
POINT_ACTIONS = ('move', 'build', 'collect', 'remove')
RESPONSE_KEYS = ('roleCommandMap', 'prompt', 'executeCmd')
PROMPT_LIMIT = 180000
COMMAND_LIMIT = 12000
ANSWER_LIMIT = 64000


def _finding(code, detail, role=None):
    return {'code': code, 'detail': detail, 'role': role}


def _points(raw):
    """Return (points, problems) for a targetPos payload; never raises."""
    if not isinstance(raw, list):
        return [], ['targetPos_missing']
    problems = []
    points = []
    for item in raw:
        if not isinstance(item, dict) or type(item.get('x')) is not int or type(item.get('y')) is not int:
            problems.append('targetPos_schema')
            continue
        points.append(Pos(item['x'], item['y']))
    return points, problems


def review_command(turn, rid, cmd):
    """Format findings for one role command. Legal-but-odd shapes are reported too."""
    if not isinstance(cmd, dict):
        return [_finding('command_not_object', '命令不是对象', rid)]
    out = []
    action = cmd.get('action')
    if action not in KNOWN_ACTIONS:
        return [_finding('unknown_action', f'未知动作 {action!r}', rid)]
    unit = next((u for u in turn.ours if u.unit_id == rid and u.health > 0), None)
    if unit is None:
        out.append(_finding('unit_not_controllable', '单位不存在或已死亡', rid))
    points, problems = _points(cmd.get('targetPos', []))
    for problem in problems:
        out.append(_finding(problem, '目标坐标结构不合法', rid))
    if any(not 0 <= p.x < turn.width or not 0 <= p.y < turn.height for p in points):
        out.append(_finding('target_out_of_bounds', '目标坐标越界', rid))
    if action in POINT_ACTIONS and len(points) != 1:
        out.append(_finding('target_count', f'{action} 需要恰好 1 个目标点', rid))
    if action == 'attack':
        if not isinstance(cmd.get('controllerId'), (str, int)):
            out.append(_finding('missing_controller', 'attack 缺少 controllerId', rid))
        if unit is not None:
            expected = 1 if unit.kind == 'railgun' else max(1, unit.level)
            if len(points) != expected:
                out.append(_finding('attack_target_count',
                                    f'{unit.kind} 等级 {unit.level} 需要 {expected} 个瞄准点', rid))
            # Aim cells may point at empty ground: rockets splash, so this is not a finding.
    if action in ('buy', 'sell'):
        if not isinstance(cmd.get('name'), str) or not cmd['name']:
            out.append(_finding('missing_item_name', f'{action} 缺少物品名', rid))
        if type(cmd.get('num', 1)) is not int or cmd.get('num', 1) < 1:
            out.append(_finding('invalid_quantity', f'{action} 数量不是正整数', rid))
    if action == 'use' and not isinstance(cmd.get('name'), str):
        out.append(_finding('missing_item_name', 'use 缺少物品名', rid))
    if action == 'submitAnswer':
        answer = cmd.get('taskAnswer')
        if not isinstance(answer, str) or not 0 < len(answer) <= ANSWER_LIMIT:
            out.append(_finding('invalid_answer', 'submitAnswer 答案为空或超长', rid))
    for value in cmd.values():
        if isinstance(value, str) and len(value) > ANSWER_LIMIT:
            out.append(_finding('field_too_large', '命令字段超过长度上限', rid))
            break
    return out


def review_response(turn, response):
    """Envelope findings: key set, channel exclusivity, sizes, duplicate actors."""
    out = []
    if not isinstance(response, dict):
        return [_finding('response_not_object', '响应不是对象')]
    extra = [k for k in response if k not in RESPONSE_KEYS]
    if extra:
        out.append(_finding('unexpected_keys', f'响应出现协议外字段 {extra}'))
    commands = response.get('roleCommandMap')
    if not isinstance(commands, dict):
        out.append(_finding('command_map_type', 'roleCommandMap 不是对象'))
        commands = {}
    prompt = response.get('prompt', '')
    execute = response.get('executeCmd', '')
    if not isinstance(prompt, str) or not isinstance(execute, str):
        out.append(_finding('channel_type', 'prompt/executeCmd 必须是字符串'))
    else:
        if prompt and execute:
            out.append(_finding('channel_conflict', 'prompt 与 executeCmd 同时存在'))
        if len(prompt) > PROMPT_LIMIT:
            out.append(_finding('prompt_too_large', f'prompt 超过 {PROMPT_LIMIT} 字符'))
        if len(execute) > COMMAND_LIMIT:
            out.append(_finding('command_too_large', f'executeCmd 超过 {COMMAND_LIMIT} 字符'))
    controllers = {str(c.get('controllerId')) for c in commands.values()
                   if isinstance(c, dict) and c.get('action') == 'attack'}
    for key, cmd in commands.items():
        try:
            rid = int(key)
        except (TypeError, ValueError):
            out.append(_finding('non_numeric_role', f'roleCommandMap 键 {key!r} 不是单位 id'))
            continue
        if str(key) in controllers:
            out.append(_finding('controller_also_acted', f'操作 #{key} 同时有本体动作', rid))
        if not isinstance(cmd, dict) or cmd.get('action') != 'submitAnswer':
            continue
        if not turn.phase_task:
            out.append(_finding('answer_without_task', '无任务时提交答案', rid))
    return out


def review_action(raw_text, pending=None):
    """Format findings for a model reply before it becomes a command or an answer."""
    out = []
    if not isinstance(raw_text, str) or not raw_text.strip():
        return [_finding('empty_model_response', '模型没有返回内容')]
    text = raw_text.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        text = '\n'.join(lines[1:-1]) if lines and lines[-1].strip() == '```' else '\n'.join(lines[1:])
    if not text.lstrip().startswith(('{', '[')):
        # Plain final answers are part of the existing protocol; only note the shape.
        out.append(_finding('plain_text_action', '模型回复不是 JSON 对象', pending and pending.get('round')))
        return out
    try:
        value = json.loads(text)
    except ValueError:
        return [_finding('malformed_json', 'JSON 解析失败', pending and pending.get('round'))]
    if not isinstance(value, dict):
        out.append(_finding('action_not_object', 'JSON 顶层不是对象', pending and pending.get('round')))
        return out
    action_type = value.get('type')
    if action_type is not None and action_type not in ('command', 'final'):
        out.append(_finding('unknown_action_type', f'未知 type {action_type!r}'))
    has_command = isinstance(value.get('executeCmd'), str) and value['executeCmd'].strip()
    has_answer = value.get('taskAnswer') is not None
    if action_type == 'command' and not value.get('command'):
        out.append(_finding('missing_command', 'type=command 但缺少 command 字段'))
    if action_type == 'final' and not value.get('answer'):
        out.append(_finding('missing_final_answer', 'type=final 但缺少 answer 字段'))
    if has_command and has_answer:
        out.append(_finding('action_conflict', '同一响应同时给出 executeCmd 与 taskAnswer'))
    return out


def audit(turn, response, state=None, round_no=None):
    """Full audit used by the engine; findings are stored for logs and replay."""
    findings = review_response(turn, response)
    for key, cmd in (response.get('roleCommandMap') or {}).items():
        try:
            rid = int(key)
        except (TypeError, ValueError):
            continue
        findings.extend(review_command(turn, rid, cmd))
    if state is not None:
        payload = {'findings': findings, 'clean': not findings}
        blackboard.write(state, 'review', payload, round_no)
    return findings
