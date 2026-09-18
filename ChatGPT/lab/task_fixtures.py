"""Protocol adapters share one oracle; no host command execution."""
import json

def mock_llm(prompt, active, profile='reasoning'):
    try:
        context=json.loads(prompt.rsplit('\n',1)[-1])
    except (ValueError,TypeError):
        context=None
    if isinstance(context,dict) and context.get('channel')=='news':
        return json.dumps({'requestId':context['requestId'],'market':[], 'treasure':{'ready':False,'missing':['fixture contains no treasure clue']}})
    if not active:return 'LOCAL FIXTURE: no active task.'
    structured=isinstance(context,dict) and 'taskKey' in context and 'roundNo' in context
    if not structured:
        return '42' if profile=='reasoning' else 'Tool evidence required; fixture command: printf 42'
    result={k:context[k] for k in ('taskKey','roundNo','requestId') if k in context}
    if context.get('stage')=='understand':
        result['taskUnderstanding']={'objective':'计算6乘7','answerFormat':'字符串42','verification':'算术复核','taskFamily':'fixture-arithmetic','reusableProcedure':['计算并复核']}
    elif profile=='reasoning' or context.get('lastCmdResult')=='[exitCode:0]\n42\n':
        result['taskAnswer']='42'
    else:
        result['executeCmd']='printf 42'
    return json.dumps(result)
