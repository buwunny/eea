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

def sexpr(fp, rect, pads, holes=()):
    lines = [f'(footprint "{fp}" (layer "F.Cu")',
             f'  (fp_rect (start {rect[0]} {rect[1]}) (end {rect[2]} {rect[3]}) (stroke (width 0.12) (type default)) (fill none) (layer "F.Fab"))']
    for x, y, d in holes:  # unplated alignment-peg holes (no net, not listed in pads)
        lines.append(f'  (pad "" np_thru_hole circle (at {x} {y}) (size {d} {d}) (drill {d}) (layers "*.Cu" "*.Mask"))')
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
 dict(part_id='Crystal_16MHz', category='clock', description='16 MHz SMD crystal (YXC X322516MLB4SI), 3.2x2.5 mm, 4-pad (pins 2,4 are case ground), 9 pF load',
      footprint='Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm', rect=(-1.6, -1.25, 1.6, 1.25),
      pads=[smd('1', -1.1, 0.85, 1.4, 1.2), smd('2', 1.1, 0.85, 1.4, 1.2), smd('3', 1.1, -0.85, 1.4, 1.2), smd('4', -1.1, -0.85, 1.4, 1.2)],
      pins=[('1', 'X1'), ('2', 'GND'), ('3', 'X2'), ('4', 'GND')], lcsc='C13738', price=0.12,
      app='Parallel-resonant 16 MHz crystal for MCU oscillator. Connect X1/X2 across the MCU oscillator pins with a load capacitor from each to GND; keep traces short.',
      support=['~18pF C from X1 to GND', '~18pF C from X2 to GND (adjust to CL: C = 2*(CL - Cstray))'],
      funcs={'1': 'Crystal terminal X1', '2': 'Case ground; tie to GND', '3': 'Crystal terminal X2', '4': 'Case ground; tie to GND'},
      ratings={'frequency_mhz': 16, 'load_cap_pf': 9, 'tolerance_ppm': 10, 'drive_level_uw_max': 100}),
 dict(part_id='Button_Tactile_SMD', category='switch', description='SMD tactile push button SPST (E-Switch TL3342F160QG, 5.2x5.2 mm), pads 1+1 and 2+2 internally joined',
      footprint='Button_Switch_SMD:SW_SPST_TL3342', rect=(-2.1, -1.6, 2.1, 1.6),
      pads=[smd('1', -3.15, -1.9, 1.7, 1), smd('1', 3.15, -1.9, 1.7, 1), smd('2', -3.15, 1.9, 1.7, 1), smd('2', 3.15, 1.9, 1.7, 1)],
      pins=[('1', 'A'), ('2', 'B')], lcsc='C2886898', price=0.25,
      app='Momentary push button: one side to GND, other to an MCU input with internal or 10k pull-up (active low); debounce in firmware or with 100nF C.',
      support=['10k pull-up R to VCC (or MCU internal pull-up)', '100nF C across A-B for hardware debounce (optional)'],
      funcs={'1': 'Terminal A; both A pads are internally connected', '2': 'Terminal B; both B pads are internally connected; A-B shorts when pressed'},
      ratings={'current_ma': 50, 'voltage_v': 12, 'contact_resistance_mohm': 100}),
 dict(part_id='Buzzer_Magnetic', category='audio', description='Through-hole 12 mm active magnetic buzzer (S&S SUN-12095-5VPA7.6), 7.6 mm pitch, built-in 2.7 kHz tone',
      footprint='Buzzer_Beeper:Buzzer_12x9.5RM7.6', rect=(-1.5, -4.75, 9.1, 4.75),
      pads=[dict(number='1', x_mm=0, y_mm=0, width_mm=2, height_mm=2, through_hole=True, shape='rect', drill=1),
            dict(number='2', x_mm=7.6, y_mm=0, width_mm=2, height_mm=2, through_hole=True, shape='circle', drill=1)],
      pins=[('1', '+'), ('2', '-')], lcsc='C360615', price=0.30,
      app='Active buzzer: sounds its own 2.7 kHz tone whenever powered. Switch it on/off at a slow rate (DC to ~20 Hz) from a 555 or MCU pin via a low-side N-MOSFET (e.g. 2N7002) with a flyback diode across it.',
      support=['N-MOSFET low-side switch (e.g. 2N7002)', '1N4148 flyback diode across buzzer (cathode to +)', '100R series R on MOSFET gate'],
      funcs={'1': 'Positive terminal; connect to supply', '2': 'Negative terminal; connect to MOSFET drain/GND'},
      ratings={'rated_v': 5, 'supply_v_range': '4-7', 'current_ma_max': 30, 'tone_freq_hz': 2700, 'spl_db_at_10cm': 85}),
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
 dict(part_id='Capacitor_Electrolytic', category='passive', description='SMD polarized electrolytic capacitor 6.3x5.4 mm (KNSCHA 100uF 16V)',
      footprint='Capacitor_SMD:CP_Elec_6.3x5.4', rect=(-3.3, -3.3, 3.3, 3.3),
      pads=[smd('1', -2.8, 0, 3.5, 1.6), smd('2', 2.8, 0, 3.5, 1.6)],
      pins=[('1', '+'), ('2', '-')], lcsc='C5246591', price=0.05,
      app='Bulk decoupling/reservoir on power rails; observe polarity (+ to higher potential). Use voltage rating >= 1.5x rail.',
      support=['100nF ceramic C in parallel for high-frequency decoupling'],
      funcs={'1': 'Positive terminal (marked side)', '2': 'Negative terminal'},
      ratings={'voltage_v': 16, 'capacitance_uf': 100, 'temp_max_c': 105}),
 # --- Added 2026-10-24: analog/sensor/control parts. LCSC ids checked against lcsc.com listings; prices are estimates.
 dict(part_id='LM358', category='ic', description='LM358 dual op-amp, SOIC-8 (onsemi LM358DR2G); single supply 3-32 V, output swings to about VCC-1.5 V',
      footprint='Package_SO:SOIC-8_3.9x4.9mm_P1.27mm', rect=(-1.95, -2.45, 1.95, 2.45), pads=soic,
      pins=[('1', 'OUT1'), ('2', 'IN1-'), ('3', 'IN1+'), ('4', 'GND'), ('5', 'IN2+'), ('6', 'IN2-'), ('7', 'OUT2'), ('8', 'VCC')],
      lcsc='C7950', price=0.07,
      app='Comparator or amplifier on one supply. Comparator: sensor divider on IN+, pot/reference divider on IN-. Non-inverting gain = 1 + Rf/Rg. Tie unused amp as a follower with IN+ to a mid-rail divider.',
      support=['100nF C on VCC to GND'],
      funcs={'1': 'Output A', '2': 'Inverting input A', '3': 'Non-inverting input A', '4': 'Ground / negative supply',
             '5': 'Non-inverting input B', '6': 'Inverting input B', '7': 'Output B', '8': 'Supply, 3-32 V'},
      ratings={'vcc_max_v': 32, 'vcc_min_v': 3, 'i_out_ma': 20, 'gbw_mhz': 1, 'output_high_v_below_vcc': 1.5},
      spice={'kind': 'subcircuit', 'name': 'EEALM358', 'subcircuit': '\n'.join([
          '.subckt EEALM358 OUT1 INN1 INP1 VN INP2 INN2 OUT2 VP',
          '* Behavioral LM358: 100 dB gain, 1 MHz GBW, 0.3 V/us slew, output between ground and VCC-1.5 V',
          'BI1 VN P1 I=max(min(0.1*(v(INP1)-v(INN1)), 4.8m), -4.8m)', 'RP1 P1 VN 1Meg', 'CP1 P1 VN 15.9n',
          'DH1 P1 VP DCLAMP', 'DL1 VN P1 DCLAMP',
          'BO1 O1 VN V=max(min(v(P1,VN), v(VP,VN)-1.5), 0.005)', 'RO1 O1 OUT1 50',
          'BI2 VN P2 I=max(min(0.1*(v(INP2)-v(INN2)), 4.8m), -4.8m)', 'RP2 P2 VN 1Meg', 'CP2 P2 VN 15.9n',
          'DH2 P2 VP DCLAMP', 'DL2 VN P2 DCLAMP',
          'BO2 O2 VN V=max(min(v(P2,VN), v(VP,VN)-1.5), 0.005)', 'RO2 O2 OUT2 50',
          '.model DCLAMP D(Is=1e-14 N=1)',
          'RQ VP VN 7k', '.ends'])}),
 dict(part_id='MMBT3904', category='transistor', description='MMBT3904 NPN transistor, SOT-23 (JSCJ); 40 V, 200 mA, hFE 100-300',
      footprint='Package_TO_SOT_SMD:SOT-23', rect=(-0.65, -1.45, 0.65, 1.45),
      pads=[smd('1', -0.95, 1, 1, 0.6), smd('2', 0.95, 1, 1, 0.6), smd('3', 0, -1, 1, 0.6)],
      pins=[('1', 'B'), ('2', 'E'), ('3', 'C')], lcsc='C20526', price=0.01,
      app='Low-side switch for LEDs, buzzers or relays up to ~100 mA: base resistor ~1-10k from the driving signal, emitter to GND, load between supply and collector.',
      support=['Base resistor (1k-10k) from the drive signal', 'Optional 100k base-to-emitter pull-down'],
      funcs={'1': 'Base; drive through a resistor', '2': 'Emitter; connect to GND for low-side switching', '3': 'Collector; connect to load'},
      ratings={'vceo_max_v': 40, 'ic_max_ma': 200, 'pd_mw': 200, 'hfe_min': 100},
      spice={'kind': 'bjt', 'model': '.model EEA3904 NPN(Is=6.734f Xti=3 Eg=1.11 Vaf=74.03 Bf=416.4 Ne=1.259 Ise=6.734f Ikf=66.78m Xtb=1.5 Br=.7371 Nc=2 Rc=1 Cjc=3.638p Mjc=.3085 Vjc=.75 Fc=.5 Cje=4.493p Mje=.2593 Vje=.75 Tr=239.5n Tf=301.2p Itf=.4 Vtf=4 Xtf=2 Rb=10)'}),
 dict(part_id='NTC_10k_0805', category='sensor', description='10k NTC thermistor, 0805, B25/50 = 3950 K (FH CMFB103F3950FANT)',
      footprint='Resistor_SMD:R_0805_2012Metric', rect=(-1, -0.625, 1, 0.625),
      pads=[smd('1', -0.95, 0, 1, 1.45), smd('2', 0.95, 0, 1, 1.45)],
      pins=[('1', '1'), ('2', '2')], lcsc='C51597', price=0.02,
      app='Temperature sensing: put it in a divider with a 10k fixed resistor; the midpoint voltage falls as temperature rises. R(T) = 10k * exp(3950 * (1/T - 1/298.15)). Simulates as its 25 C value unless value gives another resistance.',
      support=['10k fixed resistor to form a divider'],
      funcs={'1': 'Terminal 1 (non-polarized)', '2': 'Terminal 2 (non-polarized)'},
      ratings={'r25_ohms': 10000, 'b_value_k': 3950, 'temp_min_c': -40, 'temp_max_c': 125},
      spice={'kind': 'R', 'default_ohms': 10000}),
 dict(part_id='LDR_GL5528', category='sensor', description='GL5528 photoresistor (LDR), 5 mm through-hole, 3.4 mm pitch (JCHL GL5528(10-20)); 8-20k at 10 lux, about 1M dark',
      footprint='OptoDevice:R_LDR_5.1x4.3mm_P3.4mm_Vertical', rect=(-0.85, -2.15, 4.25, 2.15),
      pads=[dict(number='1', x_mm=0, y_mm=0, width_mm=1.5, height_mm=1.5, through_hole=True, shape='circle', drill=0.8),
            dict(number='2', x_mm=3.4, y_mm=0, width_mm=1.5, height_mm=1.5, through_hole=True, shape='circle', drill=0.8)],
      pins=[('1', '1'), ('2', '2')], lcsc='C10081', price=0.03,
      app='Light sensing: divider with a ~10k fixed resistor feeding a comparator or ADC; resistance drops in light. Simulates as its value (use about 10k for light, 1Meg for dark).',
      support=['~10k fixed resistor to form a divider'],
      funcs={'1': 'Terminal 1 (non-polarized)', '2': 'Terminal 2 (non-polarized)'},
      ratings={'v_max': 150, 'p_max_mw': 100, 'r_light_10lux_ohms': '8k-20k', 'r_dark_ohms': 1000000},
      spice={'kind': 'R', 'default_ohms': 10000}),
 dict(part_id='Potentiometer_10k', category='passive', description='10k single-turn SMD trimmer potentiometer, 3.8x3.6 mm (Bourns TC33X-2-103E)',
      footprint='Potentiometer_SMD:Potentiometer_Bourns_TC33X_Vertical', rect=(-1.9, -1.8, 1.9, 1.8),
      pads=[smd('1', -1.8, -1.0, 1.2, 1.2), smd('2', 1.45, 0.0, 1.5, 1.6), smd('3', -1.8, 1.0, 1.2, 1.2)],
      pins=[('1', 'END1'), ('2', 'WIPER'), ('3', 'END2')], lcsc='C719176', price=0.30,
      app='Adjustable threshold or divider: ends across the supply, wiper gives 0-VCC (e.g. comparator reference). As a variable resistor, use one end plus the wiper. Simulates with the wiper at mid-travel.',
      support=[],
      funcs={'1': 'Track end 1', '2': 'Wiper', '3': 'Track end 2'},
      ratings={'r_total_ohms': 10000, 'power_mw': 100, 'tolerance_pct': 25},
      spice={'kind': 'potentiometer', 'default_ohms': 10000}),
 dict(part_id='Switch_Slide_SPDT', category='switch', description='SPDT SMD slide switch, 8x2.8 mm right-angle (Shouhan MSK12C02); 50 mA, 12 V',
      footprint='Button_Switch_SMD:SW_SPDT_Shouhan_MSK12C02', rect=(-3.35, -1.4, 3.35, 1.4),
      pads=[smd('1', -2.25, -1.95, 0.6, 1.3), smd('2', 0.75, -1.95, 0.6, 1.3), smd('3', 2.25, -1.95, 0.6, 1.3),
            smd('SH', -3.675, -1.1, 1.05, 0.7), smd('SH', -3.675, 1.1, 1.05, 0.7), smd('SH', 3.675, -1.1, 1.05, 0.7), smd('SH', 3.675, 1.1, 1.05, 0.7)],
      holes=[(-1.5, 0, 0.85), (1.5, 0, 0.85)],
      pins=[('1', 'A'), ('2', 'COM'), ('3', 'B'), ('SH', 'SH')], lcsc='C431540', price=0.03,
      app='Power or mode select. 50 mA rating: fine for signals and small loads, too low to switch a whole board; for power, switch a MOSFET gate instead. Simulates with COM connected to A.',
      support=[],
      funcs={'1': 'Throw A; connected to COM in one position', '2': 'Common', '3': 'Throw B; connected to COM in the other position',
             'SH': 'Mounting tabs (mechanical); leave unconnected or tie to GND'},
      ratings={'current_ma': 50, 'voltage_v': 12},
      spice={'kind': 'switch'},
      pin_map={'1': 1, '2': 2, '3': 3, 'SH': 4}),
 dict(part_id='ESP32_C3_WROOM_02', category='mcu', description='ESP32-C3-WROOM-02-N4 WiFi + BLE module, RISC-V 160 MHz, 4 MB flash, PCB antenna, 18x20 mm (Espressif)',
      footprint='RF_Module:ESP32-C3-WROOM-02', rect=(-9, -12.8, 9, 7.2),
      pads=[smd(str(i + 1), -8.75, round(-6 + 1.5 * i, 2), 1.5, 0.9) for i in range(9)] +
           [smd(str(10 + i), 8.75, round(6 - 1.5 * i, 2), 1.5, 0.9) for i in range(9)] + [smd('19', 0.96, 0.2, 2.9, 2.9)],
      pins=[('1', '3V3'), ('2', 'EN'), ('3', 'IO4'), ('4', 'IO5'), ('5', 'IO6'), ('6', 'IO7'), ('7', 'IO8'), ('8', 'IO9'), ('9', 'GND'),
            ('10', 'IO10'), ('11', 'IO20'), ('12', 'IO21'), ('13', 'IO18'), ('14', 'IO19'), ('15', 'IO3'), ('16', 'IO2'), ('17', 'IO1'), ('18', 'IO0'), ('19', 'GND')],
      lcsc='C2934560', price=2.0,
      app='WiFi/BLE MCU on 3.3 V (feed from AMS1117_3V3). Put the antenna end at the board edge with no copper under it. EN needs 10k pull-up + 1uF to GND. IO9 low at reset enters download mode; IO18/IO19 are native USB D-/D+.',
      support=['10uF + 100nF C on 3V3 to GND', '10k R pull-up on EN plus 1uF C from EN to GND', '10k R pull-up on IO9 (boot strap)', 'Button from IO9 to GND for download mode (optional)'],
      funcs={'1': '3.3 V supply (3.0-3.6 V, ~350 mA peaks)', '2': 'Chip enable; high = run. 10k pull-up + 1uF to GND', '3': 'GPIO4 / ADC1_CH4',
             '4': 'GPIO5', '5': 'GPIO6', '6': 'GPIO7', '7': 'GPIO8; strapping pin, keep high at boot (LED is OK)', '8': 'GPIO9; strapping pin, low at reset = download mode',
             '9': 'Ground', '10': 'GPIO10', '11': 'GPIO20 / UART0 RX', '12': 'GPIO21 / UART0 TX', '13': 'GPIO18 / USB D-', '14': 'GPIO19 / USB D+',
             '15': 'GPIO3 / ADC1_CH3', '16': 'GPIO2; strapping pin, keep high at boot', '17': 'GPIO1 / ADC1_CH1', '18': 'GPIO0 / ADC1_CH0', '19': 'Exposed ground pad'},
      ratings={'vdd_min_v': 3.0, 'vdd_max_v': 3.6, 'peak_current_ma': 350, 'gpio_max_v': 3.6, 'flash_mb': 4}),
]

