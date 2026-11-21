"""DeepSeek control: fake CLI + isolated controller, never real model/Feishu calls."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import tower_ctl as ctl
import tower_sync as sync
import tower_dsh as dsh

SID = 'session-11111111-1111-4111-8111-111111111111'


class HarnessControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.config = {**ctl.DEF, 'work_roots':str(self.root), 'allow_ou':'fixture', 'auto_watch':0}
        for name,value in {'conf':lambda:self.config,'STATED':str(self.root),
                           'JOBSF':str(self.root/'jobs.json'),'audit':Mock(),'reply':Mock(),
                           'root_topic':Mock(),'log':Mock(),'tsend':Mock(),
                           '_wiz':{}, '_rate':{}, '_PROJ':{'ts':0,'list':[]}}.items():
            self.stack.enter_context(patch.object(ctl,name,value))
        ctl.jobs_save({})
        self.spawn=self.stack.enter_context(patch.object(dsh,'spawn',return_value=Mock(pid=999999)))
        self.stack.enter_context(patch.object(ctl.threading,'Thread'))
    def start(self,*args):
        ctl.cmd_dnew('fixture','chat',[str(self.root),*args])
        return next(iter(ctl.jobs_load().values()),{})
    def job(self,**kw):
        jd={'jid':'dj1','agent':'deepseek-harness','cwd':str(self.root),'state':'exited',
            'sess':SID,'perm':'safe','mode':'','chat_id':'chat','out_file':str(self.root/'job-dj1.out'),**kw}
        ctl.jobs_save({'dj1':jd}); return jd
    def test_new(self):
        jd=self.start('标题=fixture','权限=safe','write code')
        self.assertEqual(jd['jid'],'dj1');self.assertEqual(jd['agent'],'deepseek-harness')
        self.assertEqual(self.spawn.call_args.kwargs['perm'],'safe')
        self.assertEqual(self.spawn.call_args.args[2],'write code')
        self.assertEqual(jd['title'],'fixture')
    def test_directory_denied(self):
        ctl.cmd_dnew('fixture','chat',['/not/allowed','write'])
        self.spawn.assert_not_called()
    def test_missing_prompt(self):
        self.start('标题=fixture');self.spawn.assert_not_called()
    def test_max_jobs(self):
        self.config['max_jobs']=1;self.job(state='running')
        self.start('write');self.spawn.assert_not_called()
    def test_go(self):
        self.job();ctl.cmd_go('fixture','chat',['dj1','continue'])
        self.assertEqual(self.spawn.call_args.kwargs['session'],SID)
        self.assertEqual(self.spawn.call_args.kwargs['perm'],'safe')
        self.assertEqual(ctl.jobs_load()['dj1']['state'],'running')
    def test_go_plan_preserved(self):
        self.job(mode='plan');ctl.cmd_go('fixture','chat',['dj1','continue'])
        self.assertEqual(self.spawn.call_args.kwargs['mode'],'plan')
    def test_go_initializing(self):
        self.job(state='running',sess='');ctl.cmd_go('fixture','chat',['dj1','continue'])
        self.spawn.assert_not_called()
    def test_model_rejected_without_stop(self):
        self.job(state='running')
        with patch.object(ctl,'_kill_group') as kill:
            ctl.cmd_model('fixture','chat',['dj1','other']);kill.assert_not_called()
        self.spawn.assert_not_called();self.assertEqual(ctl.jobs_load()['dj1']['state'],'running')
    def test_stop_owned(self):
        self.job(state='running',pid=999999)
        with patch.object(ctl,'_kill_group') as kill:
            ctl.cmd_stop('fixture','chat',['dj1']);kill.assert_called_once_with(999999)
        self.assertFalse(ctl.jobs_load()['dj1'].get('retry_at'))
    def test_goal_next_round_uses_dsh(self):
        jd=self.job(exit='0', goal={'text':'fixture goal','active':True,'rounds':0,'max':3})
        self.assertTrue(ctl.goal_tick(jd))
        self.assertEqual(self.spawn.call_args.kwargs['session'],SID)
    def test_goal_kick_uses_dsh(self):
        class Done(BaseException):pass
        class Q:
            def __init__(self):self.items=[('goal-kick','fixture','chat','dj1')]
            def get(self):
                if self.items:return self.items.pop()
                raise Done()
        self.job(goal={'text':'fixture goal','active':True})
        with patch.object(ctl,'Q',Q()),self.assertRaises(Done):ctl.worker()
        self.assertEqual(self.spawn.call_args.kwargs['session'],SID)
    def test_disk_failure_stops_new_process(self):
        with patch.object(ctl,'jobs_save',side_effect=OSError('disk full')),patch.object(ctl,'_kill_group') as kill:
            self.start('write');kill.assert_called_once_with(999999)
    def test_session_and_final(self):
        jd=self.job();Path(jd['out_file']).write_text(json.dumps({'type':'session','sessionId':SID})+'\n'+json.dumps({'type':'final','text':'done'})+'\n')
        self.assertEqual(ctl.find_sess('dj1'),SID)
        self.assertEqual(ctl.final_text(jd),'done')
    def test_latest_round_no_old_final(self):
        jd=self.job();Path(jd['out_file']).write_text(json.dumps({'type':'final','text':'old'})+'\n'+json.dumps({'type':'session','sessionId':SID})+'\n')
        self.assertEqual(ctl.final_text(jd),'')
    def test_managed_stream_route(self):
        jd=self.job(root_mid='remote-root')
        path='/fixture/'+SID+'/session.v4.jsonl.zstd'
        with patch.object(sync,'jid_map',return_value=sync.jid_by_sessfile({'dj1':jd})),patch.object(sync,'jobs_raw',return_value={'dj1':jd}):
            self.assertEqual(sync.jid_of(path),'dj1')
            self.assertEqual(sync.value_root(('msg',path,1)),'remote-root')
    def test_root_controls(self):
        _,elements,_=ctl.root_card_parts('dj1',self.job(title='fixture'))
        text=json.dumps(elements,ensure_ascii=False)
        self.assertIn('继续下指令',text);self.assertNotIn('换模型',text)
        self.assertIn('"fam": "dsh"',text)
    def test_resume_external(self):
        path=str(self.root/SID/'session.v4.jsonl.zstd')
        with patch.object(ctl,'find_rollout',return_value=(path,SID.removeprefix('session-'))),patch.object(ctl.harness,'header',return_value={'id':SID,'cwd':str(self.root)}),patch.object(ctl.harness,'metadata',return_value=('demo','fixture')),patch.object(dsh,'assert_available'):
            ctl.cmd_dresume('fixture','chat',['11111111','continue'])
        self.assertEqual(self.spawn.call_args.kwargs['session'],SID)
        jd=next(iter(ctl.jobs_load().values()))
        self.assertEqual(jd['sess'],'')  # only claim after the CLI emits session identity
        self.assertEqual(jd['requested_sess'],SID)
    def test_resume_busy(self):
        with patch.object(ctl,'find_rollout',return_value=(str(self.root/'session.v4.jsonl.zstd'),'id')),patch.object(ctl.harness,'header',return_value={'id':SID,'cwd':str(self.root)}),patch.object(dsh,'assert_available',side_effect=ValueError('busy')):
            ctl.cmd_dresume('fixture','chat',['11111111','continue'])
        self.spawn.assert_not_called()
    def test_resume_duplicate(self):
        self.job();ctl._start_dsh('fixture','chat',str(self.root),'continue','fixture','safe','',SID)
        self.spawn.assert_not_called()
    def test_worker_auth(self):
        class Done(BaseException):pass
        class Q:
            def __init__(self):self.items=[('unauthorized','chat','#dnew /tmp do work','','')]
            def get(self):
                if self.items:return self.items.pop()
                raise Done()
        with patch.object(ctl,'Q',Q()),self.assertRaises(Done):ctl.worker()
        self.spawn.assert_not_called()
    def test_family_prompt_dispatch(self):
        class Done(BaseException):pass
        class Q:
            def __init__(self):self.items=[('fixture','chat','do work','','')]
            def get(self):
                if self.items:return self.items.pop()
                raise Done()
        ctl.wiz_set('fixture','chat',step='prompt_fam',fam='dsh',cwd=str(self.root))
        with patch.object(ctl,'Q',Q()),self.assertRaises(Done):ctl.worker()
        self.assertEqual(next(iter(ctl.jobs_load().values()))['agent'],'deepseek-harness')


class Adapter(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.binary=self.root/'fake-dsh'
        self.binary.write_text(f'''#!{sys.executable}
import json,sys
assert sys.argv[1:4] == ['--profile','headless','--json']
assert sys.argv[-1] == '-'
assert sys.stdin.read() == '--not-an-option\\nfixture'
print(json.dumps({{'type':'thinking','text':'PRIVATE'}}))
print(json.dumps({{'type':'tool_call','input':'PRIVATE'}}))
print(json.dumps({{'type':'session','sessionId':'{SID}'}}))
print(json.dumps({{'type':'final','text':'done'}}))
print('PRIVATE',file=sys.stderr)
''');self.binary.chmod(0o700)
    def test_fake_cli_e2e(self):
        with (self.root/'out').open('wb') as output:
            process=dsh.spawn(str(self.binary),str(self.root),'--not-an-option\nfixture',output,session=SID)
            self.assertEqual(process.wait(timeout=15),0)
        text=(self.root/'out').read_text()
        self.assertNotIn('PRIVATE',text);self.assertIn('done',text);self.assertIn(SID,text)
    def test_permissions_and_stdin(self):
        for mode,perm,expected in [('plan','full','read-only'),('','safe','workspace-write'),('','full','danger-full-access')]:
            with patch.object(dsh.subprocess,'Popen') as popen:
                dsh.spawn(str(self.binary),str(self.root),'PRIVATE',io.BytesIO(),perm=perm,mode=mode)
                self.assertEqual(popen.call_args.kwargs['env']['DSH_PERMISSION_MODE'],expected)
                self.assertNotIn('PRIVATE',str(popen.call_args.args))
                self.assertTrue(popen.call_args.kwargs['start_new_session'])
    def test_launchd_node_path(self):
        with patch.dict(os.environ,{'PATH':'/usr/bin:/bin'}):
            env=dsh.runtime_env(node_bin=str(self.binary))
        self.assertEqual(env['PATH'].split(os.pathsep)[0],str(self.root))
    def test_invalid_explicit_node_fails_closed(self):
        with self.assertRaises(ValueError):dsh.runtime_env(node_bin='/missing-node')
    def test_invalid_session(self):
        with self.assertRaises(ValueError):dsh.spawn(str(self.binary),str(self.root),'task',io.BytesIO(),session='--bad')
    def test_missing_binary(self):
        with self.assertRaises(ValueError):dsh.spawn('/missing',str(self.root),'task',io.BytesIO())
    def test_source_lock_respected(self):
        lock=self.root/'session.lock';lock.touch()
        with lock.open('rb') as owner:
            fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(ValueError):dsh.assert_available(self.root/'session.v4.jsonl.zstd')
        self.assertTrue(lock.exists());dsh.assert_available(self.root/'session.v4.jsonl.zstd')
    def test_no_lock_creation(self):
        dsh.assert_available(self.root/'session.v4.jsonl.zstd')
        self.assertFalse((self.root/'session.lock').exists())
    def test_fake_cli_failure(self):
        self.binary.write_text(f'#!{sys.executable}\nimport sys\nprint("PRIVATE",file=sys.stderr)\nsys.exit(7)\n')
        with (self.root/'out').open('wb') as output:
            process=dsh.spawn(str(self.binary),str(self.root),'task',output)
            self.assertEqual(process.wait(timeout=15),7)
        text=(self.root/'out').read_text();self.assertIn('DSH_RUN_FAILED_RC_7',text);self.assertNotIn('PRIVATE',text)


if __name__=='__main__':unittest.main()
