"""
* Render Worker Daemon
* Long-lived process that claims jobs from the NAS server over outbound HTTPS,
* renders them, and uploads results. On Windows it drives real Photoshop; with
* --fake (or PROXYSHOP_FAKE_RENDER=1) it emits placeholder PNGs for dev/testing.

Usage (from the Proxyshop repo root):
    python -m web.worker.daemon --server https://nas:8000 --token <token>
    python -m web.worker.daemon --server http://localhost:8000 --fake

Environment fallbacks:
    PROXYSHOP_SERVER_URL, PROXYSHOP_WORKER_TOKEN, PROXYSHOP_WORKER_NAME,
    PROXYSHOP_FAKE_RENDER
"""
# Standard Library Imports
import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Third Party Imports
import requests

# Local Imports
from web.shared.schema import Job, JobStatus
from web.worker.ps_watchdog import RenderWatchdog

HEARTBEAT_SECONDS = 30
POLL_WAIT_SECONDS = 25
BACKOFF_MAX = 60


class WorkerClient:
    """HTTP client for the server's /api/worker endpoints."""

    def __init__(self, server: str, token: str, name: str):
        self.base = server.rstrip('/')
        self.name = name
        self.session = requests.Session()
        self.session.headers['Authorization'] = f'Bearer {token}'

    def hello(self, capabilities) -> None:
        self.session.post(
            f'{self.base}/api/worker/hello',
            json=capabilities.model_dump(), timeout=30).raise_for_status()

    def heartbeat(self) -> None:
        self.session.post(
            f'{self.base}/api/worker/heartbeat',
            params={'worker': self.name}, timeout=30).raise_for_status()

    def next_job(self) -> Job | None:
        res = self.session.get(
            f'{self.base}/api/worker/jobs/next',
            params={'worker': self.name, 'wait': POLL_WAIT_SECONDS},
            timeout=POLL_WAIT_SECONDS + 15)
        if res.status_code == 204:
            return None
        res.raise_for_status()
        return Job(**res.json())

    def download_art(self, job: Job, dest: Path) -> Path:
        res = self.session.get(
            f'{self.base}/api/worker/jobs/{job.id}/art', timeout=120, stream=True)
        res.raise_for_status()
        suffix = Path(job.art_filename or 'art.png').suffix or '.png'
        # Name the file with Proxyshop tags so parse_card_info/assign_layout work
        name = job.card_name
        if job.set_code:
            name += f' [{job.set_code.upper()}]'
        if job.collector_number:
            name += f' {{{job.collector_number}}}'
        path = dest / f'{name}{suffix}'
        with open(path, 'wb') as f:
            for chunk in res.iter_content(1 << 20):
                f.write(chunk)
        return path

    def set_status(self, job: Job, status: JobStatus) -> None:
        self.session.post(
            f'{self.base}/api/worker/jobs/{job.id}/status',
            params={'status': status.value}, timeout=30).raise_for_status()

    def report(self, job: Job, ok: bool, result_path: Path | None,
               log: str, error: str | None) -> None:
        data = {'ok': str(ok).lower(), 'log': log or ''}
        if error:
            data['error'] = error
        files = {}
        if ok and result_path:
            files['result'] = (result_path.name, open(result_path, 'rb'), 'image/png')
        try:
            self.session.post(
                f'{self.base}/api/worker/jobs/{job.id}/result',
                data=data, files=files or None, timeout=300).raise_for_status()
        finally:
            for _, (_, fh, _) in files.items():
                fh.close()


def heartbeat_loop(client: WorkerClient, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            client.heartbeat()
        except requests.RequestException:
            pass  # transient — the main loop's backoff handles real outages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='proxyshop-worker')
    parser.add_argument('--server', default=os.environ.get('PROXYSHOP_SERVER_URL', 'http://localhost:8000'))
    parser.add_argument('--token', default=os.environ.get('PROXYSHOP_WORKER_TOKEN', 'dev-token'))
    parser.add_argument('--name', default=os.environ.get('PROXYSHOP_WORKER_NAME', 'worker'))
    parser.add_argument('--fake', action='store_true',
                        default=os.environ.get('PROXYSHOP_FAKE_RENDER', '0') == '1',
                        help='Use the fake renderer (no Photoshop needed)')
    parser.add_argument('--once', action='store_true', help='Process at most one job, then exit')
    args = parser.parse_args(argv)

    # Select renderer: fake (any OS) or real (Windows, imports Proxyshop)
    if args.fake:
        from web.worker import fake_renderer as renderer
    else:
        if sys.platform != 'win32':
            print('Real rendering requires Windows + Photoshop. Use --fake for development.',
                  file=sys.stderr)
            return 2
        from web.worker import renderer  # noqa — imports the full Proxyshop stack

    client = WorkerClient(args.server, args.token, args.name)
    watchdog = RenderWatchdog(renderer.render)
    workdir = Path(tempfile.mkdtemp(prefix='proxyshop-worker-'))

    # Handshake with retry — the server may not be up yet
    backoff = 2
    while True:
        try:
            client.hello(renderer.get_capabilities(args.name))
            print(f'Connected to {args.server} as {args.name!r}')
            break
        except requests.RequestException as e:
            print(f'Server unreachable ({e}); retrying in {backoff}s')
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(client, stop), daemon=True).start()

    backoff = 2
    try:
        while True:
            try:
                job = client.next_job()
                backoff = 2
            except requests.RequestException as e:
                print(f'Poll failed ({e}); retrying in {backoff}s')
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            if job is None:
                continue

            print(f'Claimed job {job.id}: {job.card_name}')
            job_dir = workdir / job.id
            job_dir.mkdir(parents=True, exist_ok=True)
            try:
                art = client.download_art(job, job_dir)
                client.set_status(job, JobStatus.RENDERING)
                ok, result, log, error = watchdog.run(job, art, job_dir)
                client.report(job, ok, result, log, error)
                print(f'Job {job.id}: {"done" if ok else f"failed ({error})"}')
            except requests.RequestException as e:
                print(f'Job {job.id} I/O error: {e}')
                try:
                    client.report(job, False, None, '', f'Worker I/O error: {e}')
                except requests.RequestException:
                    pass  # lease expiry will requeue it
            if args.once:
                return 0
    except KeyboardInterrupt:
        print('Shutting down.')
        return 0
    finally:
        stop.set()


if __name__ == '__main__':
    raise SystemExit(main())
