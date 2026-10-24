import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.routing_review import _supply_path, review_routed_bypasses


def point(x, y):
    return SimpleNamespace(x=round(x * 1e6), y=round(y * 1e6))


class Pad:
    def __init__(self, x, y, number='1'): self.position, self.number = point(x, y), number
    def GetNumber(self): return self.number
    def GetPosition(self): return self.position
    def GetNetCode(self): return 1
    def IsOnLayer(self, layer): return layer == 0
    def HitTest(self, p): return abs(p.x-self.position.x) < 100 and abs(p.y-self.position.y) < 100


class Track:
    def __init__(self, a, b): self.start, self.end = point(*a), point(*b)
    def GetClass(self): return 'PCB_TRACK'
    def GetNetCode(self): return 1
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetLayer(self): return 0
    def GetLength(self): return ((self.start.x-self.end.x)**2+(self.start.y-self.end.y)**2)**.5


class RoutingReviewTests(unittest.TestCase):
    def fixture(self, tracks):
        a, b = Pad(0, 0), Pad(2, 0, '8')
        board = SimpleNamespace(GetTracks=lambda: tracks, GetPads=lambda: [a, b])
        return board, SimpleNamespace(Pads=lambda: [a]), SimpleNamespace(Pads=lambda: [b])

    def test_detour_is_measured_instead_of_straight_line(self):
        board, a, b = self.fixture([Track((0, 0), (0, 5)), Track((0, 5), (2, 5)), Track((2, 5), (2, 0))])
        length, direct, error = _supply_path(board, a, b, '1', '8')
        self.assertEqual(direct, 2)
        self.assertEqual(length, 12)
        self.assertIsNone(error)

    def test_zone_only_connection_is_incomplete_not_zero_length(self):
        board, a, b = self.fixture([])
        length, direct, error = _supply_path(board, a, b, '1', '8')
        self.assertIsNone(length)
        self.assertEqual(direct, 2)
        self.assertIn('zone', error)

    def test_missing_native_dependency_preserves_explicit_incomplete_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'build').mkdir()
            (root / 'build/board.kicad_pcb').write_text('fixture')
            (root / 'build/design.json').write_text(json.dumps({'placement_checks': {'checks': [{'capacitor': 'C1'}]}}))
            with patch.dict(sys.modules, {'pcbnew': None}):
                report = review_routed_bypasses(root)
            self.assertFalse(report['verified'])
            self.assertEqual(report['status'], 'incomplete')
            self.assertTrue((root / 'build/routing_review.json').exists())
