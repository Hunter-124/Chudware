// OCCT linkage smoke test for Wave 2.
//
// Proves the auto-provisioned Open CASCADE kernel is found, linked, and runnable:
// build a box, compute its volume via BRepGProp (the mass-properties path the
// document evaluator uses), and tessellate it with BRepMesh_IncrementalMesh
// (the path geom/tessellate.cpp uses to emit AICM). A 2x3x4 box must have
// volume 24 and at least 12 triangles (6 faces x >=2 tris). Exit 0 on success.
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRep_Tool.hxx>
#include <Poly_Triangulation.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>

#include <cmath>
#include <cstdio>

int main() {
    const double L = 2.0, W = 3.0, H = 4.0;
    TopoDS_Shape box = BRepPrimAPI_MakeBox(L, W, H).Shape();

    GProp_GProps vprops;
    BRepGProp::VolumeProperties(box, vprops);
    const double vol = vprops.Mass();

    BRepMesh_IncrementalMesh mesher(box, 0.1);
    mesher.Perform();
    int ntri = 0;
    for (TopExp_Explorer ex(box, TopAbs_FACE); ex.More(); ex.Next()) {
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri =
            BRep_Tool::Triangulation(TopoDS::Face(ex.Current()), loc);
        if (!tri.IsNull()) ntri += tri->NbTriangles();
    }

    std::printf("OCCT box volume = %.6f (expected %.6f); triangles = %d\n",
                vol, L * W * H, ntri);
    const bool ok = std::fabs(vol - L * W * H) < 1e-6 && ntri >= 12;
    std::printf("%s\n", ok ? "OCCT SMOKE OK" : "OCCT SMOKE FAILED");
    return ok ? 0 : 1;
}
