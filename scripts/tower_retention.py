"""Explicit preview/confirm retention. Never sweep a directory or delete dedup IDs.

Only allowlisted terminal-job files inside the state directory and failed outbox
payloads are eligible. Every file/row is revalidated during apply. Source sessions,
credentials, task metadata and pending deliveries are always out of scope.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

VERSION=1
MAX_ITEMS=250
JOB_ID=re.compile(r'[cwqd]?j\d+')


def days(config,key,default):
    try:return max(1,min(3650,int(config.get(key,default))))
    except (TypeError,ValueError):return default


def terminal(job,cutoff):
    return (job.get('state') in ('exited','stopped') and job.get('ended',0)>0
            and job['ended']<cutoff and not job.get('retry_at')
            and not (job.get('goal') or {}).get('active'))


def file_entry(root,name,jid):
    if not JOB_ID.fullmatch(jid):return None
    if name != f'job-{jid}.out' and not re.fullmatch(r'\.rc\.'+re.escape(jid)+r'\.[0-9a-f]{32}',name):return None
    try:
        info=(root/name).lstat()
        if not stat.S_ISREG(info.st_mode):return None  # do not follow symlinks
        return {'jid':jid,'name':name,'dev':info.st_dev,'ino':info.st_ino,'size':info.st_size,'mtime_ns':info.st_mtime_ns}
    except OSError:return None


def preview(state_dir,jobs,box,config,now=None):
    now=time.time() if now is None else now
    root=Path(state_dir).resolve()
    policy={'job_days':days(config,'retention_job_days',30),
            'failed_days':days(config,'retention_failed_days',14)}
    candidates=[]
    for jid,job in sorted(jobs.items()):
        if not JOB_ID.fullmatch(jid) or not terminal(job,now-policy['job_days']*86400):continue
        names={Path(job.get('out_file') or '').name,Path(job.get('rc_file') or '').name}
        # Never adopt an external path based only on a coinciding basename.
        names={name for name in names if any(Path(job.get(key) or '').name==name and Path(job.get(key) or '').parent.resolve()==root for key in ('out_file','rc_file'))}
        names.update(path.name for path in root.glob(f'.rc.{jid}.*'))
        for name in sorted(names):
            entry=file_entry(root,name,jid)
            if entry and entry["mtime_ns"]/1e9 < now-policy["job_days"]*86400:candidates.append(entry)
            if len(candidates)>=MAX_ITEMS:break
        if len(candidates)>=MAX_ITEMS:break
    failures=box.retention_candidates(now-policy['failed_days']*86400,MAX_ITEMS)
    plan={'v':VERSION,'created_at':now,'policy':policy,'files':candidates,'failures':failures}
    plan['digest']=fingerprint(plan)
    return plan


def fingerprint(plan):
    return hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k not in ('digest','created_at')},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def apply(plan,state_dir,jobs,box,config,now=None):
    now=time.time() if now is None else now
    if plan.get('v')!=VERSION or plan.get('digest')!=fingerprint(plan):raise ValueError('preview changed')
    if now-plan.get('created_at',0)>600 or plan.get('created_at',0)>now+5:raise ValueError('preview expired')
    current=preview(state_dir,jobs,box,config,now)
    if plan['digest']!=current['digest']:raise ValueError('candidates changed; preview again')
    root=Path(state_dir).resolve()
    removed=[];skipped=[]
    for entry in plan['files']:
        job=jobs.get(entry['jid'],{})
        if not terminal(job,now-plan['policy']['job_days']*86400) or file_entry(root,entry['name'],entry['jid'])!=entry:
            skipped.append(entry['name']);continue
        try:
            (root/entry['name']).unlink()
            removed.append(entry)
        except OSError:skipped.append(entry['name'])
    expired=box.expire_payloads(plan['failures'],now-plan['policy']['failed_days']*86400)
    return {'files_removed':len(removed),'bytes_removed':sum(x['size'] for x in removed),
            'failed_payloads_expired':expired,'skipped':len(skipped),
            'jobs':sorted({x['jid'] for x in removed if x['name'].endswith('.out')})}
