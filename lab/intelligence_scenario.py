"""Deterministic integration fixture, NOT an evaluation of real LLM reasoning."""
import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch
from .referee import Match,dist,xy,ROOT
from .task_fixtures import mock_llm as task_oracle
from agent.state import Session
from agent.protocol import empty_response
from agent.telemetry.writer import flush


def oracle(prompt,active,profile='reasoning'):
    try:c=json.loads(prompt.rsplit('\n',1)[-1])
    except ValueError:return task_oracle(prompt,active,profile)
    if c.get('channel')!='news':return task_oracle(prompt,active,profile)
    # Explicit oracle for this one synthetic story, not claimed general inference.
    complete=any(e['day']>=2 for e in c['newsHistory'])
    return json.dumps({'requestId':c['requestId'],'market':[{'ore':'iron','startRound':131,'endRound':260,'price':6,'direction':'up','available':False,'holdUntil':131,'confidence':1,'evidence':'fixture官方公告：第二日铁价6、停止采集'}],
        'treasure':{'ready':complete,'position':{'x':12,'y':20} if complete else None,'items':['StarSand'],'startRound':131,'endRound':260,'confidence':1 if complete else 0,'evidence':'fixture两天线索确定祭坛、星辰之沙和时间','missing':[] if complete else ['祭坛位置']}})


class NewsMatch(Match):
    def __init__(self):
        super().__init__(1,'tools');self.treasure_results=[0,0];self.opened=False
        self.zones=[z for z in self.zones if xy(z)!=(19,17)]
        self.zones.append({'pos':{'x':19,'y':17},'neutralType':'iron'});self.mines[(19,17)]=1000

    def observation(self,side):
        p=super().observation(side);day=(self.round-1)//130+1
        p['worldNews']={'officialNews':'fixture：第二天铁矿停产，铁价从2涨到6，第三天恢复。','folkLegends':'fixture：宝藏需要星辰之沙，第二天开启。' if day==1 else 'fixture：祭坛在(12,20)，第二天结束后关闭。'}
        p['weaponShopList'].append({'name':'StarSand','price':15})
        p['vendorShopList']=[dict(name=k,price=6 if k=='iron' and day==2 else v) for k,v in [('stone',1),('iron',2),('copper',3)]]
        p['lastSummonTreasureResult']=self.treasure_results[side]
        return p

    def step(self,responses):
        self.treasure_results=[0,0]
        with patch('lab.referee.mock_llm',oracle):super().step(responses)

    def role_action(self,side,r,c,target,consumed):
        team=self.teams[side]
        if c['action']=='buy' and c.get('name')=='StarSand':
            if not any(z['neutralType']=='weaponShop' and dist(xy(r),xy(z))<=1 for z in self.zones) or team['goldNum']<15 or len(r['backpack'])>=r['backPackCapability']:return False
            team['goldNum']-=15;r['backpack'].append('StarSand');self.metrics[side]['treasure_items_bought']+=1;return True
        if c['action']=='summonTreasure':
            items=c.get('item',[])
            from collections import Counter
            if r['roleType']!='pioneer' or target is None or dist(xy(r),target)>1 or Counter(items)-Counter(r['backpack']):return False
            for name in items:r['backpack'].remove(name)
            code=4 if self.opened else 2 if target!=(12,20) or not 131<=self.round<=260 else 3 if items!=['StarSand'] else 1
            self.treasure_results[side]=code
            self.metrics[side]['treasure_attempts']+=1
            if code==1:
                self.opened=True;team['goldNum']+=200;team['totalScore']+=500;self.metrics[side]['treasures_opened']+=1
            return True
        if c['action']=='collect' and 131<=self.round<=260 and any(z['neutralType']=='iron' and xy(z)==target for z in self.zones):return False
        sold=c['action']=='sell' and c.get('name')=='iron'
        ok=super().role_action(side,r,c,target,consumed)
        if ok and sold:
            if 131<=self.round<=260:team['goldNum']+=4*c.get('num',1)
            self.metrics[side]['iron_sales_at_high_price' if 131<=self.round<=260 else 'iron_sales_at_base_price']+=c.get('num',1)
        return ok


def run(output,rounds=260):
    output.mkdir(parents=True,exist_ok=True);os.environ['CORE_GEEK_DEBUG_LOG']=str(output/'debug.ndjson')
    match=NewsMatch();session=Session()
    with (output/'requests.ndjson').open('w') as f:
        for _ in range(rounds):
            match.begin();request=match.observation(0);response=session.decide(request);match.step([response,empty_response()])
            f.write(json.dumps({'round':request['roundNo'],'request':request,'response':response,'validation':match.teams[0]['results']},ensure_ascii=False)+'\n')
            if request['roundNo']%50==0:flush()
    flush()
    result={'kind':'deterministic_news_and_treasure_fixture','real_llm':False,'host_commands_executed':False,'metrics':dict(match.metrics[0]),'intelligence':session.state.get('intelligence'),'limitations':'Hard-coded oracle and synthetic prices/clues/rewards; verifies coordination, not inference quality or official combat.'}
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(output),'metrics':result['metrics']},ensure_ascii=False))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=ROOT/'artifacts/v04-intelligence');args=parser.parse_args();run(args.output)
