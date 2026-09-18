"""Useful task/news evidence, with explicit size truncation."""
import json
from .writer import emit


def _record(payload,response,state,status='decision',error=None):
    task=state.get('task') or {}
    truncations={}
    def bounded(name,value,limit=65536):
        text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
        if len(text)>limit:truncations[name]=len(text)-limit
        return text[:limit]
    team=payload.get('teamOur') or {}
    event={'event':status,'round':payload.get('roundNo'),'team':team.get('teamId'),'side':team.get('type'),
           'gold':team.get('goldNum'),'score':team.get('totalScore'),
           'worldNews':bounded('worldNews',payload.get('worldNews',{})),
           'phaseTask':bounded('phaseTask',payload.get('phaseTask','')),
           'llmResp':bounded('llmResp',payload.get('llmResp','')),
           'lastCmdResult':bounded('lastCmdResult',payload.get('lastCmdResult','')),
           'errors':payload.get('errors',[]),'actionResults':payload.get('lastRoundRoleActionResults',{}),
           'taskState':{k:task.get(k) for k in ('instance','status','pending','last_parse_reason','parse_rejections','sequence','stage','understanding')},
           'submittedAnswers':{k:bounded('answer:'+k,c['taskAnswer']) for k,c in response.get('roleCommandMap',{}).items() if c.get('action')=='submitAnswer'},
           'prompt':bounded('prompt',response.get('prompt',''),180000),'executeCmd':response.get('executeCmd',''),
           'commands':response.get('roleCommandMap',{}),'guardDropped':state.get('guard_dropped',{}),
           'defenseAssignments':state.get('defense_assignments',[]),
           'roleDuties':state.get('role_duties',{}),'economyJobs':state.get('economy_jobs',{}),
           'intelligence':state.get('intelligence',{}),'treasureResult':payload.get('lastSummonTreasureResult',0),
           'ourUnits':[{k:r.get(k) for k in ('id','roleType','pos','health','level','backpack')} for r in team.get('roles',[])],
           'error':error,'truncatedCharacters':truncations}
    emit(event)


def record(payload,response,state,status='decision',error=None):
    try:_record(payload,response,state,status,error)
    except Exception:pass
