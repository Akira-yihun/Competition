from .model import *
from .navigation import route, STEPS
from .protocol import move_command

def _neighbours(pos):
    return tuple(Pos(pos.x+dx, pos.y+dy) for dx, dy in STEPS)

def _footprint_distance(pos, footprint):
    return min(distance(pos, cell) for cell in footprint)

def _cells_at_distance(pos, radius):
    footprint = station_footprint(pos)
    return tuple(Pos(x,y) for x in range(pos.x-radius, pos.x+2+radius)
                 for y in range(pos.y-1-radius, pos.y+1+radius)
                 if _footprint_distance(Pos(x,y), footprint) == radius)

def _walk(turn, role, target, reserved, commands):
    step, length = route(turn, role, _neighbours(target), reserved)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        reserved.add(step)
    return length
