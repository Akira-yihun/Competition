"""Bounded asynchronous plaintext logs; never blocks the decision channel.

CORE_GEEK_DEBUG_LOG=stdout (default), off, or a local log path. CORE_GEEK_LOG_FORMAT=ndjson restores legacy events.
"""
import atexit
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time

_QUEUE=queue.Queue(maxsize=256)
_LOCK=threading.Lock()
_STARTED=False
_DROPPED=0


def format_event(event):
    dump=lambda v: json.dumps(v,ensure_ascii=False,separators=(',',':'))
    if os.environ.get('CORE_GEEK_LOG_FORMAT')=='ndjson':
        return dump(event)+'\n'
    req=event.get('req',{});rsp=event.get('rsp',{});news=req.get('worldNews') or {}
    if not isinstance(news,dict):news={'officialNews':news}
    lines=[f"ROUND {event.get('round')}", 'req '+dump(req), 'rsp '+dump(rsp)]
    for label,value in (
        ('官方新闻',news.get('officialNews','')),('民间传闻',news.get('folkLegends','')),
        ('自进化任务要求',req.get('phaseTask','')),('req llm调用结果',req.get('llmResp','')),
        ('rsp llm调用prompt',rsp.get('prompt','')),('req executecmd结果',req.get('lastCmdResult','')),
        ('rsp executecmd命令',rsp.get('executeCmd',''))):
        lines.append(label+' '+dump(value))
    lines.append('diagnostics '+dump({k:v for k,v in event.items() if k not in ('req','rsp')}))
    return '\n'.join(lines)+'\n'


def _writer():
    while True:
        sink,event=_QUEUE.get()
        try:
            line=format_event(event)
            if sink=='stdout':sys.stdout.write(line);sys.stdout.flush()
            else:
                path=Path(sink);path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('a',encoding='utf-8') as f:f.write(line)
        except Exception:
            pass  # Diagnostics must not turn a successful action into fallback.
        finally:_QUEUE.task_done()


def emit(event):
    global _STARTED,_DROPPED
    sink=os.environ.get('CORE_GEEK_DEBUG_LOG','stdout')
    if sink in ('off','0',''):return
    with _LOCK:
        if not _STARTED:
            threading.Thread(target=_writer,name='debug-log',daemon=True).start();_STARTED=True
        item={'schema':'coregeek-debug/1','time':time.time(),'dropped_events':_DROPPED,**event}
        try:_QUEUE.put_nowait((sink,item));_DROPPED=0
        except queue.Full:_DROPPED+=1


def flush(timeout=2):
    end=time.monotonic()+timeout
    while _QUEUE.unfinished_tasks and time.monotonic()<end:time.sleep(.005)

atexit.register(flush)
