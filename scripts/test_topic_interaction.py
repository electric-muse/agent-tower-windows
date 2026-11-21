"""Offline regressions for watch activation, thread replies and question notices."""
import json
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import tower_ctl as ctl
import tower_sync as sync
import tower_harness as harness
from tower_questions import question_text


class Interaction(unittest.TestCase):
    def tearDown(self):
        ctl._REPLY_CONTEXT.route = {}

    def test_reply_card_and_text_stay_in_thread_without_main_fallback(self):
        ctl._REPLY_CONTEXT.route = {'chat_id': 'chat', 'message_id': 'incoming'}
        with patch.object(ctl, 'api', return_value={'code': 1}) as api:
            ctl.reply('chat', '反馈')
            ctl.reply_card('chat', '详情', 'grey', [])
            for call in api.call_args_list:
                self.assertEqual(call.args[1], '/im/v1/messages/incoming/reply')
                self.assertTrue(call.args[2]['reply_in_thread'])
            ctl.reply('other-chat', '其他群')
            self.assertIn('receive_id_type=chat_id', api.call_args.args[1])

    def test_message_preserves_root_and_message_id_without_event_identity(self):
        msg = NS(chat_id='chat', chat_type='group', parent_id='parent', root_id='root',
                 thread_id='', message_id='', content=json.dumps({'text': '#help'}))
        event = NS(event=NS(sender=NS(sender_id=NS(open_id='user')), message=msg))
        with tempfile.TemporaryDirectory() as tmp, patch.object(ctl, 'STATED', tmp), \
             patch.object(ctl, 'Q', queue.Queue()) as q, patch.object(ctl, 'log'):
            ctl.on_message(event)
            item = q.get_nowait()
            self.assertEqual(item[-1]['reply_route']['root_id'], 'root')
            self.assertEqual(item[-1]['reply_route']['message_id'], 'root')

    def test_worker_routes_root_only_and_clears_context_for_next_message(self):
        class Done(BaseException): pass
        class Once:
            def __init__(self):
                self.items = [('user', 'chat', '#unknown', '', '',
                               {'reply_route': {'chat_id': 'chat', 'root_id': 'root', 'message_id': 'incoming'}}),
                              ('user', 'chat', '#unknown', '', '')]
            def get(self):
                if self.items: return self.items.pop(0)
                raise Done()
        with patch.object(ctl, 'Q', Once()), patch.object(ctl, 'conf', return_value={**ctl.DEF, 'allow_ou': 'user'}), \
             patch.object(ctl, 'rate_ok', return_value=True), patch.object(ctl, 'wiz_get', return_value=None), \
             patch.object(ctl, 'log'), patch.object(ctl, 'api', return_value={'code': 0}) as api:
            with self.assertRaises(Done): ctl.worker()
            self.assertEqual(api.call_args_list[0].args[1], '/im/v1/messages/incoming/reply')
            self.assertIn('receive_id_type=chat_id', api.call_args_list[1].args[1])
            self.assertEqual(ctl._REPLY_CONTEXT.route, {})

    def test_failed_watch_button_remains_retryable(self):
        with patch.object(ctl, 'conf', return_value={**ctl.DEF, 'allow_ou': 'user'}), \
             patch.object(ctl, 'rate_ok', return_value=True), \
             patch.object(ctl, 'watch_session', return_value=(None, '话题回复失败')), \
             patch.object(ctl, 'reply') as reply, patch.object(ctl, 'mark_card') as mark:
            ctl.handle_ui('user', 'chat', {'act': 'watch_sess', 'sess8': 'aaaaaaaa'}, 'card')
            mark.assert_not_called()
            self.assertIn('纳管未完成', reply.call_args.args[1])

    def test_watch_arms_and_rotates_existing_main_segment(self):
        state = {'armed': False, 'off': 123, 'seg': {'no': 1, 'mid': 'main-card', 'roll': [['tool', '', 1]]},
                 'prev_seg': {'mid': 'older-main-card'}}
        with patch.object(sync, 'value_root', return_value='watch-root'):
            self.assertTrue(sync.reconcile_topic('fixture', state))
            self.assertTrue(state['armed'])
            self.assertEqual(state['seg']['mid'], '')
            self.assertEqual(state['seg']['no'], 2)
            self.assertNotIn('prev_seg', state)
            self.assertEqual(state['off'], 123)
            self.assertFalse(sync.reconcile_topic('fixture', state))

    def test_route_change_preserves_unsent_tools(self):
        state = {'seg': {'no': 1, 'roll': [['tool', '', 1]], 'mid': ''}}
        with patch.object(sync, 'value_root', return_value='watch-root'):
            sync.reconcile_topic('fixture', state)
        self.assertEqual(state['seg']['roll'], [['tool', '', 1]])
        self.assertTrue(state['pending_seg'])

    def test_topic_cache_retries_corruption_and_clears_deleted_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sync, 'STATED', tmp), \
             patch.object(sync, '_STOPIC', {'mt': 0, 'd': {}}):
            path = Path(tmp) / 'sess-topics.json'
            path.write_text('broken')
            sync.sess_topics()
            self.assertEqual(sync._STOPIC['mt'], 0)
            path.write_text(json.dumps({'session': 'root'}))
            self.assertEqual(sync.sess_topics(), {'session': 'root'})
            path.unlink()
            self.assertEqual(sync.sess_topics(), {})

    def test_questions_only_expose_user_facing_fields_across_engines(self):
        arguments = {'questions': [{'question': '选择方案？', 'options': [{'label': 'A', 'secret': 'PRIVATE'}]}],
                     'secret': 'PRIVATE'}
        codex = {'type': 'response_item', 'payload': {'type': 'function_call',
                 'name': 'functions.request_user_input', 'arguments': json.dumps(arguments)}}
        dsh = {'type': 'tool/call', 'data': {'name': 'ask_user_question', 'arguments': arguments}}
        claude = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'AskUserQuestion', 'input': arguments}]}}
        for events in (list(sync.events_of(json.dumps(codex))), list(harness.events(dsh)),
                       list(sync._claude_events(claude))):
            self.assertEqual(events[0][0], 'question')
            self.assertIn('选择方案', events[0][1])
            self.assertIn('1. A', events[0][1])
            self.assertNotIn('PRIVATE', events[0][1])
        self.assertEqual(question_text('shell', arguments), '')
        self.assertEqual(question_text('request_user_input', '{bad'), '')


if __name__ == '__main__': unittest.main()
