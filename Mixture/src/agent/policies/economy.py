"""Defender's daily gather/service/home cycle and independent economic worker.

The day schedule stays here; the *what and why* comes from ``agents/defense_agent``
(situation report + ordered work plan + small stone reserve) and
``agents/economy_agent`` (survey, selling timing, robot-aware routing).
"""
from dataclasses import replace
from ..model import Pos, distance
from ..navigation import route
from ..protocol import build_command
from ..rules import SHOP_PRICES
from ..world import _neighbours
from ..objectives import goal
from ..agents import defense_agent, economy_agent
from .roles import assign
from .construction import (_tower_sites, TOWER_LOADOUT, wall_sites, priority_wall_sites,
                           wall_preserves_access, upgrade_order, operator_hub)
from . import mining


def _max_health(unit):
    return 1500*max(1,unit.level) if unit.kind=='station' else 500+500*max(1,unit.level)


def buildings(turn):
    primary=set(priority_wall_sites(turn))
    towers=sorted(turn.weapons(),key=lambda t:upgrade_order(turn,t))
    walls=sorted(turn.walls(),key=lambda w:(w.pos not in primary,w.health/_max_health(w),w.unit_id))
    return towers+([turn.station()] if turn.station() else [])+walls


def apply_held(turn,worker,reserved,commands):
    for target in buildings(turn):
        if target.kind=='station':continue  # Base vouchers remain emergency reserves.
        prefix='Wall' if target.kind=='wall' else 'Weapon'
        name=f'{prefix}UpgradeVoucher{target.level}'
        if target.kind=='wall' and target.health<_max_health(target)*.8 and 'WallFixer' in worker.backpack:
            name='WallFixer'
        if name not in worker.backpack or target.level>=3 and name!='WallFixer':continue
        if distance(worker.pos,target.pos)<=1:
            commands[worker.unit_id]={'action':'use','name':name,'targetPos':[target.pos.dump()]}
            return True
        if mining.move_to(turn,worker,target.pos,reserved,commands,cautious=False)<10**6:return True
    return False


