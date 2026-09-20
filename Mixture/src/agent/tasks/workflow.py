"""Task instance and asynchronous command/model feedback state machine."""
import hashlib
import json
from .channel import parse_with_reason
from .memory import archive, candidates
from .prompts import INSTRUCTIONS
from .evidence import encode


def advance(turn, pioneer, state, commands, response):
    active=bool(turn.phase_task and pioneer)
    text_hash=hashlib.sha256(turn.phase_task.encode()).hexdigest()
    task=state.get('task')
    if task and (not active or task['text_hash']!=text_hash):
        archive(state,task,'ended_unknown' if pioneer else 'pioneer_unavailable')
        task=None;state['task']=None
    if not active:
        return
    if task is None:
        state['task_sequence']=state.get('task_sequence',0)+1
        instance=f"{state.get('epoch',0)}:{state['task_sequence']}:{turn.round_no}:{text_hash[:16]}"
        task={'instance':instance,'text_hash':text_hash,'started':turn.round_no,'pending':None,
              'sequence':0,'evidence':'','commands':[],'sandbox_history':[],
              'point':state.get('task_binding',{}).get('point'),
              'accepted_round':state.get('task_binding',{}).get('accepted_round',turn.round_no),
              'timeout':state.get('task_binding',{}).get('timeout',30),
              'status':'ACTIVE','stage':'understand','understanding':None}
        state['task']=task
    if task.get('ready_answer') is not None:
        if pioneer.unit_id not in commands:
            answer=task.pop('ready_answer')
            commands[pioneer.unit_id]={'action':'submitAnswer','taskAnswer':answer}
            task['answer']=answer
            task['pending']={'kind':'submit','round':turn.round_no,'id':task['instance']}
            task['status']='SUBMIT_PENDING'
        return
    pending=task.get('pending')
    consecutive=state.get('last_round',0)==turn.round_no-1
    parsed=None
    if pending and consecutive and 0 < turn.round_no-pending['round'] <= 5:
        if pending['kind']=='model':
            parsed,reason=parse_with_reason(turn.llm_response,pending,task['instance'])
            task['last_parse_reason']=reason
            if reason=='empty_llm_response' and turn.round_no-pending['round']<5:
                task['status']='MODEL_PENDING'
                return
            if parsed is None:
                task['parse_rejections']=task.get('parse_rejections',0)+1
        elif pending['kind']=='command':
            result=str(turn.raw.get('lastCmdResult',''))
            if not result and turn.round_no-pending['round']<3:
                task['last_parse_reason']='waiting_command_result'
                return
            task['evidence']=result[-65536:]
            entry=next((e for e in task['sandbox_history'] if e['round']==pending['round']),None)
            if entry is not None:
                entry.update(result=result[:24000],result_round=turn.round_no,
                             truncated_characters=max(0,len(result)-24000),
                             outcome='timeout' if result.startswith('[TIMEOUT]') else 'judger_error' if result.startswith('[JUDGER_ERROR]') else 'returned' if result else 'missing')
            task['last_parse_reason']='command_feedback' if result else 'missing_command_result'
        elif pending['kind']=='submit':
            task['status']='ACTIVE'
            task['last_parse_reason']='task_still_active_after_submission'
    elif pending:
        task['evidence']=''
        task['status']='RESYNC'
        task['last_parse_reason']='gap_or_expired_pending_call'
        state['unmatched_feedback']=state.get('unmatched_feedback',0)+1
    task['pending']=None
    if parsed:
        try:
            envelope=json.loads(turn.llm_response.strip().removeprefix('```json').removesuffix('```').strip())
            summary=envelope.get('taskSummary') if isinstance(envelope,dict) else None
            if isinstance(summary,dict) and len(json.dumps(summary))<=12000:task['summary']=summary
        except ValueError:pass
        kind,value=parsed
        if kind=='understanding':
            task['understanding']=value
            task['stage']='solve'
        elif kind=='command':
            response['executeCmd']=value
            task['commands']=(task['commands']+[value])[-64:]
            task['sandbox_history'].append({'round':turn.round_no,'command':value,'result':None})
            # Preserve every command identity; cap result text with explicit loss metadata.
            used=0
            for entry in reversed(task['sandbox_history']):
                text=entry.get('result') or ''
                allowance=max(0,80000-used)
                if len(text)>allowance:
                    entry['truncated_characters']=entry.get('truncated_characters',0)+len(text)-allowance
                    entry['result']=text[:allowance]
                used+=len(entry.get('result') or '')
            task['pending']={'kind':'command','round':turn.round_no,'id':pending['id']}
            task['status']='COMMAND_PENDING'
            return
        elif kind=='answer':
            if pioneer.unit_id in commands:
                task['ready_answer']=value;task['status']='READY_TO_SUBMIT'
                return
            commands[pioneer.unit_id]={'action':'submitAnswer','taskAnswer':value}
            task['answer']=value
            task['pending']={'kind':'submit','round':turn.round_no,'id':pending['id']}
            task['status']='SUBMIT_PENDING'
            return
    task['sequence']+=1
    request_id=f"{task['instance']}:{task['sequence']}"
    task['pending']={'kind':'model','round':turn.round_no,'id':request_id}
    task['status']='MODEL_PENDING'
    context={'taskKey':task['instance'],'requestId':request_id,'roundNo':turn.round_no,
             'task':turn.phase_task,'lastCmdResult':task['evidence'],
             'stage':task.get('stage','understand'),'understanding':task.get('understanding'),
             'draftAnswer':task.get('draft_answer',''),
             'candidateSOPs':candidates(state,task.get('point')),
             'sandboxHistory':task['sandbox_history'],
             'remainingRounds':max(0,task['timeout']-(turn.round_no-task['accepted_round'])),
             'environmentKnowledge':'unknown until observed through executeCmd',
             'errors':turn.raw.get('errors',[]),'previousAnswer':task.get('answer',''),
             'previousResponse':turn.llm_response[-16000:],'formatFeedback':task.get('last_parse_reason',''),
             'recentCommands':task.get('commands',[])[-3:],
             'verifiedSOPs':[m for m in state.get('memory',[]) if m.get('verified') and m['task_hash']==text_hash][:2]}
    phase='理解阶段：先发现环境并读取要求。' if task.get('stage')=='understand' else '解题阶段：依据证据执行、验证并提交。'
    # Legacy taskUnderstanding replies remain readable, but prompts now require an
    # executable next command or a final answer, not an extra planning-only round.
    response['prompt']=INSTRUCTIONS+phase+'\n'+encode(context)
