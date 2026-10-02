"""Bounded subprocess behavior and real installed-slicer smoke scenarios."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from .contract import SliceRefusal
from .prusaslicer import SlicerInstall, discover, run_cli, slice_mesh, validate_overrides
from .vendor_bundle import ProfileCatalog
from .gcode_check import verify_gcode


def test_explicit_missing_binary_refuses_instead_of_falling_back(tmp_path):
    result = discover({'CHUDWARE_SLICER_BIN':str(tmp_path/'missing'),'PATH':os.defpath})
    assert isinstance(result,SliceRefusal)
    assert result.code=='slicer_not_installed'
    assert 'CHUDWARE_SLICER_BIN' in result.missing_prerequisite


def test_explicit_binary_identity_changes_with_content(tmp_path):
    executable = tmp_path/'prusa-slicer'
    executable.write_text(f'#!{sys.executable}\nprint("PrusaSlicer-2.9.6 based on Slic3r")\n')
    executable.chmod(0o755)
    profiles = tmp_path/'profiles'
    profiles.mkdir()
    env = {**os.environ,'CHUDWARE_SLICER_BIN':str(executable),'CHUDWARE_SLICER_PROFILES':str(profiles)}
    first = discover(env)
    assert isinstance(first,SlicerInstall)
    executable.write_text(executable.read_text()+'# changed binary\n')
    second = discover(env)
    assert first.identity != second.identity
    assert first.profiles_dir==profiles
    assert second.source=='env'


def test_deadline_refuses_and_preserves_warning_context(tmp_path):
    script = tmp_path/'warning.py'
    noise = '[2026-10-01 21:48:46.513399] [0x00007fd1acf0a1c0] [warning] Detected missing Voronoi vertex, input polygons will be rotated back and forth.'
    script.write_text(f'#!{sys.executable}\nprint("print warning: Detected print stability issues:")\nprint({noise!r})\nprint("Floating bridge anchors")\nprint("Consider enabling supports.")\nprint("90 => Exporting G-code")\n')
    script.chmod(0o755)
    fake = SlicerInstall('binary',(str(script),),'2.9.6',None,'env','test')
    outcome = run_cli(fake,[],work_dir=tmp_path/'run',grant_dirs=[])
    assert outcome.ok
    assert 'Floating bridge anchors' in outcome.warnings[0]
    assert 'Consider enabling supports' in outcome.warnings[0]
    assert noise in outcome.stdout_tail
    assert all('Voronoi' not in warning for warning in outcome.warnings)
    script.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(10)\n')
    start = time.monotonic()
    outcome = run_cli(fake,[],work_dir=tmp_path/'timeout',grant_dirs=[],timeout_s=.1)
    assert not outcome.ok
    assert 'slicer_timeout' in outcome.stderr_tail
    assert time.monotonic()-start<3


@pytest.fixture
def real_slicer():
    install = discover()
    if isinstance(install,SliceRefusal):
        pytest.skip(install.message)
    if install.profiles_dir is None:
        pytest.skip('Installed slicer has no vendor bundles')
    return install


def test_real_vendor_slice_copies_and_review_project(real_slicer,tmp_path):
    import trimesh
    catalog = ProfileCatalog(real_slicer.profiles_dir)
    profile = catalog.resolve('Creality Ender-3 (0.4 mm nozzle)')
    mesh = tmp_path/'cube.stl'
    trimesh.creation.box(extents=(8,8,2)).export(mesh)
    config = {**profile.config,'temperature':'220','first_layer_temperature':'220','bed_temperature':'60','first_layer_bed_temperature':'60'}
    result = slice_mesh(real_slicer,mesh_paths=[mesh],config=config,output_gcode=tmp_path/'cube.gcode',work_dir=tmp_path/'work',center_xy=profile.bed_center(),copies=2,export_3mf=tmp_path/'cube.3mf',timeout_s=30)
    assert result.ok, result.stderr_tail
    assert result.project_3mf.is_file()
    checked = verify_gcode(result.gcode_path,bed_polygon=profile.bed_polygon(),max_height_mm=profile.max_height(),material='PLA',nozzle_mm=.4,expect_nozzle_c=220,expect_bed_c=60)
    rows = {r['check']:r for r in checked['checks']}
    assert rows['bounds']['status']=='pass'
    assert rows['floating_extrusion']['status']=='pass'
    assert rows['temperatures']['status']=='pass'
    assert rows['end_state']['status']=='pass'
    assert rows['layer_heights']['status']=='pass'
    assert checked['stats']['height_mm']==pytest.approx(2,abs=.1)
    assert rows['first_layer']['metrics']['coverage_mm2'] > 125
    refused = slice_mesh(real_slicer,mesh_paths=[mesh],config={**config,'layre_height':'0.2'},output_gcode=tmp_path/'bad.gcode',work_dir=tmp_path/'bad',center_xy=profile.bed_center())
    assert not refused.ok
    assert refused.refusal.code=='unknown_config_key'
    assert 'layer_height' in refused.refusal.message
    assert validate_overrides(real_slicer,profile.config,{'temperature':'220','layer_height':'0.2'}) is None
    assert validate_overrides(real_slicer,profile.config,{'layre_height':'0.2'}).code=='unknown_config_key'


def test_real_t_shape_support_warning(real_slicer,tmp_path):
    import trimesh
    catalog = ProfileCatalog(real_slicer.profiles_dir)
    profile = catalog.resolve('Creality Ender-3 (0.4 mm nozzle)')
    stem = trimesh.creation.box(extents=(3,12,12))
    stem.apply_translation((0,0,6))
    top = trimesh.creation.box(extents=(45,12,3))
    top.apply_translation((0,0,13.5))
    mesh = tmp_path/'unsupported_t.stl'
    trimesh.util.concatenate([stem,top]).export(mesh)
    result = slice_mesh(real_slicer,mesh_paths=[mesh],config={**profile.config,'support_material':'0'},output_gcode=tmp_path/'t.gcode',work_dir=tmp_path/'work',center_xy=profile.bed_center(),timeout_s=30)
    assert result.ok,result.stderr_tail
    assert any('supports' in warning.lower() for warning in result.warnings),result.stdout_tail
    checked = verify_gcode(result.gcode_path,material='PLA',nozzle_mm=.4)
    row = next(r for r in checked['checks'] if r['check']=='floating_extrusion')
    assert row['status']=='fail'
    assert row['metrics']['worst_layer_area_mm2']>2
