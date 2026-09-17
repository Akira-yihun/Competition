import importlib.util
import sys
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('simulate', Path(__file__).resolve().parents[1] / 'tools' / 'simulate.py')
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)
def reply(**kwargs):
    return dict({'roleCommandMap': {}, 'prompt': '', 'executeCmd': ''}, **kwargs)

EMPTY = reply()

class SimulatorTests(unittest.TestCase):
    def test_move_contention_swap_and_chain(self):
        self.assertEqual(sim.resolve_moves({1:(1,1),2:(3,1)}, {1:(2,1),2:(2,1)}, set()), {})
        self.assertEqual(sim.resolve_moves({1:(1,1),2:(2,1)}, {1:(2,1),2:(1,1)}, set()), {})
        self.assertEqual(sim.resolve_moves({1:(1,1),2:(2,1)}, {1:(2,1),2:(3,1)}, set()), {1:(2,1),2:(3,1)})
        self.assertEqual(sim.resolve_moves({1:(1,1),2:(2,1)}, {1:(2,1),2:(3,1)}, {(3,1)}), {})

    def test_station_footprint_and_seed(self):
        a,b = sim.Match(5),sim.Match(5)
        self.assertEqual(a.observation(0), b.observation(0))
        station = a.teams[0]['roles'][-1]
        self.assertEqual(sim.cells(station), {(5,26),(6,26),(5,25),(6,25)})

    def test_upgrade_heals_and_requires_correct_tier(self):
        m=sim.Match(); r=m.teams[0]['roles'][0]; r['pos']=sim.pos((7,24))
        r['backpack']=['StationUpgradeVoucher1']; station=m.teams[0]['roles'][-1]; station['health']=2
        self.assertTrue(m.role_action(0,r,dict(action='use',name='StationUpgradeVoucher1'),(5,26),{}))
        self.assertEqual(station['health'],3000)
        r['backpack']=['StationUpgradeVoucher1']
        self.assertFalse(m.role_action(0,r,dict(action='use',name='StationUpgradeVoucher1'),(5,26),{}))

    def test_rocket_three_full_cooldown_rounds(self):
        m=sim.Match(); m.round=71; t=m.teams[0]; r=t['roles'][0]; r['pos']=sim.pos((4,25))
        tower=sim.unit(10040,'rocket',(4,26));t['roles'].append(tower)
        m.robots=[dict(id=90000,roleType='bossRobot',pos=sim.pos((10,26)),health=800,abnormalState='',targetTeam='challenger')]
        fire=reply(roleCommandMap={'10040':dict(action='attack',controllerId='10010',targetPos=[sim.pos((10,26))])})
        m.step([fire,EMPTY]); self.assertEqual(tower['cooldown'],3)
        for expected in (2,1,0):
            m.step([fire,EMPTY]); self.assertFalse(t['results']['10040']); self.assertEqual(tower['cooldown'],expected)

    def test_command_fixture_never_executes(self):
        m=sim.Match(); m.teams[0]['active']=dict(index=0,start=1)
        m.teams[0]['roles'][1]['pos']=sim.pos((10,25))
        m.step([reply(roleCommandMap={},executeCmd='touch /tmp/DO_NOT_EXECUTE',prompt='x'),EMPTY])
        self.assertEqual(m.teams[0]['lastCmdResult'],'[exitCode:127]\nunsupported fixture command')
        self.assertEqual(m.teams[0]['llmResp'],'42')

    def test_candidate_task_command_roundtrip(self):
        m=sim.Match(task_profile="tools")
        t=m.teams[0]
        t['roles'][1]['pos']=sim.pos((10,25))
        t['active']=dict(index=0,start=1)
        t['phaseTask']='LOCAL FIXTURE 6 × 7，答案42'
        for _ in range(8):
            m.begin()
            response=sim.candidate(m.observation(0))
            m.step([response,EMPTY])
            if m.metrics[0]['completed_tasks']:
                break
        self.assertEqual(m.metrics[0]['completed_tasks'],1)
        self.assertEqual(m.metrics[0]['mock_commands'],1)

    def test_execution_failure_is_not_exception(self):
        m=sim.Match();m.step([reply(roleCommandMap={'10010':dict(action='move',targetPos=[sim.pos((5,25))])}),EMPTY])
        self.assertFalse(m.teams[0]['results']['10010']); self.assertEqual(m.teams[0]['exceptions'],0)
        m.step([reply(roleCommandMap={'10010':dict(action='move')}),EMPTY])
        self.assertEqual(m.teams[0]['exceptions'],1)

    def test_night_build_fails_without_spending(self):
        m=sim.Match(); m.round=71
        r=m.teams[0]['roles'][0]
        m.step([reply(roleCommandMap={'10010': dict(action='build',name='rocket',targetPos=[sim.pos((4,26))])}),EMPTY])
        self.assertEqual(m.teams[0]['goldNum'],75)
        self.assertFalse(m.teams[0]['results']['10010'])
        self.assertEqual(m.teams[0]['exceptions'],0)

    def test_attack_bounds_and_zero_direction(self):
        for kind, target in [('rocket',(-1,26)), ('railgun',(4,26)), ('gatling',(4,26))]:
            with self.subTest(kind=kind):
                m=sim.Match();m.round=71
                tower=sim.unit(10040,kind,(4,26));m.teams[0]['roles'].append(tower)
                m.step([reply(roleCommandMap={'10040':dict(action='attack',controllerId='10010',targetPos=[sim.pos(target)])}),EMPTY])
                self.assertFalse(m.teams[0]['results']['10040'])
                self.assertEqual(tower['cooldown'],0)
                self.assertEqual(m.teams[0]['exceptions'],0)

    def test_bad_envelope_has_no_side_effects(self):
        for patch in ({'prompt': 7}, {'executeCmd': []}, {'roleCommandMap': []}):
            with self.subTest(patch=patch):
                m=sim.Match();before=sim.xy(m.teams[0]['roles'][0])
                response=reply(roleCommandMap={'10010':dict(action='move',targetPos=[sim.pos((3,25))])})
                response.update(patch)
                m.step([response,EMPTY])
                self.assertEqual(sim.xy(m.teams[0]['roles'][0]),before)
                self.assertEqual(m.teams[0]['exceptions'],1)
        m=sim.Match();m.step([{'roleCommandMap': {}},EMPTY])
        self.assertEqual(m.teams[0]['exceptions'],1)

    def test_bad_coordinate_schema_is_exception(self):
        m=sim.Match()
        m.step([reply(roleCommandMap={'10010':dict(action='move',targetPos=[{'x':3.0,'y':25}])}),EMPTY])
        self.assertEqual(m.teams[0]['exceptions'],1)
        self.assertFalse(m.teams[0]['results']['10010'])

    def test_score_breakdown_sums_to_total(self):
        m=sim.Match();m.round=130
        m.step([EMPTY,EMPTY])
        self.assertEqual(m.metrics[0]['survival_score'],10)
        for side in (0,1):
            self.assertEqual(m.teams[side]['totalScore'],sum(m.metrics[side][key] for key in ('task_score','kill_score','survival_score')))

if __name__=='__main__':unittest.main()
