"""Bounded subprocess behavior and real installed-slicer smoke scenarios."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from ..print_safety import UnsafePrintFile, atomic_print_output, validate_output_path
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


@pytest.mark.parametrize("hazard", ["post_process", "start_gcode", "output", "review"])
def test_slicing_refuses_firmware_outputs_and_maintenance_hooks(real_slicer, tmp_path, hazard):
    import trimesh
    profile = ProfileCatalog(real_slicer.profiles_dir).resolve('Creality Ender-3 (0.4 mm nozzle)')
    mesh = tmp_path / 'cube.stl'
    trimesh.creation.box(extents=(5, 5, 1)).export(mesh)
    firmware = tmp_path / 'firmware.bin'
    hook = tmp_path / 'hook.py'
    hook.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                    f'Path({str(firmware)!r}).write_bytes(b"invalid firmware")\n')
    hook.chmod(0o755)
    config = dict(profile.config)
    if hazard == 'post_process':
        config['post_process'] = str(hook)
    elif hazard == 'start_gcode':
        config['start_gcode'] = r'G28\nM997'
    result = slice_mesh(
        real_slicer, mesh_paths=[mesh], config=config,
        output_gcode=firmware if hazard == 'output' else tmp_path / 'cube.gcode',
        export_3mf=firmware if hazard == 'review' else None,
        work_dir=tmp_path / 'work', center_xy=profile.bed_center())
    assert result.ok is False
    assert result.refusal.code == ('unsafe_post_process' if hazard == 'post_process'
                                   else 'unsafe_print_file')
    assert not firmware.exists()
    assert not (tmp_path / 'cube.gcode').exists()


@pytest.mark.parametrize('artifact', ['config', 'review', 'gcode'])
@pytest.mark.parametrize('alias_kind', ['symlink', 'hardlink'])
def test_slice_refuses_aliased_firmware_before_starting_slicer(tmp_path, artifact, alias_kind):
    firmware = tmp_path / 'firmware.bin'
    firmware.write_bytes(b'original firmware')
    mesh = tmp_path / 'model.stl'
    mesh.write_bytes(b'mesh')
    work = tmp_path / 'work'
    work.mkdir()
    gcode = tmp_path / 'model.gcode'
    review = tmp_path / 'model.3mf'
    alias = {'config': work / 'config.ini', 'review': review, 'gcode': gcode}[artifact]
    if alias_kind == 'symlink':
        alias.symlink_to(firmware)
    else:
        alias.hardlink_to(firmware)
    marker = tmp_path / 'launched'
    executable = tmp_path / 'slicer'
    executable.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                          f'Path({str(marker)!r}).write_text("launched")\n')
    executable.chmod(0o755)
    install = SlicerInstall('binary', (str(executable),), '2.9.6', None, 'env', 'test')

    result = slice_mesh(install, mesh_paths=[mesh], config={}, output_gcode=gcode,
                        work_dir=work, center_xy=None, export_3mf=review)

    assert not result.ok
    assert result.refusal.code == 'unsafe_print_file'
    assert firmware.read_bytes() == b'original firmware'
    assert not marker.exists()




def test_atomic_print_output_rejects_new_alias_without_touching_firmware(tmp_path):
    firmware = tmp_path / 'firmware.bin'
    firmware.write_bytes(b'original firmware')
    output = tmp_path / 'model.gcode'
    with pytest.raises(UnsafePrintFile):
        with atomic_print_output(output) as stream:
            stream.write(b'G1 X1\n')
            assert list(tmp_path.glob('.chudware-print-*.part'))
            output.symlink_to(firmware)
    assert firmware.read_bytes() == b'original firmware'
    assert not list(tmp_path.glob('.chudware-print-*.part'))


def test_workdir_and_special_file_aliases_are_rejected(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    work = tmp_path / 'work'
    work.symlink_to(actual, target_is_directory=True)
    executable = tmp_path / 'slicer'
    marker = tmp_path / 'launched'
    executable.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                          f'Path({str(marker)!r}).write_text("launched")\n')
    executable.chmod(0o755)
    install = SlicerInstall('binary', (str(executable),), '2.9.6', None, 'env', 'test')
    mesh = tmp_path / 'mesh.stl'
    mesh.write_bytes(b'mesh')
    result = slice_mesh(install, mesh_paths=[mesh], config={}, output_gcode=tmp_path / 'a.gcode',
                        work_dir=work, center_xy=None)
    assert not result.ok
    assert result.refusal.code == 'unsafe_print_file'
    assert not marker.exists()
    if hasattr(os, 'mkfifo'):
        fifo = tmp_path / 'out.gcode'
        os.mkfifo(fifo)
        with pytest.raises(UnsafePrintFile):
            validate_output_path(fifo)


def test_real_vendor_slice_copies_and_review_project(real_slicer,tmp_path):
    import trimesh
    catalog = ProfileCatalog(real_slicer.profiles_dir)
    profile = catalog.resolve('Creality Ender-3 (0.4 mm nozzle)')
    mesh = tmp_path/'cube.stl'
    trimesh.creation.box(extents=(8,8,2)).export(mesh)
    config = {**profile.config,'temperature':'220','first_layer_temperature':'220','bed_temperature':'60','first_layer_bed_temperature':'60'}
    destination = tmp_path / 'destination'
    destination.mkdir()
    directory_alias = tmp_path / 'directory-alias'
    directory_alias.symlink_to(destination, target_is_directory=True)
    work = directory_alias / 'work'
    work.mkdir()
    result = slice_mesh(real_slicer,mesh_paths=[mesh],config=config,output_gcode=directory_alias/'cube.gcode',work_dir=work,center_xy=profile.bed_center(),copies=2,export_3mf=directory_alias/'cube.3mf',timeout_s=30)
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
