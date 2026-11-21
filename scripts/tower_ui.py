"""Pure card title policy shared by control and streaming (no network/state writes)."""
import os
import re
import json
from functools import lru_cache
from string import Formatter

TITLE_DEFAULTS = {
    "title_max_len": 24,
    "topic_title_template": "{icon} {jid} · {title}",
    "stream_title_template": "{icon} {title} · {jid}",
}
_FIELDS = {"icon", "jid", "title", "project", "agent"}
_IMAGE_REF = re.compile(r'!\[([^\]\n]*)\]\(([^)\n]+)\)')


def sanitize_message_body(body):
    """Show agent image references as text; Feishu Markdown requires uploaded image keys."""
    if not isinstance(body, dict) or not isinstance(body.get('content'), str):
        return body
    try:
        card = json.loads(body['content'])
    except (TypeError, ValueError):
        return body

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(item) for key, item in value.items()}
        if result.get('tag') == 'markdown' and isinstance(result.get('content'), str):
            result['content'] = _IMAGE_REF.sub(
                lambda match: f'{match[1] or "图片"}（图片地址：{match[2]}）',
                result['content'])
        return result

    cleaned = visit(card)
    if cleaned == card:
        return body
    return {**body, 'content': json.dumps(cleaned, ensure_ascii=False)}


def normalize_title(value, config=None):
    try:
        limit = max(8, min(80, int((config or {}).get("title_max_len", 24))))
    except (ValueError, TypeError):
        limit = 24
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def format_title(kind, config, **values):
    key = f"{kind}_title_template"
    template = str(config.get(key) or TITLE_DEFAULTS[key])
    data = {k: str(values.get(k) or "") for k in _FIELDS}
    data["title"] = normalize_title(data["title"], config) or "未命名任务"
    try:
        # Disallow attribute/index lookup and format widths (untrusted local config).
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None and (field not in _FIELDS or spec or conversion):
                raise ValueError("unsupported title placeholder")
        text = template.format(**data)
        if not text.strip():
            raise ValueError("empty title")
    except (ValueError, KeyError, IndexError):
        text = TITLE_DEFAULTS[key].format(**data)
    return re.sub(r"\s+", " ", text).strip()[:160]


@lru_cache(maxsize=4)
def _read_config(path, stamp):
    config = dict(TITLE_DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition("=")
                if key.strip() in config and value.strip():
                    config[key.strip()] = value.strip()
    except OSError:
        pass
    return config


def title_config(path):
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    return dict(_read_config(path, stamp))
