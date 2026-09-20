"""Pioneer buys exact offerings and visits only evidence-backed treasure windows."""
from collections import Counter
from ..model import Pos,distance
from ..world import _walk
from ..navigation import route, night_caution, safe_cell
from ..world import _neighbours
from ..intelligence.news import memory


def plan(turn,pioneer,state,reserved,commands):
    mem=memory(state);t=mem.get('treasure')
    if not t or mem.get('treasure_closed') or turn.phase_task or pioneer is None:return False
    if turn.round_no>t['endRound']:mem['treasure']=None;return False
    target=Pos.load(t['position'])
    if night_caution(turn) and not safe_cell(turn,target):return False
    need=Counter(t['items'])-Counter(pioneer.backpack)
    if need:
        shop=next((p for p,k in turn.zones.items() if k=='weaponShop'),None)
        prices={i['name']:i['price'] for i in turn.raw.get('weaponShopList',[]) if isinstance(i,dict) and 'name' in i and 'price' in i}
        name=next(iter(need));price=prices.get(name)
        if shop is None or type(price) is not int or price<=0 or turn.gold<price or pioneer.backpack_full:return False
        if distance(pioneer.pos,shop)<=1:commands[pioneer.unit_id]={'action':'buy','name':name,'num':1}
        else:_walk(turn,pioneer,shop,reserved,commands)
        return pioneer.unit_id in commands
    _,length=route(turn,pioneer,_neighbours(target),reserved)
    if length>=10**6:return False
    if turn.round_no+length+1<t['startRound']-10:return False
    if distance(pioneer.pos,target)<=1:
        if turn.round_no>=t['startRound']:
            commands[pioneer.unit_id]={'action':'summonTreasure','targetPos':[target.dump()],'item':list(t['items'])}
            mem['treasure_attempt']={'round':turn.round_no,'plan':t}
        return True
    _walk(turn,pioneer,target,reserved,commands)
    return True
