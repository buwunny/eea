"""Validate declared sourcing details without claiming supplier or human verification."""
from __future__ import annotations
import math
import re
from urllib.parse import urlparse

TEXT_FIELDS = {'manufacturer', 'mpn', 'supplier', 'supplier_part_number', 'value', 'footprint', 'datasheet_url'}
NUMBER_FIELDS = {'tolerance_pct', 'voltage_rating_v', 'power_rating_w'}


def normalize_sourcing(value, component, part):
    if not isinstance(value, dict):
        raise ValueError(f"{component['ref']}.sourcing must be an object")
    unknown = set(value) - TEXT_FIELDS - NUMBER_FIELDS
    if unknown:
        raise ValueError('Unsupported sourcing fields: ' + ', '.join(sorted(unknown)))
    out = {}
    for key, raw in value.items():
        if key in TEXT_FIELDS:
            if not isinstance(raw, str) or not raw.strip() or len(raw) > 500 or any(ord(c) < 32 for c in raw):
                raise ValueError(f'Invalid sourcing {key}')
            out[key] = raw.strip()
        else:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw <= 0:
                raise ValueError(f'Invalid sourcing {key}')
            if key == 'tolerance_pct' and raw >= 100:
                raise ValueError('Sourcing tolerance_pct must be between 0 and 100')
            out[key] = float(raw)
    if 'datasheet_url' in out:
        url = urlparse(out['datasheet_url'])
        if url.scheme != 'https' or not url.netloc or url.username or url.password:
            raise ValueError('Sourcing datasheet_url must be an HTTPS URL without credentials')
    if 'footprint' in out and out['footprint'] != part['footprint']:
        raise ValueError(f"{component['ref']} sourcing footprint does not match its catalog footprint")
    if 'value' in out:
        matches = out['value'] == component['value']
        if component['part_id'] in ('R', 'C', 'Capacitor_Electrolytic'):
            from .simulation import engineering
            try:
                matches = math.isclose(engineering(out['value']), engineering(component['value']), rel_tol=1e-9)
            except ValueError:
                pass
        if not matches:
            raise ValueError(f"{component['ref']} sourcing value does not match the design value")
    if out.get('supplier', '').lower() == 'lcsc' and 'supplier_part_number' in out:
        if not re.fullmatch(r'C[0-9]+', out['supplier_part_number'], re.I):
            raise ValueError('LCSC supplier_part_number must be C followed by digits')
        out['supplier_part_number'] = out['supplier_part_number'].upper()
    if 'tolerance_pct' in out and 'tolerance_pct' in component and not math.isclose(out['tolerance_pct'], component['tolerance_pct']):
        raise ValueError(f"{component['ref']} sourcing tolerance does not match its declared design tolerance")
    return out


def sourcing_issues(component, part):
    source = component.get('sourcing', {})
    required = {'manufacturer', 'mpn', 'supplier', 'supplier_part_number', 'value', 'footprint', 'datasheet_url'}
    if component['part_id'] in ('R', 'C', 'Capacitor_Electrolytic'):
        required.add('tolerance_pct')
    if component['part_id'] == 'R':
        required.add('power_rating_w')
    if component['part_id'] in ('C', 'Capacitor_Electrolytic'):
        required.add('voltage_rating_v')
    return sorted(required - source.keys())


def trust_fields(component, part):
    """Additive part-trust facts for the BOM: catalog tier, footprint review status, SPICE coverage, stock."""
    from . import spice_models
    from .simulation import SPICE_PARTS
    if component['part_id'] in SPICE_PARTS:
        spice = 'curated'
    elif spice_models.is_generic(part.get('spice_model')):
        spice = 'generic'
    else:
        spice = 'none'
    review = part.get('footprint_review')
    out = {'tier': part.get('tier', 'curated'),
           'footprint_review': review.get('status', 'unreviewed') if isinstance(review, dict) else 'unreviewed',
           'spice': spice}
    stock = (part.get('sourcing') or {}).get('stock')
    if isinstance(stock, int) and not isinstance(stock, bool):
        out['stock'] = stock
    return out


def bom_entry(component, part):
    source = component.get('sourcing', {})
    missing = sourcing_issues(component, part)
    code = '' if component['part_id'] in ('R', 'C') else part.get('lcsc', '')
    # A declared passive order code is only emitted with matching value/package and ratings.
    if source.get('supplier', '').lower() == 'lcsc' and not missing:
        code = source['supplier_part_number']
    return {
        'ref': component['ref'], 'value': component['value'], 'footprint': part['footprint'],
        'lcsc': code, 'price_usd': part.get('price_usd', 0),
        **({'mpn': part['sourcing']['mpn'], 'library_type': part['sourcing'].get('library_type')}
           if part.get('sourcing', {}).get('mpn') and code == part.get('lcsc') else {}),
        'sourcing': source, 'sourcing_status': 'declared_unverified' if source else 'missing',
        'sourcing_missing_fields': missing,
        **trust_fields(component, part),
    }
