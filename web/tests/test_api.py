"""
* API Contract Tests — submission, worker lifecycle, auth, rate limits,
* idempotency, upload caps. All offline.
"""
# Standard Library Imports
import importlib
import io

# Third Party Imports
import pytest
from fastapi.testclient import TestClient

# Local Imports
from web.tests.conftest import make_card

WORKER_AUTH = {'Authorization': 'Bearer test-token'}
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'0' * 64


@pytest.fixture()
def appmod(tmp_path, monkeypatch):
    """Fresh app module bound to an isolated data dir, offline, known token."""
    monkeypatch.setenv('PROXYSHOP_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('PROXYSHOP_WORKER_TOKEN', 'test-token')
    monkeypatch.setenv('PROXYSHOP_OFFLINE', '1')
    monkeypatch.setenv('PROXYSHOP_MAX_UPLOAD_MB', '1')
    import web.server.app as appmod
    appmod = importlib.reload(appmod)
    yield appmod


@pytest.fixture()
def client(appmod):
    return TestClient(appmod.app)


def submit_job(client, name='Lightning Bolt', **extra):
    data = {'card_name': name, **extra}
    return client.post(
        '/api/jobs', data=data,
        files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})


class TestPages:

    def test_index(self, client):
        assert client.get('/').status_code == 200

    def test_decks_page(self, client):
        assert client.get('/decks').status_code == 200

    def test_search_page(self, client):
        assert client.get('/search?q=bolt').status_code == 200

    def test_gallery_page(self, appmod, client):
        appmod.carddb.store_card(make_card('gal-1', 'Sol Ring', 'c21', '125'))
        res = client.get('/gallery')
        assert res.status_code == 200
        assert 'Card library' in res.text
        assert 'Sol Ring' in res.text
        assert 'Apply filters' in res.text
        assert 'Show unique arts' in res.text
        assert 'Per page' in res.text
        # View modes: Images (grid), List, Full, Checklist
        assert 'Images' in res.text and 'List' in res.text
        assert 'Full' in res.text and 'Checklist' in res.text
        assert b'Card library' in client.get('/').content
        body = client.get('/api/cards/gallery').json()
        assert body['total'] == 1
        assert body['cards'][0]['name'] == 'Sol Ring'
        assert body['cards'][0]['thumb'].startswith('/api/cards/gal-1/image')
        assert body['cards'][0]['art_count'] == 1

    def test_gallery_toolbar_params(self, appmod, client):
        for i in range(3):
            appmod.carddb.store_card(make_card(
                f'id-{i}', 'Lightning Bolt', f's{i}', str(i),
                released=f'202{i}-01-01'))
        res = client.get('/gallery', params={
            'view': 'list', 'per_page': 24, 'arts': 'combine', 'q': 'Lightning'})
        assert res.status_code == 200
        assert 'card-list' in res.text
        assert 'combined arts' in res.text
        assert 'per_page=24' in res.text or 'value="24" selected' in res.text
        body = client.get('/api/cards/gallery', params={'arts': 'combine', 'q': 'Lightning'}).json()
        assert body['total'] == 1
        assert body['arts'] == 'combine'
        assert body['cards'][0]['art_count'] == 3

    def test_gallery_page_clamps_out_of_range(self, appmod, client):
        appmod.carddb.store_card(make_card('gal-1', 'Sol Ring', 'c21', '125'))
        res = client.get('/gallery?page=999')
        assert res.status_code == 200
        # Out-of-range page clamps to the last page and still shows cards
        assert 'Sol Ring' in res.text

    def test_card_detail_api(self, appmod, client):
        appmod.carddb.store_card(make_card('det-1', 'Sol Ring', 'c21', '125'))
        body = client.get('/api/cards/det-1/detail').json()
        assert body['id'] == 'det-1'
        assert body['name'] == 'Sol Ring'
        assert body['game'] == 'mtg'
        assert body['image_png'].endswith('/image?kind=png')
        assert body['can_edit'] is True
        assert body['editor_url'] == '/?card_id=det-1'
        assert any(d['label'] == 'Set' for d in body['details'])
        assert 'card' not in body
        assert client.get('/api/cards/missing/detail').status_code == 404

    def test_search_all_games_local_only(self, appmod, client):
        appmod.carddb.store_card(make_card('mtg-1', 'Charizard Dragon', 'xyz', '1'))
        pkm = make_card('pkm-1', 'Charizard', 'base1', '4')
        pkm['game'] = 'pokemon'
        appmod.carddb.store_card(pkm, game='pokemon')
        body = client.get('/api/cards/search',
                          params={'q': 'charizard', 'game': 'all'}).json()
        assert body['source'] == 'local'
        ids = {c['id'] for c in body['cards']}
        assert ids == {'mtg-1', 'pkm-1'}
        # Empty game param behaves the same
        body2 = client.get('/api/cards/search',
                           params={'q': 'charizard', 'game': ''}).json()
        assert {c['id'] for c in body2['cards']} == ids
        # HTML page renders too
        assert client.get('/search?q=charizard&game=all').status_code == 200

    def test_health(self, client):
        body = client.get('/api/health').json()
        assert body['ok'] is True
        assert body['offline'] is True


