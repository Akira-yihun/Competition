"""Replay one selected agent's recorded observations through a fresh session."""
import argparse
import json
from pathlib import Path
from .referee import ROOT
from agent.state import Session


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--agent',default='candidate')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.input.resolve()==args.output.resolve():parser.error('output must not overwrite input')
    session=Session();total=changed=0
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.input.open() as source,args.output.open('w') as dest:
        for line in source:
            record=json.loads(line)
            if record.get('agent')!=args.agent:continue
            response=session.decide(record['request']);different=response!=record['response']
            total+=1;changed+=different
            dest.write(json.dumps({'round':record['round'],'response':response,'changed':different},ensure_ascii=False)+'\n')
    print(json.dumps({'frames':total,'changed':changed,'mode':'observation_replay_not_counterfactual_match'}))

if __name__=='__main__':main()
