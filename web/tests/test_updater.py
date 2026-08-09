"""
* Update-request tests — the app hands nas-update.sh to a host watcher, since
* it cannot run a script that rebuilds the container it lives in.
* All offline; the watcher is simulated by writing the files it would write.
"""
# Standard Library Imports
import importlib
import json
import time

# Third Party Imports
import pytest
from fastapi.testclient import TestClient

# Local Imports
from web.server import updater


@pytest.fixture()
def data_dir(tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    return d


def beat(data_dir, age=0.0):
    """Simulate the host watcher's heartbeat, optionally stale."""
    updater._write_json(
        updater.update_dir(data_dir) / 'watch.json',
        {'at': time.time() - age, 'pid': 1234})


def watcher_status(data_dir, **fields):
    updater._write_json(updater.update_dir(data_dir) / 'status.json', fields)


class TestUpdaterState:

    def test_idle_without_watcher(self, data_dir):
        st = updater.status(data_dir)
        assert st['state'] == 'idle'
        assert st['watcher_online'] is False
        assert st['watcher_seen_at'] is None

    def test_stale_heartbeat_reads_as_offline(self, data_dir):
        beat(data_dir, age=updater.WATCHER_STALE_AFTER + 10)
        st = updater.status(data_dir)
        assert st['watcher_online'] is False
        assert st['watcher_seen_at']  # still reported, just old

    def test_request_writes_flag_file(self, data_dir):
        beat(data_dir)
        st = updater.request_update(data_dir, requested_by='10.0.0.5')
        assert st['state'] == 'requested'
        req = json.loads(
            (updater.update_dir(data_dir) / 'request.json').read_text('utf-8'))
        assert req['requested_by'] == '10.0.0.5'
        assert req['id']

    def test_request_is_idempotent_while_outstanding(self, data_dir):
        beat(data_dir)
        first = updater.request_update(data_dir)
        req_path = updater.update_dir(data_dir) / 'request.json'
        original = req_path.read_text('utf-8')
        second = updater.request_update(data_dir)
        assert first['state'] == second['state'] == 'requested'
        assert req_path.read_text('utf-8') == original  # not re-queued

    def test_no_second_request_while_running(self, data_dir):
        beat(data_dir)
        watcher_status(data_dir, state='running', started_at='2026-01-01T00:00:00Z')
        updater.request_update(data_dir)
        assert not (updater.update_dir(data_dir) / 'request.json').exists()
        assert updater.status(data_dir)['state'] == 'running'

    def test_pending_request_outranks_previous_success(self, data_dir):
        """A fresh request must not read as 'done' from the last run."""
        beat(data_dir)
        watcher_status(data_dir, state='ok', exit_code=0,
                       finished_at='2026-01-01T00:00:00Z')
        updater.request_update(data_dir)
        assert updater.status(data_dir)['state'] == 'requested'

    def test_stale_request_is_ignored(self, data_dir):
        """A watcher that died mid-claim can't block updates forever."""
        updater._write_json(
            updater.update_dir(data_dir) / 'request.json',
            {'id': 'old', 'at': time.time() - updater.REQUEST_STALE_AFTER - 60})
        assert updater.pending_request(data_dir) is None
        assert updater.status(data_dir)['state'] == 'idle'

    def test_failed_run_is_surfaced(self, data_dir):
        beat(data_dir)
        watcher_status(data_dir, state='failed', exit_code=3,
                       finished_at='2026-01-01T00:00:00Z', message='boom')
        st = updater.status(data_dir)
        assert st['state'] == 'failed'
        assert st['exit_code'] == 3

    def test_corrupt_files_degrade_to_idle(self, data_dir):
        d = updater.update_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        for name in ('status.json', 'watch.json', 'request.json'):
            (d / name).write_text('{not json', encoding='utf-8')
        st = updater.status(data_dir)
        assert st['state'] == 'idle'
        assert st['watcher_online'] is False

    def test_unknown_state_normalizes(self, data_dir):
        watcher_status(data_dir, state='exploded')
        assert updater.status(data_dir)['state'] == 'idle'

    def test_log_tail_limits_and_survives_missing(self, data_dir):
        assert updater.log_tail(data_dir) == []
        d = updater.update_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / 'update.log').write_text(
            '\n'.join(f'line {i}' for i in range(500)), encoding='utf-8')
        tail = updater.log_tail(data_dir, limit=10)
        assert tail == [f'line {i}' for i in range(490, 500)]

    def test_log_tail_reads_only_the_end_of_a_huge_log(self, data_dir):
        d = updater.update_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / 'update.log').write_text('x' * (400 * 1024) + '\nlast line\n',
                                      encoding='utf-8')
        assert updater.log_tail(data_dir)[-1] == 'last line'


