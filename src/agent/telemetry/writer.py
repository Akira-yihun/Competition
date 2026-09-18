"""Bounded asynchronous plaintext logs; never blocks the decision channel.

CORE_GEEK_DEBUG_LOG=stdout (default), off, or a local NDJSON path.
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


def _writer():
    while True:
        sink,event=_QUEUE.get()
        try:
            line=json.dumps(event,ensure_ascii=False,separators=(',',':'))+'\n'
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
