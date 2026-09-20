"""Stable worker duties survive death/respawn; temporary cover never recalls pioneer."""
def assign(turn,state):
    saved=state.setdefault('role_duties',{})
    workers=turn.workers();ids=[r.unit_id for r in workers]
    for key in ('defender','miner'):
        if key not in saved:
            used=set(saved.values());candidate=next((i for i in ids if i not in used),None)
            if candidate is not None:saved[key]=candidate
    defender=next((r for r in workers if r.unit_id==saved.get('defender')),None)
    if defender is None:defender=next(iter(workers),None)
    return defender,next((r for r in workers if r!=defender),None)
