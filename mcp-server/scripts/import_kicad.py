"""Generate catalog/imported_parts.json from the installed KiCad libraries.

  python scripts/import_kicad.py                       # build from catalog/import_manifest.json
  python scripts/import_kicad.py --search Regulator_Linear 'LM*'   # list symbols and their default footprints
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.kicad_import import find_kicad_dir, import_manifest, list_symbols, resolve_symbol  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--manifest', default=str(ROOT / 'catalog' / 'import_manifest.json'))
    parser.add_argument('--out', default=str(ROOT / 'catalog' / 'imported_parts.json'))
    parser.add_argument('--symbol-dir')
    parser.add_argument('--footprint-dir')
    parser.add_argument('--search', nargs='+', metavar=('LIBRARY', 'GLOB'), help='list symbols in a library instead of importing')
    args = parser.parse_args()
    symbol_dir = find_kicad_dir('symbols', args.symbol_dir)
    footprint_dir = find_kicad_dir('footprints', args.footprint_dir)
    if args.search:
        library, pattern = args.search[0], (args.search[1] if len(args.search) > 1 else '*')
        for name in list_symbols(library, symbol_dir, pattern):
            symbol = resolve_symbol(library, name, symbol_dir)
            print(f'{library}:{name}\t{len(symbol["pins"])} pins\t{symbol["properties"].get("Footprint", "") or "-"}')
        return 0
    manifest = json.loads(Path(args.manifest).read_text())
    curated = json.loads((ROOT / 'catalog' / 'curated_parts.json').read_text())
    parts, skipped = import_manifest(manifest, symbol_dir, footprint_dir, set(curated))
    out = Path(args.out)
    out.write_text(json.dumps(dict(sorted(parts.items())), indent=1, ensure_ascii=False) + '\n')
    print(f'imported {len(parts)} parts -> {out} ({out.stat().st_size // 1024} KiB)')
    for item in skipped:
        print(f'  skipped {item["part_id"]} ({item["symbol"]}): {item["reason"]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