def main():
    d = json.loads(PATH.read_text())
    for n in NEW:
        pins = [{'number': a, 'name': b} for a, b in n['pins']]
        pads = [{k: v for k, v in p.items() if k not in ('shape', 'drill')} for p in n['pads']]
        d[n['part_id']] = {'part_id': n['part_id'], 'category': n['category'], 'description': n['description'],
            'footprint': n['footprint'], 'pins': pins, 'pads': pads,
            'footprint_sexpr': sexpr(n['footprint'], n['rect'], n['pads'], n.get('holes', ())), 'lcsc': n['lcsc'], 'price_usd': n['price'],
            'spice_model': n.get('spice'), 'spice_pin_map': n.get('pin_map') or {p['number']: int(p['number']) for p in pins}, 'footprint_source': SRC}
        INFO[n['part_id']] = (n['app'], n['support'], n['funcs'], n['ratings'])
    for pid, part in d.items():
        app, support, funcs, ratings = INFO[pid]
        part['typical_application'], part['required_support_parts'], part['max_ratings'] = app, support, ratings
        part['footprint_review'] = {
            'status': 'unreviewed',
            'source': part.get('footprint_source', SRC),
            'manufacturer_drawing': None,
            'reviewed_by': None,
            'limitations': ['Verify land pattern against manufacturer drawing before fabrication',
                            'Courtyard and mechanical keepouts are not certified'],
        }
        if pid == 'ESP32_C3_WROOM_02':
            part['placement_requirements'] = ['Place antenna at board edge',
                'Manufacturer antenna keepout must be reviewed; no automatic RF keepout is claimed']
        for pin in part['pins']:
            pin['function'] = funcs[pin['number']]
    PATH.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')

if __name__ == '__main__':
    main()
