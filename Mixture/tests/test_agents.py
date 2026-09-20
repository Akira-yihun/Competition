"""Role-agent regressions: assessment, attack value, repair routing, timing, loop guards."""
import json
import unittest
from unittest.mock import patch

from agent.protocol import decode, empty_response
from agent.model import Pos, distance
from agent.navigation import route
from agent.rules import SHOP_PRICES
from agent.world import _neighbours
from agent.policies import construction
from agent.agents import (attack_agent, blackboard, defense_agent, economy_agent,
                          review_agent, self_evolve, task_agent)
from lab.referee import Match, unit, pos


def robot(uid, kind, cell, team='challenger', health=None):
    """Robot payload matching the observation shape (targetTeam decides the victim)."""
    return {'id': uid, 'roleType': kind, 'pos': pos(cell),
            'health': attack_agent.ROBOT_HEALTH[kind] if health is None else health,
            'targetTeam': team}


def arena(side=0):
    """Match with the planned battery already standing, so weapons exist from round 1."""
    m = Match()
    t = decode(m.observation(side))
    for i, cell in enumerate(construction._tower_sites(t)):
        m.teams[side]['roles'].append(unit(10040 + i, 'rocket', (cell.x, cell.y)))
    return m


def turn_at(payload_map, round_no, side=0):
    payload_map.round = round_no
    payload_map.begin()
    return decode(payload_map.observation(side))


class DefenseAgentTests(unittest.TestCase):
    def test_assessment_reports_firepower_damage_and_economy(self):
        m = arena()
        p = m.observation(0)
        p['roundNo'] = 71
        p['robot'] = {'roles': [robot(900, 'smallRobot', (9, 23)),
                                robot(901, 'middleRobot', (9, 24), team='defender')]}
        state = {}
        report = defense_agent.assess(decode(p), state)
        # Only robots that may attack our base are counted (901 is marching on the enemy).
        self.assertEqual(report['firepower']['robots']['total'], 1)
        self.assertEqual(len(report['firepower']['towers']), 3)
        self.assertEqual(report['firepower']['round_damage'], 60)  # three level-1 rockets
        self.assertIn('第71回合', report['summary'])
        self.assertEqual(state['defense_assessment']['round'], 71)
        self.assertEqual(blackboard.read(state, 'defense')['firepower']['round_damage'], 60)

    def test_damaged_wall_gets_breach_estimate(self):
        m = arena()
        t0 = decode(m.observation(0))
        site = construction.priority_wall_sites(t0)[0]
        m.teams[0]['roles'].append({**unit(40000, 'wall', (site.x, site.y)), 'health': 300})
        p = m.observation(0)
        p['roundNo'] = 140
        p['robot'] = {'roles': [robot(900, 'middleRobot', (site.x + 2, site.y))]}
        report = defense_agent.assess(decode(p), {})
        row = next(r for r in report['damage']['walls'] if r['id'] == 40000)
        self.assertEqual(row['max'], 1000)
        self.assertEqual(row['ratio'], 0.3)
        self.assertEqual(row['breach_rounds'], 30)  # 300 hp / 10 damage per round

    def test_stone_reserve_is_small_and_capped(self):
        m = arena()
        self.assertEqual(defense_agent.stone_reserve(decode(m.observation(0)), {}),
                         defense_agent.MAX_STONE_RESERVE)

    def test_repair_order_is_nearest_neighbour(self):
        m = arena()
        t = decode(m.observation(0))
        worker = t.workers()[0]
        sites = list(construction.priority_wall_sites(t))[:3]
        order, total = defense_agent.repair_order(t, worker, sites, set())
        self.assertEqual(len(order), 3)
        self.assertEqual([item['cumulative'] for item in order],
                         sorted(item['cumulative'] for item in order))
        lengths = {site: min(route(t, worker, [p], set(), cautious=False)[1]
                             for p in _neighbours(site) if t.land(p)) for site in sites}
        self.assertEqual(lengths[Pos.load(order[0]['site'])], min(lengths.values()))
        self.assertGreaterEqual(total, max(lengths.values()))

    def test_work_plan_puts_the_critical_wall_first(self):
        m = arena()
        t0 = decode(m.observation(0))
        site = construction.priority_wall_sites(t0)[0]
        m.teams[0]['roles'].append({**unit(40000, 'wall', (site.x, site.y)), 'health': 300})
        m.teams[0]['roles'][0]['backpack'] = ['WallFixer']
        p = m.observation(0)
        p['roundNo'] = 140
        p['robot'] = {'roles': [robot(900, 'middleRobot', (site.x + 2, site.y))]}
        t = decode(p)
        state = {}
        plan = defense_agent.work_plan(t, state, t.workers()[0])
        self.assertEqual(plan['jobs'][0]['kind'], 'repair_wall')
        self.assertEqual(plan['jobs'][0]['target'], site.dump())
        self.assertEqual(plan['jobs'][0]['name'], 'WallFixer')
        self.assertIsNotNone(plan['jobs'][0]['eta'])
        self.assertLessEqual(plan['stone_reserve'], defense_agent.MAX_STONE_RESERVE)
        self.assertIn('defense_plan', blackboard.snapshot(state))

    def test_task_switch_is_logged_with_reason(self):
        m = arena()
        t = decode(m.observation(0))
        worker = t.workers()[0]
        state = {}
        plan = defense_agent.work_plan(t, state, worker)
        # First job of the day: nothing to switch from, so no switch is reported.
        self.assertFalse(defense_agent.log_reason(state, plan)['switched'])
        self.assertFalse(defense_agent.log_reason(state, plan)['switched'])
        # Explicit previous job -> the ladder reports from/to plus the new reason.
        switch = defense_agent.log_reason(state, plan, previous={'kind': 'mine', 'target': None})
        self.assertTrue(switch['switched'])
        self.assertEqual(switch['from']['kind'], 'mine')
        self.assertTrue(switch['reason'])


