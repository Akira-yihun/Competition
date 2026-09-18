import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.state import Session
from agent.protocol import decode, empty_response
from agent.model import Pos, distance
from agent.policies import construction, defense, economy
from agent.guard import validate
from agent.tasks.channel import parse_with_reason
from agent.telemetry.writer import flush
from lab.referee import Match, unit, pos
from test_state import payload, model_reply


class DefenseRevisionTests(unittest.TestCase):
    def test_mirrored_u_wall_and_corner_layout(self):
        m=Match()
        for side in (0,1):
            t=decode(m.observation(side));s=t.station().pos
            walls=construction.wall_sites(t);corners=construction._tower_sites(t)
            front=s.x+3 if side==0 else s.x-2
            rear=s.x-2 if side==0 else s.x+3
            self.assertEqual(len(corners),3)
            self.assertTrue(all(p.x==front for p in walls[:6]))
            self.assertFalse(any(Pos(rear,y) in walls for y in range(s.y-2,s.y+2)))
            self.assertTrue(all(distance(p,construction.operator_hub(t))<=1 for p in corners))

    def test_three_towers_and_adjacent_night_operators_both_sides(self):
        for side in (0,1):
            m=Match(1);session=Session();built=[]
            for _ in range(71):
                m.begin();request=m.observation(side);response=session.decide(request)
                built.extend(c['name'] for c in response['roleCommandMap'].values() if c['action']=='build' and c['name']!='wall')
                if m.round==71:
                    self.assertEqual(len(decode(request).weapons()),3)
                    assignments=session.state['defense_assignments']
                    self.assertEqual(len(assignments),3)
                    self.assertTrue(all(a['distance']<=1 for a in assignments))
                replies=[empty_response(),empty_response()];replies[side]=response;m.step(replies)
                self.assertFalse(m.teams[side]['errors'])
            self.assertEqual(built[0],'rocket')
            self.assertEqual(built,['rocket']*3)
            self.assertEqual(len({a['role'] for a in assignments}),1)

    def test_recall_is_sticky_after_route_shortens(self):
        m=Match();m.round=40;m.teams[0]['roles'].append(unit(10040,'rocket',(7,24)))
        t=decode(m.observation(0));r=t.workers()[0]
        self.assertFalse(defense.should_recall(t,r))
        self.assertTrue(defense.should_recall(t,r,{'defense':{'day':0,'mobilized':[str(r.unit_id)]}}))

    def test_active_pioneer_keeps_task_at_night(self):
        m=Match();m.round=71;m.teams[0]['roles'].append(unit(10040,'rocket',(7,24)))
        p=m.observation(0);p['phaseTask']='unfinished task';s=Session();out=s.decide(p)
        self.assertIn('理解阶段',out['prompt']);self.assertEqual(out['executeCmd'],'')
        self.assertTrue(s.state['defense_assignments'])

    def test_medicine_needs_no_target(self):
        m=Match();r=m.teams[0]['roles'][0];r['health']=30;r['backpack']=['Medicine']
        t=decode(m.observation(0));commands={};economy.healing(t,commands)
        out=validate({**empty_response(),'roleCommandMap':{str(k):v for k,v in commands.items()}},t)
        self.assertEqual(out['roleCommandMap']['10010'],{'action':'use','name':'Medicine'})
        m.step([out,empty_response()]);self.assertEqual(r['health'],220)

    def test_small_ore_stock_sells_when_near_vendor(self):
        m=Match();t=decode(m.observation(0));vendor=t.vendor()
        for i,(p,kind) in enumerate(zip(construction._tower_sites(t),construction.TOWER_LOADOUT)):
            m.teams[0]['roles'].append(unit(10040+i,kind,(p.x,p.y)))
        r=m.teams[0]['roles'][0];r['pos']=pos((vendor.x-1,vendor.y));r['backpack']=['copper']
        t=decode(m.observation(0));commands={};economy.plan(t,set(),set(),commands,{})
        self.assertEqual(commands[r['id']]['action'],'sell')
        self.assertEqual(commands[r['id']]['num'],1)


class TaskDiagnosticTests(unittest.TestCase):
    def test_unwrapped_and_json_answers(self):
        pending={'kind':'model','id':'r','round':1}
        for text in ('42','{"taskAnswer":"42"}','```json\n{"taskAnswer":"42"}\n```'):
            self.assertEqual(parse_with_reason(text,pending,'t')[0],('answer','42'))
        self.assertEqual(parse_with_reason('{"result":42}',pending,'t')[0],('answer','{"result":42}'))
        self.assertEqual(parse_with_reason('{"taskAnswer":0}',pending,'t')[0],('answer','0'))
        self.assertEqual(parse_with_reason('{broken',pending,'t')[1],'malformed_json')
        self.assertEqual(parse_with_reason('{"requestId":"old","taskAnswer":"42"}',pending,'t')[1],'mismatched_requestId')

    def test_empty_feedback_waits_for_original_request(self):
        s=Session();first=s.decide(payload(1,'task'));pending=dict(s.state['task']['pending'])
        self.assertEqual(s.decide(payload(2,'task'))['prompt'],'')
        self.assertEqual(s.state['task']['pending'],pending)
        p=payload(3,'task');p['llmResp']=model_reply(first,taskAnswer='42')
        second=s.decide(p);self.assertFalse(second['roleCommandMap'])
        p=payload(4,'task');p['llmResp']=model_reply(second,taskAnswer='42')
        self.assertEqual(s.decide(p)['roleCommandMap']['10011']['taskAnswer'],'42')

    def test_plaintext_news_task_answer_and_failure_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'debug.ndjson'
            with patch.dict(os.environ,{'CORE_GEEK_DEBUG_LOG':str(path)}):
                s=Session();p=payload(1,'调测任务');p['worldNews']={'officialNews':'矿价变化'};s.decide(p)
                p=payload(2,'调测任务');p['llmResp']='{broken';s.decide(p)
                p=payload(3,'调测任务');p['llmResp']='42';s.decide(p)
                p=payload(4,'调测任务');p['llmResp']='42';s.decide(p)
                flush()
            events=[json.loads(l.removeprefix('diagnostics ')) for l in path.read_text().splitlines() if l.startswith('diagnostics ')]
            self.assertIn('矿价变化',events[0]['worldNews'])
            self.assertEqual(events[0]['phaseTask'],'调测任务')
            self.assertEqual(events[1]['llmResp'],'{broken')
            self.assertEqual(events[1]['taskState']['last_parse_reason'],'malformed_json')
            self.assertEqual(events[3]['submittedAnswers'],{'10011':'42'})
            before=path.read_bytes()
            with patch.dict(os.environ,{'CORE_GEEK_DEBUG_LOG':'off'}):Session().decide(payload());flush()
            self.assertEqual(path.read_bytes(),before)

if __name__=='__main__':unittest.main()
