from collections import deque
from .protocol import Pos, Turn, Unit
from .model import distance

STEPS = ((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1))

def route(turn: Turn, moving: Unit, goals, reserved=(), cautious=True):
    """Shortest eight-way route to any goal, including first-step reservations."""
    goals = set(goals)
    if cautious and night_caution(turn):goals={p for p in goals if safe_cell(turn,p)}
    if moving.pos in goals:
        return None, 0
    blocked = turn.blocked(moving) | frozenset(reserved) | frozenset(turn.navigation_avoid.get(moving.unit_id, ()))
    goals = {p for p in goals if turn.land(p) and p not in blocked}
    if not goals:
        return None, 10**6
    if cautious and night_caution(turn):
        limit=max(max(2,turn.width//3)+1,side_depth(turn,moving.pos))
        blocked |= frozenset(Pos(x,y) for x in range(turn.width) if min(x,turn.width-1-x)>=limit
                             for y in range(turn.height))
        blocked |= frozenset(Pos(x,y) for r in turn.robots if r.health>0
                             for x in range(max(0,r.pos.x-4),min(turn.width,r.pos.x+5))
                             for y in range(max(0,r.pos.y-4),min(turn.height,r.pos.y+5)))
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


def night_caution(turn):
    return not turn.is_day or (turn.round_no-1)%130>=60


def side_depth(turn,p):
    return min(p.x,turn.width-1-p.x)


def safe_cell(turn,p):
    return side_depth(turn,p)<=max(2,turn.width//3) and not any(
        r.health>0 and distance(r.pos,p)<=4 for r in turn.robots)


def evacuate(turn,role,reserved,commands):
    if not night_caution(turn) or safe_cell(turn,role.pos):return False
    goals=[Pos(x,y) for x in range(turn.width) for y in range(turn.height) if safe_cell(turn,Pos(x,y))]
    step,length=route(turn,role,goals,reserved)
    if step is None and length>=10**6:
        # If already inside robot range, take a legal step reducing exposure.
        def risk(p):
            return (sum(max(0,5-distance(r.pos,p)) for r in turn.robots if r.health>0),side_depth(turn,p))
        blocked=turn.blocked(role)|frozenset(reserved)
        options=[Pos(role.pos.x+dx,role.pos.y+dy) for dx,dy in STEPS]
        options=[p for p in options if turn.land(p) and p not in blocked and risk(p)<risk(role.pos)]
        if options:step=min(options,key=lambda p:(risk(p),p.x,p.y))
    if step is not None:
        from .protocol import move_command
        commands[role.unit_id]=move_command(step);reserved.add(step)
    return True
