"""Parse untrusted model data. No command is ever executed on this host."""
import json

def parse(text, pending, instance):
    if not pending or pending['kind']!='model':
        return None
    if text.strip().startswith('```'):
        text='\n'.join(text.strip().splitlines()[1:-1])
    try:
        value=json.loads(text)
    except (ValueError,TypeError):
        return None
    if not isinstance(value,dict) or value.get('taskKey')!=instance or value.get('requestId')!=pending['id'] or value.get('roundNo')!=pending['round']:
        return None
    command=value.get('executeCmd');answer=value.get('taskAnswer')
    if bool(command)==bool(answer):
        return None
    if command and isinstance(command,str) and len(command)<=12000:
        return 'command',command
    if answer and isinstance(answer,str) and len(answer)<=64000:
        return 'answer',answer
    return None
