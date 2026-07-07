"""
* Photoshop Watchdog
* Runs a render with a hard timeout and handles stuck/crashed Photoshop by
* killing and letting the next render relaunch it. Windows-only side effects,
* but importable anywhere (used by the daemon with the fake renderer too).
"""
# Standard Library Imports
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

# Local Imports
from web.shared.schema import Job

# Hard per-job ceiling — COM calls can hang indefinitely.
RENDER_TIMEOUT_SECONDS = 10 * 60

# Proactively restart Photoshop after this many renders (memory creep).
RESTART_EVERY_N_JOBS = 25


def kill_photoshop() -> None:
    """Force-kill Photoshop so the next COM connection relaunches it fresh."""
    if sys.platform != 'win32':
        return
    subprocess.run(
        ['taskkill', '/F', '/IM', 'Photoshop.exe'],
        capture_output=True, check=False)


class RenderWatchdog:
    """Wraps a renderer's render() with timeout + restart policy."""

    def __init__(
        self,
        render_fn: Callable[[Job, Path, Path], tuple[bool, Optional[Path], str, Optional[str]]],
        timeout: int = RENDER_TIMEOUT_SECONDS,
        restart_every: int = RESTART_EVERY_N_JOBS
    ):
        self.render_fn = render_fn
        self.timeout = timeout
        self.restart_every = restart_every
        self.jobs_since_restart = 0

    def run(self, job: Job, art_path: Path, out_dir: Path) -> tuple[bool, Optional[Path], str, Optional[str]]:
        """Run one render under a timeout; kill Photoshop if it hangs.

        Note: the render thread itself can't be force-stopped from Python —
        killing Photoshop makes its blocking COM call fail, which unblocks it.
        """
        result: list = [None]

        def target():
            result[0] = self.render_fn(job, art_path, out_dir)

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(self.timeout)

        if t.is_alive():
            kill_photoshop()
            t.join(30)
            self.jobs_since_restart = 0
            return False, None, '', f'Render timed out after {self.timeout}s — Photoshop was restarted'

        self.jobs_since_restart += 1
        if self.jobs_since_restart >= self.restart_every:
            kill_photoshop()
            self.jobs_since_restart = 0

        if result[0] is None:
            return False, None, '', 'Render thread died without a result'
        return result[0]
