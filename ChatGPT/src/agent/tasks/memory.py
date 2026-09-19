"""Task-point recipes preserve evidence and uncertainty, never old answers as facts."""
from copy import deepcopy
MAX_RECORDS=16


def archive(state, task, reason):
    summary=task.get('summary') or {'steps':task.get('commands',[]),'verification':'未取得模型总结，保留可观察交互记录'}
    record={'task_hash':task['text_hash'],'instance':task['instance'],'end_reason':task.get('abort_reason',reason),
            'verified':False,'evidence_level':'submitted_unconfirmed' if task.get('answer') else 'exploration_only',
            'point':task.get('point'),'summary':deepcopy(summary),
            'understanding':task.get('understanding'),'task_family':summary.get('taskFamily') or (task.get('understanding') or {}).get('taskFamily'),
            'commands':task.get('commands',[])[-24:],'sandboxHistory':deepcopy(task.get('sandbox_history',[])),
            'evidence':task.get('evidence','')[-4096:],'answer':task.get('answer','')[-4096:]}
    state.setdefault('memory',[]).append(record)
    state['memory']=state['memory'][-MAX_RECORDS:]


def candidates(state,point):
    records=state.get('memory',[])
    ordered=sorted(enumerate(records),key=lambda item:(item[1].get('point')==point,item[0]),reverse=True)
    # Recipes carry commands and the model's explicit summary, not the old final answer.
    return [{k:r.get(k) for k in ('point','summary','task_family','evidence_level','verified','end_reason')}
            for _,r in ordered[:4]]
