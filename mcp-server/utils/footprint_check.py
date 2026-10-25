"""Machine cross-check of a catalog land pattern against the footprint of the exact sourced (LCSC) part.

Evidence only: a passing check never promotes a footprint to 'reviewed' (that needs a manufacturer drawing and a human).
"""
from __future__ import annotations
import itertools
import json
import math
from pathlib import Path

CHECKS_FILE = Path(__file__).resolve().parents[1] / 'catalog' / 'footprint_checks.json'
EASYEDA_MM_PER_UNIT = 0.254  # EasyEDA canvas unit is 10 mil
DEFAULT_POSITION_TOL_MM = 0.1
DEFAULT_SIZE_TOL_MM = 0.15
_PERMUTE_LIMIT = 6


def parse_easyeda_pads(response: dict) -> list[dict]:
    """Pads (mm, effective width/height after pad rotation) from an easyeda.com/api/products/<LCSC>/components response."""
    result = (response or {}).get('result') or {}
    shapes = ((result.get('packageDetail') or {}).get('dataStr') or {}).get('shape') or []
    pads = []
    for shape in shapes:
        if not isinstance(shape, str) or not shape.startswith('PAD~'):
            continue
        f = shape.split('~')
        if len(f) < 12 or not f[8].strip():
            continue
        try:
            x, y, w, h = (float(f[i]) * EASYEDA_MM_PER_UNIT for i in (2, 3, 4, 5))
            rotation = float(f[11] or 0)
        except ValueError:
            continue
        if round(rotation) % 180 == 90:
            w, h = h, w
        pads.append({'number': f[8].strip(), 'x_mm': round(x, 4), 'y_mm': round(y, 4), 'width_mm': round(w, 4), 'height_mm': round(h, 4)})
    return pads


def _clean(pads) -> list[dict]:
    out = []
    for p in pads or []:
        try:
            out.append({'number': str(p['number']), 'x': float(p['x_mm']), 'y': float(p['y_mm']), 'w': float(p['width_mm']), 'h': float(p['height_mm'])})
        except (KeyError, TypeError, ValueError):
            raise ValueError(f'invalid pad record: {p!r}') from None
    return out


