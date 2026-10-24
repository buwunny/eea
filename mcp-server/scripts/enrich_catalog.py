"""One-off, idempotent generator: adds datasheet fields and new parts to curated_parts.json."""
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'catalog' / 'curated_parts.json'
SRC = 'EEA deterministic minimal land pattern; verify against manufacturer drawing before fabrication'

# part_id -> (typical_application, required_support_parts, {pin number: function}, max_ratings)
INFO = {
 'R': ('Generic 0805 resistor for pull-ups, LED current limiting, dividers and RC timing. LED limit R = (Vsupply - Vf) / I.',
       [], {'1': 'Terminal 1 (non-polarized)', '2': 'Terminal 2 (non-polarized)'},
       {'power_w': 0.125, 'voltage_v': 150}),
 'C': ('Generic 0805 ceramic capacitor for decoupling (100 nF per IC supply pin) and timing. Check DC-bias derating above 6.3 V.',
       [], {'1': 'Terminal 1 (non-polarized)', '2': 'Terminal 2 (non-polarized)'},
       {'voltage_v': 50}),
 'LED': ('0805 indicator LED; always use a series resistor, about 330R-1k for 5 V at 5-10 mA.',
         ['Series resistor R to the anode or cathode'],
         {'1': 'Cathode; connect toward GND/driver', '2': 'Anode; connect toward supply via resistor'},
         {'if_max_ma': 20, 'vr_max_v': 5, 'vf_typ_v': 2.0}),
 'NE555': ('Astable: f = 1.44 / ((Ra + 2*Rb) * C), duty = (Ra+Rb)/(Ra+2*Rb). Tie TRIG to THR, RESET to VCC, decouple VCC.',
           ['100nF C on VCC to GND', '10nF C on CTRL to GND (optional)', 'Ra between VCC and DISCH', 'Rb between DISCH and THR', 'Timing C between THR and GND'],
           {'1': 'Ground', '2': 'Trigger; starts cycle when < 1/3 VCC', '3': 'Output; sources/sinks up to 200 mA',
            '4': 'Active-low reset; tie to VCC if unused', '5': 'Control voltage (2/3 VCC); decouple with 10 nF',
            '6': 'Threshold; ends timing when > 2/3 VCC', '7': 'Discharge; open-collector to ground for timing cap', '8': 'Supply, 4.5-16 V'},
           {'vcc_max_v': 16, 'i_out_max_ma': 200, 'vcc_min_v': 4.5}),
 'USB_C_Receptacle_PowerOnly': ('Power-only USB-C sink. Put 5.1k pull-down from each of CC1 and CC2 to GND so a source supplies 5 V on VBUS.',
           ['5.1k R from CC1 to GND', '5.1k R from CC2 to GND', '10uF C on VBUS to GND'],
           {'A1': 'Ground', 'A4': 'VBUS 5 V power', 'A5': 'Configuration channel 1; 5.1k to GND (Rd)', 'A9': 'VBUS 5 V power',
            'A12': 'Ground', 'B1': 'Ground', 'B4': 'VBUS 5 V power', 'B5': 'Configuration channel 2; 5.1k to GND (Rd)',
            'B9': 'VBUS 5 V power', 'B12': 'Ground', 'S1': 'Shield; tie to GND', 'S2': 'Shield; tie to GND'},
           {'vbus_max_v': 5.5, 'current_default_a': 0.9, 'current_max_a': 3}),
 'AMS1117_3V3': ('Input 4.5-12 V; place 10 uF ceramic/tantalum on VIN and 22 uF on VOUT close to pins. Dropout ~1.1 V; dissipates (Vin-Vout)*I.',
                 ['10uF C on VIN to GND', '22uF C on VOUT to GND'],
                 {'1': 'Ground/adjust reference', '2': 'Regulated 3.3 V output (also tab pad); up to 800 mA', '3': 'Input voltage, 4.5-12 V'},
                 {'vin_max_v': 15, 'i_out_max_ma': 800, 'dropout_v': 1.1, 'vout_v': 3.3}),
 'PinHeader_1x04': ('2.54 mm 1x4 header for power/UART/I2C/SWD breakout; label pins by net names.',
                    [], {'1': 'Header pin 1 (square pad)', '2': 'Header pin 2', '3': 'Header pin 3', '4': 'Header pin 4'},
                    {'current_per_pin_a': 3, 'voltage_v': 250}),
 '2N7002': ('N-channel logic-level low-side switch: gate from MCU via ~100R, 10k gate pull-down, load between supply and drain.',
            ['10k R gate to source (pull-down)', 'Optional 100R series gate R'],
            {'1': 'Gate; Vgs(th) 1-2.5 V', '2': 'Source; connect to GND for low-side switching', '3': 'Drain; connect to load'},
            {'vds_max_v': 60, 'id_max_ma': 115, 'vgs_max_v': 20}),
 '1N4148': ('Small-signal switching diode: flyback/clamp for small loads, reverse-polarity or signal steering. Not for power rectification.',
            [], {'1': 'Cathode (banded side)', '2': 'Anode'},
            {'vr_max_v': 75, 'if_max_ma': 150, 'trr_ns': 4}),
}

