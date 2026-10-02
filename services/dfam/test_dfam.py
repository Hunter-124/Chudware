"""Hermetic, consumer-visible DFAM thresholds and model-frame measurements."""
from __future__ import annotations
from dataclasses import replace
import time
import numpy as np
import pytest
import trimesh
from . import FaceInfo, PrintMesh, PrintParams, analyze_printability, frame_for_build_dir, load_mesh_file


def box(size, center=(0,0,0)):
    mesh = trimesh.creation.box(extents=size)
    mesh.apply_translation(center)
    return mesh


def mesh_of(*parts, attributed=False):
    joined = trimesh.util.concatenate(parts)
    mesh = PrintMesh.from_arrays(joined.vertices, joined.faces)
    if attributed:
        mesh.faces = [FaceInfo("plane:1",1,0,"extrude:1")]
    return mesh


def check(mesh, name, params=None, **kwargs):
    result = analyze_printability(mesh, params or PrintParams(), checks=[name], **kwargs)
    return result["checks"][0]


def slot(width):
    return mesh_of(box((2,10,10),(-width/2-1,0,5)),
                   box((2,10,10),(width/2+1,0,5)),
                   box((width+4,10,2),(0,0,11)), attributed=True)


def annulus_hole(diameter, horizontal=False):
    tube = trimesh.creation.annulus(r_min=diameter/2, r_max=5, height=10, sections=64)
    normals = tube.face_normals
    triangles = tube.triangles_center
    inner = (np.abs(normals[:,2]) < .1) & (np.einsum("ij,ij->i", normals[:,:2], triangles[:,:2]) < 0)
    if horizontal:
        tube.apply_transform(trimesh.transformations.rotation_matrix(np.pi/2,[1,0,0]))
    return PrintMesh(tube.vertices, tube.faces, np.where(inner,1,0),
                     [FaceInfo("outer",1,0,"base"),FaceInfo("hole",1,1,"bore")])


def test_thin_vertical_plate_fails_and_reports_model_location():
    m = mesh_of(box((.6,20,20),(10,30,40)), attributed=True)
    result = check(m,"min_wall")
    assert result["status"] == "fail"
    assert result["value_mm"] == pytest.approx(.6, abs=.05)
    assert result["limit_mm"] == 1.
    assert result["regions"][0]["classification"] == "vertical_wall"
    center = result["regions"][0]["centroid"]
    assert np.allclose(center,[10,30,40], atol=1)
    assert result["regions"][0]["faces"] == ["plane:1"]
    assert result["regions"][0]["built_by"] == ["extrude:1"]


def test_knife_edge_wedge_warns_instead_of_failing_as_a_wall():
    # A 30 mm tall prism whose 40° tip runs to zero thickness: the slicer feathers the last
    # fraction of a millimetre, which is not a wall that is too thin to print.
    half = np.tan(np.radians(20)) * 30
    profile = [(0, 0), (30, -half), (30, half)]
    vertices = np.array([[x, y, z] for z in (0, 30) for x, y in profile], dtype=float)
    faces = np.array([[0, 2, 1], [3, 4, 5], [0, 1, 4], [0, 4, 3], [1, 2, 5], [1, 5, 4],
                      [2, 0, 3], [2, 3, 5]])
    wedge = trimesh.Trimesh(vertices, faces, process=False)
    wedge.fix_normals()
    result = check(mesh_of(wedge, attributed=True), "min_wall")
    assert result["status"] == "warn", result["note"]
    assert result["metrics"]["knife_edge_area_mm2"] > 0
    assert {r["classification"] for r in result["regions"]} == {"knife_edge"}


def test_cube_passes_and_is_deterministic():
    m = mesh_of(box((20,20,20)), attributed=True)
    p = PrintParams(bed_size_mm=(220,220,250))
    a=analyze_printability(m,p)
    b=analyze_printability(m,p)
    assert a == b
    assert a["status"] == "pass"
    assert all(r["status"] == "pass" for r in a["checks"])
    assert a["estimate"]["part_volume_mm3"] == pytest.approx(8000)
    assert a["estimate"]["support_volume_mm3"] == 0


