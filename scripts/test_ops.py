"""Diagnostics, task board, retention and bounded zstd regression; offline only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
import tower_diagnostics as diag
import tower_dsh as dsh
import tower_panel as panel
import tower_retention as retention
import tower_harness as harness
from tower_delivery import Outbox
import tower_ctl as ctl


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()


class Diagnostics(Fixtures):
    def test_categories(self):
        cases={'invalid api key PRIVATE':'auth','HTTP 402 PRIVATE':'quota',
               'SessionAlreadyOwnedError PRIVATE':'session_busy','session abc does not exist':'session_missing',
               'agent preset PRIVATE cannot be adopted':'session_incompatible',
               'approval unavailable PRIVATE':'approval_required','permission denied PRIVATE':'permission',
               'EPERM opening headless profile':'permission',
               'Cannot find package PRIVATE':'configuration','HTTP 429 PRIVATE':'rate_limit',
               'ECONNRESET PRIVATE':'network','HTTP 503 PRIVATE':'upstream','opaque PRIVATE':'unknown'}
        for raw,wanted in cases.items():
            with self.subTest(category=wanted):
                self.assertEqual(diag.classify(raw),wanted)
                result=diag.diagnostic(diag.classify(raw))
                self.assertNotIn('PRIVATE',str(result))
                self.assertEqual(result['retryable'],wanted in ('network','upstream','rate_limit'))
    def test_chunk_boundaries_and_fixed_output(self):
        capture=diag.Capture();capture.feed(b'invalid api ');capture.feed(b'key PRIVATE')
        self.assertEqual(capture.result()['category'],'auth')
        self.assertNotIn('PRIVATE',str(capture.result()))
    def test_permanent_error_survives_later_transient_noise(self):
        capture=diag.Capture();capture.feed(b"invalid api key")
        capture.feed(b"x"*4096);capture.feed(b"HTTP 503")
        self.assertEqual(capture.result()["category"],"auth")
        self.assertEqual(diag.prefer("network","quota"),"quota")
    def test_untrusted_retry_flag_ignored(self):
        path=self.root/'out';path.write_text(json.dumps({'type':'diagnostic','category':'auth','retryable':True,'action':'PRIVATE'}))
        result=diag.read_run(path)['diagnostic'];self.assertFalse(result['retryable']);self.assertNotIn('PRIVATE',str(result))
    def test_new_round_resets_stale_error(self):
        path=self.root/'out';path.write_text(json.dumps(diag.diagnostic('network'))+'\n'+json.dumps({'type':'run_start'})+'\n')
        self.assertIsNone(diag.read_run(path)['diagnostic'])
        self.assertTrue(ctl._semantic_fail({'agent':'deepseek-harness','out_file':str(path)}))
    def test_only_transient_retries(self):
        path=self.root/'out'
        for kind in diag.KINDS:
            path.write_text(json.dumps(diag.diagnostic(kind)))
            self.assertEqual(not ctl._semantic_fail({'agent':'deepseek-harness','out_file':str(path)}),kind in ('network','upstream','rate_limit'))
    def test_noisy_stderr_pipe_and_no_leak(self):
        binary=self.root/'fake';binary.write_text(f'#!{sys.executable}\nimport sys,json\nsys.stderr.write("PRIVATE"*40000+"\\nauthentication failed PRIVATE\\n");sys.stderr.flush()\nprint(json.dumps({{"type":"error","message":"invalid api key PRIVATE"}}))\nsys.exit(1)\n');binary.chmod(0o700)
        out=self.root/'out'
        with out.open('wb') as file:child=dsh.spawn(str(binary),str(self.root),'fixture',file)
        self.assertEqual(child.wait(timeout=15),1)
        self.assertNotIn('PRIVATE',out.read_text());self.assertEqual(diag.read_run(out)['diagnostic']['category'],'auth')
    def test_approval_state_from_tool(self):
        binary=self.root/'fake';events=[{'type':'session','sessionId':'session-11111111-1111-4111-8111-111111111111'},
             {'type':'tool_call','tool':'request_user_approval','input':'PRIVATE'},
             {'type':'error','message':'approval unavailable PRIVATE'}]
        binary.write_text(f'#!{sys.executable}\nimport sys\nprint({chr(10).join(json.dumps(e) for e in events)!r})\nsys.exit(1)\n');binary.chmod(0o700)
        out=self.root/'out'
        with out.open('wb') as file:child=dsh.spawn(str(binary),str(self.root),'fixture',file)
        child.wait(timeout=15)
        self.assertEqual(diag.read_run(out)['phase'],'waiting_approval')
        self.assertEqual(diag.read_run(out)['diagnostic']['category'],'approval_required')
        self.assertNotIn('PRIVATE',out.read_text())


class Panel(Fixtures):
    def test_capabilities_by_state(self):
        for state,stop,go in [('running',True,False),('stopping',False,False),('exited',False,True),('stopped',False,True)]:
            status=panel.job_status({'state':state,'exit':'0','sess':'sid'})
            self.assertEqual((status['can_stop'],status['can_go']),(stop,go))
    def test_waiting_not_mislabelled_running(self):
        status=panel.job_status({'state':'running'},{'phase':'waiting_approval'})
        self.assertIn('审批',status['label']);self.assertFalse(status['error'])
    def test_observers_never_control(self):
        rows,page,pages,total=panel.rows({}, {'11111111-1111-4111-8111-111111111111':'root'},[],{},'watch')
        self.assertEqual(total,1);self.assertEqual(rows[0]['kind'],'watch')
        self.assertFalse(rows[0]['status']['can_go']);self.assertFalse(rows[0]['status']['can_stop'])
    def test_pagination_and_dedup(self):
        jobs={'dj1':{'state':'running','sess':'session-11111111-1111-4111-8111-111111111111'}}
        watched={f'{i:08x}-1111-4111-8111-111111111111':'root' for i in range(20)}
        watched['11111111-1111-4111-8111-111111111111']='root'
        rows,page,pages,total=panel.rows(jobs,watched,[],{'dj1':panel.job_status(jobs['dj1'])},page=99)
        self.assertEqual((page,pages,total),(4,4,21));self.assertEqual(len(rows),3)
    def test_error_filter(self):
        jobs={'j1':{'state':'exited','exit':'1'},'j2':{'state':'running'}}
        rows,_,_,total=panel.rows(jobs,{},[],{k:panel.job_status(v) for k,v in jobs.items()},kind='errors')
        self.assertEqual(total,1);self.assertEqual(rows[0]['id'],'j1')
    def test_status_signature_stable_timer(self):
        job={'state':'exited','retry_at':time.time()+30,'retry_cnt':1}
        self.assertEqual(panel.status_signature(job),panel.status_signature(job))
    def test_board_card_empty_and_filters(self):
        with patch.object(ctl,'jobs_load',return_value={}),patch.object(ctl,'_st_load',return_value={}),patch.object(ctl,'_scan_active',return_value=[]),patch.object(ctl,'reply_card') as reply:
            ctl.cmd_board('user','chat',['all','1'])
        text=json.dumps(reply.call_args.args[3],ensure_ascii=False)
        self.assertIn('暂无符合',text);self.assertIn('只读观察',text)
    def test_roots_no_stop_for_finished(self):
        with patch.object(ctl,'conf',return_value=ctl.DEF):
            _,elements,_=ctl.root_card_parts('dj1',{'state':'exited','exit':'0','sess':'fixture','agent':'deepseek-harness'})
        body=json.dumps(elements,ensure_ascii=False)
        self.assertNotIn('"kind": "stop"',body);self.assertNotIn('换模型',body);self.assertIn('已完成',body)


class Retention(Fixtures):
    def setUp(self):
        super().setUp();self.now=time.time();self.old=self.now-40*86400
        self.box=Outbox(self.root/'delivery.sqlite')
        self.path=self.root/'job-dj1.out';self.path.write_text('private fixture');os.utime(self.path,(self.old,self.old))
        self.jobs={'dj1':{'state':'exited','ended':self.old,'out_file':str(self.path)}}
    def plan(self):return retention.preview(self.root,self.jobs,self.box,{},self.now)
    def test_preview_no_mutation(self):
        plan=self.plan();self.assertEqual(len(plan['files']),1);self.assertTrue(self.path.exists())
        self.assertNotIn('private fixture',json.dumps(plan))
    def test_apply_preserves_identity_and_new_files(self):
        plan=self.plan();result=retention.apply(plan,self.root,self.jobs,self.box,{},self.now)
        self.assertEqual(result['files_removed'],1);self.assertFalse(self.path.exists());self.assertIn('dj1',self.jobs)
    def test_active_retry_and_goal_protected(self):
        for field,value in [('state','running'),('retry_at',self.now+20),('goal',{'active':True})]:
            old=dict(self.jobs['dj1']);self.jobs['dj1'][field]=value
            self.assertFalse(self.plan()['files']);self.jobs['dj1']=old
    def test_symlink_and_external_protected(self):
        outside=self.root/'outside';outside.write_text('not touched')
        self.path.unlink();self.path.symlink_to(outside)
        self.assertFalse(self.plan()['files']);self.assertEqual(outside.read_text(),'not touched')
    def test_recently_modified_output_protected(self):
        os.utime(self.path,None);self.assertFalse(self.plan()['files'])
    def test_changed_file_invalidates_preview(self):
        plan=self.plan();self.path.write_text('changed')
        with self.assertRaises(ValueError):retention.apply(plan,self.root,self.jobs,self.box,{},self.now)
        self.assertEqual(self.path.read_text(),'changed')
    def test_resumed_job_invalidates_preview(self):
        plan=self.plan();self.jobs['dj1']['state']='running'
        with self.assertRaises(ValueError):retention.apply(plan,self.root,self.jobs,self.box,{},self.now)
        self.assertTrue(self.path.exists())
    def test_expired_preview(self):
        with self.assertRaises(ValueError):retention.apply(self.plan(),self.root,self.jobs,self.box,{},self.now+601)
        self.assertTrue(self.path.exists())
    def test_failed_payload_tombstone(self):
        row=self.box.put(('msg','fixture','hash'),{'body':'private fixture'},'root','hash')
        with patch('tower_delivery.time.time',return_value=self.old):self.box.finish(row['id'],error={'retryable':False})
        pending=self.box.put(('msg','pending','hash2'),{'body':'keep'},'root','hash2')
        plan=self.plan();self.assertEqual(len(plan['failures']),1)
        result=retention.apply(plan,self.root,self.jobs,self.box,{},self.now)
        self.assertEqual(result['failed_payloads_expired'],1);self.assertFalse(self.box.retry(row['id']))
        self.assertEqual(self.box.put(('msg','fixture','hash'),{'body':'fixture'},'root')['status'],'expired')
        self.assertEqual(self.box.due()[0]['id'],pending['id'])
    def test_requeued_failure_not_expired(self):
        row=self.box.put(('msg','fixture','hash'),{'body':'fixture'},'root')
        with patch('tower_delivery.time.time',return_value=self.old):self.box.finish(row['id'],error={'retryable':False})
        plan=self.plan();self.box.retry(row['id'])
        with self.assertRaises(ValueError):retention.apply(plan,self.root,self.jobs,self.box,{},self.now)
        self.assertEqual(self.box.due()[0]['id'],row['id'])
    def test_wrong_owner_confirmation_denied(self):
        with patch.object(ctl,'STATED',str(self.root)),patch.object(ctl,'_retention_previews',{}),patch.object(ctl,'jobs_load',return_value=self.jobs),patch.object(ctl,'conf',return_value=ctl.DEF),patch.object(ctl,'reply') as reply:
            ctl.cmd_retention('user1','chat',[])
            token=ctl._retention_previews[('user1','chat')]['token']
            ctl.cmd_retention('user2','chat',['apply',token])
            self.assertIn('当前用户',reply.call_args.args[1]);self.assertTrue(self.path.exists())


class IncrementalFrames(Fixtures):
    def setUp(self):
        super().setUp();self.path=self.root/'session.v4.jsonl.zstd';self.state={}
        self.addCleanup(self.close)
    def close(self):
        for entry in harness._READERS.values():entry[0].close()
        harness._READERS.clear()
    def frame(self,text):return harness.zstd.compress(text)
    def drain(self,baseline=False):
        result=[]
        for i in range(1000):
            result.append(harness.read_delta(str(self.path),self.state,baseline and i==0))
            if self.state.get('dsh_wait_size')==self.path.stat().st_size:break
        else:self.fail('reader never drained')
        return b''.join(result)
    def test_append_large_session_avoids_history(self):
        record=json.dumps({'type':'ignored','text':'x'*4000}).encode()+b'\n'
        self.path.write_bytes(b''.join(self.frame(record*16) for _ in range(256)))
        self.assertEqual(self.drain(True),b'')
        before=harness.metrics()
        with self.path.open('ab') as file:file.write(self.frame(b'{"type":"turn/start"}\n'))
        self.assertIn(b'turn/start',self.drain())
        after=harness.metrics()
        self.assertLess(after['decoded_bytes']-before['decoded_bytes'],65536)
        self.assertEqual(after['reader_reopens'],before['reader_reopens'])
    def test_restart_uses_frame_checkpoint(self):
        line=b'{"text":"'+b'x'*4000+b'"}\n'
        self.path.write_bytes(b''.join(self.frame(line*16) for _ in range(128)))
        self.drain(True);self.assertGreater(self.state['dsh_checkpoint'][0],0);self.close()
        before=harness.metrics()
        with self.path.open('ab') as file:file.write(self.frame(b'{"type":"turn/start"}\n'))
        self.assertIn(b'turn/start',self.drain())
        self.assertLess(harness.metrics()['restore_bytes']-before['restore_bytes'],65536)
    def test_continuous_unfinished_frame_append(self):
        comp=harness.zstd.ZstdCompressor()
        self.path.write_bytes(comp.compress(b'{"old":1}\n',mode=comp.FLUSH_BLOCK))
        self.drain(True)
        with self.path.open('ab') as file:file.write(comp.compress(b'{"new":2}\n',mode=comp.FLUSH_BLOCK))
        self.assertIn(b'"new"',self.drain())
    def test_line_crossing_frame_boundary(self):
        self.path.write_bytes(self.frame(b'{"old":1}\n'));self.drain(True)
        with self.path.open('ab') as file:file.write(self.frame(b'{"ne'))
        self.assertEqual(self.drain(),b'');self.close()
        with self.path.open('ab') as file:file.write(self.frame(b'w":2}\n'))
        self.assertEqual(self.drain(),b'{"new":2}\n')
    def test_decompression_budget(self):
        self.path.write_bytes(self.frame(b'x'* (harness.BUDGET*3)+b'\n'))
        before=harness.metrics()['decoded_bytes']
        harness.read_delta(str(self.path),self.state,True)
        self.assertLessEqual(harness.metrics()['decoded_bytes']-before,harness.BUDGET)
    def test_invalid_persisted_cursor_rebaselines(self):
        self.path.write_bytes(self.frame(b'{"old":1}\n'))
        self.state={"dsh_offset":10000}
        self.assertEqual(self.drain(),b'')
        with self.path.open("ab") as file:file.write(self.frame(b'{"new":2}\n'))
        self.assertEqual(self.drain(),b'{"new":2}\n')
    def test_rollback_replays_exact_bytes(self):
        self.path.write_bytes(self.frame(b'{"old":1}\n'));self.drain(True)
        prior=json.loads(json.dumps(self.state))
        with self.path.open('ab') as file:file.write(self.frame(b'{"new":2}\n'))
        first=self.drain();self.state=prior
        self.assertEqual(self.drain(),first)


if __name__=='__main__':unittest.main()
