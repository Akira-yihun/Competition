"""Readable local command/API/SOP demo. Fixtures only: NEVER executes shell or HTTP."""
import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch
from .referee import Match,ROOT
from .task_fixtures import mock_llm
from agent.state import Session
from agent.protocol import empty_response
from agent.telemetry.writer import flush

CITIES=('北京','南京','成都')


def answer(city):
    return json.dumps({'city':city,'items':[city+'文化遗产样例A']},ensure_ascii=False,separators=(',',':'))


def oracle(prompt,active,profile='reasoning'):
    try:context=json.loads(prompt.rsplit('\n',1)[-1])
    except ValueError:return mock_llm(prompt,active,profile)
    if context.get('channel')=='news':return mock_llm(prompt,active,profile)
    city=next((c for c in CITIES if c in context.get('task','')),'北京')
    history=context.get('sandboxHistory',[])
    envelope={k:context[k] for k in ('taskKey','requestId','roundNo')}
    if history and 'heritage-cli search' in history[-1]['command'] and '[exitCode:0]' in (history[-1].get('result') or ''):
        envelope.update(taskAnswer=answer(city),taskSummary={
            'taskFamily':'fixture-heritage-api','environmentFacts':['工作目录/sandbox，文档在docs'],
            'documentPaths':['/sandbox/docs/heritage-guide.md'],
            'apiRecipe':'heritage-cli search --city {city} --format json','parameterSlots':['city'],
            'steps':['定位并阅读文档','query失败时查看help','使用search和city参数','核对city与items后提交JSON'],
            'verification':'真实返回须包含本次city和items字段（本演示为fixture）',
            'failureLessons':['旧文档query子命令损坏，使用帮助信息给出的search']})
    elif not history and context.get('candidateSOPs'):
        envelope['executeCmd']=f'heritage-cli search --city {city} --format json'
    else:
        commands=['pwd && ls -a','find . -maxdepth 3 -name heritage-guide.md',
                  'cat ./docs/heritage-guide.md',f'heritage-cli query --city {city}',
                  'heritage-cli --help',f'heritage-cli search --city {city} --format json']
        envelope['executeCmd']=commands[min(len(history),len(commands)-1)]
    return json.dumps(envelope,ensure_ascii=False)


def command_result(command):
    if command=='pwd && ls -a':return '[exitCode:0]\n/sandbox\n. .. docs bin'
    if command=='find . -maxdepth 3 -name heritage-guide.md':return '[exitCode:0]\n./docs/heritage-guide.md'
    if command=='cat ./docs/heritage-guide.md':return '[exitCode:0]\nFIXTURE 文档：使用heritage-cli访问沙盒文化遗产API。旧文档query部分损坏；查询失败后检查真实help返回。答案是city和items组成的JSON。'
    if 'heritage-cli query' in command:return '[exitCode:2]\nunknown subcommand query; run heritage-cli --help'
    if command=='heritage-cli --help':return '[exitCode:0]\nUsage: heritage-cli search --city CITY --format json'
    if 'heritage-cli search' in command:
        city=next((c for c in CITIES if c in command),'北京')
        return '[exitCode:0]\n'+answer(city)
    return '[exitCode:127]\nunsupported fixture command'


class SandboxMatch(Match):
    def __init__(self):
        super().__init__(3,'tools')
        for team in self.teams:
            team['playerTasks'][0]['remaining']=3
            team['playerTasks'][1]['remaining']=0

    def role_action(self,side,r,c,target,consumed):
        if c['action']=='submitAnswer' and self.teams[side]['active'] is not None:
            city=next((city for city in CITIES if city in self.teams[side]['phaseTask']),'北京')
            try:valid=json.loads(c.get('taskAnswer',''))==json.loads(answer(city))
            except ValueError:valid=False
            if not valid:
                self.teams[side]['errors'].append({'errorCode':2,'description':'fixture city/items mismatch'})
                return True
            # Reuse the referee's score/feedback mechanics after fixture validation.
            return super().role_action(side,r,{**c,'taskAnswer':'42'},target,consumed)
        result=super().role_action(side,r,c,target,consumed)
        if result and c['action']=='acceptTask':
            task=self.teams[side]['playerTasks'][self.teams[side]['active']['index']]
            city=CITIES[3-task['remaining']]
            self.teams[side]['phaseTask']=f'LOCAL FIXTURE：查看heritage-guide.md，查询{city}的文化遗产。路径和API环境未知，文档可能部分损坏。严格提交city和items字段组成的JSON。'
        return result

    def step(self,responses):
        with patch('lab.referee.mock_llm',oracle):super().step(responses)
        for side,response in enumerate(responses):
            if response.get('executeCmd') and self.teams[side]['active'] is not None:
                self.teams[side]['lastCmdResult']=command_result(response['executeCmd'])


def run(output,rounds=180):
    output.mkdir(parents=True,exist_ok=True)
    os.environ['CORE_GEEK_DEBUG_LOG']=str(output/'rounds.log')
    os.environ.pop('CORE_GEEK_LOG_FORMAT',None)
    match=SandboxMatch();session=Session();submitted=[];commands=[]
    for _ in range(rounds):
        match.begin();request=match.observation(0);response=session.decide(request)
        if response['executeCmd']:commands.append({'round':match.round,'command':response['executeCmd']})
        for action in response['roleCommandMap'].values():
            if action['action']=='submitAnswer':submitted.append({'round':match.round,'answer':action['taskAnswer']})
        match.step([response,empty_response()])
        if match.round%20==0:flush()
    flush()
    summary={'kind':'deterministic_sandbox_fixture','real_shell_or_network_executed':False,
             'submitted':submitted,'commands':commands,'task_memories':session.state.get('memory',[]),
             'metrics':dict(match.metrics[0]),'log':str(output/'rounds.log')}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:summary[k] for k in ('kind','real_shell_or_network_executed','submitted','log')},ensure_ascii=False))
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts'/'v07-sandbox')
    args=parser.parse_args();run(args.output)
