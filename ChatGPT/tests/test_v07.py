import json
import unittest
from dataclasses import replace
from agent.engine import compute
from agent.state import Session
from agent.model import Pos,distance
from agent.protocol import decode,empty_response
from agent.policies import construction,mining,pioneer,defense,economy
from agent.objectives import begin
from agent.navigation import route
from agent.telemetry.writer import format_event
from agent.tasks.evidence import encode
from lab.referee import Match,pos,unit
from test_state import payload,model_reply


def local_match(side=0):
    m=Match();m.zones=[z for z in m.zones if z['neutralType'] not in ('stone','iron','copper')];m.mines={}
    sites=[((2,27),'stone'),((7,29),'copper'),((2,21),'iron'),((38,5),'stone'),((31,2),'copper'),((38,10),'iron')]
    for p,kind in sites:m.zones.append({'pos':pos(p),'neutralType':kind});m.mines[p]=10
    return m


def battery(m,side=0):
    t=decode(m.observation(side))
    for i,p in enumerate(construction._tower_sites(t)):m.teams[side]['roles'].append(unit(10040+side*10000+i,'rocket',(p.x,p.y)))


class RoleStrategyTests(unittest.TestCase):
    def test_first_round_every_role_has_action_and_miner_has_ore_goal(self):
        for side in (0,1):
            m=local_match(side);d=compute(m.observation(side));ids=[r.unit_id for r in decode(m.observation(side)).controllable()]
            self.assertTrue(all(str(i) in d.response['roleCommandMap'] for i in ids))
            self.assertEqual(d.state['role_plans'][str(ids[-1])]['goal'],'mine')
            self.assertEqual(d.response['roleCommandMap'][str(ids[0])]['action'],'build')

    def test_exact_layout_and_twelve_primary_walls(self):
        for side in (0,1):
            t=decode(local_match(side).observation(side));s=t.station().pos;x,y=s.x,s.y
            expected={Pos(x-1,y),Pos(x-1,y-2),Pos(x,y-2)} if side==0 else {Pos(x+2,y-1),Pos(x+2,y+1),Pos(x+1,y+1)}
            self.assertEqual(set(construction._tower_sites(t)),expected)
            hub=construction.operator_hub(t)
            self.assertEqual(min(distance(hub,p) for p in t.footprint(t.station())),1)
            self.assertTrue(all(distance(hub,p)==1 for p in expected))
            self.assertEqual(len(construction.priority_wall_sites(t)),12)
            self.assertEqual(len(construction.wall_sites(t)),19)

    def test_defender_is_ready_by_round_70_and_attacks_first_night(self):
        for side in (0,1):
            m=local_match(side);session=Session()
            for _ in range(70):
                m.begin();p=m.observation(side);out=session.decide(p)
                replies=[empty_response(),empty_response()];replies[side]=out;m.step(replies)
            m.begin();p=m.observation(side);t=decode(p);worker=t.workers()[0]
            self.assertEqual(worker.pos,construction.operator_hub(t))
            self.assertEqual(len(t.weapons()),3)
            direction=construction.facing(t);s=t.station().pos
            p['robot']={'roles':[{'id':90000,'pos':pos((s.x+direction*5,s.y)),'health':60,'targetTeam':p['teamOur']['type']}]}
            out=session.decide(p)
            self.assertTrue(any(c['action']=='attack' for c in out['roleCommandMap'].values()))
            self.assertNotIn(str(worker.unit_id),out['roleCommandMap'])

    def test_near_worker_and_base_precedes_opposite_corner_price(self):
        m=local_match();t=decode(m.observation(0));worker=t.workers()[1]
        p=m.observation(0);p['mapInfo']['zones'] += [{'pos':pos((39,1)),'neutralType':'copper'}]
        p['vendorShopList']=[{'name':'copper','price':999},{'name':'iron','price':3},{'name':'stone','price':1}]
        options=mining.candidates(decode(p),worker,{})
        self.assertTrue(options);self.assertNotIn(Pos(39,1),[o[0] for o in options])
        self.assertLessEqual(options[0][2],10)

    def test_mine_stays_locked_when_better_mine_appears(self):
        m=local_match();p=m.observation(0);t=decode(p);worker=t.workers()[1];state={};commands={}
        mining.collect(t,worker,state,set(),commands);lock=state['mine_targets'][str(worker.unit_id)]['target']
        p['mapInfo']['zones'].append({'pos':pos((6,23)),'neutralType':'copper'})
        mining.collect(decode(p),worker,state,set(),{});self.assertEqual(state['mine_targets'][str(worker.unit_id)]['target'],lock)

    def test_night_sell_block_does_not_stop_rear_mining(self):
        m=local_match();m.round=80;p=m.observation(0);r=p['teamOur']['roles'][2]
        r['pos']=pos((2,22));r['backpack']=['copper']*10
        state={'economy_jobs':{str(r['id']):{'kind':'sell','target':pos((20,16))}}};commands={}
        mining.plan_miner(decode(p),decode(p).workers()[1],state,set(),commands)
        self.assertEqual(commands[r['id']]['action'],'collect')
        self.assertEqual(commands[r['id']]['targetPos'],[pos((2,21))])

    def test_failure_feedback_excludes_repeated_cell_only_temporarily(self):
        p=payload(3);p['teamOur']['roles'][0]['roleType']='worker';p['lastRoundRoleActionResults']={'10011':False}
        state={'last_round':2,'last_actions':{'10011':{'action':'move','targetPos':[pos((11,10))]}},
               'movement_failures':{'10011':{'target':pos((11,10)),'count':1,'until':4}}}
        t=decode(p);avoid=begin(t,state);self.assertEqual(avoid[10011],(Pos(11,10),))
        step,_=route(replace(t,navigation_avoid=avoid),t.workers()[0],[Pos(13,10)])
        self.assertNotEqual(step,Pos(11,10))

    def test_night_upgrade_does_not_replace_first_shot(self):
        m=local_match();battery(m);m.round=71;p=m.observation(0);t=decode(p)
        r=p['teamOur']['roles'][0];r['pos']=construction.operator_hub(t).dump();r['backpack']=['WeaponUpgradeVoucher1']
        p['robot']={'roles':[{'id':99,'pos':pos((10,26)),'health':100}]}
        out=compute(p).response
        self.assertTrue(any(c['action']=='attack' for c in out['roleCommandMap'].values()))
        self.assertFalse(any(c['action']=='use' for c in out['roleCommandMap'].values()))

    def test_task_stand_can_move_safely_without_exiting(self):
        m=local_match();m.round=80;p=m.observation(0);p['phaseTask']='task'
        p['teamOur']['playerTasks']=[{'taskPosition':pos((10,20)),'isValid':False,'timeoutRounds':30}]
        p['teamOur']['roles'][1]['pos']=pos((10,19))
        p['robot']={'roles':[{'id':99,'pos':pos((14,19)),'health':40}]}
        t=decode(p);worker=next(r for r in t.controllable() if r.kind=='pioneer');state={};commands={}
        leaving=pioneer.safety(t,worker,state,set(),commands)
        self.assertFalse(leaving);self.assertIn(worker.unit_id,commands)
        self.assertLessEqual(distance(Pos.load(commands[worker.unit_id]['targetPos'][0]),Pos(10,20)),1)

    def test_task_escapes_if_no_safe_task_stand(self):
        m=local_match();m.round=80;p=m.observation(0);p['phaseTask']='task'
        p['teamOur']['playerTasks']=[{'taskPosition':pos((11,20)),'isValid':False}]
        p['teamOur']['roles'][1]['pos']=pos((10,20));p['robot']={'roles':[{'id':99,'pos':pos((14,20)),'health':40}]}
        t=decode(p);worker=next(r for r in t.controllable() if r.kind=='pioneer');state={};commands={}
        self.assertTrue(pioneer.safety(t,worker,state,set(),commands))
        self.assertEqual(state['task_exit_reason'],'robot_threat_no_safe_task_stand')


