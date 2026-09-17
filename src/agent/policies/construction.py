from ..model import *
from ..protocol import move_command, build_command, attack_command, sell_command
from ..navigation import route, STEPS
from ..world import _neighbours, _footprint_distance, _walk, _cells_at_distance

TOWER_LOADOUT = ("gatling", "railgun", "rocket")

def _tower_sites(turn):
    station = turn.station()
    if not station:
        return ()
    # Three separated sides leave distinct operator spaces and open exits.
    x, y = station.pos.x, station.pos.y
    facing = 1 if x < turn.width / 2 else -1
    front, back = (x+2, x-1) if facing == 1 else (x-1, x+2)
    preferred = ([Pos(front,y), Pos(x,y-2), Pos(back,y-1)] if facing==1
                 else [Pos(front,y-1), Pos(x+1,y+1), Pos(back,y)])
    others = sorted(_cells_at_distance(station.pos,1), key=lambda p:(-facing*(p.x-x),p.y))
    result = []
    for p in preferred + others:
        if p not in result and turn.land(p):
            result.append(p)
    return tuple(result[:3])
