"""
* TCG catalog cache / stop-resume tests (offline).
"""
# Standard Library Imports
import json
from pathlib import Path

# Third Party Imports
import pytest

# Local Imports
from web.shared import game_cache, games
from web.tests.conftest import make_card


@pytest.fixture(autouse=True)
def _fast_cache_pacing(monkeypatch):
    monkeypatch.setattr(game_cache, 'CACHE_PAGE_INTERVAL', 0.0)
    monkeypatch.setattr(game_cache, 'CACHE_CARD_INTERVAL', 0.0)
    monkeypatch.setattr(game_cache, 'CACHE_IMAGE_INTERVAL', 0.0)
    monkeypatch.setattr(game_cache, 'CACHE_PROVIDER_INTERVAL', 0.0)
    prev = games._provider_limiter.min_interval
    games._provider_limiter.set_interval(0)
    yield
    games._provider_limiter.set_interval(prev)


def _rb_row(i: int) -> dict:
    return {
        'id': f'ogs-{i:03d}-001',
        'name': f'Card {i}',
        'set_id': 'OGS',
        'collector_number': i,
        'image': f'https://cdn.example/card-{i}.png',
        'image_thumb': {'small': f'https://cdn.example/card-{i}-sm.webp'},
        'faction': 'fury',
        'type': 'Unit',
        'stats': {'energy': 1, 'might': 1, 'power': 0},
        'description': f'Rules {i}',
    }


class TestListRiftboundPage:

    def test_paginates_and_reads_total(self, monkeypatch):
        calls = []

        class FakeRes:
            def __init__(self, offset, limit):
                self.status_code = 200
                self.headers = {'X-Total-Count': '3'}
                self._rows = [_rb_row(i) for i in range(offset, min(offset + limit, 3))]

            def json(self):
                return self._rows

        def fake_get(url, params=None, headers=None, timeout=30, allow_redirects=True):
            calls.append(params)
            return FakeRes(params['offset'], params['limit'])

        monkeypatch.setattr(games.requests, 'get', fake_get)
        page1, total = games.list_riftbound_page(offset=0, limit=2, hydrate=False)
        assert total == 3
        assert [c['name'] for c in page1] == ['Card 0', 'Card 1']
        page2, _ = games.list_riftbound_page(offset=2, limit=2, hydrate=False)
        assert [c['name'] for c in page2] == ['Card 2']
        assert calls[0]['offset'] == 0
        assert calls[1]['offset'] == 2