class AttackAgentTests(unittest.TestCase):
    def test_only_our_base_attackers_are_threats(self):
        m = arena()
        p = m.observation(0)
        p['robot'] = {'roles': [robot(900, 'smallRobot', (10, 20), team='challenger'),
                                robot(901, 'smallRobot', (10, 21), team='defender'),
                                robot(902, 'smallRobot', (10, 22), team='')]}
        ids = {r.robot_id for r in attack_agent.threats(decode(p))}
        self.assertEqual(ids, {900, 902})

    def test_splash_and_overkill_are_valued_correctly(self):
        m = arena()
        p = m.observation(0)
        station = decode(p).station().pos
        p['robot'] = {'roles': [robot(900, 'smallRobot', (station.x + 5, station.y), health=10),
                                robot(901, 'smallRobot', (station.x + 5, station.y + 1), health=10)]}
        t = decode(p)
        live = list(attack_agent.threats(t))
        remaining = {r.robot_id: r.health for r in t.robots}
        footprint = attack_agent.base_footprint(t)
        crowd = attack_agent.aim_value(t, Pos(station.x + 5, station.y), live, remaining, footprint)
        lonely = attack_agent.aim_value(t, Pos(station.x + 9, station.y + 6), live, remaining, footprint)
        self.assertEqual(crowd['hits'], 2)
        self.assertEqual(crowd['kills'], 2)
        self.assertEqual(crowd['splash'], attack_agent.SPLASH_BONUS)
        self.assertGreater(crowd['score'], lonely['score'])
        self.assertEqual(lonely['hits'], 0)

    def test_damage_never_exceeds_remaining_health(self):
        m = arena()
        p = m.observation(0)
        station = decode(p).station().pos
        p['robot'] = {'roles': [robot(900, 'smallRobot', (station.x + 5, station.y), health=5)]}
        t = decode(p)
        live = list(attack_agent.threats(t))
        remaining = {r.robot_id: r.health for r in t.robots}
        value = attack_agent.aim_value(t, Pos(station.x + 5, station.y), live, remaining,
                                       attack_agent.base_footprint(t))
        self.assertEqual(value['damage'], 5)  # 20 centre damage on a 5 hp robot counts as 5

    def test_choose_returns_legal_aims_with_analysis(self):
        m = arena()
        p = m.observation(0)
        station = decode(p).station().pos
        p['robot'] = {'roles': [robot(900, 'largeRobot', (station.x + 5, station.y)),
                                robot(901, 'largeRobot', (station.x + 6, station.y))]}
        t = decode(p)
        tower = t.weapons()[0]
        aims, analysis = attack_agent.choose(t, tower, {})
        self.assertEqual(len(aims), 1)  # level-1 rocket: one aim
        self.assertTrue(all(0 < distance(tower.pos, aim) <= tower.range_of_attack() for aim in aims))
        self.assertGreater(analysis['damage'], 0)
        self.assertIn(analysis['mode'], ('rocket_value', 'geometry'))
        # A robot aiming at the enemy base must not become our target priority.
        p['robot'] = {'roles': [robot(900, 'largeRobot', (station.x + 5, station.y), team='defender')]}
        aims, analysis = attack_agent.choose(decode(p), tower, {})
        self.assertEqual(analysis['mode'], 'no_threat')


