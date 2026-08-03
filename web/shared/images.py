"""
* Card Image Fetching & Caching
* Downloads high-quality card images (full scans and art crops) from the
* URIs embedded in cached Scryfall card objects, storing them on disk so
* each image is fetched at most once.
* Must never import from `src/`.
"""
# Standard Library Imports
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Third Party Imports
import requests
from PIL import Image

# Local Imports
from web.shared.carddb import ScryfallSession

# Image kind -> fallback file extension (Scryfall serves png for 'png', jpg otherwise)
IMAGE_KINDS = {
    'png': '.png',           # 745x1040 hi-res full card scan
    'large': '.jpg',         # 672x936 full card scan
    'art_crop': '.jpg',      # artwork only — ideal input for the renderer
    'border_crop': '.jpg',
    'thumb': '.webp',        # derived locally, never fetched — see ensure_thumb()
}

# Extensions providers actually serve; checked in preference order so cache
# lookups are a handful of stat() calls instead of scanning the whole dir.
_CACHE_EXTS = ('.png', '.jpg', '.jpeg', '.webp')

# 'png' and 'large' are both full-card scans of the same art, just different
# resolution/format (for non-MTG games they're literally the same source
# URL, see image_uri()). A cache built with one kind (e.g. the bulk
# downloader's default 'png') would otherwise be an unnecessary miss — and a
# fresh live download — for a page that asks for the other (e.g. the
# gallery's 'large'), even though a perfectly usable scan is already on
# disk.
_FULL_SCAN_KINDS = ('png', 'large')

# Target width for generated thumbnails (gallery grid tiles render well
# under this; still crisp on a retina display at typical grid sizes).
_THUMB_WIDTH = 336


def cached_image_path(dest_dir: Path, card_id: str, kind: str) -> Optional[Path]:
    """Return the already-downloaded image for a card/kind, if any.

    For 'png'/'large' this also accepts a file cached under the other of the
    two — same underlying scan, so it's not worth a second download just
    because the caller asked for the other kind.
    """
    if not card_id:
        return None
    kinds = (
        (kind,) + tuple(k for k in _FULL_SCAN_KINDS if k != kind)
        if kind in _FULL_SCAN_KINDS else (kind,))
    for k in kinds:
        preferred = IMAGE_KINDS.get(k)
        exts = ((preferred,) if preferred else ()) + tuple(
            e for e in _CACHE_EXTS if e != preferred)
        for ext in exts:
            p = dest_dir / f'{card_id}-{k}{ext}'
            if p.is_file():
                return p
    return None


def image_uri(card: dict, kind: str) -> Optional[str]:
    """Resolve an image URI from a cached card object.

    MTG cards use Scryfall's image_uris (front face for DFCs). Other games
    (pokemon, union-arena, riftbound) carry a normalized images block where
    'large' is the highest quality available — 'png'/'large' both map to it.
    """
    if card.get('game', 'mtg') != 'mtg':
        images = card.get('images') or {}
        if kind in ('png', 'large', 'border_crop'):
            return images.get('large') or images.get('small')
        return None  # no art crops outside MTG
    uris = card.get('image_uris')
    if not uris and card.get('card_faces'):
        uris = (card['card_faces'][0] or {}).get('image_uris')
    return (uris or {}).get(kind)


def ensure_image(
    session: ScryfallSession,
    card: dict,
    kind: str,
    dest_dir: Path,
    offline: bool = False
) -> Optional[Path]:
    """Return a local path for a card image, downloading it once if needed.

    Args:
        session: Throttled Scryfall session (image CDN gets the same courtesy).
        card: Cached Scryfall card object.
        kind: One of IMAGE_KINDS.
        dest_dir: Image cache directory.
        offline: When True, only return already-cached files.

    Returns:
        Path to the image, or None when unavailable.
    """
    if kind not in IMAGE_KINDS:
        raise ValueError(f'Unknown image kind {kind!r}')
    card_id = card.get('id')
    if not card_id:
        return None
    # Cached under any known extension (providers serve png/jpg/webp variously)
    cached = cached_image_path(dest_dir, card_id, kind)
    if cached:
        return cached
    if offline:
        return None
    uri = image_uri(card, kind)
    if not uri:
        return None
    ext = Path(urlparse(uri).path).suffix.lower()
    if ext not in _CACHE_EXTS:
        # Normalize odd/missing URI suffixes so cache lookups stay deterministic.
        ext = IMAGE_KINDS[kind]
    path = dest_dir / f'{card_id}-{kind}{ext}'
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.part')
    try:
        res = session.get(uri, stream=True)
        if res.status_code != 200:
            return None
        with open(tmp, 'wb') as f:
            for chunk in res.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.rename(path)
        return path
    except requests.RequestException:
        # Transient network error mid-image: leave it uncached (counted as a
        # failure by the caller) rather than crashing the whole catalog run.
        tmp.unlink(missing_ok=True)
        return None


def ensure_thumb(
    session: ScryfallSession,
    card: dict,
    dest_dir: Path,
    offline: bool = False,
) -> Optional[Path]:
    """Return a small WebP thumbnail for a card, generating it once.

    Unlike ensure_image(), 'thumb' has no provider URI — it's always derived
    locally by downscaling whichever full-card scan is already cached (or,
    if allowed, freshly downloaded via ensure_image). This is what lets
    gallery grids ship a ~20-30KB tile instead of the full 672x936+ scan.

    Returns:
        Path to the thumbnail, or None when no source scan is available or
        Pillow can't decode it.
    """
    card_id = card.get('id')
    if not card_id:
        return None
    cached = cached_image_path(dest_dir, card_id, 'thumb')
    if cached:
        return cached
    # No separate offline early-exit here: a thumb should still be derivable
    # from an already-cached full scan while offline — ensure_image's own
    # offline flag already limits this to cache hits, only allowing a fresh
    # network download when offline=False.
    # Prefer 'large' (smaller download); fall back to 'png' if a provider/
    # card only exposes one of the two full-scan URIs.
    source = (
        ensure_image(session, card, 'large', dest_dir, offline=offline)
        or ensure_image(session, card, 'png', dest_dir, offline=offline))
    if not source:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f'{card_id}-thumb.webp'
    tmp = path.with_suffix(path.suffix + '.part')
    try:
        with Image.open(source) as im:
            im = im.convert('RGB')
            w, h = im.size
            if w > _THUMB_WIDTH:
                im = im.resize(
                    (_THUMB_WIDTH, round(h * _THUMB_WIDTH / w)), Image.LANCZOS)
            im.save(tmp, 'WEBP', quality=80, method=4)
        tmp.rename(path)
        return path
    except Exception:
        # Corrupt/unreadable source image, unsupported format, disk error…
        # fall back to the full scan rather than breaking the tile.
        tmp.unlink(missing_ok=True)
        return None
