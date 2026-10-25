"""Cross-check catalog land patterns against the footprint of the sourced LCSC part (JLCEDA/EasyEDA Official Library).

  python scripts/check_footprints.py                    # check every part with an LCSC code, write catalog/footprint_checks.json
  python scripts/check_footprints.py LM358 NE555        # only these part ids
  python scripts/check_footprints.py --refresh          # ignore the raw response cache (.cache/easyeda, gitignored)

Evidence only: results never mark a footprint as reviewed. Reference data: https://easyeda.com/api/products/<LCSC>/components
(JLCEDA/EasyEDA Official Library, https://lceda.cn and https://easyeda.com); pad units are 10 mil.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.footprint_check import CHECKS_FILE, compare_pads, parse_easyeda_pads  # noqa: E402
from utils.pcb_builder import load_catalog  # noqa: E402
from utils.telemetry import atomic_json  # noqa: E402

API = 'https://easyeda.com/api/products/{}/components?version=6.4.19.5'
CACHE = ROOT / '.cache' / 'easyeda'
SOURCE = 'JLCEDA/EasyEDA Official Library (easyeda.com products API)'


def fetch(lcsc: str, refresh: bool) -> tuple[dict, bool]:
    path = CACHE / f'{lcsc}.json'
    if path.exists() and not refresh:
        return json.loads(path.read_text()), False
    request = urllib.request.Request(API.format(lcsc), headers={'User-Agent': 'eea-footprint-check/1.0'})
    for wait in (30, 90, 240, None):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 429, 503) or wait is None:
                raise
            print(f'  HTTP {exc.code} for {lcsc}: rate limited, waiting {wait}s', file=sys.stderr)
            time.sleep(wait)
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('parts', nargs='*')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--delay', type=float, default=2.0, help='seconds between network requests')
    parser.add_argument('--position-tol', type=float, default=0.1)
    parser.add_argument('--size-tol', type=float, default=0.15)
    parser.add_argument('--out', default=str(CHECKS_FILE))
    args = parser.parse_args()
    catalog = load_catalog()
    ids = args.parts or [pid for pid, part in catalog.items() if part.get('lcsc') and part.get('pads')]
    results = {}
    for pid in ids:
        part = catalog.get(pid)
        if not part:
            print(f'{pid}: not in catalog', file=sys.stderr)
            continue
        lcsc = part.get('lcsc')
        entry = {'lcsc': lcsc, 'source': SOURCE, 'checked_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        try:
            if not lcsc:
                result = {'status': 'no_reference', 'reasons': ['part has no LCSC code'], 'worst_delta_mm': None, 'transform': None}
            else:
                data, fetched = fetch(lcsc, args.refresh)
                pads = parse_easyeda_pads(data) if data.get('success') else []
                result = compare_pads(part['pads'], pads, args.position_tol, args.size_tol)
                if not pads:
                    result['reasons'] = [f'EasyEDA returned no footprint pads for {lcsc}']
                if fetched:
                    time.sleep(args.delay)
        except Exception as exc:  # a failing part must not stop the run
            result = {'status': 'error', 'reasons': [f'{type(exc).__name__}: {exc}'], 'worst_delta_mm': None, 'transform': None}
            time.sleep(args.delay)
        entry.update(status=result['status'], worst_delta_mm=result['worst_delta_mm'], reasons=result['reasons'],
                     **({'transform': result['transform']} if result.get('transform') else {}),
                     **({'renumbering': result['renumbering']} if result.get('renumbering') else {}))
        results[pid] = entry
        print(f"{pid:28} {lcsc or '-':10} {entry['status']:13} {entry['worst_delta_mm']}  {'; '.join(entry['reasons'])[:110]}")
    out = Path(args.out)
    existing = {}
    if out.exists():
        existing = {k: v for k, v in json.loads(out.read_text()).items() if not k.startswith('_')}
    meta = {'note': 'Machine cross-check of catalog pads against the sourced LCSC part footprint; evidence only, never a review. '
                    'Regenerate with scripts/check_footprints.py.', 'source': SOURCE}
    atomic_json(out, {'_meta': meta, **dict(sorted({**existing, **{k: v for k, v in results.items() if v['status'] != 'error' or k not in existing}}.items()))})
    counts = {}
    for entry in results.values():
        counts[entry['status']] = counts.get(entry['status'], 0) + 1
    print(counts)
    return 0


if __name__ == '__main__':
    sys.exit(main())