class SandboxAndLogTests(unittest.TestCase):
    def test_all_commands_and_results_reach_next_prompt(self):
        s=Session();out=s.decide(payload(1,'查找文化遗产API文档'))
        self.assertIn('绝对路径',out['prompt']);self.assertIn('一无所知',out['prompt'])
        for r,command,result in ((2,'pwd','[exitCode:0]\n/sandbox'),(4,'ls /sandbox','[exitCode:0]\ndocs'),(6,'cat /sandbox/docs/api.txt','[exitCode:0]\nAPI partially damaged')):
            p=payload(r,'查找文化遗产API文档');p['llmResp']=model_reply(out,executeCmd=command)
            out=s.decide(p);self.assertEqual(out['executeCmd'],command)
            p=payload(r+1,'查找文化遗产API文档');p['lastCmdResult']=result;out=s.decide(p)
        context=json.loads(out['prompt'].split('\n',1)[1])
        self.assertEqual(len(context['sandboxHistory']),3)
        self.assertEqual(context['sandboxHistory'][0]['command'],'pwd')
        self.assertIn('partially damaged',context['sandboxHistory'][-1]['result'])

    def test_summary_is_available_to_same_point_next_city_without_old_answer(self):
        s=Session();s.state['task_binding']={'point':pos((10,20)),'accepted_round':1,'timeout':30}
        out=s.decide(payload(1,'查询北京文化遗产'));p=payload(2,'查询北京文化遗产')
        p['llmResp']=model_reply(out,taskAnswer='BEIJING_ONLY',taskSummary={'taskFamily':'heritage','steps':['先读取API说明，再使用city参数'],'parameterSlots':['city']})
        self.assertEqual(s.decide(p)['roleCommandMap']['10011']['taskAnswer'],'BEIJING_ONLY')
        s.decide(payload(3,''));s.state['task_binding']={'point':pos((10,20)),'accepted_round':4,'timeout':30}
        out=s.decide(payload(4,'查询南京文化遗产'));c=json.loads(out['prompt'].split('\n',1)[1])
        self.assertEqual(c['candidateSOPs'][0]['summary']['parameterSlots'],['city'])
        self.assertNotIn('BEIJING_ONLY',out['prompt']);self.assertFalse(s.state['memory'][0]['verified'])

    def test_feedback_not_lost_when_medicine_uses_role_action(self):
        s=Session();out=s.decide(payload(1,'task'));p=payload(2,'task')
        p['teamOur']['roles'][0]['health']=40;p['teamOur']['roles'][0]['backpack']=['Medicine']
        p['llmResp']=model_reply(out,taskAnswer='42');out=s.decide(p)
        self.assertEqual(out['roleCommandMap']['10011']['action'],'use')
        out=s.decide(payload(3,'task'));self.assertEqual(out['roleCommandMap']['10011']['taskAnswer'],'42')

    def test_logging_has_answer_without_diagnostics(self):
        rsp={**empty_response(),'roleCommandMap':{'10011':{'action':'submitAnswer','taskAnswer':'{"city":"北京"}'}}}
        text=format_event({'round':3,'req':{'roundNo':3},'rsp':rsp,'rolePlans':{'10011':{'goal':'submitAnswer'}}})
        self.assertTrue(text.startswith('ROUND 3\nreq '));self.assertIn('submitAnswer {',text)
        self.assertNotIn('diagnostics ',text)

    def test_large_accumulated_context_is_explicitly_bounded(self):
        c={'task':'真实任务'*500,'sandboxHistory':[{'round':i,'command':'x'*12000,'result':'y'*24000} for i in range(30)]}
        result=encode(c)
        self.assertLess(len(result),180000)
        self.assertIn('context_omitted',result)
        self.assertEqual(len(c['sandboxHistory'][0]['result']),24000)

if __name__=='__main__':unittest.main()
