import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.kicad_runner import export_manufacturing, convert_positions


class ExportTests(unittest.TestCase):
    def prepare(self, directory):
        root = Path(directory)
        (root / 'build').mkdir()
        board = root / 'build/board.kicad_pcb'
        board.write_text('board fixture')
        (root / 'build/drc_report.json').write_text(json.dumps({'verified': True, 'drc_errors': 0, 'unconnected_items': 0, 'board_sha256': hashlib.sha256(board.read_bytes()).hexdigest()}))
        return root

    def fake_export(self, args, workspace):
        output = Path(args[args.index('--output') + 1])
        if args[2] == 'gerbers':
            for layer in ('F_Cu', 'B_Cu', 'Edge_Cuts'):
                (output / f'board-{layer}.gbr').write_text('gerber fixture')
        elif args[2] == 'drill':
            (output / 'board-PTH.drl').write_text('drill fixture')
        else:
            output.write_text('Ref,Val,Package,PosX,PosY,Rot,Side\nU1,chip,SOIC,12.5,-8.25,-90,bottom\n')

    def test_exports_validate_package_and_translate_jlcpcb_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.prepare(directory)
            with patch('utils.kicad_runner._run', side_effect=self.fake_export):
                report = export_manufacturing(root, [{'ref': 'U1', 'value': 'chip', 'footprint': 'SOIC'}])
            self.assertTrue(report['manufacturing_ready'])
            self.assertEqual(report['board_sha256'], hashlib.sha256((root / 'build/board.kicad_pcb').read_bytes()).hexdigest())
            with (root / 'manufacturing/cpl.csv').open() as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, ['Designator', 'Mid X', 'Mid Y', 'Layer', 'Rotation'])
                row = next(reader)
                self.assertEqual((row['Designator'], row['Layer'], row['Rotation']), ('U1', 'Bottom', '270'))
                self.assertEqual(row['Mid X'], '12.500000mm')
            with zipfile.ZipFile(root / 'manufacturing/gerbers.zip') as archive:
                self.assertEqual(set(archive.namelist()), {'board-F_Cu.gbr', 'board-B_Cu.gbr', 'board-Edge_Cuts.gbr', 'board-PTH.drl'})

    def test_stale_drc_blocks_export_and_removes_old_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.prepare(directory)
            (root / 'manufacturing').mkdir()
            (root / 'manufacturing/gerbers.zip').write_text('stale archive')
            (root / 'build/board.kicad_pcb').write_text('changed board')
            with patch('utils.kicad_runner._run') as run:
                report = export_manufacturing(root, [])
            run.assert_not_called()
            self.assertFalse(report['manufacturing_ready'])
            self.assertFalse((root / 'manufacturing/gerbers.zip').exists())

    def test_warning_review_does_not_block_clean_drc_fabrication_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.prepare(directory)
            path = root / 'build/drc_report.json'
            drc = json.loads(path.read_text())
            drc.update(drc_warnings=1, violations=[{'type': 'silk_overlap', 'severity': 'warning', 'description': 'Review silkscreen'}])
            path.write_text(json.dumps(drc))
            with patch('utils.kicad_runner._run', side_effect=self.fake_export):
                report = export_manufacturing(root, [])
            self.assertTrue(report['manufacturing_ready'])
            self.assertTrue(report['fabrication_files_ready'])
            self.assertFalse(report['fabrication_review_ready'])
            self.assertFalse(report['assembly_ready'])
            readiness = json.loads((root / 'build/readiness.json').read_text())
            self.assertEqual(readiness['checks']['drc_review']['status'], 'needs_attention')

    def test_partial_export_cannot_leave_downloadable_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.prepare(directory)
            def fail_positions(args, workspace):
                if args[2] == 'pos':
                    raise RuntimeError('positions export failed')
                self.fake_export(args, workspace)
            with patch('utils.kicad_runner._run', side_effect=fail_positions):
                report = export_manufacturing(root, [])
            self.assertFalse(report['manufacturing_ready'])
            self.assertFalse((root / 'manufacturing/gerbers.zip').exists())
            self.assertFalse((root / 'manufacturing/cpl.csv').exists())
            self.assertEqual(list((root / 'manufacturing').glob('.export-*')), [])

    def test_position_conversion_rejects_unknown_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bad.csv'
            source.write_text('Unknown,columns\n1,2\n')
            with self.assertRaises(ValueError):
                convert_positions(source, Path(directory) / 'cpl.csv')
