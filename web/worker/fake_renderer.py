"""
* Fake Renderer (Linux/dev stub)
* Emits a placeholder PNG so the full server/worker job lifecycle can be
* exercised end-to-end without Windows or Photoshop.
"""
# Standard Library Imports
import struct
import time
import zlib
from pathlib import Path
from typing import Optional

# Local Imports
from web.shared.schema import Capabilities, Job, TemplateInfo

"""
* Placeholder PNG Generation (no dependencies)
"""


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack('>I', len(data)) + kind + data
            + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))


def make_placeholder_png(path: Path, width: int = 375, height: int = 523) -> None:
    """Write a card-proportioned solid-color PNG with a border."""
    border, inner, edge = (124, 92, 255), (30, 32, 39), 12
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # no filter
        for x in range(width):
            c = border if (
                x < edge or y < edge or x >= width - edge or y >= height - edge
            ) else inner
            rows.extend(c)
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    png = (b'\x89PNG\r\n\x1a\n'
           + _png_chunk(b'IHDR', header)
           + _png_chunk(b'IDAT', zlib.compress(bytes(rows), 6))
           + _png_chunk(b'IEND', b''))
    path.write_bytes(png)


"""
* Renderer Interface (mirrors web.worker.renderer)
"""


def get_capabilities(worker_name: str) -> Capabilities:
    """Fake template map for development."""
    return Capabilities(
        worker_name=worker_name,
        proxyshop_version='fake-0.0',
        templates={
            'normal': [
                TemplateInfo(name='Normal (Fake)', class_name='FakeTemplate'),
                TemplateInfo(name='Extended (Fake)', class_name='FakeExtendedTemplate'),
            ],
            'pokemon': [
                TemplateInfo(name='Pokémon Normal (Fake)', class_name='FakePokemonTemplate',
                             installed=True),
            ],
        },
        games=['mtg', 'pokemon'])


def render(job: Job, art_path: Path, out_dir: Path) -> tuple[bool, Optional[Path], str, Optional[str]]:
    """Pretend to render: sleep briefly, emit a placeholder PNG.

    Returns:
        (ok, result_path, log, error) — same contract as the real renderer.
    """
    game = getattr(job, 'game', None) or 'mtg'
    log = [
        f'[fake] game={game}',
        f'[fake] rendering {job.card_name!r} with template {job.template_name or "default"}',
    ]
    time.sleep(1.5)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = out_dir / f'{job.id}.png'
    make_placeholder_png(result)
    log.append(f'[fake] wrote {result.name}')
    return True, result, '\n'.join(log), None
