"""Independent rule challenges: textual rules, not simulator assumptions."""
import copy
import io
import json
import unittest
from email.message import Message
from unittest.mock import patch

from agent.brain import decide, _tower_sites
from agent.protocol import Turn, Unit, Pos, station_footprint
from agent.grid import route
from agent import server


def unit(uid, kind, x, y, **extra):
    return dict(id=uid, roleType=kind, pos=dict(x=x, y=y), health=1000,
                backpack=[], **extra)


def observation(round_no=71):
    return dict(roundNo=round_no, mapInfo=dict(width=41, height=32, zones=[]),
                teamOur=dict(type="challenger", teamId="challenge", goldNum=75,
                             roles=[unit(10013, "station", 8, 9, level=1)], playerTasks=[]),
                teamEnemy=dict(roles=[]), robot=dict(roles=[]), phaseTask="", llmResp="")


class RuleChallenges(unittest.TestCase):
    def test_phase_boundaries(self):
        for round_no, expected in [(1, True), (70, True), (71, False),
                                   (130, False), (131, True), (1300, False)]:
            self.assertEqual(Turn.load(observation(round_no)).is_day, expected)

    def test_station_origin_is_upper_left(self):
        self.assertEqual(set(station_footprint(Pos(8, 9))),
                         {Pos(8, 9), Pos(9, 9), Pos(8, 8), Pos(9, 8)})

    def test_contradictory_sample_ranges_are_conservative(self):
        for kind, reported, expected in [("gatling", 4, 3), ("railgun", 7, 6),
                                         ("rocket", 2147483647, 10)]:
            tower = Unit.load(unit(10020, kind, 10, 10, level=1, attackRange=reported))
            self.assertEqual(tower.range_of_attack(), expected)

    def test_gatling_axis_cone_has_no_obtuse_pair(self):
        payload = observation()
        payload["teamOur"]["roles"] += [unit(10010, "worker", 10, 9),
            unit(10020, "gatling", 10, 10, level=3, attackRange=7)]
        payload["robot"]["roles"] = [unit(i, "smallRobot", x, y,
            targetTeam="challenger") for i, (x, y) in enumerate([(11, 10), (11, 13), (11, 7)], 500)]
        attack = decide(payload)["roleCommandMap"].get("10020")
        if attack is not None:
            points = attack["targetPos"]
            self.assertEqual(len(points), 3)
            vectors = [(p["x"] - 10, p["y"] - 10) for p in points]
            for a in vectors:
                self.assertNotEqual(a, (0, 0))
                for b in vectors:
                    self.assertGreaterEqual(a[0] * b[0] + a[1] * b[1], 0)

    def test_cooldown_prevents_rocket_fire(self):
        payload = observation()
        payload["teamOur"]["roles"] += [unit(10010, "worker", 10, 9),
            unit(10040, "rocket", 10, 10, level=3, cooldown=1)]
        payload["robot"]["roles"] = [unit(500, "smallRobot", 12, 10, targetTeam="challenger")]
        response = decide(payload)
        self.assertNotIn("10040", response["roleCommandMap"])

    def test_controller_does_not_receive_separate_action(self):
        payload = observation()
        payload["teamOur"]["roles"] += [unit(10010, "worker", 10, 9),
            unit(10040, "rocket", 10, 10, level=3, cooldown=0)]
        payload["robot"]["roles"] = [unit(500, "smallRobot", 12, 10, targetTeam="challenger")]
        commands = decide(payload)["roleCommandMap"]
        attacks = [cmd for cmd in commands.values() if cmd["action"] == "attack"]
        self.assertTrue(attacks)
        for attack in attacks:
            self.assertNotIn(str(attack["controllerId"]), commands)
            self.assertEqual(len(attack["targetPos"]), 3)

    def test_decision_does_not_mutate_observation(self):
        payload = observation(1)
        before = copy.deepcopy(payload)
        response = decide(payload)
        self.assertEqual(payload, before)
        self.assertEqual(set(response), {"roleCommandMap", "prompt", "executeCmd"})

    def test_existing_off_plan_towers_count_towards_global_limit(self):
        payload = observation(1)
        sites = _tower_sites(Turn.load(payload))
        occupied = set(station_footprint(Pos(8, 9))) | set(sites)
        site = sites[0]
        stand = next(Pos(site.x + dx, site.y + dy) for dx in [-1, 0, 1]
                     for dy in [-1, 0, 1] if Pos(site.x + dx, site.y + dy) not in occupied)
        payload["teamOur"]["roles"] += [unit(10010, "worker", stand.x, stand.y)]
        payload["teamOur"]["roles"] += [unit(10040 + i, "rocket", 25 + i, 25, level=1)
                                             for i in range(3)]
        commands = decide(payload)["roleCommandMap"]
        self.assertFalse(any(c["action"] == "build" and c.get("name") != "wall"
                             for c in commands.values()))

    def test_tower_layout_rotates_with_side(self):
        first = observation(1)
        second = copy.deepcopy(first)
        second["teamOur"]["type"] = "defender"
        second["teamOur"]["roles"][0]["pos"] = dict(x=41-2-8, y=32-9)
        a = _tower_sites(Turn.load(first))
        b = _tower_sites(Turn.load(second))
        self.assertEqual(tuple(Pos(40-p.x, 31-p.y) for p in a), b)

    def test_shared_25_gold_cannot_build_two_towers(self):
        for first_pos, second_pos in [((11, 9), (8, 6)), ((7, 8), (8, 6)),
                                      ((10, 9), (8, 7)), ((7, 9), (10, 8))]:
            payload = observation(1)
            payload["teamOur"]["goldNum"] = 25
            payload["teamOur"]["roles"] += [unit(10010, "worker", *first_pos),
                                                  unit(10012, "worker", *second_pos)]
            builds = [c for c in decide(payload)["roleCommandMap"].values()
                      if c["action"] == "build" and c.get("name") != "wall"]
            self.assertLessEqual(len(builds), 1)

    def test_one_generic_upgrade_ticket_already_in_transit_blocks_purchase(self):
        payload = observation(1)
        payload["teamOur"]["goldNum"] = 1000
        payload["teamOur"]["roles"][0]["level"] = 3
        payload["mapInfo"]["zones"] = [dict(pos=dict(x=5, y=5), neutralType="weaponShop")]
        payload["teamOur"]["roles"] += [unit(10040+i, "rocket", 10+i, 10, level=1) for i in range(3)]
        carrier = unit(10010, "worker", 6, 5)
        carrier["backpack"] = ["WeaponUpgradeVoucher1"]
        payload["teamOur"]["roles"] += [carrier, unit(10012, "worker", 5, 6)]
        self.assertFalse(any(c["action"] == "buy" for c in decide(payload)["roleCommandMap"].values()))

    def test_level_one_gatling_always_has_exactly_one_target(self):
        payload = observation()
        payload["teamOur"]["roles"] += [unit(10010, "worker", 10, 9),
            unit(10020, "gatling", 10, 10, level=1)]
        payload["robot"]["roles"] = [unit(500+i, "smallRobot", 11, 9+i, targetTeam="challenger")
                                     for i in range(3)]
        self.assertEqual(len(decide(payload)["roleCommandMap"]["10020"]["targetPos"]), 1)

    def test_task_echo_rejects_stale_round_and_wrong_task(self):
        payload = observation(10)
        payload["teamOur"]["roles"] += [unit(10011, "pioneer", 5, 5)]
        payload["phaseTask"] = "Return verified data from the sandbox"
        initial = decide(payload)
        context = json.loads(initial["prompt"].split("\n", 1)[1])
        payload["roundNo"] = 11
        for overrides in [dict(roundNo=8), dict(taskKey="wrong")]:
            answer = dict(taskKey=context["taskKey"], roundNo=10, executeCmd="printf task-proof")
            answer.update(overrides)
            payload["llmResp"] = json.dumps(answer)
            self.assertEqual(decide(payload)["executeCmd"], "")
        payload["llmResp"] = json.dumps(dict(taskKey=context["taskKey"], roundNo=10,
                                                 executeCmd="printf task-proof"))
        actual = decide(payload)
        self.assertEqual(actual["executeCmd"], "printf task-proof")
        self.assertEqual(actual["prompt"], "")
        payload["phaseTask"] = ""
        self.assertEqual(decide(payload)["executeCmd"], "")

    def test_unreachable_goal_returns_sentinel(self):
        payload = observation(1)
        worker = unit(10010, "worker", 3, 3)
        payload["teamOur"]["roles"] += [worker]
        payload["mapInfo"]["zones"] = [dict(pos=dict(x=x, y=y), neutralType="stone")
            for x in range(2, 5) for y in range(2, 5) if (x, y) != (3, 3)]
        turn = Turn.load(payload)
        self.assertEqual(route(turn, turn.workers()[0], [Pos(10, 10)]), (None, 10**6))

    def test_http_bad_framing_and_json_return_complete_fallback(self):
        cases = [([], b"{}"), ([("Content-Length", "-1")], b""),
                 ([("Content-Length", "x")], b""),
                 ([("Content-Length", "2"), ("Content-Length", "2")], b"{}"),
                 ([("Content-Length", "2"), ("Transfer-Encoding", "chunked")], b"{}"),
                 ([("Content-Length", "5")], b"{}"),
                 ([("Content-Length", "2")], b"[]"),
                 ([("Content-Length", "1")], b"{")]
        for entries, body in cases:
            handler = object.__new__(server.Handler)
            handler.headers = Message()
            for key, value in entries:
                handler.headers[key] = value
            handler.rfile = io.BytesIO(body)
            handler.connection = __import__("unittest.mock", fromlist=["Mock"]).Mock()
            replies = []
            handler._reply = replies.append
            with patch.object(server.RUNTIME, "decide", side_effect=AssertionError("must not execute")):
                handler.do_POST()
            self.assertEqual(replies, [dict(roleCommandMap={}, prompt="", executeCmd="")])


if __name__ == "__main__":
    unittest.main()