class EconomyAgentTests(unittest.TestCase):
    def test_sell_timing_orders_night_forecast_vendor_and_funding(self):
        m = Match()
        t = decode(m.observation(0))
        miner = t.workers()[1]
        vendor = t.vendor()
        p = m.observation(0)
        p['teamOur']['roles'][2]['backpack'] = ['copper'] * 4
        t = decode(p)
        miner = t.workers()[1]
        # Night: keep mining (routing to the vendor in the dark is not worth it).
        p['roundNo'] = 80
        night = economy_agent.sell_timing(decode(p), {}, miner)
        self.assertFalse(night['sell'])
        self.assertEqual(night['reason'], 'night_keep_mining')
        # Day, standing next to the vendor: sell.
        p['roundNo'] = 131
        p['teamOur']['roles'][2]['pos'] = pos((vendor.x - 1, vendor.y))
        t = decode(p)
        near = economy_agent.sell_timing(t, {}, t.workers()[1])
        self.assertTrue(near['sell'])
        self.assertEqual(near['reason'], 'at_vendor')
        # A live forecast beats the convenience of standing at the vendor.
        state = {'intelligence': {'market': [{'ore': 'copper', 'startRound': 131, 'endRound': 300,
                                              'price': 9, 'direction': 'up', 'available': True,
                                              'holdUntil': 200, 'confidence': .9, 'evidence': 'x'}]}}
        t = decode(p)
        held = economy_agent.sell_timing(t, state, t.workers()[1])
        self.assertFalse(held['sell'])
        self.assertIn('200', held['reason'])
        # Funding gap on a day trip: sell now even without a vendor neighbour.
        p['teamOur']['roles'][2]['pos'] = pos((20, 20))
        p['teamOur']['goldNum'] = 95
        t = decode(p)
        with patch.object(economy_agent, 'funding_need', return_value=100):
            funded = economy_agent.sell_timing(t, {}, t.workers()[1])
        self.assertTrue(funded['sell'])
        self.assertIn('defense_funding', funded['reason'])

    def test_route_to_avoids_the_robot_attack_ring(self):
        from dataclasses import replace
        m = Match()
        t = decode(m.observation(0))
        miner = t.workers()[1]
        # Start outside the ring so the detour is a real choice, not a forced step.
        row = next(r for r in range(1, t.height - 1)
                   if all(t.land(Pos(miner.pos.x + dx, r)) for dx in (0, 5, 10)))
        start = Pos(miner.pos.x, row)
        guard_bot = Pos(start.x + 5, start.y)
        target = Pos(start.x + 10, start.y)
        p = m.observation(0)
        p['robot'] = {'roles': [robot(900, 'smallRobot', (guard_bot.x, guard_bot.y))]}
        t = decode(p)
        worker = replace(next(r for r in t.workers() if r.unit_id == miner.unit_id), pos=start)
        step, length = economy_agent.route_to(t, worker, target, set())
        self.assertIsNotNone(step)
        self.assertGreater(distance(step, guard_bot), attack_agent.ROBOT_RANGE + economy_agent.ROBOT_MARGIN)
        self.assertLess(length, 10**6)

    def test_survey_ranks_mines_and_records_prices(self):
        m = Match()
        t = decode(m.observation(0))
        state = {}
        survey = economy_agent.survey(t, state, t.workers()[1])
        self.assertTrue(survey['mines'])
        self.assertEqual(survey['prices'], {'stone': 1, 'iron': 2, 'copper': 3})
        self.assertEqual(state['price_history'][0]['round'], t.round_no)

    def test_funding_need_counts_the_next_voucher(self):
        m = arena()
        t = decode(m.observation(0))
        need = economy_agent.funding_need(t, {})
        self.assertGreaterEqual(need, SHOP_PRICES['StationUpgradeVoucher1'])


