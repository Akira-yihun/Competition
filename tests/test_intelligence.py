import copy
import json
import unittest
from agent.state import Session
from agent.protocol import decode,empty_response
from agent.guard import validate
from agent.intelligence import news
from agent.policies import construction,economy,defense,treasure
from agent.model import Pos,distance
from test_state import payload,model_reply
from lab.referee import Match,unit,pos


def context(response):return json.loads(response['prompt'].rsplit('\n',1)[1])

def forecast(start=131,end=390):
    return {'ore':'iron','startRound':start,'endRound':end,'price':6,'direction':'up','available':False,'holdUntil':start,'confidence':.9,'evidence':'第一天新闻：明日停产两天'}

def news_payload(r):
    p=payload(r);p['worldNews']={'officialNews':f'新闻{r}','folkLegends':f'线索{r}'};return p


class IntelligenceTests(unittest.TestCase):
    def test_quota_retry_replay_and_next_day(self):
        s=Session()
        for r in (1,3,5):
            p=news_payload(r);out=s.decide(p);self.assertTrue(out['prompt'])
            used=s.state['intelligence']['used'];self.assertEqual(s.decide(p),out);self.assertEqual(s.state['intelligence']['used'],used)
            p=news_payload(r+1);p['llmResp']=json.dumps({'requestId':context(out)['requestId'],'market':[]})
            # Keep same day's news unchanged while consuming response.
            p['worldNews']=news_payload(r)['worldNews'];self.assertFalse(s.decide(p)['prompt'])
        self.assertFalse(s.decide(news_payload(7))['prompt']);self.assertEqual(s.state['intelligence']['used'],3)
        self.assertTrue(s.decide(news_payload(131))['prompt']);self.assertEqual(s.state['intelligence']['used'],1)

    def test_history_keeps_previous_days_and_forecast_windows(self):
        s=Session();out=s.decide(news_payload(1));p=news_payload(2);p['worldNews']=news_payload(1)['worldNews']
        p['llmResp']=json.dumps({'requestId':context(out)['requestId'],'market':[forecast()]});s.decide(p)
        self.assertTrue(news.should_hold(s.state,'iron',130));self.assertFalse(news.should_hold(s.state,'iron',131))
        self.assertGreater(news.mine_value(s.state,'iron',10,2),2)
        self.assertEqual(news.mine_value(s.state,'iron',131,2),0)
        self.assertEqual(news.mine_value(s.state,'iron',391,2),2)
        out=s.decide(news_payload(131));self.assertEqual(len(context(out)['newsHistory']),2)

    def test_stale_news_never_becomes_task_answer(self):
        s=Session();out=s.decide(news_payload(1));p=payload(2,'real task');p['llmResp']='{"taskAnswer":"wrong"}'
        follow=s.decide(p);self.assertFalse(follow['roleCommandMap']);self.assertEqual(context(follow)['stage'],'understand')
        self.assertEqual(s.state['intelligence']['last_reason'],'invalid_news_response')

    def test_task_calls_do_not_consume_general_quota(self):
        s=Session();out=s.decide(payload(1,'task'))
        p=payload(2,'task');p['llmResp']=model_reply(out,taskUnderstanding={'objective':'probe'});out=s.decide(p)
        for r in (3,5,7,9):
            p=payload(r,'task');p['llmResp']=model_reply(out,executeCmd='printf 42');self.assertEqual(s.decide(p)['executeCmd'],'printf 42')
            p=payload(r+1,'task');p['lastCmdResult']='[exitCode:0]\n42';out=s.decide(p)
        self.assertEqual(s.state['intelligence']['used'],0)
        self.assertEqual(context(out)['stage'],'solve')

    def test_explicit_quota_error_stops_general_calls(self):
        s=Session();p=news_payload(1);p['errors']=[{'errorCode':5}]
        self.assertFalse(s.decide(p)['prompt']);self.assertEqual(s.state['intelligence']['used'],3)

    def test_forecast_holds_ore_then_sells_on_target_round(self):
        m=Match();p=m.observation(0);p['roundNo']=130;t=decode(p);v=t.vendor()
        p['teamOur']['roles'][2]['pos']=pos((v.x-1,v.y));p['teamOur']['roles'][2]['backpack']=['iron']*4
        p['teamOur']['roles'] += [unit(10040+i,'rocket',(q.x,q.y)) for i,q in enumerate(construction._tower_sites(t))]
        state={'intelligence':{'market':[forecast()]}}
        for r,expect in [(130,False),(131,True)]:
            p['roundNo']=r;commands={};economy.plan(decode(p),{10010},set(),commands,state)
            self.assertEqual(commands.get(10012,{}).get('action')=='sell',expect)

    def test_treasure_exact_items_and_result_blocks_repeated_sacrifice(self):
        m=Match();p=m.observation(0);p['weaponShopList'] += [{'name':'StarSand','price':15}]
        t=decode(p);pioneer=t.controllable()[1]
        plan={'ready':True,'position':pioneer.pos.dump(),'items':['StarSand','StarSand'],'startRound':1,'endRound':200,'confidence':.95,'evidence':'传闻1+2'}
        state={};mem=news.memory(state);mem['pending']={'id':'n','round':0};mem['revision']=1
        news.accept(t,mem,{'requestId':'n','market':[],'treasure':plan})
        p['teamOur']['roles'][1]['backpack']=['StarSand']*2;t=decode(p);pioneer=t.controllable()[1];commands={}
        self.assertTrue(treasure.plan(t,pioneer,state,set(),commands))
        out=validate({**empty_response(),'roleCommandMap':{str(k):v for k,v in commands.items()}},t)
        self.assertEqual(out['roleCommandMap']['10011']['item'],['StarSand']*2)
        bad=copy.deepcopy(out);bad['roleCommandMap']['10011']['item'].append('StarSand');self.assertFalse(validate(bad,t)['roleCommandMap'])
        p['roundNo']=2;p['worldNews']={};p['lastSummonTreasureResult']=3;mem['pending']=None;state['last_round']=1;news.ingest(decode(p),state)
        self.assertIsNone(mem['treasure'])
        mem['pending']={'id':'n','round':2};news.accept(decode(p),mem,{'requestId':'n','market':[],'treasure':plan});self.assertIsNone(mem['treasure'])

    def test_treasure_feedback_gap_does_not_repeat_offering(self):
        state={};mem=news.memory(state)
        plan={'position':{'x':10,'y':10},'items':[],'startRound':1,'endRound':100,'revision':0}
        mem['treasure']=plan;mem['treasure_attempt']={'round':1,'plan':plan}
        p=payload(4);p['lastSummonTreasureResult']=1
        news.ingest(decode(p),state)
        self.assertIsNone(mem['treasure']);self.assertIsNone(mem['treasure_attempt'])
        self.assertFalse(mem.get('treasure_closed'));self.assertEqual(mem['treasure_feedback'][0]['code'],0)

    def test_treasure_waits_until_window_and_stops_after_success(self):
        m=Match();p=m.observation(0);t=decode(p);pioneer=t.controllable()[1];state={};mem=news.memory(state)
        mem['treasure']={'position':pioneer.pos.dump(),'items':[],'startRound':5,'endRound':10,'revision':0}
        commands={};self.assertTrue(treasure.plan(t,pioneer,state,set(),commands));self.assertFalse(commands)
        p['roundNo']=5;t=decode(p);treasure.plan(t,pioneer,state,set(),commands);self.assertEqual(commands[pioneer.unit_id]['action'],'summonTreasure')
        p['roundNo']=6;p['lastSummonTreasureResult']=1;news.ingest(decode(p),state);self.assertTrue(mem['treasure_closed'])

    def test_single_operator_rotates_ready_rockets_without_moving(self):
        m=Match();p=m.observation(0);p['roundNo']=71;t=decode(p);hub=construction.operator_hub(t)
        p['teamOur']['roles'][0]['pos']=hub.dump()
        p['teamOur']['roles'] += [unit(10040+i,'rocket',(q.x,q.y)) for i,q in enumerate(construction._tower_sites(t))]
        p['robot']={'roles':[{'id':999,'roleType':'bossRobot','pos':{'x':9,'y':23},'health':800,'targetTeam':'challenger'}]}
        state={};shots=[]
        for r in range(71,75):
            p['roundNo']=r;t=decode(p);commands={};defense.plan(t,list(t.workers()),set(),commands,state)
            attacks=[(rid,c) for rid,c in commands.items() if c['action']=='attack'];self.assertLessEqual(len(attacks),1)
            self.assertNotIn(10010,commands)
            for u in p['teamOur']['roles']:u['cooldown']=max(0,u.get('cooldown',0)-1)
            if attacks:
                rid,c=attacks[0];shots.append(rid);self.assertEqual(c['controllerId'],'10010')
                next(u for u in p['teamOur']['roles'] if u['id']==rid)['cooldown']=3
        self.assertEqual(len(shots),3);self.assertEqual(len(set(shots)),3)

    def test_clear_map_completes_walls_first_day(self):
        m=Match();m.zones=[z for z in m.zones if z['neutralType'] not in ('stone','iron','copper')];m.mines={}
        for p in [(2,20),(10,28)]:
            m.zones.append({'pos':pos(p),'neutralType':'stone'});m.mines[p]=100
        session=Session()
        for _ in range(70):m.begin();m.step([session.decide(m.observation(0)),empty_response()])
        self.assertEqual(m.metrics[0]['walls_built'],16);self.assertEqual(m.metrics[0]['towers_built'],3)

    def test_purchase_priority_rocket_then_station_then_wall(self):
        m=Match();p=m.observation(0);p['roundNo']=131;p['teamOur']['goldNum']=500
        t=decode(p);shop=next(q for q,k in t.zones.items() if k=='weaponShop');p['teamOur']['roles'][0]['pos']=pos((shop.x-1,shop.y))
        rockets=[unit(10040+i,'rocket',(q.x,q.y)) for i,q in enumerate(construction._tower_sites(t))]
        p['teamOur']['roles']+=rockets+[unit(40000,'wall',(8,25))]
        for stage,expected in [(0,'WeaponUpgradeVoucher1'),(1,'StationUpgradeVoucher1'),(2,'WallUpgradeVoucher1')]:
            if stage>=1:
                for r in rockets:r['level']=3
            if stage==2:next(u for u in p['teamOur']['roles'] if u['roleType']=='station')['level']=3
            t=decode(p);commands={};economy._upgrade(t,t.workers()[0],500,set(),commands,set())
            self.assertEqual(commands[10010]['name'],expected)

    def test_held_upgrade_is_used_after_dusk_recall(self):
        m=Match();p=m.observation(0);p['roundNo']=71;t=decode(p);p['teamOur']['roles'][0]['pos']=construction.operator_hub(t).dump()
        p['teamOur']['roles'][0]['backpack']=['WeaponUpgradeVoucher1']
        p['teamOur']['roles'] += [unit(10040+i,'rocket',(q.x,q.y)) for i,q in enumerate(construction._tower_sites(t))]
        t=decode(p);commands={};defense.plan(t,list(t.workers()),set(),commands,{})
        self.assertEqual(commands[10010]['action'],'use');self.assertEqual(len(commands),1)

    def test_first_day_both_collect_stone_and_later_only_defender(self):
        m=Match();p=m.observation(0);p['teamOur']['goldNum']=0;t=decode(p)
        p['teamOur']['roles'] += [unit(10040+i,'rocket',(q.x,q.y)) for i,q in enumerate(construction._tower_sites(t))]
        for r,expected in [(10,['stone','stone']),(140,['stone','mine'])]:
            p['roundNo']=r;state={};commands={};economy.plan(decode(p),set(),set(),commands,state)
            self.assertEqual([state['economy_jobs'][str(i)]['kind'] for i in (10010,10012)],expected)

if __name__=='__main__':unittest.main()
