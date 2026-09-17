"""Protocol adapters share one oracle; no host command execution."""
import json

def mock_llm(prompt, active, profile='reasoning'):
    if not active:
        return 'LOCAL FIXTURE: no active task.'
    try:
        context=json.loads(prompt.rsplit('\n',1)[-1])
    except (ValueError,TypeError):
        context=None
    structured=isinstance(context,dict) and 'taskKey' in context and 'roundNo' in context
    if not structured:
        return '42' if profile=='reasoning' else 'Tool evidence required; fixture command: printf 42'
    result={k:context[k] for k in ('taskKey','roundNo','requestId') if k in context}
    if profile=='reasoning' or context.get('lastCmdResult')=='[exitCode:0]\n42\n':
        result['taskAnswer']='42'
    else:
        result['executeCmd']='printf 42'
    return json.dumps(result)