def purchase_options(turn,worker,budget):
    prices={**SHOP_PRICES,**{i['name']:i['price'] for i in turn.raw.get('weaponShopList',[]) if isinstance(i,dict) and 'name' in i and 'price' in i}}
    free=(worker.capacity or 100)-len(worker.backpack)
    if free<=0:return []
    result=[];seen=set()
    ordered=buildings(turn);towers=turn.weapons();base=turn.station()
    # One weapon improvement, then reserve base healing; weapons precede walls.
    if towers and any(t.level>1 for t in towers) and base:ordered=[base]+[t for t in ordered if t!=base]
    for target in ordered:
        if not 1<=target.level<3:continue
        prefix='Station' if target.kind=='station' else 'Wall' if target.kind=='wall' else 'Weapon'
        name=f'{prefix}UpgradeVoucher{target.level}'
        if name in seen:continue
        seen.add(name)
        if any(name in r.backpack for r in turn.controllable()):continue
        price=prices.get(name)
        if type(price) is not int or not 0<price<=budget:continue
        count=1
        if prefix=='Weapon':
            count=min(free,budget//price,sum(t.level==target.level for t in towers))
        result.append((name,count,price))
    if worker.health<150 and 'Medicine' not in worker.backpack and budget>=prices['Medicine']:
        result.insert(0,('Medicine',1,prices['Medicine']))
    damaged=[w for w in turn.walls() if w.health<_max_health(w)*.8]
    if damaged and 'WallFixer' not in worker.backpack and budget>=prices['WallFixer']:
        result.append(('WallFixer',min(free,len(damaged),budget//prices['WallFixer']),prices['WallFixer']))
    return result


def _upgrade(turn,worker,budget,reserved,commands,claimed):
    """Compatibility helper: carried weapon/wall vouchers first, then affordable procurement."""
    if apply_held(turn,worker,reserved,commands):return budget,True
    shop=next((p for p,k in turn.zones.items() if k=='weaponShop'),None)
    options=purchase_options(turn,worker,budget)
    if not shop or not options:return budget,False
    name,count,price=options[0]
    if distance(worker.pos,shop)<=1:
        commands[worker.unit_id]={'action':'buy','name':name,'num':count}
        return budget-price*count,True
    return budget,mining.move_to(turn,worker,shop,reserved,commands)<10**6


def healing(turn,commands):
    for role in turn.controllable():
        maximum=200 if role.kind=='pioneer' else 220
        if role.health<maximum*.65 and 'Medicine' in role.backpack:
            commands[role.unit_id]={'action':'use','name':'Medicine'}
        elif 'WallFixer' in role.backpack:
            primary=set(priority_wall_sites(turn))
            walls=sorted(turn.walls(),key=lambda w:(w.pos not in primary,w.health/_max_health(w)))
            wall=next((w for w in walls if distance(role.pos,w.pos)<=1 and w.health<_max_health(w)*.5),None)
            if wall:commands[role.unit_id]={'action':'use','name':'WallFixer','targetPos':[wall.pos.dump()]}


def _construct(turn,worker,sites,name,state,reserved,commands):
    occupied=turn.occupied_cells()
    for site in sites:
        if site in occupied or site in reserved:continue
        if name=='wall' and not wall_preserves_access(turn,worker,site,reserved):continue
        if route(turn,worker,_neighbours(site),reserved,cautious=False)[1]>=10**6:continue
        if distance(worker.pos,site)==1:
            commands[worker.unit_id]=build_command(site,name);reserved.add(site)
        else:mining.move_to(turn,worker,site,reserved,commands,cautious=False)
        goal(state,turn,worker,'build_'+name,site,'按固定阵型施工，优先前侧墙和通行')
        return True
    return False


def maintenance_sites(turn):
    """Wall cells worth maintaining (delegated so one module owns the layout rule)."""
    return defense_agent.maintenance_sites(turn)


def _trip_cost(turn,worker,stops,hub,reserved):
    """Actual reachable stand-to-stand route estimates, including service actions."""
    here=worker;total=0
    for target in stops:
        stands=[p for p in _neighbours(target) if turn.land(p) and p not in reserved]
        options=[(route(turn,here,[p],reserved,cautious=False)[1],p) for p in stands]
        if not options:return 10**6
        length,stand=min(options,key=lambda item:(item[0],item[1].x,item[1].y))
        if length>=10**6:return length
        total+=length+2;here=replace(here,pos=stand)
    return total+route(turn,here,[hub],reserved,cautious=False)[1]


def plan_defender(turn,worker,state,reserved,commands):
    hub=operator_hub(turn)
    if not hub:return
    day=(turn.round_no-1)//130;phase=(turn.round_no-1)%130;left=70-phase
    cycle=state.setdefault('defender_cycle',{})
    if cycle.get('day')!=day:
        cycle.clear();cycle.update(day=day,phase='gather',serviced=False)
    if not turn.is_day:
        goal(state,turn,worker,'defend',hub,'夜间固定操作位等待冷却或目标')
        return
    # Situation report + ordered next-day plan (assessment, damage, economy, ETAs).
    plan=defense_agent.work_plan(turn,state,worker)
    reserve=plan['stone_reserve']
    switch=defense_agent.log_reason(state,plan)
    if switch['switched']:
        # Task switches are explicit: keep the previous job and the reason for review.
        state['defense_task_switch']=switch
        state.setdefault('defense_switch_history',[]).append({'round':turn.round_no,**switch})
        del state['defense_switch_history'][:-12]
    if turn.vendor() and distance(worker.pos,turn.vendor())<=1 and any(k in worker.backpack for k in ('iron','copper')):
        if mining.sell(turn,worker,state,reserved,commands,force=True,keep_stone=reserve):return
    if len(turn.weapons())<3 and turn.gold>=25:
        if _construct(turn,worker,_tower_sites(turn),'rocket',state,reserved,commands):return
    missing=[p for p in maintenance_sites(turn) if p not in turn.occupied_cells()]
    # Repair route: nearest-neighbour order over the walls that still need work, so the
    # worker does not zig-zag between opposite ends of the ring. Each ordering step is a
    # path search, so it is cached per day instead of recomputed every round.
    cache=state.get('defense_route')
    key=[p.dump() for p in sorted(missing,key=lambda p:(p.x,p.y))]
    if not isinstance(cache,dict) or cache.get('day')!=day or cache.get('sites')!=key:
        ordered,_=defense_agent.repair_order(turn,worker,missing,reserved)
        cache={'day':day,'sites':key,'order':[item['site'] for item in ordered]}
        state['defense_route']=cache
    planned=[Pos.load(item) for item in cache['order']]
    route_order=planned+[p for p in missing if p not in set(planned)]
    stones=worker.backpack.count('stone')
    return_length=route(turn,worker,[hub],reserved,cautious=False)[1]
    held=sum('UpgradeVoucher' in item and not item.startswith('Station') for item in worker.backpack)
    # Reserve time for the walk home, carried upgrades and construction. Never start
    # a trip merely because there are enough coins without checking its completion.
    # Stone beyond the reserve is cargo to sell, not stock to hoard.
    home_work=2*min(stones,len(missing))+held
    if left<=return_length+home_work+5:cycle['phase']='home'
    if cycle['phase']=='gather':
        options=purchase_options(turn,worker,max(0,turn.gold-25*max(0,3-len(turn.weapons()))))
        cargo=sum(worker.backpack.count(k) for k in ('iron','copper'))
        stock_ready=not missing or stones>=min(reserve,len(missing))
        if stock_ready and (stones or cargo>=20 or options):
            cycle['phase']='service' if options or cargo else 'home'
        elif missing and not stock_ready:
            if mining.collect(turn,worker,state,reserved,commands,stone=True,
                              latest_return=max(0,left-min(len(missing),reserve+5)-5)):return
            cycle['phase']='home' if stones else 'service'
        elif not worker.backpack_full:
            if mining.collect(turn,worker,state,reserved,commands,latest_return=max(0,left-12)):return
            cycle['phase']='service'
    if cycle['phase']=='service':
        shop=next((p for p,k in turn.zones.items() if k=='weaponShop'),None)
        vendor=turn.vendor()
        options=purchase_options(turn,worker,max(0,turn.gold-25*max(0,3-len(turn.weapons()))))
        # Surplus stone above the reserve goes to market with the ore.
        sale_cargo=sum(worker.backpack.count(k) for k in ('iron','copper'))+max(0,stones-reserve)
        destinations=([vendor] if sale_cargo and vendor else [])+([shop] if options and shop else [])
        travel=_trip_cost(turn,worker,destinations,hub,reserved)+home_work+5
        if cycle.get('serviced') or left<=travel:
            cycle['phase']='home'
        else:
            if sale_cargo and mining.sell(turn,worker,state,reserved,commands,force=True,keep_stone=reserve):return
            if options and shop:
                name,count,price=options[0]
                if distance(worker.pos,shop)<=1:
                    commands[worker.unit_id]={'action':'buy','name':name,'num':count}
                    goal(state,turn,worker,'procure',shop,f'集中采购，武器优先并保留基地救命券（{plan["headline"]}）',phase='service')
                    return
                if mining.move_to(turn,worker,shop,reserved,commands)<10**6:
                    goal(state,turn,worker,'procure',shop,'一次服务行程完成采购后回基地',phase='service')
                    return
            cycle['phase']='home';cycle['serviced']=True
    if cycle['phase']=='home':
        if apply_held(turn,worker,reserved,commands):
            goal(state,turn,worker,'upgrade',hub,'携带券回基地后立即升级；基地券留作救命',phase='home')
            return
        if stones and missing and _construct(turn,worker,route_order,'wall',state,reserved,commands):
            goal(state,turn,worker,'build_wall',route_order[0] if route_order else hub,
                 f'按最近邻修复路径补墙：{plan["headline"]}',phase='home')
            return
        if not missing and not purchase_options(turn,worker,turn.gold) and not cycle.get('serviced') and left>return_length+20:
            cycle['phase']='gather'
            if mining.collect(turn,worker,state,reserved,commands,latest_return=left-8):return
        # Only one maintenance/service circuit per day; use remaining time for a
        # genuinely local mine if it cannot delay night duty and work is complete.
        if not missing and left>12 and not held:
            options=mining.candidates(turn,worker,state,reserved=reserved)
            nearby=[o for o in options if o[1]<=3 and distance(o[0],hub)<=3]
            if nearby and not worker.backpack_full:
                saved=state.setdefault('mine_targets',{}).get(str(worker.unit_id))
                state['mine_targets'][str(worker.unit_id)]={'target':nearby[0][0].dump(),'ore':turn.zones[nearby[0][0]]}
                if mining.collect(turn,worker,state,reserved,commands,latest_return=left-5):return
                if saved:state['mine_targets'][str(worker.unit_id)]=saved
        step,length=route(turn,worker,[hub],reserved,cautious=False)
        if step is not None:
            from ..protocol import move_command
            commands[worker.unit_id]=move_command(step);reserved.add(step)
        goal(state,turn,worker,'defend_ready',hub,f'当天修缮结束或返岗截止，保留未完成工作到次日（{plan["headline"]}）',phase='home')


def plan(turn,recalled,reserved,commands,state=None):
    state=state if state is not None else {}
    defender,miner=assign(turn,state)
    for worker in turn.workers():
        if worker.unit_id in recalled or worker.unit_id in commands:continue
        if worker==defender:
            hub=operator_hub(turn)
            reserved.discard(hub)
            plan_defender(turn,worker,state,reserved,commands)
            if hub:reserved.add(hub)
        else:mining.plan_miner(turn,worker,state,reserved,commands)
