"""Shared blackboard: every role agent publishes a bounded structured summary.

This is the data plumbing the deferred global observer (docs/design/12 §3) will
read through ``snapshot``. Rules:

* writers only touch the ``blackboard`` key of the passed session state;
* payloads are JSON-serializable and truncated with an explicit marker;
* the latest entry per section is kept, plus a short section order for logging.

Nothing here performs platform calls, model calls or filesystem writes.
"""
import json

# Sections published by the role agents. The order doubles as log order.
SECTIONS = ('defense', 'defense_plan', 'attack', 'economy', 'economy_plan', 'task',
            'self_evolve', 'review')
CHAR_LIMIT = 6000


def _board(state):
    """Return the blackboard mapping, creating it on first use."""
    board = state.get('blackboard')
    if not isinstance(board, dict):
        board = {'sections': {}}
        state['blackboard'] = board
    board.setdefault('sections', {})
    return board


def write(state, section, payload, round_no=None):
    """Publish one agent summary for this round and return the stored entry."""
    board = _board(state)
    text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)
    if len(text) > CHAR_LIMIT:
        # Keep the summary reviewable instead of letting one section dominate state.
        payload = {'truncated': True, 'characters': len(text), 'excerpt': text[:CHAR_LIMIT]}
    entry = {'round': int(round_no if round_no is not None else state.get('last_round', 0) + 1),
             'data': payload}
    board['sections'][section] = entry
    order = [name for name in board.get('order', []) if name != section]
    order.append(section)
    board['order'] = order[-len(SECTIONS):]
    return entry


def read(state, section):
    """Latest published payload for a section, or None when it never wrote."""
    entry = _board(state)['sections'].get(section)
    return entry.get('data') if isinstance(entry, dict) else None


def snapshot(state):
    """Whole board for the future observer: {section: {'round': n, 'data': ...}}."""
    sections = _board(state)['sections']
    return {name: entry for name, entry in sections.items() if isinstance(entry, dict)}


def log_summary(state, limit=1200):
    """Compact one-line-per-section view used by the telemetry log line."""
    result = {}
    for name, entry in snapshot(state).items():
        text = json.dumps(entry.get('data'), ensure_ascii=False, separators=(',', ':'), default=str)
        result[name] = {'round': entry.get('round'), 'summary': text[:limit]}
    return result
