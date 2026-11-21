"""SQLite outbox. Durable before sending; bounded retries, explicit manual replay."""
from contextlib import contextmanager
import json
import os
import sqlite3
import time
import uuid
import hashlib
from pathlib import Path


class Outbox:
    def __init__(self, path): self.path = str(path)
    @contextmanager
    def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        c = sqlite3.connect(self.path, timeout=5)
        try:
            os.chmod(self.path, 0o600)
            c.row_factory = sqlite3.Row
            c.execute('''CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, key TEXT UNIQUE, payload TEXT, root TEXT, hash TEXT,
                status TEXT, attempts INTEGER, next_at REAL, created_at REAL, updated_at REAL,
                error TEXT, message_id TEXT)''')
            c.execute("CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status,next_at)")
            c.execute('CREATE TABLE IF NOT EXISTS message_plans (event TEXT PRIMARY KEY, max_len INTEGER, content_hash TEXT)')
            with c:
                yield c
        finally:
            c.close()
    def put(self, key, card, root, hash_value=''):
        k = json.dumps(key, ensure_ascii=False)
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex, k, json.dumps(card, ensure_ascii=False), root, hash_value,
                 'pending', 0, 0, time.time(), time.time(), '', ''))
            return dict(c.execute('SELECT * FROM deliveries WHERE key=?', (k,)).fetchone())
    def message_limit(self, event, text, limit):
        """Freeze splitting before any part is sent; retain hashes, never another body."""
        ident = hashlib.sha256(json.dumps(event, ensure_ascii=False).encode()).hexdigest()
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO message_plans VALUES (?,?,?)', (ident, int(limit), digest))
            row = c.execute('SELECT max_len,content_hash FROM message_plans WHERE event=?', (ident,)).fetchone()
            if row['content_hash'] != digest:
                raise ValueError('source event content changed')
            return row['max_len']
    def due(self, limit=20, after=None):
        with self.connect() as c:
            suffix = ' AND (created_at>? OR (created_at=? AND id>?))' if after else ''
            args = (time.time(), *([after[0], after[0], after[1]] if after else []), limit)
            return [dict(r) for r in c.execute("SELECT * FROM deliveries WHERE status='pending' AND next_at<=?" + suffix + " ORDER BY created_at,id LIMIT ?", args)]
    def finish(self, ident, mid='', error=None):
        with self.connect() as c:
            row=c.execute('SELECT * FROM deliveries WHERE id=?',(ident,)).fetchone()
            attempts=row['attempts']+1
            error=error or {}
            status='sent' if mid else ('pending' if error.get('retryable', True) and attempts < 4 else 'failed')
            delay=max(min(300, 5 * 2**(attempts-1)), error.get('retry_after',0))
            c.execute('UPDATE deliveries SET status=?, attempts=?, next_at=?, updated_at=?, error=?, message_id=? WHERE id=?',
                      (status,attempts,time.time()+delay,time.time(),json.dumps(error),mid,ident))
            # Successful payload is no longer needed (retain identity for dedup).
            if mid: c.execute("UPDATE deliveries SET payload='{}' WHERE id=?", (ident,))
            return status
    def retry(self, ident):
        # Full ID only, no arbitrary paths or routing overrides from chat.
        with self.connect() as c:
            return c.execute("UPDATE deliveries SET status='pending',attempts=0,next_at=0 WHERE id=? AND status='failed'",(ident,)).rowcount==1
    def summary(self):
        with self.connect() as c:
            counts=dict(c.execute('SELECT status,COUNT(*) FROM deliveries GROUP BY status'))
            failures=[dict(r) for r in c.execute("SELECT id,attempts,error,created_at FROM deliveries WHERE status='failed' ORDER BY created_at DESC LIMIT 10")]
            last=c.execute("SELECT MAX(updated_at) FROM deliveries WHERE status='sent'").fetchone()[0]
            recent=c.execute("SELECT COUNT(*) FROM deliveries WHERE status='failed' AND updated_at>=?", (time.time()-300,)).fetchone()[0]
            sources=[dict(r) for r in c.execute("SELECT json_extract(key,'$[1]') AS source, COUNT(*) AS count, MIN(created_at) AS oldest_at FROM deliveries WHERE status='pending' GROUP BY source")]
            return {'counts':counts,'failed':failures,'last_sent_at':last,
                    'recent_failed':recent,'pending_sources':sources}

    def retention_candidates(self, cutoff, limit=250):
        with self.connect() as c:
            return [dict(row) for row in c.execute(
                "SELECT id,updated_at,length(CAST(payload AS BLOB)) AS bytes FROM deliveries WHERE status='failed' AND updated_at<? AND payload!='{}' ORDER BY id LIMIT ?", (cutoff,limit))]

    def expire_payloads(self, candidates, cutoff):
        count=0
        with self.connect() as c:
            for row in candidates:
                count+=c.execute("UPDATE deliveries SET payload='{}',status='expired',error=?,updated_at=? WHERE id=? AND status='failed' AND updated_at=? AND updated_at<? AND length(CAST(payload AS BLOB))=?",
                                 ('{"kind":"retention_expired"}',time.time(),row['id'],row['updated_at'],cutoff,row['bytes'])).rowcount
        return count


class CommandInbox:
    """Durable command identities only: never persist prompt/callback payloads."""
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            os.chmod(self.path, 0o600)
            connection.execute('CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, status TEXT, queued_at REAL, updated_at REAL)')
            with connection:
                yield connection
        finally:
            connection.close()

    def claim(self, source, event_id):
        ident = hashlib.sha256(json.dumps([source, event_id]).encode()).hexdigest()
        with self.connect() as connection:
            inserted = connection.execute('INSERT OR IGNORE INTO commands VALUES (?,?,?,?)',
                                           (ident, 'queued', time.time(), time.time())).rowcount
        return ident if inserted else ''

    def mark(self, ident, status):
        if status not in ('processing', 'handled', 'failed', 'interrupted'):
            raise ValueError('invalid command state')
        with self.connect() as connection:
            connection.execute('UPDATE commands SET status=?,updated_at=? WHERE id=?', (status, time.time(), ident))

    def recover(self):
        # An interrupted side effect may already have happened. Never auto-replay it.
        with self.connect() as connection:
            return connection.execute("UPDATE commands SET status='interrupted',updated_at=? WHERE status IN ('queued','processing')", (time.time(),)).rowcount

    def summary(self):
        with self.connect() as connection:
            counts = dict(connection.execute('SELECT status,COUNT(*) FROM commands GROUP BY status'))
            oldest = connection.execute("SELECT MIN(queued_at) FROM commands WHERE status='queued'").fetchone()[0]
        return {'counts': counts, 'oldest_wait_seconds': max(0, int(time.time() - oldest)) if oldest else 0}
