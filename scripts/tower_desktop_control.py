"""Resolve explicit local Codex desktop threads for Feishu control."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3


CATALOG = Path.home() / '.codex/sqlite/codex-dev.db'
SESSION_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


def attach_title(text):
    match = re.fullmatch(
        r'\s*(?:我要|我想|请帮我)?\s*(?:控制|接管|进入)\s*Codex\s*'
        r'(?:里|中)?\s*(.+?)\s*(?:这个|的)?任务\s*', text, re.I)
    if not match:
        return ''
    return clean_title(match.group(1))


def clean_title(value):
    return value.strip().strip('“”「」\'" ').removesuffix('的').strip()


def catalog_matches(value, catalog=CATALOG):
    value = clean_title(value)
    if not value:
        return []
    query = ('SELECT thread_id, display_title, cwd FROM local_thread_catalog '
             'WHERE host_id = ? AND source_kind = ? AND thread_source = ? AND ')
    if SESSION_ID.fullmatch(value):
        query += 'thread_id = ?'
    else:
        query += 'display_title = ? COLLATE NOCASE'
    try:
        with closing(sqlite3.connect(f'file:{Path(catalog).as_posix()}?mode=ro',
                                     uri=True, timeout=1)) as db:
            rows = db.execute(query, ('local', 'vscode', 'user', value)).fetchall()
    except (OSError, sqlite3.Error):
        return []
    return [{'id': sid, 'title': title, 'cwd': cwd} for sid, title, cwd in rows]