class TaskAgentTests(unittest.TestCase):
    def test_wall_fixer_is_used_on_a_critical_wall(self):
        m = arena()
        t0 = decode(m.observation(0))
        site = construction.priority_wall_sites(t0)[0]
        m.teams[0]['roles'].append({**unit(40000, 'wall', (site.x, site.y)), 'health': 200})
        p = m.observation(0)
        p['roundNo'] = 75
        p['robot'] = {'roles': [robot(900, 'middleRobot', (site.x + 2, site.y))]}
        pioneer = next(r for r in p['teamOur']['roles'] if r['roleType'] == 'pioneer')
        pioneer['pos'] = pos((site.x + 1, site.y))
        pioneer['backpack'] = ['WallFixer']
        t = decode(p)
        unit_pioneer = next(r for r in t.controllable() if r.kind == 'pioneer')
        commands = {}
        state = {}
        self.assertTrue(task_agent.use_wall_fixer(t, unit_pioneer, state, set(), commands))
        self.assertEqual(commands[unit_pioneer.unit_id]['action'], 'use')
        self.assertEqual(commands[unit_pioneer.unit_id]['name'], 'WallFixer')
        self.assertEqual(commands[unit_pioneer.unit_id]['targetPos'], [site.dump()])

    def test_pioneer_ignores_robots_marching_on_the_enemy(self):
        from agent.policies import pioneer as pioneer_policy
        m = Match()
        p = m.observation(0)
        p['roundNo'] = 80
        p['phaseTask'] = 'task'
        p['teamOur']['playerTasks'] = [{'taskPosition': pos((10, 20)), 'isValid': False, 'timeoutRounds': 30}]
        p['teamOur']['roles'][1]['pos'] = pos((10, 19))
        # Same geometry as test_v07, but this robot attacks the enemy base.
        p['robot'] = {'roles': [robot(99, 'smallRobot', (14, 19), team='defender')]}
        t = decode(p)
        pioneer = next(r for r in t.controllable() if r.kind == 'pioneer')
        commands = {}
        self.assertFalse(pioneer_policy.safety(t, pioneer, {}, set(), commands))
        self.assertFalse(commands)

    def test_has_task_work_follows_phase_and_validity(self):
        m = Match()
        t = decode(m.observation(0))
        self.assertTrue(task_agent.has_task_work(t))
        p = m.observation(0)
        p['teamOur']['playerTasks'] = []
        self.assertFalse(task_agent.has_task_work(decode(p)))

    def test_support_buys_a_repair_pack_with_surplus_gold(self):
        m = arena()
        t0 = decode(m.observation(0))
        site = construction.priority_wall_sites(t0)[0]
        m.teams[0]['roles'].append({**unit(40000, 'wall', (site.x, site.y)), 'health': 400})
        shop = next(q for q, kind in t0.zones.items() if kind == 'weaponShop')
        p = m.observation(0)
        p['roundNo'] = 140
        p['teamOur']['goldNum'] = 400
        pioneer = next(r for r in p['teamOur']['roles'] if r['roleType'] == 'pioneer')
        pioneer['pos'] = pos((shop.x - 1, shop.y))
        t = decode(p)
        unit_pioneer = next(r for r in t.controllable() if r.kind == 'pioneer')
        commands = {}
        state = {}
        self.assertTrue(task_agent.buy_for_defense(t, unit_pioneer, state, set(), commands))
        self.assertEqual(commands[unit_pioneer.unit_id]['action'], 'buy')
        self.assertEqual(commands[unit_pioneer.unit_id]['name'], 'WallFixer')

    def test_support_keeps_gold_for_the_defender(self):
        m = arena()
        t0 = decode(m.observation(0))
        site = construction.priority_wall_sites(t0)[0]
        m.teams[0]['roles'].append({**unit(40000, 'wall', (site.x, site.y)), 'health': 400})
        shop = next(q for q, kind in t0.zones.items() if kind == 'weaponShop')
        p = m.observation(0)
        p['roundNo'] = 140
        p['teamOur']['goldNum'] = SHOP_PRICES['WallFixer']  # enough for the pack, not for the reserve
        pioneer = next(r for r in p['teamOur']['roles'] if r['roleType'] == 'pioneer')
        pioneer['pos'] = pos((shop.x - 1, shop.y))
        t = decode(p)
        unit_pioneer = next(r for r in t.controllable() if r.kind == 'pioneer')
        commands = {}
        self.assertFalse(task_agent.buy_for_defense(t, unit_pioneer, {}, set(), commands))
        self.assertFalse(commands)

    def test_support_delivers_a_carried_weapon_voucher(self):
        m = arena()
        t = decode(m.observation(0))
        tower = t.weapons()[0]
        p = m.observation(0)
        p['roundNo'] = 140
        pioneer = next(r for r in p['teamOur']['roles'] if r['roleType'] == 'pioneer')
        pioneer['backpack'] = ['WeaponUpgradeVoucher1']
        # Standing next to the tower: use it immediately.
        pioneer['pos'] = pos((tower.pos.x + 1, tower.pos.y))
        t = decode(p)
        unit_pioneer = next(r for r in t.controllable() if r.kind == 'pioneer')
        commands = {}
        self.assertTrue(task_agent.use_held(t, unit_pioneer, {}, set(), commands))
        self.assertEqual(commands[unit_pioneer.unit_id]['name'], 'WeaponUpgradeVoucher1')
        self.assertEqual(commands[unit_pioneer.unit_id]['action'], 'use')
        # Far away: walk towards the tower instead of idling with the voucher.
        p['teamOur']['roles'][1]['pos'] = pos((2, 20))
        t = decode(p)
        unit_pioneer = next(r for r in t.controllable() if r.kind == 'pioneer')
        commands = {}
        self.assertTrue(task_agent.use_held(t, unit_pioneer, {}, set(), commands))
        self.assertEqual(commands[unit_pioneer.unit_id]['action'], 'move')

    def test_support_does_not_buy_vouchers_without_surplus(self):
        """The pioneer must never strand gold the defender needs for its own upgrade."""
        m = arena()
        p = m.observation(0)
        p['roundNo'] = 140
        p['teamOur']['goldNum'] = SHOP_PRICES['WeaponUpgradeVoucher1'] + 20  # no surplus margin
        shop = next(q for q, kind in decode(p).zones.items() if kind == 'weaponShop')
        pioneer = next(r for r in p['teamOur']['roles'] if r['roleType'] == 'pioneer')
        pioneer['pos'] = pos((shop.x - 1, shop.y))
        t = decode(p)
        unit_pioneer = next(r for r in t.controllable() if r.kind == 'pioneer')
        commands = {}
        self.assertFalse(task_agent.buy_for_defense(t, unit_pioneer, {}, set(), commands))
        self.assertFalse(commands)


