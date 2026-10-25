"""In2 supply plane selection by IC supply pins, with the reason reported."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.design_blocks import EXAMPLES, compose_design
from utils.freerouting_runner import plane_selection, select_plane_nets
from utils.pcb_builder import normalize_design


def example_design(name):
    e = EXAMPLES[name]
    r = compose_design(e['blocks'], e['connections'], e.get('board_width_mm'), e.get('board_height_mm'))
    return normalize_design(r['components'], r['nets'], r['board_width_mm'], r['board_height_mm'], layer_count=4)


class PlaneSelectionTests(unittest.TestCase):
    def test_examples_pick_their_3v3_rail(self):
        for name in ('usb_c_3v3_breakout', 'esp32_c3_usb_uart', 'rp2040_usb_board'):
            ground, supply, reasons = plane_selection(example_design(name))
            self.assertEqual((ground, supply), ('GND', '3V3'), name)
            self.assertIn('IC supply pin', reasons['supply'])
            self.assertEqual(select_plane_nets(example_design(name)), ('GND', '3V3'))

    def test_more_ic_supply_pins_beat_more_connections(self):
        comps = [{'ref': 'U1', 'part_id': 'NE555'}, {'ref': 'C1', 'part_id': 'C', 'value': '100n'},
                 {'ref': 'R1', 'part_id': 'R', 'value': '1k'}, {'ref': 'R2', 'part_id': 'R', 'value': '1k'},
                 {'ref': 'R3', 'part_id': 'R', 'value': '1k'}]
        nets = [{'name': '9V', 'connections': ['U1.8', 'U1.4', 'C1.1']},
                {'name': '5V', 'connections': ['R1.1', 'R2.1', 'R3.1', 'R1.2', 'R2.2']},
                {'name': 'GND', 'connections': ['U1.1', 'C1.2', 'R3.2']}]
        d = normalize_design(comps, nets, 30, 25, layer_count=4)
        self.assertEqual(select_plane_nets(d)[1], '9V')


if __name__ == '__main__':
    unittest.main()
