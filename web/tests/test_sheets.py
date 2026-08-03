"""
* Image Cache & Proxy Sheet Tests — all offline.
"""
# Standard Library Imports
import io

# Third Party Imports
import pytest
from PIL import Image

# Local Imports
from web.shared import images, sheets
from web.tests.conftest import make_card


def _card_with_images(card_id='img-1', name='Lightning Bolt'):
    card = make_card(card_id, name, 'sta', '42')
    card['image_uris'] = {
        'png': f'https://cards.example/{card_id}.png',
        'art_crop': f'https://cards.example/{card_id}-art.jpg'}
    return card


def _png_bytes(size=(74, 104), color=(200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'PNG')
    return buf.getvalue()


class FakeSession:
    """Serves generated PNG bytes for any URL; counts requests."""

    def __init__(self):
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        payload = _png_bytes()

        class Res:
            status_code = 200
            def iter_content(self, chunk_size):
                yield payload
        return Res()


class BigFakeSession:
    """Like FakeSession but serves a full-resolution-sized fake scan, so
    thumbnail generation has something real to downscale."""

    def __init__(self):
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        payload = _png_bytes(size=(1490, 2080))

        class Res:
            status_code = 200
            def iter_content(self, chunk_size):
                yield payload
        return Res()


class TestImageUri:

    def test_front_face_fallback_for_dfc(self):
        card = make_card('dfc-1', 'Delver of Secrets // Insectile Aberration')
        card.pop('image_uris', None)
        card['card_faces'] = [
            {'image_uris': {'png': 'https://cards.example/front.png'}},
            {'image_uris': {'png': 'https://cards.example/back.png'}}]
        assert images.image_uri(card, 'png') == 'https://cards.example/front.png'

    def test_missing_kind_returns_none(self):
        assert images.image_uri(make_card('x', 'Opt'), 'png') is None


class TestEnsureImage:

    def test_downloads_once_then_cached(self, tmp_path):
        session = FakeSession()
        card = _card_with_images()
        p1 = images.ensure_image(session, card, 'png', tmp_path)
        p2 = images.ensure_image(session, card, 'png', tmp_path)
        assert p1 == p2 and p1.exists()
        assert session.calls == 1

    def test_offline_uncached_returns_none(self, tmp_path):
        session = FakeSession()
        assert images.ensure_image(
            session, _card_with_images(), 'png', tmp_path, offline=True) is None
        assert session.calls == 0

    def test_offline_cached_still_served(self, tmp_path):
        session = FakeSession()
        card = _card_with_images()
        images.ensure_image(session, card, 'png', tmp_path)
        path = images.ensure_image(session, card, 'png', tmp_path, offline=True)
        assert path is not None
        assert session.calls == 1

    def test_unknown_kind_raises(self, tmp_path):
        with pytest.raises(ValueError):
            images.ensure_image(FakeSession(), _card_with_images(), 'huge', tmp_path)

    def test_odd_uri_suffix_normalized(self, tmp_path):
        # URIs with strange suffixes get the kind's canonical extension so
        # deterministic cache lookups keep working.
        session = FakeSession()
        card = _card_with_images()
        card['image_uris']['png'] = 'https://cards.example/img.php?id=1'
        path = images.ensure_image(session, card, 'png', tmp_path)
        assert path is not None and path.suffix == '.png'
        assert images.ensure_image(session, card, 'png', tmp_path) == path
        assert session.calls == 1


class TestCachedImagePath:

    def test_finds_any_known_extension(self, tmp_path):
        (tmp_path / 'abc-png.webp').write_bytes(b'x')
        assert images.cached_image_path(tmp_path, 'abc', 'png') == tmp_path / 'abc-png.webp'

    def test_prefers_kind_extension(self, tmp_path):
        (tmp_path / 'abc-png.png').write_bytes(b'x')
        (tmp_path / 'abc-png.jpg').write_bytes(b'x')
        assert images.cached_image_path(tmp_path, 'abc', 'png') == tmp_path / 'abc-png.png'

    def test_ignores_partial_downloads(self, tmp_path):
        (tmp_path / 'abc-png.png.part').write_bytes(b'x')
        assert images.cached_image_path(tmp_path, 'abc', 'png') is None

    def test_missing_returns_none(self, tmp_path):
        assert images.cached_image_path(tmp_path, 'abc', 'png') is None
        assert images.cached_image_path(tmp_path, '', 'png') is None

    def test_large_falls_back_to_cached_png(self, tmp_path):
        # 'png' and 'large' are the same underlying scan at different
        # resolutions — a request for one should be served by a cache built
        # under the other rather than treated as a miss.
        (tmp_path / 'abc-png.png').write_bytes(b'x')
        assert images.cached_image_path(tmp_path, 'abc', 'large') == tmp_path / 'abc-png.png'

    def test_png_falls_back_to_cached_large(self, tmp_path):
        (tmp_path / 'abc-large.jpg').write_bytes(b'x')
        assert images.cached_image_path(tmp_path, 'abc', 'png') == tmp_path / 'abc-large.jpg'

    def test_own_kind_preferred_over_fallback(self, tmp_path):
        (tmp_path / 'abc-large.jpg').write_bytes(b'x')
        (tmp_path / 'abc-png.png').write_bytes(b'x')
        assert images.cached_image_path(tmp_path, 'abc', 'png') == tmp_path / 'abc-png.png'

    def test_art_crop_has_no_cross_kind_fallback(self, tmp_path):
        # art_crop/border_crop are genuinely different images from the full
        # scan — no fallback should apply to them.
        (tmp_path / 'abc-large.jpg').write_bytes(b'x')
        (tmp_path / 'abc-png.png').write_bytes(b'x')
        assert images.cached_image_path(tmp_path, 'abc', 'art_crop') is None


class TestEnsureImageKindFallback:
    """A cache built with one full-scan kind serves requests for the other
    without an extra network call — this is what fixes the gallery serving
    'kind=large' tiles against a cache the bulk downloader built as 'png'."""

    def test_large_request_served_from_cached_png(self, tmp_path):
        session = FakeSession()
        card = _card_with_images()
        images.ensure_image(session, card, 'png', tmp_path)
        assert session.calls == 1
        path = images.ensure_image(session, card, 'large', tmp_path)
        assert path is not None
        assert session.calls == 1  # no extra download

    def test_png_request_served_from_cached_large(self, tmp_path):
        session = FakeSession()
        card = _card_with_images()
        card['image_uris']['large'] = f'https://cards.example/{card["id"]}-large.jpg'
        images.ensure_image(session, card, 'large', tmp_path)
        assert session.calls == 1
        path = images.ensure_image(session, card, 'png', tmp_path)
        assert path is not None
        assert session.calls == 1


class TestEnsureThumb:

    def test_generates_webp_from_cached_scan(self, tmp_path):
        session = FakeSession()
        card = _card_with_images()
        images.ensure_image(session, card, 'png', tmp_path)
        assert session.calls == 1
        thumb = images.ensure_thumb(session, card, tmp_path)
        assert thumb is not None and thumb.suffix == '.webp' and thumb.exists()
        assert session.calls == 1  # derived locally, no extra download
        assert images.ensure_thumb(session, card, tmp_path) == thumb

    def test_downloads_source_when_uncached(self, tmp_path):
        session = FakeSession()
        card = _card_with_images()
        thumb = images.ensure_thumb(session, card, tmp_path)
        assert thumb is not None
        assert session.calls == 1  # one download for the source scan

    def test_offline_uncached_returns_none(self, tmp_path):
        session = FakeSession()
        card = _card_with_images()
        assert images.ensure_thumb(session, card, tmp_path, offline=True) is None
        assert session.calls == 0

    def test_smaller_than_source(self, tmp_path):
        card = _card_with_images()
        # A generously large fake scan so the resize actually shrinks it.
        big_session = BigFakeSession()
        source = images.ensure_image(big_session, card, 'png', tmp_path)
        thumb = images.ensure_thumb(big_session, card, tmp_path)
        assert thumb is not None
        assert thumb.stat().st_size < source.stat().st_size


class TestSheetPdf:

    def _images(self, tmp_path, n):
        paths = []
        for i in range(n):
            p = tmp_path / f'card{i}.png'
            p.write_bytes(_png_bytes())
            paths.append(p)
        return paths

    def test_nine_cards_one_page(self, tmp_path):
        out = tmp_path / 'sheet.pdf'
        pages = sheets.build_sheet_pdf(self._images(tmp_path, 9), out)
        assert pages == 1
        assert out.read_bytes()[:5] == b'%PDF-'

    def test_ten_cards_two_pages(self, tmp_path):
        out = tmp_path / 'sheet.pdf'
        assert sheets.build_sheet_pdf(self._images(tmp_path, 10), out, paper='a4') == 2

    def test_empty_raises(self, tmp_path):
        with pytest.raises(ValueError):
            sheets.build_sheet_pdf([], tmp_path / 'x.pdf')

    def test_bad_paper_raises(self, tmp_path):
        with pytest.raises(ValueError):
            sheets.build_sheet_pdf(self._images(tmp_path, 1), tmp_path / 'x.pdf', paper='legal')