class SelfEvolveTests(unittest.TestCase):
    def turn(self, round_no=5):
        m = Match()
        m.round = round_no
        m.begin()
        return decode(m.observation(0))

    def task(self, round_no=1):
        return {'timeout': 30, 'accepted_round': round_no, 'text': 't',
                'se': self_evolve.new_state('t')}

    def test_repeat_and_budget_guards(self):
        turn = self.turn()
        task = self.task()
        self.assertIsNone(self_evolve.guard_reason(turn, task))
        for _ in range(self_evolve.REPEAT_LIMIT):
            self_evolve.note_command(task, 'ls', 'p', turn.round_no)
        self.assertEqual(self_evolve.guard_reason(turn, task)[0], 'repeated_command')
        task = self.task()
        for index in range(self_evolve.MAX_STEPS):
            self_evolve.note_command(task, f'cmd{index}', 'p', turn.round_no)
        self.assertEqual(self_evolve.guard_reason(turn, task)[0], 'budget_exhausted')
        self.assertTrue(self_evolve.answer_only(turn, task))
        self.assertTrue(self_evolve.stop_reason(turn, task).startswith('budget_exhausted'))
        context = self_evolve.build_context({}, turn, task, {})
        self.assertIn('stopReason', context)
        self.assertIn('progress', context)

    def test_no_progress_and_low_rounds_guards(self):
        turn = self.turn(6)
        task = self.task()
        for index in range(3):
            step = self_evolve.note_command(task, f'c{index}', 'p', turn.round_no)
            step['result'] = 'identical'
        self.assertEqual(self_evolve.guard_reason(turn, task)[0], 'no_progress')
        late = self.turn(30)
        self.assertEqual(self_evolve.guard_reason(late, self.task())[0], 'low_remaining_rounds')

    def test_parse_action_never_raises(self):
        pending = {'kind': 'model', 'id': 'r', 'round': 1}
        for text in ('', 'not json', '{broken', '{"type":"weird"}', '```json\n{"taskAnswer":"42"}\n```', None):
            parsed, reason, purpose, findings = self_evolve.parse_action(text, pending, 'inst')
            self.assertIsInstance(reason, str)
            self.assertIsInstance(findings, list)
        self.assertEqual(self_evolve.parse_action('{"taskAnswer":"42"}', pending, 'inst')[0], ('answer', '42'))
        self.assertEqual(self_evolve.parse_action('{"executeCmd":"ls","purpose":"看目录"}', pending, 'inst')[2], '看目录')

    def test_summary_extraction_is_bounded_and_safe(self):
        self.assertIsNone(self_evolve.summary_from('{broken'))
        self.assertIsNone(self_evolve.summary_from('{"taskAnswer":"42"}'))
        big = {'taskFamily': 'x' * 20000}
        self.assertIsNone(self_evolve.summary_from(json.dumps({'taskSummary': big})))
        self.assertEqual(self_evolve.summary_from(json.dumps({'taskSummary': {'taskFamily': 'ok'}})),
                         {'taskFamily': 'ok'})


