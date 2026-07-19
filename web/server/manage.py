"""
* Server Management CLI
* Run inside the server container (or any host with web/ on the path):
*   python -m web.server.manage bulk-download
*   python -m web.server.manage cache-game --game riftbound
*   python -m web.server.manage cache-game --game mtg --set mh3 --art showcase
*   python -m web.server.manage cache-game --game pokemon --set sv3 --type Fire
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
    checkpoint_path, load_checkpoint, progress_dict, request_stop,
    reset_checkpoint, run_cache_game)
from web.shared.images import IMAGE_KINDS

DATA_DIR = Path(os.environ.get('PROXYSHOP_DATA_DIR', 'data'))
IMAGES_DIR = DATA_DIR / 'images'
CACHE_RUNS_DIR = DATA_DIR / 'cache-runs'


def _filters_from_args(args) -> dict:
    return {
        'set': getattr(args, 'set_code', None),
        'type': getattr(args, 'type', None),
        'types': getattr(args, 'types', None),
        'rarity': getattr(args, 'rarity', None),
        'art': getattr(args, 'art', None),
        'artist': getattr(args, 'artist', None),
        'year': getattr(args, 'year', None),
        'tags': getattr(args, 'tags', None),
        'subtype': getattr(args, 'subtype', None),
        'supertype': getattr(args, 'supertype', None),
        'regulation': getattr(args, 'regulation', None),
        'name': getattr(args, 'name', None),
        'q': getattr(args, 'q', None),
    }


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
        help='Cache cards (+ HQ images) with stop/resume; MTG/Pokémon need filters')
    p_cache.add_argument(
        '--game', required=True, choices=list(games.CATALOG_GAMES),
        help='mtg/pokemon require filters; riftbound/union-arena can mirror all')
    p_cache.add_argument('--stop', action='store_true')
    p_cache.add_argument('--status', action='store_true')
    p_cache.add_argument('--reset', action='store_true')
    p_cache.add_argument('--fresh', action='store_true')
    p_cache.add_argument('--images-only', action='store_true')
    p_cache.add_argument('--no-images', action='store_true')
    p_cache.add_argument('--no-hydrate', action='store_true')
    p_cache.add_argument(
        '--kind', default='png', choices=sorted(IMAGE_KINDS),
        help='Image kind (mtg also supports art_crop)')
    p_cache.add_argument('--page-size', type=int, default=50)
    # Selective filters
    p_cache.add_argument('--set', dest='set_code', help='Set code (mh3, sv3, …)')
    p_cache.add_argument('--type', help='MTG type line fragment or Pokémon type')
    p_cache.add_argument('--types', help='Pokémon types CSV')
    p_cache.add_argument('--rarity', help='Rarity filter')
    p_cache.add_argument(
        '--art', help='MTG art flags CSV: showcase,borderless,extended,fullart,…')
    p_cache.add_argument('--artist', help='MTG artist name')
    p_cache.add_argument('--year', help='MTG release year')
    p_cache.add_argument('--tags', help='Extra Scryfall fragments (otag:, is:, …)')
    p_cache.add_argument('--subtype', help='Pokémon subtype (V, EX, …)')
    p_cache.add_argument('--supertype', help='Pokémon / Trainer / Energy')
    p_cache.add_argument('--regulation', help='Pokémon regulation mark (G, H, …)')
    p_cache.add_argument('--name', help='Pokémon name substring')
    p_cache.add_argument('--q', help='Raw provider query (advanced)')

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
            print(json.dumps(
                progress_dict(progress, db_count=db.count_by_game(game)),
                indent=2))
            return 0
        if args.stop:
            path = request_stop(CACHE_RUNS_DIR, game)
            print(f'Stop requested for {game} ({path}).')
            print('The running cache-game will exit after the current card.')
            return 0

        filters = _filters_from_args(args)
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
                filters=filters,
            )
        except (games.ProviderError, ValueError, RuntimeError) as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return 1
        return 0

    if args.cmd == 'stats':
        for k, v in db.stats().items():
            print(f'{k}: {v}')
        for g in games.GAMES:
            print(f'cards[{g}]: {db.count_by_game(g)}')
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
