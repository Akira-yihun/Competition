"""Task-point commitment and local avoidance, independent of worker night zoning."""
from ..model import Pos,distance
from ..navigation import route,STEPS
from ..world import _neighbours
from ..protocol import move_command
from ..objectives import goal

POWER={'smallRobot':5,'middleRobot':10,'largeRobot':20,'bossRobot':40}


def risk(turn,p):
    # All robots are visible. Use their actual position, not a blanket central-map ban.
    # A robot at distance four may move into its three-cell attack range next turn.
    return sum(POWER.get(r.kind,20)*max(0,5-distance(r.pos,p)) for r in turn.robots
               if r.health>0 and r.abnormal_state!='dizzy')


def task_cells(turn,task):
    return turn.task_cells(task)


def stands(turn,task):
    return {p for cell in task_cells(turn,task) for p in _neighbours(cell) if turn.land(p)}


def bind(turn,worker,task,state):
    binding={'point':task.position.dump(),'cells':[p.dump() for p in task_cells(turn,task)],
             'stands':[p.dump() for p in stands(turn,task)],'accepted_round':turn.round_no,
             'timeout':task.timeout_rounds,'kind':task.kind}
    state['task_binding']=binding
    return binding


def safety(turn,worker,state,reserved,commands):
    binding=state.get('task_binding')
    if turn.phase_task and not binding:
        task=min(turn.tasks,key=lambda t:distance(worker.pos,t.position),default=None)
        if task:binding=bind(turn,worker,task,state)
    if not risk(turn,worker.pos):return False
    legal=[Pos(worker.pos.x+dx,worker.pos.y+dy) for dx,dy in STEPS]
    blocked=turn.blocked(worker)|frozenset(reserved)|frozenset(turn.navigation_avoid.get(worker.unit_id,()))
    legal=[p for p in legal if turn.land(p) and p not in blocked]
    allowed={Pos.load(p) for p in binding.get('stands',[])} if binding and turn.phase_task else set()
    nearby=[p for p in legal if p in allowed and risk(turn,p)==0]
    # Prefer preserving the task when a safe adjacent task cell exists.
    choices=nearby or [p for p in legal if risk(turn,p)<risk(turn,worker.pos)]
    if not choices:
        goal(state,turn,worker,'task_shelter',worker.pos,'检测到机器人，但没有更安全的合法一步')
        return False
    step=min(choices,key=lambda p:(risk(turn,p),p not in allowed,p.x,p.y))
    commands[worker.unit_id]=move_command(step);reserved.add(step)
    leaving=bool(turn.phase_task and binding and step not in allowed)
    if leaving:
        state['task_exit_reason']='robot_threat_no_safe_task_stand'
        if state.get('task'):state['task']['abort_reason']=state['task_exit_reason']
    goal(state,turn,worker,'escape_task' if leaving else 'task_avoid',step,
         '机器人逼近，优先任务区内安全换位；必要时保命退出')
    return leaving


def plan(turn,worker,state,reserved,commands,response):
    if turn.phase_task:
        task=state.get('task') or {}
        goal(state,turn,worker,'solve_task',state.get('task_binding',{}).get('point'),
             '保持任务站位，等待沙盒或模型反馈' if task.get('pending') else '根据累积证据推进任务',
             phase=task.get('status','ACTIVE'))
        return
    if worker.unit_id in commands:return
    state.pop('task_binding',None)
    tasks=[t for t in turn.tasks if t.valid]
    saved=state.get('pioneer_target')
    if saved:
        selected=next((t for t in tasks if t.position.dump()==saved),None)
        if selected:tasks=[selected]+[t for t in tasks if t!=selected]
    else:tasks.sort(key=lambda t:-(t.score_reward+.5*t.gold_reward)/(4+distance(worker.pos,t.position)))
    danger={Pos(x,y) for r in turn.robots if r.health>0 and r.abnormal_state!='dizzy'
            for x in range(max(0,r.pos.x-4),min(turn.width,r.pos.x+5))
            for y in range(max(0,r.pos.y-4),min(turn.height,r.pos.y+5))}
    for task in tasks:
        goals=stands(turn,task)-danger
        step,length=route(turn,worker,goals,set(reserved)|danger,cautious=False)
        if length>=10**6:continue
        state['pioneer_target']=task.position.dump()
        if length==0:
            if not state.get('intelligence',{}).get('pending'):
                commands[worker.unit_id]={'action':'acceptTask'};bind(turn,worker,task,state)
                goal(state,turn,worker,'accept_task',task.position,'就位领取任务，记录任务点与截止期')
            else:goal(state,turn,worker,'task_channel_wait',task.position,'已到任务点，等待当前新闻调用返回')
        else:
            commands[worker.unit_id]=move_command(step);reserved.add(step)
            goal(state,turn,worker,'travel_task',task.position,'持续前往选定任务点，不因昼夜切换更改目标')
        return
    goal(state,turn,worker,'task_refresh_wait',worker.pos,'任务点冷却、耗尽或路线暂时危险')
