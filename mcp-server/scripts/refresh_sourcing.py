"""Resolve LCSC codes and prices for catalog parts from the public jlcparts database (yaqwsx/jlcparts, MIT).

  python scripts/refresh_sourcing.py --download DIR     # fetch + unpack cache.sqlite3 (about 230 MB download, 1.7 GB on disk)
  python scripts/refresh_sourcing.py --db DIR/cache.sqlite3                 # rebuild catalog/sourcing.json
  python scripts/refresh_sourcing.py --db DIR/cache.sqlite3 --find 'LM358%' [--package SOIC-8]   # explore candidates

catalog/sourcing_queries.json says how to find each part (MPN patterns and a package substring). The result is
catalog/sourcing.json, an overlay that load_catalog() applies on top of both catalog tiers. Prices are a snapshot
of JLCPCB list prices in USD at the database date; they are estimates, not quotes.
"""
import argparse
import json
import os
import sqlite3
import struct
import sys
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = 'https://yaqwsx.github.io/jlcparts/data/'
MIN_STOCK = 100


def download(directory: Path) -> Path:
    """Fetch the split cache.zip and stream-inflate its single cache.sqlite3 member.

    The archive is a multi-volume zip (cache.z01.. then cache.zip). Info-ZIP cannot always join it, so the
    first local file header is parsed directly and its deflate stream decoded across the volumes.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()) and not (directory / 'cache.sqlite3').exists():
        raise SystemExit(f'{directory} is not empty; pass an empty directory for downloaded data')
    volumes = []
    for index in range(1, 100):
        name = f'cache.z{index:02d}'
        try:
            urllib.request.urlopen(urllib.request.Request(BASE_URL + name, method='HEAD'), timeout=30).close()
        except OSError:
            break
        volumes.append(name)
    volumes.append('cache.zip')
    for name in volumes:
        target = directory / name
        if not target.exists():
            print(f'downloading {name} ...', flush=True)
            urllib.request.urlretrieve(BASE_URL + name, target)
    paths = [directory / name for name in volumes]
    state = {'i': 0, 'f': open(paths[0], 'rb')}

    def read(n):
        data = state['f'].read(n)
        while len(data) < n and state['i'] < len(paths) - 1:
            state['i'] += 1
            state['f'] = open(paths[state['i']], 'rb')
            data += state['f'].read(n - len(data))
        return data
    if read(4) != b'PK\x07\x08':
        raise SystemExit('unexpected archive layout (no split-archive marker)')
    sig, _v, _f, method, _t, _d, _crc, _cs, _us, name_len, extra_len = struct.unpack('<IHHHHHIIIHH', read(30))
    member = read(name_len).decode()
    read(extra_len)
    if sig != 0x04034B50 or method != 8 or member != 'cache.sqlite3':
        raise SystemExit(f'unexpected archive member {member!r}')
    inflater = zlib.decompressobj(-15)
    out = directory / 'cache.sqlite3'
    with open(out, 'wb') as stream:
        while not inflater.eof:
            block = read(1 << 20)
            if not block:
                raise SystemExit('archive ended early')
            stream.write(inflater.decompress(block))
    for path in paths:
        path.unlink()
    print(f'wrote {out}')
    return out


def connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def parse_tiers(text: str) -> list[dict]:
    """'1-49:0.2094,50-149:0.1633,5000-:0.1117' -> [{'qty': 1, 'price_usd': 0.2094}, ...]"""
    tiers = []
    for piece in text.split(','):
        quantity, _, price = piece.partition(':')
        try:
            tiers.append({'qty': int(quantity.split('-')[0]), 'price_usd': float(price)})
        except ValueError:
            continue
    return tiers


def candidates(db, patterns, package=None, manufacturer=None, limit=50):
    rows = []
    for rank, pattern in enumerate(patterns):
        query = ('select lcsc, mfr, package, manufacturer, library_type, preferred, stock, price, fetched_at, datasheet, description '
                 'from jlc_components where present=1 and stock>=? and mfr like ?')
        args = [MIN_STOCK, pattern]
        if package:
            query += ' and package like ?'
            args.append(f'%{package}%')
        if manufacturer:
            query += ' and manufacturer like ?'
            args.append(f'%{manufacturer}%')
        for row in db.execute(query + ' limit 500', args):
            tiers = parse_tiers(row[7])
            if tiers:
                rows.append({'rank': rank, 'lcsc': f'C{row[0]}', 'mpn': row[1], 'package': row[2], 'manufacturer': row[3],
                             'library_type': row[4], 'preferred': bool(row[5]), 'stock': row[6], 'price_tiers': tiers,
                             'fetched_at': row[8], 'datasheet': row[9], 'description': row[10]})
    # JLCPCB "basic" parts avoid the per-feeder assembly fee, so they sort first; then preferred, query order,
    # stock depth and finally the lowest quantity-1 price.
    rows.sort(key=lambda r: (r['library_type'] != 'base', not r['preferred'], r['rank'], -min(r['stock'], 10**6), r['price_tiers'][0]['price_usd']))
    return rows[:limit]


def resolve(db, query):
    rows = candidates(db, query['mpn'], query.get('package'), query.get('manufacturer'))
    if query.get('lcsc'):
        rows = [r for r in rows if r['lcsc'] == query['lcsc']] or rows
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--download', metavar='DIR')
    parser.add_argument('--db')
    parser.add_argument('--queries', default=str(ROOT / 'catalog' / 'sourcing_queries.json'))
    parser.add_argument('--out', default=str(ROOT / 'catalog' / 'sourcing.json'))
    parser.add_argument('--find', metavar='MPN_LIKE')
    parser.add_argument('--package')
    args = parser.parse_args()
    if args.download:
        path = download(Path(args.download))
        args.db = args.db or str(path)
    if not args.db:
        parser.error('--db (or --download) is required')
    db = connect(args.db)
    if args.find:
        for r in candidates(db, [args.find], args.package, limit=15):
            print(f"{r['lcsc']:>10} {r['library_type']:6} {r['mpn'][:28]:28} {r['package'][:18]:18} {r['manufacturer'][:26]:26} stock={r['stock']:>8} ${r['price_tiers'][0]['price_usd']}")
        return 0
    queries = json.loads(Path(args.queries).read_text())
    result, missing, newest = {}, [], 0
    for part_id, query in sorted(queries.items()):
        if part_id.startswith('_'):
            continue
        rows = resolve(db, query)
        if not rows:
            missing.append(part_id)
            continue
        best = rows[0]
        newest = max(newest, best['fetched_at'])
        result[part_id] = {
            'lcsc': best['lcsc'], 'price_usd': best['price_tiers'][0]['price_usd'],
            'sourcing': {'source': 'jlcparts', 'mpn': best['mpn'], 'manufacturer': best['manufacturer'], 'package': best['package'],
                         'library_type': best['library_type'], 'stock': best['stock'], 'price_tiers': best['price_tiers'][:6],
                         'datasheet': best['datasheet'],
                         'alternates': [{'lcsc': r['lcsc'], 'mpn': r['mpn'], 'manufacturer': r['manufacturer'], 'price_usd': r['price_tiers'][0]['price_usd']}
                                        for r in rows[1:4]]}}
    # Existing catalog codes with no query must at least exist in the database; otherwise they are cleared.
    for name in ('curated_parts.json', 'imported_parts.json'):
        path = ROOT / 'catalog' / name
        for part_id, part in (json.loads(path.read_text()) if path.exists() else {}).items():
            code = part.get('lcsc')
            if not code or part_id in result or part_id in queries:
                continue
            row = db.execute('select mfr, package from jlc_components where lcsc=? and present=1', (int(code.lstrip('C')),)).fetchone()
            if row is None:
                result[part_id] = {'lcsc': '', 'sourcing': {'source': 'jlcparts', 'status': 'unverified',
                                                           'reason': f'{code} is not in the JLCPCB parts database; choose an orderable part'}}
    stamp = datetime.fromtimestamp(newest, timezone.utc).strftime('%Y-%m-%d') if newest else None
    document = {'_meta': {'source': 'yaqwsx/jlcparts database (https://github.com/yaqwsx/jlcparts)', 'data_date': stamp,
                          'note': 'JLCPCB list prices in USD at qty 1 and stock when the database was built; estimates, not quotes. '
                                  'Regenerate with scripts/refresh_sourcing.py.'},
                **result}
    Path(args.out).write_text(json.dumps(document, indent=1, ensure_ascii=False) + '\n')
    print(f'resolved {len(result)} parts (data date {stamp}) -> {args.out}')
    for part_id in missing:
        print(f'  NO CANDIDATE for {part_id}: relax mpn/package in sourcing_queries.json', file=sys.stderr)
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
