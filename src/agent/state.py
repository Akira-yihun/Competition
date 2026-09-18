"""In-memory session state: locked, bounded and explicitly versioned."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import threading
from time import monotonic
from .engine import compute, Decision
from .protocol import decode, empty_response
from .config import DEFAULT
from .telemetry.events import record


def digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

class Session:
    def __init__(self):
        self.state={'version':0,'last_round':0,'epoch':0,'task':None,'memory':[]}
        self.cache=OrderedDict()
        self.lock=threading.Lock()

    def reset(self):
        with self.lock:
            epoch=self.state['epoch']+1
            self.state={'version':0,'last_round':0,'epoch':epoch,'task':None,'memory':[]}
            self.cache.clear()

    def decide(self,payload,executor=None,deadline=None):
        deadline=deadline or monotonic()+DEFAULT.request_seconds
        try:
            turn=decode(payload)
            fingerprint=digest(payload)
        except (ValueError,KeyError,TypeError):
            return empty_response()
        if not self.lock.acquire(timeout=max(0,deadline-monotonic())):
            return empty_response()
        try:
            cached=self.cache.get(turn.round_no)
            if cached:
                response=deepcopy(cached[1]) if cached[0]==fingerprint else empty_response()
                record(payload,response,self.state,'replay' if cached[0]==fingerprint else 'conflicting_request')
                return response
            if turn.round_no<=self.state['last_round']:
                return empty_response()
            snapshot=deepcopy(self.state)
            version=self.state["version"]
            error=None
            try:
                result=(executor or compute)(payload,snapshot,deadline)
                if result is None or result.based_on_version!=version or monotonic()>=deadline:
                    raise TimeoutError('discarded decision')
                response=result.response
                new_state=result.state
            except Exception as exc:
                error={'type':type(exc).__name__,'message':str(exc)[:2000]}
                response=empty_response()
                new_state=deepcopy(self.state)
                new_state['task']=None  # Unknown pending calls cannot survive fallback.
                new_state['fallbacks']=new_state.get('fallbacks',0)+1
            new_state['last_round']=turn.round_no
            new_state['version']=version+1
            self.state=new_state
            record(payload,response,new_state,error=error)
            self.cache[turn.round_no]=(fingerprint,deepcopy(response))
            while len(self.cache)>DEFAULT.cached_rounds:
                self.cache.popitem(last=False)
            return deepcopy(response)
        finally:
            self.lock.release()

class SessionStore:
    """Identity is limited to observed team/side/map size; no invented match ID.

    Deployment must isolate indistinguishable matches. Explicit reset/new process
    starts another same-side match; a changed side gets a separate cold session.
    """
    def __init__(self):
        self.sessions={}
        self.lock=threading.Lock()

    def get(self,payload):
        team=payload.get('teamOur',{}); info=payload.get('mapInfo',{})
        key=(str(team.get('teamId','')),str(team.get('type','')),info.get('width'),info.get('height'))
        with self.lock:
            if key not in self.sessions:
                if len(self.sessions)>=DEFAULT.max_sessions:
                    return None
                self.sessions[key]=Session()
            return self.sessions[key]
