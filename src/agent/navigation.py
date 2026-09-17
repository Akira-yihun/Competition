from collections import deque
from .protocol import Pos, Turn, Unit

STEPS = ((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1))

def route(turn: Turn, moving: Unit, goals, reserved=()):
    """Shortest eight-way route to any goal, including first-step reservations."""
    goals = set(goals)
    if moving.pos in goals:
        return None, 0
    blocked = turn.blocked(moving) | frozenset(reserved)
    goals = {p for p in goals if turn.land(p) and p not in blocked}
    if not goals:
        return None, 10**6
    steps = STEPS
    feedback = turn.raw.get("lastRoundRoleActionResults") or {}
    if feedback.get(str(moving.unit_id), feedback.get(moving.unit_id)) is False:
        # Break repeat contention with an unseen simultaneous mover using a
        # deterministic alternate shortest-path tie order on failed actions.
        steps = STEPS[::-1]
    queue = deque([(moving.pos, None, 0)])
    seen = {moving.pos}
    while queue:
        pos, first, length = queue.popleft()
        for dx, dy in steps:
            cell = Pos(pos.x + dx, pos.y + dy)
            if cell in seen or cell in blocked or not turn.land(cell):
                continue
            step = first or cell
            if cell in goals:
                return step, length + 1
            seen.add(cell)
            queue.append((cell, step, length + 1))
    return None, 10**6

def next_step(turn: Turn, moving: Unit, goal: Pos):
    return route(turn, moving, [goal])[0]
