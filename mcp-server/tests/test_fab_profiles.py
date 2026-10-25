"""Fabricator profiles: values, sources and the KiCad DRC rules they write (verified with kicad-cli)."""
import collections
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import fab_profiles
from utils.fab_profiles import PROFILES, get_profile, kicad_rules, resolve_profile
from utils.pcb_builder import normalize_design, write_board, write_project

COMPONENTS = [{'ref': 'R1', 'part_id': 'R', 'value': '1k', 'x_mm': 10, 'y_mm': 10},
              {'ref': 'R2', 'part_id': 'R', 'value': '1k', 'x_mm': 30, 'y_mm': 10}]
NETS = [{'name': 'A', 'connections': ['R1.1', 'R2.1']}, {'name': 'B', 'connections': ['R1.2', 'R2.2']}]
CODES = {'A': 1, 'B': 2}


def violation_board(profile, scale):
    """Board with one item per profile rule at `scale` times the limit, and a Default class at the minimums."""
    rules = get_profile(profile)['rules']
    layers = max(get_profile(profile)['layer_counts'][0], 2)
    design = normalize_design(COMPONENTS, NETS, 40, 30, layer_count=layers, fabricator_profile=profile,
                              netclasses={'Default': {'track_mm': rules['min_track_mm'], 'clearance_mm': rules['min_clearance_mm']}})
    k = scale
    width = 0.2
    edge = rules['min_edge_clearance_mm'] * k + width / 2
    gap = rules['min_clearance_mm'] * k + width
    segments = [
        {'start': (3, 20), 'end': (8, 20), 'width': rules['min_track_mm'] * k, 'net': 'A'},                   # track_width
        {'start': (12, 20), 'end': (17, 20), 'width': width, 'net': 'A'},                                      # clearance
        {'start': (12, 20 + gap), 'end': (17, 20 + gap), 'width': width, 'net': 'B'},
        {'start': (22, edge), 'end': (27, edge), 'width': width, 'net': 'A'},                                  # copper_edge_clearance
    ]
    drill = max(rules['min_via_drill_mm'], rules['min_hole_mm'])
    ring = rules['min_annular_ring_mm']
    big = max(rules['min_via_dia_mm'], drill + 2 * ring) * 1.3
    vias = [
        # (x, y, diameter, drill)
        (4, 25, rules['min_via_dia_mm'] * k, min(drill, rules['min_via_dia_mm'] * k - 2 * ring * 1.01)),       # via_diameter
        (9, 25, max(big, drill * 2), max(big, drill * 2) - 2 * ring * k),                                      # annular_width
        (14, 25, big, rules['min_hole_mm'] * k),                                                               # drill_out_of_range
        (20, 25, big, drill), (20 + drill + rules['min_hole_to_hole_mm'] * k, 25, big, drill),                # hole_to_hole
    ]
    return design, segments, _vias(vias)


def _vias(vias):
    return '\n'.join(f' (via (at {x:g} {y:g}) (size {d:.4f}) (drill {h:.4f}) (layers "F.Cu" "B.Cu") (net 1))' for x, y, d, h in vias)


# min_hole_clearance is written too, but for vias the copper clearance (ring + clearance) is always
# reached first and KiCad reports only that, so it is not asserted here.


def drc_types(directory, profile, scale):
    design, segments, vias = violation_board(profile, scale)
    board = Path(directory) / 'board.kicad_pcb'
    write_board(design, board, segments, vias)
    write_project(design, Path(directory) / 'board.kicad_pro')
    output = Path(directory) / 'drc.json'
    subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all', '--output', str(output), str(board)],
                   capture_output=True, text=True, timeout=120)
    return collections.Counter(v['type'] for v in json.loads(output.read_text())['violations'])


RULE_TYPES = ('track_width', 'clearance', 'copper_edge_clearance', 'via_diameter', 'annular_width', 'drill_out_of_range',
              'hole_to_hole')


