#!/usr/bin/env python3
"""Deterministic, approximate local referee. Never executes agent executeCmd."""
from __future__ import annotations
import argparse
import copy
import json
import hashlib
import time
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
from agent.brain import decide as candidate
from agent.state import Session
from .rules import xy, pos, dist, cells, resolve_moves, on_line
from .task_fixtures import mock_llm
from baseline_agent.brain import decide as baseline

TOWERS = ('gatling', 'railgun', 'rocket')
MOBILE = ('worker', 'pioneer')
RANGES = {'gatling': (3, 5, 7), 'railgun': (6, 8, 10), 'rocket': (10, 15, 99)}
PRICES = {f'{kind}UpgradeVoucher{level}': price for kind, costs in [('Weapon', (100, 150)), ('Station', (100, 150)), ('Wall', (20, 30))] for level, price in enumerate(costs, 1)}
ROBOT = {'smallRobot': (40, 5, 1), 'middleRobot': (60, 10, 2), 'largeRobot': (500, 20, 4), 'bossRobot': (800, 40, 10)}
LIMITATIONS = [
    'Approximate local referee, NOT official win rate or official judgement.',
    'Synthetic seeded maps, mine prices, robot wave counts/spawns and greedy routing; official generators are unavailable.',
    'Reasoning profile exposes the same answer/latency to both protocols. Tools profile is a mechanism test only, not a comparative score.',
    'Synthetic tasks have answer 42, 50 score/60 gold, 30-round timeout, 30-round refresh, 8 tasks per point; deterministic mock LLM, no real LLM.',
    'executeCmd is NEVER executed: every task command receives an explicit fixture output. No actual sandbox/API task solving is assessed.',
    'Gatling/railgun lines use cell-centre supercover; official boundary/intersection details are unknown. Friendly-fire and PvP damage are unsupported.',
    'Treasure, news-driven disruptions, robot summon orders and non-upgrade consumables are unsupported; emitted actions fail explicitly.',
    'Same-turn economy resolves by ascending role ID; moving into a vacated cell is allowed unless a swap/collision occurs. Role/robot cross-motion is conservative.',
    'Robot AI targets nearest opposing obstruction within range 3, otherwise greedily approaches station; not the official robot AI.',
    'Round numbers start at 1; task-point-2 second-cell placement, spawn positions and damage tie credit are local assumptions.',
    'If exactly one base is destroyed at the round limit, the surviving base wins locally; the official text does not explicitly resolve this case.',
    'Robot/role moves are not planned jointly; greedy robot congestion and last-hit side credit can alter survival and combat scores.',
    'One win plus one draw is treated as a local match win; two draws use total scores. Official mixed-draw wording is underspecified.',
    'Calls decide directly with JSON-shaped requests; HTTP timing/network behavior must be tested separately.',
]












def unit(uid, kind, p):
    hp = {'station': 1500, 'worker': 220, 'pioneer': 200}.get(kind, 1000)
    return dict(id=uid, roleType=kind, pos=pos(p), health=hp, level=1, cooldown=0,
                attackRange=RANGES.get(kind, (0,))[0], attackPower=20 if kind == 'rocket' else 10 if kind in TOWERS else 0,
                backPackCapability=100 if kind == 'worker' else 40 if kind == 'pioneer' else 0, backpack=[])






