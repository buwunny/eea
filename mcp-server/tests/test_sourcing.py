"""LCSC sourcing overlay, its generator, and the bundled third-party license files."""
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.pcb_builder import load_catalog, load_curated_catalog
from utils.procurement import bom_entry

spec = importlib.util.spec_from_file_location('refresh_sourcing', ROOT / 'scripts' / 'refresh_sourcing.py')
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


def fake_db():
    db = sqlite3.connect(':memory:')
    db.execute('create table jlc_components (lcsc integer, present integer, stock integer, mfr text, package text, manufacturer text, '
               'library_type text, preferred integer, price text, fetched_at integer, datasheet text, description text)')
    rows = [(1, 1, 5000, 'ABC123', 'SOT-23', 'Maker A', 'expand', 0, '1-9:0.50,10-:0.40', 100, '', ''),
            (2, 1, 900, 'ABC123', 'SOT-23', 'Maker B', 'base', 0, '1-9:0.90,10-:0.80', 200, '', ''),
            (3, 1, 50, 'ABC123', 'SOT-23', 'Maker C', 'base', 0, '1-9:0.10', 300, '', ''),      # below minimum stock
            (4, 1, 9000, 'ABC123', 'SOIC-8', 'Maker D', 'base', 0, '1-9:0.10', 400, '', ''),    # wrong package
            (5, 0, 9000, 'ABC123', 'SOT-23', 'Maker E', 'base', 0, '1-9:0.10', 500, '', '')]    # not present
    db.executemany('insert into jlc_components values (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    return db


class GeneratorTests(unittest.TestCase):
    def test_price_tiers_parse(self):
        self.assertEqual(refresh.parse_tiers('1-49:0.2094,50-149:0.1633,5000-:0.1117'),
                         [{'qty': 1, 'price_usd': 0.2094}, {'qty': 50, 'price_usd': 0.1633}, {'qty': 5000, 'price_usd': 0.1117}])
        self.assertEqual(refresh.parse_tiers('garbage'), [])

    def test_ranking_prefers_basic_in_stock_matching_package(self):
        rows = refresh.candidates(fake_db(), ['ABC123'], package='SOT-23')
        self.assertEqual([r['lcsc'] for r in rows], ['C2', 'C1'])
        self.assertEqual(rows[0]['price_tiers'][0]['price_usd'], 0.9)
        self.assertEqual(refresh.candidates(fake_db(), ['NOPE%']), [])
        self.assertEqual(refresh.resolve(fake_db(), {'mpn': ['ABC123'], 'package': 'SOT-23', 'lcsc': 'C1'})[0]['lcsc'], 'C1')


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.overlay = json.loads((ROOT / 'catalog' / 'sourcing.json').read_text())
        self.entries = {k: v for k, v in self.overlay.items() if not k.startswith('_')}

    def test_every_entry_is_well_formed_and_applied(self):
        catalog = load_catalog()
        self.assertGreater(len(self.entries), 40)
        self.assertRegex(self.overlay['_meta']['data_date'], r'^\d{4}-\d{2}-\d{2}$')
        for pid, entry in self.entries.items():
            with self.subTest(part=pid):
                self.assertIn(pid, catalog)
                self.assertEqual(catalog[pid]['lcsc'], entry['lcsc'])
                if entry['lcsc']:
                    self.assertRegex(entry['lcsc'], r'^C\d+$')
                    self.assertGreater(entry['price_usd'], 0)
                    self.assertEqual(catalog[pid]['price_usd'], entry['price_usd'])
                    self.assertTrue(entry['sourcing']['mpn'])
                    self.assertGreaterEqual(entry['sourcing']['stock'], refresh.MIN_STOCK)
                else:
                    self.assertEqual(entry['sourcing']['status'], 'unverified')

    def test_queries_and_overlay_agree(self):
        queries = json.loads((ROOT / 'catalog' / 'sourcing_queries.json').read_text())
        wanted = {k for k in queries if not k.startswith('_')}
        self.assertEqual(wanted - set(self.entries), set())
        self.assertTrue(wanted <= set(load_catalog()))

    def test_value_dependent_parts_stay_unsourced(self):
        catalog = load_catalog()
        # R and C keep a representative code in the catalog, but the BOM never emits it: the value decides the order code
        for pid in ('R', 'C'):
            self.assertEqual(bom_entry({'ref': 'X1', 'part_id': pid, 'value': '1k'}, catalog[pid])['lcsc'], '')

    def test_bom_entry_carries_mpn_for_sourced_parts(self):
        part = load_catalog()['AMS1117_3V3']
        item = bom_entry({'ref': 'U1', 'part_id': 'AMS1117_3V3', 'value': '3.3V'}, part)
        self.assertEqual(item['lcsc'], 'C6186')
        self.assertEqual(item['mpn'], 'AMS1117-3.3')
        self.assertEqual(item['library_type'], 'base')

    def test_curated_catalog_also_receives_sourcing(self):
        self.assertEqual(load_curated_catalog()['NE555']['lcsc'], 'C7593')
        self.assertEqual(load_curated_catalog()['NE555']['tier'], 'curated')


class LicenseTests(unittest.TestCase):
    def test_license_files_and_notice_are_bundled(self):
        for name in ('KiCad-Libraries-LICENSE.md', 'CC-BY-SA-4.0.txt', 'jlcparts-MIT.txt'):
            self.assertGreater((ROOT / 'catalog' / 'licenses' / name).stat().st_size, 500, name)
        notice = (ROOT / 'catalog' / 'NOTICE.md').read_text()
        for needle in ('CC-BY-SA-4.0', 'KiCad', 'jlcparts', 'Redistributing', 'licenses/KiCad-Libraries-LICENSE.md'):
            self.assertIn(needle, notice)
        self.assertIn('Attribution-ShareAlike 4.0 International', (ROOT / 'catalog' / 'licenses' / 'CC-BY-SA-4.0.txt').read_text()[:200])
        self.assertIn('CC-BY-SA', (ROOT / 'catalog' / 'licenses' / 'KiCad-Libraries-LICENSE.md').read_text())


if __name__ == '__main__':
    unittest.main()
