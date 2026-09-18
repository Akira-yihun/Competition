"""Corner towers, enemy-facing U walls and an open rear entrance."""
from ..model import Pos, distance
from ..navigation import route
from ..world import _neighbours, _cells_at_distance

TOWER_LOADOUT = ('rocket', 'rocket', 'rocket')


def facing(turn):
    station=turn.station()
    return 1 if station and station.pos.x < turn.width/2 else -1


def operator_hub(turn):
    station=turn.station()
    if not station:return None
    x,y=station.pos.x,station.pos.y
    return Pos(x-2,y-1) if facing(turn)==1 else Pos(x+3,y)


def _tower_sites(turn):
    station=turn.station()
    if not station:return ()
    x,y=station.pos.x,station.pos.y
    # Three legal inner-ring cells all within one cell of the rear operator hub.
    sites=([Pos(x-1,y-2),Pos(x-1,y-1),Pos(x-1,y)] if facing(turn)==1 else
           [Pos(x+2,y+1),Pos(x+2,y),Pos(x+2,y-1)])
    return tuple(p for p in sites if turn.land(p))


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
    hub=operator_hub(turn)
    if hub and turn.land(hub) and distance(hub,tower.pos)<=1:return (hub,)
    return tuple(p for p in _neighbours(tower.pos) if turn.land(p))


def wall_preserves_access(turn,worker,site,reserved):
    hub=operator_hub(turn)
    if hub is None or site==hub:return False
    # The wall must leave a geometric route; moving teammates do not seal a wall.
    from dataclasses import replace
    mobile_ids={r.unit_id for r in turn.controllable() if r.unit_id!=worker.unit_id}
    static_turn=replace(turn,ours=tuple(r for r in turn.ours if r.unit_id not in mobile_ids))
    return route(static_turn,worker,[hub],(set(reserved)-{hub})|{site})[1]<10**6
