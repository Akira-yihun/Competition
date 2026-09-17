"""Bounded evidence records, never executable host instructions."""
MAX_RECORDS=16

def archive(state, task, reason):
    record={'task_hash':task['text_hash'],'instance':task['instance'],'end_reason':reason,
            'verified':False,'commands':task.get('commands',[])[-4:],
            'evidence':task.get('evidence','')[-4096:],'answer':task.get('answer','')[-4096:]}
    # phaseTask disappearance alone cannot prove success. Candidates are retained
    # for diagnostics; unverified recipes are never auto-submitted or auto-run.
    state.setdefault('memory',[]).append(record)
    state['memory']=state['memory'][-MAX_RECORDS:]
