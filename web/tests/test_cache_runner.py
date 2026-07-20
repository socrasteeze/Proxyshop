"""
* Queue-worker tests for the cache runner (offline; run_cache_game stubbed).
"""
# Standard Library Imports
import threading
import time
from pathlib import Path

# Third Party Imports
import pytest

# Local Imports
from web.server import cache_runner
from web.shared import download_queue as dq
from web.shared import game_cache
from web.shared.game_cache import CacheProgress, checkpoint_path, save_checkpoint


class _DB:
    def count_by_game(self, game):
        return 0


@pytest.fixture(autouse=True)
def _reset_runner():
    cache_runner._threads.clear()
    cache_runner._live.clear()
    cache_runner._errors.clear()
    yield


def _wait_idle(game, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not cache_runner.is_running(game):
            return True
        time.sleep(0.02)
    return False


def _mark_done(game, runs_dir, filters):
    save_checkpoint(
        checkpoint_path(runs_dir, game),
        CacheProgress(
            game=game, status='done',
            filters=game_cache.normalize_filters(game, filters), stored=1))


def test_worker_drains_in_order(tmp_path, monkeypatch):
    ran = []

    def fake_run(*, db, game, filters, runs_dir, **kw):
        ran.append(dict(filters))
        _mark_done(game, runs_dir, filters)
        return None

    monkeypatch.setattr(cache_runner, 'run_cache_game', fake_run)
    kw = dict(db=_DB(), images_dir=tmp_path / 'img', runs_dir=tmp_path / 'runs')
    cache_runner.enqueue('mtg', filters={'tags': 'art:dragon'}, **kw)
    cache_runner.enqueue('mtg', filters={'tags': 'art:angel'}, **kw)
    assert _wait_idle('mtg')
    assert ran == [{'tags': 'art:dragon'}, {'tags': 'art:angel'}]
    assert dq.load_queue(tmp_path / 'runs', 'mtg') == []


def test_queue_visible_while_running(tmp_path, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def fake_run(*, db, game, filters, runs_dir, **kw):
        started.set()
        release.wait(5)
        _mark_done(game, runs_dir, filters)
        return None

    monkeypatch.setattr(cache_runner, 'run_cache_game', fake_run)
    runs = tmp_path / 'runs'
    kw = dict(db=_DB(), images_dir=tmp_path / 'img', runs_dir=runs)
    cache_runner.enqueue('mtg', filters={'tags': 'art:dragon'}, **kw)
    cache_runner.enqueue('mtg', filters={'tags': 'art:angel'}, **kw)
    assert started.wait(5)
    st = cache_runner.status('mtg', db=_DB(), runs_dir=runs)
    assert st['queued_count'] == 2
    states = [q['state'] for q in st['queue']]
    assert states[0] == 'running' and states[1] == 'queued'
    release.set()
    assert _wait_idle('mtg')
    assert dq.load_queue(runs, 'mtg') == []


def test_remove_pending_while_running(tmp_path, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def fake_run(*, db, game, filters, runs_dir, **kw):
        started.set()
        release.wait(5)
        _mark_done(game, runs_dir, filters)
        return None

    monkeypatch.setattr(cache_runner, 'run_cache_game', fake_run)
    runs = tmp_path / 'runs'
    kw = dict(db=_DB(), images_dir=tmp_path / 'img', runs_dir=runs)
    cache_runner.enqueue('mtg', filters={'tags': 'art:dragon'}, **kw)
    cache_runner.enqueue('mtg', filters={'tags': 'art:angel'}, **kw)
    assert started.wait(5)
    items = dq.load_queue(runs, 'mtg')
    head, pending_id = items[0]['id'], items[1]['id']
    # Removing the running head is a no-op; removing the pending one works.
    cache_runner.remove_item('mtg', head, db=_DB(), runs_dir=runs)
    assert dq.head(runs, 'mtg')['id'] == head  # still there
    cache_runner.remove_item('mtg', pending_id, db=_DB(), runs_dir=runs)
    assert len(dq.load_queue(runs, 'mtg')) == 1
    release.set()
    assert _wait_idle('mtg')


def test_offline_no_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cache_runner, 'run_cache_game',
                        lambda **kw: calls.append(1))
    runs = tmp_path / 'runs'
    # enqueue an item, let it drain (fake no-op leaves checkpoint absent → not
    # done → worker stops after one pass, leaving the item queued).
    cache_runner.enqueue(
        'mtg', filters={'tags': 'art:x'},
        db=_DB(), images_dir=tmp_path / 'img', runs_dir=runs)
    assert _wait_idle('mtg')
    assert calls == [1]
    # Not marked done → stays queued for a later resume.
    assert len(dq.load_queue(runs, 'mtg')) == 1