def _transform(pads, rotation, mirror):
    out = []
    for p in pads:
        x, y = p['x'], p['y']
        if mirror:
            y = -y
        for _ in range(rotation // 90):
            x, y = -y, x
        swap = rotation % 180 == 90
        out.append({**p, 'x': x, 'y': y, 'w': p['h'] if swap else p['w'], 'h': p['w'] if swap else p['h']})
    return out


def _centroid(pads):
    return (sum(p['x'] for p in pads) / len(pads), sum(p['y'] for p in pads) / len(pads))


def _pin1_key(number):
    digits = ''.join(ch for ch in number if ch.isdigit())
    return (0 if number == '1' else 1, int(digits) if digits else 10 ** 9, number)


def _pair(cat_pads, ref_pads, offset):
    """Pair pads with equal numbers; groups sharing a number take the assignment with the smallest worst distance."""
    pairs = []
    dx0, dy0 = offset
    for number in sorted({p['number'] for p in cat_pads}):
        a = [p for p in cat_pads if p['number'] == number]
        b = [{**p, 'x': p['x'] + dx0, 'y': p['y'] + dy0} for p in ref_pads if p['number'] == number]
        if len(a) == 1:
            pairs.append((a[0], b[0]))
        elif len(a) <= _PERMUTE_LIMIT:
            best = min(itertools.permutations(b), key=lambda perm: max(math.hypot(p['x'] - q['x'], p['y'] - q['y']) for p, q in zip(a, perm)))
            pairs.extend(zip(a, best))
        else:
            left = list(b)
            for p in a:
                q = min(left, key=lambda c: math.hypot(p['x'] - c['x'], p['y'] - c['y']))
                left.remove(q)
                pairs.append((p, q))
    return pairs


def _evaluate(cat_pads, ref_pads, rotation, mirror, align):
    ref = _transform(ref_pads, rotation, mirror)
    if align == 'pad1':
        key = min((p['number'] for p in cat_pads), key=_pin1_key)
        a = _centroid([p for p in cat_pads if p['number'] == key])
        b = _centroid([p for p in ref if p['number'] == key])
    else:
        a, b = _centroid(cat_pads), _centroid(ref)
    offset = (a[0] - b[0], a[1] - b[1])
    deltas = []
    for c, r in _pair(cat_pads, ref, offset):
        dx, dy = r['x'] - c['x'], r['y'] - c['y']
        deltas.append({'number': c['number'], 'dx_mm': round(dx, 4), 'dy_mm': round(dy, 4), 'position_mm': round(math.hypot(dx, dy), 4),
                       'size_mm': round(max(abs(r['w'] - c['w']), abs(r['h'] - c['h'])), 4)})
    worst = max((d['position_mm'] for d in deltas), default=0.0)
    return worst, offset, deltas


def compare_pads(catalog_pads, reference_pads, position_tol_mm=DEFAULT_POSITION_TOL_MM, size_tol_mm=DEFAULT_SIZE_TOL_MM) -> dict:
    """Compare pad lists after the best rigid alignment (8 rotations/mirrors x pad-1 or centroid anchoring).

    Returns status pass | mismatch | no_reference | error, per-pad deltas, worst deltas, the transform used and reasons.
    """
    try:
        cat = _clean(catalog_pads)
        ref = _clean(reference_pads) if reference_pads else []
    except ValueError as exc:
        return _result('error', reasons=[str(exc)])
    if not cat:
        return _result('error', reasons=['catalog part has no pads'])
    if not ref:
        return _result('no_reference', reasons=['no reference pads available for the sourced part'])
    cat_numbers, ref_numbers = sorted(p['number'] for p in cat), sorted(p['number'] for p in ref)
    if cat_numbers != ref_numbers:
        missing = sorted(set(cat_numbers) - set(ref_numbers))
        extra = sorted(set(ref_numbers) - set(cat_numbers))
        reasons = [f'pad count/numbering differs: catalog has {len(cat)} pads, reference has {len(ref)}']
        if missing:
            reasons.append(f'pads only in catalog: {missing[:10]}')
        if extra:
            reasons.append(f'pads only in reference: {extra[:10]}')
        if not missing and not extra:
            reasons.append('pad numbers repeat a different number of times')
        return _result('mismatch', reasons=reasons)
    best = None
    for rotation, mirror, align in itertools.product((0, 90, 180, 270), (False, True), ('pad1', 'centroid')):
        worst, offset, deltas = _evaluate(cat, ref, rotation, mirror, align)
        score = (round(worst, 6), mirror, rotation != 0, align != 'pad1')
        if best is None or score < best[0]:
            best = (score, rotation, mirror, align, offset, deltas)
    _, rotation, mirror, align, offset, deltas = best
    worst_pos = max(d['position_mm'] for d in deltas)
    worst_size = max(d['size_mm'] for d in deltas)
    transform = {'rotation_deg': rotation, 'mirror_y': mirror, 'align': align, 'offset_mm': [round(offset[0], 4), round(offset[1], 4)]}
    reasons = []
    bad_pos = sorted((d for d in deltas if d['position_mm'] > position_tol_mm), key=lambda d: -d['position_mm'])
    bad_size = sorted((d for d in deltas if d['size_mm'] > size_tol_mm), key=lambda d: -d['size_mm'])
    if bad_pos:
        reasons.append(f"{len(bad_pos)} pad(s) off by more than {position_tol_mm} mm (worst pad {bad_pos[0]['number']}: {bad_pos[0]['position_mm']} mm)")
    if bad_size:
        reasons.append(f"{len(bad_size)} pad(s) differ in size by more than {size_tol_mm} mm (worst pad {bad_size[0]['number']}: {bad_size[0]['size_mm']} mm)")
    status = 'mismatch' if reasons else 'pass'
    if status == 'pass' and mirror:
        reasons.append('matches only when mirrored: confirm the pin order is not reversed')
    return _result(status, deltas, worst_pos, worst_size, transform, reasons)


def _result(status, deltas=None, worst_pos=None, worst_size=None, transform=None, reasons=None):
    worst = None if worst_pos is None else round(max(worst_pos, worst_size or 0.0), 4)
    return {'status': status, 'pads': deltas or [], 'worst_position_mm': worst_pos, 'worst_size_mm': worst_size,
            'worst_delta_mm': worst, 'transform': transform, 'reasons': reasons or []}


def load_footprint_checks(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or CHECKS_FILE).read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith('_') and isinstance(v, dict)} if isinstance(data, dict) else {}


def describe_check(part_id: str, checks: dict | None = None) -> dict:
    """Compact machine cross-check summary for one part ('missing' when none was recorded)."""
    check = (load_footprint_checks() if checks is None else checks).get(part_id)
    if not check:
        return {'status': 'missing', 'note': 'no machine cross-check against the sourced part; run scripts/check_footprints.py'}
    out = {k: check[k] for k in ('status', 'lcsc', 'worst_delta_mm', 'checked_at', 'source') if check.get(k) is not None}
    if check.get('reasons'):
        out['reasons'] = check['reasons']
    return out
