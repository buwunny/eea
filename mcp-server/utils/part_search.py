"""Compact search and detail views over the merged curated + imported part catalog."""
from __future__ import annotations
import re
from .pcb_builder import load_catalog
from .footprint_check import describe_check

DETAIL_FIELDS = ('typical_application', 'required_support_parts', 'max_ratings', 'datasheet')


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r'[^a-z0-9.+]+', text.lower()) if t]


def _simulates(part_id: str, part: dict) -> bool:
    from .simulation import has_spice_model
    return has_spice_model(part_id, part)


def summary(part_id: str, part: dict) -> dict:
    return {'part_id': part_id, 'tier': part.get('tier', 'curated'), 'category': part.get('category'),
            'description': part.get('description', ''), 'footprint': part['footprint'], 'pin_count': len(part['pins']),
            'simulates': _simulates(part_id, part), 'lcsc': part.get('lcsc') or None, 'price_usd': part.get('price_usd') or None}


def search_parts(query: str = '', category: str | None = None, package: str | None = None,
                 tier: str = 'all', limit: int = 15) -> dict:
    """Rank parts by how many query words match the id (heaviest), keywords/description, pin names and footprint."""
    if tier not in ('all', 'curated', 'imported'):
        raise ValueError("tier must be 'all', 'curated' or 'imported'")
    limit = max(1, min(int(limit), 50))
    words = _tokens(query)
    results = []
    for part_id, part in load_catalog().items():
        if tier != 'all' and part.get('tier', 'curated') != tier:
            continue
        if category and category.lower() not in str(part.get('category', '')).lower():
            continue
        if package and package.lower() not in part['footprint'].lower():
            continue
        score = 0
        if words:
            ident = part_id.lower()
            pins = ' '.join(f"{p['name']} {p.get('function', '')}" for p in part['pins']).lower()
            text = ' '.join(str(part.get(f, '')) for f in ('description', 'keywords', 'typical_application', 'category')).lower()
            footprint = part['footprint'].lower()
            for word in words:
                if word == ident:
                    score += 20
                elif word in ident:
                    score += 8
                if word in text:
                    score += 3
                if word in pins:
                    score += 1
                if word in footprint:
                    score += 1
            if score == 0:
                continue
        results.append((-score, 0 if part.get('tier', 'curated') == 'curated' else 1, part_id, part))
    results.sort(key=lambda r: r[:3])
    shown = [summary(pid, part) for _, _, pid, part in results[:limit]]
    return {'total_matches': len(results), 'shown': len(shown), 'results': shown}


def part_details(part_id: str) -> dict:
    catalog = load_catalog()
    if part_id not in catalog:
        close = [pid for pid in catalog if part_id.lower() in pid.lower()][:8]
        raise ValueError(f'unknown part_id {part_id!r}' + (f'; similar: {close}' if close else '; use search_parts'))
    part = catalog[part_id]
    tier = part.get('tier', 'curated')
    review = part.get('footprint_review') or {}
    detail = {**summary(part_id, part),
              'pins': [f"{p['number']} {p['name']}: {p.get('function', '')}".rstrip(': ') for p in part['pins']],
              'pad_count': len(part['pads']), 'footprint_review': review.get('status', 'unreviewed'),
              **{f: part[f] for f in DETAIL_FIELDS if part.get(f)}}
    if part.get('lcsc') or tier == 'imported':
        detail['footprint_cross_check'] = describe_check(part_id)
    sourcing = part.get('sourcing') or {}
    if sourcing.get('mpn'):
        detail['sourcing'] = {k: sourcing.get(k) for k in ('mpn', 'manufacturer', 'package', 'library_type', 'stock', 'price_tiers', 'alternates') if sourcing.get(k) is not None}
    elif sourcing.get('status') == 'unverified':
        detail['sourcing'] = {'status': 'unverified', 'reason': sourcing.get('reason')}
    model = part.get('spice_model')
    if tier == 'imported':
        detail['import'] = part.get('import', {}).get('symbol')
        detail['license'] = (part.get('import') or {}).get('license')
        cautions = ['Imported from KiCad libraries: pinout and footprint come from the symbol/footprint pair but ratings, '
                    'support parts and package orientation are NOT reviewed; confirm them against the datasheet.']
        if model:
            cautions.append(f"Simulates with a generic {model.get('template')} model ({model.get('basis')}), not a vendor model: results are indicative.")
        else:
            cautions.append('No SPICE model: the part is skipped in simulation and flagged as unmodeled. Use skip_simulation or model its function with curated parts.')
        if not part.get('lcsc'):
            cautions.append('No LCSC code: BOM cost excludes it; choose an orderable part matching the value and footprint.')
        detail['cautions'] = cautions
    elif model and model.get('kind') == 'generic':
        detail['cautions'] = [f"Simulates with a generic {model.get('template')} model, not a vendor model: results are indicative."]
    return detail
