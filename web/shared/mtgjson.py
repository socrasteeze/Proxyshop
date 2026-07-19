"""
* MTGJSON Price Importer
* Enriches the local card database with aggregated paper prices from
* MTGJSON (https://mtgjson.com): TCGplayer retail (USD) and Cardmarket
* retail (EUR).
*
* MTGJSON files are large single-line JSON documents, so everything here is
* stream-parsed with ijson to stay friendly to low-RAM NAS hardware.
* Must never import from `src/`.
"""
# Standard Library Imports
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

# Third Party Imports
import ijson
import requests

# Local Imports
from web.shared.carddb import CardDB, HEADERS

MTGJSON_API = 'https://mtgjson.com/api/v5'
URL_IDENTIFIERS = f'{MTGJSON_API}/AllIdentifiers.json'
URL_PRICES_TODAY = f'{MTGJSON_API}/AllPricesToday.json'


def download(url: str, dest: Path) -> Path:
    """Stream-download an MTGJSON file to dest dir. Returns the file path."""
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / url.rsplit('/', 1)[-1]
    with requests.get(url, headers=HEADERS, stream=True, timeout=60) as res:
        res.raise_for_status()
        with open(target, 'wb') as f:
            for chunk in res.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return target


def _latest(price_history: Optional[dict]) -> Optional[float]:
    """MTGJSON retail blocks map date -> price; take the most recent date."""
    if not price_history:
        return None
    try:
        value = price_history[max(price_history)]
        return float(value) if isinstance(value, (int, float, str, Decimal)) else None
    except (ValueError, TypeError):
        return None


def build_uuid_map(db: CardDB, identifiers_path: Path) -> dict[str, str]:
    """Map MTGJSON uuid -> Scryfall id, restricted to cards already in our DB.

    Restricting to known Scryfall ids keeps the map small even though
    AllIdentifiers covers every printing in existence.
    """
    known = {
        row['id'] for row in
        db._conn().execute('SELECT id FROM cards').fetchall()}
    uuid_map: dict[str, str] = {}
    with open(identifiers_path, 'rb') as f:
        for uuid, card in ijson.kvitems(f, 'data'):
            idents = card.get('identifiers') or {}
            sid = idents.get('scryfallId')
            if sid and sid in known:
                uuid_map[uuid] = sid
    return uuid_map


def import_prices(db: CardDB, identifiers_path: Path, prices_path: Path) -> int:
    """Import today's MTGJSON paper prices for cards present in the local DB.

    Returns:
        Number of cards whose price row was updated.
    """
    uuid_map = build_uuid_map(db, identifiers_path)
    count = 0
    con = db._conn()
    with open(prices_path, 'rb') as f:
        for uuid, entry in ijson.kvitems(f, 'data'):
            sid = uuid_map.get(uuid)
            if not sid:
                continue
            paper = entry.get('paper') or {}
            tcg = (paper.get('tcgplayer') or {}).get('retail') or {}
            cm = (paper.get('cardmarket') or {}).get('retail') or {}
            usd = _latest(tcg.get('normal'))
            usd_foil = _latest(tcg.get('foil'))
            eur = _latest(cm.get('normal'))
            if usd is None and usd_foil is None and eur is None:
                continue
            db.set_price(sid, usd=usd, usd_foil=usd_foil, eur=eur,
                         source='mtgjson', commit=False)
            count += 1
            if count % 5000 == 0:
                con.commit()
    con.commit()
    if count:
        db.set_meta(
            'mtgjson_prices_at',
            time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    return count
