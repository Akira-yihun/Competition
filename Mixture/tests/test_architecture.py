import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent.scheduler import Intent, ReservationTable
from agent.guard import validate
from agent.protocol import decode
from lab.task_fixtures import mock_llm
from loop.store import save
from loop.cli import run

class ArchitectureTests(unittest.TestCase):
    def test_rejected_reservation_does_not_spend(self):
        turn=decode({'roundNo':1,'mapInfo':{'width':41,'height':32},'teamOur':{'goldNum':25}})
        table=ReservationTable(25,2)
        a=Intent(1,{'action':'build','name':'rocket','targetPos':[{'x':5,'y':5}]})
        b=Intent(2,{'action':'build','name':'rocket','targetPos':[{'x':6,'y':5}]})
        self.assertTrue(table.reserve(a,turn));self.assertFalse(table.reserve(b,turn))
        self.assertEqual(table.gold,0);self.assertEqual(table.towers,3);self.assertNotIn(2,table.actors)

    def test_reasoning_oracle_same_answer_for_both_formats(self):
        raw=mock_llm('answer only',True)
        structured=json.loads(mock_llm(json.dumps({'taskKey':'i','roundNo':1,'requestId':'r'}),True))
        self.assertEqual(raw,structured['taskAnswer']);self.assertNotIn('executeCmd',structured)

    def test_tools_profile_does_not_leak_answer_to_legacy(self):
        self.assertNotEqual(mock_llm('answer only',True,'tools'),'42')
        reply=json.loads(mock_llm(json.dumps({'taskKey':'i','roundNo':1,'requestId':'r'}),True,'tools'))
        self.assertEqual(reply['executeCmd'],'printf 42');self.assertNotIn('taskAnswer',reply)

    def test_atomic_store_replaces_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json';save(path,{'version':1});save(path,{'version':2})
            self.assertEqual(json.loads(path.read_text()),{'version':2})
            self.assertFalse(path.with_suffix('.json.tmp').exists())

    def test_remote_is_blocked_without_side_effects(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'not-created'
            self.assertEqual(run(target,'1',130,'official')['status'],'BLOCKED_CONFIG')
            self.assertFalse(target.exists())

if __name__=='__main__':unittest.main()