class Match:
    def __init__(self, seed=1, task_profile="reasoning"):
        self.task_profile = task_profile
        self.rng = random.Random(seed)
        self.round = 1
        self.next_robot = 90000
        self.zones = []
        self.mines = {}
        self.robots = []
        self.teams = []
        self.metrics = [Counter(), Counter()]
        for side, kind in enumerate(('challenger', 'defender')):
            base = (5, 26) if side == 0 else (34, 6)
            prefix = 10000 + side * 10000
            spawn = [(4, 25), (4, 24), (5, 24)] if side == 0 else [(36, 6), (36, 7), (35, 7)]
            roles = [unit(prefix + 10, 'worker', spawn[0]), unit(prefix + 11, 'pioneer', spawn[1]), unit(prefix + 12, 'worker', spawn[2]), unit(prefix + 13, 'station', base)]
            taskpos = [(11, 25), (11, 20)] if side == 0 else [(29, 6), (29, 11)]
            tasks = [dict(taskType=f'自进化类{n + 1}', taskPosition=pos(p), coldDownRounds=0, scoreReward=50, goldReward=60, isValid=True, timeoutRounds=30, remaining=8) for n, p in enumerate(taskpos)]
            self.teams.append(dict(type=kind, teamId=str(side), teamName=kind, goldNum=75, totalScore=0, roles=roles, playerTasks=tasks, active=None, phaseTask='', llmResp='', lastCmdResult='', results={}, errors=[], death=None, graves=[], llm_calls=0, exceptions=0))
            for n, p in enumerate(taskpos):
                self.zones.append(dict(pos=pos(p), neutralType=f'{kind}TaskPoint{n + 1}'))
            self.zones.append(dict(pos=pos((taskpos[1][0], taskpos[1][1] + (1 if side == 0 else -1))), neutralType=f'{kind}TaskPoint2'))
        self.zones += [dict(pos=pos((20, 16)), neutralType='vendor'), dict(pos=pos((20, 14)), neutralType='weaponShop')]
        for kind in ('stone', 'iron', 'copper'):
            for _ in range(8):
                self.spawn_mine(kind)

    def spawn_mine(self, kind):
        occupied = {xy(z) for z in self.zones} | {p for t in self.teams for r in t['roles'] for p in cells(r)} | {xy(r) for r in self.robots}
        bases = [next((r for r in t['roles'] if r['roleType'] == 'station'), None) for t in self.teams]
        for _ in range(10000):
            p = (self.rng.randrange(41), self.rng.randrange(32))
            if p not in occupied and all(b is None or min(dist(p, c) for c in cells(b)) > 2 for b in bases):
                self.zones.append(dict(pos=pos(p), neutralType=kind))
                self.mines[p] = 10
                return

    def begin(self):
        phase = (self.round - 1) % 130
        day = (self.round - 1) // 130 + 1
        if phase == 0:
            self.robots = []
            for t in self.teams:
                t['llm_calls'] = 0
        if phase == 70:
            for side, t in enumerate(self.teams):
                kinds = ['smallRobot'] * (3 + day * 2) + ['middleRobot'] * max(0, day - 1) + ['largeRobot'] * max(0, day - 3) + ['bossRobot'] * max(0, day - 7)
                for n, kind in enumerate(kinds):
                    p = (18 + n % 5, 5 + n // 5) if side == 0 else (18 + n % 5, 26 - n // 5)
                    self.robots.append(dict(id=self.next_robot, roleType=kind, pos=pos(p), health=ROBOT[kind][0], abnormalState='', targetTeam=t['type']))
                    self.next_robot += 1
        for side, t in enumerate(self.teams):
            for task in t['playerTasks']:
                task['coldDownRounds'] = max(0, task['coldDownRounds'] - 1)
                task['isValid'] = task['remaining'] > 0 and task['coldDownRounds'] == 0
            if t['active'] is not None and self.round - t['active']['start'] > 30:
                self.end_task(t)
                t['errors'].append(dict(errorCode=1, description='fixture task timeout'))
            if phase == 20 and t['death'] is None:
                station = next(r for r in t['roles'] if r['roleType'] == 'station')
                occupied = {xy(z) for z in self.zones} | {p for team in self.teams for r in team['roles'] for p in cells(r)}
                for r in t['graves']:
                    options = sorted(((x, y) for x in range(41) for y in range(32) if (x, y) not in occupied), key=lambda p: (dist(p, xy(station)), p))
                    if options:
                        r['pos'] = pos(options[0]); r['health'] = 220 if r['roleType'] == 'worker' else 200
                        occupied.add(options[0]); t['roles'].append(r)
                t['graves'] = []

    def observation(self, side):
        t, enemy = self.teams[side], self.teams[1-side]
        visible = [r for r in enemy['roles'] if r['roleType'] in ('station', 'wall') or any(min(dist(xy(r), p) for p in cells(u)) <= 4 for u in t['roles'])]
        return copy.deepcopy(dict(roundNo=self.round, mapInfo=dict(width=41, height=32, zones=self.zones), teamOur={k: t[k] for k in ('type', 'teamId', 'teamName', 'goldNum', 'totalScore', 'roles', 'playerTasks')}, teamEnemy=dict(roles=visible), robot=dict(roles=self.robots), phaseTask=t['phaseTask'], llmResp=t['llmResp'], lastCmdResult=t['lastCmdResult'], lastRoundRoleActionResults=t['results'], lastSummonTreasureResult=0, worldNews=dict(officialNews='LOCAL FIXTURE: fixed mineral prices.', folkLegends=''), vendorShopList=[dict(name=k, price=v) for k, v in [('stone', 1), ('iron', 2), ('copper', 3)]], weaponShopList=[dict(name=k, price=v) for k, v in PRICES.items()], errors=t['errors']))

    def end_task(self, t):
        if t['active'] is not None:
            task = t['playerTasks'][t['active']['index']]
            task['coldDownRounds'] = 31
            task['remaining'] -= 1
            task['isValid'] = False
        t['active'] = None; t['phaseTask'] = ''

    def step(self, responses):
        damage = defaultdict(int)
        credit = {}
        proposals, positions = {}, {r['id']: xy(r) for t in self.teams for r in t['roles'] if r['roleType'] in MOBILE}
        mobile_lookup = {r['id']: (s, r) for s, t in enumerate(self.teams) for r in t['roles'] if r['roleType'] in MOBILE}
        static = {xy(z) for z in self.zones} | {p for t in self.teams for r in t['roles'] if r['roleType'] not in MOBILE for p in cells(r)} | {xy(r) for r in self.robots}
        consumed_mines = Counter()
        for side, (t, response) in enumerate(zip(self.teams, responses)):
            t['results'] = {}; t['errors'] = []; t['llmResp'] = ''; t['lastCmdResult'] = ''
            used = set()
            malformed = (not isinstance(response, dict)
                         or not isinstance(response.get('roleCommandMap'), dict)
                         or not isinstance(response.get('prompt'), str)
                         or not isinstance(response.get('executeCmd'), str))
            # Reject the entire malformed envelope before causing any side effects.
            if malformed:
                response = dict(roleCommandMap={}, prompt='', executeCmd='')
            commands = response['roleCommandMap']
            for key, command in sorted(commands.items(), key=lambda kv: str(kv[0])):
                self.metrics[side]['commands'] += 1
                ok = False
                try:
                    rid = int(key); r = next((u for u in t['roles'] if u['id'] == rid), None)
                    if not isinstance(command, dict): raise ValueError('command must be an object')
                    action = command['action']
                    targets = command.get('targetPos', [])
                    if not isinstance(targets, list) or any(not isinstance(p, dict) or type(p.get('x')) is not int or type(p.get('y')) is not int for p in targets):
                        raise ValueError('targetPos must contain integer coordinates')
                    if action != 'attack' and len(targets) > 1:
                        raise ValueError('action requires at most one target')
                    for field in ('name', 'taskAnswer', 'controllerId'):
                        if field in command and not isinstance(command[field], str):
                            raise ValueError(field + ' must be a string')
                    if 'num' in command and type(command['num']) is not int:
                        raise ValueError('num must be an integer')
                    if 'item' in command and (not isinstance(command['item'], list) or any(not isinstance(item, str) for item in command['item'])):
                        raise ValueError('item must be a list of strings')
                    if action not in ('move', 'attack', 'sell', 'buy', 'build', 'remove', 'acceptTask', 'submitAnswer', 'summonTreasure', 'use', 'drop', 'collect'):
                        raise ValueError('unknown action')
                    if action in ('move', 'attack', 'build', 'remove', 'collect', 'summonTreasure') and not targets:
                        raise ValueError('missing targetPos')
                    if action in ('build', 'sell', 'buy', 'use', 'drop') and not command.get('name'):
                        raise ValueError('missing name')
                    if action == 'attack' and not command.get('controllerId'):
                        raise ValueError('missing controllerId')
                    if action == 'submitAnswer' and 'taskAnswer' not in command:
                        raise ValueError('missing taskAnswer')
                    if action == 'use' and (command.get('name') in PRICES or command.get('name') in ('WallFixer', 'Bomb', 'DizzyWeapon')) and not targets:
                        raise ValueError('use requires targetPos')
                    if action == 'summonTreasure' and 'item' not in command:
                        raise ValueError('summonTreasure requires item')
                    target = xy(targets[0]) if targets else None
                    if r is not None:
                        if action == 'move' and r['roleType'] in MOBILE and rid not in used:
                            proposals[rid] = target
                            ok = True
                        elif action == 'attack':
                            controller = next((u for u in t['roles'] if str(u['id']) == str(command.get('controllerId')) and u['roleType'] in MOBILE), None)
                            points = [xy(p) for p in targets]
                            kind = r['roleType']
                            count = 1 if kind == 'railgun' else r['level']
                            ok = kind in TOWERS and (self.round-1)%130 >= 70 and r['cooldown'] == 0 and controller is not None and controller['id'] not in used and str(controller['id']) not in commands and dist(xy(controller), xy(r)) <= 1 and len(points) == count and all(0 <= p[0] < 41 and 0 <= p[1] < 32 and dist(xy(r), p) <= r['attackRange'] and (kind == 'rocket' or p != xy(r)) for p in points)
                            vectors = [(p[0]-xy(r)[0], p[1]-xy(r)[1]) for p in points]
                            if kind == 'gatling' and any(a[0]*b[0]+a[1]*b[1] < 0 for a in vectors for b in vectors): ok = False
                            if ok:
                                used.add(controller['id'])
                                hit = False
                                for p in points:
                                    if kind == 'rocket':
                                        for robot in self.robots:
                                            d = dist(xy(robot), p)
                                            if d <= 1:
                                                damage[robot['id']] += 20 if d == 0 else 10
                                                credit[robot['id']] = side; hit = True
                                    else:
                                        along = sorted((b for b in self.robots if on_line(xy(r), p, xy(b))), key=lambda b: (dist(xy(r), xy(b)), b['id']))
                                        energy = 10 if kind == 'gatling' else 10 * r['level']
                                        for b in along:
                                            dealt = min(energy, b['health'])
                                            damage[b['id']] += dealt; credit[b['id']] = side; energy -= dealt; hit = True
                                            if kind == 'gatling' or energy <= 0: break
                                if kind == 'rocket': r['cooldown'] = 4
                                self.metrics[side]['attacks'] += 1
                                ok = hit
                        elif r['roleType'] in MOBILE:
                            ok = self.role_action(side, r, command, target, consumed_mines)
                except (KeyError, TypeError, ValueError, IndexError):
                    malformed = True
                t['results'][str(key)] = ok
                if not ok: self.metrics[side]['invalid_actions'] += 1
            if malformed:
                t['exceptions'] += 1; self.metrics[side]['malformed_responses'] += 1
                t['errors'].append(dict(errorCode=4, description='malformed command'))
            if isinstance(response, dict) and response.get('prompt'):
                if t['active'] is not None or t['llm_calls'] < 3:
                    t['llmResp'] = mock_llm(response['prompt'], t['active'] is not None, self.task_profile)
                    if t['active'] is None: t['llm_calls'] += 1
                    self.metrics[side]['mock_llm_calls'] += 1
                else: t['errors'].append(dict(errorCode=5, description='daily quota'))
            if isinstance(response, dict) and response.get('executeCmd'):
                t['lastCmdResult'] = ('[exitCode:0]\n42\n' if response['executeCmd'].strip() == 'printf 42' else '[exitCode:127]\nunsupported fixture command') if t['active'] is not None else '[JUDGER_ERROR]\nno active task'
                self.metrics[side]['mock_commands'] += 1
        static |= {p for t in self.teams for r in t['roles'] if r['roleType'] not in MOBILE for p in cells(r)}
        moved = resolve_moves(positions, proposals, static)
        for rid, target in proposals.items():
            side, role = mobile_lookup[rid]
            if rid in moved: role['pos'] = pos(target)
            else:
                self.teams[side]['results'][str(rid)] = False
                self.metrics[side]['collisions'] += 1
                self.metrics[side]['invalid_actions'] += 1
        self.robot_turn(damage)
        for side, t in enumerate(self.teams):
            if t['active'] is not None:
                pioneer = next((r for r in t['roles'] if r['roleType'] == 'pioneer'), None)
                point = xy(t['playerTasks'][t['active']['index']]['taskPosition'])
                if pioneer is None or dist(xy(pioneer), point) > 1: self.end_task(t)
            survivors = []
            for r in t['roles']:
                r['health'] -= damage[r['id']]
                r['cooldown'] = max(0, r['cooldown'] - 1)
                if r['health'] > 0: survivors.append(r)
                elif r['roleType'] == 'station': t['death'] = self.round
                elif r['roleType'] in MOBILE:
                    t['graves'].append(r)
                    if r['roleType'] == 'pioneer': self.end_task(t)
            t['roles'] = survivors
            if self.round % 130 == 0 and t['death'] is None:
                points = 10 * (self.round // 130)
                t['totalScore'] += points
                self.metrics[side]['survival_score'] += points
        for b in self.robots:
            b['health'] -= damage[b['id']]
            if b['health'] <= 0 and b['id'] in credit:
                side = credit[b['id']]
                points = ROBOT[b['roleType']][2]
                self.teams[side]['totalScore'] += points
                self.metrics[side]['kill_score'] += points
                self.metrics[side]['kills'] += 1
        self.robots = [b for b in self.robots if b['health'] > 0]
        for p, count in consumed_mines.items():
            self.mines[p] -= count
            if self.mines[p] <= 0:
                zone = next(z for z in self.zones if xy(z) == p)
                kind = zone['neutralType']; self.zones.remove(zone); del self.mines[p]
                self.spawn_mine(kind)
        self.round += 1

    def role_action(self, side, r, c, target, consumed):
        t = self.teams[side]; action = c['action']; name = c.get('name', '')
        pack = r['backpack']; adjacent = target is not None and dist(xy(r), target) <= 1
        occupied = {xy(z) for z in self.zones} | {p for tm in self.teams for u in tm['roles'] for p in cells(u)} | {xy(b) for b in self.robots}
        if action == 'collect':
            if r['roleType'] != 'worker' or not adjacent or target not in self.mines or len(pack) >= r['backPackCapability']: return False
            pack.append(next(z['neutralType'] for z in self.zones if xy(z) == target)); consumed[target] += 1
            self.metrics[side]['collected'] += 1
            return True
        if action in ('buy', 'sell'):
            shop = 'vendor' if action == 'sell' else 'weaponShop'
            if not any(z['neutralType'] == shop and dist(xy(r), xy(z)) <= 1 for z in self.zones): return False
            n = c.get('num', 1)
            if not isinstance(n, int) or n < 1: return False
            if action == 'sell':
                if name not in ('stone', 'iron', 'copper') or pack.count(name) < n: return False
                for _ in range(n): pack.remove(name)
                t['goldNum'] += {'stone': 1, 'iron': 2, 'copper': 3}[name] * n
                self.metrics[side]['sold'] += n
            else:
                if name not in PRICES or t['goldNum'] < PRICES[name]*n or len(pack)+n > r['backPackCapability']: return False
                pack.extend([name]*n); t['goldNum'] -= PRICES[name]*n
                self.metrics[side]['purchases'] += n
            return True
        if action == 'build':
            station = next((u for u in t['roles'] if u['roleType'] == 'station'), None)
            if (self.round-1)%130 >= 70 or r['roleType'] != 'worker' or not adjacent or target == xy(r) or station is None or target is None or not (0 <= target[0] < 41 and 0 <= target[1] < 32): return False
            radius = min(dist(target, p) for p in cells(station))
            prior = next((u for u in t['roles'] if xy(u) == target and u['roleType'] in TOWERS), None)
            if name not in TOWERS + ('wall',) or target in occupied and prior is None: return False
            if name == 'wall':
                if radius != 2 or 'stone' not in pack: return False
                pack.remove('stone'); uid = 40000 + side*1000 + self.metrics[side]['walls_built']
                self.metrics[side]['walls_built'] += 1
            else:
                if radius != 1 or t['goldNum'] < 25 or prior is None and sum(u['roleType'] in TOWERS for u in t['roles']) >= 3: return False
                t['goldNum'] -= 25
                if prior is not None: t['roles'].remove(prior)
                prefix = 10000 + side*10000 + {'gatling': 20, 'railgun': 30, 'rocket': 40}[name]
                uid = next(i for i in range(prefix, prefix+3) if all(u['id'] != i for u in t['roles']))
                self.metrics[side]['towers_built'] += 1
            t['roles'].append(unit(uid, name, target)); return True
        if action == 'use':
            if name not in pack or name not in PRICES or target is None: return False
            building = next((u for u in t['roles'] if target in cells(u) and u['roleType'] not in MOBILE), None)
            if building is None or min(dist(xy(r), p) for p in cells(building)) > 1: return False
            kind = building['roleType']; group = 'Weapon' if kind in TOWERS else 'Station' if kind == 'station' else 'Wall'
            level = building['level']
            if level >= 3 or name != f'{group}UpgradeVoucher{level}': return False
            pack.remove(name); building['level'] += 1
            building['health'] = 1500*building['level'] if kind == 'station' else 500 + building['level']*500
            if kind in TOWERS: building['attackRange'] = RANGES[kind][building['level']-1]
            self.metrics[side]['upgrades'] += 1
            return True
        if action == 'remove':
            wall = next((u for u in t['roles'] if u['roleType'] == 'wall' and xy(u) == target), None)
            if r['roleType'] != 'worker' or not adjacent or wall is None: return False
            t['roles'].remove(wall); return True
        if action == 'drop' and name in pack:
            pack.remove(name); return True
        if action == 'acceptTask':
            if r['roleType'] != 'pioneer' or t['active'] is not None: return False
            for n, task in enumerate(t['playerTasks']):
                if task['isValid'] and dist(xy(r), xy(task['taskPosition'])) <= 1:
                    t['active'] = dict(index=n, start=self.round)
                    t['phaseTask'] = 'LOCAL FIXTURE 自进化任务：请计算 6 × 7。最终 taskAnswer 必须是字符串 42。可调用命令获得 fixture 结果。'
                    self.metrics[side]['accepted_tasks'] += 1
                    return True
            return False
        if action == 'submitAnswer':
            if r['roleType'] != 'pioneer' or t['active'] is None: return False
            if str(c.get('taskAnswer', '')).strip() != '42':
                t['errors'].append(dict(errorCode=2, description='fixture answer mismatch')); return True
            elapsed = max(1, self.round-t['active']['start'])
            points = 50 + 150/elapsed
            t['totalScore'] += points; t['goldNum'] += 60
            self.metrics[side]['task_score'] += points
            self.metrics[side]['completed_tasks'] += 1
            self.end_task(t); return True
        self.metrics[side]['unsupported_actions'] += 1
        return False

    def robot_turn(self, damage):
        occupied = {p for t in self.teams for r in t['roles'] for p in cells(r)} | {xy(z) for z in self.zones}
        robot_positions = {b['id']: xy(b) for b in self.robots}
        proposals = {}
        for b in self.robots:
            team = next(t for t in self.teams if t['type'] == b['targetTeam'])
            targets = sorted(team['roles'], key=lambda r: (min(dist(xy(b), p) for p in cells(r)), r['id']))
            if not targets: continue
            near = targets[0]
            if min(dist(xy(b), p) for p in cells(near)) <= 3:
                damage[near['id']] += ROBOT[b['roleType']][1]
                continue
            station = next((r for r in targets if r['roleType'] == 'station'), near)
            p = xy(b)
            opts = [(p[0]+dx, p[1]+dy) for dx in (-1,0,1) for dy in (-1,0,1) if (dx,dy) != (0,0)]
            opts.sort(key=lambda q: (min(dist(q,c) for c in cells(station)), q))
            valid = [q for q in opts if q not in occupied and 0 <= q[0] < 41 and 0 <= q[1] < 32]
            if valid: proposals[b['id']] = valid[0]
        moved = resolve_moves(robot_positions, proposals, occupied)
        for b in self.robots:
            if b['id'] in moved: b['pos'] = pos(moved[b['id']])

    def outcome(self):
        a, b = self.teams
        if a['death'] != b['death']:
            da, db = a['death'] or 10**9, b['death'] or 10**9
            return 0 if da > db else 1
        return 0 if a['totalScore'] > b['totalScore'] else 1 if a['totalScore'] < b['totalScore'] else None

