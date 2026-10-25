import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.footprint_check import apply_explanation, compare_pads, describe_check, load_footprint_checks, parse_easyeda_pads
from utils.pcb_builder import normalize_design
from utils.part_search import part_details
from utils.readiness import update_readiness

FIXTURE = Path(__file__).parent / 'fixtures' / 'easyeda_C7593_trimmed.json'


def soic8(pitch=1.27, half_span=2.6):
    pads = []
    for i in range(4):
        y = (i - 1.5) * pitch
        pads.append({'number': str(i + 1), 'x_mm': -half_span, 'y_mm': y, 'width_mm': 1.6, 'height_mm': 0.6})
        pads.append({'number': str(8 - i), 'x_mm': half_span, 'y_mm': y, 'width_mm': 1.6, 'height_mm': 0.6})
    return pads


def moved(pads, fn):
    out = []
    for p in pads:
        x, y, w, h = fn(p['x_mm'], p['y_mm'], p['width_mm'], p['height_mm'])
        out.append({**p, 'x_mm': x, 'y_mm': y, 'width_mm': w, 'height_mm': h})
    return out


class ComparePadsTests(unittest.TestCase):
    def test_identical_pads_pass(self):
        result = compare_pads(soic8(), soic8())
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['worst_delta_mm'], 0.0)
        self.assertEqual(result['transform']['rotation_deg'], 0)
        self.assertEqual(len(result['pads']), 8)

    def test_translated_reference_passes(self):
        result = compare_pads(soic8(), moved(soic8(), lambda x, y, w, h: (x + 1016.0, y - 762.0, w, h)))
        self.assertEqual(result['status'], 'pass')
        self.assertAlmostEqual(result['transform']['offset_mm'][0], -1016.0, places=3)

    def test_rotated_references_pass_with_swapped_pad_sizes(self):
        for angle, fn in ((90, lambda x, y, w, h: (-y, x, h, w)), (180, lambda x, y, w, h: (-x, -y, w, h)),
                          (270, lambda x, y, w, h: (y, -x, h, w))):
            with self.subTest(angle=angle):
                result = compare_pads(soic8(), moved(soic8(), fn))
                self.assertEqual(result['status'], 'pass', result['reasons'])
                self.assertEqual(result['worst_delta_mm'], 0.0)

    def test_mirrored_reference_passes_but_is_flagged(self):
        pads = [{'number': '1', 'x_mm': 0.0, 'y_mm': 0.0, 'width_mm': 1.0, 'height_mm': 1.0},
                {'number': '2', 'x_mm': 2.0, 'y_mm': 0.5, 'width_mm': 1.0, 'height_mm': 1.0},
                {'number': '3', 'x_mm': 4.5, 'y_mm': 0.0, 'width_mm': 1.0, 'height_mm': 1.0}]
        result = compare_pads(pads, moved(pads, lambda x, y, w, h: (x + 3.0, -y, w, h)))
        self.assertEqual(result['status'], 'pass')
        self.assertTrue(result['transform']['mirror_y'])
        self.assertTrue(any('mirrored' in r for r in result['reasons']))

    def test_shifted_pad_mismatches(self):
        ref = soic8()
        ref[3] = {**ref[3], 'x_mm': ref[3]['x_mm'] + 0.3}
        result = compare_pads(soic8(), ref)
        self.assertEqual(result['status'], 'mismatch')
        self.assertGreaterEqual(result['worst_delta_mm'], 0.1)
        self.assertTrue(any('off by more than' in r for r in result['reasons']))

    def test_pad_size_difference_mismatches_and_tolerance_is_configurable(self):
        ref = moved(soic8(), lambda x, y, w, h: (x, y, w + 0.4, h))
        self.assertEqual(compare_pads(soic8(), ref)['status'], 'mismatch')
        self.assertEqual(compare_pads(soic8(), ref, size_tol_mm=0.5)['status'], 'pass')
        shifted = moved(soic8(), lambda x, y, w, h: (x + (0.12 if x > 0 else 0), y, w, h))
        self.assertEqual(compare_pads(soic8(), shifted, position_tol_mm=0.2)['status'], 'pass')

    def test_different_pin_pitch_mismatches(self):
        result = compare_pads(soic8(), soic8(pitch=1.0))
        self.assertEqual(result['status'], 'mismatch')

    def test_swapped_pad_numbers_mismatch(self):
        ref = soic8()
        ref[0], ref[1] = {**ref[0], 'number': ref[1]['number']}, {**ref[1], 'number': ref[0]['number']}
        self.assertEqual(compare_pads(soic8(), ref)['status'], 'mismatch')

    def test_pads_sharing_numbers_are_matched_by_position(self):
        tab = [{'number': '2', 'x_mm': 0.0, 'y_mm': 3.15, 'width_mm': 1.2, 'height_mm': 2.0},
               {'number': '1', 'x_mm': -2.3, 'y_mm': 3.15, 'width_mm': 1.2, 'height_mm': 2.0},
               {'number': '3', 'x_mm': 2.3, 'y_mm': 3.15, 'width_mm': 1.2, 'height_mm': 2.0},
               {'number': '2', 'x_mm': 0.0, 'y_mm': -3.15, 'width_mm': 3.6, 'height_mm': 2.2}]
        ref = list(reversed(tab))
        result = compare_pads(tab, ref)
        self.assertEqual(result['status'], 'pass', result['reasons'])
        self.assertEqual(len(result['pads']), 4)

    def test_same_shape_with_other_pad_numbers_is_reported_but_still_mismatches(self):
        relay = [('1', 0, 0), ('2', 1.95, 6.05), ('3', 14.15, 6.05), ('4', 14.2, -6.0), ('5', 1.95, -5.95)]
        cat = [{'number': n, 'x_mm': x, 'y_mm': y, 'width_mm': 3.0, 'height_mm': 3.0} for n, x, y in relay]
        rename = {'1': '5', '2': '1', '3': '2', '4': '3', '5': '4'}  # catalog -> reference numbering
        ref = [{**p, 'number': rename[p['number']], 'x_mm': p['x_mm'] + 50, 'y_mm': p['y_mm'] - 20} for p in cat]
        result = compare_pads(cat, ref)
        self.assertEqual(result['status'], 'mismatch')
        self.assertEqual(result['renumbering']['reference_to_catalog'], {v: k for k, v in rename.items()})
        self.assertTrue(any('renumbered' in r and 'confirm pin functions' in r for r in result['reasons']), result['reasons'])

    def test_different_shape_gets_no_renumbering(self):
        ref = moved(soic8(), lambda x, y, w, h: (x * 1.2, y, w, h))
        self.assertNotIn('renumbering', compare_pads(soic8(), ref))

    def test_two_pad_spacing_difference_is_named(self):
        cat = [{'number': '1', 'x_mm': 0, 'y_mm': 0, 'width_mm': 2.2, 'height_mm': 2.2}, {'number': '2', 'x_mm': 10.16, 'y_mm': 0, 'width_mm': 2.2, 'height_mm': 2.2}]
        ref = [{**cat[0]}, {**cat[1], 'x_mm': 8.7}]
        result = compare_pads(cat, ref)
        self.assertEqual(result['status'], 'mismatch')
        self.assertIn('pad spacing differs: catalog 10.16 mm, reference 8.70 mm', result['reasons'])

    def test_numbering_difference_is_reported(self):
        ref = soic8()[:-1]
        result = compare_pads(soic8(), ref)
        self.assertEqual(result['status'], 'mismatch')
        self.assertTrue(any('only in catalog' in r for r in result['reasons']))

    def test_missing_reference_and_bad_input(self):
        self.assertEqual(compare_pads(soic8(), [])['status'], 'no_reference')
        self.assertEqual(compare_pads(soic8(), None)['status'], 'no_reference')
        self.assertEqual(compare_pads([], soic8())['status'], 'error')
        self.assertEqual(compare_pads([{'number': '1'}], soic8())['status'], 'error')


