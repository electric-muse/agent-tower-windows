"""Offline regression: synthetic transcripts only, no credentials/network/real tasks."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
try:
    from compression import zstd
except ImportError:
    from backports import zstd
import tower_harness as h
from tower_delivery import Outbox
from tower_runtime import redact, http_failure, RedactedStream, private_json


def frame(events):
    return zstd.compress(b''.join(json.dumps(e).encode()+b'\n' for e in events))


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'project'/'11111111-1111-4111-8111-111111111111'/'session.v4.jsonl.zstd'
        self.path.parent.mkdir(parents=True)
        self.header = {'type':'session','version':4,'cwd':'/tmp/demo'}
        self.msg = {'type':'assistant/message','data':{'message':{'content':[{'type':'text','text':'hello'},{'type':'reasoning','text':'PRIVATE'}], 'stream':'PRIVATE'}}}
        self.path.write_bytes(frame([self.header,self.msg]))
        self.addCleanup(self.close)
    def close(self):
        for entry in h._READERS.values(): entry[0].close()
        h._READERS.clear(); h._HEADERS.clear()
    def test_discovery(self):
        self.assertEqual(h.files(self.tmp.name),[str(self.path)])
        self.assertEqual(h.metadata(self.path),('demo','DeepSeek Harness'))
    def test_corrupt_skipped(self):
        self.path.write_bytes(b"not a zstd frame")
        self.assertEqual(h.files(self.tmp.name),[])
    def test_skip_child(self):
        self.header['parentSession']='parent'; self.path.write_bytes(frame([self.header]))
        self.assertEqual(h.files(self.tmp.name),[])
    def test_filter_private_events(self):
        self.assertEqual(list(h.events(self.msg)),[('msg','hello')])
        self.assertEqual(list(h.events({'type':'tool/call','data':{'name':'shell','arguments':'PRIVATE'}})),[('tool','shell|')])
        self.assertEqual(list(h.events({'type':'user/message','data':{'content':'PRIVATE'}})),[])
    def test_append_restart(self):
        state={}; self.assertEqual(h.read_delta(self.path,state,True),b'')
        with self.path.open('ab') as f: f.write(frame([self.msg]))
        self.assertIn(b'hello',h.read_delta(self.path,state))
        self.close()
        self.assertEqual(h.read_delta(self.path,state),b'')
        with self.path.open('ab') as f: f.write(frame([self.msg]))
        self.assertIn(b'hello',h.read_delta(self.path,state))
    def test_partial_frame(self):
        state={}; h.read_delta(self.path,state,True)
        data=frame([self.msg]); cut=len(data)//2
        with self.path.open('ab') as f: f.write(data[:cut])
        self.assertEqual(h.read_delta(self.path,state),b'')
        with self.path.open('ab') as f: f.write(data[cut:])
        self.assertIn(b'hello',h.read_delta(self.path,state))
    def test_partial_line(self):
        state={};h.read_delta(self.path,state,True)
        with self.path.open('ab') as f:f.write(zstd.compress(b'{"type":"turn/start"'))
        self.assertEqual(h.read_delta(self.path,state),b'')
        with self.path.open('ab') as f:f.write(zstd.compress(b'}\n'))
        self.assertIn(b'turn/start',h.read_delta(self.path,state))
    def test_cursor_rollback(self):
        state={};h.read_delta(self.path,state,True);old=dict(state)
        with self.path.open('ab') as f:f.write(frame([self.msg]))
        first=h.read_delta(self.path,state)
        self.assertEqual(h.read_delta(self.path,old),first)
    def test_replacement_baselines(self):
        state={};h.read_delta(self.path,state,True)
        other=self.path.with_suffix('.new');other.write_bytes(frame([self.header,self.msg]));other.replace(self.path)
        self.assertEqual(h.read_delta(self.path,state),b'')


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'outbox.sqlite';self.box=Outbox(self.path)
    def put(self):return self.box.put(('msg','fixture','hash'),{'text':'answer'},'original-root','hash')
    def test_durable_dedup(self):
        row=self.put();self.assertEqual(row['id'],self.put()['id'])
        self.assertEqual(Outbox(self.path).due()[0]['root'],'original-root')
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
    def test_success(self):
        row=self.put();self.box.finish(row['id'],'message-id')
        self.assertFalse(self.box.due());self.assertFalse(self.box.retry(row['id']))
        self.assertEqual(self.put()['payload'],'{}')
    def test_permanent_and_manual_retry(self):
        row=self.put();self.box.finish(row['id'],error={'http_status':400,'retryable':False})
        self.assertEqual(self.box.summary()['counts'],{'failed':1})
        self.assertTrue(self.box.retry(row['id']))
        self.assertEqual(self.box.due()[0]['root'],'original-root')
        self.assertEqual(self.box.due()[0]['id'],row['id'])
    def test_bounded_retry(self):
        row=self.put()
        for i in range(4):
            result=self.box.finish(row['id'],error={'retryable':True})
            self.assertEqual(result,'pending' if i<3 else 'failed')
        self.assertFalse(self.box.due())
    def test_backoff(self):
        row=self.put();self.box.finish(row['id'],error={'retryable':True,'retry_after':100})
        self.assertFalse(self.box.due())
    def test_safe_http(self):
        err=urllib.error.HTTPError('https://example.invalid',400,'bad',{},io.BytesIO(b'{"code":123,"msg":"PRIVATE","error":{"log_id":"req1"}}'))
        out=http_failure(err);err.close()
        self.assertFalse(out['retryable']);self.assertEqual(out['code'],'123');self.assertNotIn('PRIVATE',json.dumps(out))
    def test_rate_limit(self):
        err=urllib.error.HTTPError('https://example.invalid',429,'bad',{'Retry-After':'60'},io.BytesIO(b'{}'))
        self.assertEqual(http_failure(err)['retry_after'],60);err.close()
    def test_private_json(self):
        p=self.path.with_suffix('.json');private_json(p,{'safe':True})
        self.assertEqual(p.stat().st_mode & 0o777,0o600)
    def test_redaction(self):
        for value in ['wss://example.invalid/ws?access_key=PRIVATE&ticket=PRIVATE','token=PRIVATE','{"access_key":"PRIVATE"}']:
            self.assertNotIn('PRIVATE',redact(value))
    def test_split_redaction(self):
        p=self.path.with_suffix('.log');stream=RedactedStream(io.StringIO(),p)
        stream.write('wss://example.invalid/ws?access_');stream.write('key=PRIVATE\n')
        self.assertNotIn('PRIVATE',p.read_text())


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        import tower_sync as sync
        import tower_ctl as ctl
        self.sync,self.ctl=sync,ctl
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.sender=sync.Sender()
        for name,value in [('STATE',str(self.root/'sync.json')),('STATED',str(self.root)),
                           ('TEXTSENT',str(self.root/'sent')),('LOGD',str(self.root)),
                           ('DRY',False),('_MSGTRY',{}),('_MSGPEND',{}),('SENDER',self.sender),
                           ('conf',lambda: sync.DEF)]:
            ctx=patch.object(sync,name,value);ctx.start();self.addCleanup(ctx.stop)
        self.key=('msg','fixture',1)
        sync._MSGTRY[self.key]=['hash','answer',0];sync._MSGPEND['hash']=True
    def enqueue(self):
        with patch.object(self.sync,'value_root',return_value='original-root'):
            self.sender.enqueue(self.key,'create',card={'card':{}})
    def test_enqueue_failure_releases_memory(self):
        with patch.object(Outbox,'put',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.enqueue()
        self.assertNotIn(self.key,self.sync._MSGTRY)
        self.assertNotIn('hash',self.sync._MSGPEND)
    def test_sender_replay(self):
        self.enqueue()
        self.assertTrue(self.sender.wake.is_set())
        with patch.object(self.sync,'app_send',return_value='message-id') as send, patch.object(self.sync.time,'sleep'):
            self.sender.replay_due()
        row=self.sender.outbox().put(('msg','fixture','hash'),{},'', 'hash')
        self.assertEqual(send.call_args.args[1:],('original-root',row['id']))
        self.assertEqual(self.sender.take_results()[self.key],('created','message-id'))
        self.assertEqual(row['status'],'sent')
    def test_sender_permanent_failure(self):
        self.enqueue()
        def failure(*args):
            self.sync._API_ERROR.value={'retryable':False,'http_status':400};return None
        with patch.object(self.sync,'app_send',side_effect=failure),patch.object(self.sync.time,'sleep'):
            self.sender.replay_due()
        self.assertEqual(self.sender.outbox().summary()['counts'],{'failed':1})
        self.assertNotIn(self.key,self.sync._MSGTRY)
        self.assertFalse((self.root/'sent').exists())
    def test_scan_rolls_back_cursor_on_outbox_failure(self):
        import copy
        src=self.root/'fixture.jsonl';src.write_text('{"fixture":1}\n')
        original={'off':0,'armed':True,'task':'fixture','proj':'demo','ev':0,'seg':None}
        snapshot={str(src):copy.deepcopy(original)}
        cfg=dict(self.sync.DEF) if hasattr(self.sync,'DEF') else self.sync.conf().copy()
        cfg.update(seg_mode=0,poll=0.001,save_gap=0)
        captured=[]
        with patch.object(self.sync,'conf',return_value=cfg), \
             patch.object(self.sync,'app_mode',return_value=True), \
             patch.object(self.sender,'start'), \
             patch.object(self.sender,'thread',unittest.mock.Mock(is_alive=lambda: True)), \
             patch.object(self.sync,'load_state',return_value=snapshot), \
             patch.object(self.sync,'_scan_files',return_value=[str(src)]), \
             patch.object(self.sync,'events_of',return_value=iter([('msg','fixture answer')])), \
             patch.object(self.sync,'value_root',return_value='root'), \
             patch.object(self.sync,'sent_set',return_value=set()), \
             patch.object(self.sync,'title_of',return_value='fixture'), \
             patch.object(self.sync,'note_of2',return_value='fixture'), \
             patch.object(Outbox,'put',side_effect=OSError('disk full')), \
             patch.object(self.sync,'save_state',side_effect=lambda v: captured.append(copy.deepcopy(v)) or True), \
             patch.dict(os.environ,{'SYNC_TEST_SECS':'-1'}):
            self.sync.scan_dir(test_dir=str(self.root))
        self.assertEqual(captured[-1][str(src)]['off'],0)
        self.assertEqual(captured[-1][str(src)]['ev'],0)
    def test_harness_watch_root_template(self):
        path='/fixture/11111111-1111-4111-8111-111111111111/session.v4.jsonl.zstd'
        with patch.object(self.ctl,'find_rollout',return_value=(path,h.session_id(path))), \
             patch.object(self.ctl,'STOPICF',str(self.root/'sess-topics.json')), \
             patch.object(self.ctl,'_st_load',return_value={}),patch.object(self.ctl,'_st_save'), \
             patch.object(h,'metadata',return_value=('demo','fixture')), \
             patch.object(self.ctl,'conf',return_value={'topic_title_template':'{agent} | {title}'}), \
             patch.object(self.ctl,'action_card',return_value='root') as card, \
             patch.object(self.ctl,'api',return_value={}),patch.object(self.ctl,'audit'):
            self.ctl.watch_session('chat','11111111',quiet=True)
        self.assertEqual(card.call_args.args[1],'deepseek-harness | fixture')
        self.assertIn('只读',str(card.call_args.args[2]))
    def test_state_disk_full(self):
        with patch.object(self.sync,'private_json',side_effect=OSError('disk full')):
            self.assertFalse(self.sync.save_state({}))
    def test_harness_routing_title(self):
        path='/fixture/11111111-1111-4111-8111-111111111111/session.v4.jsonl.zstd'
        with patch.object(self.sync,'jobs_raw',return_value={}),patch.object(self.sync,'jid_map',return_value={}),patch.object(self.sync,'sess_topics',return_value={h.session_id(path):'root'}):
            self.assertEqual(self.sync.value_root(('msg',path,1)),'root')
            self.assertEqual(self.sync.card_kind(path),'🐋')
            with patch.object(self.sync,'title_config',return_value={'stream_title_template':'{agent} | {title}'}):
                self.assertEqual(self.sync.title_of(path,{'task':'fixture'}),'deepseek-harness | fixture')
    def test_health_does_not_confuse_liveness(self):
        import time
        (self.root/'sync.heartbeat').write_text(str(time.time()))
        (self.root/'health.json').write_text(json.dumps({'updated_at':time.time(),'ws_connected':False}))
        with patch('sys.stdout',io.StringIO()):self.assertEqual(self.sync.doctor(),1)
    def test_health_command(self):
        with patch.object(self.ctl,'STATED',str(self.root)),patch.object(self.ctl,'LOGD',str(self.root)),patch.object(self.ctl,'reply') as reply:
            self.ctl.cmd_health('sender','chat',[])
        self.assertIn('WebSocket',reply.call_args.args[1])
    def test_find_rejects_globs(self):
        self.assertEqual(self.ctl.find_rollout('*'),('',''))
    def test_audit_never_records_plain_prompt(self):
        with patch.object(self.ctl,'AUDIT',str(self.root/'audit.log')):
            self.ctl.audit('user','PRIVATE secret prompt','DENY(reason)')
        self.assertNotIn('PRIVATE',(self.root/'audit.log').read_text())


if __name__=='__main__':unittest.main()
