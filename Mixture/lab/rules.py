from collections import Counter

def xy(obj):
    p = obj.get('pos', obj)
    return p['x'], p['y']

def pos(p):
    return {'x': p[0], 'y': p[1]}

def dist(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

def cells(role):
    x, y = xy(role)
    return {(x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1)} if role['roleType'] == 'station' else {(x, y)}

def resolve_moves(positions, proposals, static):
    """Resolve simultaneous destination contention, swaps and blocked chains."""
    bad = {i for i, p in proposals.items() if p in static or not (0 <= p[0] < 41 and 0 <= p[1] < 32) or dist(positions[i], p) != 1}
    counts = Counter(proposals.values())
    bad.update(i for i, p in proposals.items() if counts[p] > 1)
    owner = {p: i for i, p in positions.items()}
    for i, p in proposals.items():
        j = owner.get(p)
        if j is not None and proposals.get(j) == positions[i]:
            bad.update((i, j))
    while True:
        more = {i for i, p in proposals.items() if p in owner and (owner[p] not in proposals or owner[p] in bad)}
        if more <= bad:
            break
        bad |= more
    return {i: p for i, p in proposals.items() if i not in bad}

def on_line(origin, target, p):
    """Segment versus closed unit cell, with deterministic corner inclusion."""
    lo, hi = 0.0, 1.0
    for axis in (0, 1):
        delta = target[axis] - origin[axis]
        if delta == 0:
            if abs(origin[axis] - p[axis]) > .5:
                return False
        else:
            aa, bb = ((p[axis] - .5 - origin[axis]) / delta, (p[axis] + .5 - origin[axis]) / delta)
            lo, hi = max(lo, min(aa, bb)), min(hi, max(aa, bb))
    return lo <= hi