def sexpr(fp, rect, pads):
    lines = [f'(footprint "{fp}" (layer "F.Cu")',
             f'  (fp_rect (start {rect[0]} {rect[1]}) (end {rect[2]} {rect[3]}) (stroke (width 0.12) (type default)) (fill none) (layer "F.Fab"))']
    for p in pads:
        if p['through_hole']:
            lines.append(f'  (pad "{p["number"]}" thru_hole {p.get("shape","circle")} (at {p["x_mm"]} {p["y_mm"]}) (size {p["width_mm"]} {p["height_mm"]}) (drill {p["drill"]}) (layers "*.Cu" "*.Mask"))')
        else:
            lines.append(f'  (pad "{p["number"]}" smd rect (at {p["x_mm"]} {p["y_mm"]}) (size {p["width_mm"]} {p["height_mm"]}) (layers "F.Cu" "F.Paste" "F.Mask"))')
    lines.append(')')
    return '\n'.join(lines)

def smd(n, x, y, w, h):
    return {'number': n, 'x_mm': x, 'y_mm': y, 'width_mm': w, 'height_mm': h, 'through_hole': False}

soic = [smd(str(i+1), -3.5875, round(-1.905+1.27*i, 3), 1.625, 0.65) for i in range(4)] + \
       [smd(str(8-i), 3.5875, round(-1.905+1.27*i, 3), 1.625, 0.65) for i in range(4)]

