"""Read-only DeepSeek Harness v4 adapter. Never reads credentials or reasoning streams."""
import io
import json
import os
from pathlib import Path
import re
from collections import OrderedDict
from tower_questions import question_text

try:
    from compression import zstd
except ImportError:
    try:
        from backports import zstd
    except ImportError:
        zstd = None
DecodeError = zstd.ZstdError if zstd else RuntimeError

def _open(path):
    if zstd is None: raise RuntimeError('Harness support needs Python 3.14 or backports.zstd')
    return zstd.open(path, 'rb')

LIMIT = 8 * 1024 * 1024  # maximum decompressed line; never unbounded decompress
_READERS = OrderedDict()
_HEADERS = {}


def root(): return Path(os.path.expanduser('~/.dsh/sessions'))
def is_harness(path): return Path(path).name == 'session.v4.jsonl.zstd'
def session_id(path):
    name=Path(path).parent.name
    m=re.fullmatch(r'(?:session-)?([0-9a-f-]{36})',name)
    return m.group(1) if m else ''


def header(path):
    stat=os.stat(path); sig=(stat.st_dev,stat.st_ino,stat.st_mtime_ns,stat.st_size)
    if path in _HEADERS and _HEADERS[path][0]==sig: return _HEADERS[path][1]
    with _open(path) as f: value=json.loads(f.readline(65536))
    if not isinstance(value,dict) or value.get('type')!='session' or value.get('version')!=4:
        raise ValueError('unsupported Harness session schema')
    if len(_HEADERS) >= 1024: _HEADERS.pop(next(iter(_HEADERS)))
    _HEADERS[path]=(sig,value)
    return value


def files(base=None):
    found=[]
    for p in (Path(base) if base else root()).glob('*/*/session.v4.jsonl.zstd'):
        try:
            h=header(str(p))
            # Exclude delegated/seeded context; otherwise inherited history floods the group.
            if session_id(p) and not h.get('parentSession') and not h.get('isSeeded'):
                found.append(str(p))
        except (OSError, ValueError, EOFError, RuntimeError, DecodeError): continue
    return found


def metadata(path):
    h=header(path)
    title=''
    # UI cache is optional; source header remains authoritative for cwd/schema.
    cache=Path(os.path.expanduser('~/.dsh/storages/session_projcache/sessions'))/(Path(path).parent.name+'.json')
    try:
        if cache.stat().st_size < 8*1024*1024:
            rows=json.loads(cache.read_text(encoding='utf-8')).get('record',{}).get('rows',{})
            title=rows.get('title',{}).get('val','')
    except (OSError,ValueError): pass
    return Path(h.get('cwd') or '').name or '?', str(title or 'DeepSeek Harness')[:80]


# Per-tick decoding budgets apply to both live input and cursor restoration.
BUDGET = 2 * 1024 * 1024
CHUNK = 65536
_METRICS = {'compressed_read':0, 'decoded_bytes':0, 'reader_reopens':0, 'restore_bytes':0}


def metrics(): return dict(_METRICS)


class _Reader:
    """Keep the decompressor alive across temporary EOF, including partial frames.

    Checkpoints are *known frame boundaries*, never searched magic-byte guesses.
    A restart/eviction resumes at the last boundary not beyond the committed line.
    """
    def __init__(self, path, checkpoint):
        if zstd is None: raise RuntimeError('missing zstd decoder')
        self.raw = open(path, 'rb')
        self.raw.seek(checkpoint[0])
        self.decoder = zstd.ZstdDecompressor()
        self.pending = b''
        self.buffer = bytearray()
        self.produced = checkpoint[1]
        self.committed = checkpoint[1]
        self.boundaries = [tuple(checkpoint)]
        self.closed = False
        _METRICS['reader_reopens'] += 1
    def close(self):
        if not self.closed:
            self.raw.close(); self.closed = True
    def piece(self, limit):
        # One decoded piece; frame transitions with no plaintext are allowed.
        if self.decoder.eof:
            self.pending = self.decoder.unused_data
            self.boundaries.append((self.raw.tell()-len(self.pending), self.produced))
            self.decoder = zstd.ZstdDecompressor()
        data = b''
        if self.decoder.needs_input:
            if self.pending:
                data,self.pending = self.pending,b''
            else:
                data = self.raw.read(CHUNK)
                _METRICS['compressed_read'] += len(data)
            if not data: return None  # temporary source EOF, not decoder shutdown
        out = self.decoder.decompress(data, max_length=min(CHUNK,limit))
        self.produced += len(out)
        _METRICS['decoded_bytes'] += len(out)
        return out
    def checkpoint(self, offset):
        valid = [point for point in self.boundaries if point[1] <= offset]
        best = valid[-1] if valid else self.boundaries[0]
        self.boundaries = [best] + [p for p in self.boundaries if p[1] > offset]
        return list(best)


