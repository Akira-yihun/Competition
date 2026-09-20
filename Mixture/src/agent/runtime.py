"""Killable compute workers return proposals to the main process state owner."""
import multiprocessing
from time import monotonic
from .engine import compute
from .config import DEFAULT
from .state import SessionStore


def _worker(payload,snapshot,deadline,pipe):
    try:
        pipe.send(compute(payload,snapshot,deadline))
    except Exception as exc:
        pipe.send({'worker_error':type(exc).__name__,'message':str(exc)[:2000]})
    finally:
        pipe.close()


def execute(payload,snapshot,deadline):
    ctx=multiprocessing.get_context('spawn')
    reader,writer=ctx.Pipe(duplex=False)
    process=ctx.Process(target=_worker,args=(payload,snapshot,deadline,writer),daemon=True)
    try:
        process.start();writer.close()
        remaining=max(0,min(DEFAULT.decision_seconds,deadline-monotonic()-0.25))
        result=reader.recv() if reader.poll(remaining) else None
        if isinstance(result,dict) and 'worker_error' in result:
            raise RuntimeError(result['worker_error']+': '+result['message'])
        return result
    except (EOFError,OSError,ValueError):
        return None
    finally:
        reader.close();writer.close()
        if process.pid is not None:
            if process.is_alive():process.terminate()
            process.join(timeout=0.1)
            if process.is_alive():
                process.kill();process.join(timeout=0.1)

class Runtime:
    def __init__(self):
        self.store=SessionStore()

    def decide(self,payload,deadline=None):
        from .protocol import empty_response
        session=self.store.get(payload)
        if session is None:return empty_response()
        return session.decide(payload,execute,deadline)
