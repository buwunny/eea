import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.procurement import normalize_sourcing, bom_entry


class ProcurementTests(unittest.TestCase):
    def setUp(self):
        self.part = {'footprint': 'Resistor_SMD:R_0805_2012Metric', 'lcsc': 'C1', 'price_usd': .01}
        self.component = {'ref': 'R1', 'part_id': 'R', 'value': '10k', 'tolerance_pct': 1}
        self.source = {'manufacturer': 'Example', 'mpn': 'EX10K', 'supplier': 'LCSC', 'supplier_part_number': 'C123',
                       'value': '10000', 'footprint': self.part['footprint'], 'datasheet_url': 'https://example.com/EX10K.pdf',
                       'tolerance_pct': 1, 'power_rating_w': .125}

    def test_matching_sourcing_is_declared_and_never_claimed_verified(self):
        component = {**self.component, 'sourcing': normalize_sourcing(self.source, self.component, self.part)}
        item = bom_entry(component, self.part)
        self.assertEqual(item['lcsc'], 'C123')
        self.assertEqual(item['sourcing_status'], 'declared_unverified')
        self.assertEqual(item['sourcing_missing_fields'], [])

    def test_wrong_value_package_tolerance_and_review_claims_are_rejected(self):
        for changed in ({'value': '1k'}, {'footprint': 'different'}, {'tolerance_pct': 5}, {'reviewed': True},
                        {'datasheet_url': 'file:///secret'}, {'power_rating_w': float('nan')}, {'supplier_part_number': 'wrong'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                normalize_sourcing({**self.source, **changed}, self.component, self.part)

    def test_incomplete_passive_source_does_not_enter_assembly_bom(self):
        component = {**self.component, 'sourcing': {'supplier': 'LCSC', 'supplier_part_number': 'C123'}}
        item = bom_entry(component, self.part)
        self.assertEqual(item['lcsc'], '')
        self.assertIn('power_rating_w', item['sourcing_missing_fields'])

    def test_normalization_preserves_sourcing_and_uses_its_tolerance(self):
        from utils.pcb_builder import normalize_design
        component = {key: value for key, value in self.component.items() if key != 'tolerance_pct'}
        design = normalize_design([{**component, 'sourcing': self.source}], [])
        self.assertEqual(design['components'][0]['tolerance_pct'], 1)
        self.assertEqual(design['bom'][0]['lcsc'], 'C123')
        self.assertEqual(design['bom'][0]['sourcing_status'], 'declared_unverified')
