"""Opening construction, stone supply, short sale trips and useful spending."""
from ..model import Pos, distance
from ..protocol import move_command, build_command, sell_command
from ..navigation import route, night_caution, safe_cell
from ..world import _neighbours, _walk
from ..rules import SHOP_PRICES
from ..intelligence.news import should_hold, mine_value
from .roles import assign
from .construction import _tower_sites, TOWER_LOADOUT, wall_sites, wall_preserves_access, upgrade_order


def _max_health(unit):
    return 1500*max(1,unit.level) if unit.kind=='station' else 500+500*max(1,unit.level)


def _upgrade(turn,worker,budget,reserved,commands,claimed):
    station=turn.station()
    towers=sorted(turn.weapons(),key=lambda t:upgrade_order(turn,t))
    walls=sorted(turn.walls(),key=lambda w:(w.health/_max_health(w),w.unit_id))
    buildings=towers+([station] if station else [])+walls
    for target in buildings:
        if not 1<=target.level<3 or target.unit_id in claimed:continue
        prefix='Station' if target.kind=='station' else 'Wall' if target.kind=='wall' else 'Weapon'
        name=f'{prefix}UpgradeVoucher{target.level}'
        if target.kind=='station':continue  # Reserved for emergency_upgrade.
        if name in worker.backpack:
            claimed.add(target.unit_id)
            if min(distance(worker.pos,p) for p in turn.footprint(target))<=1:
                commands[worker.unit_id]={'action':'use','name':name,'targetPos':[target.pos.dump()]}
            else:_walk(turn,worker,target.pos,reserved,commands)
            return budget,True
    shop=next((p for p,k in turn.zones.items() if k=='weaponShop'),None)
    if not shop or worker.backpack_full:return budget,False
    prices={**SHOP_PRICES,**{i['name']:i['price'] for i in turn.raw.get('weaponShopList',[]) if isinstance(i,dict) and 'name'in i and 'price'in i}}
    if worker.health<132 and 'Medicine' not in worker.backpack and budget>=prices['Medicine']:
        if distance(worker.pos,shop)<=1:
            commands[worker.unit_id]={'action':'buy','name':'Medicine','num':1}
            return budget-prices['Medicine'],True
        _walk(turn,worker,shop,reserved,commands);return budget,True
    buying=([towers[0]] if towers and all(t.level==1 for t in towers) else [])+([station] if station else [])+towers+walls
    for target in buying:
        if not 1<=target.level<3 or target.unit_id in claimed:continue
        prefix='Station' if target.kind=='station' else 'Wall' if target.kind=='wall' else 'Weapon'
        name=f'{prefix}UpgradeVoucher{target.level}'
        if any(name in r.backpack for r in turn.controllable()):continue
        # Preserve savings for the first rocket upgrade rather than consuming
        # every spare 20 gold on a healthy wall.
        if target.kind=='wall' and target.health>.65*_max_health(target) and any(t.kind=='rocket' and t.level==1 for t in towers):continue
        price=prices.get(name)
        if type(price) is not int or price<=0:continue
        if budget<price:return budget,False
        claimed.add(target.unit_id)
        if distance(worker.pos,shop)<=1:
            commands[worker.unit_id]={'action':'buy','name':name,'num':1}
            return budget-price,True
        _walk(turn,worker,shop,reserved,commands);return budget,True
    return budget,False


def healing(turn,commands):
    for role in turn.controllable():
        maximum=200 if role.kind=='pioneer' else 220
        if role.health<maximum*.65 and 'Medicine' in role.backpack:
            commands[role.unit_id]={'action':'use','name':'Medicine'}
        elif 'WallFixer' in role.backpack:
            wall=next((w for w in turn.walls() if distance(role.pos,w.pos)<=1 and w.health<_max_health(w)*.5),None)
            if wall:commands[role.unit_id]={'action':'use','name':'WallFixer','targetPos':[wall.pos.dump()]}