class EasyedaParserTests(unittest.TestCase):
    def setUp(self):
        self.pads = parse_easyeda_pads(json.loads(FIXTURE.read_text()))

    def test_parses_eight_pads_in_mm(self):
        self.assertEqual(sorted(p['number'] for p in self.pads), [str(n) for n in range(1, 9)])
        by_number = {p['number']: p for p in self.pads}
        self.assertAlmostEqual(by_number['2']['x_mm'] - by_number['1']['x_mm'], 1.27, places=2)
        self.assertAlmostEqual(by_number['1']['y_mm'] - by_number['8']['y_mm'], 5.2, places=1)
        self.assertAlmostEqual(by_number['1']['width_mm'], 0.588, places=3)

    def test_matches_a_kicad_soic8_land_pattern(self):
        kicad = soic8(half_span=2.7)
        kicad = [{**p, 'width_mm': 1.55, 'height_mm': 0.6} for p in kicad]
        self.assertEqual(compare_pads(kicad, self.pads, position_tol_mm=0.15, size_tol_mm=0.3)['status'], 'pass')
        self.assertEqual(compare_pads(kicad, self.pads)['status'], 'mismatch')

    def test_pad_rotation_swaps_width_and_height_and_unnumbered_pads_are_skipped(self):
        response = {'result': {'packageDetail': {'dataStr': {'shape': [
            'PAD~RECT~100~100~10~2~1~~7~0~~270~gge1',
            'PAD~RECT~110~100~10~2~1~~8~0~~0~gge2',
            'PAD~RECT~120~100~10~2~1~~~0~~0~gge3',
            'TRACK~1~2~~1 1 2 2~gge4', 'PAD~broken']}}}}
        pads = parse_easyeda_pads(response)
        self.assertEqual([p['number'] for p in pads], ['7', '8'])
        self.assertAlmostEqual(pads[0]['width_mm'], 0.508)
        self.assertAlmostEqual(pads[0]['height_mm'], 2.54)
        self.assertAlmostEqual(pads[1]['x_mm'] - pads[0]['x_mm'], 2.54)

    def test_empty_or_failed_response_has_no_pads(self):
        self.assertEqual(parse_easyeda_pads({}), [])
        self.assertEqual(parse_easyeda_pads({'success': False, 'result': None}), [])


