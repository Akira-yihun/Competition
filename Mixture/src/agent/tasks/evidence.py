"""Fit accumulated evidence to the wire limit, with explicit omissions."""
from copy import deepcopy
import json


def encode(context,limit=165000):
    data=deepcopy(context);omitted=[]
    def dump():return json.dumps(data,ensure_ascii=False,separators=(',',':'))
    # Keep the full unmodified command/result in the turn logs. Within model context,
    # discard old bulky excerpts first, retaining identity and omission counts.
    for entry in data.get('sandboxHistory',[]):
        if len(dump())<=limit:break
        for field in ('result','command'):
            text=entry.get(field)
            if isinstance(text,str) and len(text)>512:
                entry[field]=text[:256]+'\n[上下文长度限制：中间内容省略]\n'+text[-256:]
                entry['context_omitted_'+field]=len(text)-512
    for key in ('previousResponse','lastCmdResult','candidateSOPs','verifiedSOPs','understanding'):
        if len(dump())<=limit:break
        if data.get(key):data[key]=None;omitted.append(key)
    history=data.get('sandboxHistory',[])
    while len(dump())>limit and len(history)>2:
        history.pop(0);data['omittedOldInteractions']=data.get('omittedOldInteractions',0)+1
    if len(dump())>limit:
        task=data.get('task','')
        data['task']=task[:50000]+'\n[任务文本超出上下文上限，未显示部分不得猜测]\n'+task[-10000:]
        omitted.append('task_middle')
    if omitted:data['contextOmissions']=omitted
    return dump()
