import copy
import hashlib
import json
import unittest
from pathlib import Path
from agent.state import Session, SessionStore
from agent.protocol import empty_response
from agent.engine import compute, Decision


def payload(round_no=1, task=''):
    return {'roundNo':round_no,'mapInfo':{'width':41,'height':32,'zones':[]},
            'teamOur':{'type':'challenger','teamId':'state-test','goldNum':0,'roles':[
                {'id':10011,'roleType':'pioneer','health':200,'pos':{'x':10,'y':10}}]},
            'phaseTask':task,'llmResp':''}

def model_reply(response,**fields):
    context=json.loads(response['prompt'].split('\n',1)[1])
    return json.dumps({**{k:context[k] for k in ('taskKey','requestId','roundNo')},**fields})

class SessionTests(unittest.TestCase):
    def test_duplicate_is_exact_and_does_not_advance(self):
        session=Session();p=payload(1,'task')
        first=session.decide(p); version=session.state['version']
        self.assertEqual(session.decide(p),first)
        self.assertEqual(session.state['version'],version)
        first['prompt']='tampered'
        self.assertNotEqual(session.decide(p)['prompt'],'tampered')

    def test_conflict_does_not_mutate_state(self):
        s=Session();p=payload(1,'task');s.decide(p);before=copy.deepcopy(s.state)
        p['llmResp']='changed'
        self.assertEqual(s.decide(p),empty_response());self.assertEqual(s.state,before)

    def test_task_command_evidence_answer_roundtrip(self):
        s=Session();a=s.decide(payload(1,'task'))
        p=payload(2,'task');p['llmResp']=model_reply(a,executeCmd='printf 42')
        b=s.decide(p);self.assertEqual(b['executeCmd'],'printf 42');self.assertEqual(b['prompt'],'')
        p=payload(3,'task');p['lastCmdResult']='[exitCode:0]\n42\n'
        c=s.decide(p);self.assertIn('[exitCode:0]',c['prompt'])
        p=payload(4,'task');p['llmResp']=model_reply(c,taskAnswer='42')
        d=s.decide(p);self.assertEqual(d['roleCommandMap']['10011']['taskAnswer'],'42');self.assertEqual(d['prompt'],'')
        s.decide(payload(5,''));self.assertFalse(s.state['memory'][0]['verified'])

    def test_same_text_new_instance_rejects_old_response(self):
        s=Session();a=s.decide(payload(1,'same'));s.decide(payload(2,''));b=s.decide(payload(3,'same'))
        self.assertNotEqual(json.loads(a['prompt'].split('\n',1)[1])['taskKey'],json.loads(b['prompt'].split('\n',1)[1])['taskKey'])
        p=payload(4,'same');p['llmResp']=model_reply(a,taskAnswer='wrong')
        self.assertEqual(s.decide(p)['roleCommandMap'],{})

    def test_gap_cannot_use_old_command_result(self):
        s=Session();a=s.decide(payload(1,'task'));p=payload(2,'task');p['llmResp']=model_reply(a,executeCmd='printf 42');s.decide(p)
        p=payload(5,'task');p['lastCmdResult']='SECRET_STALE'
        self.assertNotIn('SECRET_STALE',s.decide(p)['prompt']);self.assertEqual(s.state['unmatched_feedback'],1)

    def test_worker_failure_cannot_commit_proposal(self):
        s=Session()
        def fail(p,state,deadline):
            state['memory']=['uncommitted']
            raise RuntimeError('fail')
        self.assertEqual(s.decide(payload(),fail),empty_response())
        self.assertEqual(s.state['memory'],[])
        self.assertEqual(s.state['fallbacks'],1)

    def test_stale_version_is_discarded(self):
        s=Session()
        def stale(p,state,deadline):return Decision({'prompt':'wrong'},state,99)
        self.assertEqual(s.decide(payload(),stale),empty_response())

    def test_reset_changes_epoch_and_clears_memory(self):
        s=Session();s.decide(payload(1,'task'));s.reset()
        self.assertEqual(s.state['epoch'],1);self.assertIsNone(s.state['task']);self.assertFalse(s.cache)

    def test_teams_and_sides_are_isolated(self):
        store=SessionStore();a=payload();b=copy.deepcopy(a);b['teamOur']['type']='defender'
        self.assertIsNot(store.get(a),store.get(b));self.assertIs(store.get(a),store.get(a))

    def test_main_entrypoint_is_unchanged(self):
        root=Path(__file__).resolve().parents[1]
        saved=json.loads((root/'fixtures/entry-identity.json').read_text())
        self.assertEqual(hashlib.sha256((root/'main3.py').read_bytes()).hexdigest(),saved['main3_sha256'])

if __name__=='__main__':unittest.main()
