"""Useful task/news evidence, with explicit size truncation."""
import json
from copy import deepcopy
from .writer import emit
from ..agents import blackboard


def _record(payload,response,state,status='decision',error=None):
    task=state.get('task') or {}
    truncations={}
    def bounded(name,value,limit=65536):
        text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
        if len(text)>limit:truncations[name]=len(text)-limit
        return text[:limit]
    team=payload.get('teamOur') or {}
    event={'req':deepcopy(payload),'rsp':deepcopy(response),'event':status,'round':payload.get('roundNo'),'team':team.get('teamId'),'side':team.get('type'),
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
           'rolePlans':deepcopy(state.get('role_plans',{})),
           'roleDuties':state.get('role_duties',{}),'economyJobs':state.get('economy_jobs',{}),
           'intelligence':state.get('intelligence',{}),'treasureResult':payload.get('lastSummonTreasureResult',0),
           'ourUnits':[{k:r.get(k) for k in ('id','roleType','pos','health','level','backpack')} for r in team.get('roles',[])],
           # Role-agent summaries: what each agent concluded and why it acted.
           # These stay dicts (already compact) so the log line is readable JSON.
           'defenseAssessment':defense_summary(state),
           'defensePlan':state.get('defense_plan',{}),
           'defenseSwitch':state.get('defense_task_switch',{}),
           'attackAssessment':state.get('attack_assessment',{}),
           'economyPlan':state.get('economy_plan',{}),
           'economySurvey':economy_summary(state),
           'taskPlan':state.get('task_plan',{}),
           'selfEvolve':blackboard.read(state,'self_evolve') or {},
           'review':blackboard.read(state,'review') or {},
           'error':error,'truncatedCharacters':truncations}
    emit(event)


def defense_summary(state):
    """Compact defence situation report (full tables stay in session state)."""
    report=state.get('defense_assessment') or {}
    if not report:return {}
    return {'summary':report.get('summary'),'firepower':report.get('firepower'),
            'damage':{k:v for k,v in (report.get('damage') or {}).items() if k!='walls'},
            'walls':(report.get('damage') or {}).get('walls',[])[:8],'economy':report.get('economy')}


def economy_summary(state):
    """Compact economy survey: prices, best mine and how many mines were seen."""
    survey=state.get('economy_survey') or {}
    return {'round':survey.get('round'),'prices':survey.get('prices'),'best':survey.get('best'),
            'mines':len(survey.get('mines',[]))}


def record(payload,response,state,status='decision',error=None):
    try:_record(payload,response,state,status,error)
    except Exception:pass

