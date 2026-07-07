"""
* Fake Renderer Tests — placeholder PNG validity and renderer contract.
"""
# Standard Library Imports
import struct
import zlib

# Local Imports
from web.shared.schema import Job
from web.worker.fake_renderer import get_capabilities, make_placeholder_png, render


def test_placeholder_png_is_valid(tmp_path):
    path = tmp_path / 'card.png'
    make_placeholder_png(path, width=30, height=42)
    data = path.read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    # IHDR dimensions match
    w, h = struct.unpack('>II', data[16:24])
    assert (w, h) == (30, 42)
    # IDAT decompresses to the right size: h rows of (1 filter byte + w*3 rgb)
    idat_start = data.index(b'IDAT') + 4
    idat_len = struct.unpack('>I', data[data.index(b'IDAT') - 4:data.index(b'IDAT')])[0]
    raw = zlib.decompress(data[idat_start:idat_start + idat_len])
    assert len(raw) == 42 * (1 + 30 * 3)


def test_render_contract(tmp_path):
    job = Job(id='job-1', card_name='Lightning Bolt')
    ok, result, log, error = render(job, tmp_path / 'art.png', tmp_path / 'out')
    assert ok is True
    assert result.exists()
    assert result.name == 'job-1.png'
    assert 'Lightning Bolt' in log
    assert error is None


def test_capabilities_shape():
    caps = get_capabilities('dev')
    assert caps.worker_name == 'dev'
    assert 'normal' in caps.templates
    assert caps.templates['normal'][0].installed