class ProfileTests(unittest.TestCase):
    def test_profiles_are_complete_and_sourced(self):
        self.assertEqual(fab_profiles.DEFAULT_PROFILE, 'conservative')
        for name, profile in PROFILES.items():
            with self.subTest(name):
                self.assertEqual(set(profile['rules']), set(fab_profiles.KICAD_RULE_KEYS) | {'min_via_drill_mm'})
                self.assertEqual(set(kicad_rules(name)), set(fab_profiles.KICAD_RULE_KEYS.values()))
                fab_profiles.check_class('Default', profile['default_class'], name)
                if name.startswith('jlcpcb'):
                    self.assertTrue(profile['sources'] and all(s.startswith('https://jlcpcb.com/') for s in profile['sources']))
                    self.assertEqual(fab_profiles.summary(name)['checked'], fab_profiles.CHECKED)
        # Sensible standard values, not JLCPCB's absolute minimums (0.10 / 0.09 mm track, 0.15 mm hole).
        self.assertGreater(PROFILES['jlcpcb_2l']['rules']['min_track_mm'], 0.10)
        self.assertGreater(PROFILES['jlcpcb_4l']['rules']['min_track_mm'], 0.09)
        self.assertGreater(PROFILES['jlcpcb_4l']['rules']['min_hole_mm'], 0.15)

    def test_profile_layer_counts(self):
        self.assertEqual(resolve_profile(None, 2), 'conservative')
        self.assertEqual(resolve_profile('jlcpcb_4l', 4), 'jlcpcb_4l')
        for bad in (('jlcpcb_4l', 2), ('jlcpcb_2l', 4), ('nope', 2)):
            with self.assertRaises(ValueError):
                resolve_profile(*bad)
        with self.assertRaisesRegex(ValueError, 'jlcpcb_2l'):
            normalize_design(COMPONENTS, NETS, 40, 30, fabricator_profile='jlcpcb_4l')
        self.assertEqual(normalize_design(COMPONENTS, NETS, 40, 30, layer_count=4, fabricator_profile='jlcpcb_4l')['fabricator_profile'], 'jlcpcb_4l')

    def test_project_file_carries_profile_rules(self):
        design = normalize_design(COMPONENTS, NETS, 40, 30, layer_count=4, fabricator_profile='jlcpcb_4l')
        with tempfile.TemporaryDirectory() as directory:
            write_project(design, Path(directory) / 'board.kicad_pro')
            project = json.loads((Path(directory) / 'board.kicad_pro').read_text())
        self.assertEqual(project['board']['design_settings']['rules'], kicad_rules('jlcpcb_4l'))
        self.assertEqual(project['net_settings']['classes'][0]['name'], 'Default')


@unittest.skipUnless(shutil.which('kicad-cli'), 'kicad-cli unavailable')
class ProfileDrcTests(unittest.TestCase):
    """Each claimed rule: an item just below the profile limit is flagged, just above it is not."""
    def test_drc_enforces_every_profile_rule(self):
        for profile in PROFILES:
            with self.subTest(profile), tempfile.TemporaryDirectory() as low, tempfile.TemporaryDirectory() as high:
                below, above = drc_types(low, profile, 0.9), drc_types(high, profile, 1.1)
                for kind in RULE_TYPES:
                    self.assertGreaterEqual(below[kind], 1, f'{profile}: {kind} not flagged below the limit: {dict(below)}')
                    self.assertEqual(above[kind], 0, f'{profile}: {kind} flagged above the limit: {dict(above)}')

    def test_rules_differ_between_profiles(self):
        # A 0.15 mm track is legal for jlcpcb_4l but below the conservative 0.2 mm minimum.
        with tempfile.TemporaryDirectory() as directory:
            design = normalize_design(COMPONENTS, NETS, 40, 30, layer_count=4, fabricator_profile='jlcpcb_4l')
            segments = [{'start': (3, 20), 'end': (8, 20), 'width': 0.15, 'net': 'A'}]
            counts = {}
            for profile in ('jlcpcb_4l', 'conservative'):
                design['fabricator_profile'] = profile
                write_board(design, Path(directory) / 'board.kicad_pcb', segments)
                write_project(design, Path(directory) / 'board.kicad_pro')
                subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--output', str(Path(directory) / 'drc.json'),
                                str(Path(directory) / 'board.kicad_pcb')], capture_output=True, timeout=120)
                counts[profile] = sum(v['type'] == 'track_width' for v in json.loads((Path(directory) / 'drc.json').read_text())['violations'])
        self.assertEqual(counts, {'jlcpcb_4l': 0, 'conservative': 1})


if __name__ == '__main__':
    unittest.main()
