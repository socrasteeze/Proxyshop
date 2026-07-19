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
