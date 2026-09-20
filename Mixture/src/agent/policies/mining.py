"""Local mineral targets, sticky claims, and safe batched sales.

Planning (sell vs collect, price/funding reasoning, robot-aware routing) lives in
``agents/economy_agent``; this module keeps the mine lock, value ranking and the
actual collect/sell commands.
"""
from dataclasses import replace
from ..model import Pos, distance
from ..navigation import route, night_caution, safe_cell
from ..world import _neighbours
from ..protocol import move_command, sell_command
from ..objectives import goal
from ..agents import economy_agent
from ..intelligence.news import mine_value, should_hold

SURVEY_INTERVAL = 5  # mine survey costs one path per mine, so refresh it periodically


def local(turn, p):
    base=turn.station()
    if not base:return True
    # Never travel into the opposing base's corner for marginal ore prices.
    same_side=(p.x<=turn.width//2) if base.pos.x<turn.width/2 else (p.x>=turn.width//2)
    return same_side and distance(base.pos,p)<=max(turn.width,turn.height)//2


def behind(turn,p):
    base=turn.station()
    return bool(base and (p.x<=base.pos.x if base.pos.x<turn.width/2 else p.x>=base.pos.x+1))


def move_to(turn, worker, target, reserved, commands, cautious=True):
    """Robot-aware step: avoid the 3-cell attack ring, fall back to a plain route."""
    step,length=economy_agent.route_to(turn,worker,target,reserved,cautious=cautious)
    if step is not None:
        commands[worker.unit_id]=move_command(step);reserved.add(step)
    return length


def candidates(turn,worker,state,stone=False,reserved=()):
    prices={i['name']:i['price'] for i in turn.raw.get('vendorShopList',[]) if isinstance(i,dict) and 'name' in i and 'price' in i}
    mines=turn.stone_mines() if stone else turn.mines()
    mines=[p for p in mines if local(turn,p) and (not night_caution(turn) or safe_cell(turn,p))
           and mine_value(state,turn.zones[p],turn.round_no,max(1,prices.get(turn.zones[p],1)))>0]
    options=[];base=turn.station();vendor=turn.vendor()
    for p in mines:
        length=route(turn,worker,_neighbours(p),reserved)[1]
        if length>=10**6:continue
        home=distance(base.pos,p) if base else 0
        batch=min(10,(worker.capacity or 100)-len(worker.backpack))
        price=1 if stone else mine_value(state,turn.zones[p],turn.round_no,max(1,prices.get(turn.zones[p],1)))
        value=price*max(1,batch)/(max(1,batch)+2*length+2*home+.5*(distance(p,vendor) if vendor else home)+2)
        options.append((p,length,home,value))
    if not options:return []
    # Near BOTH worker and base before price ranking. Expand only if no nearby mine.
    for radius in (10,16,max(turn.width,turn.height)//2):
        nearby=[o for o in options if o[1]<=radius+2 and o[2]<=radius]
        if nearby:options=nearby;break
    if not turn.is_day:
        rear=[o for o in options if behind(turn,o[0])]
        if rear:options=rear
    return sorted(options,key=lambda o:(-o[3],o[1]+o[2],o[0].x,o[0].y))


def collect(turn,worker,state,reserved,commands,stone=False,latest_return=None):
    key=str(worker.unit_id);locks=state.setdefault('mine_targets',{})
    lock=locks.get(key);choices=[]
    if lock:
        p=Pos.load(lock['target'])
        eligible=p in turn.mines() and turn.zones.get(p)==lock.get('ore') and (not stone or turn.zones[p]=='stone')
        price=next((i['price'] for i in turn.raw.get('vendorShopList',[]) if i.get('name')==turn.zones.get(p)),1)
        eligible=eligible and local(turn,p) and (not night_caution(turn) or safe_cell(turn,p)) and mine_value(state,turn.zones.get(p,''),turn.round_no,max(1,price))>0
        if eligible:
            length=route(turn,worker,_neighbours(p),reserved)[1]
            if length<10**6:
                choices=[(p,length,0,0)];lock.pop('unreachable_since',None)
            else:
                first=lock.setdefault('unreachable_since',turn.round_no)
                if turn.round_no-first<3:
                    goal(state,turn,worker,'mine_wait',p,'矿点暂时不可达，短暂等待并保留目标')
                    return True
        if not choices:locks.pop(key,None)
    if not choices:choices=candidates(turn,worker,state,stone,reserved)
    for p,length,home,value in choices:
        if latest_return is not None:
            from .construction import operator_hub
            hub=operator_hub(turn)
            if hub:
                stands=[q for q in _neighbours(p) if turn.land(q)]
                home=min((route(turn,replace(worker,pos=q),[hub],set(reserved)-{hub},cautious=False)[1] for q in stands),default=10**6)
                if length+1+home+3>latest_return:continue
        locks[key]={'target':p.dump(),'ore':turn.zones[p],'since':lock.get('since',turn.round_no) if lock and lock.get('target')==p.dump() else turn.round_no}
        if distance(worker.pos,p)<=1:
            commands[worker.unit_id]={'action':'collect','targetPos':[p.dump()]}
        else:move_to(turn,worker,p,reserved,commands)
        state.setdefault('economy_jobs',{})[key]={'kind':'stone' if stone else 'mine','target':p.dump()}
        goal(state,turn,worker,'collect_stone' if stone else 'mine',p,'持续采集当前矿点，耗尽或更高优先级工作才切换')
        return True
    return False


def sell(turn,worker,state,reserved,commands,force=False,keep_stone=0):
    vendor=turn.vendor()
    if not vendor:return False
    prices={i['name']:i['price'] for i in turn.raw.get('vendorShopList',[]) if isinstance(i,dict) and 'name' in i and 'price' in i}
    ores=[(name,max(0,worker.backpack.count(name)-(keep_stone if name=='stone' else 0))) for name in ('copper','iron','stone')]
    ores=[(name,n) for name,n in ores if n>0 and (force or not should_hold(state,name,turn.round_no))]
    if not ores:return False
    lock=state.get('mine_targets',{}).get(str(worker.unit_id),{})
    exhausted=lock and Pos.load(lock['target']) not in turn.mines()
    job=state.setdefault('economy_jobs',{}).get(str(worker.unit_id),{})
    total=sum(n for _,n in ores)
    # Ten-ore depletion alone does not force a cross-map sale trip.
    if not (force or worker.backpack_full or total>=30 or distance(worker.pos,vendor)<=1
            or job.get('kind')=='sell' or exhausted and not candidates(turn,worker,state,reserved=reserved)):
        return False
    name,n=max(ores,key=lambda item:item[1]*prices.get(item[0],1))
    if distance(worker.pos,vendor)<=1:commands[worker.unit_id]=sell_command(name,n)
    elif move_to(turn,worker,vendor,reserved,commands)>=10**6:return False
    state['economy_jobs'][str(worker.unit_id)]={'kind':'sell','target':vendor.dump()}
    goal(state,turn,worker,'sell',vendor,'集中变现库存，为下一轮采集和采购腾出空间')
    return True


def plan_miner(turn,worker,state,reserved,commands):
    # Economy agent: mine/price/position survey (periodic) plus the sell-or-collect call.
    survey=state.get('economy_survey') or {}
    if survey.get('round',0)<=turn.round_no-SURVEY_INTERVAL:
        economy_agent.survey(turn,state,worker)
    decision=economy_agent.plan(turn,worker,state,reserved)
    # Funds needed for the first weapon/base vouchers justify an early sale; the
    # economy agent reports that as force=True with reason defense_funding.
    if decision['kind']=='sell':
        if sell(turn,worker,state,reserved,commands,force=decision.get('force',False),
                keep_stone=decision.get('keep_stone',0)):return
    if decision['kind'] in ('sell','collect') and not worker.backpack_full:
        if collect(turn,worker,state,reserved,commands):return
    goal(state,turn,worker,'safe_wait',worker.pos,
         f"满包等待商路开放或暂时没有可达的本地安全矿（经济判断：{decision['reason']}）")
