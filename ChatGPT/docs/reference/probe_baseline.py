#!/usr/bin/env python3
"""Read-only local evidence probe, not a game simulator or platform validator."""
import json
import math
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[3]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / 'src'))
from agent.protocol import Turn
from agent.brain import decide, _attack_targets

payload = json.loads((PROJECT.parents[1] / 'docs/request.txt').read_text())
response = decide(payload)
print(json.dumps({'probe': 'official_request_smoke', 'response': response}, ensure_ascii=False))
payload['robot'] = {'roles': [
    {'id': i, 'pos': {'x': x, 'y': y}, 'health': 40}
    for i, x, y in [(1, 11, 10), (2, 11, 13), (3, 11, 7)]
]}
payload['teamOur']['roles'] = [{
    'id': 10020, 'pos': {'x': 10, 'y': 10}, 'roleType': 'gatling',
    'health': 2000, 'level': 3, 'attackRange': 7,
}]
turn = Turn.load(payload)
targets = _attack_targets(turn, turn.weapons()[0])
angles = []
for i, a in enumerate(targets):
    for b in targets[i + 1:]:
        u, v = (a.x - 10, a.y - 10), (b.x - 10, b.y - 10)
        denom = math.hypot(*u) * math.hypot(*v)
        if denom:
            cosine = max(-1, min(1, sum(x*y for x, y in zip(u, v)) / denom))
            angles.append(math.degrees(math.acos(cosine)))
print(json.dumps({'probe': 'gatling_axis_cone', 'targets': [p.dump() for p in targets],
    'max_pair_angle_deg': max(angles, default=0),
    'rule_violation_observed': any(a > 90.000001 for a in angles)}, ensure_ascii=False))
