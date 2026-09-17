"""Versioned local match evaluation; never an official promotion signal."""
import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from .referee import Match, ROOT, LIMITATIONS, baseline
from agent.state import Session

def run(seed, rounds, swapped, output, profile="reasoning"):
    match = Match(seed, profile)
    candidate = Session().decide
    agents = (baseline, candidate) if swapped else (candidate, baseline)
    labels = ('baseline', 'candidate') if swapped else ('candidate', 'baseline')
    latencies = [[], []]
    stream = output / f'seed-{seed}-{"swapped" if swapped else "normal"}.ndjson'
    with stream.open('w') as f:
        for _ in range(rounds):
            match.begin()
            requests = [match.observation(s) for s in (0,1)]
            responses = []
            for side in (0,1):
                started = time.perf_counter()
                try:
                    response = agents[side](requests[side]) if match.teams[side]['exceptions'] < 5 else dict(roleCommandMap={}, prompt='', executeCmd='')
                except Exception as exc:
                    response = None
                    match.metrics[side]['agent_exceptions'] += 1
                latencies[side].append((time.perf_counter()-started)*1000)
                responses.append(response)
            round_no = match.round
            match.step(responses)
            for side in (0,1):
                f.write(json.dumps(dict(seed=seed, swapped=swapped, agent=labels[side], side=side, round=round_no, request=requests[side], response=responses[side], validation=match.teams[side]['results'], errors=match.teams[side]['errors']), ensure_ascii=False, separators=(',', ':'))+'\n')
            if all(t['death'] is not None for t in match.teams) or all(t['exceptions'] >= 5 for t in match.teams): break
    winner = match.outcome()
    return dict(seed=seed, swapped=swapped, rounds=match.round-1, winner=labels[winner] if winner is not None else 'draw', stream=str(stream), teams={label: dict(side=side, score=round(t['totalScore'], 4), score_breakdown={k: round(match.metrics[side][k],4) for k in ('task_score','kill_score','survival_score')}, gold=t['goldNum'], station_destroyed_round=t['death'], station_health=next((r['health'] for r in t['roles'] if r['roleType']=='station'),0), metrics=dict(match.metrics[side]), decide_ms=dict(max=round(max(latencies[side]),3), p95=round(sorted(latencies[side])[min(len(latencies[side])-1, int(len(latencies[side])*.95))],3))) for side,(label,t) in enumerate(zip(labels,match.teams))})

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', default='1,2,3')
    parser.add_argument('--rounds', type=int, default=1300)
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts'/'simulation')
    parser.add_argument('--task-profile', choices=('reasoning','tools'), default='reasoning')
    args = parser.parse_args()
    if not 1 <= args.rounds <= 1300: parser.error('--rounds must be 1..1300')
    args.output.mkdir(parents=True, exist_ok=True)
    initial_hashes = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for directory in (ROOT/'src'/'agent', ROOT/'lab', ROOT/'tools'/'baseline_agent') for p in directory.rglob('*.py')}
    halves = [run(seed,args.rounds,swap,args.output,args.task_profile) for seed in map(int,args.seeds.split(',')) for swap in (False,True)]
    matches = []
    for a,b in zip(halves[::2],halves[1::2]):
        scores = {label: a['teams'][label]['score']+b['teams'][label]['score'] for label in ('candidate','baseline')}
        wins = Counter((a['winner'], b['winner']))
        winner = a['winner'] if a['winner']==b['winner'] and a['winner']!='draw' else max(scores,key=scores.get) if scores['candidate'] != scores['baseline'] else 'draw'
        # One half win plus one draw wins the match; split wins use aggregate score.
        if wins['draw']==1: winner = 'candidate' if wins['candidate'] else 'baseline'
        matches.append(dict(seed=a['seed'], winner=winner, combined_scores=scores))
    source_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for directory in (ROOT/'src'/'agent', ROOT/'tools'/'baseline_agent', ROOT/'lab') for p in sorted(directory.rglob('*.py'))}
    source_hashes['lab/evaluate.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result = dict(source_sha256=source_hashes, source_unchanged=all(source_hashes.get(k)==v for k,v in initial_hashes.items()), comparison_eligible=args.task_profile=='reasoning', task_profile=args.task_profile, kind='approximate_local_simulation', limitations=LIMITATIONS, fixture=dict(task_answer='42', command_execution=False, llm='deterministic_fixture'), rounds_limit=args.rounds, halves=halves, matches=matches, match_outcomes=dict(Counter(m['winner'] for m in matches)))
    path = args.output/'summary.json'
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(summary=str(path), match_outcomes=result['match_outcomes'], warning=LIMITATIONS[0]),ensure_ascii=False))

if __name__ == "__main__": main()
