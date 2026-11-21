"""Image upload and routing regressions for watched Codex sessions."""
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tower_sync as sync
from tower_media import checked_image, codex_session_cwd, image_references


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/8k8AAAAASUVORK5CYII=')
SID = 'aaaaaaaa-1111-7111-8111-111111111111'


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / 'poster.png'
        self.image.write_bytes(PNG)

    def test_only_local_images_inside_session_are_selected(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        outside = Path(other.name) / 'outside.png'
        outside.write_bytes(PNG)
        text = (f'![封面](poster.png) ![重复]({self.image}) '
                f'![外部]({outside}) ![网址](https://example.com/a.png)')
        found = image_references(text, self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['path'], str(self.image.resolve()))
        self.assertEqual(found[0]['mime'], 'image/png')
        self.image.write_bytes(PNG + b'changed')
        with self.assertRaises(ValueError):
            checked_image(found[0]['path'], self.root, found[0]['sha256'])

    def test_codex_session_cwd_requires_matching_header(self):
        source = self.root / 'rollout.jsonl'
        source.write_text(json.dumps({'type': 'session_meta',
                                      'payload': {'id': SID, 'cwd': str(self.root)}}) + '\n',
                          encoding='utf-8')
        self.assertEqual(codex_session_cwd(source, SID), str(self.root))
        self.assertEqual(codex_session_cwd(source, 'other'), '')

    def test_upload_uses_multipart_and_image_message_stays_in_topic(self):
        payload = {'kind': 'local_image', **checked_image(self.image, self.root)}
        response = io.BytesIO(b'{"code":0,"data":{"image_key":"img_test"}}')
        with patch.object(sync, 'app_cfg', return_value={'chat_id': 'chat'}), \
             patch.object(sync, 'token', return_value='token'), \
             patch.object(sync.urllib.request, 'urlopen', return_value=response) as upload, \
             patch.object(sync, 'api', return_value={'code': 0, 'data': {'message_id': 'image-mid'}}) as send:
            self.assertEqual(sync.app_send(payload, 'topic-root', 'delivery-id'), 'image-mid')
        request = upload.call_args.args[0]
        self.assertIn('/im/v1/images', request.full_url)
        self.assertIn(b'name="image_type"', request.data)
        self.assertIn(PNG, request.data)
        self.assertEqual(send.call_args.args[1], '/im/v1/messages/topic-root/reply')
        body = send.call_args.args[2]
        self.assertEqual(body['msg_type'], 'image')
        self.assertEqual(json.loads(body['content']), {'image_key': 'img_test'})
        self.assertTrue(body['reply_in_thread'])
        self.assertEqual(body['uuid'], 'delivery-id')

    def test_queue_deduplicates_replayed_image(self):
        with patch.object(sync, 'STATE', str(self.root / 'sync.json')), \
             patch.object(sync, 'app_mode', return_value=True):
            identity = ('image', 'session-source', 'event')
            text = '成图 ![封面](poster.png)'
            self.assertEqual(sync.queue_local_images(identity, text, self.root, 'topic-root'), 1)
            self.assertEqual(sync.queue_local_images(identity, text, self.root, 'topic-root'), 1)
            rows = sync.SENDER.outbox().due()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['root'], 'topic-root')
        self.assertEqual(json.loads(rows[0]['payload'])['kind'], 'local_image')

    def test_queued_image_uses_durable_sender(self):
        sender = sync.Sender()
        with patch.object(sync, 'STATE', str(self.root / 'sync.json')), \
             patch.object(sync, 'DRY', False), \
             patch.object(sync, 'session_policy', return_value={'paused': False}), \
             patch.object(sync, 'app_send', return_value='image-mid') as send, \
             patch.object(sync.time, 'sleep'):
            payload = {'kind': 'local_image', **checked_image(self.image, self.root)}
            sender.outbox().put(('image', 'session', 'event', 0), payload, 'topic-root')
            sender.replay_due()
            self.assertEqual(sender.outbox().summary()['counts']['sent'], 1)
            self.assertEqual(send.call_args.args[1], 'topic-root')


if __name__ == '__main__':
    unittest.main()
