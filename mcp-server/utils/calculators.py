"""Pure design calculators with standard-value rounding. No files, telemetry or git."""
from __future__ import annotations
import math

E24 = (1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1)
E12 = (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)
CAP_SET = (1e-9, 10e-9, 100e-9, 1e-6, 10e-6, 100e-6)


def _decade_values(series, lo, hi):
    out = []
    for exp in range(-12, 10):
        for m in series:
            v = round(m * 10 ** exp, 12)
            if lo <= v <= hi:
                out.append(v)
    return out


def _clean(v):
    return float(f'{v:.6g}')


def nearest_standard(value, series=E24):
    """Nearest standard value (log distance)."""
    if value <= 0:
        raise ValueError('value must be positive')
    return min(_decade_values(series, value / 10, value * 10), key=lambda s: abs(math.log(s / value)))


def next_higher_standard(value, series=E24):
    if value <= 0:
        raise ValueError('value must be positive')
    return min(s for s in _decade_values(series, value * (1 - 1e-9), value * 10))


def _positive(v, name):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
        raise ValueError(f'{name} must be a positive number')
    return float(v)


def calc_555_astable(freq_hz, duty_pct=None, c_farads=None):
    """f = 1.44/((R1+2R2)C); duty = (R1+R2)/(R1+2R2). Resistors rounded to E24."""
    freq_hz = _positive(freq_hz, 'freq_hz')
    notes = []
    duty = 50.0 if duty_pct is None else float(duty_pct)
    if not 0 < duty < 100:
        raise ValueError('duty_pct must be between 0 and 100')
    if duty <= 50.0:
        if duty < 50.0:
            notes.append(f'Basic 555 astable cannot go below 50% duty (requested {duty:g}%). Clamped to ~51%; '
                         'for lower duty put a 1N4148 across R2 (anode on the pin 7 side, cathode on the pin 2/6 side; duty becomes R1/(R1+R2)) '
                         'or invert the output.')
        else:
            notes.append('50% is the theoretical limit (R1 -> 0); using ~51%.')
        duty = 51.0
    duty = min(duty, 99.0)
    f = duty / 100
    # R1+2R2 = T_total = 1.44/(f C); R2 = (1-f)*Rt ; R1 = Rt - 2R2
    def solve(c):
        rt = 1.44 / (freq_hz * c)
        r2 = (1 - f) * rt
        r1 = rt - 2 * r2
        return r1, r2
    if c_farads is None:
        c = None
        for cand in CAP_SET:
            r1, r2 = solve(cand)
            if 1e3 <= r1 and r2 >= 1e3 and max(r1, r2) <= 1e6:
                c = cand
                break
        if c is None:
            c = min(CAP_SET, key=lambda cc: abs(math.log10(max(solve(cc)[1], 1e-9) / 1e4)))
            notes.append('No capacitor in the standard set keeps both resistors within 1k-1M; picked the closest.')
        c_farads = c
    else:
        c_farads = _positive(c_farads, 'c_farads')
    r1, r2 = solve(c_farads)
    if r1 < 1e3:
        notes.append('R1 below 1k at the requested duty; raised to 1k (duty will be higher; check actual_duty_pct).')
        r1 = 1e3
    r1s, r2s = nearest_standard(r1), nearest_standard(max(r2, 1e3))
    actual_f = 1.44 / ((r1s + 2 * r2s) * c_farads)
    actual_d = 100 * (r1s + r2s) / (r1s + 2 * r2s)
    if not 1e3 <= r2s <= 1e6 or not 1e3 <= r1s <= 1e6:
        notes.append('A resistor is outside 1k-1M; consider a different capacitor.')
    notes.append('Use a 10nF cap on CTRL (pin 5) to ground for stability. Component values are E24.')
    return {'r1_ohms': _clean(r1s), 'r2_ohms': _clean(r2s), 'c_farads': _clean(c_farads),
            'actual_freq_hz': _clean(actual_f), 'actual_duty_pct': _clean(actual_d),
            'formula': 'f = 1.44/((R1+2*R2)*C); duty = (R1+R2)/(R1+2*R2)', 'notes': notes}


def calc_led_resistor(vin_v, vf_v, current_ma):
    vin_v, vf_v, current_ma = _positive(vin_v, 'vin_v'), _positive(vf_v, 'vf_v'), _positive(current_ma, 'current_ma')
    if vin_v <= vf_v:
        raise ValueError('vin_v must exceed the LED forward voltage vf_v')
    ideal = (vin_v - vf_v) / (current_ma / 1000)
    r = next_higher_standard(ideal)
    actual_ma = (vin_v - vf_v) / r * 1000
    power_mw = (actual_ma / 1000) ** 2 * r * 1000
    return {'r_ohms': _clean(r), 'ideal_ohms': _clean(ideal), 'actual_current_ma': _clean(actual_ma),
            'power_mw': _clean(power_mw), 'recommended_package_ok': power_mw <= 125 * 0.5,
            'notes': 'Next-higher E24 value; 0805 rated 125 mW, derated to 50%.'}


def calc_voltage_divider(vin_v, vout_v, r_total_ohms=100000):
    vin_v, vout_v, r_total_ohms = _positive(vin_v, 'vin_v'), _positive(vout_v, 'vout_v'), _positive(r_total_ohms, 'r_total_ohms')
    if vout_v >= vin_v:
        raise ValueError('vout_v must be below vin_v')
    ratio = vout_v / vin_v
    r_bottom_ideal = r_total_ohms * ratio
    best = None
    for rb in _decade_values(E24, r_bottom_ideal / 3, r_bottom_ideal * 3):
        for rt in _decade_values(E24, (r_total_ohms - r_bottom_ideal) / 3, (r_total_ohms - r_bottom_ideal) * 3):
            err = abs(vin_v * rb / (rb + rt) - vout_v)
            key = (round(err, 9), abs(math.log((rb + rt) / r_total_ohms)))
            if best is None or key < best[0]:
                best = (key, rt, rb)
    _, rt, rb = best
    actual = vin_v * rb / (rb + rt)
    return {'r_top': _clean(rt), 'r_bottom': _clean(rb), 'actual_vout': _clean(actual),
            'error_pct': _clean(100 * (actual - vout_v) / vout_v), 'formula': 'Vout = Vin*R_bottom/(R_top+R_bottom)'}
