"""
* Per-game download queue for the TCG cache runner.
* Each catalog game has an ordered queue of download specs. Index 0 is the
* active item (running or next to run); the runner drains the queue in order,
* one job at a time per game (games still run in parallel with each other).
* Persisted as JSON under runs_dir so pending work survives a restart.
* Must never import from `src/`.
"""
# Standard Library Imports
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Optional

# Local Imports
from web.shared.cache_filters import friendly_filters, normalize_filters

# One lock guards all queue-file reads/writes across threads (worker + API).
_lock = threading.RLock()


def queue_path(runs_dir: Path, game: str) -> Path:
    return Path(runs_dir) / f'{game}.queue.json'


def _item_id(game: str, filters: Optional[dict], kind: str, images_only: bool) -> str:
    """Stable id from the normalized spec, so identical specs de-dupe."""
    payload = json.dumps({
        'g': game,
        'f': normalize_filters(game, filters),
        'k': kind,
        'i': bool(images_only),
    }, sort_keys=True)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]


def load_queue(runs_dir: Path, game: str) -> list[dict]:
    p = queue_path(runs_dir, game)
    with _lock:
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return []
        return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else []


def save_queue(runs_dir: Path, game: str, items: list[dict]) -> None:
    p = queue_path(runs_dir, game)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.json.part')
        tmp.write_text(json.dumps(items, indent=2) + '\n', encoding='utf-8')
        tmp.replace(p)


def enqueue(
    runs_dir: Path,
    game: str,
    filters: Optional[dict],
    *,
    kind: str = 'png',
    images_only: bool = False,
    stamp: Optional[str] = None,
) -> dict:
    """Append a download spec (de-duped by normalized spec). Returns the item.

    An identical spec already in the queue is returned as-is rather than
    duplicated, so re-clicking Add is harmless.
    """
    with _lock:
        items = load_queue(runs_dir, game)
        iid = _item_id(game, filters, kind, images_only)
        for it in items:
            if it.get('id') == iid:
                return it
        norm = normalize_filters(game, filters)
        item = {
            'id': iid,
            'filters': filters or {},
            'kind': kind,
            'images_only': bool(images_only),
            'label': friendly_filters(game, norm),
            'added_at': stamp or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        items.append(item)
        save_queue(runs_dir, game, items)
        return item


def head(runs_dir: Path, game: str) -> Optional[dict]:
    """The active item (index 0), or None when the queue is empty."""
    items = load_queue(runs_dir, game)
    return items[0] if items else None


def pop_head(runs_dir: Path, game: str, item_id: Optional[str] = None) -> Optional[dict]:
    """Remove and return the head. If item_id is given it must match (guards
    against popping a job the caller didn't just finish)."""
    with _lock:
        items = load_queue(runs_dir, game)
        if not items:
            return None
        if item_id is not None and items[0].get('id') != item_id:
            return None
        removed = items.pop(0)
        save_queue(runs_dir, game, items)
        return removed


def remove(runs_dir: Path, game: str, item_id: str) -> Optional[dict]:
    """Remove any item by id (head or pending). Returns it, or None."""
    with _lock:
        items = load_queue(runs_dir, game)
        for i, it in enumerate(items):
            if it.get('id') == item_id:
                removed = items.pop(i)
                save_queue(runs_dir, game, items)
                return removed
        return None


def is_head(runs_dir: Path, game: str, item_id: str) -> bool:
    h = head(runs_dir, game)
    return bool(h and h.get('id') == item_id)


def clear_pending(runs_dir: Path, game: str) -> int:
    """Drop every item except the active head. Returns how many were removed."""
    with _lock:
        items = load_queue(runs_dir, game)
        if len(items) <= 1:
            return 0
        removed = len(items) - 1
        save_queue(runs_dir, game, items[:1])
        return removed