def test_cantilever_is_not_a_bridge_and_support_envelope():
    m=mesh_of(box((2,10,10),(0,0,5)),box((12,10,2),(5,0,11)),attributed=True)
    p=PrintParams(max_bridge_mm=10)
    result=analyze_printability(m,p,checks=["bridges","support","overhang"])
    rows={r["check"]:r for r in result["checks"]}
    assert rows["bridges"]["metrics"]["bridge_area_mm2"] == 0
    assert rows["support"]["metrics"]["support_volume_mm3"] == pytest.approx(1000,rel=.1)
    assert rows["overhang"]["metrics"]["area_mm2"] == pytest.approx(100,rel=.1)
    assert rows["support"]["metrics"]["enclosed_support_mm3"] == 0


def test_bridge_clean_sag_warning_and_failure_tiers():
    m=slot(6)
    clean=check(m,"bridges",PrintParams(max_bridge_mm=10))
    sag=check(m,"bridges",PrintParams(max_bridge_mm=4))
    fail=check(m,"bridges",PrintParams(max_bridge_mm=2))
    assert clean["status"] == "pass"
    assert clean["metrics"]["max_span_mm"] == pytest.approx(6,abs=.3)
    assert sag["status"] == "warn"
    assert fail["status"] == "fail"
    assert check(m,"overhang",PrintParams(max_bridge_mm=10))["metrics"]["area_mm2"] == 0
    assert check(m,"overhang",PrintParams(max_bridge_mm=2))["metrics"]["area_mm2"] > 40


def test_sealed_void_and_unremovable_ceiling_support():
    outer=box((20,20,20))
    inner=box((10,10,10))
    inner.invert()
    m=mesh_of(outer,inner,attributed=True)
    result=check(m,"internal_voids",PrintParams(max_bridge_mm=4))
    assert result["metrics"]["sealed_cavities"] == 1
    assert result["metrics"]["void_volume_mm3"] == pytest.approx(1000,rel=.1)
    assert result["status"] == "fail"
    sla=check(m,"internal_voids",PrintParams(process="sla"))
    assert sla["status"] == "fail"
    assert "drain" in sla["fix"][0]
    bridgeable=check(m,"internal_voids",PrintParams(max_bridge_mm=12))
    assert bridgeable["status"] == "warn"


def test_vertical_hole_thresholds_and_raw_mesh_reason():
    assert check(annulus_hole(.8),"small_holes")["status"] == "fail"
    warning=check(annulus_hole(2.5),"small_holes")
    assert warning["status"] == "warn"
    assert warning["regions"][0]["diameter_mm"] == pytest.approx(2.5,abs=.03)
    assert check(annulus_hole(4),"small_holes")["status"] == "pass"
    raw=check(mesh_of(box((20,20,20))),"small_holes")
    assert raw["status"] == "not_evaluated"
    assert "Raw mesh" in raw["note"]


def test_small_horizontal_hole_excluded_from_support():
    tube=annulus_hole(4,horizontal=True)
    row=check(tube,"overhang")
    assert row["metrics"]["self_supporting_holes"][0]["diameter_mm"] == pytest.approx(4,abs=.03)
    raw=PrintMesh.from_arrays(tube.vertices,tube.triangles)
    fitted=check(raw,"overhang")
    assert fitted["metrics"]["self_supporting_holes"][0]["diameter_mm"] == pytest.approx(4,abs=.03)
    assert check(raw,"small_holes")["status"] == "not_evaluated"


def test_pillar_and_warp_adhesion_warnings():
    result=check(mesh_of(box((5,5,60))),"bed_adhesion")
    assert result["status"] == "warn"
    assert result["metrics"]["contact_area_mm2"] == 25
    assert result["metrics"]["tip_ratio"] == 12
    assert check(mesh_of(box((160,20,10))),"bed_adhesion",PrintParams(material="ABS"))["metrics"]["warp_risk"]
    assert not check(mesh_of(box((160,20,10))),"bed_adhesion",PrintParams(material="ABS",process="sla"))["metrics"]["warp_risk"]


def test_build_volume_uses_diagonal_yaw_and_height_limit():
    p=PrintParams(bed_size_mm=(220,220,250))
    assert check(mesh_of(box((300,20,10))),"build_volume",p)["status"] == "fail"
    fitted=check(mesh_of(box((280,5,10))),"build_volume",p)
    assert fitted["status"] == "pass"
    assert not fitted["metrics"]["fits_at_current_yaw"]
    assert abs(fitted["metrics"]["yaw_adjustment_deg"]) > 10
    assert check(mesh_of(box((20,20,260))),"build_volume",p)["status"] == "fail"


