"""Bind model data to the one pending call; reject explicit stale identities."""
import json


def parse_with_reason(text,pending,instance):
    if not pending or pending['kind']!='model':return None,'no_pending_model'
    if not isinstance(text,str) or not text.strip():return None,'empty_llm_response'
    text=text.strip()
    if text.startswith('```'):
        lines=text.splitlines()
        text='\n'.join(lines[1:-1]) if lines[-1].strip()=='```' else '\n'.join(lines[1:])
    try:value=json.loads(text)
    except ValueError:
        if text.startswith(('{','[')):return None,'malformed_json'
        value=None
    if isinstance(value,dict):
        for key,expected in [('taskKey',instance),('requestId',pending['id']),('roundNo',pending['round'])]:
            if key in value and value[key]!=expected:return None,'mismatched_'+key
        command=value.get('executeCmd');answer=value.get('taskAnswer')
        if isinstance(answer,(dict,list,int,float,bool)):answer=json.dumps(answer,ensure_ascii=False)
        if bool(command)==bool(answer):
            # A JSON answer object is valid task data, but an ambiguous envelope is not.
            if not any(k in value for k in ('taskKey','requestId','roundNo','executeCmd','taskAnswer')):
                return ('answer',text),'plain_json_answer'
            return None,'ambiguous_or_empty_action'
        if isinstance(command,str) and command.strip() and len(command)<=12000:
            return ('command',command),'structured_command'
        if isinstance(answer,str) and answer.strip() and len(answer)<=64000:
            return ('answer',answer),'structured_answer'
        return None,'invalid_action_type_or_size'
    if len(text)>64000:return None,'answer_too_large'
    # Only called when the task has one matching, still pending model request.
    # Plain finals (e.g. 42) need not echo bookkeeping fields.
    return ('answer',str(value) if isinstance(value,(int,float)) else value if isinstance(value,str) else text),'plain_final_answer'


def parse(text,pending,instance):
    return parse_with_reason(text,pending,instance)[0]
