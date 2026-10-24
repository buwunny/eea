import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import normalize_design,build_board,load_catalog
from utils.simulation import simulate
from utils.freerouting_runner import autoroute, enforce_minimum_track_width, configure_native_rules
from utils.kicad_runner import verify_drc,export_manufacturing


def astable():
    components=[{'ref':'U1','part_id':'NE555','x_mm':25,'y_mm':20},{'ref':'R1','part_id':'R','value':'10k','x_mm':10,'y_mm':10},{'ref':'R2','part_id':'R','value':'330k','x_mm':20,'y_mm':10},{'ref':'C1','part_id':'C','value':'1u','x_mm':35,'y_mm':10}]
    nets=[{'name':'GND','connections':['U1.1','C1.2']},{'name':'VCC','connections':['U1.8','U1.4','R1.1']},{'name':'THR','connections':['U1.2','U1.6','R2.2','C1.1']},{'name':'DISCH','connections':['U1.7','R1.2','R2.1']},{'name':'OUT','connections':['U1.3']}]
    return components,nets


class EngineTests(unittest.TestCase):
    def test_native_fanout_cannot_leave_tracks_below_board_minimum(self):
        from types import SimpleNamespace
        class Track:
            def __init__(self, width, kind='PCB_TRACK'): self.width, self.kind = width, kind
            def GetClass(self): return self.kind
            def GetWidth(self): return self.width
            def SetWidth(self, width): self.width = width
        tracks = [Track(150000), Track(200000), Track(250000), Track(150000, 'PCB_ARC'), Track(100000, 'PCB_VIA')]
        settings = SimpleNamespace(m_TrackMinWidth=200000)
        board = SimpleNamespace(GetDesignSettings=lambda: settings, GetTracks=lambda: tracks)
        self.assertEqual(enforce_minimum_track_width(board, None), 2)
        self.assertEqual([track.width for track in tracks], [200000, 200000, 250000, 200000, 100000])
        self.assertEqual(settings.m_TrackMinWidth, 200000)

    def test_native_dsn_rules_honor_requested_width_and_existing_constraints(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        netclass = Mock()
        settings = SimpleNamespace(m_TrackMinWidth=200000, m_MinClearance=200000)
        board = SimpleNamespace(GetDesignSettings=lambda: settings, GetAllNetClasses=lambda: {'Default': netclass})
        pcbnew = SimpleNamespace(FromMM=lambda value: round(value * 1000000), ToMM=lambda value: value / 1000000)
        self.assertEqual(configure_native_rules(board, pcbnew, 0.25, 0.15), (0.25, 0.2))
        netclass.SetTrackWidth.assert_called_with(250000)
        netclass.SetClearance.assert_called_with(200000)
        self.assertEqual(configure_native_rules(board, pcbnew, 0.1, 0.2), (0.2, 0.2))

    def test_python_router_obeys_deadline_and_reports_unrouted_links(self):
        with tempfile.TemporaryDirectory() as directory, patch('shutil.which', return_value=None), patch('utils.freerouting_runner.ROUTE_TIME_BUDGET_SECONDS', 0):
            workspace = Path(directory)
            cs, ns = astable()
            build_board(normalize_design(cs, ns), workspace / 'build')
            report = autoroute(workspace)
            self.assertGreater(report['unconnected_items'], 0)
            self.assertFalse(report['retryable_unchanged'])
            self.assertTrue(any('time limit' in warning for warning in report['warnings']))

    def test_validation_rejects_conflicting_pins_unknown_parts_and_nonfinite_coordinates(self):
        cs,ns=astable()
        with self.assertRaises(ValueError): normalize_design(cs,ns+[{'name':'SHORT','connections':['U1.GND']}])
        with self.assertRaises(ValueError): normalize_design([{'ref':'X1','part_id':'invented'}],[])
        cs[0]['x_mm']=float('nan')
        with self.assertRaises(ValueError): normalize_design(cs,ns)

    def test_catalog_and_board_embed_every_pad_with_valid_pad_grammar(self):
        self.assertGreaterEqual(len(load_catalog()),9)
        with tempfile.TemporaryDirectory() as directory:
            cs,ns=astable(); build=Path(directory)/'build'
            report=build_board(normalize_design(cs,ns),build)
            text=(build/'board.kicad_pcb').read_text()
            self.assertIn('(pad "1" smd rect (net ',text)
            self.assertIn('(layer "Edge.Cuts")',text)
            self.assertEqual(text.count('('),text.count(')'))
            self.assertFalse(report['verified'])
            self.assertTrue(all(not item['lcsc'] for item in report['bom'] if item['ref'].startswith(('R', 'C'))))
            self.assertTrue((Path(directory)/'src/design.py').exists())

    def test_fallback_end_to_end_never_claims_verified_or_fabricated_gerbers(self):
        with tempfile.TemporaryDirectory() as directory, patch('shutil.which',return_value=None), patch('utils.simulation.subprocess.run',side_effect=OSError('no simulator')):
            workspace=Path(directory); cs,ns=astable(); design=normalize_design(cs,ns)
            build_board(design,workspace/'build')
            simulation=simulate(cs,ns,{'probe_nets':['OUT','THR'],'target_freq_hz':2.0},workspace)
            self.assertEqual(simulation['backend'],'analytical-555')
            self.assertFalse(simulation['sim_passed'])
            self.assertAlmostEqual(simulation['measurements']['frequency_hz'],2.153,places=2)
            route=autoroute(workspace)
            self.assertEqual(route['backend'],'python-grid-astar')
            drc=verify_drc(workspace)
            self.assertIsNone(drc['drc_errors'])
            self.assertFalse(drc['verified'])
            exported=export_manufacturing(workspace,design['bom'])
            self.assertFalse(exported['manufacturing_ready'])
            self.assertFalse((workspace/'manufacturing/gerbers.zip').exists())
            self.assertTrue((workspace/'manufacturing/bom.csv').exists())

    def test_simulation_rejects_unknown_probes_and_unbounded_samples(self):
        cs,ns=astable()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): simulate(cs,ns,{'probe_nets':['UNKNOWN']},directory)
            with self.assertRaises(ValueError): simulate(cs,ns,{'end_time_ms':1e7,'step_time_ms':0.0001},directory)
            with self.assertRaises(ValueError): simulate(cs,ns,{'frequency_tolerance_pct':-1},directory)

    def test_dc_sweep_degrades_without_simulator_and_validates_sweep(self):
        cs,ns=astable()
        with tempfile.TemporaryDirectory() as directory, patch('shutil.which',return_value=None):
            report=simulate(cs,ns,{'analysis_type':'dc','probe_nets':['OUT']},directory)
            self.assertFalse(report['verified'])
            self.assertEqual(report['analysis_type'],'dc')
            self.assertEqual(report['backend'],'unavailable')
            with self.assertRaises(ValueError): simulate(cs,ns,{'analysis_type':'dc','sweep_step_v':0},directory)

    def test_empty_native_drc_does_not_certify_board(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace=Path(directory); build_board(normalize_design(*astable()),workspace/'build')
            def fake_run(args, workspace, **kwargs):
                Path(args[args.index('--output') + 1]).write_text('{}')
            with patch('utils.kicad_runner._run',side_effect=fake_run):
                report=verify_drc(workspace)
                self.assertFalse(report['verified'])
                self.assertIsNone(report['drc_errors'])

    def test_skidl_failure_restores_deterministic_netlist(self):
        with tempfile.TemporaryDirectory() as directory:
            build=Path(directory)/'build'
            def partial_write(*args,**kwargs):
                (build/'board.net').write_text('truncated native output')
                class Failed:
                    returncode=1
                    stdout=''
                return Failed()
            with patch('utils.pcb_builder.subprocess.run',side_effect=partial_write):
                report=build_board(normalize_design(*astable()),build)
            self.assertEqual(report['backend'],'deterministic-xml')
            self.assertIn('<export version="D">',(build/'board.net').read_text())

    def test_rebuild_invalidates_verification_and_manufacturing(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace=Path(directory); (workspace/'build').mkdir(); (workspace/'manufacturing').mkdir()
            (workspace/'build/drc_report.json').write_text('{}'); (workspace/'manufacturing/gerbers.zip').write_bytes(b'old')
            build_board(normalize_design(*astable()),workspace/'build')
            self.assertFalse((workspace/'build/drc_report.json').exists())
            self.assertFalse((workspace/'manufacturing/gerbers.zip').exists())


if __name__=='__main__': unittest.main()