NEW = [
 dict(part_id='Crystal_16MHz', category='clock', description='16 MHz SMD crystal, 3.2x2.5 mm, 4-pad (pins 2,4 are case ground)',
      footprint='Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm', rect=(-1.6, -1.25, 1.6, 1.25),
      pads=[smd('1', -1.1, 0.85, 1.4, 1.2), smd('2', 1.1, 0.85, 1.4, 1.2), smd('3', 1.1, -0.85, 1.4, 1.2), smd('4', -1.1, -0.85, 1.4, 1.2)],
      pins=[('1', 'X1'), ('2', 'GND'), ('3', 'X2'), ('4', 'GND')], lcsc='C13738', price=0.12,
      app='Parallel-resonant 16 MHz crystal for MCU oscillator. Connect X1/X2 across the MCU oscillator pins with a load capacitor from each to GND; keep traces short.',
      support=['~18pF C from X1 to GND', '~18pF C from X2 to GND (adjust to CL: C = 2*(CL - Cstray))'],
      funcs={'1': 'Crystal terminal X1', '2': 'Case ground; tie to GND', '3': 'Crystal terminal X2', '4': 'Case ground; tie to GND'},
      ratings={'frequency_mhz': 16, 'load_cap_pf': 20, 'tolerance_ppm': 20, 'drive_level_uw_max': 100}),
 dict(part_id='Button_Tactile_SMD', category='switch', description='SMD tactile push button SPST (TL3342), pads 1+1 and 2+2 internally joined',
      footprint='Button_Switch_SMD:SW_SPST_TL3342', rect=(-2.1, -1.6, 2.1, 1.6),
      pads=[smd('1', -3.15, -1.9, 1.7, 1), smd('1', 3.15, -1.9, 1.7, 1), smd('2', -3.15, 1.9, 1.7, 1), smd('2', 3.15, 1.9, 1.7, 1)],
      pins=[('1', 'A'), ('2', 'B')], lcsc='C318884', price=0.06,
      app='Momentary push button: one side to GND, other to an MCU input with internal or 10k pull-up (active low); debounce in firmware or with 100nF C.',
      support=['10k pull-up R to VCC (or MCU internal pull-up)', '100nF C across A-B for hardware debounce (optional)'],
      funcs={'1': 'Terminal A; both A pads are internally connected', '2': 'Terminal B; both B pads are internally connected; A-B shorts when pressed'},
      ratings={'current_ma': 50, 'voltage_v': 12, 'contact_resistance_mohm': 100}),
 dict(part_id='Buzzer_Magnetic', category='audio', description='Through-hole 12x9.5 mm magnetic buzzer, 7.6 mm pitch (needs oscillating drive)',
      footprint='Buzzer_Beeper:Buzzer_12x9.5RM7.6', rect=(-1.5, -4.75, 9.1, 4.75),
      pads=[dict(number='1', x_mm=0, y_mm=0, width_mm=2, height_mm=2, through_hole=True, shape='rect', drill=1),
            dict(number='2', x_mm=7.6, y_mm=0, width_mm=2, height_mm=2, through_hole=True, shape='circle', drill=1)],
      pins=[('1', '+'), ('2', '-')], lcsc='C94598', price=0.35,
      app='Passive magnetic transducer: drive with a ~2-4 kHz square wave from a 555/MCU PWM via a low-side N-MOSFET (e.g. 2N7002) with a flyback diode across it.',
      support=['N-MOSFET low-side switch (e.g. 2N7002)', '1N4148 flyback diode across buzzer (cathode to +)', '100R series R on MOSFET gate'],
      funcs={'1': 'Positive terminal; connect to supply', '2': 'Negative terminal; connect to MOSFET drain/GND'},
      ratings={'rated_v': 5, 'current_ma_max': 30, 'resonant_freq_hz': 2700, 'spl_db_at_10cm': 85}),
 dict(part_id='WS2812B', category='opto', description='WS2812B addressable RGB LED, 5x5 mm PLCC4',
      footprint='LED_SMD:LED_WS2812B_PLCC4_5.0x5.0mm_P3.2mm', rect=(-2.5, -2.5, 2.5, 2.5),
      pads=[smd('1', -2.45, -1.65, 1.5, 0.9), smd('2', -2.45, 1.65, 1.5, 0.9), smd('3', 2.45, 1.65, 1.5, 0.9), smd('4', 2.45, -1.65, 1.5, 0.9)],
      pins=[('1', 'VDD'), ('2', 'DOUT'), ('3', 'VSS'), ('4', 'DIN')], lcsc='C114586', price=0.09,
      app='Single-wire RGB LED at 800 kHz; chain DOUT to next DIN. Supply 3.7-5.3 V; data high level must be >= 0.7*VDD (use level shifter from 3.3 V MCU at 5 V).',
      support=['100nF C on VDD to GND per LED', '300-470R series R on DIN', 'Bulk 100uF+ C per strip supply'],
      funcs={'1': 'Supply, 3.7-5.3 V', '2': 'Data out; connect to next LED DIN', '3': 'Ground', '4': 'Data in; high >= 0.7*VDD'},
      ratings={'vdd_max_v': 5.3, 'vdd_min_v': 3.7, 'i_max_ma_per_led': 60, 'data_rate_khz': 800}),
 dict(part_id='ATtiny85_SOIC8', category='mcu', description='ATtiny85-20SU 8-bit AVR MCU, 8 KB flash, SOIC-8 (5.3 mm body)',
      footprint='Package_SO:SOIC-8_5.3x5.3mm_P1.27mm', rect=(-2.65, -2.65, 2.65, 2.65), pads=soic,
      pins=[('1', 'PB5/RESET'), ('2', 'PB3'), ('3', 'PB4'), ('4', 'GND'), ('5', 'PB0'), ('6', 'PB1'), ('7', 'PB2'), ('8', 'VCC')],
      lcsc='C89852', price=0.95,
      app='Small AVR MCU: 2.7-5.5 V, internal 8 MHz RC (no crystal needed). Program via ISP using RESET, PB0 (MOSI), PB1 (MISO), PB2 (SCK).',
      support=['100nF C on VCC to GND', '10k pull-up R on RESET to VCC', '6-pin ISP header (optional)'],
      funcs={'1': 'PB5/RESET; active-low reset (pull up 10k), GPIO if RESET fuse disabled', '2': 'PB3 GPIO / ADC3 / XTAL1',
             '3': 'PB4 GPIO / ADC2 / XTAL2', '4': 'Ground', '5': 'PB0 GPIO / MOSI / SDA / PWM OC0A',
             '6': 'PB1 GPIO / MISO / PWM OC0B', '7': 'PB2 GPIO / SCK / SCL / ADC1', '8': 'Supply, 2.7-5.5 V'},
      ratings={'vcc_max_v': 5.5, 'vcc_min_v': 2.7, 'i_per_pin_ma': 40, 'f_max_mhz_at_4v5': 20}),
 dict(part_id='Capacitor_Electrolytic', category='passive', description='SMD polarized electrolytic capacitor 6.3x5.4 mm (e.g. 100uF 16V)',
      footprint='Capacitor_SMD:CP_Elec_6.3x5.4', rect=(-3.3, -3.3, 3.3, 3.3),
      pads=[smd('1', -2.8, 0, 3.5, 1.6), smd('2', 2.8, 0, 3.5, 1.6)],
      pins=[('1', '+'), ('2', '-')], lcsc='C3342', price=0.10,
      app='Bulk decoupling/reservoir on power rails; observe polarity (+ to higher potential). Use voltage rating >= 1.5x rail.',
      support=['100nF ceramic C in parallel for high-frequency decoupling'],
      funcs={'1': 'Positive terminal (marked side)', '2': 'Negative terminal'},
      ratings={'voltage_v': 16, 'capacitance_uf': 100, 'temp_max_c': 105}),
]

def main():
    d = json.loads(PATH.read_text())
    for n in NEW:
        pins = [{'number': a, 'name': b} for a, b in n['pins']]
        pads = [{k: v for k, v in p.items() if k not in ('shape', 'drill')} for p in n['pads']]
        d[n['part_id']] = {'part_id': n['part_id'], 'category': n['category'], 'description': n['description'],
            'footprint': n['footprint'], 'pins': pins, 'pads': pads,
            'footprint_sexpr': sexpr(n['footprint'], n['rect'], n['pads']), 'lcsc': n['lcsc'], 'price_usd': n['price'],
            'spice_model': None, 'spice_pin_map': {p['number']: int(p['number']) for p in pins}, 'footprint_source': SRC}
        INFO[n['part_id']] = (n['app'], n['support'], n['funcs'], n['ratings'])
    for pid, part in d.items():
        app, support, funcs, ratings = INFO[pid]
        part['typical_application'], part['required_support_parts'], part['max_ratings'] = app, support, ratings
        for pin in part['pins']:
            pin['function'] = funcs[pin['number']]
    PATH.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')

if __name__ == '__main__':
    main()
