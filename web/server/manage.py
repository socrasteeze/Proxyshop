"""
* Server Management CLI
* Run inside the server container (or any host with web/ on the path):
*   python -m web.server.manage bulk-download
*   python -m web.server.manage bulk-import FILE
*   python -m web.server.manage cache-game --game riftbound
*   python -m web.server.manage cache-game --game riftbound --stop
*   python -m web.server.manage stats
"""
# Standard Library Imports
import argparse
import json
import os
import sys
from pathlib import Path

# Local Imports
from web.shared import games
from web.shared.carddb import CardDB
from web.shared.game_cache import (
    checkpoint_path, load_checkpoint, request_stop, reset_checkpoint, run_cache_game)

DATA_DIR = Path(os.environ.get('PROXYSHOP_DATA_DIR', 'data'))
IMAGES_DIR = DATA_DIR / 'images'
CACHE_RUNS_DIR = DATA_DIR / 'cache-runs'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='proxyshop-web-manage')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_dl = sub.add_parser('bulk-download', help='Download and import the latest Scryfall bulk data')
    p_dl.add_argument('--kind', default='default_cards',
                      choices=['default_cards', 'oracle_cards', 'all_cards'])
    p_dl.add_argument('--keep', action='store_true', help='Keep the downloaded JSON file')

    p_imp = sub.add_parser('bulk-import', help='Import a local Scryfall bulk data JSON file')
    p_imp.add_argument('file', type=Path)

    p_mtg = sub.add_parser(
        'mtgjson-prices',
        help='Download MTGJSON price data and update prices for cached cards')
    p_mtg.add_argument('--keep', action='store_true', help='Keep the downloaded JSON files')

    p_cache = sub.add_parser(
        'cache-game',
        help='Cache a small TCG catalog (+ HQ images) with stop/resume')
    p_cache.add_argument(
        '--game', required=True, choices=list(games.CATALOG_GAMES),
        help='TCG to cache (riftbound uses RiftScribe; union-arena uses apitcg)')
    p_cache.add_argument(
        '--stop', action='store_true',
        help='Ask a running cache-game for this TCG to stop after the current card')
    p_cache.add_argument(
        '--status', action='store_true',
        help='Show checkpoint progress for this TCG and exit')
    p_cache.add_argument(
        '--reset', action='store_true',
        help='Delete the checkpoint/stop flag for this TCG and exit')
    p_cache.add_argument(
        '--fresh', action='store_true',
        help='Ignore any previous checkpoint and start from the beginning')
    p_cache.add_argument(
        '--images-only', action='store_true',
        help='Only download missing images for cards already in the local DB')
    p_cache.add_argument(
        '--no-images', action='store_true',
        help='Store card JSON only; skip HQ image downloads')
    p_cache.add_argument(
        '--no-hydrate', action='store_true',
        help='Riftbound: skip per-card detail fetch (faster, thinner JSON)')
    p_cache.add_argument(
        '--kind', default='png', choices=['png', 'large'],
        help='Image kind to download (default: png → provider large scan)')
    p_cache.add_argument(
        '--page-size', type=int, default=50,
        help='Catalog page size (default 50)')

    sub.add_parser('stats', help='Show card DB statistics')

    args = parser.parse_args(argv)
    db = CardDB(DATA_DIR / 'cards.db')

    if args.cmd == 'bulk-download':
        print(f'Fetching latest {args.kind} bulk data from Scryfall…')
        path = db.download_bulk(DATA_DIR / 'bulk', kind=args.kind)
        print(f'Downloaded {path} ({path.stat().st_size >> 20}MB), importing…')
        count = db.import_bulk(path)
        print(f'Imported {count:,} cards.')
        if not args.keep:
            path.unlink(missing_ok=True)
        return 0

    if args.cmd == 'bulk-import':
        if not args.file.exists():
            print(f'No such file: {args.file}', file=sys.stderr)
            return 1
        count = db.import_bulk(args.file)
        print(f'Imported {count:,} cards.')
        return 0

    if args.cmd == 'mtgjson-prices':
        from web.shared import mtgjson
        bulk_dir = DATA_DIR / 'bulk'
        print('Downloading MTGJSON AllIdentifiers (large, may take a while)…')
        idents = mtgjson.download(mtgjson.URL_IDENTIFIERS, bulk_dir)
        print('Downloading MTGJSON AllPricesToday…')
        prices = mtgjson.download(mtgjson.URL_PRICES_TODAY, bulk_dir)
        print('Importing prices for cards in the local DB…')
        count = mtgjson.import_prices(db, idents, prices)
        print(f'Updated prices for {count:,} cards.')
        if not args.keep:
            idents.unlink(missing_ok=True)
            prices.unlink(missing_ok=True)
        return 0

    if args.cmd == 'cache-game':
        game = args.game
        if args.reset:
            reset_checkpoint(CACHE_RUNS_DIR, game)
            print(f'Reset checkpoint for {game}.')
            return 0
        if args.status:
            progress = load_checkpoint(checkpoint_path(CACHE_RUNS_DIR, game))
            if not progress:
                print(f'No checkpoint for {game}.')
                return 0
            print(json.dumps({
                'game': progress.game,
                'status': progress.status,
                'mode': progress.mode,
                'offset': progress.offset,
                'page': progress.page,
                'total_hint': progress.total_hint,
                'stored': progress.stored,
                'images_ok': progress.images_ok,
                'images_skip': progress.images_skip,
                'images_fail': progress.images_fail,
                'updated_at': progress.updated_at,
                'message': progress.message,
                'db_count': db.count_by_game(game),
            }, indent=2))
            return 0
        if args.stop:
            path = request_stop(CACHE_RUNS_DIR, game)
            print(f'Stop requested for {game} ({path}).')
            print('The running cache-game will exit after the current card.')
            return 0

        print(f'==> Caching {game} into {DATA_DIR} (images → {IMAGES_DIR})')
        print('    Rate-limited by default (safe for NAS IP). Stop with:')
        print('    docker exec proxyshop-web '
              f'python -m web.server.manage cache-game --game {game} --stop')
        print('    Or Ctrl+C in this terminal. Re-run this command to resume.')
        try:
            run_cache_game(
                db=db,
                game=game,
                images_dir=IMAGES_DIR,
                runs_dir=CACHE_RUNS_DIR,
                download_images=not args.no_images,
                hydrate=not args.no_hydrate,
                image_kind=args.kind,
                page_size=max(args.page_size, 1),
                images_only=args.images_only,
                fresh=args.fresh,
            )
        except (games.ProviderError, ValueError) as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return 1
        return 0

    if args.cmd == 'stats':
        for k, v in db.stats().items():
            print(f'{k}: {v}')
        for g in games.GAMES:
            if g == 'mtg':
                continue
            print(f'cards[{g}]: {db.count_by_game(g)}')
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
