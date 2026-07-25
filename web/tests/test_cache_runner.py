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
from web.shared import game_cache, games
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


def test_provider_error_retries_then_succeeds(tmp_path, monkeypatch):
    """A transient 5xx retries the same item instead of stalling the queue."""
    monkeypatch.setattr(cache_runner, '_ITEM_RETRY_BACKOFF', 0.0)
    monkeypatch.setattr(cache_runner, '_ITEM_MAX_ATTEMPTS', 3)
    calls = {'n': 0}

    def flaky_run(*, db, game, filters, runs_dir, **kw):
        calls['n'] += 1
        if calls['n'] == 1:
            raise games.ProviderError('Provider HTTP 500: upstream boom')
        _mark_done(game, runs_dir, filters)

    monkeypatch.setattr(cache_runner, 'run_cache_game', flaky_run)
    runs = tmp_path / 'runs'
    cache_runner.enqueue(
        'pokemon', filters={'set': 'base1'},
        db=_DB(), images_dir=tmp_path / 'img', runs_dir=runs)
    assert _wait_idle('pokemon')
    assert calls['n'] == 2
    assert dq.load_queue(runs, 'pokemon') == []  # completed, not stranded


def test_provider_error_parks_item_and_moves_on(tmp_path, monkeypatch):
    """One dead item must not block the rest of the game's queue."""
    monkeypatch.setattr(cache_runner, '_ITEM_RETRY_BACKOFF', 0.0)
    monkeypatch.setattr(cache_runner, '_ITEM_MAX_ATTEMPTS', 2)
    ran = []

    def run(*, db, game, filters, runs_dir, **kw):
        ran.append(dict(filters))
        if filters.get('set') == 'base1':
            raise games.ProviderError('Provider HTTP 503')
        _mark_done(game, runs_dir, filters)

    monkeypatch.setattr(cache_runner, 'run_cache_game', run)
    runs = tmp_path / 'runs'
    kw = dict(db=_DB(), images_dir=tmp_path / 'img', runs_dir=runs)
    cache_runner.enqueue('pokemon', filters={'set': 'base1'}, **kw)
    cache_runner.enqueue('pokemon', filters={'set': 'swsh1'}, **kw)
    assert _wait_idle('pokemon', timeout=10)
    # base1 burned its attempts, then swsh1 got its turn and finished.
    assert [f.get('set') for f in ran].count('swsh1') == 1
    remaining = [i['filters'].get('set') for i in dq.load_queue(runs, 'pokemon')]
    assert remaining == ['base1']  # parked, still retryable by the user


def test_provider_error_pauses_when_alone(tmp_path, monkeypatch):
    """Sole failing item: retry, then pause — never spin."""
    monkeypatch.setattr(cache_runner, '_ITEM_RETRY_BACKOFF', 0.0)
    monkeypatch.setattr(cache_runner, '_ITEM_MAX_ATTEMPTS', 3)
    calls = {'n': 0}

    def always_fail(*, db, game, filters, runs_dir, **kw):
        calls['n'] += 1
        raise games.ProviderError('Provider HTTP 500')

    monkeypatch.setattr(cache_runner, 'run_cache_game', always_fail)
    runs = tmp_path / 'runs'
    cache_runner.enqueue(
        'pokemon', filters={'set': 'base1'},
        db=_DB(), images_dir=tmp_path / 'img', runs_dir=runs)
    assert _wait_idle('pokemon', timeout=10)
    assert calls['n'] == 3  # bounded by _ITEM_MAX_ATTEMPTS
    assert len(dq.load_queue(runs, 'pokemon')) == 1
    st = cache_runner.status('pokemon', db=_DB(), runs_dir=runs)
    assert 'Provider HTTP 500' in st.get('error', '')


def test_non_provider_error_does_not_retry(tmp_path, monkeypatch):
    """Bad filters won't fix themselves — fail once and surface it."""
    monkeypatch.setattr(cache_runner, '_ITEM_RETRY_BACKOFF', 0.0)
    calls = {'n': 0}

    def bad_filters(*, db, game, filters, runs_dir, **kw):
        calls['n'] += 1
        raise ValueError('pokemon cache needs filters')

    monkeypatch.setattr(cache_runner, 'run_cache_game', bad_filters)
    runs = tmp_path / 'runs'
    cache_runner.enqueue(
        'pokemon', filters={'set': 'base1'},
        db=_DB(), images_dir=tmp_path / 'img', runs_dir=runs)
    assert _wait_idle('pokemon')
    assert calls['n'] == 1
    st = cache_runner.status('pokemon', db=_DB(), runs_dir=runs)
    assert 'needs filters' in st.get('error', '')


def test_stop_during_retry_backoff(tmp_path, monkeypatch):
    """Stop stays responsive while waiting between attempts."""
    monkeypatch.setattr(cache_runner, '_ITEM_RETRY_BACKOFF', 30.0)
    runs = tmp_path / 'runs'
    calls = {'n': 0}

    def fail_then_stop(*, db, game, filters, runs_dir, **kw):
        calls['n'] += 1
        game_cache.request_stop(runs_dir, game)  # user hits Stop mid-run
        raise games.ProviderError('Provider HTTP 500')

    monkeypatch.setattr(cache_runner, 'run_cache_game', fail_then_stop)
    cache_runner.enqueue(
        'pokemon', filters={'set': 'base1'},
        db=_DB(), images_dir=tmp_path / 'img', runs_dir=runs)
    # Must return promptly rather than sleeping out the full backoff.
    assert _wait_idle('pokemon', timeout=5)
    assert calls['n'] == 1
    assert len(dq.load_queue(runs, 'pokemon')) == 1


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
