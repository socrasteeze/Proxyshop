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

# Local Imports
from web.shared.carddb import ScryfallSession

# Image kind -> fallback file extension (Scryfall serves png for 'png', jpg otherwise)
IMAGE_KINDS = {
    'png': '.png',           # 745x1040 hi-res full card scan
    'large': '.jpg',         # 672x936 full card scan
    'art_crop': '.jpg',      # artwork only — ideal input for the renderer
    'border_crop': '.jpg',
}


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
    # Cached under any extension (providers serve png/jpg/webp variously)
    cached = sorted(dest_dir.glob(f'{card_id}-{kind}.*'))
    cached = [p for p in cached if not p.name.endswith('.part')]
    if cached:
        return cached[0]
    if offline:
        return None
    uri = image_uri(card, kind)
    if not uri:
        return None
    ext = Path(urlparse(uri).path).suffix.lower() or IMAGE_KINDS[kind]
    path = dest_dir / f'{card_id}-{kind}{ext}'
    dest_dir.mkdir(parents=True, exist_ok=True)
    res = session.get(uri, stream=True)
    if res.status_code != 200:
        return None
    tmp = path.with_suffix(path.suffix + '.part')
    with open(tmp, 'wb') as f:
        for chunk in res.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    tmp.rename(path)
    return path
