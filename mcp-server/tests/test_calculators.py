import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.calculators import calc_555_astable, calc_led_resistor, calc_voltage_divider, nearest_standard


class CalculatorTests(unittest.TestCase):
    def test_555_two_hertz(self):
        r = calc_555_astable(2.0)
        self.assertLess(abs(r['actual_freq_hz'] - 2.0) / 2.0, 0.05)
        self.assertTrue(1e3 <= r['r1_ohms'] <= 1e6 and 1e3 <= r['r2_ohms'] <= 1e6)
        self.assertGreater(r['actual_duty_pct'], 50)
        check = 1.44 / ((r['r1_ohms'] + 2 * r['r2_ohms']) * r['c_farads'])
        self.assertAlmostEqual(check, r['actual_freq_hz'], delta=0.01 * check)

    def test_555_duty_and_clamp(self):
        r = calc_555_astable(1000, duty_pct=75)
        self.assertLess(abs(r['actual_duty_pct'] - 75), 5)
        self.assertLess(abs(r['actual_freq_hz'] - 1000) / 1000, 0.06)
        low = calc_555_astable(1000, duty_pct=30)
        self.assertGreater(low['actual_duty_pct'], 50)
        self.assertTrue(any('50%' in n for n in low['notes']))

    def test_555_given_capacitor(self):
        r = calc_555_astable(10, c_farads=1e-6)
        self.assertEqual(r['c_farads'], 1e-6)

    def test_led_resistor(self):
        r = calc_led_resistor(5, 2, 10)
        self.assertEqual(r['r_ohms'], 300)
        self.assertLessEqual(r['actual_current_ma'], 10.0001)
        r = calc_led_resistor(5, 2.1, 10)
        self.assertEqual(r['r_ohms'], 300)  # ideal 290 -> next higher E24
        self.assertTrue(r['recommended_package_ok'])
        with self.assertRaises(ValueError):
            calc_led_resistor(1.5, 2, 10)

    def test_divider(self):
        r = calc_voltage_divider(5, 3.3)
        self.assertLess(abs(r['actual_vout'] - 3.3), 0.05)
        self.assertEqual(nearest_standard(4800), 4700)


if __name__ == '__main__':
    unittest.main()
