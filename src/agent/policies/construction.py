"""Corner towers, enemy-facing U walls and an open rear entrance."""
from ..model import Pos, distance
from ..navigation import route
from ..world import _neighbours, _cells_at_distance

TOWER_LOADOUT = ('rocket', 'gatling', 'railgun')


def facing(turn):
    station=turn.station()
    return 1 if station and station.pos.x < turn.width/2 else -1


def _tower_sites(turn):
    station=turn.station()
    if not station:return ()
    x,y=station.pos.x,station.pos.y
    # Rotate the whole layout, preserving clear cardinal corridors around base.
    corners=([Pos(x+2,y-2),Pos(x+2,y+1),Pos(x-1,y+1),Pos(x-1,y-2)]
             if facing(turn)==1 else
             [Pos(x-1,y+1),Pos(x-1,y-2),Pos(x+2,y-2),Pos(x+2,y+1)])
    return tuple(p for p in corners if turn.land(p))[:3]


def wall_sites(turn):
    station=turn.station()
    if not station:return ()
    x,y=station.pos.x,station.pos.y;direction=facing(turn)
    front=x+3 if direction==1 else x-2
    # Front first, then top/bottom from front to rear. Rear middle four stay open.
    front_cells=[Pos(front, yy) for yy in range(y-3,y+3)]
    front_cells.sort(key=lambda p:(abs(p.y-(y-.5)),p.y))
    horizontals=[Pos(xx,yy) for xx in range(x-2,x+4) for yy in (y-3,y+2) if xx!=front]
    horizontals.sort(key=lambda p:(abs(p.x-front),p.y))
    return tuple(p for p in front_cells+horizontals if turn.land(p))


def operator_stands(turn,tower):
    station=turn.station()
    candidates=[p for p in _neighbours(tower.pos) if turn.land(p)]
    if not station:return tuple(candidates)
    ring=set(_cells_at_distance(station.pos,1))
    inside=[p for p in candidates if p in ring]
    return tuple(inside or candidates)


def wall_preserves_access(turn,worker,site,reserved):
    """Do not seal the builder outside or erase a tower's only operator cell."""
    extra=set(reserved)|{site}
    for tower in turn.weapons():
        goals=[p for p in operator_stands(turn,tower) if p not in extra]
        if not goals or route(turn,worker,goals,extra)[1]>=10**6:
            return False
    return True