class TestJobSubmission:

    def test_submit_pokemon_compose_without_worker(self, client):
        res = client.post(
            '/api/jobs',
            data={'card_name': 'Pikachu', 'game': 'pokemon', 'render_mode': 'compose'},
            files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 200
        body = res.json()
        assert body['render_mode'] == 'compose'
        assert body['status'] == 'done'
        job = client.get(f"/api/jobs/{body['id']}").json()
        assert job['status'] == 'done'
        assert job['result_filename']
        dl = client.get(f"/api/jobs/{body['id']}/result")
        assert dl.status_code == 200
        assert dl.content[:8] == b'\x89PNG\r\n\x1a\n'

    def test_submit_riftbound_compose(self, client):
        res = client.post(
            '/api/jobs',
            data={'card_name': 'Annie', 'game': 'riftbound', 'render_mode': 'auto'},
            files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 200
        assert res.json()['render_mode'] == 'compose'
        assert res.json()['status'] == 'done'

    def test_submit_rejects_non_renderable_game(self, client):
        res = client.post(
            '/api/jobs',
            data={'card_name': 'Gon', 'game': 'union-arena'},
            files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 422
        assert 'not renderable' in res.json()['detail'].lower()

    def test_submit_mtg_compose(self, appmod, client):
        appmod.carddb.store_card(make_card('id-bolt', 'Lightning Bolt', 'lea', '161'))
        # Patch art so compose doesn't need CDN
        res = client.post(
            '/api/jobs',
            data={'card_name': 'Lightning Bolt', 'game': 'mtg', 'render_mode': 'compose'},
            files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 200
        body = res.json()
        assert body['render_mode'] == 'compose'
        assert body['status'] == 'done'

    def test_api_compose_preview(self, client):
        card = {
            'name': 'Opt', 'mana_cost': '{U}', 'type_line': 'Instant',
            'oracle_text': 'Scry 1.', 'colors': ['U'], 'set': 'dom',
            'collector_number': '60',
        }
        res = client.post(
            '/api/compose',
            data={'game': 'mtg', 'card_json': __import__('json').dumps(card)},
            files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 200
        assert res.headers['content-type'].startswith('image/png')
        assert res.content[:8] == b'\x89PNG\r\n\x1a\n'

    def test_api_compose_with_art_transform(self, client):
        import json as _json
        card = {
            'name': 'Opt', 'mana_cost': '{U}', 'type_line': 'Instant',
            'oracle_text': 'Scry 1.', 'colors': ['U'], 'set': 'dom',
            'collector_number': '60',
        }
        res = client.post(
            '/api/compose',
            data={
                'game': 'mtg',
                'card_json': _json.dumps(card),
                'art_transform': _json.dumps({
                    'scale': 1.4, 'offset_x': -0.3, 'offset_y': 0.2}),
            },
            files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 200
        assert res.content[:8] == b'\x89PNG\r\n\x1a\n'

    def test_submit_compose_with_client_card_json(self, appmod, client):
        import json as _json
        appmod.carddb.store_card(make_card('cj-1', 'Lightning Bolt', 'lea', '161'))
        # Fake art so compose doesn't need CDN
        from pathlib import Path
        from PIL import Image
        art = Path(appmod.IMAGES_DIR) / 'cj-1-art_crop.jpg'
        art.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (64, 64), (200, 40, 40)).save(art)

        edited = {
            'id': 'cj-1',
            'name': 'Lightning Bolt',
            'mana_cost': '{R}',
            'type_line': 'Instant',
            'oracle_text': 'CUSTOM TEXT FROM EDITOR',
            'colors': ['R'],
            'set': 'lea',
            'collector_number': '161',
        }
        res = client.post(
            '/api/jobs',
            data={
                'card_name': 'Lightning Bolt',
                'game': 'mtg',
                'render_mode': 'compose',
                'card_json': _json.dumps(edited),
            },
            files={'art': ('custom.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 200
        body = res.json()
        assert body['status'] == 'done'
        job = client.get(f"/api/jobs/{body['id']}").json()
        stored = _json.loads(job['card_json'])
        assert stored['oracle_text'] == 'CUSTOM TEXT FROM EDITOR'
        assert stored.get('_custom_art') is True

    def test_editor_page(self, appmod, client):
        appmod.carddb.store_card(make_card('edit-1', 'Sol Ring', 'c21', '125'))
        # Legacy /edit redirects into Make workspace
        bare = client.get('/edit', follow_redirects=False)
        assert bare.status_code in (302, 307)
        assert bare.headers['location'] == '/'
        with_id = client.get('/edit?card_id=edit-1', follow_redirects=False)
        assert with_id.status_code in (302, 307)
        assert 'card_id=edit-1' in with_id.headers['location']
        home = client.get('/?card_id=edit-1')
        assert home.status_code == 200
        assert b'Sol Ring' in home.content
        assert b'Make a card' in home.content
        page = client.get('/card/edit-1')
        assert page.status_code == 200
        assert b'/?card_id=edit-1' in page.content
        assert b'Open in editor' in page.content

    def test_delete_job(self, client):
        res = submit_job(client, name='Delete Me')
        job_id = res.json()['id']
        assert client.get(f'/api/jobs/{job_id}').status_code == 200
        deleted = client.delete(f'/api/jobs/{job_id}')
        assert deleted.status_code == 200
        assert deleted.json()['ok'] is True
        assert client.get(f'/api/jobs/{job_id}').status_code == 404
        assert client.delete(f'/api/jobs/{job_id}').status_code == 404

    def test_search_includes_thumb(self, appmod, client):
        appmod.carddb.store_card(make_card('thumb-1', 'Lightning Bolt', 'lea', '161'))
        body = client.get('/api/cards/search', params={'q': 'lightning', 'game': 'mtg'}).json()
        assert body['cards']
        card = body['cards'][0]
        assert card['id'] == 'thumb-1'
        assert card['thumb'] == '/api/cards/thumb-1/image?kind=large'
        assert card['game'] == 'mtg'

    def test_card_resolution_flag(self, appmod, client):
        # Unknown card in offline mode -> queued but unresolved
        assert submit_job(client).json()['card_resolved'] is False
        # Known card -> resolved with embedded JSON
        appmod.carddb.store_card(make_card('id-1', 'Sol Ring', 'c21', '125'))
        res = submit_job(client, name='Sol Ring')
        assert res.json()['card_resolved'] is True

    def test_rejects_bad_extension(self, client):
        res = client.post(
            '/api/jobs', data={'card_name': 'Bolt'},
            files={'art': ('art.exe', io.BytesIO(b'MZ'), 'application/x-msdownload')})
        assert res.status_code == 422

    def test_upload_cap_413(self, client):
        big = io.BytesIO(b'0' * (2 * 1024 * 1024))  # 2MB > 1MB cap
        res = client.post(
            '/api/jobs', data={'card_name': 'Bolt'},
            files={'art': ('art.png', big, 'image/png')})
        assert res.status_code == 413

    def test_idempotency_key_dedupes(self, client):
        a = submit_job(client, idempotency_key='same-key').json()
        b = submit_job(client, idempotency_key='same-key').json()
        assert a['id'] == b['id']
        assert len(client.get('/api/jobs').json()) == 1

    def test_missing_card_name_422(self, client):
        res = client.post(
            '/api/jobs', data={},
            files={'art': ('a.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 422


class TestRateLimits:

    def test_submit_limited_with_retry_after(self, appmod, client):
        limit, _ = appmod.RATE_LIMITS['submit']
        for _ in range(limit):
            assert submit_job(client).status_code == 200
        res = submit_job(client)
        assert res.status_code == 429
        assert 'Retry-After' in res.headers


class TestWorkerAuth:

    def test_worker_endpoints_require_token(self, client):
        assert client.get('/api/worker/jobs/next').status_code == 401
        assert client.post('/api/worker/heartbeat').status_code == 401
        bad = {'Authorization': 'Bearer wrong'}
        assert client.get('/api/worker/jobs/next', headers=bad).status_code == 401


class TestWorkerLifecycle:

    def test_full_job_lifecycle(self, client):
        # Handshake
        caps = {'worker_name': 'w1', 'proxyshop_version': 'test',
                'templates': {'normal': [{'name': 'M15', 'class_name': 'M15Template'}]}}
        assert client.post('/api/worker/hello', json=caps, headers=WORKER_AUTH).status_code == 200
        assert client.get('/api/templates').json()['templates']['normal'][0]['name'] == 'M15'

        # Submit + claim
        job_id = submit_job(client).json()['id']
        claimed = client.get('/api/worker/jobs/next', headers=WORKER_AUTH).json()
        assert claimed['id'] == job_id

        # Art download
        art = client.get(f'/api/worker/jobs/{job_id}/art', headers=WORKER_AUTH)
        assert art.status_code == 200
        assert art.content == PNG_BYTES

        # Status -> rendering
        assert client.post(
            f'/api/worker/jobs/{job_id}/status', params={'status': 'rendering'},
            headers=WORKER_AUTH).status_code == 200

        # Result upload
        res = client.post(
            f'/api/worker/jobs/{job_id}/result', headers=WORKER_AUTH,
            data={'ok': 'true', 'log': 'rendered fine'},
            files={'result': ('out.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 200

        job = client.get(f'/api/jobs/{job_id}').json()
        assert job['status'] == 'done'
        assert job['log'] == 'rendered fine'

        # Browser download
        dl = client.get(f'/api/jobs/{job_id}/result')
        assert dl.status_code == 200
        assert dl.content == PNG_BYTES

    def test_no_jobs_204(self, client):
        res = client.get('/api/worker/jobs/next', headers=WORKER_AUTH)
        assert res.status_code == 204

    def test_success_without_file_422(self, client):
        job_id = submit_job(client).json()['id']
        client.get('/api/worker/jobs/next', headers=WORKER_AUTH)
        res = client.post(
            f'/api/worker/jobs/{job_id}/result', headers=WORKER_AUTH,
            data={'ok': 'true'})
        assert res.status_code == 422

    def test_failure_report_requeues(self, client):
        job_id = submit_job(client).json()['id']
        client.get('/api/worker/jobs/next', headers=WORKER_AUTH)
        client.post(
            f'/api/worker/jobs/{job_id}/result', headers=WORKER_AUTH,
            data={'ok': 'false', 'error': 'PS crashed'})
        assert client.get(f'/api/jobs/{job_id}').json()['status'] == 'queued'


class FakeImageSession:
    """Serves a small generated PNG for any image URL."""

    def get(self, url, **kwargs):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (74, 104), (60, 60, 200)).save(buf, 'PNG')
        payload = buf.getvalue()

        class Res:
            status_code = 200
            def iter_content(self, chunk_size):
                yield payload
        return Res()


def _card_with_images(card_id, name, set_code='sta', number='42'):
    card = make_card(card_id, name, set_code, number)
    card['image_uris'] = {
        'png': f'https://cards.example/{card_id}.png',
        'art_crop': f'https://cards.example/{card_id}-art.jpg'}
    return card


class TestArtlessSubmit:

    def test_submit_without_art_uses_scryfall_art(self, appmod, client):
        appmod.carddb.store_card(_card_with_images('id-1', 'Lightning Bolt'))
        appmod.carddb._session = FakeImageSession()
        appmod.OFFLINE = False
        res = client.post('/api/jobs', data={'card_name': 'Lightning Bolt'})
        assert res.status_code == 200
        job = client.get(f"/api/jobs/{res.json()['id']}").json()
        assert job['art_filename'] == 'art.jpg'

    def test_submit_without_art_unknown_card_422(self, client):
        res = client.post('/api/jobs', data={'card_name': 'Not A Real Card'})
        assert res.status_code == 422

    def test_submit_without_art_no_image_offline_422(self, appmod, client):
        # Card known but image not cached and offline -> reject with guidance
        appmod.carddb.store_card(_card_with_images('id-2', 'Sol Ring'))
        res = client.post('/api/jobs', data={'card_name': 'Sol Ring'})
        assert res.status_code == 422
        assert 'upload an art file' in res.json()['detail']


class TestDeckImages:

    def _deck_with_cards(self, appmod):
        appmod.carddb.store_card(_card_with_images('id-1', 'Lightning Bolt'))
        appmod.carddb.store_card(_card_with_images('id-2', 'Sol Ring', 'c21', '125'))
        return appmod.carddb.save_deck('Zip Deck', [
            ('id-1', 'Lightning Bolt', 4, 'main'),
            ('id-2', 'Sol Ring', 1, 'main'),
            (None, 'Mystery Card', 1, 'main')])

    def test_zip_contains_unique_images_and_manifest(self, appmod, client, tmp_path):
        import io
        import zipfile
        deck_id = self._deck_with_cards(appmod)
        appmod.carddb._session = FakeImageSession()
        appmod.OFFLINE = False
        res = client.get(f'/api/decks/{deck_id}/images')
        assert res.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(res.content))
        names = set(zf.namelist())
        assert 'decklist.txt' in names
        assert any(n.startswith('Lightning Bolt [STA]') for n in names)
        assert any(n.startswith('Sol Ring [C21]') for n in names)
        listing = zf.read('decklist.txt').decode()
        assert '4 Lightning Bolt' in listing
        assert 'Mystery Card' in listing  # reported as missing

    def test_zip_404_unknown_deck(self, client):
        assert client.get('/api/decks/nope/images').status_code == 404


class TestSheets:

    def test_sheet_from_deck(self, appmod, client):
        appmod.carddb.store_card(_card_with_images('id-1', 'Lightning Bolt'))
        deck_id = appmod.carddb.save_deck(
            'Sheet Deck', [('id-1', 'Lightning Bolt', 10, 'main')])
        appmod.carddb._session = FakeImageSession()
        appmod.OFFLINE = False
        res = client.post('/api/sheets', data={'deck_id': deck_id})
        assert res.status_code == 200
        body = res.json()
        assert body['cards'] == 10
        assert body['pages'] == 2  # 9 per page
        pdf = client.get(body['url'])
        assert pdf.status_code == 200
        assert pdf.content[:5] == b'%PDF-'

    def test_sheet_requires_source(self, client):
        assert client.post('/api/sheets', data={}).status_code == 422

    def test_sheet_bad_paper_422(self, appmod, client):
        res = client.post('/api/sheets', data={'deck_id': 'x', 'paper': 'legal'})
        assert res.status_code == 422


class TestGalleryFilters:

    def _seed(self, appmod):
        boss = make_card('pkm-1', 'Boss Orders', 'sv1', '189')
        boss['game'] = 'pokemon'
        boss['provider_data'] = {'supertype': 'Trainer', 'subtypes': ['Supporter'],
                                 'types': [], 'rarity': 'Rare Holo'}
        pika = make_card('pkm-2', 'Pikachu', 'sv1', '63')
        pika['game'] = 'pokemon'
        pika['provider_data'] = {'supertype': 'Pokémon', 'subtypes': ['Basic'],
                                 'types': ['Lightning'], 'rarity': 'Common'}
        for c in (boss, pika):
            appmod.carddb.store_card(c, game='pokemon')

    def test_compose_query_quotes_spaces(self, appmod):
        assert appmod._compose_gallery_query(
            'pika', {'supertype': 'Trainer', 'rarity': 'Rare Holo', 'type': ''}
        ) == 'pika supertype:Trainer rarity:"Rare Holo"'

    def test_gallery_supertype_dropdown(self, appmod, client):
        self._seed(appmod)
        r = client.get('/gallery', params={'game': 'pokemon', 'fsupertype': 'Trainer'})
        assert r.status_code == 200
        assert 'Boss Orders' in r.text
        assert 'Pikachu' not in r.text

    def test_gallery_rarity_with_space(self, appmod, client):
        self._seed(appmod)
        r = client.get('/gallery', params={'game': 'pokemon', 'frarity': 'Rare Holo'})
        assert r.status_code == 200
        assert 'Boss Orders' in r.text
        assert 'Pikachu' not in r.text


class TestGallerySortViews:

    def _seed_mtg(self, appmod):
        cheap = make_card('m-1', 'Llanowar Elves', 'dom', '168')
        cheap.update({'cmc': 1, 'rarity': 'common', 'colors': ['G'],
                      'type_line': 'Creature — Elf Druid', 'mana_cost': '{G}',
                      'oracle_text': '{T}: Add {G}.', 'power': '1', 'toughness': '1',
                      'artist': 'Chris Rahn', 'edhrec_rank': 200,
                      'prices': {'usd': '0.25', 'eur': '0.20', 'tix': '0.02'},
                      'legalities': {'commander': 'legal', 'modern': 'legal',
                                     'standard': 'not_legal'}})
        pricey = make_card('m-2', 'Mana Crypt', 'ema', '225')
        pricey.update({'cmc': 0, 'rarity': 'mythic', 'colors': [],
                       'type_line': 'Artifact', 'mana_cost': '{0}',
                       'artist': 'Alan Pollack', 'edhrec_rank': 5,
                       'prices': {'usd': '120.00', 'eur': '90.00', 'tix': '20.0'},
                       'legalities': {'commander': 'legal', 'modern': 'not_legal'}})
        for c in (cheap, pricey):
            appmod.carddb.store_card(c)

    def test_price_sort_desc(self, appmod, client):
        self._seed_mtg(appmod)
        r = client.get('/gallery', params={'game': 'mtg', 'sort': 'usd'})
        assert r.status_code == 200
        # Default (Auto) direction for price is descending — pricey card first.
        assert r.text.index('Mana Crypt') < r.text.index('Llanowar Elves')

    def test_price_sort_asc_override(self, appmod, client):
        self._seed_mtg(appmod)
        r = client.get('/gallery',
                       params={'game': 'mtg', 'sort': 'usd', 'direction': 'asc'})
        assert r.text.index('Llanowar Elves') < r.text.index('Mana Crypt')

    def test_mtg_offers_scryfall_sorts(self, appmod, client):
        self._seed_mtg(appmod)
        r = client.get('/gallery', params={'game': 'mtg'})
        for label in ('Mana value', 'EDHREC rank', 'Price: USD', 'Artist name'):
            assert label in r.text

    def test_other_game_hides_price_sorts(self, appmod, client):
        appmod.carddb.store_card(make_card('m-1', 'Llanowar Elves', 'dom', '168'))
        r = client.get('/gallery', params={'game': 'pokemon'})
        assert 'Mana value' not in r.text
        assert 'EDHREC rank' not in r.text

    def test_checklist_view_shows_details(self, appmod, client):
        self._seed_mtg(appmod)
        r = client.get('/gallery', params={'game': 'mtg', 'view': 'checklist'})
        assert r.status_code == 200
        assert 'checklist' in r.text
        assert 'Artist' in r.text and 'Chris Rahn' in r.text

    def test_full_view_shows_oracle_and_legalities(self, appmod, client):
        self._seed_mtg(appmod)
        r = client.get('/gallery', params={'game': 'mtg', 'view': 'full'})
        assert r.status_code == 200
        assert 'card-full-list' in r.text
        assert 'Creature — Elf Druid' in r.text
        assert 'legal-yes' in r.text

    def test_list_view_shows_type(self, appmod, client):
        self._seed_mtg(appmod)
        r = client.get('/gallery', params={'game': 'mtg', 'view': 'list'})
        assert 'card-detail-list' in r.text
        assert 'Artifact' in r.text

    def test_full_view_lists_other_prints(self, appmod, client):
        # Two printings of the same card (shared oracle_id → one art group).
        p1 = make_card('sol-1', 'Sol Ring', 'c21', '263')
        p2 = make_card('sol-2', 'Sol Ring', 'lea', '270')
        p1['prices'] = {'usd': '1.50'}
        p2['prices'] = {'usd': '3500.00'}
        for c in (p1, p2):
            appmod.carddb.store_card(c)
        r = client.get('/gallery',
                       params={'game': 'mtg', 'view': 'full', 'arts': 'combine'})
        assert r.status_code == 200
        assert 'full-prints' in r.text
        # The prints panel lists both set printings of the grouped card.
        assert 'C21 #263' in r.text
        assert 'LEA #270' in r.text


class TestMultiGameSearch:

    def _pokemon_card(self, card_id='pkm-xy7-54', name='Gardevoir'):
        return {'object': 'card', 'id': card_id, 'game': 'pokemon', 'name': name,
                'set': 'xy7', 'set_name': 'Ancient Origins', 'collector_number': '54',
                'lang': 'en', 'released_at': '2015-08-12',
                'images': {'large': 'https://img.example/big.png'}}

    def test_unknown_game_422(self, client):
        res = client.get('/api/cards/search', params={'q': 'pikachu', 'game': 'yugioh'})
        assert res.status_code == 422

    def test_provider_fallback_caches(self, appmod, client, monkeypatch):
        from web.shared import games as games_mod
        appmod.OFFLINE = False
        monkeypatch.setitem(
            games_mod.PROVIDERS, 'pokemon',
            lambda q, limit: [self._pokemon_card()])
        body = client.get('/api/cards/search',
                          params={'q': 'gardevoir', 'game': 'pokemon'}).json()
        assert body['source'] == 'live'
        assert body['cards'][0]['id'] == 'pkm-xy7-54'
        # Cached: now resolves locally with the provider gone
        monkeypatch.setitem(
            games_mod.PROVIDERS, 'pokemon',
            lambda q, limit: (_ for _ in ()).throw(AssertionError('should not be called')))
        body2 = client.get('/api/cards/search',
                           params={'q': 'gardevoir', 'game': 'pokemon'}).json()
        assert body2['source'] == 'local'

    def test_provider_error_502_with_message(self, appmod, client, monkeypatch):
        from web.shared import games as games_mod
        appmod.OFFLINE = False

        def boom(q, limit):
            raise games_mod.ProviderError('needs an API key')
        monkeypatch.setitem(games_mod.PROVIDERS, 'union-arena', boom)
        res = client.get('/api/cards/search',
                         params={'q': 'itadori', 'game': 'union-arena'})
        assert res.status_code == 502
        assert 'API key' in res.json()['detail']

    def test_offline_non_mtg_stays_local(self, client):
        body = client.get('/api/cards/search',
                          params={'q': 'gardevoir', 'game': 'pokemon'}).json()
        assert body == {'source': 'local', 'game': 'pokemon', 'cards': []}


class TestCardViews:

    def test_card_detail_page(self, appmod, client):
        appmod.carddb.store_card(_card_with_images('id-1', 'Lightning Bolt'))
        res = client.get('/card/id-1')
        assert res.status_code == 200
        assert 'Lightning Bolt' in res.text
        assert '/api/cards/id-1/image?kind=png' in res.text

    def test_card_detail_404(self, client):
        assert client.get('/card/nope').status_code == 404

    def test_card_image_endpoint(self, appmod, client):
        appmod.carddb.store_card(_card_with_images('id-1', 'Lightning Bolt'))
        appmod.carddb._session = FakeImageSession()
        appmod.OFFLINE = False
        res = client.get('/api/cards/id-1/image', params={'kind': 'png'})
        assert res.status_code == 200
        assert res.content[:8] == b'\x89PNG\r\n\x1a\n'
        # Immutable cache headers so grid thumbs aren't re-fetched per view
        assert 'immutable' in res.headers.get('cache-control', '')

    def test_card_image_bad_kind_422(self, appmod, client):
        appmod.carddb.store_card(_card_with_images('id-1', 'Lightning Bolt'))
        assert client.get('/api/cards/id-1/image',
                          params={'kind': 'huge'}).status_code == 422

    def test_card_image_placeholder_when_no_art(self, appmod, client):
        # Some cards (e.g. basic Pokemon Energy) have no image at all. The
        # endpoint serves a card-shaped SVG placeholder rather than 404ing so
        # the gallery/detail views still render a tile.
        appmod.carddb.store_card(make_card('id-1', 'Basic Energy', 'svi', '1'))
        appmod.OFFLINE = False
        res = client.get('/api/cards/id-1/image', params={'kind': 'png'})
        assert res.status_code == 200
        assert res.headers['content-type'].startswith('image/svg+xml')
        assert 'Basic Energy' in res.text

    def test_card_image_unknown_id_still_404(self, client):
        assert client.get('/api/cards/nope/image').status_code == 404


class TestDeckImport:

    def test_paste_import_offline(self, appmod, client):
        appmod.carddb.store_card(make_card('id-1', 'Lightning Bolt', 'sta', '42'))
        res = client.post('/api/decks/import', data={
            'name': 'My Deck',
            'text': '4 Lightning Bolt\n1 Card That Does Not Exist'})
        assert res.status_code == 200
        report = res.json()
        assert report['deck_name'] == 'My Deck'
        assert len(report['resolved']) == 1
        assert len(report['unresolved']) == 1
        assert report['from_cache'] == 1
        # Deck persisted
        decks = client.get('/api/decks').json()
        assert decks[0]['name'] == 'My Deck'
        assert decks[0]['cards'] == 5

    def test_empty_import_422(self, client):
        assert client.post('/api/decks/import', data={}).status_code == 422
        assert client.post('/api/decks/import', data={'text': '// nothing'}).status_code == 422

    def test_card_search_api_local(self, appmod, client):
        appmod.carddb.store_card(make_card('id-1', 'Lightning Bolt', 'sta', '42'))
        body = client.get('/api/cards/search', params={'q': 'light'}).json()
        assert body['source'] == 'local'
        assert body['cards'][0]['name'] == 'Lightning Bolt'
        short = client.get('/api/cards/search', params={'q': 'x'}).json()
        assert short == {'source': 'local', 'game': 'mtg', 'cards': []}

    def test_card_search_offline_miss_stays_empty(self, client):
        # Offline mode: no local match must NOT attempt a Scryfall call
        body = client.get('/api/cards/search', params={'q': 'black lotus'}).json()
        assert body['cards'] == []

    def test_card_search_scryfall_fallback(self, appmod, client):
        # Allow network path, stub the Scryfall session
        appmod.carddb.offline = False

        class FakeResponse:
            status_code = 200
            def json(self):
                return {'object': 'list',
                        'data': [make_card('api-9', 'Black Lotus', 'lea', '232')]}

        class FakeSession:
            def get(self, url, **kwargs):
                return FakeResponse()

        appmod.carddb._session = FakeSession()
        body = client.get('/api/cards/search', params={'q': 'black lotus'}).json()
        assert body['source'] == 'scryfall'
        assert body['cards'][0]['name'] == 'Black Lotus'
        # Result was cached: subsequent search is local even with network off
        appmod.carddb.offline = True
        body2 = client.get('/api/cards/search', params={'q': 'black lotus'}).json()
        assert body2['source'] == 'local'
        assert body2['cards'][0]['name'] == 'Black Lotus'


class TestTagCache:

    def test_cached_tag_resolves_locally(self, appmod, client):
        # Seed two dragons + record the tag membership, then search offline.
        for cid, name in (('d-1', 'Shivan Dragon'), ('d-2', 'Dragon Whelp')):
            appmod.carddb.store_card(make_card(cid, name, 'tst', cid[-1]))
        appmod.carddb.record_tag('art:dragon', ['d-1', 'd-2'])
        body = client.get('/api/cards/search',
                          params={'q': 'art:dragon', 'game': 'mtg'}).json()
        assert body['source'] == 'tag-cache'
        assert {c['name'] for c in body['cards']} == {'Shivan Dragon', 'Dragon Whelp'}

    def test_card_library_filters_by_cached_tag(self, appmod, client):
        # Card Library (/gallery) is cached-only: a cached tag filters the grid;
        # an uncached tag shows nothing (never a live call on keystroke).
        for cid, name in (('d-1', 'Shivan Dragon'), ('d-2', 'Dragon Whelp'),
                          ('e-1', 'Llanowar Elves')):
            appmod.carddb.store_card(make_card(cid, name, 'tst', cid[-1]))
        appmod.carddb.record_tag('art:dragon', ['d-1', 'd-2'])
        r = client.get('/gallery', params={'game': 'mtg', 'q': 'art:dragon'})
        assert 'Shivan Dragon' in r.text and 'Dragon Whelp' in r.text
        assert 'Llanowar Elves' not in r.text
        # Uncached tag → empty grid, no literal-text matches.
        r2 = client.get('/gallery', params={'game': 'mtg', 'q': 'art:unknown'})
        assert 'Shivan Dragon' not in r2.text

    def test_uncached_tag_offline_is_empty(self, appmod, client):
        # Offline + not cached → no local field-scan for the literal 'art:' text.
        body = client.get('/api/cards/search',
                          params={'q': 'art:dragon', 'game': 'mtg'}).json()
        assert body['source'] == 'tag-uncached'
        assert body['cards'] == []

    def test_uncached_tag_goes_live(self, appmod, client):
        appmod.OFFLINE = False
        appmod.carddb.offline = False

        class FakeResponse:
            status_code = 200
            def json(self):
                return {'object': 'list',
                        'data': [make_card('api-d', 'Bane of the Living', 'tst', '9')]}

        class FakeSession:
            def get(self, url, **kwargs):
                return FakeResponse()

        appmod.carddb._session = FakeSession()
        body = client.get('/api/cards/search',
                          params={'q': 'art:dragon', 'game': 'mtg'}).json()
        assert body['source'] == 'scryfall'

    def test_tags_api_list_and_delete(self, appmod, client):
        appmod.carddb.store_card(make_card('d-1', 'Shivan Dragon', 'tst', '1'))
        appmod.carddb.record_tag('art:dragon', ['d-1'])
        listing = client.get('/api/tags').json()
        assert listing['tags'][0]['tag'] == 'art:dragon'
        assert listing['tags'][0]['count'] == 1
        r = client.post('/api/tags/delete', json={'tag': 'art:dragon'})
        assert r.status_code == 200 and r.json()['removed'] is True
        assert client.get('/api/tags').json()['tags'] == []

    def test_tags_delete_requires_tag(self, client):
        assert client.post('/api/tags/delete', json={}).status_code == 422

    def test_library_offers_to_cache_uncached_tag(self, appmod, client):
        # An uncached tag in the Card Library surfaces the cache affordance
        # (online) rather than a live fetch.
        appmod.OFFLINE = False
        html = client.get('/gallery', params={'q': 'art:dragon', 'game': 'mtg'}).text
        assert 'Cache this tag for offline' in html

    def test_search_redirects_to_library(self, client):
        # /search is retired → redirects to the consolidated Card Library.
        res = client.get('/search', params={'q': 'bolt', 'game': 'mtg'},
                         follow_redirects=False)
        assert res.status_code in (302, 307)
        assert res.headers['location'].startswith('/gallery')
        assert 'q=bolt' in res.headers['location']


class TestCacheGameApi:

    def test_status_idle(self, client):
        body = client.get('/api/cache-game/riftbound').json()
        assert body['status'] == 'idle'
        assert body['running'] is False
        assert body['db_count'] == 0

    def test_unknown_game_422(self, client):
        assert client.get('/api/cache-game/yugioh').status_code == 422
        assert client.post('/api/cache-game/yugioh/start').status_code == 422

    def test_start_blocked_when_offline(self, client):
        res = client.post('/api/cache-game/riftbound/start')
        assert res.status_code == 503

    def test_mtg_requires_filters(self, appmod, client):
        appmod.OFFLINE = False
        res = client.post(
            '/api/cache-game/mtg/start',
            content=b'{"filters":{}}',
            headers={'Content-Type': 'application/json'})
        assert res.status_code == 422
        assert 'filter' in res.json()['detail'].lower()

    def test_start_stop_with_stub(self, appmod, client, monkeypatch):
        appmod.OFFLINE = False
        started = {'n': 0}
        stopped = {'n': 0}

        def fake_start(game, **kwargs):
            started['n'] += 1
            return {
                'game': game, 'status': 'running', 'running': True,
                'stored': 0, 'images_ok': 0, 'images_skip': 0, 'images_fail': 0,
                'db_count': 0, 'filters': kwargs.get('filters') or {},
            }

        def fake_stop(game, **kwargs):
            stopped['n'] += 1
            return {
                'game': game, 'status': 'stopped', 'running': False,
                'stored': 3, 'images_ok': 2, 'images_skip': 0, 'images_fail': 1,
                'db_count': 3, 'message': 'stop requested',
            }

        monkeypatch.setattr(appmod.cache_runner, 'enqueue', fake_start)
        monkeypatch.setattr(appmod.cache_runner, 'stop', fake_stop)
        body = client.post(
            '/api/cache-game/mtg/start',
            content=b'{"filters":{"set":"mh3"}}',
            headers={'Content-Type': 'application/json'}).json()
        assert body['running'] is True
        assert started['n'] == 1
        body = client.post('/api/cache-game/mtg/stop').json()
        assert body['status'] == 'stopped'
        assert stopped['n'] == 1

    def test_card_library_includes_cache_panel(self, client):
        res = client.get('/gallery', params={'game': 'riftbound'})
        assert res.status_code == 200
        assert 'id="cache-panel"' in res.text
        assert 'Download' in res.text
        assert 'cache-jobs' in res.text
        # The live log now lives on the Logs tab, linked from the cache panel.
        assert 'id="cache-log-link"' in res.text
        assert 'id="cache-log"' not in res.text

    def test_cache_jobs_endpoint(self, client):
        body = client.get('/api/cache-jobs').json()
        assert 'jobs' in body
        assert 'any_running' in body
        assert body['any_running'] is False
        for game in ('mtg', 'pokemon', 'riftbound', 'union-arena'):
            assert game in body['jobs']

    def test_status_includes_queue(self, client):
        body = client.get('/api/cache-game/mtg').json()
        assert body['queue'] == []
        assert body['queued_count'] == 0

    def test_start_enqueues_without_running(self, appmod, client, monkeypatch):
        # Stub the worker so /start only enqueues (no real download thread).
        appmod.OFFLINE = False
        monkeypatch.setattr(appmod.cache_runner, '_ensure_worker',
                            lambda *a, **k: None)
        r = client.post(
            '/api/cache-game/mtg/start',
            content=b'{"filters":{"tags":"art:dragon"}}',
            headers={'Content-Type': 'application/json'})
        assert r.status_code == 200
        body = r.json()
        assert body['queued_count'] == 1
        assert body['queue'][0]['label'] == 'art:dragon'
        # A second, different filter stacks; identical one de-dupes.
        client.post('/api/cache-game/mtg/start',
                    content=b'{"filters":{"tags":"art:angel"}}',
                    headers={'Content-Type': 'application/json'})
        client.post('/api/cache-game/mtg/start',
                    content=b'{"filters":{"tags":"art:dragon"}}',
                    headers={'Content-Type': 'application/json'})
        body = client.get('/api/cache-game/mtg').json()
        assert body['queued_count'] == 2

    def test_queue_remove_and_clear(self, appmod, client, monkeypatch):
        appmod.OFFLINE = False
        monkeypatch.setattr(appmod.cache_runner, '_ensure_worker',
                            lambda *a, **k: None)
        for tag in ('art:dragon', 'art:angel', 'art:demon'):
            client.post('/api/cache-game/mtg/start',
                        content=f'{{"filters":{{"tags":"{tag}"}}}}'.encode(),
                        headers={'Content-Type': 'application/json'})
        body = client.get('/api/cache-game/mtg').json()
        assert body['queued_count'] == 3
        pending_id = body['queue'][1]['id']
        body = client.post('/api/cache-game/mtg/queue/remove',
                           json={'id': pending_id}).json()
        assert body['queued_count'] == 2
        # Clear pending leaves only the head.
        body = client.post('/api/cache-game/mtg/queue/clear').json()
        assert body['queued_count'] == 1

    def test_queue_remove_requires_id(self, client):
        assert client.post(
            '/api/cache-game/mtg/queue/remove', json={}).status_code == 422

    def test_resume_blocked_offline(self, client):
        assert client.post('/api/cache-game/mtg/resume').status_code == 503

    def test_cache_log_endpoint(self, client, tmp_path, appmod):
        appmod.CACHE_RUNS_DIR = tmp_path
        appmod.cache_runner._logs.clear()
        empty = client.get('/api/cache-game/riftbound/log').json()
        assert empty['game'] == 'riftbound'
        assert empty['lines'] == []
        appmod.cache_runner.log('riftbound', tmp_path, 'hello from test')
        body = client.get('/api/cache-game/riftbound/log').json()
        assert len(body['lines']) == 1
        assert 'hello from test' in body['lines'][0]

    def test_logs_page(self, client):
        res = client.get('/logs', params={'game': 'riftbound'})
        assert res.status_code == 200
        assert 'id="logs-page"' in res.text
        assert 'id="logs-log"' in res.text
        assert 'Logs' in res.text
