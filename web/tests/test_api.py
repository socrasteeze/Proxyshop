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

    def test_submit_and_fetch(self, client):
        res = submit_job(client)
        assert res.status_code == 200
        job_id = res.json()['id']
        job = client.get(f'/api/jobs/{job_id}').json()
        assert job['status'] == 'queued'
        assert job['card_name'] == 'Lightning Bolt'
        assert job['art_filename'] == 'art.png'

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
        assert short == {'source': 'local', 'cards': []}

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
