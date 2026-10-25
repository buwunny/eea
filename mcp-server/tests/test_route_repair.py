"""Repair of Freerouting neck-downs that widening to the board minimum pushes into clearance."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import normalize_design, pad_positions, write_board


def have_pcbnew():
    try:
        import pcbnew  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_pcbnew(), 'pcbnew unavailable')
class RepairHelperTests(unittest.TestCase):
    def setUp(self):
        import pcbnew
        self.pcbnew = pcbnew
        # Two resistors side by side: R1.2 and R2.1 face each other on different nets.
        self.design = normalize_design(
            [{'ref': 'R1', 'part_id': 'R', 'value': '1k', 'x_mm': 10, 'y_mm': 10},
             {'ref': 'R2', 'part_id': 'R', 'value': '1k', 'x_mm': 14, 'y_mm': 10}],
            [{'name': 'A', 'connections': ['R1.1']}, {'name': 'B', 'connections': ['R1.2']},
             {'name': 'C', 'connections': ['R2.1']}, {'name': 'D', 'connections': ['R2.2']}], 30, 20)
        self.pads = {p['connection']: p for p in pad_positions(self.design)}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'b.kicad_pcb'
        write_board(self.design, path)
        self.board = pcbnew.LoadBoard(str(path))

    def track(self, net, start, end, width_mm):
        pcbnew = self.pcbnew
        t = pcbnew.PCB_TRACK(self.board)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]), pcbnew.FromMM(start[1])))
        t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]), pcbnew.FromMM(end[1])))
        t.SetWidth(pcbnew.FromMM(width_mm))
        t.SetLayer(pcbnew.F_Cu)
        t.SetNet(self.board.GetNetsByName()[net])
        self.board.Add(t)
        return t

    def test_conflicts_are_found_only_where_clearance_is_cut(self):
        from utils.freerouting_runner import clearance_conflicts
        b, c = self.pads['R1.2'], self.pads['R2.1']
        gap_x = (b['x_mm'] + b['width_mm'] / 2 + c['x_mm'] - c['width_mm'] / 2) / 2
        tight = self.track('B', (b['x_mm'], b['y_mm']), (gap_x, b['y_mm']), 1.0)  # reaches into R2.1's clearance
        loose = self.track('B', (b['x_mm'], b['y_mm']), (b['x_mm'], b['y_mm'] + 3), 0.2)
        self.assertEqual(clearance_conflicts(self.board, self.pcbnew, [tight, loose]), [tight])

    def test_dangling_stubs_are_trimmed_but_pad_to_pad_tracks_stay(self):
        from utils.freerouting_runner import _trim_dangling_tracks
        a = self.pads['R1.1']
        self.track('A', (a['x_mm'], a['y_mm']), (a['x_mm'], a['y_mm'] + 2), 0.25)              # stub1: pad -> free point
        self.track('A', (a['x_mm'], a['y_mm'] + 2), (a['x_mm'] + 1, a['y_mm'] + 3), 0.25)      # stub2: continues into free space
        self.track('A', (a['x_mm'], a['y_mm']), (a['x_mm'], a['y_mm']), 0.25)                  # on the pad at both ends
        removed = _trim_dangling_tracks(self.board, self.pcbnew, {self.board.GetNetsByName()['A'].GetNetCode()})
        # stub2 ends in free space, then stub1 does; the zero-length segment on the pad stays.
        self.assertEqual(removed, 2)
        remaining = [t for t in self.board.GetTracks() if t.GetNetname() == 'A']
        self.assertEqual(len(remaining), 1)

    def test_removed_items_stay_owned_by_kicad(self):
        from utils.freerouting_runner import _remove
        t = self.track('A', (1, 1), (2, 1), 0.25)
        _remove(self.board, t)
        self.assertFalse(t.thisown)  # Python must not free it while KiCad may still reference it
        import gc
        gc.collect()


if __name__ == '__main__':
    unittest.main()
