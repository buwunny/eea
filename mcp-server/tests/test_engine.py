import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from utils.pcb_builder import normalize_design,build_board,load_catalog
from utils.simulation import simulate
from utils.freerouting_runner import autoroute
from utils.kicad_runner import verify_drc,export_manufacturing


def astable():
    components=[{'ref':'U1','part_id':'NE555','x_mm':25,'y_mm':20},{'ref':'R1','part_id':'R','value':'10k','x_mm':10,'y_mm':10},{'ref':'R2','part_id':'R','value':'330k','x_mm':20,'y_mm':10},{'ref':'C1','part_id':'C','value':'1u','x_mm':35,'y_mm':10}]
    nets=[{'name':'GND','connections':['U1.1','C1.2']},{'name':'VCC','connections':['U1.8','U1.4','R1.1']},{'name':'THR','connections':['U1.2','U1.6','R2.2','C1.1']},{'name':'DISCH','connections':['U1.7','R1.2','R2.1']},{'name':'OUT','connections':['U1.3']}]
    return components,nets


class EngineTests(unittest.TestCase):
    def test_validation_rejects_conflicting_pins_unknown_parts_and_nonfinite_coordinates(self):
        cs,ns=astable()
        with self.assertRaises(ValueError): normalize_design(cs,ns+[{'name':'SHORT','connections':['U1.GND']}])
        with self.assertRaises(ValueError): normalize_design([{'ref':'X1','part_id':'invented'}],[])
        cs[0]['x_mm']=float('nan')
        with self.assertRaises(ValueError): normalize_design(cs,ns)

    def test_catalog_and_board_embed_every_pad_with_valid_pad_grammar(self):
        self.assertEqual(len(load_catalog()),9)
        with tempfile.TemporaryDirectory() as directory:
            cs,ns=astable(); build=Path(directory)/'build'
            report=build_board(normalize_design(cs,ns),build)
            text=(build/'board.kicad_pcb').read_text()
            self.assertIn('(pad "1" smd rect (net ',text)
            self.assertIn('(layer "Edge.Cuts")',text)
            self.assertEqual(text.count('('),text.count(')'))
            self.assertFalse(report['verified'])
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
            def fake_run(*args,**kwargs):
                (workspace/'build/drc_report.json').write_text('{}')
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