def read_delta(path, state, baseline=False):
    stat = os.stat(path)
    identity = [stat.st_dev,stat.st_ino]
    replaced = (state.get('dsh_identity') not in (None,identity) or
                stat.st_size < state.get('dsh_seen_size',state.get('off',0)))
    if replaced:
        for key in list(state):
            if key.startswith('dsh_'):state.pop(key)
        state['dsh_seed'] = True
    state['dsh_identity'] = identity
    state['dsh_seen_size'] = stat.st_size
    target = state.setdefault('dsh_offset',0)
    if baseline:state['dsh_seed'] = True
    entry = _READERS.pop(path,None)
    if entry and (replaced or entry[1]!=identity or entry[2]!=target):
        entry[0].close();entry=None
    if entry is None:
        checkpoint = state.get('dsh_checkpoint',[0,0])
        if (not isinstance(checkpoint,list) or len(checkpoint)!=2 or
            not all(isinstance(x,int) and x>=0 for x in checkpoint) or
            checkpoint[0]>stat.st_size or checkpoint[1]>target):checkpoint=[0,0]
        entry = [_Reader(path,checkpoint),identity,target]
    _READERS[path] = entry
    while len(_READERS)>8:
        _,old=_READERS.popitem(last=False);old[0].close()
    reader = entry[0]
    # At an unchanged, drained source we do zero IO and retain partial-frame state.
    if state.get('dsh_wait_size')==stat.st_size and not reader.buffer:return b''
    work=0;records=0;out=[];iterations=0
    try:
        while work<BUDGET and records<256 and iterations<512:
            iterations+=1
            # Restore uncompressed position only within the checkpointed frame.
            if reader.committed<target and reader.buffer:
                skip=min(target-reader.committed,len(reader.buffer))
                del reader.buffer[:skip];reader.committed+=skip
                _METRICS['restore_bytes']+=skip
            if reader.committed>=target:
                end=reader.buffer.find(b'\n')
                if end>=0:
                    if end+1>LIMIT:raise ValueError('Harness record exceeds 8 MiB')
                    line=bytes(reader.buffer[:end+1]);del reader.buffer[:end+1]
                    reader.committed+=len(line);records+=1
                    state['dsh_offset']=reader.committed;entry[2]=reader.committed
                    state['dsh_checkpoint']=reader.checkpoint(reader.committed)
                    if not state.get('dsh_seed'):out.append(line)
                    continue
                if len(reader.buffer)>LIMIT:raise ValueError('Harness record exceeds 8 MiB')
            piece=reader.piece(BUDGET-work)
            if piece is None:
                if reader.committed < target:
                    # Invalid persisted cursor: rebaseline, never replay history.
                    state.update(dsh_offset=0, dsh_checkpoint=[0,0], dsh_seed=True)
                    state.pop('dsh_wait_size',None)
                    reader.close();_READERS.pop(path,None)
                    return b''
                state['off']=stat.st_size
                state['dsh_wait_size']=stat.st_size
                state['dsh_checkpoint']=reader.checkpoint(reader.committed)
                # A partial historical line stays in baseline until it completes.
                if not reader.buffer and reader.committed>=target:state['dsh_seed']=False
                break
            work+=len(piece)
            reader.buffer.extend(piece)
        return b''.join(out)
    except Exception:
        reader.close();_READERS.pop(path,None)
        raise


def events(value):
    if not isinstance(value,dict): return
    kind=value.get('type');data=value.get('data') or {}
    if not isinstance(data,dict): return
    if kind=='turn/start': yield 'turn',''
    elif kind=='assistant/message':
        message=data.get('message') or {}
        if not isinstance(message,dict) or not isinstance(message.get('content',[]),list): return
        text='\n'.join(x.get('text','') for x in message.get('content',[])
                       if isinstance(x,dict) and x.get('type')=='text' and isinstance(x.get('text'),str))
        if text.strip():yield 'msg',text.strip()
    elif kind=='tool/call':
        question = question_text(data.get('name'), data.get('arguments') or data.get('input'))
        if question:
            yield 'question', question
            return
        # Tool names only; arguments and tool output can contain credentials.
        yield 'tool',str(data.get('name') or 'tool')+'|'
