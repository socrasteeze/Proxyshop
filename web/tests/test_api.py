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

    def test_health(self, client):
        body = client.get('/api/health').json()
        assert body['ok'] is True
        assert body['offline'] is True


class TestJobSubmission:

    def test_submit_pokemon_requires_art(self, client):
        res = client.post('/api/jobs', data={
            'card_name': 'Pikachu', 'game': 'pokemon'})
        assert res.status_code == 422
        assert 'art' in res.json()['detail'].lower()

    def test_submit_rejects_non_renderable_game(self, client):
        res = client.post(
            '/api/jobs',
            data={'card_name': 'Annie', 'game': 'riftbound'},
            files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 422
        assert 'not renderable' in res.json()['detail'].lower()

    def test_submit_pokemon_with_art(self, client):
        res = client.post(
            '/api/jobs',
            data={'card_name': 'Pikachu', 'game': 'pokemon', 'set_code': 'sv1'},
            files={'art': ('art.png', io.BytesIO(PNG_BYTES), 'image/png')})
        assert res.status_code == 200
        body = res.json()
        assert body['game'] == 'pokemon'
        job = client.get(f"/api/jobs/{body['id']}").json()
        assert job['game'] == 'pokemon'

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

    def test_card_image_bad_kind_422(self, appmod, client):
        appmod.carddb.store_card(_card_with_images('id-1', 'Lightning Bolt'))
        assert client.get('/api/cards/id-1/image',
                          params={'kind': 'huge'}).status_code == 422


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
