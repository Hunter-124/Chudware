"""Consumer-visible profile selection, inheritance, and condition behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

from .vendor_bundle import ProfileCatalog, evaluate_condition, write_ini


@pytest.fixture
def catalog(tmp_path):
    text = '''[vendor]
name = Creality
[printer:*base*]
nozzle_diameter = 0.4
bed_shape = 3x3,228x3,228x228,3x228
max_print_height = 250
printer_notes = PRINTER_VENDOR_CREALITY
retract_length = 5
start_gcode = G28\\nM109 S220
[printer:*later*]
retract_length = 4
[printer:Creality Ender-3 (0.4 mm nozzle)]
inherits = *base*; *later*
printer_model = ENDER3
default_print_profile = 0.16 OPTIMAL
default_filament_profile = Generic PLA
[printer:Creality Ender-3 V2 (0.4 mm nozzle)]
inherits = *base*
printer_model = ENDER3V2
[printer:Creality Ender-3 Pro (0.4 mm nozzle)]
inherits = *base*
printer_model = ENDER3PRO
[printer:Creality Ender-3 (0.6 mm nozzle)]
inherits = *base*
nozzle_diameter = 0.6
[print:*base*]
compatible_printers_condition = printer_notes=~/.*CREALITY.*/ and nozzle_diameter[0]==0.4
[print:0.16 OPTIMAL]
inherits = *base*
layer_height = 0.16
[print:0.20 NORMAL]
inherits = *base*
layer_height = 0.2
[print:0.20 SPEED]
inherits = *base*
layer_height = 0.2
[print:V2 only]
compatible_printers_condition = printer_model == "ENDER3V2"
layer_height = 0.1
[filament:*PLA*]
filament_type = PLA
compatible_printers_condition = printer_notes=~/.*CREALITY.*/
[filament:Generic PLA]
inherits = *PLA*
temperature = 215
[filament:Fancy PLA]
inherits = *PLA*
temperature = 210
[filament:Generic PETG]
filament_type = PETG
'''
    (tmp_path/'test.ini').write_text(text)
    return ProfileCatalog(tmp_path)


def test_exact_model_beats_variants_and_nozzle_filter(catalog):
    rows = catalog.find_printers('ender 3', 0.4)
    assert rows[0]['name'] == 'Creality Ender-3 (0.4 mm nozzle)'
    assert all(r['nozzle_mm'] == 0.4 for r in rows)
    assert catalog.find_printers('ender 3 v2', .4)[0]['name'].endswith('V2 (0.4 mm nozzle)')


def test_resolve_defaults_and_layer_height_quality_ties(catalog, tmp_path):
    printer = 'Creality Ender-3 (0.4 mm nozzle)'
    profile = catalog.resolve(printer)
    assert profile.print_ == '0.16 OPTIMAL'
    assert profile.filament == 'Generic PLA'
    assert profile.config['retract_length'] == '4'
    assert profile.bed_center() == (115.5,115.5)
    assert profile.max_height() == 250
    assert catalog.default_print(printer, .2) == '0.20 NORMAL'
    assert catalog.default_filament(printer, 'PETG') == 'Generic PETG'
    assert 'V2 only' not in catalog.compatible_prints(printer)
    ini = tmp_path/'flat.ini'
    write_ini(profile.config, ini)
    assert 'start_gcode = G28\\nM109 S220\n' in ini.read_text()
    assert 'inherits =' not in ini.read_text()
    assert 'compatible_printers_condition =' not in ini.read_text()


@pytest.mark.parametrize('expression, expected', [
    ('nozzle_diameter[0] == 0.4 and not (printer_model == "V2")', True),
    ('nozzle_diameter[0] > 0.4 or printer_model != "ENDER3"', False),
    ('printer_notes =~ /CREALITY/ and nozzle_diameter[0] <= 0.4', True),
    ('printer_notes !~ /PRUSA/', True),
    ('unsupported_function(printer_model)', None),
    ('unknown_key == 3', None),
    ('nozzle_diameter[4] == 0.4', None),
])
def test_condition_subset_and_unknown(expression, expected):
    assert evaluate_condition(expression, {'nozzle_diameter':'0.4,0.6','printer_model':'ENDER3','printer_notes':'PRINTER_VENDOR_CREALITY'}) is expected


def test_cycles_refuse_and_unknown_conditions_report(tmp_path):
    (tmp_path/'cycle.ini').write_text('[vendor]\nname = Example\n[printer:A]\ninherits = B\n[printer:B]\ninherits = A\n')
    catalog = ProfileCatalog(tmp_path)
    with pytest.raises(ValueError, match='cycle'):
        catalog.find_printers('')
    (tmp_path/'cycle.ini').write_text('[vendor]\nname = Example\n[printer:A]\nprinter_model = A\n[print:normal]\ncompatible_printers_condition = new_function(printer_model)\n')
    catalog = ProfileCatalog(tmp_path)
    assert catalog.compatible_prints('A') == ['normal']
    assert 'unknown compatible_printers_condition' in catalog.warnings[0]
