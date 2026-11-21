#!/usr/bin/env python3
"""Explicit opt-in live Harness smoke, isolated logs/storage/workdir, no Feishu sends.

Uses installed headless profile/credentials via dsh, never reads/copies credentials.
Only fixed fixture content is requested. Emits safe assertions, not model output.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import time
import tower_dsh as dsh
from tower_diagnostics import read_run


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--binary',default=os.path.expanduser('~/.npm-global/bin/dsh'))
    parser.add_argument('--profile-patch', help='Existing local configuration overlay; never copied or printed')
    parser.add_argument('--keep',action='store_true',help='Keep isolated fixture for follow-up verification; remove explicitly afterward')
    parser.add_argument('--output',help='Safe JSON report destination')
    args=parser.parse_args()
    if not args.execute: parser.error('live model calls require --execute')
    os.umask(0o077)
    temp=tempfile.mkdtemp(prefix='tower-live-')
    root=Path(temp).resolve();work=root/'work';work.mkdir()
    patch=root/'isolate.yaml'
    patch.write_text('\n'.join([
        '- id: session-persistence-jsonl', '  config:', '    root: '+json.dumps(str(root/'sessions')),
        '- id: storage-json', '  config:', '    root: '+json.dumps(str(root/'storage')),
        '- id: session-title-llm', '  disabled: true',
    ])+'\n')
    launcher=root/'dsh-isolated'
    launcher.write_text('#!/bin/sh\nexec '+shlex.quote(args.binary)+((' --patch '+shlex.quote(str(Path(args.profile_patch).expanduser().resolve()))) if args.profile_patch else '')+' --patch '+shlex.quote(str(patch))+' "$@"\n')
    launcher.chmod(0o700)
    report={'root':str(root), 'checks':{},'runs':[]}
    children=[]
    def run(task,session=''):
        out=root/f'run-{len(report["runs"])}.jsonl'
        with out.open('wb') as file:
            child=dsh.spawn(str(launcher),str(work),task,file,session=session,perm='safe')
        children.append(child)
        try:code=child.wait(timeout=150)
        except __import__('subprocess').TimeoutExpired:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=5)
            except __import__('subprocess').TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
            code=-1
        state=read_run(out)
        sid=''
        for line in out.read_text().splitlines():
            event=json.loads(line)
            if event.get('type')=='session':sid=event.get('sessionId','')
        report['runs'].append({'exit':code,'category':(state.get('diagnostic') or {}).get('category'),'session_confirmed':bool(sid)})
        return code,sid
    try:
        code,sid=run('This is an authorized isolated integration test. In the current directory only, create tower-fixture.txt containing exactly TOWER_ROUND_ONE followed by a newline. Do not inspect other directories or use network tools. After writing it reply only TEST_DONE.')
        report['checks']['new_write'] = code==0 and (work/'tower-fixture.txt').exists() and (work/'tower-fixture.txt').read_text()=='TOWER_ROUND_ONE\n'
        if report['checks']['new_write']:
            code,sid2=run('Continue the isolated test. Read only tower-fixture.txt in this directory, then replace its contents with exactly TOWER_ROUND_TWO followed by a newline. Do not inspect other directories or use network tools. Reply only TEST_DONE.',sid)
            report['checks']['resume_write']=code==0 and sid2==sid and (work/'tower-fixture.txt').read_text()=='TOWER_ROUND_TWO\n'
        print(json.dumps(report,ensure_ascii=False))
        if args.output:Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if report['checks'].get('resume_write') else 1
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL);child.wait()
        if not args.keep: __import__('shutil').rmtree(root)


if __name__=='__main__':raise SystemExit(main())
