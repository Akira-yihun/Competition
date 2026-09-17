from ..model import *
from ..protocol import move_command, build_command, attack_command, sell_command
from ..navigation import route, STEPS
from ..world import _neighbours, _footprint_distance, _walk, _cells_at_distance
from .construction import _tower_sites, TOWER_LOADOUT

def _upgrade(turn,worker,budget,reserved,commands,claimed_upgrades):
    station=turn.station()
    buildings=([station] if station else [])+sorted(turn.weapons(),key=lambda t:(t.kind!='rocket',t.level,t.unit_id))
    for target in buildings:
        if not 1<=target.level<3 or target.unit_id in claimed_upgrades:
            continue
        name=('Station' if target.kind=='station' else 'Weapon')+'UpgradeVoucher'+str(target.level)
        if name in worker.backpack:
            claimed_upgrades.add(target.unit_id)
            if min(distance(worker.pos,p) for p in turn.footprint(target))<=1:
                commands[worker.unit_id]={'action':'use','name':name,'targetPos':[target.pos.dump()]}
            else:
                _walk(turn,worker,target.pos,reserved,commands)
            return budget,True
    shop=next((p for p,k in turn.zones.items() if k=='weaponShop'),None)
    if not shop or worker.backpack_full:
        return budget,False
    prices={i['name']:i['price'] for i in turn.raw.get('weaponShopList',[]) if isinstance(i,dict) and 'name'in i and 'price'in i}
    for target in buildings:
        if not 1<=target.level<3 or target.unit_id in claimed_upgrades:
            continue
        name=('Station' if target.kind=='station' else 'Weapon')+'UpgradeVoucher'+str(target.level)
        # Voucher is generic. Avoid buying duplicate tickets already in transit.
        if any(name in r.backpack for r in turn.controllable()):
            continue
        price=prices.get(name,100 if target.level==1 else 150)
        if not isinstance(price,int) or price<=0 or budget<price:
            continue
        claimed_upgrades.add(target.unit_id)
        if distance(worker.pos,shop)<=1:
            commands[worker.unit_id]={'action':'buy','name':name,'num':1}
            return budget-price,True
        _walk(turn,worker,shop,reserved,commands)
        return budget,True
    return budget,False

def plan(turn, recalled, reserved, commands):
    payload = turn.raw
    sites=_tower_sites(turn)
    occupied=turn.occupied_cells()
    planned=set()
    upgrades=set()
    budget=turn.gold
    count=len(turn.weapons())
    for worker in turn.workers():
        if worker.unit_id in recalled:
            continue
        free=[(i,p) for i,p in enumerate(sites) if p not in occupied and p not in reserved and p not in planned]
        if free and count<3 and budget>=25:
            i,site=min(free,key=lambda ip:(distance(worker.pos,ip[1]),ip[0]))
            planned.add(site)
            reserved.add(site)
            count+=1
            # Reserve even while travelling, preventing duplicate construction plans.
            budget-=25
            if distance(worker.pos,site)<=1:
                commands[worker.unit_id]=build_command(site,TOWER_LOADOUT[i])
            else:
                _walk(turn,worker,site,reserved,commands)
            continue
        budget,upgrading=_upgrade(turn,worker,budget,reserved,commands,upgrades)
        if upgrading:
            continue
        vendor=turn.vendor()
        prices={i['name']:i['price'] for i in payload.get('vendorShopList',[]) if isinstance(i,dict) and 'name'in i and 'price'in i}
        minerals=[(name,worker.backpack.count(name)) for name in ('copper','iron','stone')]
        name,quantity=max(minerals,key=lambda nc:nc[1]*max(1,prices.get(nc[0],1)))
        if vendor and quantity and (quantity>=8 or worker.backpack_full or distance(worker.pos,vendor)<=1):
            if distance(worker.pos,vendor)<=1:
                commands[worker.unit_id]=sell_command(name,quantity)
            else:
                _walk(turn,worker,vendor,reserved,commands)
            continue
        if not worker.backpack_full:
            mines=sorted(turn.mines(),key=lambda p: (-(max(1,prices.get(turn.zones[p],1)))/(8+distance(worker.pos,p)+(distance(p,vendor) if vendor else 20)),p.x,p.y))
            for mine in mines:
                if distance(worker.pos,mine)<=1:
                    commands[worker.unit_id]={'action':'collect','targetPos':[mine.dump()]}
                    break
                if _walk(turn,worker,mine,reserved,commands)<10**6:
                    break