class EvidenceIntegrationTests(unittest.TestCase):
    CHECKS = {'_meta': {}, 'R': {'status': 'mismatch', 'lcsc': 'C17414', 'worst_delta_mm': 0.42, 'reasons': ['1 pad(s) off']},
              'LED': {'status': 'pass', 'lcsc': 'C2295', 'worst_delta_mm': 0.02, 'reasons': []},
              'C': {'status': 'no_reference', 'lcsc': 'C49678', 'worst_delta_mm': None, 'reasons': ['none']}}

    def test_load_and_describe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checks.json'
            self.assertEqual(load_footprint_checks(path), {})
            path.write_text(json.dumps(self.CHECKS))
            checks = load_footprint_checks(path)
            self.assertNotIn('_meta', checks)
            self.assertEqual(describe_check('R', checks)['status'], 'mismatch')
            self.assertEqual(describe_check('NOPE', checks)['status'], 'missing')

    def test_explanation_applies_only_to_the_exact_result_it_was_written_for(self):
        check = {'status': 'mismatch', 'lcsc': 'C1', 'worst_delta_mm': 0.73, 'reasons': ['off']}
        note = {'lcsc': 'C1', 'worst_delta_mm': 0.73, 'reason': 'lead pitch', 'evidence': 'datasheet', 'recorded_by': 'x'}
        explained = apply_explanation(check, note)
        self.assertEqual(explained['status'], 'explained')
        self.assertEqual(explained['explanation']['reason'], 'lead pitch')
        for stale in ({**note, 'worst_delta_mm': 0.5}, {**note, 'lcsc': 'C2'}):
            result = apply_explanation(check, stale)
            self.assertEqual(result['status'], 'mismatch')
            self.assertTrue(any('different cross-check result' in r for r in result['reasons']))
        self.assertEqual(apply_explanation({'status': 'pass', 'lcsc': 'C1'}, note)['status'], 'pass')
        self.assertIs(apply_explanation(check, None), check)

    def test_load_applies_explanations_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path, notes = Path(directory) / 'checks.json', Path(directory) / 'notes.json'
            path.write_text(json.dumps(self.CHECKS))
            notes.write_text(json.dumps({'_meta': {}, 'R': {'lcsc': 'C17414', 'worst_delta_mm': 0.42, 'reason': 'style', 'evidence': 'drawing'}}))
            checks = load_footprint_checks(path, notes)
            self.assertEqual(checks['R']['status'], 'explained')
            self.assertEqual(describe_check('R', checks)['explanation']['evidence'], 'drawing')
            self.assertEqual(checks['LED']['status'], 'pass')

    def test_recorded_explanations_match_current_checks(self):
        from utils.footprint_check import CHECKS_FILE, EXPLANATIONS_FILE
        notes = json.loads(EXPLANATIONS_FILE.read_text())
        checks = load_footprint_checks()
        for pid in (k for k in notes if not k.startswith('_')):
            with self.subTest(part=pid):
                self.assertEqual(checks[pid]['status'], 'explained', f'{pid}: explanation is stale; recheck and update {EXPLANATIONS_FILE.name}')

    def test_part_details_shows_cross_check_but_never_promotes_review(self):
        with patch('utils.part_search.describe_check', return_value={'status': 'pass', 'lcsc': 'C7593'}):
            detail = part_details('NE555')
        self.assertEqual(detail['footprint_cross_check']['status'], 'pass')
        self.assertNotEqual(detail['footprint_review'], 'reviewed')

    def test_normalize_design_warns_on_mismatch_only(self):
        components = [{'ref': 'R1', 'part_id': 'R', 'value': '1k', 'x_mm': 10, 'y_mm': 10}, {'ref': 'D1', 'part_id': 'LED', 'value': 'red', 'x_mm': 20, 'y_mm': 10}]
        nets = [{'name': 'A', 'connections': ['R1.1']}, {'name': 'B', 'connections': ['R1.2', 'D1.1']}, {'name': 'C', 'connections': ['D1.2']}]
        with patch('utils.footprint_check.load_footprint_checks', return_value=self.CHECKS):
            design = normalize_design(components, nets, 40, 30)
        warning = [w for w in design['warnings'] if 'sourced LCSC part' in w]
        self.assertEqual(len(warning), 1)
        self.assertIn("'R'", warning[0])
        self.assertNotIn('LED', warning[0])
        by_ref = {r['component']: r for r in design['footprint_reviews']}
        self.assertEqual(by_ref['R1']['cross_check'], 'mismatch')
        self.assertEqual(by_ref['D1']['cross_check'], 'pass')
        self.assertNotEqual(by_ref['D1']['status'], 'reviewed')
        with patch('utils.footprint_check.load_footprint_checks', return_value={**self.CHECKS, 'R': {**self.CHECKS['R'], 'status': 'explained'}}):
            design = normalize_design(components, nets, 40, 30)
        self.assertFalse([w for w in design['warnings'] if 'sourced LCSC part' in w])
        self.assertEqual({r['component']: r for r in design['footprint_reviews']}['R1']['cross_check'], 'explained')

    def test_readiness_mentions_cross_check_but_stays_incomplete(self):
        from test_readiness import ReadinessTests
        for status, text in (('mismatch', 'WARNING: pads differ'), ('pass', 'machine check, not a review'),
                             ('explained', 'recorded datasheet reason (not a review)'),
                             ('no_reference', 'No reference footprint'), ('missing', 'No footprint cross-check recorded')):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = ReadinessTests().fixture(directory)
                checks = {'R': {'status': status}} if status != 'missing' else {}
                with patch('utils.readiness.load_footprint_checks', return_value=checks):
                    footprint = update_readiness(root)['checks']['footprint_review']
                self.assertEqual(footprint['status'], 'incomplete')
                self.assertTrue(any(text in reason and 'R1' in reason for reason in footprint['reasons']), footprint['reasons'])
                self.assertEqual(footprint['components'][0]['cross_check'], status)
                self.assertEqual(footprint['components'][0]['status'], 'unreviewed')


if __name__ == '__main__':
    unittest.main()
