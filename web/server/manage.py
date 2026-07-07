"""
* Server Management CLI
* Run inside the server container (or any host with web/ on the path):
*   python -m web.server.manage bulk-download        # fetch + import nightly bulk data
*   python -m web.server.manage bulk-import FILE     # import an already-downloaded file
*   python -m web.server.manage stats
"""
# Standard Library Imports
import argparse
import os
import sys
from pathlib import Path

# Local Imports
from web.shared.carddb import CardDB

DATA_DIR = Path(os.environ.get('PROXYSHOP_DATA_DIR', 'data'))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='proxyshop-web-manage')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_dl = sub.add_parser('bulk-download', help='Download and import the latest Scryfall bulk data')
    p_dl.add_argument('--kind', default='default_cards',
                      choices=['default_cards', 'oracle_cards', 'all_cards'])
    p_dl.add_argument('--keep', action='store_true', help='Keep the downloaded JSON file')

    p_imp = sub.add_parser('bulk-import', help='Import a local Scryfall bulk data JSON file')
    p_imp.add_argument('file', type=Path)

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

    if args.cmd == 'stats':
        for k, v in db.stats().items():
            print(f'{k}: {v}')
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
