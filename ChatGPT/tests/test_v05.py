"""Regression scenarios for the September 19 strategy requirements."""
import json
import unittest
from dataclasses import replace
from agent.protocol import decode, empty_response
from agent.model import Pos, distance
from agent.engine import compute
from agent.policies import combat, construction, defense, economy
from agent.navigation import route, safe_cell
from agent.telemetry.writer import format_event
from lab.referee import Match, unit, pos


def arena(side=0):
    m=Match();t=decode(m.observation(side))
    for i,p in enumerate(construction._tower_sites(t)):
        m.teams[side]['roles'].append(unit(900+i,'rocket',(p.x,p.y)))
    return m


class RevisionTests(unittest.TestCase):
    def test_round_log_preserves_raw_values_and_order(self):
        req={'roundNo':5,'worldNews':{'officialNews':'a\nb','folkLegends':'传闻'},'phaseTask':'x'*70000,'llmResp':'result','lastCmdResult':'ok'}
        rsp={**empty_response(),'prompt':'p\nq','executeCmd':'pwd'}
        lines=format_event({'round':5,'req':req,'rsp':rsp}).splitlines()
        self.assertEqual(lines[0],'ROUND 5')
        self.assertEqual(json.loads(lines[1][4:]),req)
        self.assertEqual(json.loads(lines[2][4:]),rsp)
        self.assertEqual([l.split(' ')[0] for l in lines[3:6]],['官方新闻','民间传闻','自进化任务要求'])
        self.assertTrue(lines[6].startswith('req llm'));self.assertTrue(lines[9].startswith('rsp executecmd'))

    def test_rocket_nearest_anchor_and_second_row_mirrored(self):
        for side in (0,1):
            m=arena(side);t=decode(m.observation(side));tower=replace(t.weapons()[0],level=1,attack_range=99)
            direction=construction.facing(t);x=t.station().pos.x+direction*7;y=tower.pos.y
            p=m.observation(side);p['robot']={'roles':[{'id':50+i,'pos':pos((x+direction*i,y)),'health':40 if i==0 else 500,'roleType':'smallRobot' if i==0 else 'largeRobot'} for i in range(3)]}
            t=decode(p);aim=combat._attack_targets(t,tower)[0]
            self.assertEqual(aim,Pos(x+direction,y))
            # A remote crowd must not pull aim away from the nearest robot.
            p['robot']['roles'] += [{'id':100+i,'pos':pos((x+direction*5,y+i%3)),'health':500} for i in range(12)]
            self.assertLessEqual(distance(combat._attack_targets(decode(p),replace(tower,level=3))[0],Pos(x,y)),1)

    def test_urgent_robot_is_direct_target(self):
        m=arena();p=m.observation(0);s=decode(p).station();p['robot']={'roles':[{'id':5,'pos':pos((s.pos.x+3,s.pos.y)),'health':40}]}
        t=decode(p);self.assertEqual(combat._attack_targets(t,t.weapons()[0])[0],t.robots[0].pos)

    def test_upgrade_enemy_facing_side_mirrors(self):
        for side in (0,1):
            t=decode(arena(side).observation(side));tower=t.weapons()[0];direction=construction.facing(t)
            front=replace(tower,unit_id=901,pos=Pos(tower.pos.x+direction,tower.pos.y))
            self.assertEqual(sorted([tower,front],key=lambda x:construction.upgrade_order(t,x))[0],front)

    def test_reserve_station_voucher_and_emergency_heal(self):
        for health,expected in ((1500,False),(99,True),(100,False)):
            m=arena();p=m.observation(0);t=decode(p);s=t.station();r=p['teamOur']['roles'][0]
            r['pos']=pos((s.pos.x-1,s.pos.y));r['backpack']=['StationUpgradeVoucher1']
            next(r for r in p['teamOur']['roles'] if r['roleType']=='station')['health']=health
            commands={};self.assertEqual(defense.emergency_upgrade(decode(p),{},set(),commands),expected)
            if expected:self.assertEqual(commands[r['id']]['action'],'use')
            else:
                economy._upgrade(decode(p),decode(p).workers()[0],0,set(),commands,set())
                self.assertFalse(any(c['action']=='use' and c.get('name')=='StationUpgradeVoucher1' for c in commands.values()))

    def test_forecast_triggers_above_100(self):
        m=arena();p=m.observation(0);s=decode(p).station();r=p['teamOur']['roles'][0]
        r['pos']=pos((s.pos.x-1,s.pos.y));r['backpack']=['StationUpgradeVoucher1']
        next(r for r in p['teamOur']['roles'] if r['roleType']=='station')['health']=150
        p['robot']={'roles':[{'id':600+i,'pos':pos((s.pos.x+4,s.pos.y+i)),'health':800,'roleType':'bossRobot'} for i in range(2)]}
        commands={};self.assertTrue(defense.emergency_upgrade(decode(p),{},set(),commands))
        self.assertEqual(commands[r['id']]['action'],'use')

    def test_station_voucher_bought_after_first_weapon_upgrade(self):
        m=arena();p=m.observation(0);t=decode(p);shop=next(p for p,k in t.zones.items() if k=='weaponShop')
        worker=replace(t.workers()[0],pos=Pos(shop.x-1,shop.y));t=replace(t,ours=tuple(replace(r,level=2) if r.unit_id==t.weapons()[0].unit_id else r for r in t.ours))
        commands={};economy._upgrade(t,worker,100,set(),commands,set())
        self.assertEqual(commands[worker.unit_id]['name'],'StationUpgradeVoucher1')

    def test_mine_lock_survives_price_change_and_resets_on_depletion(self):
        m=arena();m.round=140;p=m.observation(0);t=decode(p);worker=t.workers()[1]
        near=Pos(worker.pos.x+2,worker.pos.y+2);far=Pos(worker.pos.x+7,worker.pos.y+2)
        p['mapInfo']['zones']=[{'pos':q.dump(),'neutralType':kind} for q,kind in ((near,'copper'),(far,'iron'))]
        state={};commands={};economy.plan(decode(p),{t.workers()[0].unit_id},set(),commands,state)
        self.assertEqual(state['mine_targets'][str(worker.unit_id)]['target'],near.dump())
        p['vendorShopList']=[{'name':'iron','price':100},{'name':'copper','price':1}]
        economy.plan(decode(p),{t.workers()[0].unit_id},set(),{},state)
        self.assertEqual(state['mine_targets'][str(worker.unit_id)]['target'],near.dump())
        p['mapInfo']['zones']=p['mapInfo']['zones'][1:]
        economy.plan(decode(p),{t.workers()[0].unit_id},set(),{},state)
        self.assertEqual(state['mine_targets'][str(worker.unit_id)]['target'],far.dump())

    def test_night_route_does_not_cross_middle(self):
        m=arena();m.round=71;t=decode(m.observation(0));worker=t.workers()[1]
        self.assertGreaterEqual(route(t,worker,[Pos(t.width-2,worker.pos.y)])[1],10**6)

    def test_night_pioneer_keeps_working_without_nearby_robots(self):
        m=arena();m.round=71;p=m.observation(0)
        pioneer=next(r for r in p['teamOur']['roles'] if r['roleType']=='pioneer')
        pioneer['pos']=pos((p['mapInfo']['width']//2,10));p['phaseTask']='unfinished task'
        out=compute(p).response
        self.assertTrue(out['prompt'])
        self.assertNotIn(str(pioneer['id']),out['roleCommandMap'])

if __name__=='__main__':unittest.main()
