import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from agent.brain import decide, empty_response
from agent.protocol import Turn, Pos
from agent.grid import route

class SubmissionTests(unittest.TestCase):
    def test_official_sample_repeatable(self):
        payload=json.loads((ROOT/'../../docs/request.txt').read_text())
        before=json.dumps(payload,sort_keys=True)
        first=decide(payload)
        self.assertEqual(first,decide(payload))
        self.assertEqual(set(first),set(empty_response()))
        self.assertEqual(before,json.dumps(payload,sort_keys=True))
        json.dumps(first)

    def test_stationary_route_has_no_key_error(self):
        payload=json.loads((ROOT/'../../docs/request.txt').read_text())
        turn=Turn.load(payload)
        role=turn.controllable()[0]
        self.assertEqual(route(turn,role,[role.pos]),(None,0))

    def test_reserved_first_step_is_avoided(self):
        payload={'roundNo':1,'mapInfo':{'width':8,'height':8},'teamOur':{'roles':[{'id':1,'pos':{'x':1,'y':1},'roleType':'worker','health':220}]}}
        turn=Turn.load(payload)
        step,length=route(turn,turn.controllable()[0],[Pos(5,1)],[Pos(2,1)])
        self.assertNotEqual(step,Pos(2,1))
        self.assertEqual(length,4)

    def test_failed_move_changes_tie_breaking(self):
        payload={'roundNo':2,'mapInfo':{'width':8,'height':8},'teamOur':{'roles':[{'id':1,'pos':{'x':1,'y':1},'roleType':'worker','health':220}]}}
        turn=Turn.load(payload)
        first,length=route(turn,turn.controllable()[0],[Pos(5,1)])
        payload['lastRoundRoleActionResults']={'1':False}
        turn=Turn.load(payload)
        alternative,other_length=route(turn,turn.controllable()[0],[Pos(5,1)])
        self.assertEqual(length,other_length)
        self.assertNotEqual(first,alternative)

    def test_dead_units_do_not_block(self):
        payload={'roundNo':1,'mapInfo':{'width':8,'height':8},'teamOur':{'roles':[{'id':1,'pos':{'x':1,'y':1},'roleType':'worker','health':220},{'id':2,'pos':{'x':2,'y':1},'roleType':'worker','health':0}]}}
        turn=Turn.load(payload)
        self.assertNotIn(Pos(2,1),turn.blocked(turn.controllable()[0]))

if __name__=='__main__':
    unittest.main()