class TestUpdateApi:

    @pytest.fixture()
    def appmod(self, tmp_path, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_DATA_DIR', str(tmp_path / 'data'))
        monkeypatch.setenv('PROXYSHOP_OFFLINE', '1')
        import web.server.app as appmod
        yield importlib.reload(appmod)

    @pytest.fixture()
    def client(self, appmod):
        return TestClient(appmod.app)

    def test_settings_page_renders(self, client):
        res = client.get('/settings')
        assert res.status_code == 200
        assert 'Update &amp; restart' in res.text

    def test_status_endpoint(self, client):
        body = client.get('/api/update').json()
        assert body['state'] == 'idle'
        assert body['watcher_online'] is False
        assert 'log' in body and 'version' in body

    def test_request_rejected_without_watcher(self, client):
        """No watcher means the request would sit unread — say so, don't lie."""
        res = client.post('/api/update')
        assert res.status_code == 503
        assert 'nas-watch.sh' in res.json()['detail']

    def test_request_accepted_with_watcher(self, appmod, client):
        beat(appmod.DATA_DIR)
        res = client.post('/api/update')
        assert res.status_code == 200
        assert res.json()['state'] == 'requested'
        assert (updater.update_dir(appmod.DATA_DIR) / 'request.json').is_file()

    def test_request_rate_limited(self, appmod, client):
        beat(appmod.DATA_DIR)
        limit = appmod.RATE_LIMITS['update'][0]
        codes = [client.post('/api/update').status_code for _ in range(limit + 1)]
        assert codes[-1] == 429


class TestAssetVersion:
    """Static URLs carry a build token so a deploy can't be served new HTML
    against a browser's pre-deploy CSS. See updater.asset_version()."""

    def test_uses_build_commit_when_stamped(self, tmp_path, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_BUILD_COMMIT', 'f548bba1234567890')
        assert updater.asset_version(tmp_path) == 'f548bba12345'  # trimmed to 12

    def test_falls_back_to_asset_mtime_locally(self, tmp_path, monkeypatch):
        monkeypatch.delenv('PROXYSHOP_BUILD_COMMIT', raising=False)
        (tmp_path / 'app.css').write_text('a{}')
        (tmp_path / 'app.js').write_text('//')
        first = updater.asset_version(tmp_path)
        assert first.isdigit()
        # An edit must produce a new token, or a dev reload serves stale CSS.
        import os
        later = (tmp_path / 'app.css').stat().st_mtime + 60
        os.utime(tmp_path / 'app.css', (later, later))
        assert updater.asset_version(tmp_path) != first

    def test_survives_a_missing_static_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv('PROXYSHOP_BUILD_COMMIT', raising=False)
        assert updater.asset_version(tmp_path / 'nope') == 'dev'

    @pytest.fixture()
    def appmod(self, tmp_path, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_DATA_DIR', str(tmp_path / 'data'))
        monkeypatch.setenv('PROXYSHOP_OFFLINE', '1')
        monkeypatch.setenv('PROXYSHOP_BUILD_COMMIT', 'deadbeefcafe')
        import web.server.app as appmod
        yield importlib.reload(appmod)

    def test_pages_link_versioned_assets(self, appmod):
        """The whole point: the served HTML must not request a bare app.css."""
        html = TestClient(appmod.app).get('/').text
        assert '/static/app.css?v=deadbeefcafe' in html
        assert '/static/app.js?v=deadbeefcafe' in html
        assert '"/static/app.css"' not in html
