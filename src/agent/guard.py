"""Independent final checks; no reward optimization or side effects."""
from .model import Pos, distance
from .rules import TOWER_TYPES
from .protocol import empty_response
from .scheduler import Intent, arbitrate


def legal(rid, cmd, turn):
    unit=next((u for u in turn.ours if u.unit_id==rid and u.health>0),None)
    if unit is None or not isinstance(cmd,dict):
        return False
    action=cmd.get('action')
    targets=cmd.get('targetPos',[])
    if not isinstance(targets,list) or any(not isinstance(p,dict) or type(p.get('x')) is not int or type(p.get('y')) is not int for p in targets):
        return False
    points=[Pos.load(p) for p in targets]
    if any(not 0<=p.x<turn.width or not 0<=p.y<turn.height for p in points):
        return False
    if action in ('move','build','collect') and len(points)!=1:
        return False
    if action=='attack':
        controller=next((r for r in turn.controllable() if str(r.unit_id)==cmd.get('controllerId')),None)
        count=1 if unit.kind=='railgun' else max(1,unit.level)
        if turn.is_day or unit.kind not in TOWER_TYPES or unit.cooldown or controller is None or distance(controller.pos,unit.pos)>1 or len(points)!=count:
            return False
        if any(distance(unit.pos,p)>unit.range_of_attack() for p in points):
            return False
        if unit.kind=='gatling':
            vectors=[(p.x-unit.pos.x,p.y-unit.pos.y) for p in points]
            return all(v!=(0,0) for v in vectors) and all(a[0]*b[0]+a[1]*b[1]>=0 for a in vectors for b in vectors)
        return True
    if unit.kind not in ('worker','pioneer'):
        return False
    if action=='move':
        return distance(unit.pos,points[0])==1 and turn.land(points[0]) and points[0] not in turn.blocked(unit)
    if action=='build':
        station=turn.station();name=cmd.get('name');target=points[0]
        return bool(turn.is_day and unit.kind=='worker' and station and distance(unit.pos,target)==1 and turn.land(target) and target not in turn.blocked(unit) and name in TOWER_TYPES+('wall',) and min(distance(target,p) for p in turn.footprint(station))==(2 if name=='wall' else 1) and (name!='wall' or 'stone' in unit.backpack))
    if action=='collect':
        return unit.kind=='worker' and not unit.backpack_full and distance(unit.pos,points[0])<=1 and points[0] in turn.mines()
    if action in ('buy','sell'):
        quantity=cmd.get('num',1);name=cmd.get('name')
        shop='vendor' if action=='sell' else 'weaponShop'
        if not isinstance(name,str) or type(quantity) is not int or quantity<1 or not any(k==shop and distance(unit.pos,p)<=1 for p,k in turn.zones.items()):
            return False
        return unit.backpack.count(name)>=quantity if action=='sell' else unit.capacity is None or len(unit.backpack)+quantity<=unit.capacity
    if action=='use':
        name=cmd.get('name')
        if name=='Medicine':return name in unit.backpack and not points
        if len(points)!=1:return False
        target=next((u for u in turn.ours if u.health>0 and points[0] in turn.footprint(u)),None)
        name=cmd.get('name')
        if name not in unit.backpack or target is None or min(distance(unit.pos,p) for p in turn.footprint(target))>1:
            return False
        if name=='WallFixer':return target.kind=='wall'
        group='Station' if target.kind=='station' else 'Weapon' if target.kind in TOWER_TYPES else 'Wall'
        return target.level in (1,2) and name==f'{group}UpgradeVoucher{target.level}'
    if action=='acceptTask':
        return unit.kind=='pioneer' and not turn.phase_task and any(t.valid and distance(unit.pos,t.position)<=1 for t in turn.tasks)
    if action=='submitAnswer':
        return unit.kind=='pioneer' and bool(turn.phase_task) and isinstance(cmd.get('taskAnswer'),str) and 0<len(cmd['taskAnswer'])<=64000
    return False


def validate(response, turn):
    result=empty_response()
    intents=[]
    for key,cmd in response.get('roleCommandMap',{}).items():
        try:
            rid=int(key)
            if legal(rid,cmd,turn):
                intents.append(Intent(rid,cmd))
        except (KeyError,TypeError,ValueError):
            continue
    result['roleCommandMap']=arbitrate(intents,turn)
    active=bool(turn.phase_task and any(r.kind=='pioneer' for r in turn.controllable()))
    for key,limit in [('prompt',180000),('executeCmd',12000)]:
        value=response.get(key,'')
        if active and isinstance(value,str) and len(value)<=limit:
            result[key]=value
    if result['executeCmd']:
        result['prompt']=''
    return result
