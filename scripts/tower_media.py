"""Select local images explicitly referenced by an agent's Markdown reply."""
import hashlib
import json
import re
from pathlib import Path


IMAGE_REF = re.compile(r'!\[([^\]\n]*)\]\(([^)\n]+)\)')
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif'}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4


def checked_image(reference, root_dir, expected_hash=None):
    """Return a fingerprint only for a supported image inside its session directory."""
    root = Path(root_dir).resolve(strict=True)
    raw = str(reference).strip().strip('<>')
    if not raw or '://' in raw:
        raise ValueError('unsupported image reference')
    candidate = Path(raw)
    path = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('image is outside the session directory')
    mime = IMAGE_TYPES.get(path.suffix.lower())
    if not mime or path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError('unsupported image type or size')
    content = path.read_bytes()
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError('invalid image size')
    digest = hashlib.sha256(content).hexdigest()
    if expected_hash is not None and digest != expected_hash:
        raise ValueError('image changed after it was queued')
    return {'path': str(path), 'root_dir': str(root), 'sha256': digest, 'mime': mime}


def image_references(text, root_dir):
    """Ignore links that are missing, remote, oversized, or outside the session."""
    found, seen = [], set()
    for match in IMAGE_REF.finditer(text):
        try:
            image = checked_image(match.group(2), root_dir)
        except (OSError, ValueError):
            continue
        if image['path'] in seen:
            continue
        seen.add(image['path'])
        found.append(image)
        if len(found) >= MAX_IMAGES_PER_MESSAGE:
            break
    return found


def codex_session_cwd(source, expected_id):
    try:
        with open(source, encoding='utf-8') as session:
            head = json.loads(session.readline())
        meta = head.get('payload') or {}
        if head.get('type') != 'session_meta' or meta.get('id') != expected_id:
            return ''
        return meta.get('cwd') or ''
    except (OSError, ValueError, TypeError):
        return ''