class TestCacheGameResume:

    def test_stop_and_resume(self, carddb, tmp_path, monkeypatch):
        pages = {
            0: [_rb_row(0), _rb_row(1)],
            2: [_rb_row(2)],
        }

        def fake_list(offset=0, limit=50, hydrate=False):
            rows = pages.get(offset, [])
            cards = []
            for c in rows:
                n = games._normalize_riftbound_card(c)
                if n:
                    cards.append(n)
            return cards, 3

        monkeypatch.setattr(games, 'list_riftbound_page', fake_list)
        monkeypatch.setattr(
            game_cache.images, 'ensure_image', lambda *a, **k: None)

        real_store = game_cache._store_and_image
        stored = {'n': 0}

        def store_then_maybe_stop(db, images_dir, card, progress, watch=None):
            real_store(db, images_dir, card, progress, watch)
            stored['n'] += 1
            if stored['n'] >= 1:
                raise game_cache.StopRequested('test stop')

        monkeypatch.setattr(game_cache, '_store_and_image', store_then_maybe_stop)

        class Watch:
            def __init__(self, runs_dir, game, *, use_signals=True):
                self.runs_dir = runs_dir
                self.game = game

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def check(self):
                return None

        monkeypatch.setattr(game_cache, '_StopWatch', Watch)

        runs = tmp_path / 'runs'
        images_dir = tmp_path / 'images'
        progress = game_cache.run_cache_game(
            db=carddb,
            game='riftbound',
            images_dir=images_dir,
            runs_dir=runs,
            download_images=True,
            hydrate=False,
            page_size=2,
            print_fn=lambda *a, **k: None,
        )
        assert progress.status == 'stopped'
        assert progress.stored == 1
        assert carddb.count_by_game('riftbound') == 1

        monkeypatch.setattr(game_cache, '_store_and_image', real_store)
        done = game_cache.run_cache_game(
            db=carddb,
            game='riftbound',
            images_dir=images_dir,
            runs_dir=runs,
            download_images=True,
            hydrate=False,
            page_size=2,
            print_fn=lambda *a, **k: None,
        )
        assert done.status == 'done'
        assert carddb.count_by_game('riftbound') == 3

    def test_status_reset_stop_helpers(self, tmp_path):
        runs = tmp_path / 'runs'
        path = game_cache.request_stop(runs, 'riftbound')
        assert path.is_file()
        game_cache.save_checkpoint(
            game_cache.checkpoint_path(runs, 'riftbound'),
            game_cache.CacheProgress(game='riftbound', status='stopped', stored=4))
        loaded = game_cache.load_checkpoint(
            game_cache.checkpoint_path(runs, 'riftbound'))
        assert loaded and loaded.stored == 4
        game_cache.reset_checkpoint(runs, 'riftbound')
        assert not game_cache.checkpoint_path(runs, 'riftbound').is_file()
        assert not game_cache.stop_path(runs, 'riftbound').is_file()

    def test_images_only_skips_existing(self, carddb, tmp_path, monkeypatch):
        card = make_card('rb-1', 'Annie')
        card['game'] = 'riftbound'
        card['images'] = {'large': 'https://cdn.example/a.png'}
        carddb.store_card(card, game='riftbound')
        images_dir = tmp_path / 'images'
        images_dir.mkdir()
        (images_dir / 'rb-1-png.png').write_bytes(b'x')

        calls = {'n': 0}

        def fake_ensure(*a, **k):
            calls['n'] += 1
            return None

        monkeypatch.setattr(game_cache.images, 'ensure_image', fake_ensure)
        progress = game_cache.run_cache_game(
            db=carddb,
            game='riftbound',
            images_dir=images_dir,
            runs_dir=tmp_path / 'runs',
            images_only=True,
            fresh=True,
            print_fn=lambda *a, **k: None,
        )
        assert progress.status == 'done'
        assert progress.images_skip == 1
        assert calls['n'] == 0

    def test_mtg_rejects_empty_filters(self, carddb, tmp_path):
        with pytest.raises(ValueError, match='filter'):
            game_cache.run_cache_game(
                db=carddb,
                game='mtg',
                images_dir=tmp_path / 'images',
                runs_dir=tmp_path / 'runs',
                filters={},
                print_fn=lambda *a, **k: None,
            )

    def test_mtg_paginated_cache(self, carddb, tmp_path, monkeypatch):
        pages = {
            1: ([{
                'object': 'card', 'id': 'scry-1', 'name': 'Bolt',
                'set': 'lea', 'collector_number': '161', 'lang': 'en',
                'image_uris': {'png': 'https://cdn.example/1.png'},
            }], 2, True),
            2: ([{
                'object': 'card', 'id': 'scry-2', 'name': 'Bolt2',
                'set': 'lea', 'collector_number': '162', 'lang': 'en',
                'image_uris': {'png': 'https://cdn.example/2.png'},
            }], 2, False),
        }

        def fake_page(query, page=1, store=False):
            return pages[page]

        monkeypatch.setattr(carddb, 'list_scryfall_page', fake_page)
        monkeypatch.setattr(game_cache.images, 'ensure_image', lambda *a, **k: None)

        class Watch:
            def __init__(self, runs_dir, game, *, use_signals=True):
                self.runs_dir = runs_dir
                self.game = game

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def check(self):
                return None

        monkeypatch.setattr(game_cache, '_StopWatch', Watch)
        progress = game_cache.run_cache_game(
            db=carddb,
            game='mtg',
            images_dir=tmp_path / 'images',
            runs_dir=tmp_path / 'runs',
            filters={'set': 'lea'},
            fresh=True,
            print_fn=lambda *a, **k: None,
        )
        assert progress.status == 'done'
        assert progress.stored == 2
        assert 'set:lea' in progress.query
        assert carddb.count_by_game('mtg') == 2