class ReviewAgentTests(unittest.TestCase):
    def test_flags_bad_command_and_channel_conflict(self):
        m = Match()
        t = decode(m.observation(0))
        findings = review_agent.review_command(t, 10010, {'action': 'attack', 'targetPos': [pos((5, 5))]})
        codes = {f['code'] for f in findings}
        self.assertIn('missing_controller', codes)
        response = {**empty_response(), 'prompt': 'x', 'executeCmd': 'ls',
                    'roleCommandMap': {'10010': {'action': 'collect', 'targetPos': [pos((2, 20))]}}}
        codes = {f['code'] for f in review_agent.review_response(t, response)}
        self.assertIn('channel_conflict', codes)

    def test_clean_response_and_blackboard_entry(self):
        m = Match()
        t = decode(m.observation(0))
        response = {**empty_response(), 'roleCommandMap': {'10010': {'action': 'collect', 'targetPos': [pos((2, 20))]}}}
        state = {}
        self.assertFalse(review_agent.audit(t, response, state))
        self.assertTrue(blackboard.read(state, 'review')['clean'])

    def test_model_action_findings(self):
        codes = {f['code'] for f in review_agent.review_action('{broken')}
        self.assertIn('malformed_json', codes)
        codes = {f['code'] for f in review_agent.review_action('{"type":"command"}')}
        self.assertIn('missing_command', codes)
        codes = {f['code'] for f in review_agent.review_action('{"executeCmd":"ls","taskAnswer":"42"}')}
        self.assertIn('action_conflict', codes)


class BlackboardTests(unittest.TestCase):
    def test_round_trip_history_and_truncation(self):
        state = {}
        blackboard.write(state, 'defense', {'a': 1}, 5)
        blackboard.write(state, 'economy', {'b': 2}, 5)
        self.assertEqual(blackboard.read(state, 'defense'), {'a': 1})
        self.assertEqual(sorted(blackboard.snapshot(state)), ['defense', 'economy'])
        blackboard.write(state, 'task', {'big': 'x' * 20000}, 6)
        self.assertTrue(blackboard.read(state, 'task')['truncated'])
        self.assertLessEqual(len(blackboard.log_summary(state)['task']['summary']), 1200)


class EngineIntegrationTests(unittest.TestCase):
    """Whole-round regressions: agent code must never make the decision fall back."""

    def test_work_plan_handles_a_carried_station_voucher(self):
        m = arena()
        m.teams[0]['roles'][0]['backpack'] = ['StationUpgradeVoucher1']
        p = m.observation(0)
        p['roundNo'] = 140
        next(u for u in p['teamOur']['roles'] if u['roleType'] == 'station')['health'] = 90
        t = decode(p)
        plan = defense_agent.work_plan(t, {}, t.workers()[0])
        self.assertEqual(plan['jobs'][0]['kind'], 'use')
        self.assertEqual(plan['jobs'][0]['name'], 'StationUpgradeVoucher1')

    def test_a_full_day_never_falls_back_to_an_empty_response(self):
        """Regression: a KeyError in the defence work plan froze whole day rounds."""
        from agent.state import Session
        m = arena()
        m.teams[0]['roles'][0]['backpack'] = ['StationUpgradeVoucher1']
        session = Session()
        for _ in range(140):
            m.begin()
            response = session.decide(m.observation(0))
            m.step([response, empty_response()])
        self.assertFalse(session.state.get('fallbacks'))
        self.assertFalse(m.teams[0]['errors'])
        self.assertTrue(session.state['role_plans'])


if __name__ == '__main__':
    unittest.main()
