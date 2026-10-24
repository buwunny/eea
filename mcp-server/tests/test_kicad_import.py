"""KiCad library importer and the imported-parts tier of the catalog."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import kicad_import, part_search, spice_models
from utils.pcb_builder import load_catalog, load_curated_catalog, normalize_design, build_board

SYMBOLS = '''(kicad_symbol_lib (version 20231120)
 (symbol "BASE" (property "Reference" "U") (property "Value" "BASE") (property "Footprint" "Fake:SOT3")
  (property "Description" "Fake LDO") (property "ki_keywords" "ldo regulator")
  (symbol "BASE_0_1")
  (symbol "BASE_1_1"
   (pin power_in line (at 0 0 0) (length 2) (name "VI" (effects)) (number "3" (effects)))
   (pin power_out line (at 0 0 0) (length 2) (name "VO" (effects)) (number "2" (effects)))
   (pin power_in line (at 0 0 0) (length 2) (name "~{EN}" (effects)) (number "1" (effects)))))
 (symbol "DERIVED" (extends "BASE") (property "Value" "DERIVED") (property "Description" "Derived LDO"))
 (symbol "BADPINS" (property "Footprint" "Fake:SOT3")
  (symbol "BADPINS_1_1" (pin input line (at 0 0 0) (length 2) (name "A" (effects)) (number "9" (effects))))))
'''
FOOTPRINT = '''(footprint "SOT3" (layer "F.Cu")
 (attr smd)
 (fp_rect (start -2 -2) (end 2 2) (layer "F.CrtYd") (stroke (width 0.05) (type default)))
 (pad "1" smd roundrect (at -1 -1) (size 0.8 0.6) (layers "F.Cu" "F.Mask" "F.Paste") (roundrect_rratio 0.25))
 (pad "2" smd rect (at 0 1 90) (size 0.8 0.4) (layers "F.Cu" "F.Mask" "F.Paste"))
 (pad "3" smd rect (at 1 -1) (size 0.8 0.6) (layers "F.Cu" "F.Mask" "F.Paste"))
 (pad "" np_thru_hole circle (at 0 0) (size 1 1) (drill 1) (layers "*.Cu" "*.Mask")))
'''


def fake_libraries(root: Path):
    (root / 'sym').mkdir()
    (root / 'sym' / 'Fake.kicad_sym').write_text(SYMBOLS)
    pretty = root / 'fp' / 'Fake.pretty'
    pretty.mkdir(parents=True)
    (pretty / 'SOT3.kicad_mod').write_text(FOOTPRINT)
    return root / 'sym', root / 'fp'


class ImporterUnitTests(unittest.TestCase):
    def test_parser_handles_strings_and_nesting(self):
        tree = kicad_import.parse_sexp('(a "b \\"c\\" d" (e 1.5) f)')
        self.assertEqual(tree, [['a', 'b "c" d', ['e', '1.5'], 'f']])
        with self.assertRaises(ValueError):
            kicad_import.parse_sexp('(a (b)')

    def test_pin_names_are_connection_safe(self):
        self.assertEqual(kicad_import.pin_name('~{RESET}', '4'), 'RESET_N')
        self.assertEqual(kicad_import.pin_name('V_{OUT}', '1'), 'VOUT')
        self.assertEqual(kicad_import.pin_name('', '7'), '7')
        self.assertEqual(kicad_import.pin_name('A B', '7'), 'A_B')

    def test_build_part_from_fake_libraries(self):
        with tempfile.TemporaryDirectory() as d:
            sym, fp = fake_libraries(Path(d))
            part = kicad_import.build_part('FAKE', 'Fake:DERIVED', None, sym, fp, {'max_ratings': {'vin_max_v': 6}})
            self.assertEqual(part['tier'], 'imported')
            self.assertEqual(sorted(p['number'] for p in part['pins']), ['1', '2', '3'])
            self.assertEqual({p['number']: p['name'] for p in part['pins']}['1'], 'EN_N')  # inherited via extends
            self.assertEqual(part['description'], 'Derived LDO (SOT3)')
            self.assertEqual(part['max_ratings'], {'vin_max_v': 6})
            self.assertIsNone(part['spice_model'])
            self.assertEqual(part['footprint_review']['status'], 'unreviewed')
            # rotated pad swaps its size; mounting hole is kept in the S-expression but not in pads
            pad2 = next(p for p in part['pads'] if p['number'] == '2')
            self.assertEqual((pad2['width_mm'], pad2['height_mm']), (0.4, 0.8))
            self.assertEqual(len(part['pads']), 3)
            self.assertIn('np_thru_hole', part['footprint_sexpr'])
            self.assertIn('(fp_rect (start -2 -2) (end 2 2)', part['footprint_sexpr'])

    def test_symbol_pin_without_pad_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            sym, fp = fake_libraries(Path(d))
            with self.assertRaisesRegex(ValueError, 'no pad'):
                kicad_import.build_part('BAD', 'Fake:BADPINS', None, sym, fp)

    def test_manifest_skips_collisions_and_reports_reasons(self):
        with tempfile.TemporaryDirectory() as d:
            sym, fp = fake_libraries(Path(d))
            manifest = {'entries': [{'part_id': 'GOOD', 'symbol': 'Fake:BASE'}, {'part_id': 'R', 'symbol': 'Fake:BASE'},
                                    {'part_id': 'MISSING', 'symbol': 'Fake:NOPE'}],
                        'auto': [{'library': 'Fake', 'match': 'BAD*'}]}
            parts, skipped = kicad_import.import_manifest(manifest, sym, fp, {'R'})
            self.assertEqual(set(parts), {'GOOD'})
            reasons = {s['part_id']: s['reason'] for s in skipped}
            self.assertIn('curated parts win', reasons['R'])
            self.assertIn('not found', reasons['MISSING'])
            self.assertIn('no pad', reasons['BADPINS'])


class ImportedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.imported = {k: v for k, v in load_catalog().items() if v.get('tier') == 'imported'}
        if not self.imported:
            self.skipTest('catalog/imported_parts.json not generated')

    def test_tiers_merge_without_overriding_curated(self):
        catalog = load_catalog()
        self.assertFalse(set(self.imported) & set(load_curated_catalog()))
        self.assertEqual(catalog['NE555']['tier'], 'curated')
        self.assertTrue(set(load_curated_catalog()) <= set(catalog))

    def test_every_imported_part_is_consistent(self):
        for pid, part in self.imported.items():
            with self.subTest(part=pid):
                pad_numbers = {p['number'] for p in part['pads']}
                self.assertEqual({p['number'] for p in part['pins']}, pad_numbers)
                found = re.findall(r'\(pad "([^"]+)" (\w+) \w+ \(at ([-\d.]+) ([-\d.]+)\)', part['footprint_sexpr'])
                self.assertEqual([(n, float(x), float(y)) for n, _, x, y in found],
                                 [(p['number'], float(p['x_mm']), float(p['y_mm'])) for p in part['pads']])
                if part['spice_model']:
                    spice_models.validate(part['spice_model'], pad_numbers)
                self.assertEqual(part['footprint_review']['status'], 'unreviewed')
                self.assertEqual(part['import']['license']['spdx'], 'CC-BY-SA-4.0')
                self.assertTrue(part['import']['symbol'] and part['import']['footprint'])
                self.assertIn(':', part['footprint'])
                for pin in part['pins']:
                    self.assertRegex(pin['name'], r'^[^\s"]+$')
                    self.assertTrue(pin['function'])

    def test_imported_parts_build_a_board_with_warning(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'EEA_WORKSPACE': d}):
            components = [{'ref': 'U1', 'part_id': 'AP2112K_3V3', 'value': 'AP2112K-3.3'},
                          {'ref': 'C1', 'part_id': 'C', 'value': '1u'}]
            pins = {p['name']: p['number'] for p in load_catalog()['AP2112K_3V3']['pins']}
            nets = [{'name': 'GND', 'connections': ['U1.GND', 'C1.2']},
                    {'name': 'VIN', 'connections': ['U1.VIN', 'U1.EN', 'C1.1']}]
            self.assertIn('GND', pins)
            design = normalize_design(components, nets, 40, 30)
            self.assertTrue(any('Imported KiCad parts' in w and 'AP2112K_3V3' in w for w in design['warnings']))
            build = Path(d) / 'build'
            build.mkdir()
            build_board(design, build)
            text = (build / 'board.kicad_pcb').read_text()
            self.assertIn('Package_TO_SOT_SMD:SOT-23-5', text)
            self.assertIn('"GND"', text)


class SearchTests(unittest.TestCase):
    def test_search_ranks_exact_id_first_and_filters(self):
        found = part_search.search_parts('NE555')
        self.assertEqual(found['results'][0]['part_id'], 'NE555')
        by_package = part_search.search_parts('', package='SOT-223', tier='imported')
        self.assertTrue(by_package['results'] and all('SOT-223' in r['footprint'] for r in by_package['results']))
        self.assertTrue(all(r['tier'] == 'imported' for r in by_package['results']))
        self.assertEqual(part_search.search_parts('zzzznotapart')['total_matches'], 0)
        self.assertLessEqual(part_search.search_parts('', limit=3)['shown'], 3)
        with self.assertRaises(ValueError):
            part_search.search_parts('x', tier='bogus')

    def test_search_finds_by_function(self):
        ids = {r['part_id'] for r in part_search.search_parts('motor driver bridge', limit=20)['results']}
        if any(v.get('tier') == 'imported' for v in load_catalog().values()):
            self.assertIn('L293D_DIP16', ids)

    def test_details_have_cautions_only_for_imported(self):
        self.assertNotIn('cautions', part_search.part_details('NE555'))
        imported = next((k for k, v in load_catalog().items() if v.get('tier') == 'imported' and not v.get('spice_model')), None)
        if imported:
            detail = part_search.part_details(imported)
            self.assertTrue(any('No SPICE model' in c for c in detail['cautions']))
            self.assertFalse(detail['simulates'])
        modeled = next((k for k, v in load_catalog().items() if v.get('tier') == 'imported' and v.get('spice_model')), None)
        if modeled:
            detail = part_search.part_details(modeled)
            self.assertTrue(detail['simulates'])
            self.assertTrue(any('generic' in c for c in detail['cautions']))
            self.assertEqual(detail['license']['spdx'], 'CC-BY-SA-4.0')
        with self.assertRaisesRegex(ValueError, 'unknown part_id'):
            part_search.part_details('nope')


if __name__ == '__main__':
    unittest.main()
