"""
* Scheduled catalog-refresh tests (offline; nothing is actually downloaded).
* The point of these is the cooperation rules: a scheduled refresh must never
* duplicate queued work, and must never override what the user asked for.
"""
# Standard Library Imports
from pathlib import Path

# Third Party Imports
import pytest

# Local Imports
from web.server import auto_cache, cache_runner
from web.shared import download_queue as dq
from web.shared import game_cache
from web.shared.game_cache import (
    CacheProgress, checkpoint_path, request_stop, save_checkpoint)

GAME = 'union-arena'


class _DB:
    def count_by_game(self, game):
        return 0


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Default env for every test: enabled, the two catalog games, 24h."""
    cache_runner._threads.clear()
    monkeypatch.delenv('PROXYSHOP_AUTO_CACHE', raising=False)
    monkeypatch.delenv('PROXYSHOP_AUTO_CACHE_GAMES', raising=False)
    monkeypatch.delenv('PROXYSHOP_AUTO_CACHE_HOURS', raising=False)
    yield


def _recorder():
    """A stand-in for cache_runner.enqueue that records calls."""
    calls = []

    def fake(game, **kw):
        calls.append({'game': game, **kw})
        return {}

    return calls, fake


def _tick(tmp_path, fake, **kw):
    return auto_cache.tick(
        db=_DB(), images_dir=tmp_path / 'img', runs_dir=tmp_path,
        enqueue_fn=fake, **kw)


def _checkpoint(tmp_path, game, status):
    save_checkpoint(
        checkpoint_path(tmp_path, game),
        CacheProgress(
            game=game, status=status,
            filters=game_cache.normalize_filters(game, {}), stored=5))


class TestScope:

    def test_defaults_to_the_complete_catalog_games(self):
        assert auto_cache.scheduled_games() == ('union-arena', 'weiss-schwarz')

    def test_mtg_and_pokemon_are_not_scheduled(self):
        # They are filter-driven; there is no "whole catalog" to refresh.
        assert 'mtg' not in auto_cache.scheduled_games()
        assert 'pokemon' not in auto_cache.scheduled_games()

    def test_games_are_configurable_and_validated(self, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_AUTO_CACHE_GAMES', 'riftbound,not-a-game')
        assert auto_cache.scheduled_games() == ('riftbound',)

    def test_can_be_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv('PROXYSHOP_AUTO_CACHE', '0')
        calls, fake = _recorder()
        assert _tick(tmp_path, fake) == []
        assert calls == []

    def test_offline_never_downloads(self, tmp_path):
        calls, fake = _recorder()
        assert _tick(tmp_path, fake, offline=True) == []
        assert calls == []


class TestScheduling:

    def test_first_run_is_due(self, tmp_path):
        calls, fake = _recorder()
        actions = _tick(tmp_path, fake)
        queued = [a for a in actions if a['action'] == 'queued']
        assert {a['game'] for a in queued} == {'union-arena', 'weiss-schwarz'}
        assert {c['game'] for c in calls} == {'union-arena', 'weiss-schwarz'}

    def test_refresh_asks_for_the_full_catalog_without_wiping_state(self, tmp_path):
        calls, fake = _recorder()
        _tick(tmp_path, fake)
        call = next(c for c in calls if c['game'] == GAME)
        assert call['filters'] == {}
        assert call['images_only'] is False
        # fresh=True would discard the user's queue and checkpoint.
        assert call['fresh'] is False

    def test_not_due_again_until_the_interval_elapses(self, tmp_path):
        calls, fake = _recorder()
        _tick(tmp_path, fake, now=1_000_000.0)
        assert len(calls) == 2
        actions = _tick(tmp_path, fake, now=1_000_000.0 + 3600)
        assert all(a['action'] == 'skipped' for a in actions)
        assert len(calls) == 2  # unchanged

    def test_due_again_after_the_interval(self, tmp_path):
        calls, fake = _recorder()
        _tick(tmp_path, fake, now=1_000_000.0)
        _tick(tmp_path, fake, now=1_000_000.0 + 24 * 3600 + 1)
        assert len(calls) == 4

    def test_interval_is_configurable(self, tmp_path, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_AUTO_CACHE_HOURS', '6')
        calls, fake = _recorder()
        _tick(tmp_path, fake, now=1_000_000.0)
        _tick(tmp_path, fake, now=1_000_000.0 + 6 * 3600 + 1)
        assert len(calls) == 4

    def test_stamp_survives_a_restart(self, tmp_path):
        calls, fake = _recorder()
        _tick(tmp_path, fake, now=1_000_000.0)
        # Simulated restart: fresh module state, same runs_dir on disk.
        assert auto_cache.due(tmp_path, GAME, 1_000_000.0 + 60, 24 * 3600) is False


class TestCollisionGuards:
    """A scheduled refresh may append to the queue; it may never take it over."""

    def test_idle_queue_is_safe(self, tmp_path):
        assert auto_cache.should_skip(tmp_path, GAME) is None

    def test_defers_when_paused_by_the_user(self, tmp_path):
        # Pressing Stop leaves the item queued with a 'stopped' checkpoint.
        # Enqueuing would restart the worker and resume it behind the user's
        # back, so this must defer instead.
        dq.enqueue(tmp_path, GAME, {}, kind='png')
        _checkpoint(tmp_path, GAME, 'stopped')
        assert auto_cache.should_skip(tmp_path, GAME) == 'paused by the user'

        calls, fake = _recorder()
        actions = _tick(tmp_path, fake)
        act = next(a for a in actions if a['game'] == GAME)
        assert act['action'] == 'deferred'
        assert not [c for c in calls if c['game'] == GAME]

    def test_deferred_game_is_not_stamped_so_it_retries(self, tmp_path):
        dq.enqueue(tmp_path, GAME, {}, kind='png')
        _checkpoint(tmp_path, GAME, 'stopped')
        calls, fake = _recorder()
        _tick(tmp_path, fake, now=1_000_000.0)
        # User resumes and the queue drains; next tick should act immediately
        # rather than waiting out a full interval.
        dq.save_queue(tmp_path, GAME, [])
        checkpoint_path(tmp_path, GAME).unlink()
        actions = _tick(tmp_path, fake, now=1_000_000.0 + 60)
        act = next(a for a in actions if a['game'] == GAME)
        assert act['action'] == 'queued'

    def test_defers_while_a_stop_is_in_flight(self, tmp_path):
        request_stop(tmp_path, GAME)
        assert auto_cache.should_skip(tmp_path, GAME) == 'a stop is in flight'

    def test_defers_when_the_queue_is_stalled_on_an_error(self, tmp_path):
        # Head left in place after a crash, worker not running.
        dq.enqueue(tmp_path, GAME, {}, kind='png')
        _checkpoint(tmp_path, GAME, 'running')
        assert auto_cache.should_skip(tmp_path, GAME) == (
            'queue is stalled on an unfinished item')

    def test_running_game_is_not_skipped(self, tmp_path, monkeypatch):
        # A live download just means "append"; the queue de-dupes and the
        # worker picks it up when the current item finishes.
        dq.enqueue(tmp_path, GAME, {'set': 'x'}, kind='png')
        monkeypatch.setattr(cache_runner, 'is_running', lambda g: g == GAME)
        assert auto_cache.should_skip(tmp_path, GAME) is None

    def test_repeat_enqueue_of_the_same_spec_does_not_duplicate(self, tmp_path):
        # The real de-dupe lives in download_queue; assert the refresh spec is
        # stable so a second scheduled run can never stack a duplicate.
        first = dq.enqueue(tmp_path, GAME, {}, kind='png', images_only=False)
        again = dq.enqueue(tmp_path, GAME, {}, kind='png', images_only=False)
        assert first['id'] == again['id']
        assert len(dq.load_queue(tmp_path, GAME)) == 1

    def test_user_queue_is_preserved_alongside_a_refresh(self, tmp_path):
        # A user's own filtered download stays queued and keeps its position.
        dq.enqueue(tmp_path, GAME, {'set': 'mine'}, kind='png')
        calls, fake = _recorder()
        _tick(tmp_path, fake)
        items = dq.load_queue(tmp_path, GAME)
        assert items[0]['filters'] == {'set': 'mine'}

    def test_one_bad_game_does_not_stop_the_others(self, tmp_path):
        def fake(game, **kw):
            if game == 'union-arena':
                raise RuntimeError('provider exploded')
            return {}

        actions = _tick(tmp_path, fake)
        by_game = {a['game']: a for a in actions}
        assert by_game['union-arena']['action'] == 'error'
        assert by_game['weiss-schwarz']['action'] == 'queued'


class TestStatus:

    def test_reports_schedule_and_blockers(self, tmp_path):
        st = auto_cache.status(tmp_path)
        assert st['enabled'] is True
        assert st['interval_hours'] == 24.0
        assert set(st['games']) == {'union-arena', 'weiss-schwarz'}
        assert st['games'][GAME]['blocked_by'] is None

    def test_surfaces_a_pause_as_a_blocker(self, tmp_path):
        dq.enqueue(tmp_path, GAME, {}, kind='png')
        _checkpoint(tmp_path, GAME, 'stopped')
        st = auto_cache.status(tmp_path)
        assert st['games'][GAME]['blocked_by'] == 'paused by the user'


class TestThread:

    def test_start_is_a_noop_when_offline(self, tmp_path):
        assert auto_cache.start(
            db=_DB(), images_dir=tmp_path, runs_dir=tmp_path, offline=True) is False

    def test_start_is_a_noop_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_AUTO_CACHE', '0')
        assert auto_cache.start(
            db=_DB(), images_dir=tmp_path, runs_dir=tmp_path) is False

    def test_start_is_a_noop_with_no_scheduled_games(self, tmp_path, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_AUTO_CACHE_GAMES', '')
        assert auto_cache.start(
            db=_DB(), images_dir=tmp_path, runs_dir=tmp_path) is False
