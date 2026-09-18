"""Offline resumable package -> simulation -> evidence report pipeline."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from .store import save, locked

ROOT=Path(__file__).resolve().parents[1]


def source_identity():
    paths=[ROOT/'main3.py',ROOT/'run.sh',ROOT/'pyproject.toml']
    for directory in ('src/agent','lab','tools/baseline_agent'):
        paths.extend((ROOT/directory).rglob('*.py'))
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def run(directory, seeds, rounds, adapter):
    if adapter!='local':
        return {'status':'BLOCKED_CONFIG','reason':'Official upload/build/match APIs and budgets are not configured.'}
    with locked(directory):
        path=directory/'state.json'
        state=json.loads(path.read_text()) if path.exists() else {'phase':'CREATED','provider':'local','seeds':seeds,'rounds':rounds,'sources':source_identity(),'operations':[]}
        if state['sources']!=source_identity():
            raise ValueError('source changed; create a new iteration directory')
        if state['seeds']!=seeds or state['rounds']!=rounds:
            raise ValueError('plan changed; create a new iteration directory')
        save(path,state)
        package=directory/'submission.tar.gz'
        if state['phase']=='CREATED':
            state['operations'].append({'operation':'package','status':'INTENT'});save(path,state)
            subprocess.run([sys.executable,str(ROOT/'tools/package.py'),'--output',str(package)],cwd=ROOT,check=True,stdout=subprocess.DEVNULL,timeout=30)
            state['artifact_sha256']=hashlib.sha256(package.read_bytes()).hexdigest()
            state['operations'][-1]['status']='DONE';state['phase']='PACKAGED';save(path,state)
        if not package.exists() or hashlib.sha256(package.read_bytes()).hexdigest()!=state['artifact_sha256']:
            raise ValueError('artifact identity mismatch')
        if state['phase']=='PACKAGED':
            # Repeating an interrupted local simulation is safe: no remote side effects.
            state['operations'].append({'operation':'simulation','status':'INTENT'});save(path,state)
            subprocess.run([sys.executable,'-m','lab.evaluate','--seeds',seeds,'--rounds',str(rounds),'--output',str(directory/'matches')],cwd=ROOT,check=True,stdout=subprocess.DEVNULL,timeout=900)
            result=json.loads((directory/'matches/summary.json').read_text())
            if not result['source_unchanged'] or source_identity()!=state['sources']:
                raise ValueError('source changed during evaluation')
            state['operations'][-1]['status']='DONE';state['phase']='EVALUATED';save(path,state)
        summary=directory/'matches/summary.json'
        if state['phase']=='EVALUATED':
            state['summary_sha256']=hashlib.sha256(summary.read_bytes()).hexdigest()
            state['decision']='INCONCLUSIVE_LOCAL_ONLY'
            report={'provider':'local','decision':state['decision'],'artifact_sha256':state['artifact_sha256'],'summary_sha256':state['summary_sha256'],'official_result':None}
            save(directory/'report.json',report)
            state['phase']='ARCHIVED';save(path,state)
        if hashlib.sha256(summary.read_bytes()).hexdigest()!=state['summary_sha256']:
            raise ValueError('summary identity mismatch')
        return {'status':state['phase'],'decision':state['decision'],'directory':str(directory)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run','resume','status'])
    parser.add_argument('--iteration',type=Path,required=True)
    parser.add_argument('--seeds',default='1')
    parser.add_argument('--rounds',type=int,default=130)
    parser.add_argument('--adapter',choices=['local','official'],default='local')
    args=parser.parse_args();directory=args.iteration.resolve()
    if args.command in ('status','resume'):
        state=json.loads((directory/'state.json').read_text())
        if args.command=='status':print(json.dumps(state,ensure_ascii=False));return
        args.seeds=state['seeds'];args.rounds=state['rounds']
    if not 1<=args.rounds<=1300 or not 1<=len(args.seeds.split(','))<=10:
        parser.error('bounded plan requires 1..1300 rounds and 1..10 seeds')
    for seed in args.seeds.split(','):int(seed)
    try:result=run(directory,args.seeds,args.rounds,args.adapter)
    except Exception as exc:
        print(json.dumps({'status':'FAILED','error':str(exc)}));raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