def plan(turn,recalled,reserved,commands,state=None):
    sites=_tower_sites(turn);occupied=turn.occupied_cells();planned=set();upgrades=set()
    budget=turn.gold;count=len(turn.weapons());walls=wall_sites(turn)
    free_walls=[p for p in walls if p not in occupied]
    state=state if state is not None else {}
    jobs=state.setdefault('economy_jobs',{})
    workers=turn.workers();defender,miner=assign(turn,state)
    first_day=turn.round_no<=130
    # Ensure rocket is the first actual tower, not simply the closest planned site.
    opening=not turn.weapons()
    for worker in workers:
        if worker.unit_id in recalled or worker.unit_id in commands:continue
        free=[(i,p) for i,p in enumerate(sites) if p not in occupied and p not in planned and p not in reserved ]
        if turn.is_day and free and count<3 and budget>=25 and (first_day or worker==defender):
            options=[(i,p,route(turn,worker,_neighbours(p),reserved)[1]) for i,p in free]
            options=[o for o in options if o[2]<10**6]
            if options:
                i,site,_=min(options,key=lambda v:(v[0],v[2]))
                planned.add(site);reserved.add(site);count+=1;budget-=25
                jobs[str(worker.unit_id)]={'kind':'build','target':site.dump(),'name':TOWER_LOADOUT[i]}
                if distance(worker.pos,site)==1:commands[worker.unit_id]=build_command(site,TOWER_LOADOUT[i])
                else:_walk(turn,worker,site,reserved,commands)
                continue
        if opening and turn.is_day:
            # The second worker stages at the next corner instead of leaving
            # for a distant mine before the rocket has been confirmed.
            if len(sites)>1:_walk(turn,worker,sites[1],reserved,commands)
            continue
        # Until all three towers exist, do not spend their initial 75 gold on items.
        vendor=turn.vendor();prices={i['name']:i['price'] for i in turn.raw.get('vendorShopList',[]) if isinstance(i,dict) and 'name'in i and 'price'in i}
        minerals=[(name,worker.backpack.count(name)) for name in ('copper','iron','stone')]
        sellable=[(name,n) for name,n in minerals if n and (name!='stone' or not free_walls or not first_day and worker==miner) and (not should_hold(state,name,turn.round_no) or len(turn.weapons())<3 and turn.gold<25)]
        previous=jobs.get(str(worker.unit_id),{})
        if sellable and vendor:
            name,quantity=max(sellable,key=lambda nc:nc[1]*max(1,prices.get(nc[0],1)))
            if worker.backpack_full or distance(worker.pos,vendor)<=1 or previous.get('kind')=='sell' or (previous.get('kind') in ('mine','stone') and Pos.load(previous['target']) not in turn.mines()):
                jobs[str(worker.unit_id)]={'kind':'sell','target':vendor.dump()}
                if distance(worker.pos,vendor)<=1:commands[worker.unit_id]=sell_command(name,quantity)
                else:_walk(turn,worker,vendor,reserved,commands)
                continue
        if turn.is_day and worker==defender and len(turn.weapons())==3:
            budget,done=_upgrade(turn,worker,budget,reserved,commands,upgrades)
            if done:continue
        gathering_batch=previous.get('kind')=='stone' and Pos.load(previous['target']) in turn.stone_mines() and not worker.backpack_full
        builder=first_day or worker==defender
        if turn.is_day and builder and free_walls and 'stone' in worker.backpack and len(turn.weapons())==3 and not gathering_batch:
            front=turn.station().pos.x+(3 if turn.station().pos.x<turn.width/2 else -2)
            ordered_walls=sorted(free_walls,key=lambda p:(p.x!=front,route(turn,worker,_neighbours(p),reserved)[1],walls.index(p)))
            for site in ordered_walls:
                if site in planned or site in reserved:continue
                if not wall_preserves_access(turn,worker,site,reserved):continue
                if route(turn,worker,_neighbours(site),reserved)[1]>=10**6:continue
                planned.add(site);reserved.add(site)
                jobs[str(worker.unit_id)]={'kind':'wall','target':site.dump()}
                if distance(worker.pos,site)==1:commands[worker.unit_id]=build_command(site,'wall')
                else:_walk(turn,worker,site,reserved,commands)
                break
            if worker.unit_id in commands:continue
        if worker.backpack_full:
            if vendor and any(should_hold(state,name,turn.round_no) for name,n in minerals if n):
                jobs[str(worker.unit_id)]={'kind':'hold_for_price','until':min(f['holdUntil'] for f in state['intelligence']['market'] if f.get('holdUntil') and f['holdUntil']>turn.round_no)}
                _walk(turn,worker,vendor,reserved,commands)
            continue
        # One worker stocks a useful batch of stone; the other maintains cash flow.
        stone_job=bool(free_walls and builder and turn.stone_mines() and turn.is_day)
        mines=turn.stone_mines() if stone_job else turn.mines()
        mines=[p for p in mines if not night_caution(turn) or safe_cell(turn,p)]
        mines=[p for p in mines if mine_value(state,turn.zones[p],turn.round_no,max(1,prices.get(turn.zones[p],1)))>0]
        if stone_job:
            other_targets={tuple(j['target'].values()) for rid,j in jobs.items() if rid!=str(worker.unit_id) and j.get('kind')=='stone' and isinstance(j.get('target'),dict)}
            locked=state.get('mine_targets',{}).get(str(worker.unit_id),{}).get('target')
            independent=[p for p in mines if (p.x,p.y) not in other_targets or p.dump()==locked]
            mines=independent or mines
        # Score actual walking time, sale distance and current/forecast prices.
        paths={p:route(turn,worker,_neighbours(p),reserved)[1] for p in mines}
        ordered=sorted((p for p in mines if paths[p]<10**6),key=lambda p:(
            -(1 if stone_job else mine_value(state,turn.zones[p],turn.round_no,max(1,prices.get(turn.zones[p],1))))
            /(2+2*paths[p]+(distance(p,vendor) if vendor else 20)),p.x,p.y))
        lock=state.setdefault('mine_targets',{}).get(str(worker.unit_id))
        if lock:
            old=Pos.load(lock['target'])
            if old in ordered and turn.zones.get(old)==lock.get('ore'):
                ordered.remove(old);ordered.insert(0,old)
            else:state['mine_targets'].pop(str(worker.unit_id),None)
        # Keep the target across selling trips until it disappears or becomes unsafe.
        for mine in ordered:
            state.setdefault('mine_targets',{})[str(worker.unit_id)]={'target':mine.dump(),'ore':turn.zones[mine]}
            if distance(worker.pos,mine)<=1:
                commands[worker.unit_id]={'action':'collect','targetPos':[mine.dump()]}
                jobs[str(worker.unit_id)]={'kind':'stone' if stone_job else 'mine','target':mine.dump()}
                break
            if _walk(turn,worker,mine,reserved,commands)<10**6:
                jobs[str(worker.unit_id)]={'kind':'stone' if stone_job else 'mine','target':mine.dump()}
                break
    if state is not None:state['economy_jobs']=jobs
