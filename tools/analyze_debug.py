#!/usr/bin/env python3
"""Summarize plaintext CoreGeek diagnostic events without exposing task bodies."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(path):
    counts=Counter();reasons=Counter();errors=Counter();submitted=[];night=Counter()
    for line in path.open(encoding='utf-8'):
        try:event=json.loads(line)
        except ValueError:continue  # Startup banner may share stdout.
        if not isinstance(event,dict) or event.get('schema')!='coregeek-debug/1':continue
        counts['events']+=1
        counts['dropped_events']+=event.get('dropped_events',0)
        if event.get('event')!='decision':continue
        if event.get('error'):errors[event['error'].get('type','unknown')]+=1
        state=event.get('taskState') or {}
        if event.get('phaseTask'):counts['task_rounds']+=1
        if event.get('prompt'):counts['model_requests']+=1
        if event.get('executeCmd'):counts['commands_requested']+=1
        if state.get('last_parse_reason'):reasons[state['last_parse_reason']]+=1
        for role,answer in event.get('submittedAnswers',{}).items():
            submitted.append({'round':event.get('round'),'side':event.get('side'),'role':role,'answer_characters':len(answer)})
        if (event.get('round',1)-1)%130>=70:
            assignments=event.get('defenseAssignments',[])
            night['rounds']+=1
            night['three_adjacent_operators']+=len(assignments)==3 and all(a['distance']<=1 for a in assignments)
        for command in event.get('commands',{}).values():counts['action_'+command.get('action','unknown')]+=1
    return {'counts':dict(counts),'task_reasons_by_round':dict(reasons),'decision_errors':dict(errors),'submissions':submitted,'night':dict(night),'note':'提交次数不代表成功次数；完整题目、新闻、模型原文和答案见输入日志。'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path)
    args=parser.parse_args()
    print(json.dumps(summarize(args.input),ensure_ascii=False,indent=2))
