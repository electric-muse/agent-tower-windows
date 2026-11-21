"""Pure task presentation: control capability and transport health stay separate."""
import hashlib
import json
import time
from tower_diagnostics import diagnostic


def job_status(job, run=None, now=None):
    now = time.time() if now is None else now
    state = job.get('state')
    issue = (run or {}).get('diagnostic')
    if job.get('output_expired_at') and state not in ('running','stopping','switching'):
        suffix = ' · 历史输出已清理'
    else: suffix = ''
    if state == 'running':
        phase = (run or {}).get('phase')
        label = {'starting':'初始化中','waiting_input':'等待输入','waiting_approval':'等待本机审批',
                 'tool_running':'工具执行中'}.get(phase,'运行中')
        return {'label':label,'icon':'🟢' if phase not in ('waiting_input','waiting_approval') else '🟠',
                'error':False,'hint':'审批需在本机完成；不会自动提升权限' if phase=='waiting_approval' else '',
                'can_stop':True,'can_go':False}
    if state in ('stopping','switching'):
        return {'label':'停止中' if state=='stopping' else '切换中','icon':'🟠',
                'error':False,'hint':'','can_stop':False,'can_go':False}
    if job.get('retry_at'):
        return {'label':'等待自动重试','icon':'🟠','error':False,
                'hint':f"约 {max(0,int(job['retry_at']-now))} 秒后重试（第 {job.get('retry_cnt',0)} 次）",
                'can_stop':True,'can_go':False}
    stopped = state=='stopped'
    failed = not stopped and str(job.get('exit'))!='0'
    if failed and job.get('agent')=='deepseek-harness':issue=issue or diagnostic()
    label='已停止' if stopped else '执行失败' if failed else '已完成'
    return {'label':label+suffix,'icon':'⏹' if stopped else '🔴' if failed else '✅','error':failed,
            'hint':f"{issue['label']} · {issue['action']}" if issue and failed else '',
            'can_stop':False,'can_go':bool(job.get('sess'))}


def status_signature(job, run=None):
    status=job_status(job,run,now=job.get('retry_at',0))
    return hashlib.sha256(json.dumps([status,job.get('title'),job.get('exit'),job.get('retry_cnt')],sort_keys=True).encode()).hexdigest()


def rows(jobs, watched, active, statuses, kind='all', page=1, page_size=6, preferences=None):
    """No arbitrary paths, card payloads or prompts in board rows."""
    entries=[]
    preferences = preferences or {}
    managed={str(job.get('sess') or '').removeprefix('session-') for job in jobs.values()}
    for jid,job in jobs.items():
        status=statuses[jid]
        entries.append({'id':jid,'kind':'control','title':str(job.get('title') or job.get('cwd','').rstrip('/').split('/')[-1] or jid),
                        'goal':bool((job.get('goal') or {}).get('active')),'agent':job.get('agent','codex'),'at':job.get('started',0), 'status':status})
    observed={str(key):{'title':preferences.get(key, {}).get('title') or '历史观察会话',
                       'agent':preferences.get(key, {}).get('agent', 'desktop'), 'at':0,
                       'project':preferences.get(key, {}).get('project', '')} for key in watched if key not in managed}
    for icon,sid,project,title,mtime,_ in active:
        matches=[key for key in observed if key.startswith(sid)]
        for key in matches:
            observed[key]={'title':preferences.get(key, {}).get('title') or title or project or sid,
                           'project':project,'agent':{'🐋':'deepseek-harness','🤖':'claude','🧩':'qoder','🦞':'wcodebuddy'}.get(icon,'desktop'),'at':mtime}
    for key,value in observed.items():
        policy = preferences.get(key, {})
        mode = {'full':'完整播报','result':'仅结果','errors':'异常提醒'}.get(policy.get('mode','full'),'完整播报')
        label = '已暂停播报' if policy.get('paused') else ('近期有活动' if value['at'] else '历史观察') + ' · ' + mode
        entries.append({'id':key,'kind':'watch',**value,
                        'status':{'label':label,'icon':'👁',
                                  'error':False,'hint':'只读观察，不可直接遥控','can_stop':False,'can_go':False}})
    entries=[entry for entry in entries if kind=='all' or kind==entry['kind'] or kind=='errors' and entry['status']['error']]
    entries.sort(key=lambda row:(row['status']['can_stop'],row['at']),reverse=True)
    pages=max(1,(len(entries)+page_size-1)//page_size)
    page=max(1,min(page,pages))
    return entries[(page-1)*page_size:page*page_size],page,pages,len(entries)