def test_process_semantics_and_check_validation():
    m=mesh_of(box((20,20,20)),attributed=True)
    sls=analyze_printability(m,PrintParams(process="sls"))
    rows={r["check"]:r for r in sls["checks"]}
    assert all(rows[k]["status"] == "not_applicable" for k in ("overhang","support","bridges"))
    other=analyze_printability(m,PrintParams(process="cnc"))
    assert all(r["status"] == "not_applicable" for r in other["checks"])
    thin=mesh_of(box((.3,20,20)),attributed=True)
    p=PrintParams(min_wall_mm=.2,nozzle_mm=.4)
    assert check(thin,"min_wall",p)["status"] == "fail"
    for process in ("sla","sls"):
        assert check(thin,"min_wall",replace(p,process=process))["status"] == "pass"
    with pytest.raises(ValueError,match="unknown print checks"):
        analyze_printability(m,PrintParams(),checks=["unknown"])


def test_mesh_loading_and_doc_face_join(tmp_path):
    original=box((20,20,20))
    path=tmp_path/"cube.stl"
    original.export(path)
    loaded=load_mesh_file(path)
    assert check(loaded,"bed_adhesion")["metrics"]["contact_area_mm2"] == 400
    doc={"pos":original.vertices,"idx":original.faces,"faces":[{"body":3,"face_id":7,"first_tri":0,"tri_count":12,"surf_type":0}]}
    m=PrintMesh.from_doc_mesh(doc,[{"kind":"face","sid":"f7","body":3,"topo":7,"owner_feature":"base"}])
    assert m.faces[0].sid == "f7"
    assert m.tri_face.tolist() == [0]*12
    assert PrintMesh.from_doc_mesh(doc,[]).faces[0].sid is None


def test_rotation_is_model_to_build_and_antiparallel_stable():
    for d in ([0,0,1],[0,0,-1],[1,2,3]):
        r=frame_for_build_dir(d,30)
        assert np.allclose(r@ (np.array(d)/np.linalg.norm(d)),[0,0,1])
        assert np.allclose(r@r.T,np.eye(3))
        assert np.linalg.det(r) == pytest.approx(1)


def test_fifty_thousand_triangles_bounded_runtime():
    parts=[]
    for x in (-25,0,25):
        sphere=trimesh.creation.icosphere(subdivisions=5,radius=10)
        sphere.apply_translation([x,0,0])
        parts.append(sphere)
    m=mesh_of(*parts)
    assert len(m.triangles) >= 50000
    start=time.perf_counter()
    result=analyze_printability(m,PrintParams())
    elapsed=time.perf_counter()-start
    assert elapsed < 15
    assert result["estimate"]["part_volume_mm3"] == pytest.approx(3*4/3*np.pi*1000,rel=.01)


def test_diagonal_bridge_reports_short_direction_span():
    m=slot(6)
    rotation=frame_for_build_dir([0,0,1],45)
    m.vertices=m.vertices @ rotation.T
    result=check(m,"bridges",PrintParams(max_bridge_mm=7))
    assert result["status"] == "pass"
    assert result["metrics"]["max_span_mm"] == pytest.approx(6,abs=.5)


def test_support_removal_access_width_and_model_landing():
    def chamber(opening):
        width=(14-opening)/2
        center=(14+opening)/4
        return mesh_of(box((14,14,2),(0,0,1)), box((14,14,2),(0,0,13)),
                       box((2,10,10),(-6,0,7)),box((2,10,10),(6,0,7)),
                       box((14,2,10),(0,6,7)),
                       box((width,2,10),(-center,-6,7)),
                       box((width,2,10),(center,-6,7)),attributed=True)
    p=PrintParams(max_bridge_mm=4,access_mm=3)
    narrow=check(chamber(1),"support",p)
    accessible=check(chamber(4),"support",p)
    assert narrow["status"] == "fail"
    assert narrow["metrics"]["enclosed_support_mm3"] > 100
    assert accessible["metrics"]["enclosed_support_mm3"] == 0
    assert accessible["status"] == "warn"
    assert accessible["metrics"]["landing_on_model_mm2"] > 50


def test_horizontal_skin_layers_and_informational_gap_fill():
    skin=check(mesh_of(box((20,20,.5))),"min_wall",
               PrintParams(min_wall_mm=.4,nozzle_mm=.3,layer_height_mm=.2))
    assert skin["status"] == "warn"
    assert skin["regions"][0]["layers"] == pytest.approx(2.5,abs=.01)
    gap=check(mesh_of(box((1.6,20,20))),"min_wall")
    assert gap["status"] == "pass"
    assert gap["metrics"]["gap_fill"]["status"] == "warn"
