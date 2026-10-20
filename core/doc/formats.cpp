// File import/export implementation — see formats.h.
//
// Ports the neutral-B-rep AND the mesh lanes of engine/doc/formats.py onto OCCT's
// data-exchange + mesh toolkits. The Python oracle reaches the same readers/writers for
// the B-rep formats (OCP/build123d) and uses trimesh/lib3mf/rhino3dm for the mesh
// formats; this C++ side reaches OCCT's RWStl / RWObj / RWGltf instead. The parity gate
// re-imports a C++-written file through the Python oracle (trimesh) and compares the
// ROBUST mesh invariant (vertex/triangle count + bbox + volume) — never a byte compare.

// lib3mf (3MF read/write) — CONFINED to this TU. We use the header-only
// CppDynamic binding, which dlopens lib3mf.dll at runtime (CWrapper::loadLibrary)
// — no import lib on the link line, MSVC-safe, exactly like the Python lib3mf
// wheel (both wrap the SAME lib3mf.dll C ABI 2.5.0, so the mesh extraction is
// bit-identical). Guarded by CHUDWARE_WITH_LIB3MF so a build without the SDK
// provisioned still compiles (the 3MF paths then fall through to the loud "not
// supported" errors below).
//
// IMPORTANT: <windows.h> must be included FULL (no WIN32_LEAN_AND_MEAN) and
// BEFORE the OCCT headers, because (1) the binding calls MultiByteToWideChar /
// CP_UTF8 (winnls.h), which the lean macro strips, and (2) OCCT headers define
// WIN32_LEAN_AND_MEAN, which would poison a later include. Pulling it in first
// guarantees the full Win32 surface for the binding.
#ifdef CHUDWARE_WITH_LIB3MF
#ifdef _WIN32
// NOMINMAX: windows.h otherwise defines min()/max() macros that collide with
// std::max({...}) in the PLY reader below.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <dlfcn.h>  // dladdr: locate lib3mf.so beside this module (libchudware.so)
#endif
#include "lib3mf_dynamic.hpp"
#endif

// SolidWorks COM import (sw_export_step). Confined to this TU; Windows-only. Pulls full
// <windows.h> + the OLE Automation surface (IDispatch/VARIANT) and link-imports the COM
// runtime via #pragma so no CMakeLists change is needed. NOMINMAX keeps the PLY reader's
// std::max happy. Off Windows, sw_export_step throws the actionable "Save As STEP" error.
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <objbase.h>
#include <oleauto.h>
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#endif

// openNURBS (Rhino .3dm read/write) — CONFINED to this TU, guarded by
// CHUDWARE_WITH_OPENNURBS. opennurbs.h is the single umbrella header; it pulls in
// <windows.h> itself on Win32. NOMINMAX (so its includes don't redefine
// min/max) is set above when lib3mf is enabled; set it here too for an
// openNURBS-only build. The static lib (opennurbsStatic) carries the bundled
// zlib + Win32 system libs. We read the STORED ON_Mesh (m_V float32 / m_F
// quad/tri) — the SAME data the rhino3dm wheel exposes — so extraction is
// bit-identical to the Python oracle.
#ifdef CHUDWARE_WITH_OPENNURBS
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
// opennurbs.h is fine with the lean Win32 surface; it does not use the codepage
// helpers the lib3mf binding needs.
#endif
#endif
#include "opennurbs.h"
#endif

#include "doc/formats.h"
#include "doc/acis_read.h"
#include "doc/acis_write.h"
#include "doc/dwg.h"
#include "doc/dwg_native.h"
#include "doc/dxf.h"
#include "doc/solidworks_tessellation.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <random>

#ifndef _WIN32
#include <fcntl.h>  // O_EXCL reservation of the dwg2dxf output
#include <unistd.h>
#endif
#include <map>
#include <set>
#include <unordered_map>

#include <BRepAdaptor_Curve.hxx>
#include <BRepBuilderAPI_GTransform.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeSolid.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepBuilderAPI_Sewing.hxx>
#include <BRepLib.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepMesh_Context.hxx>
#include <BRepMesh_EdgeParameterProvider.hxx>
#include <IMeshData_Edge.hxx>
#include <IMeshData_Face.hxx>
#include <IMeshData_Model.hxx>
#include <IMeshData_ParametersListArrayAdaptor.hxx>
#include <IMeshData_Wire.hxx>
#include <BRep_Builder.hxx>
#include <Bnd_Box.hxx>
#include <Geom_Plane.hxx>
#include <BRep_Tool.hxx>
#include <BRepTools.hxx>
#include <BRepTools_ReShape.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <GCPnts_AbscissaPoint.hxx>
#include <GCPnts_TangentialDeflection.hxx>
#include <GeomAbs_CurveType.hxx>
#include <Geom_BSplineCurve.hxx>
#include <TColgp_Array1OfPnt.hxx>
#include <TColStd_Array1OfReal.hxx>
#include <TColStd_Array1OfInteger.hxx>
#include <gp_Elips.hxx>
#include <gp_Ax2.hxx>
#include <GProp_GProps.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <IGESControl_Controller.hxx>
#include <IGESControl_Reader.hxx>
#include <IGESControl_Writer.hxx>
#include <Interface_Static.hxx>
#include <Message_ProgressRange.hxx>
#include <Precision.hxx>
#include <OSD_Path.hxx>
#include <Poly_PolygonOnTriangulation.hxx>
#include <Poly_Triangulation.hxx>
#include <RWGltf_CafReader.hxx>
#include <RWGltf_CafWriter.hxx>
#include <RWObj.hxx>
#include <RWStl.hxx>
#include <STEPControl_Reader.hxx>
#include <STEPControl_Writer.hxx>
#include <ShapeAnalysis_FreeBounds.hxx>
#include <ShapeFix_Shape.hxx>
#include <ShapeFix_Solid.hxx>
#include <Poly_Triangle.hxx>
#include <TColStd_IndexedDataMapOfStringString.hxx>
#include <TDF_LabelSequence.hxx>
#include <TDocStd_Document.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_FormatVersion.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopTools_HSequenceOfShape.hxx>
#include <TopTools_MapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shell.hxx>
#include <TopoDS_Solid.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <Standard_Failure.hxx>
#include <XCAFApp_Application.hxx>
#include <XCAFDoc_DocumentTool.hxx>
#include <XCAFDoc_ShapeTool.hxx>
#include <gp_Ax2.hxx>
#include <gp_Pln.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>
#include <gp_Trsf.hxx>
#include <gp_GTrsf.hxx>
#include <gp_Mat.hxx>
#include <gp_XYZ.hxx>

#include "doc/session.h"  // OpError

namespace chudware::doc {

namespace {

// Lower-case a string (extension matching).
std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

// Merge bodies into one compound (mirrors formats.py::_shapes_to_export_list); a single
// body is returned as-is.
TopoDS_Shape as_compound(const std::vector<TopoDS_Shape>& bodies) {
    if (bodies.size() == 1) return bodies.front();
    TopoDS_Compound comp;
    BRep_Builder bb;
    bb.MakeCompound(comp);
    for (const TopoDS_Shape& s : bodies)
        if (!s.IsNull()) bb.Add(comp, s);
    return comp;
}

// Sew the export bodies into a watertight B-rep (shell promoted to SOLID when closed),
// used by the IGES writer. IGESControl_Writer in BRep mode (write.iges.brep.mode=1) emits
// a real manifold_solid_brep ONLY when it is handed a SOLID — a bare COMPOUND of loose
// FACEs writes as disconnected trimmed surfaces that DON'T sew back into a closed solid on
// re-import (the P2 bug: re-read came back as no body / an open shell of volume 0). Sewing
// here guarantees the writer sees one closed solid per lump. as_compound is kept for the
// face-mode/default callers; this is the BRep-mode preparation. A single already-solid body
// is returned unchanged; a truly open surface body falls through as the sewn shell so
// surfaces still export (just not as a closed solid).
TopoDS_Shape as_sewn_solid(const std::vector<TopoDS_Shape>& bodies) {
    // Collect the existing solids first — if every body is already a solid we hand them
    // straight to the writer (no need to re-sew good geometry).
    std::vector<TopoDS_Shape> solids;
    bool any_non_solid = false;
    for (const TopoDS_Shape& b : bodies) {
        if (b.IsNull()) continue;
        bool had_solid = false;
        for (TopExp_Explorer ex(b, TopAbs_SOLID); ex.More(); ex.Next()) {
            solids.push_back(ex.Current());
            had_solid = true;
        }
        if (!had_solid) any_non_solid = true;
    }
    if (!any_non_solid && !solids.empty())
        return as_compound(solids);  // already-solid bodies: write them as-is

    // Otherwise sew every face of every body into shells, then promote closed shells to
    // solids (the same as_solid path used by mesh_to_shape / IGES import). The tolerance
    // matches the IGES import sewer (0.01) so a body we just wrote re-sews on re-read.
    BRepBuilderAPI_Sewing sew(0.01);
    int added = 0;
    for (const TopoDS_Shape& b : bodies) {
        if (b.IsNull()) continue;
        sew.Add(b);
        ++added;
    }
    if (added == 0) return as_compound(bodies);
    sew.Perform();
    TopoDS_Shape sewn = sew.SewedShape();
    if (sewn.IsNull()) return as_compound(bodies);
    TopoDS_Compound out;
    BRep_Builder bb;
    bb.MakeCompound(out);
    int n_added = 0;
    for (TopExp_Explorer ex(sewn, TopAbs_SHELL); ex.More(); ex.Next()) {
        TopoDS_Shell shell = TopoDS::Shell(ex.Current());
        TopoDS_Shape unit = shell;
        if (BRep_Tool::IsClosed(shell)) {
            try {
                BRepBuilderAPI_MakeSolid ms(shell);
                if (ms.IsDone()) {
                    ShapeFix_Solid sfix;
                    TopoDS_Shape solid = sfix.SolidFromShell(shell);
                    unit = solid.IsNull() ? TopoDS_Shape(ms.Solid()) : solid;
                }
            } catch (const Standard_Failure&) {
                unit = shell;  // leave a problematic shell as a shell
            }
        }
        bb.Add(out, unit);
        ++n_added;
    }
    if (n_added == 0) return sewn;             // no shells (free faces only) — write what we sewed
    if (n_added == 1) {
        // Unwrap the single unit out of the compound so the writer sees a bare SOLID.
        for (TopExp_Explorer ex(out, TopAbs_SOLID); ex.More(); ex.Next()) return ex.Current();
        for (TopExp_Explorer ex(out, TopAbs_SHELL); ex.More(); ex.Next()) return ex.Current();
    }
    return out;
}

// True iff a re-read IGES shape carries at least one topologically-valid SOLID with a
// non-zero (closed) volume. This is the HONEST round-trip check for IGES: the writer can
// claim success while emitting trimmed surfaces that re-import as an open shell of volume 0,
// so we re-read the file and require a real closed solid before reporting validated.
bool iges_reread_has_closed_solid(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return false;
    bool any = false;
    for (TopExp_Explorer ex(shape, TopAbs_SOLID); ex.More(); ex.Next()) {
        const TopoDS_Shape solid = ex.Current();
        if (!BRepCheck_Analyzer(solid).IsValid()) continue;
        GProp_GProps props;
        BRepGProp::VolumeProperties(solid, props);
        if (std::abs(props.Mass()) > 1e-9) { any = true; break; }
    }
    return any;
}

// A zero-angle cusp between distinct tangent boundary curves can have overlapping
// first chords when OCCT gives both curves the same angular sampling. The face
// triangulator then loses the strip between them, although the B-rep is valid.
// Refine the longer overlapping chord ON ITS EXACT CURVE before triangulating any
// faces; all incident pcurves receive the same extra sample. This changes only the
// discrete boundary, never the topology, surface, or requested deflection bounds.
class ExportMeshContext final : public BRepMesh_Context {
public:
    Standard_Boolean HealModel() override {
        if (!BRepMesh_Context::HealModel()) return Standard_False;
        const auto& model = GetModel();
        for (int fi = 0; fi < model->FacesNb(); ++fi) {
            const auto& face = model->GetFace(fi);
            for (int wi = 0; wi < face->WiresNb(); ++wi) {
                const auto& wire = face->GetWire(wi);
                for (int ei = 0; ei < wire->EdgesNb(); ++ei) {
                    const int next = (ei + 1) % wire->EdgesNb();
                    const IMeshData::IEdgeHandle a = wire->GetEdge(ei);
                    const IMeshData::IEdgeHandle b = wire->GetEdge(next);
                    if (a == b || a->GetDegenerated() || b->GetDegenerated()) continue;
                    const auto& pa = a->GetPCurve(face.get(), wire->GetEdgeOrientation(ei));
                    const auto& pb = b->GetPCurve(face.get(), wire->GetEdgeOrientation(next));
                    if (pa->ParametersNb() < 2 || pb->ParametersNb() < 2) continue;
                    for (;;) {
                        const int la = pa->ParametersNb() - 1, lb = pb->ParametersNb() - 1;
                        int ia = 0, ib = 0;
                        double distance = std::numeric_limits<double>::max();
                        for (int x : {0, la}) for (int y : {0, lb}) {
                            const double d = pa->GetPoint(x).SquareDistance(pb->GetPoint(y));
                            if (d < distance) { distance = d; ia = x; ib = y; }
                        }
                        if (distance > Precision::PConfusion() * Precision::PConfusion()) break;
                        const int ja = ia == 0 ? 1 : la - 1;
                        const int jb = ib == 0 ? 1 : lb - 1;
                        const gp_XY da = pa->GetPoint(ja).XY() - pa->GetPoint(ia).XY();
                        const gp_XY db = pb->GetPoint(jb).XY() - pb->GetPoint(ib).XY();
                        const double length = std::max(da.Modulus(), db.Modulus());
                        // Opposite chords are an ordinary smooth junction, not a cusp.
                        if (da.Dot(db) <= 0.0 ||
                            std::abs(da.Crossed(db)) > Precision::PConfusion() * length) break;
                        const bool split_a = da.SquareModulus() >= db.SquareModulus();
                        split_boundary_chord(split_a ? a : b, split_a ? ia : ib,
                                             split_a ? ja : jb);
                    }
                }
            }
        }
        return Standard_True;
    }

private:
    void split_boundary_chord(const IMeshData::IEdgeHandle& edge, int end, int other) {
        const auto& curve = edge->GetCurve();
        const double t0 = curve->GetParameter(end), t1 = curve->GetParameter(other);
        const double t = t0 + (t1 - t0) * 0.5;
        BRepAdaptor_Curve exact(edge->GetEdge());
        const gp_Pnt point = exact.Value(t);
        const gp_Vec chord(curve->GetPoint(end), curve->GetPoint(other));
        const gp_Vec half(curve->GetPoint(end), point);
        if (t == t0 || t == t1 ||
            chord.Crossed(half).Magnitude() <= Precision::Confusion() * chord.Magnitude())
            throw OpError("export_failed", "Mesh boundary cusp cannot be resolved on its exact curve");
        // Located copies of one native face share its TShape triangulation.
        // Their corresponding edges are IsPartner, even though IsSame is false.
        // Keep the exact parameter samples identical across these instances;
        // otherwise whichever face meshes last leaves its neighbour with a
        // different boundary polygon. Identity comes from topology, not proximity.
        const auto& model = GetModel();
        for (int i = 0; i < model->EdgesNb(); ++i) {
            const auto& partner = model->GetEdge(i);
            if (edge->GetEdge().IsPartner(partner->GetEdge()))
                insert_boundary_sample(partner, t);
        }
    }

    static void insert_boundary_sample(const IMeshData::IEdgeHandle& edge, double t) {
        const auto& curve = edge->GetCurve();
        int insert = -1;
        for (int i = 0; i < curve->ParametersNb(); ++i) {
            if (curve->GetParameter(i) == t) return;
            if (i == 0) continue;
            const double a = curve->GetParameter(i - 1), b = curve->GetParameter(i);
            if ((a < t && t < b) || (b < t && t < a)) insert = i;
        }
        if (insert < 0)
            throw OpError("export_failed", "Located mesh edge has an incompatible parameter range");
        const gp_Pnt point = BRepAdaptor_Curve(edge->GetEdge()).Value(t);
        curve->InsertPoint(insert, point, t);
        for (int pi = 0; pi < edge->PCurvesNb(); ++pi) {
            const auto& pc = edge->GetPCurve(pi);
            const IMeshData::IFaceHandle face = pc->GetFace();
            IMeshData::ICurveArrayAdaptorHandle params(new IMeshData::ICurveArrayAdaptor(curve));
            BRepMesh_EdgeParameterProvider<IMeshData::ICurveArrayAdaptorHandle> provider(
                edge, pc->GetOrientation(), face, params);
            double parameter = 0.0;
            // Non-SameParameter projection is sequential, just as in Tessellate2d.
            for (int i = 0; i <= insert; ++i)
                parameter = provider.Parameter(i, curve->GetPoint(i));
            gp_Pnt2d uv;
            provider.GetPCurve()->D0(parameter, uv);
            pc->InsertPoint(insert, uv, parameter);
        }
    }
};

// Tessellate a shape in place at the requested quality so a mesh writer has a
// triangulation to emit. The prior triangulation is cleared first so the exact
// requested deflection is honored even if the shape was already meshed at another
// quality (e.g. a finer dialog "estimate" preceding a coarser export).
void mesh_shape(const TopoDS_Shape& shape, const TessOptions& tess = TessOptions{}) {
    BRepTools::Clean(shape);
    BRepMesh_IncrementalMesh m;
    m.SetShape(shape);
    m.ChangeParameters().Deflection = tess.lin_deflection;
    m.ChangeParameters().Relative = tess.relative;
    m.ChangeParameters().Angle = tess.ang_deflection;
    m.ChangeParameters().InParallel = Standard_True;
    m.Perform(new ExportMeshContext);
}

std::string abs_path(const std::string& path) {
    std::error_code ec;
    std::filesystem::path p = std::filesystem::absolute(path, ec);
    return ec ? path : p.string();
}

#ifdef CHUDWARE_WITH_LIB3MF
// A named mesh extracted from a 3MF mesh object — the C++ analogue of the
// oracle's (name, MeshBody(verts, tris)) tuple. Source vertices are float32
// and triangle indices uint32; non-mm model units are scaled to document mm.
struct NamedMesh {
    std::string name;
    MeshData mesh;  // vertices widened to double for unit conversion
};

// Load lib3mf.dll. The CppDynamic binding LoadLibraryW's the given filename; we
// pass the DLL beside THIS module (libchudware.dll) so the staged copy is found
// regardless of the process CWD. Falls back to the bare name (PATH search).
Lib3MF::PWrapper load_lib3mf() {
#ifdef _WIN32
    std::string dll_path = "lib3mf.dll";
    HMODULE self = nullptr;
    if (::GetModuleHandleExA(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCSTR>(&load_lib3mf), &self) &&
        self) {
        char buf[MAX_PATH] = {0};
        DWORD n = ::GetModuleFileNameA(self, buf, MAX_PATH);
        if (n > 0 && n < MAX_PATH) {
            std::filesystem::path p(buf);
            std::filesystem::path beside = p.parent_path() / "lib3mf.dll";
            if (std::filesystem::exists(beside)) dll_path = beside.string();
        }
    }
#else
    // POSIX: prefer the lib3mf shared object staged beside this module (the engine),
    // located via dladdr; fall back to the loader search by bare soname. The bare
    // name has to match the platform too — dlopen("lib3mf.so") on Mach-O can only
    // ever fail.
#  if defined(__APPLE__)
    std::string dll_path = "lib3mf.dylib";
#  else
    std::string dll_path = "lib3mf.so";
#  endif
    Dl_info info;
    if (dladdr(reinterpret_cast<void*>(&load_lib3mf), &info) && info.dli_fname) {
        std::filesystem::path dir = std::filesystem::path(info.dli_fname).parent_path();
        for (const char* name : {"lib3mf.so", "lib3mf.so.2", "lib3mf.2.dylib", "lib3mf.dylib"}) {
            std::filesystem::path beside = dir / name;
            if (std::filesystem::exists(beside)) { dll_path = beside.string(); break; }
        }
    }
#endif
    try {
        return Lib3MF::CWrapper::loadLibrary(dll_path);
    } catch (const std::exception& e) {
        throw OpError("import_failed",
                      std::string("3MF: could not load lib3mf (") + e.what() + ")",
                      {{"reason", "lib3mf load failed"}, {"dll", dll_path}});
    }
}

// Read every mesh object out of a .3mf like formats.py::_import_3mf: iterate
// the model's mesh objects in file order, skip empty ones, convert model units
// to mm, pull triangles, name = GetName() or "<stem>_<idx>".
std::vector<NamedMesh> read_3mf_objects(const std::string& path) {
    Lib3MF::PWrapper wrapper = load_lib3mf();
    Lib3MF::PModel model = wrapper->CreateModel();
    Lib3MF::PReader reader = model->QueryReader("3mf");
    reader->ReadFromFile(path);
    double unit_to_mm = 1.0;
    switch (model->GetUnit()) {
        case Lib3MF::eModelUnit::MicroMeter: unit_to_mm = 0.001; break;
        case Lib3MF::eModelUnit::MilliMeter: break;
        case Lib3MF::eModelUnit::CentiMeter: unit_to_mm = 10.0; break;
        case Lib3MF::eModelUnit::Inch: unit_to_mm = 25.4; break;
        case Lib3MF::eModelUnit::Foot: unit_to_mm = 304.8; break;
        case Lib3MF::eModelUnit::Meter: unit_to_mm = 1000.0; break;
        default:
            throw OpError("import_failed", "3MF: unsupported model length unit", {{"path", path}});
    }

    const std::string stem = std::filesystem::path(path).stem().string();
    std::vector<NamedMesh> out;
    Lib3MF::PMeshObjectIterator it = model->GetMeshObjects();
    int idx = 0;
    while (it->MoveNext()) {
        ++idx;
        Lib3MF::PMeshObject mesh = it->GetCurrentMeshObject();
        if (mesh->GetTriangleCount() == 0) continue;  // matches the oracle's skip
        std::vector<Lib3MF::sPosition> gv;
        std::vector<Lib3MF::sTriangle> gi;
        mesh->GetVertices(gv);
        mesh->GetTriangleIndices(gi);
        NamedMesh nm;
        std::string nm_name = mesh->GetName();
        nm.name = nm_name.empty() ? (stem + "_" + std::to_string(idx)) : nm_name;
        nm.mesh.verts.reserve(gv.size() * 3);
        for (const Lib3MF::sPosition& v : gv) {
            for (float coordinate : v.m_Coordinates)
                nm.mesh.verts.push_back(static_cast<double>(coordinate) * unit_to_mm);
        }
        nm.mesh.tris.reserve(gi.size() * 3);
        for (const Lib3MF::sTriangle& t : gi) {
            nm.mesh.tris.push_back(t.m_Indices[0]);
            nm.mesh.tris.push_back(t.m_Indices[1]);
            nm.mesh.tris.push_back(t.m_Indices[2]);
        }
        out.push_back(std::move(nm));
    }
    return out;
}

// Write named meshes to a .3mf, mirroring engine/doc/formats.py::_export_3mf_lib3mf:
// one AddMeshObject + AddBuildItem(identity) per body; SetName; SetGeometry with
// the positions (float) and triangles (uint32). Skips empty meshes.
void write_3mf_meshes(const std::vector<NamedMesh>& meshes, const std::string& path) {
    Lib3MF::PWrapper wrapper = load_lib3mf();
    Lib3MF::PModel model = wrapper->CreateModel();
    Lib3MF::sTransform identity = wrapper->GetIdentityTransform();
    int added = 0;
    for (const NamedMesh& nm : meshes) {
        const MeshData& m = nm.mesh;
        if (m.triangle_count() == 0) continue;
        Lib3MF::PMeshObject mo = model->AddMeshObject();
        mo->SetName(nm.name);
        std::vector<Lib3MF::sPosition> ps;
        ps.reserve(m.vertex_count());
        for (std::size_t i = 0; i < m.vertex_count(); ++i) {
            Lib3MF::sPosition p;
            p.m_Coordinates[0] = static_cast<Lib3MF_single>(m.verts[3 * i]);
            p.m_Coordinates[1] = static_cast<Lib3MF_single>(m.verts[3 * i + 1]);
            p.m_Coordinates[2] = static_cast<Lib3MF_single>(m.verts[3 * i + 2]);
            ps.push_back(p);
        }
        std::vector<Lib3MF::sTriangle> ts;
        ts.reserve(m.triangle_count());
        for (std::size_t i = 0; i < m.triangle_count(); ++i) {
            Lib3MF::sTriangle t;
            t.m_Indices[0] = m.tris[3 * i];
            t.m_Indices[1] = m.tris[3 * i + 1];
            t.m_Indices[2] = m.tris[3 * i + 2];
            ts.push_back(t);
        }
        mo->SetGeometry(ps, ts);
        model->AddBuildItem(mo.get(), identity);
        ++added;
    }
    if (added == 0)
        throw OpError("export_failed", "3MF export: no mesh geometry to write",
                      {{"format", "3mf"}, {"path", path}});
    model->QueryWriter("3mf")->WriteToFile(path);
}
#endif  // CHUDWARE_WITH_LIB3MF

// --------------------------------------------------------------------------- DXF import
//
// Clean-room port of engine/doc/formats_dxf.py's core-entity subset (LINE / CIRCLE / ARC /
// LWPOLYLINE / POLYLINE) onto a hand-written ASCII DXF parser + OCCT edge builders — no
// ezdxf, no third-party DXF library. ELLIPSE / SPLINE / INSERT (block references) are
// intentionally OUT of scope for this increment (the Python oracle covers them; porting
// spline flattening + block-instancing is a follow-up) — they are counted as skipped like
// any other unsupported entity.
//
// A DXF ASCII stream is a flat sequence of (group-code, value) line PAIRS: group code on
// one line (an int, possibly with leading/trailing whitespace), the value on the next. We
// walk pairs, track SECTION/ENDSEC to isolate ENTITIES, and collect each entity (from a
// code-0 entity-type line up to the next code-0) as an ordered vector of (code, value)
// pairs — mirroring ezdxf's own tag stream before it builds typed DXF entities.

constexpr double kPi = 3.14159265358979323846;

namespace dxf {

// One flat (group code, string value) pair — the DXF "tag".
struct Tag {
    int code = 0;
    std::string value;
};

// One entity: its DXF type name (e.g. "LINE") plus the ordered tag stream that followed
// its code-0 header line, up to (not including) the next code-0.
struct Entity {
    std::string type;
    std::vector<Tag> tags;
};

std::string trim(const std::string& s) {
    std::size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return std::string();
    std::size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

// Read the whole file as a flat (code, value) tag stream. Tolerant: a short trailing line
// (a code with no following value) just ends the stream early rather than throwing.
std::vector<Tag> read_tags(const std::string& path) {
    std::ifstream is(path, std::ios::in | std::ios::binary);
    if (!is) throw OpError("import_failed", "DXF: cannot open input", {{"path", path}});
    std::vector<Tag> tags;
    std::string code_line, value_line;
    while (std::getline(is, code_line)) {
        if (!std::getline(is, value_line)) break;  // malformed trailing pair — stop gracefully
        std::string ct = trim(code_line);
        if (ct.empty()) continue;
        int code = 0;
        try {
            code = std::stoi(ct);
        } catch (const std::exception&) {
            continue;  // unparsable code line — skip this pair, keep going
        }
        Tag t;
        t.code = code;
        // Text groups 1/3 keep their spaces: an ACIS SAT line (and its continuation) is
        // space-delimited, so trimming would fuse the tokens on either side of a split.
        if (code == 1 || code == 3) {
            if (!value_line.empty() && value_line.back() == '\r') value_line.pop_back();
            t.value = value_line;
        } else {
            t.value = trim(value_line);
        }
        tags.push_back(std::move(t));
    }
    return tags;
}

// Pull every code-0 entity of `section` ("ENTITIES" or "BLOCKS") out of the tag stream, in
// file order. Everything up to the next code-0 (or ENDSEC) belongs to it. HEADER variables
// are handled separately by insunits_scale().
std::vector<Entity> parse_section(const std::vector<Tag>& tags, const std::string& section) {
    std::vector<Entity> out;
    bool in_section = false;
    bool expect_section_name = false;
    Entity* cur = nullptr;

    for (const Tag& t : tags) {
        if (t.code == 0) {
            if (t.value == "SECTION") {
                expect_section_name = true;
                in_section = false;
                cur = nullptr;
                continue;
            }
            if (t.value == "ENDSEC") {
                in_section = false;
                cur = nullptr;
                continue;
            }
            if (t.value == "EOF") break;
            if (in_section) {
                out.emplace_back();
                out.back().type = t.value;
                cur = &out.back();
            } else {
                cur = nullptr;
            }
            continue;
        }
        if (expect_section_name && t.code == 2) {
            in_section = (t.value == section);
            expect_section_name = false;
            continue;
        }
        if (cur != nullptr) cur->tags.push_back(t);
    }
    return out;
}

std::vector<Entity> parse_entities(const std::vector<Tag>& tags) {
    return parse_section(tags, "ENTITIES");
}

// Millimetres per unit of $INSUNITS `code`; false for an unknown code. 0 (unitless) counts
// as millimetres: the coordinates already have the caller's intended scale.
bool units_mm(int code, double* mm) {
    switch (code) {
        case 0: case 4: *mm = 1.0; return true;
        case 1: *mm = 25.4; return true;           // inches
        case 2: *mm = 304.8; return true;          // feet
        case 3: *mm = 1609344.0; return true;      // miles
        case 5: *mm = 10.0; return true;           // centimeters
        case 6: *mm = 1000.0; return true;         // meters
        case 7: *mm = 1000000.0; return true;      // kilometers
        case 8: *mm = 0.0000254; return true;      // microinches
        case 9: *mm = 0.0254; return true;         // mils
        case 10: *mm = 914.4; return true;         // yards
        case 11: *mm = 1e-7; return true;          // angstroms
        case 12: *mm = 1e-6; return true;          // nanometers
        case 13: *mm = 0.001; return true;         // microns
        case 14: *mm = 100.0; return true;         // decimeters
        case 15: *mm = 10000.0; return true;       // decameters
        case 16: *mm = 100000.0; return true;      // hectometers
        case 17: *mm = 1e12; return true;          // gigameters
        case 18: *mm = 149597870700000.0; return true; // astronomical units
        case 19: *mm = 9.4607304725808e18; return true; // light years
        case 20: *mm = 3.085677581491367e19; return true; // parsecs
        case 21: *mm = 1200.0 / 3937.0 * 1000.0; return true; // US survey feet
        case 22: *mm = 100.0 / 3937.0 * 1000.0; return true;  // US survey inches
        case 23: *mm = 3600.0 / 3937.0 * 1000.0; return true; // US survey yards
        case 24: *mm = 6336000.0 / 3937.0 * 1000.0; return true; // US survey miles
        default: return false;
    }
}

// HEADER $INSUNITS is a code-9 variable followed by a code-70 integer. Zero (or
// absence) means unitless, so coordinates already have the caller's intended scale.
double insunits_scale(const std::vector<Tag>& tags, const std::string& path) {
    bool in_header = false, expect_section_name = false, expect_units = false;
    int units = 0;
    for (const Tag& t : tags) {
        if (t.code == 0) {
            if (t.value == "SECTION") {
                expect_section_name = true;
                in_header = false;
            } else if (t.value == "ENDSEC") {
                in_header = false;
                expect_units = false;
            }
            continue;
        }
        if (expect_section_name && t.code == 2) {
            in_header = t.value == "HEADER";
            expect_section_name = false;
            continue;
        }
        if (!in_header) continue;
        if (t.code == 9) {
            expect_units = t.value == "$INSUNITS";
        } else if (expect_units && t.code == 70) {
            try { units = std::stoi(t.value); }
            catch (const std::exception&) {
                throw OpError("import_failed", "DXF: invalid $INSUNITS", {{"path", path}});
            }
            expect_units = false;
        }
    }
    double mm = 1.0;
    if (!units_mm(units, &mm))
        throw OpError("import_failed", "DXF: unsupported $INSUNITS " + std::to_string(units),
                      {{"path", path}, {"units", units}});
    return mm;
}

// Fetch the first value for `code`, or `def_val` when absent.
double get_d(const Entity& e, int code, double def_val = 0.0) {
    for (const Tag& t : e.tags)
        if (t.code == code) {
            try { return std::stod(t.value); } catch (const std::exception&) { return def_val; }
        }
    return def_val;
}

long get_i(const Entity& e, int code, long def_val = 0) {
    for (const Tag& t : e.tags)
        if (t.code == code) {
            try { return std::stol(t.value); } catch (const std::exception&) { return def_val; }
        }
    return def_val;
}

// Collect EVERY value for `code`, in document order. SPLINE repeats 10/20/30 (control points)
// and 40 (knots); get_d's first-match is not enough for those.
std::vector<double> get_all_d(const Entity& e, int code) {
    std::vector<double> out;
    for (const Tag& t : e.tags)
        if (t.code == code) {
            try { out.push_back(std::stod(t.value)); } catch (const std::exception&) {}
        }
    return out;
}

std::string get_s(const Entity& e, int code, const std::string& def_val = std::string()) {
    for (const Tag& t : e.tags)
        if (t.code == code) return t.value;
    return def_val;
}

// A BLOCKS-section definition: its base point and member entities (BLOCK .. ENDBLK).
// `record` is the owning BLOCK_RECORD handle (BLOCK group 330; empty in R12 files), which
// is how a DWG entity names the block it belongs to.
struct Block {
    gp_XYZ base{0.0, 0.0, 0.0};
    std::string record;
    std::vector<Entity> entities;
};
using Blocks = std::unordered_map<std::string, Block>;

Blocks parse_blocks(const std::vector<Tag>& tags) {
    Blocks out;
    Block* cur = nullptr;
    for (Entity& e : parse_section(tags, "BLOCKS")) {
        if (e.type == "BLOCK") {
            cur = &out[get_s(e, 2)];
            cur->base = gp_XYZ(get_d(e, 10), get_d(e, 20), get_d(e, 30));
            cur->record = get_s(e, 330);
            cur->entities.clear();
        } else if (e.type == "ENDBLK") {
            cur = nullptr;
        } else if (cur != nullptr) {
            cur->entities.push_back(std::move(e));
        }
    }
    return out;
}

// Entity extrusion direction (210/220/230), the Z axis of its object coordinate system.
gp_Dir extrusion(const Entity& e) {
    const double x = get_d(e, 210, 0.0), y = get_d(e, 220, 0.0), z = get_d(e, 230, 1.0);
    const double n = std::sqrt(x * x + y * y + z * z);
    if (n < 1e-12) return gp_Dir(0.0, 0.0, 1.0);
    return gp_Dir(x / n, y / n, z / n);
}

bool is_world_z(const gp_Dir& n) { return n.Z() > 1.0 - 1e-12; }

// OCS -> WCS rotation by the DXF Arbitrary Axis Algorithm; identity for extrusion +Z.
gp_Mat ocs_matrix(const gp_Dir& n) {
    const gp_XYZ N = n.XYZ();
    gp_XYZ ax = (std::abs(N.X()) < 1.0 / 64.0 && std::abs(N.Y()) < 1.0 / 64.0)
                    ? gp_XYZ(0.0, 1.0, 0.0).Crossed(N)
                    : gp_XYZ(0.0, 0.0, 1.0).Crossed(N);
    ax.Normalize();
    gp_XYZ ay = N.Crossed(ax);
    ay.Normalize();
    return gp_Mat(ax, ay, N);  // columns: OCS X, Y, Z in world coordinates
}

gp_Trsf ocs_trsf(const gp_Dir& n) {
    const gp_Mat m = ocs_matrix(n);
    gp_Trsf t;
    t.SetValues(m(1, 1), m(1, 2), m(1, 3), 0.0, m(2, 1), m(2, 2), m(2, 3), 0.0,
                m(3, 1), m(3, 2), m(3, 3), 0.0);
    return t;
}

// DXF SPLINE → OCCT B-spline edge. Control-point + knot form (the common machine-DXF case — e.g.
// every curve in a splined art file): poles = 10/20/30, degree = 71, flat knot vector = 40,
// optional per-pole weights = 41. The flat DXF knots are compressed into OCCT's
// (unique knots, multiplicities). Returns a null edge on any failure (caller counts it skipped).
// Fit-point-only splines (no control points) are not handled here — a further follow-up.
TopoDS_Edge spline_edge(const Entity& e) {
    const int degree = static_cast<int>(get_i(e, 71, 3));
    std::vector<double> xs = get_all_d(e, 10), ys = get_all_d(e, 20), zs = get_all_d(e, 30);
    std::vector<double> knots_flat = get_all_d(e, 40);
    std::vector<double> weights = get_all_d(e, 41);
    const std::size_t n = xs.size();
    if (n < 2 || degree < 1 ||
        knots_flat.size() != n + static_cast<std::size_t>(degree) + 1)
        return TopoDS_Edge();

    TColgp_Array1OfPnt poles(1, static_cast<int>(n));
    for (std::size_t i = 0; i < n; ++i)
        poles.SetValue(static_cast<int>(i) + 1,
                       gp_Pnt(xs[i], ys[i], i < zs.size() ? zs[i] : 0.0));

    std::vector<double> uk;   // unique knot values
    std::vector<int> um;      // their multiplicities
    for (double k : knots_flat) {
        if (!uk.empty() && std::abs(k - uk.back()) < 1e-9) ++um.back();
        else { uk.push_back(k); um.push_back(1); }
    }
    TColStd_Array1OfReal occt_knots(1, static_cast<int>(uk.size()));
    TColStd_Array1OfInteger occt_mults(1, static_cast<int>(um.size()));
    for (std::size_t i = 0; i < uk.size(); ++i) {
        occt_knots.SetValue(static_cast<int>(i) + 1, uk[i]);
        occt_mults.SetValue(static_cast<int>(i) + 1, um[i]);
    }

    Handle(Geom_BSplineCurve) curve;
    if (weights.size() == n) {
        TColStd_Array1OfReal w(1, static_cast<int>(n));
        for (std::size_t i = 0; i < n; ++i) w.SetValue(static_cast<int>(i) + 1, weights[i]);
        curve = new Geom_BSplineCurve(poles, w, occt_knots, occt_mults, degree);
    } else {
        curve = new Geom_BSplineCurve(poles, occt_knots, occt_mults, degree);
    }
    return BRepBuilderAPI_MakeEdge(curve);
}

// DXF ELLIPSE → OCCT elliptical-arc edge. center = 10/20/30, major-axis endpoint RELATIVE to the
// center = 11/21/31, ratio (minor/major) = 40, start/end params (radians) = 41/42.
TopoDS_Edge ellipse_edge(const Entity& e) {
    const double cx = get_d(e, 10), cy = get_d(e, 20), cz = get_d(e, 30);
    const double mx = get_d(e, 11), my = get_d(e, 21), mz = get_d(e, 31);
    const double ratio = get_d(e, 40, 1.0);
    double u0 = get_d(e, 41, 0.0), u1 = get_d(e, 42, 2.0 * kPi);
    const double major = std::sqrt(mx * mx + my * my + mz * mz);
    if (major < 1e-12) return TopoDS_Edge();
    // Center and major axis are WCS; the extrusion is the ellipse normal.
    gp_Ax2 ax(gp_Pnt(cx, cy, cz), extrusion(e), gp_Dir(mx, my, mz));
    gp_Elips el(ax, major, major * ratio);
    if (u1 <= u0) u1 += 2.0 * kPi;
    return BRepBuilderAPI_MakeEdge(el, u0, u1);
}

// A DXF polyline *bulge* segment → circular-arc edge (port of _bulge_arc). bulge =
// tan(theta/4); the arc apex sits off the chord midpoint along the left normal by the
// signed sagitta bulge*chord/2. A three-point arc through (p1, apex, p2) reproduces it
// exactly. Falls back to a straight line edge if the 3-point arc construction fails.
TopoDS_Edge bulge_arc_edge(double x1, double y1, double x2, double y2, double bulge, double z) {
    const double dx = x2 - x1, dy = y2 - y1;
    const double chord = std::hypot(dx, dy);
    if (chord < 1e-9) throw Standard_Failure("degenerate bulge segment");
    const double sagitta = bulge * chord / 2.0;
    const double ux = dx / chord, uy = dy / chord;
    const double nx = -uy, ny = ux;  // left normal
    const double mx = (x1 + x2) / 2.0, my = (y1 + y2) / 2.0;
    const double ax = mx + nx * sagitta, ay = my + ny * sagitta;
    try {
        GC_MakeArcOfCircle mk(gp_Pnt(x1, y1, z), gp_Pnt(ax, ay, z), gp_Pnt(x2, y2, z));
        if (mk.IsDone()) return BRepBuilderAPI_MakeEdge(mk.Value());
    } catch (const Standard_Failure&) {
        // fall through to the line fallback below
    }
    return BRepBuilderAPI_MakeEdge(gp_Pnt(x1, y1, z), gp_Pnt(x2, y2, z));
}

// One polyline vertex: (x, y, z, bulge-of-segment-starting-here).
struct PolyVertex {
    double x = 0, y = 0, z = 0, bulge = 0;
};

// Turn an ordered vertex list (+closed flag) into line/bulge-arc edges, one per segment
// (wrapping the last->first segment when closed). Mirrors _polyline_edges' segment loop.
void polyline_vertices_to_edges(const std::vector<PolyVertex>& pts, bool closed,
                                std::vector<TopoDS_Edge>& out) {
    const std::size_t n = pts.size();
    if (n < 2) return;
    const std::size_t segs = closed ? n : (n - 1);
    for (std::size_t i = 0; i < segs; ++i) {
        const PolyVertex& p1 = pts[i];
        const PolyVertex& p2 = pts[(i + 1) % n];
        if (std::hypot(p2.x - p1.x, p2.y - p1.y) < 1e-9 && std::abs(p2.z - p1.z) < 1e-9)
            continue;  // coincident vertices — skip degenerate segment
        try {
            if (std::abs(p1.bulge) < 1e-12)
                out.push_back(BRepBuilderAPI_MakeEdge(gp_Pnt(p1.x, p1.y, p1.z),
                                                      gp_Pnt(p2.x, p2.y, p2.z)));
            else
                out.push_back(bulge_arc_edge(p1.x, p1.y, p2.x, p2.y, p1.bulge, p1.z));
        } catch (const Standard_Failure&) {
            // one bad segment must not abort the whole import
        }
    }
}

// LWPOLYLINE: 70 flags (bit 0x1 closed), 38 elevation (z, default 0). Vertices are
// repeated groups: each 10 begins a vertex (paired 20 = y), an optional 42 = bulge for the
// segment STARTING at that vertex.
void lwpolyline_to_edges(const Entity& e, std::vector<TopoDS_Edge>& out) {
    const long flags = get_i(e, 70, 0);
    const bool closed = (flags & 0x1) != 0;
    const double elev = get_d(e, 38, 0.0);

    // Vertices are repeated groups: each 10 begins a new vertex (paired 20 = y); an
    // optional 42 = bulge for the segment STARTING at that vertex may follow before the
    // next 10. Buffer the in-progress vertex and flush it when the next 10 (or end of tag
    // stream) arrives, so a trailing 42 is captured.
    std::vector<PolyVertex> pts;
    bool have_x = false;
    double cur_x = 0, cur_y = 0, cur_bulge = 0;
    auto flush = [&]() {
        if (have_x) pts.push_back({cur_x, cur_y, elev, cur_bulge});
    };
    for (const Tag& t : e.tags) {
        if (t.code == 10) {
            flush();
            try { cur_x = std::stod(t.value); } catch (const std::exception&) { cur_x = 0.0; }
            cur_y = 0.0;
            cur_bulge = 0.0;
            have_x = true;
        } else if (t.code == 20 && have_x) {
            try { cur_y = std::stod(t.value); } catch (const std::exception&) { cur_y = 0.0; }
        } else if (t.code == 42 && have_x) {
            try { cur_bulge = std::stod(t.value); } catch (const std::exception&) { cur_bulge = 0.0; }
        }
    }
    flush();
    polyline_vertices_to_edges(pts, closed, out);
}

}  // namespace dxf

// An ACIS solid/sheet carried by a 3DSOLID / BODY / REGION / *SURFACE entity.
struct DxfAcisBody {
    std::string layer;
    TopoDS_Shape shape;
    bool solid = false;
};

// Everything a DXF drawing contributes: curve edges for the 2D/3D profile body, triangle
// soups per effective layer (3DFACE / POLYFACE / polygon mesh), ACIS bodies, and what was
// not imported.
struct DxfGeometry {
    std::vector<TopoDS_Edge> edges;
    std::map<std::string, MeshData> meshes;  // effective layer -> triangle soup
    std::vector<DxfAcisBody> acis_bodies;
    std::size_t triangles = 0;
    int skipped = 0;  // entities that produced nothing (text, dimensions, hatches, failures)
    int acis = 0;     // ACIS-backed entities that could NOT be imported (named in acis_notes)
    std::vector<std::string> acis_notes;
};

bool is_acis_entity(const std::string& type) {
    return type == "3DSOLID" || type == "BODY" || type == "REGION" || type == "SURFACE" ||
           type == "PLANESURFACE" || type == "EXTRUDEDSURFACE" || type == "LOFTEDSURFACE" ||
           type == "REVOLVEDSURFACE" || type == "SWEPTSURFACE" || type == "NURBSURFACE";
}

// DWG/DXF handles are hex strings; compare them upper-case without leading zeros.
std::string handle_key(const std::string& h) {
    std::string k;
    for (char c : h) {
        if (std::isspace(static_cast<unsigned char>(c))) continue;
        if (k.empty() && c == '0') continue;
        k.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
    }
    return k.empty() ? std::string("0") : k;
}

// ACIS data kept outside the entity text, by entity handle: the DXF R2013+ ACDSDATA section,
// or the native DWG reader (dwg_native.h) for a DWG of any version. SAB bytes or SAT text.
using AcisPayloads = std::unordered_map<std::string, std::string>;

// The ACIS payload of `e`: the data keyed by its handle (group 5) when there is one, else its
// inline SAT (groups 1 + 3 continuations; DXF up to R2010). For a DWG the handle-keyed data
// always wins: LibreDWG dwg2dxf re-emits inline SAT one group 1 per 4096-byte DWG block and
// per 255 characters, splitting tokens, so its SAT lines are not trustworthy.
std::string acis_payload(const dxf::Entity& e, const AcisPayloads& payloads) {
    const auto it = payloads.find(handle_key(dxf::get_s(e, 5)));
    if (it != payloads.end()) return it->second;
    std::vector<std::string> lines;
    for (const dxf::Tag& t : e.tags) {
        if (t.code == 1) lines.push_back(t.value);
        else if (t.code == 3 && !lines.empty()) lines.back() += t.value;
    }
    return lines.empty() ? std::string() : decrypt_dxf_sat(lines);
}

void add_acis_entity(const dxf::Entity& e, const std::string& layer, const AcisPayloads& payloads,
                     DxfGeometry& out) {
    const std::string handle = dxf::get_s(e, 5);
    const std::string who = e.type + (handle.empty() ? std::string() : " " + handle);
    const std::string data = acis_payload(e, payloads);
    if (data.empty()) {
        ++out.acis;
        out.acis_notes.push_back(who + " carries no ACIS data");
        return;
    }
    try {
        AcisReadResult r = read_acis(data);
        for (const std::string& w : r.warnings) out.acis_notes.push_back(who + ": " + w);
        if (r.bodies.empty()) {
            ++out.acis;
            out.acis_notes.push_back(who + ": the ACIS data holds no body");
        }
        for (AcisBody& b : r.bodies)
            if (!b.shape.IsNull()) out.acis_bodies.push_back({layer, std::move(b.shape), b.solid});
    } catch (const std::exception& ex) {
        ++out.acis;
        out.acis_notes.push_back(who + ": " + ex.what());
    }
}

// Block members on layer "0" take the layer of the INSERT that places them.
std::string effective_layer(const dxf::Entity& e, const std::string& parent) {
    std::string layer = dxf::get_s(e, 8, "0");
    if (layer.empty()) layer = "0";
    return (layer == "0" && !parent.empty()) ? parent : layer;
}

void add_triangle(DxfGeometry& g, const std::string& layer, const gp_Pnt& a, const gp_Pnt& b,
                  const gp_Pnt& c) {
    if (a.Distance(b) < 1e-12 || b.Distance(c) < 1e-12 || a.Distance(c) < 1e-12) return;
    MeshData& m = g.meshes[layer];
    const auto base = static_cast<uint32_t>(m.vertex_count());
    for (const gp_Pnt* p : {&a, &b, &c}) {
        m.verts.push_back(p->X());
        m.verts.push_back(p->Y());
        m.verts.push_back(p->Z());
    }
    m.tris.insert(m.tris.end(), {base, base + 1, base + 2});
    ++g.triangles;
}

// A quad (or triangle when the last corner repeats) split into triangles.
void add_polygon(DxfGeometry& g, const std::string& layer, const std::vector<gp_Pnt>& c) {
    if (c.size() < 3) return;
    add_triangle(g, layer, c[0], c[1], c[2]);
    if (c.size() == 4 && c[3].Distance(c[2]) > 1e-12) add_triangle(g, layer, c[0], c[2], c[3]);
}

void transform_edges(std::vector<TopoDS_Edge>& edges, std::size_t from, const gp_Trsf& t) {
    for (std::size_t k = from; k < edges.size(); ++k)
        edges[k] = TopoDS::Edge(BRepBuilderAPI_Transform(edges[k], t, Standard_True).Shape());
}

gp_Pnt vertex_point(const dxf::Entity& v) {
    return gp_Pnt(dxf::get_d(v, 10), dxf::get_d(v, 20), dxf::get_d(v, 30));
}

// POLYLINE (old-style) + its trailing VERTEX/SEQEND entities. 2D polylines are OCS at the
// header elevation, 3D polylines (flag 8) are WCS, polyface meshes (64) and M x N polygon
// meshes (16) become triangles. `i` indexes the POLYLINE; it is advanced past SEQEND so the
// caller's loop continues after it.
void polyline_old_style(const std::vector<dxf::Entity>& entities, std::size_t& i,
                        const std::string& layer, DxfGeometry& out) {
    const dxf::Entity& head = entities[i];
    const long flags = dxf::get_i(head, 70, 0);
    std::vector<const dxf::Entity*> verts;
    std::size_t j = i + 1;
    for (; j < entities.size(); ++j) {
        const dxf::Entity& e = entities[j];
        if (e.type == "SEQEND") { ++j; break; }
        if (e.type == "VERTEX") verts.push_back(&e);  // tolerate stray entities before SEQEND
    }
    i = j - 1;  // caller's for-loop does ++i

    if (flags & 64) {
        // Polyface: position vertices (flags 128|64), then face records (128) whose 71..74
        // hold 1-based vertex indices (negative = hidden edge).
        std::vector<gp_Pnt> pos;
        for (const dxf::Entity* v : verts) {
            const long vf = dxf::get_i(*v, 70, 0);
            if ((vf & 128) && (vf & 64)) pos.push_back(vertex_point(*v));
        }
        for (const dxf::Entity* v : verts) {
            const long vf = dxf::get_i(*v, 70, 0);
            if (!(vf & 128) || (vf & 64)) continue;
            std::vector<gp_Pnt> corners;
            for (int code = 71; code <= 74; ++code) {
                const long idx = std::labs(dxf::get_i(*v, code, 0));
                if (idx >= 1 && static_cast<std::size_t>(idx) <= pos.size())
                    corners.push_back(pos[static_cast<std::size_t>(idx) - 1]);
            }
            add_polygon(out, layer, corners);
        }
        return;
    }
    if (flags & 16) {
        const long m = dxf::get_i(head, 71, 0), n = dxf::get_i(head, 72, 0);
        if (m < 2 || n < 2 || verts.size() < static_cast<std::size_t>(m * n)) return;
        const bool close_m = (flags & 1) != 0, close_n = (flags & 32) != 0;
        auto at = [&](long r, long c) { return vertex_point(*verts[(r % m) * n + (c % n)]); };
        for (long r = 0; r < (close_m ? m : m - 1); ++r)
            for (long c = 0; c < (close_n ? n : n - 1); ++c)
                add_polygon(out, layer, {at(r, c), at(r, c + 1), at(r + 1, c + 1), at(r + 1, c)});
        return;
    }
    const bool is3d = (flags & 8) != 0;
    const double elevation = dxf::get_d(head, 30, 0.0);
    std::vector<dxf::PolyVertex> pts;
    for (const dxf::Entity* v : verts) {
        if (dxf::get_i(*v, 70, 0) & 16) continue;  // spline frame control point, not on curve
        dxf::PolyVertex p;
        p.x = dxf::get_d(*v, 10, 0.0);
        p.y = dxf::get_d(*v, 20, 0.0);
        p.z = is3d ? dxf::get_d(*v, 30, 0.0) : elevation;
        p.bulge = is3d ? 0.0 : dxf::get_d(*v, 42, 0.0);
        pts.push_back(p);
    }
    const std::size_t from = out.edges.size();
    dxf::polyline_vertices_to_edges(pts, (flags & 0x1) != 0, out.edges);
    const gp_Dir n = dxf::extrusion(head);
    if (!is3d && !dxf::is_world_z(n)) transform_edges(out.edges, from, dxf::ocs_trsf(n));
}

constexpr int kMaxBlockDepth = 16;  // nesting bound; also stops self-referencing blocks

// One budget spans root entities, every nested block and the temporary block geometry.
// Count logical expansion before constructing it; charge exact geometry before copying.
struct DxfExpansionBudget {
    std::size_t instances = 0, work = 0, edges = 0, vertices = 0, triangles = 0, bodies = 0;
    static constexpr std::size_t kInstances = 100000, kGeometry = 1000000, kWork = 4000000;

    static void charge(std::size_t& used, std::size_t amount, std::size_t copies,
                       std::size_t limit, const char* kind) {
        if (amount != 0 && copies > (limit - used) / amount)
            throw OpError("import_failed", std::string("DXF expansion budget exceeded: ") + kind,
                          {{"limit", limit}});
        used += amount * copies;
    }
};

long insert_dimension(const dxf::Entity& e, int code) {
    for (const auto& tag : e.tags) {
        if (tag.code != code) continue;
        try {
            std::size_t consumed = 0;
            const long value = std::stol(tag.value, &consumed);
            if (consumed == tag.value.size() && value >= 1 &&
                static_cast<unsigned long>(value) <= DxfExpansionBudget::kInstances)
                return value;
        } catch (const std::exception&) {
            // Convert malformed/overflowing tags to the same controlled import error.
        }
        throw OpError("import_failed", "DXF INSERT has invalid or over-budget array dimensions");
    }
    return 1;
}

std::size_t insert_instances(const dxf::Entity& e) {
    const long cols = insert_dimension(e, 70), rows = insert_dimension(e, 71);
    if (cols < 1 || rows < 1)
        throw OpError("import_failed", "DXF INSERT rows and columns must be positive");
    const auto c = static_cast<std::size_t>(cols), r = static_cast<std::size_t>(rows);
    if (c > DxfExpansionBudget::kInstances || r > DxfExpansionBudget::kInstances / c)
        throw OpError("import_failed", "DXF INSERT array exceeds instance budget");
    return c * r;
}

void admit_dxf_expansion(const std::vector<dxf::Entity>& entities, const dxf::Blocks& blocks,
                         int depth, std::size_t copies, DxfExpansionBudget& budget) {
    for (const auto& e : entities) {
        DxfExpansionBudget::charge(budget.work, 1, copies, DxfExpansionBudget::kWork, "entities");
        DxfExpansionBudget::charge(budget.work, e.tags.size(), copies,
                                   DxfExpansionBudget::kWork, "entity tags");
        if (e.type != "INSERT") continue;
        const auto found = blocks.find(dxf::get_s(e, 2));
        if (found == blocks.end() || depth >= kMaxBlockDepth) continue;
        const auto instances = insert_instances(e);
        DxfExpansionBudget::charge(budget.instances, instances, copies,
                                   DxfExpansionBudget::kInstances, "instances");
        // The instance admission above proves this product fits before recursion.
        admit_dxf_expansion(found->second.entities, blocks, depth + 1, copies * instances, budget);
    }
}

void charge_acis_geometry(const DxfAcisBody& body, std::size_t copies,
                          DxfExpansionBudget& budget) {
    DxfExpansionBudget::charge(budget.bodies, 1, copies, 10000, "ACIS bodies");
    for (TopExp_Explorer ex(body.shape, TopAbs_EDGE); ex.More(); ex.Next())
        DxfExpansionBudget::charge(budget.edges, 1, copies, DxfExpansionBudget::kGeometry, "edges");
    for (TopExp_Explorer ex(body.shape, TopAbs_VERTEX); ex.More(); ex.Next())
        DxfExpansionBudget::charge(budget.vertices, 1, copies,
                                   DxfExpansionBudget::kGeometry * 3, "vertices");
}

void charge_copied_geometry(const DxfGeometry& geometry, std::size_t copies,
                            DxfExpansionBudget& budget) {
    DxfExpansionBudget::charge(budget.edges, geometry.edges.size(), copies,
                               DxfExpansionBudget::kGeometry, "edges");
    DxfExpansionBudget::charge(budget.vertices, geometry.edges.size(), copies * 2,
                               DxfExpansionBudget::kGeometry * 3, "edge vertices");
    for (const auto& [layer, mesh] : geometry.meshes) {
        (void)layer;
        DxfExpansionBudget::charge(budget.vertices, mesh.vertex_count(), copies,
                                   DxfExpansionBudget::kGeometry * 3, "vertices");
        DxfExpansionBudget::charge(budget.triangles, mesh.triangle_count(), copies,
                                   DxfExpansionBudget::kGeometry, "triangles");
    }
    for (const auto& body : geometry.acis_bodies) charge_acis_geometry(body, copies, budget);
}

void collect_entities(const std::vector<dxf::Entity>& entities, const dxf::Blocks& blocks,
                      const AcisPayloads& payloads, const std::string& parent_layer, int depth,
                      DxfGeometry& out, DxfExpansionBudget& budget);

// INSERT / MINSERT: collect the block once, then place each array instance with
// world = OCS(ins + Rz(rot) * (S * (p - base) + grid offset)).
void place_insert(const dxf::Entity& e, const dxf::Blocks& blocks, const AcisPayloads& payloads,
                  const std::string& layer, int depth, DxfGeometry& out, DxfExpansionBudget& budget) {
    const auto found = blocks.find(dxf::get_s(e, 2));
    if (found == blocks.end() || depth >= kMaxBlockDepth) return;
    const std::size_t instances = insert_instances(e);
    DxfGeometry sub;
    collect_entities(found->second.entities, blocks, payloads, layer, depth + 1, sub, budget);
    charge_copied_geometry(sub, instances, budget);
    out.skipped += sub.skipped;
    out.acis += sub.acis;
    out.acis_notes.insert(out.acis_notes.end(), sub.acis_notes.begin(), sub.acis_notes.end());

    const double sx = dxf::get_d(e, 41, 1.0), sy = dxf::get_d(e, 42, 1.0), sz = dxf::get_d(e, 43, 1.0);
    const double rot = dxf::get_d(e, 50, 0.0) * kPi / 180.0;
    const long cols = insert_dimension(e, 70), rows = insert_dimension(e, 71);
    const double col_step = dxf::get_d(e, 44, 0.0), row_step = dxf::get_d(e, 45, 0.0);
    const gp_XYZ ins(dxf::get_d(e, 10), dxf::get_d(e, 20), dxf::get_d(e, 30));
    const gp_Mat ocs = dxf::ocs_matrix(dxf::extrusion(e));
    const gp_Mat rz(std::cos(rot), -std::sin(rot), 0.0, std::sin(rot), std::cos(rot), 0.0,
                    0.0, 0.0, 1.0);
    gp_Mat scale;
    scale.SetDiagonal(sx, sy, sz);
    const gp_Mat m = ocs * rz * scale;
    const bool mirrored = m.Determinant() < 0.0;
    const bool uniform = std::abs(std::abs(sx) - std::abs(sy)) < 1e-12 * std::abs(sx) &&
                         std::abs(std::abs(sx) - std::abs(sz)) < 1e-12 * std::abs(sx);
    TopoDS_Compound block_edges;
    BRep_Builder bb;
    bb.MakeCompound(block_edges);
    for (const TopoDS_Edge& edge : sub.edges) bb.Add(block_edges, edge);

    for (long r = 0; r < rows; ++r) {
        for (long c = 0; c < cols; ++c) {
            const gp_XYZ offset = rz * gp_XYZ(c * col_step, r * row_step, 0.0);
            const gp_XYZ t = ocs * (ins + offset) - m * found->second.base;
            // Uniform placements move B-rep exactly; a non-uniform scale needs GTransform
            // (which converts the geometry to NURBS).
            auto place = [&](const TopoDS_Shape& shape) -> TopoDS_Shape {
                try {
                    if (!uniform) throw Standard_Failure("non-uniform block scale");
                    gp_Trsf tr;
                    tr.SetValues(m(1, 1), m(1, 2), m(1, 3), t.X(), m(2, 1), m(2, 2), m(2, 3),
                                 t.Y(), m(3, 1), m(3, 2), m(3, 3), t.Z());
                    return BRepBuilderAPI_Transform(shape, tr, Standard_True).Shape();
                } catch (const Standard_Failure&) {
                    return BRepBuilderAPI_GTransform(shape, gp_GTrsf(m, t), Standard_True).Shape();
                }
            };
            if (!sub.edges.empty()) {
                const TopoDS_Shape placed = place(block_edges);
                for (TopExp_Explorer ex(placed, TopAbs_EDGE); ex.More(); ex.Next())
                    out.edges.push_back(TopoDS::Edge(ex.Current()));
            }
            for (const DxfAcisBody& body : sub.acis_bodies) {
                try {
                    out.acis_bodies.push_back({body.layer, place(body.shape), body.solid});
                } catch (const Standard_Failure& ex) {
                    ++out.acis;
                    out.acis_notes.push_back("block '" + dxf::get_s(e, 2) +
                                             "': an ACIS body could not be placed (" +
                                             ex.GetMessageString() + ")");
                }
            }
            for (const auto& [mesh_layer, mesh] : sub.meshes) {
                MeshData& dst = out.meshes[mesh_layer];
                const auto base = static_cast<uint32_t>(dst.vertex_count());
                for (std::size_t k = 0; k + 2 < mesh.verts.size(); k += 3) {
                    const gp_XYZ p = m * gp_XYZ(mesh.verts[k], mesh.verts[k + 1], mesh.verts[k + 2]) + t;
                    dst.verts.insert(dst.verts.end(), {p.X(), p.Y(), p.Z()});
                }
                for (std::size_t k = 0; k + 2 < mesh.tris.size(); k += 3) {
                    // A mirrored placement flips handedness; keep the outward winding.
                    dst.tris.push_back(base + mesh.tris[k]);
                    dst.tris.push_back(base + mesh.tris[k + (mirrored ? 2 : 1)]);
                    dst.tris.push_back(base + mesh.tris[k + (mirrored ? 1 : 2)]);
                }
                out.triangles += mesh.triangle_count();
            }
        }
    }
}

// Entities -> edges / meshes / ACIS bodies (port of _entities_to_edges plus blocks, OCS, mesh
// and ACIS entities). OCCT failures in individual entities remain skippable; admission
// failures reject the entire drawing and must never be swallowed as an empty entity.
void collect_entities(const std::vector<dxf::Entity>& entities, const dxf::Blocks& blocks,
                      const AcisPayloads& payloads, const std::string& parent_layer, int depth,
                      DxfGeometry& out, DxfExpansionBudget& budget) {
    for (std::size_t i = 0; i < entities.size(); ++i) {
        const dxf::Entity& e = entities[i];
        const std::string layer = effective_layer(e, parent_layer);
        if (is_acis_entity(e.type)) {
            const std::size_t before = out.acis_bodies.size();
            add_acis_entity(e, layer, payloads, out);
            for (std::size_t k = before; k < out.acis_bodies.size(); ++k)
                charge_acis_geometry(out.acis_bodies[k], 1, budget);
            continue;
        }
        const std::size_t edges_before = out.edges.size();
        const std::size_t tris_before = out.triangles;
        const std::size_t acis_before = out.acis_bodies.size();
        try {
            if (e.type == "LINE") {
                const gp_Pnt p1(dxf::get_d(e, 10), dxf::get_d(e, 20), dxf::get_d(e, 30));
                const gp_Pnt p2(dxf::get_d(e, 11), dxf::get_d(e, 21), dxf::get_d(e, 31));
                if (p1.Distance(p2) > 1e-9) out.edges.push_back(BRepBuilderAPI_MakeEdge(p1, p2));
            } else if (e.type == "CIRCLE" || e.type == "ARC") {
                // Center is in the entity's OCS; angles run CCW about its extrusion.
                const gp_Pnt c(dxf::get_d(e, 10), dxf::get_d(e, 20), dxf::get_d(e, 30));
                const gp_Circ circ(gp_Ax2(c, gp_Dir(0, 0, 1)), dxf::get_d(e, 40));
                if (e.type == "CIRCLE") {
                    out.edges.push_back(BRepBuilderAPI_MakeEdge(circ));
                } else {
                    const double a1 = dxf::get_d(e, 50) * kPi / 180.0;
                    double a2 = dxf::get_d(e, 51) * kPi / 180.0;
                    while (a2 <= a1) a2 += 2.0 * kPi;
                    out.edges.push_back(BRepBuilderAPI_MakeEdge(circ, a1, a2));
                }
                const gp_Dir n = dxf::extrusion(e);
                if (!dxf::is_world_z(n)) transform_edges(out.edges, edges_before, dxf::ocs_trsf(n));
            } else if (e.type == "LWPOLYLINE") {
                dxf::lwpolyline_to_edges(e, out.edges);
                const gp_Dir n = dxf::extrusion(e);
                if (!dxf::is_world_z(n)) transform_edges(out.edges, edges_before, dxf::ocs_trsf(n));
            } else if (e.type == "POLYLINE") {
                polyline_old_style(entities, i, layer, out);
            } else if (e.type == "SPLINE") {
                TopoDS_Edge se = dxf::spline_edge(e);
                if (!se.IsNull()) out.edges.push_back(se);
            } else if (e.type == "ELLIPSE") {
                TopoDS_Edge ee = dxf::ellipse_edge(e);
                if (!ee.IsNull()) out.edges.push_back(ee);
            } else if (e.type == "3DFACE") {
                std::vector<gp_Pnt> corners;
                for (int k = 0; k < 4; ++k)
                    corners.emplace_back(dxf::get_d(e, 10 + k), dxf::get_d(e, 20 + k),
                                         dxf::get_d(e, 30 + k));
                add_polygon(out, layer, corners);
            } else if (e.type == "INSERT") {
                place_insert(e, blocks, payloads, layer, depth, out, budget);
            }
            // TEXT / DIMENSION / HATCH / … produce nothing and count as skipped below.
        } catch (const Standard_Failure&) {
            // one bad entity must not abort the whole import
        } catch (const OpError&) {
            throw;
        } catch (const std::exception&) {
            // ditto for any non-OCCT exception (e.g. a malformed numeric tag)
        }
        if (e.type != "INSERT") {
            DxfExpansionBudget::charge(budget.edges, out.edges.size() - edges_before, 1,
                                       DxfExpansionBudget::kGeometry, "edges");
            DxfExpansionBudget::charge(budget.vertices, out.edges.size() - edges_before, 2,
                                       DxfExpansionBudget::kGeometry * 3, "edge vertices");
            DxfExpansionBudget::charge(budget.triangles, out.triangles - tris_before, 1,
                                       DxfExpansionBudget::kGeometry, "triangles");
            DxfExpansionBudget::charge(budget.vertices, out.triangles - tris_before, 3,
                                       DxfExpansionBudget::kGeometry * 3, "vertices");
        }
        if (out.edges.size() == edges_before && out.triangles == tris_before &&
            out.acis_bodies.size() == acis_before)
            ++out.skipped;
    }
}

// Stitch loose edges into connected wires; with `make_faces`, closed wires → Faces (kept as a
// Wire when the face build fails, e.g. non-planar/self-intersecting); open wires always stay
// reference wires. Port of _assemble_wires.
void dxf_assemble_wires(const std::vector<TopoDS_Edge>& edges, bool make_faces,
                        TopoDS_Compound& out) {
    BRep_Builder bb;
    bb.MakeCompound(out);
    if (edges.empty()) return;

    Handle(TopTools_HSequenceOfShape) seq = new TopTools_HSequenceOfShape;
    for (const TopoDS_Edge& e : edges) seq->Append(e);
    Handle(TopTools_HSequenceOfShape) wires = new TopTools_HSequenceOfShape;
    ShapeAnalysis_FreeBounds::ConnectEdgesToWires(seq, 1e-6, Standard_False, wires);

    for (int i = 1; i <= wires->Length(); ++i) {
        TopoDS_Wire w = TopoDS::Wire(wires->Value(i));
        bool added_face = false;
        if (make_faces && w.Closed()) {
            try {
                BRepBuilderAPI_MakeFace mf(w, Standard_True);
                if (mf.IsDone()) {
                    bb.Add(out, mf.Face());
                    added_face = true;
                }
            } catch (const Standard_Failure&) {
                // non-planar / self-intersecting closed wire — keep it as a wire below
            }
        }
        if (!added_face) bb.Add(out, w);
    }
}

// A parsed DXF: one profile compound of faces (closed regions) + free wires (open), mirroring
// formats_dxf.py::bodies_from_doc, one mesh-built body per layer, and every ACIS body.
// Millimetres. `insunits` is the drawing's $INSUNITS code (0 when absent).
struct DxfImport {
    TopoDS_Shape profile;
    std::vector<std::pair<std::string, TopoDS_Shape>> meshes;  // (layer, body)
    std::vector<DxfAcisBody> acis_bodies;
    int skipped = 0;
    int acis = 0;
    std::vector<std::string> acis_notes;
    int insunits = 0;
};

int insunits_code(const std::vector<dxf::Tag>& tags) {
    for (std::size_t i = 0; i + 1 < tags.size(); ++i)
        if (tags[i].code == 9 && tags[i].value == "$INSUNITS" && tags[i + 1].code == 70) {
            try { return std::stoi(tags[i + 1].value); } catch (const std::exception&) { return 0; }
        }
    return 0;
}

// DXF R2013+ keeps ACIS bodies as SAB in the ACDSDATA section: each ACDSRECORD names its
// entity (AcDbDs::ID -> 320 handle) and carries the bytes as 310 hex chunks after ASM_Data
// (94 = byte count).
AcisPayloads parse_acdsdata(const std::vector<dxf::Tag>& tags) {
    AcisPayloads out;
    auto hex = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    for (const dxf::Entity& rec : dxf::parse_section(tags, "ACDSDATA")) {
        if (rec.type != "ACDSRECORD") continue;
        std::string handle, bytes;
        long size = -1;
        enum { kNone, kId, kAsm } field = kNone;
        for (const dxf::Tag& t : rec.tags) {
            if (t.code == 2) {
                field = t.value == "AcDbDs::ID" ? kId : t.value == "ASM_Data" ? kAsm : kNone;
            } else if (field == kId && t.code == 320) {
                handle = t.value;
            } else if (field == kAsm && t.code == 94) {
                try { size = std::stol(t.value); } catch (const std::exception&) { size = -1; }
            } else if (field == kAsm && t.code == 310) {
                for (std::size_t k = 0; k + 1 < t.value.size(); k += 2) {
                    const int hi = hex(t.value[k]), lo = hex(t.value[k + 1]);
                    if (hi < 0 || lo < 0) break;
                    bytes.push_back(static_cast<char>(hi * 16 + lo));
                }
            }
        }
        if (size >= 0 && static_cast<std::size_t>(size) < bytes.size()) bytes.resize(size);
        if (!handle.empty() && !bytes.empty()) out[handle_key(handle)] = std::move(bytes);
    }
    return out;
}

// Fold the native DWG reader's ACIS entities into the converted DXF: payloads by handle, and
// any entity dwg2dxf dropped (LibreDWG skips SURFACE classes) is re-created as a stub entity
// in the model space or block that owns it, on its layer.
void merge_native_acis(const DwgNativeInfo& native, const std::vector<dxf::Tag>& tags,
                       std::vector<dxf::Entity>& entities, dxf::Blocks& blocks,
                       AcisPayloads& payloads, std::vector<std::string>& notes) {
    std::set<std::string> present;
    auto note_handles = [&present](const std::vector<dxf::Entity>& list) {
        for (const dxf::Entity& e : list) present.insert(handle_key(dxf::get_s(e, 5)));
    };
    note_handles(entities);
    for (const auto& [name, block] : blocks) note_handles(block.entities);

    std::unordered_map<std::string, std::string> layer_names;   // LAYER handle -> name
    std::unordered_map<std::string, std::string> record_names;  // BLOCK_RECORD handle -> name
    for (const dxf::Entity& t : dxf::parse_section(tags, "TABLES")) {
        if (t.type == "LAYER") layer_names[handle_key(dxf::get_s(t, 5))] = dxf::get_s(t, 2);
        if (t.type == "BLOCK_RECORD") record_names[handle_key(dxf::get_s(t, 5))] = dxf::get_s(t, 2);
    }
    auto hex_handle = [](std::uint64_t h) {
        char buf[24];
        std::snprintf(buf, sizeof(buf), "%llX", static_cast<unsigned long long>(h));
        return std::string(buf);
    };
    for (const DwgAcisEntity& a : native.acis) {
        const std::string key = hex_handle(a.handle);
        if (!a.data.empty()) payloads[key] = a.data;
        if (present.count(key)) continue;
        dxf::Entity stub;
        stub.type = a.type;
        stub.tags.push_back({5, key});
        const auto layer = layer_names.find(hex_handle(a.layer));
        stub.tags.push_back({8, layer == layer_names.end() ? std::string("0") : layer->second});
        const auto record = record_names.find(hex_handle(a.owner));
        const std::string owner = record == record_names.end() ? std::string() : record->second;
        std::string upper = owner;
        for (char& c : upper) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        if (upper == "*MODEL_SPACE") {
            entities.push_back(std::move(stub));
        } else if (upper.rfind("*PAPER_SPACE", 0) == 0) {
            notes.push_back(a.type + " " + key + " is in paper space and was not imported");
        } else if (auto b = blocks.find(owner); b != blocks.end()) {
            b->second.entities.push_back(std::move(stub));
        } else {
            notes.push_back(a.type + " " + key + " has an unknown owner " + hex_handle(a.owner) +
                            " and was not imported");
        }
    }
    notes.insert(notes.end(), native.warnings.begin(), native.warnings.end());
}

DxfImport read_dxf(const std::string& path, const DwgNativeInfo* native = nullptr) {
    const std::vector<dxf::Tag> tags = dxf::read_tags(path);
    const double unit_to_mm = dxf::insunits_scale(tags, path);
    std::vector<dxf::Entity> entities = dxf::parse_entities(tags);
    dxf::Blocks blocks = dxf::parse_blocks(tags);
    AcisPayloads payloads = parse_acdsdata(tags);
    DxfGeometry g;
    if (native) merge_native_acis(*native, tags, entities, blocks, payloads, g.acis_notes);
    DxfExpansionBudget budget;
    admit_dxf_expansion(entities, blocks, 0, 1, budget);
    collect_entities(entities, blocks, payloads, std::string(), 0, g, budget);
    DxfImport r;
    r.skipped = g.skipped;
    r.acis = g.acis;
    r.acis_notes = std::move(g.acis_notes);
    r.insunits = insunits_code(tags);
    gp_Trsf scale;
    if (unit_to_mm != 1.0) scale.SetScale(gp_Pnt(0, 0, 0), unit_to_mm);
    if (!g.edges.empty()) {
        // A 2D drawing's closed loops become profile faces (to extrude or measure). Beside 3D
        // model content (ACIS solids/surfaces, meshes) the lines and circles are the author's
        // construction curves — AutoCAD shows them as curves, so filling them would put lids
        // on open tubes and discs inside parts. They stay wires there.
        const bool has_3d = !g.acis_bodies.empty() || !g.meshes.empty() || g.acis > 0;
        TopoDS_Compound comp;
        dxf_assemble_wires(g.edges, !has_3d, comp);
        r.profile = comp;
        if (unit_to_mm != 1.0)
            r.profile = BRepBuilderAPI_Transform(comp, scale, Standard_True).Shape();
    }
    for (auto& [layer, mesh] : g.meshes) {
        if (mesh.triangle_count() == 0) continue;
        if (unit_to_mm != 1.0)
            for (double& v : mesh.verts) v *= unit_to_mm;
        TopoDS_Shape body = mesh_to_shape(mesh);
        if (!body.IsNull()) r.meshes.emplace_back(layer, body);
    }
    for (DxfAcisBody& b : g.acis_bodies) {
        if (unit_to_mm != 1.0) b.shape = BRepBuilderAPI_Transform(b.shape, scale, Standard_True).Shape();
        r.acis_bodies.push_back(std::move(b));
    }
    return r;
}

// DXF (or a DWG converted to `dxf_path`, with its natively read ACIS data) -> named bodies:
// the profile is named after the source file; each mesh body after the file and its layer;
// ACIS solids/surfaces `<stem>_solidN` / `<stem>_surfaceN` (bare stem when a body is alone).
// Refuses by name when nothing importable remains; every ACIS entity that could not be read
// is reported by type and handle, never dropped silently.
std::vector<std::pair<std::string, TopoDS_Shape>> dxf_bodies(
    const std::string& dxf_path, const std::string& source_path, const std::string& fmt,
    std::vector<std::string>* warnings, int* source_units,
    const DwgNativeInfo* native = nullptr) {
    const DxfImport r = read_dxf(dxf_path, native);
    if (source_units) *source_units = r.insunits;
    std::string stem = std::filesystem::path(source_path).stem().string();
    if (stem.empty()) stem = fmt;
    std::vector<std::pair<std::string, TopoDS_Shape>> out;
    const std::size_t total = (r.profile.IsNull() ? 0 : 1) + r.meshes.size() + r.acis_bodies.size();
    if (!r.profile.IsNull()) out.emplace_back(stem, r.profile);
    for (const auto& [layer, body] : r.meshes)
        out.emplace_back(total == 1 ? stem : stem + "_" + layer, body);
    int solids = 0, surfaces = 0;
    for (const DxfAcisBody& b : r.acis_bodies) {
        const std::string kind = b.solid ? "_solid" + std::to_string(++solids)
                                         : "_surface" + std::to_string(++surfaces);
        out.emplace_back(total == 1 ? stem : stem + kind, b.shape);
    }
    std::string label = fmt;
    for (char& c : label) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    std::string acis_note;
    for (const std::string& n : r.acis_notes) acis_note += (acis_note.empty() ? "" : "; ") + n;
    if (out.empty())
        throw OpError("import_failed",
                      label + " '" + source_path + "' has no importable geometry (" +
                          std::to_string(r.skipped) +
                          " unsupported/empty entity(ies) skipped — text, dimensions and "
                          "hatches are not imported" +
                          (acis_note.empty() ? std::string() : "; ACIS: " + acis_note) + ").",
                      {{"path", source_path}, {"format", fmt}, {"skipped", r.skipped},
                       {"acis_entities", r.acis}});
    if (warnings)
        for (const std::string& n : r.acis_notes) warnings->push_back(label + " ACIS: " + n);
    return out;
}

// $INSUNITS code for an ACIS header's millimetres-per-unit (0 = none of the DXF units).
int insunits_for_mm_per_unit(double mm) {
    struct Unit { int code; double mm; };
    static const Unit kUnits[] = {{4, 1.0},    {1, 25.4}, {2, 304.8}, {5, 10.0},
                                  {6, 1000.0}, {13, 0.001}, {9, 0.0254}, {14, 100.0}};
    for (const Unit& u : kUnits)
        if (mm > 0.0 && std::abs(mm - u.mm) <= 1e-9 * u.mm) return u.code;
    return 0;
}

// ACIS bodies -> named import bodies scaled to millimetres (`<stem>`, or `<stem>_solidN` /
// `<stem>_surfaceN` when there are several).
std::vector<std::pair<std::string, TopoDS_Shape>> name_acis_bodies(
    std::vector<AcisBody>& bodies, double mm_per_unit, const std::string& stem) {
    gp_Trsf scale;
    if (mm_per_unit > 0.0 && mm_per_unit != 1.0) scale.SetScale(gp_Pnt(0, 0, 0), mm_per_unit);
    std::vector<std::pair<std::string, TopoDS_Shape>> out;
    int solids = 0, surfaces = 0;
    for (AcisBody& b : bodies) {
        if (b.shape.IsNull()) continue;
        TopoDS_Shape shape = scale.Form() == gp_Identity
                                 ? b.shape
                                 : BRepBuilderAPI_Transform(b.shape, scale, Standard_True).Shape();
        const std::string suffix = b.solid ? "_solid" + std::to_string(++solids)
                                           : "_surface" + std::to_string(++surfaces);
        out.emplace_back(bodies.size() == 1 ? stem : stem + suffix, std::move(shape));
    }
    return out;
}

// Standalone ACIS file (.sat text, .sab binary), as AutoCAD ACISOUT and most modelers write.
std::vector<std::pair<std::string, TopoDS_Shape>> sat_bodies(const std::string& path,
                                                             std::vector<std::string>* warnings,
                                                             int* source_units) {
    std::ifstream is(path, std::ios::binary);
    if (!is) throw OpError("import_failed", "SAT: cannot open input", {{"path", path}});
    const std::string data((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    AcisReadResult r;
    try {
        r = read_acis(data);
    } catch (const std::exception& e) {
        throw OpError("import_failed", std::string("ACIS: ") + e.what(),
                      {{"path", path}, {"format", "sat"}});
    }
    if (warnings)
        for (const std::string& w : r.warnings) warnings->push_back("ACIS: " + w);
    if (source_units) *source_units = insunits_for_mm_per_unit(r.mm_per_unit);
    auto out = name_acis_bodies(r.bodies, r.mm_per_unit, std::filesystem::path(path).stem().string());
    if (out.empty())
        throw OpError("import_failed", "ACIS file '" + path + "' holds no body",
                      {{"path", path}, {"format", "sat"}});
    return out;
}

// A DWG whose 2D entities cannot be converted (no LibreDWG dwg2dxf) still yields its model
// space ACIS solids/surfaces from the native reader, in ACIS header units.
std::vector<std::pair<std::string, TopoDS_Shape>> dwg_native_only_bodies(
    const DwgNativeInfo& native, const std::string& path, std::vector<std::string>* warnings,
    int* source_units) {
    std::vector<AcisBody> bodies;
    double mm_per_unit = 0.0;
    std::vector<std::string> notes = native.warnings;
    for (const DwgAcisEntity& a : native.acis) {
        char key[24];
        std::snprintf(key, sizeof(key), "%llX", static_cast<unsigned long long>(a.handle));
        if (a.owner != native.model_space) {
            notes.push_back(a.type + " " + key +
                            " lies outside model space (block or paper space) and needs LibreDWG "
                            "dwg2dxf to be placed");
            continue;
        }
        if (a.data.empty()) {
            notes.push_back(a.type + " " + key + " carries no ACIS data");
            continue;
        }
        try {
            AcisReadResult r = read_acis(a.data);
            if (mm_per_unit <= 0.0) mm_per_unit = r.mm_per_unit;
            for (const std::string& w : r.warnings) notes.push_back(a.type + " " + key + ": " + w);
            for (AcisBody& b : r.bodies) bodies.push_back(std::move(b));
        } catch (const std::exception& e) {
            notes.push_back(a.type + " " + key + ": " + e.what());
        }
    }
    if (source_units) *source_units = insunits_for_mm_per_unit(mm_per_unit);
    auto out = name_acis_bodies(bodies, mm_per_unit, std::filesystem::path(path).stem().string());
    if (out.empty())
        throw OpError("import_failed",
                      std::string("DWG import needs LibreDWG's `dwg2dxf` for this file's 2D "
                                  "entities, which was not found. ") + kLibredwgInstallHint,
                      {{"path", path}, {"format", "dwg"}});
    if (warnings) {
        warnings->push_back("DWG: only the 3D solids/surfaces were imported; lines, arcs, "
                            "text and blocks need LibreDWG's `dwg2dxf`. " +
                            std::string(kLibredwgInstallHint));
        for (const std::string& n : notes) warnings->push_back("DWG ACIS: " + n);
    }
    return out;
}

}  // namespace

// Keep solid ordinals stable, then append maximal surface/curve/point remnants.
// A surface-only import remains one body, preserving its existing grouping.
std::vector<TopoDS_Shape> split_import_bodies(const TopoDS_Shape& shape) {
    std::vector<TopoDS_Shape> solids;
    for (TopExp_Explorer ex(shape, TopAbs_SOLID); ex.More(); ex.Next())
        solids.push_back(ex.Current());
    if (solids.empty()) {
        if (!shape.IsNull()) solids.push_back(shape);
        return solids;
    }

    TopTools_MapOfShape covered;
    for (const TopoDS_Shape& solid : solids) TopExp::MapShapes(solid, covered);
    // Membership includes location but ignores orientation: an explicitly repeated
    // face/edge of a solid or shell must not become another body. Visit higher-order
    // topology first so shell faces and face wires are covered before their own pass.
    for (TopAbs_ShapeEnum kind :
         {TopAbs_SHELL, TopAbs_FACE, TopAbs_WIRE, TopAbs_EDGE, TopAbs_VERTEX}) {
        for (TopExp_Explorer ex(shape, kind); ex.More(); ex.Next()) {
            if (covered.Contains(ex.Current())) continue;
            // A different shell/wire can still wrap already-owned faces/edges.
            // In that case retain only its uncovered members on the later pass.
            if (kind == TopAbs_SHELL || kind == TopAbs_WIRE) {
                const TopAbs_ShapeEnum member =
                    kind == TopAbs_SHELL ? TopAbs_FACE : TopAbs_EDGE;
                bool overlaps = false;
                for (TopExp_Explorer part(ex.Current(), member); part.More(); part.Next()) {
                    if (covered.Contains(part.Current())) {
                        overlaps = true;
                        break;
                    }
                }
                if (overlaps) continue;
            }
            solids.push_back(ex.Current());
            TopExp::MapShapes(ex.Current(), covered);
        }
    }
    return solids;
}

std::string dwg_to_temp_dxf(const std::string& path) {
    std::error_code ec;
    std::filesystem::path tmp_dir = std::filesystem::temp_directory_path(ec);
    if (ec) tmp_dir = std::filesystem::path(".");
    std::random_device random;
    std::filesystem::path out_dxf;
    for (int attempt = 0; attempt < 64; ++attempt) {
        const auto nonce = (static_cast<std::uint64_t>(random()) << 32) ^ random();
        out_dxf = tmp_dir / ("chudware-dwg2dxf-" + std::to_string(nonce) + ".dxf");
#ifdef _WIN32
        HANDLE reservation = CreateFileW(out_dxf.wstring().c_str(), GENERIC_WRITE, 0, nullptr,
                                         CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY, nullptr);
        if (reservation != INVALID_HANDLE_VALUE) { CloseHandle(reservation); break; }
#else
        const int reservation = ::open(out_dxf.c_str(), O_CREAT | O_EXCL | O_WRONLY, 0600);
        if (reservation >= 0) { ::close(reservation); break; }
#endif
        out_dxf.clear();
    }
    if (out_dxf.empty())
        throw OpError("import_failed", "DWG import could not reserve a private temporary output.",
                      {{"path", path}, {"format", "dwg"}});

    // -y: dwg2dxf refuses to overwrite the reserved (already existing) output without it.
    const bool converted =
        run_libredwg("dwg2dxf", {"-y", "-o", out_dxf.string(), path});
    if (converted && std::filesystem::exists(out_dxf, ec) &&
        std::filesystem::file_size(out_dxf, ec) > 0 && !ec)
        return out_dxf.string();

    std::filesystem::remove(out_dxf, ec);
    const std::string reason = libredwg_tool("dwg2dxf").empty()
                                   ? std::string("DWG import needs LibreDWG's `dwg2dxf`, which was "
                                                 "not found. ") + kLibredwgInstallHint
                                   : std::string("LibreDWG dwg2dxf could not convert this DWG; "
                                                 "save it as DXF or STEP from the source CAD.");
    throw OpError("import_failed", reason, {{"path", path}, {"format", "dwg"}});
}

std::size_t dxf_entity_count(const std::string& path) {
    return dxf::parse_entities(dxf::read_tags(path)).size();
}

namespace {

#ifdef CHUDWARE_WITH_OPENNURBS
// A named mesh pulled out of a .3dm object — the C++ analogue of the oracle's
// (name, MeshBody(verts float32, tris uint32)) tuple. verts live in MeshData as
// doubles, but the source is ON_3fPoint (float32, m_V), so every value widens
// exactly and a float32 reconstruction on the gate side is bit-identical to the
// oracle's np.float32 verts.
struct OnNamedMesh {
    std::string name;
    MeshData mesh;
};

// Triangulate one ON_Mesh EXACTLY as engine/doc/formats.py::_rhino_mesh_to_arrays:
//   verts = float32 [(V.x, V.y, V.z) for each m_V]
//   for each face: a,b,c = vi[0..2], d = vi[3]; emit (a,b,c); if d != c emit (a,c,d)
// rhino3dm's Faces[fi] always yields a 4-tuple (vi[0..3]); a triangle has
// vi[3]==vi[2], so the quad test reduces to vi[3] != vi[2] — IDENTICAL to the
// oracle's `d != c`.
MeshData on_mesh_to_arrays(const ON_Mesh& mesh) {
    MeshData out;
    const int nv = mesh.m_V.Count();
    out.verts.reserve(static_cast<std::size_t>(nv) * 3);
    for (int i = 0; i < nv; ++i) {
        const ON_3fPoint& p = mesh.m_V[i];  // float32, exactly like rhino3dm's mesh.Vertices[i]
        out.verts.push_back(static_cast<double>(p.x));
        out.verts.push_back(static_cast<double>(p.y));
        out.verts.push_back(static_cast<double>(p.z));
    }
    const int nf = mesh.m_F.Count();
    out.tris.reserve(static_cast<std::size_t>(nf) * 6);
    for (int fi = 0; fi < nf; ++fi) {
        const ON_MeshFace& f = mesh.m_F[fi];
        const uint32_t a = static_cast<uint32_t>(f.vi[0]);
        const uint32_t b = static_cast<uint32_t>(f.vi[1]);
        const uint32_t c = static_cast<uint32_t>(f.vi[2]);
        const uint32_t d = static_cast<uint32_t>(f.vi[3]);
        out.tris.push_back(a);
        out.tris.push_back(b);
        out.tris.push_back(c);
        if (d != c) {  // a real quad → second triangle (a, c, d)
            out.tris.push_back(a);
            out.tris.push_back(c);
            out.tris.push_back(d);
        }
    }
    return out;
}

// Read every mesh-bearing object out of a .3dm, BIT-IDENTICAL to
// engine/doc/formats.py::_import_3dm. Iterates model objects in the SAME order
// rhino3dm's `model.Objects` does — ONX_ModelComponentIterator over
// ON_ModelComponent::Type::ModelGeometry (manifest order; RenderLights, which
// follow in rhino3dm's order, carry no mesh and are skipped anyway). For each
// object: if the geometry is an ON_Mesh, use it; if an ON_Extrusion, pull its
// CACHED render mesh (ON::any_mesh — the same stored mesh GetMesh(MeshType.Any)
// returns; NO meshing is performed); skip when there is no mesh / 0 verts / 0
// faces. Name = the object attribute name, else "<stem>_<n>".
std::vector<OnNamedMesh> read_3dm_objects(const std::string& path) {
    ONX_Model model;
    if (!model.Read(path.c_str(), nullptr))
        throw OpError("import_failed", "could not read Rhino file '" + path + "'",
                      {{"path", path}, {"format", "3dm"}});

    const std::string stem = std::filesystem::path(path).stem().string();
    std::vector<OnNamedMesh> out;
    ONX_ModelComponentIterator it(model, ON_ModelComponent::Type::ModelGeometry);
    for (ON_ModelComponentReference cref = it.FirstComponentReference();
         !cref.IsEmpty(); cref = it.NextComponentReference()) {
        const ON_ModelGeometryComponent* gc =
            ON_ModelGeometryComponent::Cast(cref.ModelComponent());
        if (nullptr == gc) continue;
        const ON_Geometry* g = gc->Geometry(nullptr);
        if (nullptr == g) continue;

        const ON_Mesh* mesh = ON_Mesh::Cast(g);
        if (nullptr == mesh) {
            const ON_Extrusion* ext = ON_Extrusion::Cast(g);
            if (nullptr != ext)
                mesh = ext->m_mesh_cache.Mesh(ON::any_mesh);  // cached render mesh; no generation
        }
        if (nullptr == mesh || mesh->m_V.Count() == 0 || mesh->m_F.Count() == 0)
            continue;  // matches the oracle's skip (no mesh / 0 verts / 0 faces)

        OnNamedMesh nm;
        const ON_3dmObjectAttributes* attrs = gc->Attributes(nullptr);
        std::string name;
        if (nullptr != attrs && attrs->m_name.IsNotEmpty()) {
            // ON_wString -> UTF-8, matching the Python str the oracle reads from
            // obj.Attributes.Name. ON_String(const ON_wString&) does the UTF-16->UTF-8
            // conversion.
            ON_String s(attrs->m_name);
            if (s.Array() != nullptr && s.Length() > 0)
                name.assign(s.Array(), static_cast<std::size_t>(s.Length()));
        }
        if (name.empty())
            name = stem + "_" + std::to_string(out.size() + 1);
        nm.name = name;
        nm.mesh = on_mesh_to_arrays(*mesh);
        out.push_back(std::move(nm));
    }

    if (out.empty())
        throw OpError("import_failed",
                      "'" + path + "' has no mesh geometry rhino3dm can read "
                      "(NURBS/Brep objects need re-export from Rhino as STEP, or with render meshes)",
                      {{"path", path}, {"format", "3dm"}});
    return out;
}

// Write named meshes to a .3dm, mirroring engine/doc/formats.py::_export_3dm:
// one ON_Mesh per body (vertices via SetVertex(ON_3fPoint) == float32, faces via
// SetTriangle), added with ON_3dmObjectAttributes(name) and written with
// ONX_Model::Write(version=0). Skips empty meshes; the version-0 archive is the
// same the rhino3dm File3dm.Write(path, 0) produces.
void write_3dm_meshes(const std::vector<OnNamedMesh>& meshes, const std::string& path) {
    ONX_Model model;
    int added = 0;
    for (const OnNamedMesh& nm : meshes) {
        const MeshData& m = nm.mesh;
        if (m.triangle_count() == 0) continue;
        ON_Mesh on_mesh;
        const std::size_t nv = m.vertex_count();
        for (std::size_t i = 0; i < nv; ++i) {
            // float32 store, exactly like rhino3dm's Mesh.Vertices.Add(x,y,z)
            // (which builds an ON_3fPoint(float,float,float)).
            on_mesh.SetVertex(static_cast<int>(i),
                              ON_3fPoint(static_cast<float>(m.verts[3 * i]),
                                         static_cast<float>(m.verts[3 * i + 1]),
                                         static_cast<float>(m.verts[3 * i + 2])));
        }
        const std::size_t nt = m.triangle_count();
        for (std::size_t i = 0; i < nt; ++i) {
            // SetTriangle(idx, a, b, c) is the ON-level equivalent of rhino3dm's
            // Mesh.Faces.AddFace(a,b,c) -> SetQuad(idx,a,b,c,c): a tri with vi[3]==vi[2].
            on_mesh.SetTriangle(static_cast<int>(i),
                                static_cast<int>(m.tris[3 * i]),
                                static_cast<int>(m.tris[3 * i + 1]),
                                static_cast<int>(m.tris[3 * i + 2]));
        }
        ON_3dmObjectAttributes attr;
        ON_wString wname(nm.name.c_str());  // UTF-8 (char) -> ON_wString
        attr.m_name = wname;
        model.AddModelGeometryComponent(&on_mesh, &attr);
        ++added;
    }
    if (added == 0)
        throw OpError("export_failed", "3DM export: no mesh geometry to write",
                      {{"format", "3dm"}, {"path", path}});
    if (!model.Write(path.c_str(), 0))
        throw OpError("export_failed", "openNURBS ONX_Model::Write returned failure",
                      {{"format", "3dm"}, {"path", path}});
}
#endif  // CHUDWARE_WITH_OPENNURBS

}  // namespace

std::string detect_format(const std::string& path) {
    std::string ext = lower(std::filesystem::path(path).extension().string());
    static const std::pair<const char*, const char*> kMap[] = {
        {".step", "step"}, {".stp", "step"}, {".igs", "iges"}, {".iges", "iges"},
        {".brep", "brep"}, {".stl", "stl"},  {".3mf", "3mf"},  {".obj", "obj"},
        {".ply", "ply"},   {".gltf", "gltf"},{".glb", "glb"},  {".3dm", "3dm"},
        {".sldprt", "sldprt"}, {".sldasm", "sldasm"}, {".x_t", "x_t"}, {".x_b", "x_b"},
        {".dxf", "dxf"}, {".dwg", "dwg"}, {".sat", "sat"}, {".sab", "sat"},
    };
    for (const auto& [e, f] : kMap)
        if (ext == e) return f;
    throw OpError("invalid_op", "cannot detect format from extension '" + ext + "'",
                  {{"reason", "unknown extension"}, {"path", path}});
}

double insunits_mm(int code) {
    double mm = 1.0;
    if (!dxf::units_mm(code, &mm))
        throw OpError("invalid_op", "unknown $INSUNITS code " + std::to_string(code),
                      {{"units", code}});
    return mm;
}

int parse_units(const std::string& units) {
    static const std::pair<const char*, int> kNames[] = {
        {"mm", 4}, {"millimeters", 4}, {"in", 1}, {"inch", 1}, {"inches", 1}, {"ft", 2},
        {"feet", 2}, {"cm", 5}, {"m", 6}, {"meters", 6}, {"um", 13}, {"microns", 13},
        {"mil", 9}, {"mils", 9}, {"dm", 14}};
    const std::string u = lower(dxf::trim(units));
    for (const auto& [name, code] : kNames)
        if (u == name) return code;
    if (!u.empty() && u.size() <= 3 &&
        std::all_of(u.begin(), u.end(), [](char c) { return c >= '0' && c <= '9'; })) {
        const int code = std::stoi(u);
        double mm = 0.0;
        if (code != 0 && dxf::units_mm(code, &mm)) return code;
    }
    throw OpError("invalid_op",
                  "units '" + units + "' is not one of mm, in, ft, cm, m, um, mil, dm or a "
                  "nonzero $INSUNITS code",
                  {{"units", units}});
}

// --------------------------------------------------------------- format registry (D4)
// Byte-parity port of engine/doc/formats.py::FORMAT_REGISTRY. Order and every field
// (fmt/label/exts/lane/can_import/can_export) match the Python oracle exactly —
// test_formats_registry compares the doc_formats payload against the oracle dump.
// Note the matrix describes the FULL engine surface (some lanes still route through
// deferred paths that raise a precise error at call time — same as the oracle).
const std::vector<FormatInfo>& format_registry() {
    static const std::vector<FormatInfo> kRegistry = {
        {"step",   "STEP",                {".step", ".stp"}, "brep",        true,  true},
        {"iges",   "IGES",                {".iges", ".igs"}, "brep",        true,  true},
        {"brep",   "BREP (OpenCASCADE)",  {".brep"},         "brep",        true,  true},
        {"stl",    "STL",                 {".stl"},          "mesh",        true,  true},
        {"3mf",    "3MF",                 {".3mf"},          "mesh",        true,  true},
        {"obj",    "Wavefront OBJ",       {".obj"},          "mesh",        true,  true},
        {"ply",    "Stanford PLY",        {".ply"},          "mesh",        true,  true},
        {"gltf",   "glTF",                {".gltf"},         "mesh",        true,  true},
        {"glb",    "glTF binary",         {".glb"},          "mesh",        true,  true},
        {"3dm",    "Rhino",               {".3dm"},          "rhino",       true,  true},
        // DXF / DWG: profiles import as faces + wires, 3D mesh entities as mesh bodies and
        // ACIS 3DSOLID/BODY/REGION/SURFACE entities as exact B-rep (acis_read.h; R2013+ DWG
        // data storage via dwg_native.h). Export writes solids/shells as ACIS 3DSOLID/BODY
        // (DXF R2000, native DWG R2000 writer) and 2D bodies as lines/arcs
        // (bodies_to_dxf_document). DWG 2D-entity import needs LibreDWG dwg2dxf (dwg.h).
        {"dxf",    "AutoCAD DXF",         {".dxf"},          "brep",        true,  true},
        {"dwg",    "AutoCAD DWG",         {".dwg"},          "brep",        true,  true},
        // ACIS solids (AutoCAD ACISIN/ACISOUT, Inventor, SpaceClaim...): .sat text / .sab
        // binary in; ACIS 7.0 .sat out.
        {"sat",    "ACIS SAT",            {".sat", ".sab"},  "brep",        true,  true},
        // Proprietary — import is experimental (SolidWorks COM -> STEP); we never write
        // these, so can_export is false (export STEP instead).
        {"sldprt", "SolidWorks part",     {".sldprt"},       "proprietary", true,  false},
        {"sldasm", "SolidWorks assembly", {".sldasm"},       "proprietary", true,  false},
        {"x_t",    "Parasolid (text)",    {".x_t"},          "proprietary", true,  false},
        {"x_b",    "Parasolid (binary)",  {".x_b"},          "proprietary", true,  false},
    };
    return kRegistry;
}

// --------------------------------------------------------------- SolidWorks COM → STEP
// Port of engine/doc/formats_solidworks.py (sw_com_export_step + solidworks_available) to
// native Win32 IDispatch late-binding (no SolidWorks type library on the link line). The
// automation sequence MIRRORS the proven Python path exactly:
//   Dispatch("SldWorks.Application") -> .Visible=false -> OpenDoc6(file,type,1,"",0,0)
//   -> model.SaveAs3(step,0,0) -> CloseDoc(file). Result verified by STEP file presence.
#ifdef _WIN32
namespace {

std::wstring widen_utf8(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(n, 0);
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}

// Minimal IDispatch late-binding call. `args` are in REVERSE order (DISPPARAMS rgvarg[0]
// is the LAST parameter). PROPERTYPUT names the value arg. Returns the HRESULT.
HRESULT sw_invoke(IDispatch* disp, WORD flags, const wchar_t* name, VARIANT* result, int argc,
                  VARIANT* args) {
    if (!disp) return E_POINTER;
    DISPID dispid;
    LPOLESTR nm = const_cast<LPOLESTR>(name);
    HRESULT hr = disp->GetIDsOfNames(IID_NULL, &nm, 1, LOCALE_USER_DEFAULT, &dispid);
    if (FAILED(hr)) return hr;
    DISPPARAMS dp{args, nullptr, (UINT)argc, 0};
    DISPID putid = DISPID_PROPERTYPUT;
    if (flags & DISPATCH_PROPERTYPUT) {
        dp.cNamedArgs = 1;
        dp.rgdispidNamedArgs = &putid;
    }
    return disp->Invoke(dispid, IID_NULL, LOCALE_USER_DEFAULT, flags, &dp, result, nullptr, nullptr);
}

VARIANT v_bstr(const std::wstring& s) {
    VARIANT v; VariantInit(&v); v.vt = VT_BSTR; v.bstrVal = SysAllocString(s.c_str()); return v;
}
VARIANT v_i4(LONG n) { VARIANT v; VariantInit(&v); v.vt = VT_I4; v.lVal = n; return v; }
VARIANT v_bool(bool b) {
    VARIANT v; VariantInit(&v); v.vt = VT_BOOL; v.boolVal = b ? VARIANT_TRUE : VARIANT_FALSE; return v;
}

}  // namespace

bool solidworks_available() {
    CLSID clsid;
    // CLSIDFromProgID hits the registry's SldWorks.Application registration without
    // launching SolidWorks (matches the Python winreg probe).
    return SUCCEEDED(CLSIDFromProgID(L"SldWorks.Application", &clsid));
}

std::string sw_export_step(const std::string& src_path) {
    std::string ext = lower(std::filesystem::path(src_path).extension().string());
    // swDocumentTypes_e: PART=1, ASSEMBLY=2. Parasolid (.x_t/.x_b) opens as a PART.
    const LONG doctype = (ext == ".sldasm") ? 2 : 1;
    const std::string stem = std::filesystem::path(src_path).stem().string();
    std::error_code ec;
    std::filesystem::path outdir =
        std::filesystem::temp_directory_path() / ("sw_" + std::to_string(GetTickCount64()));
    std::filesystem::create_directories(outdir, ec);
    const std::string out_step = (outdir / (stem + ".step")).string();

    const std::string actionable =
        ". Open the part in SolidWorks and 'Save As' STEP (.step), then import that.";

    HRESULT hrInit = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    const bool didInit = SUCCEEDED(hrInit);
    IDispatch* swApp = nullptr;
    IDispatch* model = nullptr;
    auto cleanup = [&]() {
        if (model) { model->Release(); model = nullptr; }
        if (swApp) { swApp->Release(); swApp = nullptr; }
        if (didInit) CoUninitialize();
    };
    auto fail = [&](const std::string& why) -> std::string {
        cleanup();
        throw OpError("import_failed", "SolidWorks COM import: " + why + actionable,
                      {{"path", src_path}});
    };

    CLSID clsid;
    if (FAILED(CLSIDFromProgID(L"SldWorks.Application", &clsid)))
        return fail("SolidWorks is not installed / COM-registered on this machine");
    if (FAILED(CoCreateInstance(clsid, nullptr, CLSCTX_LOCAL_SERVER | CLSCTX_INPROC_SERVER,
                                IID_PPV_ARGS(&swApp))) ||
        !swApp)
        return fail("could not start SolidWorks");

    {  // swApp.Visible = false
        VARIANT a = v_bool(false);
        sw_invoke(swApp, DISPATCH_PROPERTYPUT, L"Visible", nullptr, 1, &a);
        VariantClear(&a);
    }
    {  // model = swApp.OpenDoc6(file, type, options=1[Silent], config="", &errors, &warnings).
       // Errors/Warnings are [in,out] ByRef LONG — pass VT_I4|VT_BYREF or OpenDoc6 fails.
        LONG errCode = 0, warnCode = 0;
        VARIANT vWarn; VariantInit(&vWarn); vWarn.vt = VT_I4 | VT_BYREF; vWarn.plVal = &warnCode;
        VARIANT vErr;  VariantInit(&vErr);  vErr.vt = VT_I4 | VT_BYREF;  vErr.plVal = &errCode;
        VARIANT cfg = v_bstr(L""), opt = v_i4(1), typ = v_i4(doctype),
                fn = v_bstr(widen_utf8(src_path));
        VARIANT args[6] = {vWarn, vErr, cfg, opt, typ, fn};  // reverse order
        VARIANT res; VariantInit(&res);
        HRESULT hr = sw_invoke(swApp, DISPATCH_METHOD, L"OpenDoc6", &res, 6, args);
        // Don't VariantClear the byref VT_I4 wrappers (they alias stack LONGs); clear the rest.
        VariantClear(&cfg); VariantClear(&opt); VariantClear(&typ); VariantClear(&fn);
        if (SUCCEEDED(hr) && res.vt == VT_DISPATCH && res.pdispVal) {
            model = res.pdispVal;  // owns the ref
        } else {
            VariantClear(&res);
            // Fallback to the simpler OpenDoc(file, type) (older SW / odd marshalling).
            VARIANT a2[2] = {v_i4(doctype), v_bstr(widen_utf8(src_path))};
            VARIANT r2; VariantInit(&r2);
            HRESULT hr2 = sw_invoke(swApp, DISPATCH_METHOD, L"OpenDoc", &r2, 2, a2);
            for (VARIANT& a : a2) VariantClear(&a);
            if (SUCCEEDED(hr2) && r2.vt == VT_DISPATCH && r2.pdispVal)
                model = r2.pdispVal;
            else {
                VariantClear(&r2);
                return fail("could not open '" + src_path + "' (OpenDoc6 hr=0x" +
                            [&] { char b[16]; std::snprintf(b, sizeof b, "%08lx", (unsigned long)hr);
                                  return std::string(b); }() +
                            ", err=" + std::to_string(errCode) + ")");
            }
        }
    }
    {  // model.SaveAs3(step, version=0, options=0). Success verified by file presence below.
        VARIANT args[3] = {v_i4(0), v_i4(0), v_bstr(widen_utf8(out_step))};
        VARIANT res; VariantInit(&res);
        sw_invoke(model, DISPATCH_METHOD, L"SaveAs3", &res, 3, args);
        for (VARIANT& a : args) VariantClear(&a);
        VariantClear(&res);
    }
    {  // swApp.CloseDoc(file)
        VARIANT a = v_bstr(widen_utf8(src_path));
        sw_invoke(swApp, DISPATCH_METHOD, L"CloseDoc", nullptr, 1, &a);
        VariantClear(&a);
    }
    cleanup();
    if (!std::filesystem::exists(out_step) || std::filesystem::file_size(out_step, ec) == 0)
        throw OpError("import_failed",
                      "SolidWorks produced no STEP output (SaveAs failed)" + actionable,
                      {{"path", src_path}});
    return out_step;
}

#else   // non-Windows: SolidWorks/Parasolid import is unavailable.
bool solidworks_available() { return false; }
std::string sw_export_step(const std::string& src_path) {
    throw OpError("import_failed",
                  "SolidWorks/Parasolid import needs Windows + SolidWorks. Export STEP (.step) "
                  "from SolidWorks and import that.",
                  {{"path", src_path}});
}
#endif

// --------------------------------------------------------------------------- mesh soup

MeshData shape_to_mesh(const TopoDS_Shape& shape, const TessOptions& tess) {
    MeshData mesh;
    if (shape.IsNull()) return mesh;
    mesh_shape(shape, tess);
    // Walk every face's triangulation, baking the face location and concatenating into
    // one global vertex/triangle buffer (de-dup is not needed for the count/bbox/volume
    // invariant the gate checks; OBJ/PLY readers re-weld coincident verts identically on
    // both sides only after process=False, so we emit raw per-face triangles).
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        if (tri.IsNull() || tri->NbTriangles() == 0) continue;
        const gp_Trsf trsf = loc.Transformation();
        const bool reversed = (face.Orientation() == TopAbs_REVERSED);
        const uint32_t base = static_cast<uint32_t>(mesh.vertex_count());
        for (int ni = 1; ni <= tri->NbNodes(); ++ni) {
            gp_Pnt p = tri->Node(ni).Transformed(trsf);
            mesh.verts.push_back(p.X());
            mesh.verts.push_back(p.Y());
            mesh.verts.push_back(p.Z());
        }
        for (int ti = 1; ti <= tri->NbTriangles(); ++ti) {
            Standard_Integer n1, n2, n3;
            tri->Triangle(ti).Get(n1, n2, n3);
            if (reversed) std::swap(n2, n3);  // outward-consistent winding
            mesh.tris.push_back(base + static_cast<uint32_t>(n1 - 1));
            mesh.tris.push_back(base + static_cast<uint32_t>(n2 - 1));
            mesh.tris.push_back(base + static_cast<uint32_t>(n3 - 1));
        }
    }
    return mesh;
}

// Pull the triangle soup out of a single Poly_Triangulation (the readers return one).
namespace {
// The facets of one face triangulation that STL serializes, in the triangulation's
// own winding (each caller applies its face occurrence's orientation). A pole has
// distinct UV nodes at one exact 3D point. Omitted facets are identified by native
// topology, never by proximity, and have zero oriented boundary and no net surface,
// so omitting them welds and heals nothing:
//  * Pole facet: two corners are nodes on the SAME native degenerate edge, so its
//    oriented boundary is A->B + B->B + B->A = 0.
//  * Folded pole fin pair: BRepMesh can link BOTH UV ends of a pole to one interior
//    UV edge X-Y (a node column inserted toward the pole), giving (P1,X,Y) and
//    (P2,Y,X) with P1, P2 on the same degenerate edge at one exact point. In 3D that
//    is one facet plus its reverse, a double-covered spike: the pair sums to zero.
// Collinear facets, short edges, and float32 collapse are NOT filtered.
std::vector<Poly_Triangle> stl_face_facets(const TopoDS_Face& face) {
    TopLoc_Location loc;
    const auto& tri = BRep_Tool::Triangulation(face, loc);
    if (tri.IsNull() || tri->NbTriangles() == 0)
        throw OpError("export_failed", "STL source face has no triangulation");
    // pole[d][node]: the node lies on the polygon of degenerate edge d.
    std::vector<std::vector<bool>> pole;
    for (TopExp_Explorer ex(face, TopAbs_EDGE); ex.More(); ex.Next()) {
        const TopoDS_Edge edge = TopoDS::Edge(ex.Current());
        if (!BRep_Tool::Degenerated(edge)) continue;
        const auto& polygon = BRep_Tool::PolygonOnTriangulation(edge, tri, loc);
        if (polygon.IsNull()) continue;
        auto& on = pole.emplace_back(static_cast<std::size_t>(tri->NbNodes()) + 1, false);
        for (int i = polygon->Nodes().Lower(); i <= polygon->Nodes().Upper(); ++i)
            on[polygon->Nodes()(i)] = true;
    }
    std::vector<Poly_Triangle> facets;
    facets.reserve(static_cast<std::size_t>(tri->NbTriangles()));
    for (int i = 1; i <= tri->NbTriangles(); ++i) {
        const Poly_Triangle facet = tri->Triangle(i);
        int n[3];
        facet.Get(n[0], n[1], n[2]);
        bool omit = false;
        for (int k = 0; k < 3 && !omit; ++k) {
            const int a = n[k], b = n[(k + 1) % 3];
            if (tri->Node(a).SquareDistance(tri->Node(b)) != 0.0) continue;
            for (const auto& on : pole) omit |= on[a] && on[b];
            if (!omit)
                throw OpError("export_failed",
                              "STL contains coincident facet corners outside a native pole");
        }
        if (!omit) facets.push_back(facet);
    }
    std::vector<bool> fin(facets.size(), false);
    for (const auto& on : pole) {
        // Facets with exactly one corner on this pole, keyed by their opposite UV
        // edge X-Y. A valid triangulation has at most two facets on one edge.
        std::map<std::pair<int, int>, std::vector<std::size_t>> by_edge;
        for (std::size_t f = 0; f < facets.size(); ++f) {
            int n[3];
            facets[f].Get(n[0], n[1], n[2]);
            if (on[n[0]] + on[n[1]] + on[n[2]] != 1) continue;
            const int p = on[n[0]] ? 0 : on[n[1]] ? 1 : 2;
            const int x = n[(p + 1) % 3], y = n[(p + 2) % 3];
            by_edge[{std::min(x, y), std::max(x, y)}].push_back(f);
        }
        for (const auto& [xy, ids] : by_edge) {
            if (ids.size() != 2) continue;
            int a[3], b[3];
            facets[ids[0]].Get(a[0], a[1], a[2]);
            facets[ids[1]].Get(b[0], b[1], b[2]);
            const int pa = on[a[0]] ? 0 : on[a[1]] ? 1 : 2;
            const int pb = on[b[0]] ? 0 : on[b[1]] ? 1 : 2;
            if (a[pa] != b[pb] && tri->Node(a[pa]).SquareDistance(tri->Node(b[pb])) == 0.0 &&
                a[(pa + 1) % 3] == b[(pb + 2) % 3] && a[(pa + 2) % 3] == b[(pb + 1) % 3])
                fin[ids[0]] = fin[ids[1]] = true;
        }
    }
    std::size_t kept = 0;
    for (std::size_t f = 0; f < facets.size(); ++f)
        if (!fin[f]) facets[kept++] = facets[f];
    facets.resize(kept);
    return facets;
}

std::array<double, 3> stl_position(const gp_Pnt& p, bool ascii) {
    std::array<double, 3> key{p.X(), p.Y(), p.Z()};
    for (double& v : key) {
        // Match RWStl's emitted representation, not a tolerance weld. ASCII uses
        // %e (six decimals); binary stores IEEE float32. Never move source nodes.
        if (ascii) {
            char text[64];
            std::snprintf(text, sizeof(text), "%.6e", v);
            v = std::strtod(text, nullptr);
        } else {
            v = static_cast<float>(v);
        }
        if (!std::isfinite(v))
            throw OpError("export_failed", "STL coordinate is not finite in the output representation");
    }
    return key;
}

// Validate each source solid independently, so an assembly can still contain
// separate touching solids. No repair or tolerance-welding is performed: every
// serialized edge must appear twice, once in each direction. `facets[i - 1]` are
// the serialized facets of `faces(i)`, exactly what stl_triangulation writes.
void validate_stl_solid(const TopoDS_Shape& solid, const TopTools_IndexedMapOfShape& faces,
                        const std::vector<std::vector<Poly_Triangle>>& facets, bool ascii) {
    std::map<std::array<double, 3>, std::uint32_t> positions;
    struct Incidence { int count = 0, balance = 0; };
    std::unordered_map<std::uint64_t, Incidence> edges;
    for (TopExp_Explorer ex(solid, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        const auto& tri = BRep_Tool::Triangulation(face, loc);
        std::vector<std::uint32_t> ids(static_cast<std::size_t>(tri->NbNodes()) + 1);
        for (int i = 1; i <= tri->NbNodes(); ++i) {
            const auto key = stl_position(tri->Node(i).Transformed(loc.Transformation()), ascii);
            const auto inserted = positions.emplace(key, static_cast<std::uint32_t>(positions.size()));
            ids[i] = inserted.first->second;
        }
        for (const auto& facet : facets[static_cast<std::size_t>(faces.FindIndex(face)) - 1]) {
            int n[3];
            facet.Get(n[0], n[1], n[2]);
            if (face.Orientation() == TopAbs_REVERSED) std::swap(n[1], n[2]);
            for (int k = 0; k < 3; ++k) {
                const auto a = ids[n[k]], b = ids[n[(k + 1) % 3]];
                const auto key = (static_cast<std::uint64_t>(std::min(a, b)) << 32) | std::max(a, b);
                auto& use = edges[key];
                ++use.count;
                use.balance += a < b ? 1 : -1;
            }
        }
    }
    if (edges.empty())
        throw OpError("export_failed", "STL source solid has no facets");
    for (const auto& edge : edges)
        if (edge.second.count != 2 || edge.second.balance != 0)
            throw OpError("export_failed",
                          "STL tessellation is not a closed consistently oriented source solid");
}

Handle(Poly_Triangulation) stl_triangulation(const TopoDS_Shape& shape, bool ascii) {
    TopTools_IndexedMapOfShape faces;
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    std::vector<std::vector<Poly_Triangle>> facets(static_cast<std::size_t>(faces.Extent()));
    for (int i = 1; i <= faces.Extent(); ++i)
        facets[static_cast<std::size_t>(i) - 1] = stl_face_facets(TopoDS::Face(faces(i)));
    int nodes = 0, count = 0;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        const auto& tri = BRep_Tool::Triangulation(face, loc);
        const auto& kept = facets[static_cast<std::size_t>(faces.FindIndex(face)) - 1];
        if (nodes > std::numeric_limits<int>::max() - tri->NbNodes() ||
            kept.size() > static_cast<std::size_t>(std::numeric_limits<int>::max() - count))
            throw OpError("export_failed", "STL triangulation exceeds native index limits");
        nodes += tri->NbNodes();
        count += static_cast<int>(kept.size());
    }
    if (count == 0) throw OpError("export_failed", "STL has no facets to write");
    Handle(Poly_Triangulation) mesh = new Poly_Triangulation(nodes, count, Standard_False);
    int offset = 0, written = 0;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        const auto& tri = BRep_Tool::Triangulation(face, loc);
        for (int i = 1; i <= tri->NbNodes(); ++i)
            mesh->SetNode(offset + i, tri->Node(i).Transformed(loc.Transformation()));
        for (const auto& facet : facets[static_cast<std::size_t>(faces.FindIndex(face)) - 1]) {
            int n[3];
            facet.Get(n[0], n[1], n[2]);
            if (face.Orientation() == TopAbs_REVERSED) std::swap(n[1], n[2]);
            gp_Pnt p[3];
            for (int k = 0; k < 3; ++k) {
                n[k] += offset;
                const auto key = stl_position(mesh->Node(n[k]), ascii);
                p[k].SetCoord(key[0], key[1], key[2]);
            }
            if (gp_Vec(p[0], p[1]).Crossed(gp_Vec(p[0], p[2])).SquareMagnitude() == 0.0)
                throw OpError("export_failed", "STL facet collapses in the output representation");
            mesh->SetTriangle(++written, Poly_Triangle(n[0], n[1], n[2]));
        }
        offset += tri->NbNodes();
    }
    for (TopExp_Explorer ex(shape, TopAbs_SOLID); ex.More(); ex.Next())
        validate_stl_solid(ex.Current(), faces, facets, ascii);
    return mesh;
}

MeshData triangulation_to_mesh(const Handle(Poly_Triangulation)& tri) {
    MeshData mesh;
    if (tri.IsNull() || tri->NbNodes() == 0 || tri->NbTriangles() == 0) return mesh;
    mesh.verts.reserve(tri->NbNodes() * 3);
    mesh.tris.reserve(tri->NbTriangles() * 3);
    for (int ni = 1; ni <= tri->NbNodes(); ++ni) {
        gp_Pnt p = tri->Node(ni);
        mesh.verts.push_back(p.X());
        mesh.verts.push_back(p.Y());
        mesh.verts.push_back(p.Z());
    }
    for (int ti = 1; ti <= tri->NbTriangles(); ++ti) {
        Standard_Integer n1, n2, n3;
        tri->Triangle(ti).Get(n1, n2, n3);
        mesh.tris.push_back(static_cast<uint32_t>(n1 - 1));
        mesh.tris.push_back(static_cast<uint32_t>(n2 - 1));
        mesh.tris.push_back(static_cast<uint32_t>(n3 - 1));
    }
    return mesh;
}

// Concatenate every face-triangulation of a shape (glTF reader returns a B-rep shape
// whose faces carry triangulations) into one mesh soup.
MeshData shape_triangulations_to_mesh(const TopoDS_Shape& shape) {
    MeshData mesh;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        if (tri.IsNull() || tri->NbTriangles() == 0) continue;
        const gp_Trsf trsf = loc.Transformation();
        const uint32_t base = static_cast<uint32_t>(mesh.vertex_count());
        for (int ni = 1; ni <= tri->NbNodes(); ++ni) {
            gp_Pnt p = tri->Node(ni).Transformed(trsf);
            mesh.verts.push_back(p.X());
            mesh.verts.push_back(p.Y());
            mesh.verts.push_back(p.Z());
        }
        for (int ti = 1; ti <= tri->NbTriangles(); ++ti) {
            Standard_Integer n1, n2, n3;
            tri->Triangle(ti).Get(n1, n2, n3);
            mesh.tris.push_back(base + static_cast<uint32_t>(n1 - 1));
            mesh.tris.push_back(base + static_cast<uint32_t>(n2 - 1));
            mesh.tris.push_back(base + static_cast<uint32_t>(n3 - 1));
        }
    }
    return mesh;
}

// ---- hand-written deterministic ASCII OBJ / PLY writers ---------------------
// OCCT 7.9.3 has no non-XCAF OBJ/PLY *writer* (only the CAF writers, which would drag
// the assembly stack). A flat triangle soup is trivial + byte-deterministic to emit, so
// we write OBJ/PLY ourselves; trimesh (the oracle) reads both robustly for the gate.

void write_obj(const MeshData& mesh, const std::string& path) {
    std::ofstream os(path, std::ios::out | std::ios::trunc);
    if (!os) throw OpError("export_failed", "OBJ: cannot open output", {{"path", path}});
    os << "# chudware OBJ export\n";
    os.setf(std::ios::fixed);
    os.precision(6);
    const std::size_t nv = mesh.vertex_count();
    for (std::size_t i = 0; i < nv; ++i)
        os << "v " << mesh.verts[3 * i] << ' ' << mesh.verts[3 * i + 1] << ' '
           << mesh.verts[3 * i + 2] << '\n';
    const std::size_t nt = mesh.triangle_count();
    for (std::size_t i = 0; i < nt; ++i)  // OBJ is 1-based
        os << "f " << (mesh.tris[3 * i] + 1) << ' ' << (mesh.tris[3 * i + 1] + 1) << ' '
           << (mesh.tris[3 * i + 2] + 1) << '\n';
    if (!os) throw OpError("export_failed", "OBJ: write error", {{"path", path}});
}

void write_ply(const MeshData& mesh, const std::string& path) {
    std::ofstream os(path, std::ios::out | std::ios::trunc);
    if (!os) throw OpError("export_failed", "PLY: cannot open output", {{"path", path}});
    const std::size_t nv = mesh.vertex_count();
    const std::size_t nt = mesh.triangle_count();
    os << "ply\nformat ascii 1.0\ncomment chudware PLY export\n";
    os << "element vertex " << nv << "\n";
    os << "property float x\nproperty float y\nproperty float z\n";
    os << "element face " << nt << "\n";
    os << "property list uchar int vertex_indices\n";
    os << "end_header\n";
    os.setf(std::ios::fixed);
    os.precision(6);
    for (std::size_t i = 0; i < nv; ++i)
        os << mesh.verts[3 * i] << ' ' << mesh.verts[3 * i + 1] << ' '
           << mesh.verts[3 * i + 2] << '\n';
    for (std::size_t i = 0; i < nt; ++i)
        os << "3 " << mesh.tris[3 * i] << ' ' << mesh.tris[3 * i + 1] << ' '
           << mesh.tris[3 * i + 2] << '\n';
    if (!os) throw OpError("export_failed", "PLY: write error", {{"path", path}});
}

#include "doc/ply_read.inc"

// ---- glTF / GLB via OCCT's XCAF-backed reader/writer ------------------------
Handle(TDocStd_Document) new_xcaf_doc() {
    Handle(XCAFApp_Application) app = XCAFApp_Application::GetApplication();
    Handle(TDocStd_Document) doc;
    app->NewDocument("BinXCAF", doc);
    return doc;
}

void write_gltf(const std::vector<TopoDS_Shape>& bodies, const std::string& path, bool binary) {
    Handle(TDocStd_Document) doc = new_xcaf_doc();
    Handle(XCAFDoc_ShapeTool) tool = XCAFDoc_DocumentTool::ShapeTool(doc->Main());
    for (const TopoDS_Shape& s : bodies)
        tool->AddShape(s, /*makeAssembly=*/Standard_False);  // premeshed by export_bodies
    TColStd_IndexedDataMapOfStringString file_info;
    RWGltf_CafWriter writer(path.c_str(), binary);
    // OCCT's converter describes source/destination units as metres per unit.
    // Document coordinates are mm; glTF coordinates are necessarily metres.
    writer.ChangeCoordinateSystemConverter().SetInputLengthUnit(0.001);
    writer.ChangeCoordinateSystemConverter().SetOutputLengthUnit(1.0);
    if (!writer.Perform(doc, file_info, Message_ProgressRange()))
        throw OpError("export_failed", "glTF writer returned failure", {{"path", path}});
}

MeshData read_gltf(const std::string& path) {
    RWGltf_CafReader reader;
    Handle(TDocStd_Document) doc = new_xcaf_doc();
    reader.SetDocument(doc);
    reader.SetSystemLengthUnit(0.001);  // glTF metres -> document millimetres
    reader.SetParallel(Standard_False);
    if (!reader.Perform(path.c_str(), Message_ProgressRange()))
        throw OpError("import_failed", "glTF reader returned failure", {{"path", path}});
    TopoDS_Shape shape = reader.SingleShape();
    if (shape.IsNull())
        throw OpError("import_failed", "glTF reader produced no shape", {{"path", path}});
    return shape_triangulations_to_mesh(shape);
}

}  // namespace

// --------------------------------------------------------------------------- read_mesh

MeshData read_mesh(const std::string& path, const std::string& fmt) {
    if (fmt == "stl") {
        Handle(Poly_Triangulation) tri = RWStl::ReadFile(path.c_str());
        if (tri.IsNull())
            throw OpError("import_failed", "STL reader returned failure", {{"path", path}});
        return triangulation_to_mesh(tri);
    }
    if (fmt == "obj") {
        Handle(Poly_Triangulation) tri = RWObj::ReadFile(path.c_str());
        if (tri.IsNull())
            throw OpError("import_failed", "OBJ reader returned failure", {{"path", path}});
        return triangulation_to_mesh(tri);
    }
    if (fmt == "ply") return read_ply(path);
    if (fmt == "gltf" || fmt == "glb") return read_gltf(path);
    if (fmt == "3mf") {
#ifdef CHUDWARE_WITH_LIB3MF
        // Concatenate every 3MF mesh object into one soup (re-basing indices) for
        // the generic single-mesh read_mesh contract; import_bodies handles 3mf
        // per-object to match the oracle's one-MeshBody-per-object naming.
        std::vector<NamedMesh> objs = read_3mf_objects(path);
        MeshData mesh;
        for (const NamedMesh& nm : objs) {
            const uint32_t base = static_cast<uint32_t>(mesh.vertex_count());
            mesh.verts.insert(mesh.verts.end(), nm.mesh.verts.begin(), nm.mesh.verts.end());
            for (uint32_t i : nm.mesh.tris) mesh.tris.push_back(base + i);
        }
        return mesh;
#else
        throw OpError("import_failed",
                      "3MF import requires lib3mf — rebuild core with the SDK provisioned "
                      "(-DCHUDWARE_WITH_OCCT=ON pulls in ProvisionLib3mf).",
                      {{"format", fmt}, {"path", path}});
#endif
    }
    if (fmt == "3dm") {
#ifdef CHUDWARE_WITH_OPENNURBS
        // Concatenate every 3dm mesh object into one soup (re-basing indices) for
        // the generic single-mesh read_mesh contract; import_bodies handles 3dm
        // per-object to match the oracle's one-MeshBody-per-object naming.
        std::vector<OnNamedMesh> objs = read_3dm_objects(path);
        MeshData mesh;
        for (const OnNamedMesh& nm : objs) {
            const uint32_t base = static_cast<uint32_t>(mesh.vertex_count());
            mesh.verts.insert(mesh.verts.end(), nm.mesh.verts.begin(), nm.mesh.verts.end());
            for (uint32_t i : nm.mesh.tris) mesh.tris.push_back(base + i);
        }
        return mesh;
#else
        throw OpError("import_failed",
                      "3DM import requires openNURBS — rebuild core with the SDK provisioned "
                      "(-DCHUDWARE_WITH_OCCT=ON pulls in ProvisionOpenNURBS).",
                      {{"format", fmt}, {"path", path}});
#endif
    }
    throw OpError("import_failed", "read_mesh: unsupported mesh format '" + fmt + "'",
                  {{"format", fmt}, {"path", path}});
}

std::vector<std::pair<std::string, MeshData>> read_mesh_raw(const std::string& path,
                                                            const std::string& fmt) {
    std::vector<std::pair<std::string, MeshData>> out;
    if (fmt == "3mf") {
#ifdef CHUDWARE_WITH_LIB3MF
        for (NamedMesh& nm : read_3mf_objects(path))
            out.emplace_back(nm.name, std::move(nm.mesh));
        return out;
#else
        throw OpError("import_failed",
                      "3MF read_mesh_raw requires lib3mf — rebuild with the SDK provisioned.",
                      {{"format", fmt}, {"path", path}});
#endif
    }
    if (fmt == "3dm") {
#ifdef CHUDWARE_WITH_OPENNURBS
        for (OnNamedMesh& nm : read_3dm_objects(path))
            out.emplace_back(nm.name, std::move(nm.mesh));
        return out;
#else
        throw OpError("import_failed",
                      "3DM read_mesh_raw requires openNURBS — rebuild with the SDK provisioned.",
                      {{"format", fmt}, {"path", path}});
#endif
    }
    // Other mesh formats are single-soup: one entry named after the file stem.
    const std::string stem = std::filesystem::path(path).stem().string();
    out.emplace_back(stem, read_mesh(path, fmt));
    return out;
}

// --------------------------------------------------------------------------- mesh_to_shape

namespace {

// Positional weld key for a triangle soup: 1e-6 mm buckets (tighter than the 1e-6 sew
// tolerance is loose), so a vertex duplicated per strip/face still welds to one position.
struct MeshWeldKey {
    std::int64_t x, y, z;
    bool operator==(const MeshWeldKey& o) const { return x == o.x && y == o.y && z == o.z; }
};
struct MeshWeldKeyHash {
    std::size_t operator()(const MeshWeldKey& k) const {
        std::size_t h = std::hash<std::int64_t>{}(k.x);
        h ^= std::hash<std::int64_t>{}(k.y) + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
        h ^= std::hash<std::int64_t>{}(k.z) + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
        return h;
    }
};
MeshWeldKey mesh_weld_key(double x, double y, double z) {
    const double q = 1e6;  // 1e-6 mm buckets
    return MeshWeldKey{static_cast<std::int64_t>(std::llround(x * q)),
                       static_cast<std::int64_t>(std::llround(y * q)),
                       static_cast<std::int64_t>(std::llround(z * q))};
}

// Smooth per-corner shading normals for a triangle soup, split across creases.
//
// The mesh lane's faces are planar facets (one per triangle, or one per coplanar triangle
// set), so the display tessellator (core/geom/tessellate.cpp) would otherwise compute one
// flat normal per face and a curved import renders as discrete facets. Authored per-vertex
// normals are NOT used to fix that: the SolidWorks saved-display normal arrays measured
// WRONG on real parts (a turned spacer carried nothing but (0,0,±1) on all 2208 vertices; on
// another part the authored sign disagreed with the triangle winding for 1040 of 1176
// triangles), so they are no longer parsed at all. These normals come from the geometry
// instead.
//
// Math. n_t = (p1-p0) x (p2-p0) is twice the triangle's area times its unit normal, so
// summing raw cross products IS an area-weighted average. For corner k of triangle t the
// sum runs over the triangles incident to that corner's WELDED position whose unit normal
// lies within the crease angle (40 deg, i.e. dot >= cos 40 deg) of t's own unit normal —
// a vertex on a hard edge therefore gets a different average per side (nothing smooths
// across the crease) while a tessellated cylinder, fillet or tread band gets one
// continuous normal field. Degenerate triangles have a zero normal, so they neither
// contribute to nor receive a usable average.
//
// Normals follow the soup's OWN winding. The consumer flips a REVERSED face's normals and
// its index order together, so the downstream orientation fix keeps the two consistent.
struct MeshSmoothNormals {
    std::unordered_map<MeshWeldKey, std::uint32_t, MeshWeldKeyHash> pos_id;  // weld key -> id
    std::vector<std::uint32_t> vert_pos;  // per soup vertex: welded position id
    std::vector<std::uint32_t> at_off;    // CSR offsets over positions, size npos+1
    std::vector<std::uint32_t> at_tri;    // CSR payload: triangles incident to each position
    std::vector<float> corner;            // 9 floats per triangle (3 corners x xyz)
};

MeshSmoothNormals compute_mesh_smooth_normals(const MeshData& mesh) {
    MeshSmoothNormals sn;
    const std::size_t nv = mesh.vertex_count();
    const std::size_t nt = mesh.triangle_count();
    if (nt == 0) return sn;

    sn.vert_pos.resize(nv);
    sn.pos_id.reserve(nv * 2);
    std::uint32_t npos = 0;
    for (std::size_t v = 0; v < nv; ++v) {
        const auto ins = sn.pos_id.emplace(
            mesh_weld_key(mesh.verts[3 * v], mesh.verts[3 * v + 1], mesh.verts[3 * v + 2]), npos);
        if (ins.second) ++npos;
        sn.vert_pos[v] = ins.first->second;
    }

    // CSR adjacency: which triangles touch each welded position.
    sn.at_off.assign(static_cast<std::size_t>(npos) + 1, 0u);
    for (std::size_t t = 0; t < nt; ++t)
        for (int k = 0; k < 3; ++k) ++sn.at_off[sn.vert_pos[mesh.tris[3 * t + k]] + 1];
    for (std::uint32_t p = 0; p < npos; ++p) sn.at_off[p + 1] += sn.at_off[p];
    sn.at_tri.assign(sn.at_off.back(), 0u);
    {
        std::vector<std::uint32_t> cursor(sn.at_off.begin(), sn.at_off.end() - 1);
        for (std::size_t t = 0; t < nt; ++t)
            for (int k = 0; k < 3; ++k)
                sn.at_tri[cursor[sn.vert_pos[mesh.tris[3 * t + k]]]++] =
                    static_cast<std::uint32_t>(t);
    }

    // Area-weighted (raw cross product) and unit triangle normals.
    std::vector<double> raw(nt * 3, 0.0), unit(nt * 3, 0.0);
    for (std::size_t t = 0; t < nt; ++t) {
        const std::uint32_t a = mesh.tris[3 * t], b = mesh.tris[3 * t + 1],
                            c = mesh.tris[3 * t + 2];
        const double ux = mesh.verts[3 * b] - mesh.verts[3 * a];
        const double uy = mesh.verts[3 * b + 1] - mesh.verts[3 * a + 1];
        const double uz = mesh.verts[3 * b + 2] - mesh.verts[3 * a + 2];
        const double vx = mesh.verts[3 * c] - mesh.verts[3 * a];
        const double vy = mesh.verts[3 * c + 1] - mesh.verts[3 * a + 1];
        const double vz = mesh.verts[3 * c + 2] - mesh.verts[3 * a + 2];
        const double nx = uy * vz - uz * vy;
        const double ny = uz * vx - ux * vz;
        const double nz = ux * vy - uy * vx;
        raw[3 * t] = nx; raw[3 * t + 1] = ny; raw[3 * t + 2] = nz;
        const double len = std::sqrt(nx * nx + ny * ny + nz * nz);
        if (len > 1e-30) {
            unit[3 * t] = nx / len; unit[3 * t + 1] = ny / len; unit[3 * t + 2] = nz / len;
        }
    }

    const double kCosCrease = std::cos(40.0 * 3.14159265358979323846 / 180.0);
    sn.corner.assign(nt * 9, 0.0f);
    for (std::size_t t = 0; t < nt; ++t) {
        const double rx = unit[3 * t], ry = unit[3 * t + 1], rz = unit[3 * t + 2];
        if (rx == 0.0 && ry == 0.0 && rz == 0.0) continue;  // degenerate triangle
        for (int k = 0; k < 3; ++k) {
            double ax = 0.0, ay = 0.0, az = 0.0;
            const std::uint32_t p = sn.vert_pos[mesh.tris[3 * t + k]];
            for (std::uint32_t i = sn.at_off[p]; i < sn.at_off[p + 1]; ++i) {
                const std::uint32_t j = sn.at_tri[i];
                if (unit[3 * j] * rx + unit[3 * j + 1] * ry + unit[3 * j + 2] * rz < kCosCrease)
                    continue;  // across a crease (or degenerate) — do not smooth into it
                ax += raw[3 * j]; ay += raw[3 * j + 1]; az += raw[3 * j + 2];
            }
            double len = std::sqrt(ax * ax + ay * ay + az * az);
            if (!(len > 1e-30)) { ax = rx; ay = ry; az = rz; len = 1.0; }  // fall back to flat
            sn.corner[9 * t + 3 * k] = static_cast<float>(ax / len);
            sn.corner[9 * t + 3 * k + 1] = static_cast<float>(ay / len);
            sn.corner[9 * t + 3 * k + 2] = static_cast<float>(az / len);
        }
    }
    return sn;
}

// Attach the computed smooth normals to a just-sewn triangle-soup shape.
//
// Runs AFTER sewing on purpose: BRepBuilderAPI_Sewing rebuilds faces, so a triangulation
// attached before it would be dropped. Each sewn face IS one source triangle, so it is
// identified by its three WELDED vertex positions — the unique soup triangle incident to
// all three — and each face vertex then takes that triangle's per-corner normal. The
// triangulation is written in the face's OWN frame (the location a sewn face may carry is
// removed first) so BRep_Tool::Triangulation(face, loc) returns nodes consistent with `loc`.
void attach_mesh_smooth_normals(const TopoDS_Shape& shape, const MeshData& mesh,
                                const MeshSmoothNormals& sn) {
    if (shape.IsNull() || sn.corner.empty()) return;
    BRep_Builder builder;
    for (TopExp_Explorer fx(shape, TopAbs_FACE); fx.More(); fx.Next()) {
        const TopoDS_Face face = TopoDS::Face(fx.Current());
        gp_Pnt pts[3];
        std::uint32_t fpos[3] = {0, 0, 0};
        int n = 0;
        bool usable = true;
        for (TopExp_Explorer vx(face, TopAbs_VERTEX); vx.More() && usable; vx.Next()) {
            const gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(vx.Current()));
            bool seen = false;  // a triangle's wire visits each vertex twice (two edges)
            for (int i = 0; i < n; ++i)
                if (pts[i].SquareDistance(p) < 1e-18) seen = true;
            if (seen) continue;
            if (n == 3) { usable = false; break; }  // not a plain triangle — leave it alone
            const auto it = sn.pos_id.find(mesh_weld_key(p.X(), p.Y(), p.Z()));
            if (it == sn.pos_id.end()) { usable = false; break; }
            pts[n] = p;
            fpos[n] = it->second;
            ++n;
        }
        if (!usable || n != 3) continue;
        if (fpos[0] == fpos[1] || fpos[1] == fpos[2] || fpos[0] == fpos[2]) continue;

        std::size_t tri_idx = 0;
        bool found = false;
        for (std::uint32_t i = sn.at_off[fpos[0]]; i < sn.at_off[fpos[0] + 1] && !found; ++i) {
            const std::uint32_t t = sn.at_tri[i];
            const std::uint32_t q0 = sn.vert_pos[mesh.tris[3 * t]],
                                q1 = sn.vert_pos[mesh.tris[3 * t + 1]],
                                q2 = sn.vert_pos[mesh.tris[3 * t + 2]];
            bool all = true;
            for (int k = 0; k < 3; ++k)
                if (fpos[k] != q0 && fpos[k] != q1 && fpos[k] != q2) all = false;
            if (all) { tri_idx = t; found = true; }
        }
        if (!found) continue;
        const std::uint32_t tq[3] = {sn.vert_pos[mesh.tris[3 * tri_idx]],
                                     sn.vert_pos[mesh.tris[3 * tri_idx + 1]],
                                     sn.vert_pos[mesh.tris[3 * tri_idx + 2]]};

        // Nodes are emitted in the SOURCE TRIANGLE's corner order, NOT in the order the
        // vertex explorer happens to walk the wire. That ordering IS the display winding:
        // Poly_Triangle(1,2,3) over arbitrary nodes gave every face a coin-flip winding, so
        // the AICM mesh was self-inconsistent (286 of 736 triangles wound against their own
        // stored normal on a real SolidWorks spacer) and its signed volume came out
        // -375019 mm³ against the B-rep's +481597 mm³. Composed with the face's orientation
        // by core/geom/tessellate.cpp, the soup winding is the coherent one the sewer chose.
        gp_Pnt node[3];
        gp_Dir nrm[3];
        bool complete = true;
        for (int k = 0; k < 3 && complete; ++k) {
            complete = false;
            const float* c = &sn.corner[9 * tri_idx + 3 * k];
            const double sq = (double)c[0] * c[0] + (double)c[1] * c[1] + (double)c[2] * c[2];
            if (sq < 1e-12) break;  // no usable normal — let the tessellator compute one
            for (int i = 0; i < 3; ++i) {
                if (fpos[i] != tq[k]) continue;
                node[k] = pts[i];
                nrm[k] = gp_Dir(c[0], c[1], c[2]);
                complete = true;
                break;
            }
        }
        if (!complete) continue;

        Handle(Poly_Triangulation) tri =
            new Poly_Triangulation(3, 1, /*hasUV=*/Standard_False, /*hasNormals=*/Standard_True);
        const bool located = !face.Location().IsIdentity();
        const gp_Trsf inv = face.Location().Transformation().Inverted();
        for (int k = 0; k < 3; ++k) {
            gp_Pnt p = node[k];
            gp_Dir d = nrm[k];
            if (located) {
                p.Transform(inv);
                d.Transform(inv);
            }
            tri->SetNode(k + 1, p);
            tri->SetNormal(k + 1, d);
        }
        tri->SetTriangle(1, Poly_Triangle(1, 2, 3));
        builder.UpdateFace(face, tri);
    }
}

// Closed shell -> solid, mirroring the historical ShapeFix_Solid preference. Null on failure.
TopoDS_Shape solid_from_closed_shell(const TopoDS_Shell& shell) {
    try {
        BRepBuilderAPI_MakeSolid ms(shell);
        if (!ms.IsDone()) return TopoDS_Shape();
        ShapeFix_Solid fix;
        const TopoDS_Shape solid = fix.SolidFromShell(shell);
        return solid.IsNull() ? TopoDS_Shape(ms.Solid()) : solid;
    } catch (const Standard_Failure&) {
        return TopoDS_Shape();
    }
}

double shape_signed_volume(const TopoDS_Shape& s) {
    try {
        GProp_GProps props;
        BRepGProp::VolumeProperties(s, props);
        return props.Mass();
    } catch (const Standard_Failure&) {
        return 0.0;
    }
}

// Closed-mesh lane: a welded, closed, consistently wound, edge-connected triangle mesh is
// built into B-rep topology DIRECTLY, with adjacent coplanar triangles merged into one planar
// face. Two reasons it does not take the per-triangle + sewing path below:
//   * Sewing rediscovers adjacency the mesh already states. The indices name every shared
//     vertex and edge, so the shell is assembled from ONE shared TopoDS_Edge per mesh edge.
//   * One face per triangle splits every flat region of the part into slivers. A topology-
//     optimization surface (c247 Optimized_Bending_Beam) came in as 26,496 triangle faces:
//     its 960 mm^2 clamped and loaded end faces were 264 triangles each, so a "-x" face pick
//     resolved to one 3.6 mm^2 sliver, and the baked import blob, the AICM owner map and every
//     boolean carried the same 26k faces. A planar face per coplanar region keeps the exact
//     surface (vertices within `tol` of the region plane) and leaves curved regions faceted.
// Returns a null shape when the mesh is not a single closed oriented 2-manifold, or when a
// coplanar region cannot be bounded unambiguously and splitting it fails too, so the caller
// keeps the general sewing lane for open, cracked, multi-shell or inconsistent soups.
TopoDS_Shape closed_mesh_solid(const MeshData& mesh, const MeshSmoothNormals& sn) {
    const std::size_t nt = mesh.triangle_count();
    const std::size_t npos = sn.at_off.empty() ? 0 : sn.at_off.size() - 1;
    if (nt < 4 || npos < 4) return TopoDS_Shape();

    // Welded positions (first soup vertex of each weld bucket).
    std::vector<gp_Pnt> pos(npos);
    std::vector<char> pos_set(npos, 0);
    for (std::size_t v = 0; v < sn.vert_pos.size(); ++v) {
        const std::uint32_t p = sn.vert_pos[v];
        if (pos_set[p]) continue;
        pos[p] = gp_Pnt(mesh.verts[3 * v], mesh.verts[3 * v + 1], mesh.verts[3 * v + 2]);
        pos_set[p] = 1;
    }
    Bnd_Box box;
    for (const gp_Pnt& p : pos) box.Add(p);
    // Coplanarity tolerance: the relative precision mesh formats store (STL/3MF/OBJ hold
    // float32 positions, ~6e-8 relative) with headroom, scaled by the part size.
    const double tol = std::max(1e-7, 1e-6 * std::sqrt(box.SquareExtent()));

    // Welded triangles, unit normals, areas. A degenerate triangle leaves a hole in the
    // closed mesh, so it sends the mesh to the sewing lane.
    std::vector<std::array<std::uint32_t, 3>> tri(nt);
    std::vector<gp_Dir> nrm(nt);
    std::vector<double> area(nt);
    for (std::size_t t = 0; t < nt; ++t) {
        for (int k = 0; k < 3; ++k) tri[t][k] = sn.vert_pos[mesh.tris[3 * t + k]];
        const gp_Vec c = gp_Vec(pos[tri[t][0]], pos[tri[t][1]]).Crossed(
            gp_Vec(pos[tri[t][0]], pos[tri[t][2]]));
        if (tri[t][0] == tri[t][1] || tri[t][1] == tri[t][2] || tri[t][0] == tri[t][2] ||
            c.Magnitude() < 1e-12)
            return TopoDS_Shape();
        nrm[t] = gp_Dir(c);
        area[t] = 0.5 * c.Magnitude();
    }

    // Edge table: each undirected edge used by exactly two triangles, once in each direction.
    struct MeshEdge {
        std::int64_t tri[2] = {-1, -1};
        TopoDS_Edge shape;  // built lazily, only for edges that bound a face
    };
    auto edge_key = [](std::uint32_t a, std::uint32_t b) {
        return (static_cast<std::uint64_t>(std::min(a, b)) << 32) | std::max(a, b);
    };
    std::unordered_map<std::uint64_t, MeshEdge> edges;
    edges.reserve(nt * 2);
    for (std::size_t t = 0; t < nt; ++t)
        for (int k = 0; k < 3; ++k) {
            const std::uint32_t a = tri[t][k], b = tri[t][(k + 1) % 3];
            MeshEdge& e = edges[edge_key(a, b)];
            const int slot = a < b ? 0 : 1;  // slot 0: traversed low->high
            if (e.tri[slot] >= 0) return TopoDS_Shape();  // non-manifold or mis-wound
            e.tri[slot] = static_cast<std::int64_t>(t);
        }
    for (const auto& kv : edges)
        if (kv.second.tri[0] < 0 || kv.second.tri[1] < 0) return TopoDS_Shape();  // open
    auto neighbour = [&](std::size_t t, int k) {
        const MeshEdge& e = edges.at(edge_key(tri[t][k], tri[t][(k + 1) % 3]));
        return static_cast<std::size_t>(e.tri[0] == static_cast<std::int64_t>(t) ? e.tri[1]
                                                                                  : e.tri[0]);
    };
    {  // one edge-connected shell; several shells keep the sewing lane's handling
        std::vector<char> seen(nt, 0);
        std::vector<std::size_t> stack{0};
        seen[0] = 1;
        std::size_t reached = 1;
        while (!stack.empty()) {
            const std::size_t t = stack.back();
            stack.pop_back();
            for (int k = 0; k < 3; ++k) {
                const std::size_t u = neighbour(t, k);
                if (!seen[u]) { seen[u] = 1; ++reached; stack.push_back(u); }
            }
        }
        if (reached != nt) return TopoDS_Shape();
    }

    // Coplanar regions, seeded largest triangle first so each region plane comes from its
    // best-conditioned facet; a neighbour joins when all three corners lie within `tol` of
    // the seed plane on the same side.
    std::vector<std::size_t> order(nt);
    for (std::size_t t = 0; t < nt; ++t) order[t] = t;
    std::stable_sort(order.begin(), order.end(),
                     [&](std::size_t a, std::size_t b) { return area[a] > area[b]; });
    std::vector<std::int64_t> region(nt, -1);
    std::vector<std::vector<std::size_t>> regions;
    std::vector<gp_Pln> planes;
    for (const std::size_t s : order) {
        if (region[s] >= 0) continue;
        const std::int64_t r = static_cast<std::int64_t>(regions.size());
        const gp_Pln pln(pos[tri[s][0]], nrm[s]);
        regions.emplace_back();
        planes.push_back(pln);
        std::vector<std::size_t> stack{s};
        region[s] = r;
        while (!stack.empty()) {
            const std::size_t t = stack.back();
            stack.pop_back();
            regions.back().push_back(t);
            for (int k = 0; k < 3; ++k) {
                const std::size_t u = neighbour(t, k);
                if (region[u] >= 0 || nrm[u].Dot(nrm[s]) <= 0.0) continue;
                bool flat = true;
                for (int c = 0; c < 3 && flat; ++c)
                    flat = pln.Distance(pos[tri[u][c]]) <= tol;
                if (!flat) continue;
                region[u] = r;
                stack.push_back(u);
            }
        }
    }

    BRep_Builder bb;
    std::vector<TopoDS_Vertex> vtx(npos);
    auto vertex = [&](std::uint32_t p) -> const TopoDS_Vertex& {
        if (vtx[p].IsNull()) bb.MakeVertex(vtx[p], pos[p], Precision::Confusion());
        return vtx[p];
    };
    // The face-bounding edge a->b, oriented along the traversal.
    auto oriented_edge = [&](std::uint32_t a, std::uint32_t b) {
        MeshEdge& e = edges.at(edge_key(a, b));
        if (e.shape.IsNull()) {
            const std::uint32_t lo = std::min(a, b), hi = std::max(a, b);
            e.shape = BRepBuilderAPI_MakeEdge(vertex(lo), vertex(hi));
        }
        return a < b ? e.shape : TopoDS::Edge(e.shape.Reversed());
    };
    // One planar face over `tris` (all on plane `pln`), bounded by its boundary loops; null
    // when the boundary pinches (a vertex starting two boundary edges) or has no single outer
    // loop, which the caller resolves by splitting the region into triangles.
    auto planar_face = [&](const std::vector<std::size_t>& tris, const gp_Pln& pln,
                           std::int64_t r) -> TopoDS_Face {
        std::unordered_map<std::uint32_t, std::uint32_t> next;  // boundary a -> b
        for (const std::size_t t : tris)
            for (int k = 0; k < 3; ++k) {
                if (region[neighbour(t, k)] == r) continue;
                if (!next.emplace(tri[t][k], tri[t][(k + 1) % 3]).second) return TopoDS_Face();
            }
        TopoDS_Face face;
        bb.MakeFace(face, new Geom_Plane(pln), Precision::Confusion());
        const gp_Dir axis = pln.Axis().Direction();
        int outer = 0;
        std::unordered_map<std::uint32_t, char> used;
        TopTools_ListOfShape face_edges;
        for (const auto& start : next) {
            if (used.count(start.first)) continue;
            TopoDS_Wire wire;
            bb.MakeWire(wire);
            gp_XYZ newell(0.0, 0.0, 0.0);
            std::uint32_t a = start.first;
            do {
                used.emplace(a, 1);
                const auto it = next.find(a);
                if (it == next.end()) return TopoDS_Face();
                const std::uint32_t b = it->second;
                const TopoDS_Edge e = oriented_edge(a, b);
                bb.Add(wire, e);
                face_edges.Append(e);
                newell += pos[a].XYZ().Crossed(pos[b].XYZ());
                a = b;
            } while (a != start.first);
            wire.Closed(Standard_True);
            if (newell.Dot(axis.XYZ()) > 0.0) ++outer;  // counter-clockwise: the outer loop
            bb.Add(face, wire);
        }
        if (outer != 1 || used.size() != next.size()) return TopoDS_Face();
        BRepLib::BuildPCurveForEdgesOnPlane(face_edges, face);
        // Region corners sit up to `tol` off the plane: widen their edges/vertices to match.
        double dev = 0.0;
        for (const std::size_t t : tris)
            for (int k = 0; k < 3; ++k) dev = std::max(dev, pln.Distance(pos[tri[t][k]]));
        if (dev > Precision::Confusion()) {
            for (TopTools_ListIteratorOfListOfShape it(face_edges); it.More(); it.Next()) {
                const TopoDS_Edge& e = TopoDS::Edge(it.Value());
                bb.UpdateEdge(e, 1.01 * dev);
                TopoDS_Vertex v0, v1;
                TopExp::Vertices(e, v0, v1);
                bb.UpdateVertex(v0, 1.01 * dev);
                bb.UpdateVertex(v1, 1.01 * dev);
            }
        }
        // Display triangulation: the region's own triangles with the soup's smooth corner
        // normals (core/geom/tessellate.cpp keeps a normal-bearing triangulation as is).
        std::unordered_map<std::uint32_t, int> node;
        for (const std::size_t t : tris)
            for (int k = 0; k < 3; ++k) node.emplace(tri[t][k], 0);
        Handle(Poly_Triangulation) pt = new Poly_Triangulation(
            static_cast<int>(node.size()), static_cast<int>(tris.size()), Standard_False,
            Standard_True);
        int n = 0;
        for (auto& kv : node) {
            kv.second = ++n;
            pt->SetNode(n, pos[kv.first]);
            pt->SetNormal(n, axis);
        }
        int i = 0;
        for (const std::size_t t : tris) {
            int id[3];
            for (int k = 0; k < 3; ++k) {
                id[k] = node.at(tri[t][k]);
                const float* c = &sn.corner[9 * t + 3 * k];
                const double sq = (double)c[0] * c[0] + (double)c[1] * c[1] + (double)c[2] * c[2];
                if (sq >= 1e-12) pt->SetNormal(id[k], gp_Dir(c[0], c[1], c[2]));
            }
            pt->SetTriangle(++i, Poly_Triangle(id[0], id[1], id[2]));
        }
        bb.UpdateFace(face, pt);
        return face;
    };

    TopoDS_Shell shell;
    bb.MakeShell(shell);
    for (std::size_t r = 0; r < regions.size(); ++r) {
        const std::int64_t rid = static_cast<std::int64_t>(r);
        TopoDS_Face face = planar_face(regions[r], planes[r], rid);
        if (!face.IsNull()) {
            bb.Add(shell, face);
            continue;
        }
        // Unbounded region: one face per triangle on its own plane.
        for (const std::size_t t : regions[r]) {
            const std::int64_t own = -2 - static_cast<std::int64_t>(t);
            region[t] = own;
            face = planar_face({t}, gp_Pln(pos[tri[t][0]], nrm[t]), own);
            if (face.IsNull()) return TopoDS_Shape();
            bb.Add(shell, face);
        }
    }
    shell.Closed(Standard_True);
    TopoDS_Solid solid;
    bb.MakeSolid(solid);
    bb.Add(solid, shell);
    if (shape_signed_volume(solid) > 0.0) return solid;
    // Inward-wound soup: the same faces explored complemented (see mesh_to_shape).
    TopoDS_Solid flipped;
    bb.MakeSolid(flipped);
    bb.Add(flipped, shell.Reversed());
    return shape_signed_volume(flipped) > 0.0 ? TopoDS_Shape(flipped) : TopoDS_Shape();
}

}  // namespace

TopoDS_Shape mesh_to_shape(const MeshData& mesh) {
    if (mesh.verts.size() % 3 != 0 || mesh.tris.size() % 3 != 0)
        throw OpError("import_failed", "Mesh has an incomplete vertex or triangle");
    if (!std::all_of(mesh.verts.begin(), mesh.verts.end(),
                     [](double v) { return std::isfinite(v); }))
        throw OpError("import_failed", "Mesh has non-finite vertex coordinates");
    const std::size_t vertex_count = mesh.vertex_count();
    for (uint32_t index : mesh.tris)
        if (index >= vertex_count)
            throw OpError("import_failed", "Mesh triangle index is outside the vertex array");
    if (mesh.triangle_count() == 0) return TopoDS_Shape();
    const MeshSmoothNormals sn = compute_mesh_smooth_normals(mesh);
    // A clean closed mesh takes the direct lane (closed_mesh_solid). Its result must pass
    // BRepCheck before it replaces the proven sewing lane, which still handles everything
    // else. Measured on the c247 topology beam (26,496 triangles): direct 0.2 s + 1.0 s
    // check -> 14,290 faces, against 8.1 s of sewing -> 26,496 faces, same volume.
    try {
        const TopoDS_Shape solid = closed_mesh_solid(mesh, sn);
        if (!solid.IsNull() && BRepCheck_Analyzer(solid).IsValid()) return solid;
    } catch (const Standard_Failure&) {
        // fall through to sewing
    }
    // Build one triangular FACE per triangle (degenerate ones dropped), sew them into a
    // shell, then promote to a SOLID when the shell is closed (the "as_solid" path). The
    // resulting B-rep carries a real volume/bbox so it flows through the import feature
    // exactly like a B-rep body.
    BRepBuilderAPI_Sewing sew(1e-6);
    const std::size_t nt = mesh.triangle_count();
    int added = 0;
    for (std::size_t i = 0; i < nt; ++i) {
        const uint32_t a = mesh.tris[3 * i], b = mesh.tris[3 * i + 1], c = mesh.tris[3 * i + 2];
        gp_Pnt p0(mesh.verts[3 * a], mesh.verts[3 * a + 1], mesh.verts[3 * a + 2]);
        gp_Pnt p1(mesh.verts[3 * b], mesh.verts[3 * b + 1], mesh.verts[3 * b + 2]);
        gp_Pnt p2(mesh.verts[3 * c], mesh.verts[3 * c + 1], mesh.verts[3 * c + 2]);
        // Skip degenerate triangles (zero-area) — they cannot form a face.
        gp_Vec n(p0, p1);
        n.Cross(gp_Vec(p0, p2));
        if (n.Magnitude() < 1e-12) continue;
        try {
            TopoDS_Vertex v0 = BRepBuilderAPI_MakeVertex(p0);
            TopoDS_Vertex v1 = BRepBuilderAPI_MakeVertex(p1);
            TopoDS_Vertex v2 = BRepBuilderAPI_MakeVertex(p2);
            TopoDS_Edge e0 = BRepBuilderAPI_MakeEdge(v0, v1);
            TopoDS_Edge e1 = BRepBuilderAPI_MakeEdge(v1, v2);
            TopoDS_Edge e2 = BRepBuilderAPI_MakeEdge(v2, v0);
            TopoDS_Wire w = BRepBuilderAPI_MakeWire(e0, e1, e2);
            TopoDS_Face f = BRepBuilderAPI_MakeFace(w, /*OnlyPlane=*/Standard_True);
            if (!f.IsNull()) { sew.Add(f); ++added; }
        } catch (const Standard_Failure&) {
            // skip the bad triangle
        }
    }
    if (added == 0) return TopoDS_Shape();
    sew.Perform();
    TopoDS_Shape sewn = sew.SewedShape();
    if (sewn.IsNull()) return TopoDS_Shape();
    // Promote a closed shell to a solid (volume becomes meaningful).
    TopoDS_Shape result = sewn;
    for (TopExp_Explorer ex(sewn, TopAbs_SHELL); ex.More(); ex.Next()) {
        TopoDS_Shell shell = TopoDS::Shell(ex.Current());
        if (!BRep_Tool::IsClosed(shell)) continue;
        const TopoDS_Shape solid = solid_from_closed_shell(shell);
        if (solid.IsNull()) break;  // keep the sewn shell
        result = solid;
        // A triangle soup carries NO guarantee that its winding is outward, and neither
        // sewing nor ShapeFix_Solid re-orients one: a SolidWorks saved-display import came
        // through INSIDE-OUT (signed volume -374963 mm³ on a 375017 mm³ spacer, so
        // eval_mass was ~28% wrong and the shading lit the far side). A closed solid must
        // enclose POSITIVE volume, so when it does not, rebuild from the reversed shell —
        // every face is then explored complemented, which flips winding and shading normals
        // together (core/geom/tessellate.cpp).
        if (shape_signed_volume(result) >= 0.0) break;
        const TopoDS_Shape flipped =
            solid_from_closed_shell(TopoDS::Shell(shell.Reversed()));
        if (!flipped.IsNull() && shape_signed_volume(flipped) > 0.0) result = flipped;
        break;  // single closed shell is the common case
    }
    // Smooth shading for every mesh import (SolidWorks saved-display, STL, OBJ, PLY, 3MF,
    // 3DM): one planar face per triangle otherwise renders as discrete facets. A 40 deg
    // crease keeps genuinely flat geometry flat, so a boxy STL is unchanged.
    attach_mesh_smooth_normals(result, mesh, sn);
    return result;
}

// --------------------------------------------------------------------------- export

namespace {

// ---- DXF / DWG / SAT part export -------------------------------------------------------
// Bodies become one DxfDocument shared by the DXF and DWG writers. Shell/solid geometry is
// written as exact ACIS (3DSOLID for solids, BODY for open shells) that AutoCAD opens as
// editable solids; curves that belong to no face — and every edge of a body without shells,
// e.g. an imported 2D profile — as LINE, ARC/CIRCLE (planes parallel to XY) or tessellated
// 3D line runs. Coordinates are written in `tess.units`.

// `body` (mm) uniformly scaled into $INSUNITS `units`.
TopoDS_Shape to_units(const TopoDS_Shape& body, int units) {
    const double mm = insunits_mm(units);
    if (mm == 1.0) return body;
    gp_Trsf scale;
    scale.SetScale(gp_Pnt(0, 0, 0), 1.0 / mm);
    return BRepBuilderAPI_Transform(body, scale, Standard_True).Shape();
}

// The solids and shells of `body` (everything an ACIS body can hold) as one compound, and
// whether it is closed (no shell outside a solid). With `free_faces`, faces in no shell (a 2D
// profile's regions) join as one-face sheets, so a document that already writes ACIS keeps
// them as faces: its re-import (formats.cpp read_dxf) reads plain curves beside 3D content as
// construction wires, never as regions.
TopoDS_Shape acis_part(const TopoDS_Shape& body, bool free_faces, bool* solid) {
    TopoDS_Compound comp;
    BRep_Builder bb;
    bb.MakeCompound(comp);
    bool any = false;
    *solid = true;
    for (TopExp_Explorer ex(body, TopAbs_SOLID); ex.More(); ex.Next()) {
        bb.Add(comp, ex.Current());
        any = true;
    }
    for (TopExp_Explorer ex(body, TopAbs_SHELL, TopAbs_SOLID); ex.More(); ex.Next()) {
        bb.Add(comp, ex.Current());
        any = true;
        *solid = false;
    }
    if (free_faces)
        for (TopExp_Explorer ex(body, TopAbs_FACE, TopAbs_SHELL); ex.More(); ex.Next()) {
            TopoDS_Shell sheet;
            bb.MakeShell(sheet);
            bb.Add(sheet, ex.Current());
            bb.Add(comp, sheet);
            any = true;
            *solid = false;
        }
    return any ? TopoDS_Shape(comp) : TopoDS_Shape();
}

// True when any body has shell/solid geometry, i.e. the export writes ACIS.
bool has_shells(const std::vector<TopoDS_Shape>& bodies) {
    for (const TopoDS_Shape& b : bodies)
        if (!b.IsNull() && TopExp_Explorer(b, TopAbs_SHELL).More()) return true;
    return false;
}

void edge_to_dxf(const TopoDS_Edge& edge, const std::string& layer, const TessOptions& tess,
                 DxfDocument& doc) {
    if (BRep_Tool::Degenerated(edge)) return;
    BRepAdaptor_Curve c(edge);
    const double u0 = c.FirstParameter(), u1 = c.LastParameter();
    auto xyz = [](const gp_Pnt& p) { return std::array<double, 3>{p.X(), p.Y(), p.Z()}; };
    if (c.GetType() == GeomAbs_Line) {
        const gp_Pnt a = c.Value(u0), b = c.Value(u1);
        if (a.Distance(b) > 1e-9) doc.lines3.push_back({layer, xyz(a), xyz(b)});
        return;
    }
    if (c.GetType() == GeomAbs_Circle) {
        const gp_Circ circ = c.Circle();
        const gp_Dir n = circ.Axis().Direction();
        if (std::abs(n.Z()) > 1.0 - 1e-9) {
            const gp_Pnt ctr = circ.Location();
            DxfArc a;
            a.layer = layer;
            a.center = {ctr.X(), ctr.Y()};
            a.z = ctr.Z();
            a.radius = circ.Radius();
            if (std::abs(u1 - u0 - 2.0 * kPi) < 1e-9) {
                a.circle = true;
            } else {
                const gp_Pnt p0 = c.Value(u0), p1 = c.Value(u1);
                const double d0 = std::atan2(p0.Y() - ctr.Y(), p0.X() - ctr.X()) * 180.0 / kPi;
                const double d1 = std::atan2(p1.Y() - ctr.Y(), p1.X() - ctr.X()) * 180.0 / kPi;
                // The parameter runs CCW about the circle axis: about -Z that is clockwise,
                // so the CCW arc DXF expects goes the other way round.
                a.start_deg = n.Z() > 0.0 ? d0 : d1;
                a.end_deg = n.Z() > 0.0 ? d1 : d0;
            }
            doc.arcs.push_back(a);
            return;
        }
        // Any other plane: the circle's axis is the entity extrusion, so the arc keeps its
        // exact geometry; centre and angles are in that extrusion's Arbitrary Axis OCS, where
        // the parameter runs CCW like DXF angles.
        const gp_Mat to_ocs = dxf::ocs_matrix(n).Transposed();
        const gp_XYZ ctr = to_ocs * circ.Location().XYZ();
        DxfArc a;
        a.layer = layer;
        a.center = {ctr.X(), ctr.Y()};
        a.z = ctr.Z();
        a.radius = circ.Radius();
        a.normal = {n.X(), n.Y(), n.Z()};
        if (std::abs(u1 - u0 - 2.0 * kPi) < 1e-9) {
            a.circle = true;
        } else {
            const gp_XYZ p0 = to_ocs * c.Value(u0).XYZ(), p1 = to_ocs * c.Value(u1).XYZ();
            a.start_deg = std::atan2(p0.Y() - ctr.Y(), p0.X() - ctr.X()) * 180.0 / kPi;
            a.end_deg = std::atan2(p1.Y() - ctr.Y(), p1.X() - ctr.X()) * 180.0 / kPi;
        }
        doc.arcs.push_back(a);
        return;
    }
    double deflection = tess.lin_deflection;
    if (tess.relative) deflection *= std::max(1e-6, GCPnts_AbscissaPoint::Length(c, u0, u1));
    GCPnts_TangentialDeflection pts(c, u0, u1, tess.ang_deflection, deflection);
    for (int k = 1; k < pts.NbPoints(); ++k) {
        const gp_Pnt a = pts.Value(k), b = pts.Value(k + 1);
        if (a.Distance(b) > 1e-9) doc.lines3.push_back({layer, xyz(a), xyz(b)});
    }
}

DxfDocument bodies_to_dxf_document(const std::vector<TopoDS_Shape>& bodies,
                                   const TessOptions& tess) {
    DxfDocument doc;
    doc.insunits = tess.units;
    // Chord tolerances are millimetres; the written curves are in `units`.
    TessOptions curve_tess = tess;
    if (!tess.relative) curve_tess.lin_deflection /= insunits_mm(tess.units);
    SatWriteOptions sat;
    sat.version = 400;  // the ACIS version AutoCAD embeds in R2000 DWG/DXF
    sat.mm_per_unit = insunits_mm(tess.units);
    std::size_t live = 0;
    for (const TopoDS_Shape& b : bodies) live += b.IsNull() ? 0 : 1;
    const bool doc_3d = has_shells(bodies);
    std::size_t index = 0;
    for (const TopoDS_Shape& source : bodies) {
        if (source.IsNull()) continue;
        const TopoDS_Shape body = to_units(source, tess.units);
        const std::string layer = live > 1 ? "BODY" + std::to_string(++index) : std::string("0");
        if (live > 1) doc.add_layer(layer, 7);
        bool solid = false;
        const TopoDS_Shape part = acis_part(body, doc_3d, &solid);
        if (!part.IsNull()) {
            try {
                doc.acis.push_back({layer, write_sat(part, sat), solid, sat.version});
            } catch (const std::exception& e) {
                throw OpError("export_failed",
                              "body " + std::to_string(index == 0 ? 1 : index) +
                                  " cannot be written as ACIS: " + e.what(),
                              {{"reason", "acis_write"}});
            }
        }
        // Every edge the ACIS body does not carry (free curves, loose 2D profile faces).
        TopTools_IndexedMapOfShape carried;
        if (!part.IsNull()) TopExp::MapShapes(part, TopAbs_EDGE, carried);
        TopTools_IndexedMapOfShape edges;
        TopExp::MapShapes(body, TopAbs_EDGE, edges);
        for (int i = 1; i <= edges.Extent(); ++i)
            if (!carried.Contains(edges(i)))
                edge_to_dxf(TopoDS::Edge(edges(i)), layer, curve_tess, doc);
    }
    return doc;
}

}  // namespace

std::string export_bodies(const std::vector<TopoDS_Shape>& bodies, const std::string& path,
                          const std::string& fmt, const TessOptions& tess) {
    if (bodies.empty())
        throw OpError("export_failed", "export: no bodies to write", {{"format", fmt}});
    const std::string out = abs_path(path);
    std::filesystem::path parent = std::filesystem::path(out).parent_path();
    if (!parent.empty()) {
        std::error_code ec;
        std::filesystem::create_directories(parent, ec);
    }
    const TopoDS_Shape shape = as_compound(bodies);

    try {
        if (fmt == "step") {
            STEPControl_Writer w;
            // Transfer one SOLID at a time, not the whole COMPOUND AsIs (Issue 3). A boolean
            // result body can be a compound of DISJOINT LUMPS (e.g. a steering body: knuckles +
            // tie rod + column + handlebar); transferring that compound AsIs can emit a single
            // malformed manifold_solid_brep that re-reads as one INVALID solid, so the export
            // self-validation rejects the file even though .brep/.step+ wrote it fine. Exploding
            // to individual solids writes each lump as its own valid STEP solid (an implicit
            // STEP assembly), and a re-read recovers them all. A body with NO solid (an open
            // sheet/shell surface body) is transferred as-is; mixed bodies retain
            // their uncovered surfaces and curves alongside the solids.
            std::vector<TopoDS_Shape> units;
            for (const TopoDS_Shape& b : bodies) {
                if (b.IsNull()) continue;
                for (TopoDS_Shape& unit : split_import_bodies(b))
                    units.push_back(std::move(unit));
            }
            if (units.empty()) units.push_back(shape);
            for (const TopoDS_Shape& u : units)
                if (w.Transfer(u, STEPControl_AsIs) != IFSelect_RetDone)
                    throw OpError("export_failed", "STEP writer: transfer failed", {{"format", fmt}});
            if (w.Write(out.c_str()) != IFSelect_RetDone)
                throw OpError("export_failed", "STEP writer: write failed", {{"path", out}});
            return out;
        }
        if (fmt == "iges") {
            // P2 fix — make IGES round-trip as a SOLID, not loose trimmed surfaces.
            //
            // The default IGESControl_Writer writes in FACE mode (write.iges.brep.mode=0):
            // each face becomes an independent trimmed-surface entity (IGES type 144). On
            // re-import those surfaces DON'T sew back into a closed solid, so a re-read came
            // back as NO body (one part) or an OPEN shell of volume 0 with free edges
            // (another) — while the writer still reported success. Two changes fix it:
            //   (1) Switch the writer to BRep mode so it emits manifold_solid_brep entities
            //       (IGES 186) that re-import AS solids. Init() registers the IGES static
            //       parameters first (otherwise SetIVal has nothing to set), then we flip
            //       write.iges.brep.mode=1 BEFORE constructing the writer (it snapshots the
            //       mode at construction).
            //   (2) Hand the writer a SEWN solid, not the bare compound: BRep mode only emits
            //       a real solid when it is given a TopoDS_Solid, and a boolean-result body
            //       can be a compound of loose faces. as_sewn_solid sews + promotes closed
            //       shells to solids so the writer always sees watertight geometry.
            IGESControl_Controller::Init();
            Interface_Static::SetIVal("write.iges.brep.mode", 1);
            const TopoDS_Shape iges_shape = as_sewn_solid(bodies);
            IGESControl_Writer w("MM", /*modecr=*/1);  // unit mm, BRep write mode
            w.AddShape(iges_shape);
            w.ComputeModel();
            if (!w.Write(out.c_str()))
                throw OpError("export_failed", "IGES writer returned failure", {{"path", out}});

            // Did the INPUT contain a closed solid? Only then do we DEMAND that a solid
            // round-trips — a legitimate open surface/sheet body has no solid to recover, so
            // writing its surfaces is the correct outcome (not an error). This keeps surface
            // export working while making the solid case honest.
            bool expected_solid = false;
            for (const TopoDS_Shape& b : bodies) {
                if (b.IsNull()) continue;
                if (TopExp_Explorer(b, TopAbs_SOLID).More()) { expected_solid = true; break; }
            }

            // HONEST round-trip validation (independent of the brep.mode success above). Even
            // in BRep mode a degenerate body could fail to recover, so we RE-READ the file we
            // just wrote and, when a solid was expected, require a topologically-valid closed
            // solid before returning. If it doesn't round-trip, throw export_failed here — the
            // caller (doc_export_file) runs this BEFORE it sets meta["validated"], so a throw
            // guarantees we never report a false validated:true for an IGES file that comes
            // back as no body / an open shell of volume 0.
            try {
                auto reread = import_bodies(out, "iges");
                if (reread.empty())
                    throw OpError("export_failed",
                                  "IGES export wrote " + out + " but re-reading it recovered no "
                                  "bodies — the file is incomplete or malformed and would not "
                                  "open elsewhere",
                                  {{"path", out}, {"format", fmt}});
                if (expected_solid) {
                    bool ok = false;
                    for (const auto& nb : reread)
                        if (iges_reread_has_closed_solid(nb.second)) { ok = true; break; }
                    if (!ok)
                        throw OpError("export_failed",
                                      "IGES export wrote " + out + " but re-reading it recovered no "
                                      "valid closed solid (the file would re-import as no body or "
                                      "an open shell of volume 0) — IGES is lossy for this "
                                      "geometry; use STEP for a guaranteed solid round-trip",
                                      {{"path", out}, {"format", fmt}});
                }
            } catch (const OpError&) {
                throw;
            } catch (const std::exception& e) {
                throw OpError("export_failed",
                              std::string("IGES export ") + out +
                                  " failed round-trip validation (not re-openable as a solid): " +
                                  e.what(),
                              {{"path", out}, {"format", fmt}});
            }
            return out;
        }
        if (fmt == "brep") {
            if (!BRepTools::Write(shape, out.c_str()))
                throw OpError("export_failed", "BREP writer returned failure", {{"path", out}});
            return out;
        }
        if (fmt == "stl") {
            mesh_shape(shape, tess);
            const auto mesh = stl_triangulation(shape, tess.ascii);
            const OSD_Path destination(out.c_str());
            if (!(tess.ascii ? RWStl::WriteAscii(mesh, destination)
                             : RWStl::WriteBinary(mesh, destination)))
                throw OpError("export_failed", "STL writer returned failure", {{"path", out}});
            return out;
        }
        if (fmt == "obj" || fmt == "ply") {
            MeshData mesh;
            for (const TopoDS_Shape& body : bodies) {
                MeshData part = shape_to_mesh(body, tess);
                if (part.triangle_count() == 0) continue;
                const uint32_t base = static_cast<uint32_t>(mesh.vertex_count());
                mesh.verts.insert(mesh.verts.end(), part.verts.begin(), part.verts.end());
                for (uint32_t index : part.tris) mesh.tris.push_back(base + index);
            }
            if (mesh.triangle_count() == 0)
                throw OpError("export_failed", "export: no triangles to write as " + fmt,
                              {{"path", out}, {"format", fmt}});
            if (fmt == "obj") write_obj(mesh, out);
            else write_ply(mesh, out);
            return out;
        }
        if (fmt == "gltf" || fmt == "glb") {
            std::vector<TopoDS_Shape> meshed;
            for (const TopoDS_Shape& body : bodies)
                if (shape_to_mesh(body, tess).triangle_count() != 0) meshed.push_back(body);
            if (meshed.empty())
                throw OpError("export_failed", "export: no triangles to write as " + fmt,
                              {{"path", out}, {"format", fmt}});
            write_gltf(meshed, out, fmt == "glb");
            return out;
        }
        if (fmt == "3mf") {
#ifdef CHUDWARE_WITH_LIB3MF
            // Tessellate each body (Python BRepMesh defaults via shape_to_mesh) into
            // one named 3MF mesh object, mirroring _export_3mf_lib3mf (one mesh object
            // + build item per body). Export parity is gated as a ROUND-TRIP (the OCCT
            // tessellation differs from the oracle's), not byte/mesh-identical.
            std::vector<NamedMesh> meshes;
            for (std::size_t i = 0; i < bodies.size(); ++i) {
                if (bodies[i].IsNull()) continue;
                NamedMesh nm;
                nm.name = "Body" + std::to_string(i + 1);
                nm.mesh = shape_to_mesh(bodies[i], tess);
                if (nm.mesh.triangle_count() > 0) meshes.push_back(std::move(nm));
            }
            write_3mf_meshes(meshes, out);
            return out;
#else
            throw OpError("export_failed",
                          "3MF export requires lib3mf — rebuild core with the SDK provisioned "
                          "(-DCHUDWARE_WITH_OCCT=ON pulls in ProvisionLib3mf).",
                          {{"format", fmt}, {"reason", "lib3mf not provisioned"}});
#endif
        }
        if (fmt == "3dm") {
#ifdef CHUDWARE_WITH_OPENNURBS
            // Tessellate each body (Python BRepMesh defaults via shape_to_mesh) into
            // one named ON_Mesh object, mirroring _export_3dm (one AddMesh per body).
            // Export parity is gated as a ROUND-TRIP (the OCCT tessellation differs
            // from the oracle's), not byte/mesh-identical.
            std::vector<OnNamedMesh> meshes;
            for (std::size_t i = 0; i < bodies.size(); ++i) {
                if (bodies[i].IsNull()) continue;
                OnNamedMesh nm;
                nm.name = "Body" + std::to_string(i + 1);
                nm.mesh = shape_to_mesh(bodies[i], tess);
                if (nm.mesh.triangle_count() > 0) meshes.push_back(std::move(nm));
            }
            write_3dm_meshes(meshes, out);
            return out;
#else
            throw OpError("export_failed",
                          "3DM export requires openNURBS — rebuild core with the SDK provisioned "
                          "(-DCHUDWARE_WITH_OCCT=ON pulls in ProvisionOpenNURBS).",
                          {{"format", fmt}, {"reason", "openNURBS not provisioned"}});
#endif
        }
        if (fmt == "sat") {
            // ACIS 7.0 text, one ACIS body per body with faces (AutoCAD ACISIN / IMPORT makes
            // one 3DSOLID each). Face-less bodies (2D profiles) have no ACIS form here.
            std::vector<TopoDS_Shape> parts;
            for (const TopoDS_Shape& b : bodies) {
                if (b.IsNull()) continue;
                bool solid = false;
                const TopoDS_Shape part = acis_part(to_units(b, tess.units), true, &solid);
                if (!part.IsNull()) parts.push_back(part);
            }
            if (parts.empty())
                throw OpError("export_failed",
                              "SAT export: no body has faces (2D profiles go to DXF/DWG)",
                              {{"path", out}, {"format", fmt}});
            SatWriteOptions opt;
            opt.version = 700;
            opt.mm_per_unit = insunits_mm(tess.units);
            std::string text;
            try {
                text = write_sat_bodies(parts, opt);
            } catch (const std::exception& e) {
                throw OpError("export_failed", std::string("SAT export: ") + e.what(),
                              {{"path", out}, {"format", fmt}});
            }
            const std::filesystem::path staged = out + ".partial";
            {
                std::ofstream os(staged, std::ios::binary | std::ios::trunc);
                os.write(text.data(), static_cast<std::streamsize>(text.size()));
                if (!os.flush())
                    throw OpError("export_failed", "SAT export: write failed",
                                  {{"path", out}, {"format", fmt}});
            }
            std::error_code ec;
            std::filesystem::rename(staged, out, ec);
            if (ec)
                throw OpError("export_failed", "SAT export: cannot move into place: " + ec.message(),
                              {{"path", out}, {"format", fmt}});
            return out;
        }
        if (fmt == "dxf" || fmt == "dwg") {
            const DxfDocument doc = bodies_to_dxf_document(bodies, tess);
            try {
                return fmt == "dxf" ? write_dxf(out, doc) : write_dwg(out, doc);
            } catch (const std::runtime_error& e) {
                throw OpError("export_failed", e.what(), {{"format", fmt}, {"path", out}});
            }
        }
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& e) {
        throw OpError("export_failed", std::string("export to ") + fmt + " threw: " + e.what(),
                      {{"format", fmt}, {"path", out}});
    }
    throw OpError("export_failed",
                  "export format '" + fmt + "' is not supported by the C++ core",
                  {{"format", fmt}, {"reason", "unsupported format"}});
}

std::pair<std::size_t, std::size_t> estimate_tessellation(
    const std::vector<TopoDS_Shape>& bodies, const TessOptions& tess) {
    std::size_t tris = 0, verts = 0;
    for (const TopoDS_Shape& s : bodies) {
        if (s.IsNull()) continue;
        const MeshData m = shape_to_mesh(s, tess);
        tris += m.triangle_count();
        verts += m.vertex_count();
    }
    return {tris, verts};
}

// --------------------------------------------------------------------------- import

namespace {

std::vector<std::pair<std::string, TopoDS_Shape>> name_bodies(
    const std::vector<TopoDS_Shape>& solids) {
    std::vector<std::pair<std::string, TopoDS_Shape>> out;
    for (std::size_t i = 0; i < solids.size(); ++i)
        out.emplace_back("Body" + std::to_string(i + 1), solids[i]);
    return out;
}

TopoDS_Shape place_solidworks_body(const TopoDS_Shape& shape,
                                   const std::array<double, 16>& m) {
    static const std::array<double, 16> identity = {
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0};
    if (m == identity) return shape;
    // SolidWorks serializes row-vector transforms in metres. OCCT SetValues takes the
    // equivalent column-vector 3x4 matrix, and Chudware's geometry unit is millimetres.
    gp_Trsf trsf;
    trsf.SetValues(
        m[0], m[4], m[8], m[12] * 1000.0,
        m[1], m[5], m[9], m[13] * 1000.0,
        m[2], m[6], m[10], m[14] * 1000.0);
    // Prefer a pure LOCATION for a rigid transform: BRepBuilderAPI_Transform's copy
    // rebuilds every face, which throws away the Poly_Triangulation carrying the mesh
    // lane's smooth normals (attach_mesh_smooth_normals) and would leave assembly instances
    // faceted while an identity-placed part shaded smoothly. A located instance also shares
    // one TShape across instances, which is what the shared-mesh cache above already
    // assumes. Non-rigid (scaled) matrices still go through the copying transformer.
    if (std::abs(trsf.ScaleFactor() - 1.0) < 1e-12)
        return shape.Moved(TopLoc_Location(trsf));
    BRepBuilderAPI_Transform placed(shape, trsf, Standard_True);
    if (!placed.IsDone() || placed.Shape().IsNull())
        throw OpError("import_failed", "SolidWorks component transform failed");
    return placed.Shape();
}

}  // namespace

std::vector<std::pair<std::string, TopoDS_Shape>> import_bodies(
    const std::string& path, const std::string& fmt, std::vector<std::string>* warnings,
    int* source_units) {
    if (source_units) *source_units = 0;
    if (!std::filesystem::exists(path))
        throw OpError("import_failed", "import: file not found: " + path, {{"path", path}});
    try {
        if (fmt == "step") {
            STEPControl_Reader r;
            if (r.ReadFile(path.c_str()) != IFSelect_RetDone)
                throw OpError("import_failed", "STEP reader: ReadFile failed", {{"path", path}});
            const int roots = r.NbRootsForTransfer();
            const int transferred = r.TransferRoots();
            if (transferred != roots) {
                const std::string message = std::to_string(transferred) + " of " +
                                            std::to_string(roots) + " STEP roots translated";
                if (warnings && transferred > 0) warnings->push_back(message);
                else throw OpError("import_failed", "STEP reader: " + message,
                                   {{"path", path}, {"transferred", transferred}, {"roots", roots}});
            }
            TopoDS_Shape shape = r.OneShape();
            if (shape.IsNull())
                throw OpError("import_failed", "STEP reader: no shape transferred", {{"path", path}});
            return name_bodies(split_import_bodies(shape));
        }
        if (fmt == "iges") {
            IGESControl_Reader r;
            if (r.ReadFile(path.c_str()) != IFSelect_RetDone)
                throw OpError("import_failed", "IGES reader: ReadFile failed", {{"path", path}});
            const int roots = r.NbRootsForTransfer();
            const int transferred = r.TransferRoots();
            if (transferred != roots) {
                const std::string message = std::to_string(transferred) + " of " +
                                            std::to_string(roots) + " IGES roots translated";
                if (warnings && transferred > 0) warnings->push_back(message);
                else throw OpError("import_failed", "IGES reader: " + message,
                                   {{"path", path}, {"transferred", transferred}, {"roots", roots}});
            }
            TopoDS_Shape raw = r.OneShape();
            if (raw.IsNull())
                throw OpError("import_failed", "IGES reader: no shape transferred", {{"path", path}});
            // IGES is a face soup: sew into shells + heal the topology (formats.py _import_iges).
            BRepBuilderAPI_Sewing sew(0.01);
            sew.Add(raw);
            sew.Perform();
            TopoDS_Shape sewn = sew.SewedShape();
            ShapeFix_Shape fix(sewn);
            fix.Perform();
            TopoDS_Shape fixed = fix.Shape();
            if (fixed.IsNull()) fixed = raw;
            // Promote every valid closed free shell without discarding its siblings.
            // Existing solids retain their order; newly recovered solids follow them.
            BRepTools_ReShape remaining;
            std::vector<TopoDS_Shape> promoted;
            for (TopExp_Explorer ex(fixed, TopAbs_SHELL, TopAbs_SOLID);
                 ex.More(); ex.Next()) {
                const TopoDS_Shell shell = TopoDS::Shell(ex.Current());
                if (remaining.IsRecorded(shell) || !BRep_Tool::IsClosed(shell)) continue;
                try {
                    ShapeFix_Solid sfix;
                    const TopoDS_Shape solid = sfix.SolidFromShell(shell);
                    if (!solid.IsNull() && solid.ShapeType() == TopAbs_SOLID &&
                        BRepCheck_Analyzer(solid).IsValid()) {
                        remaining.Remove(shell);
                        promoted.push_back(solid);
                    }
                } catch (const Standard_Failure&) {
                    // Preserve the original shell if promotion cannot recover a solid.
                }
            }
            if (promoted.empty()) return name_bodies(split_import_bodies(fixed));
            promoted.insert(promoted.begin(), remaining.Apply(fixed));
            return name_bodies(split_import_bodies(as_compound(promoted)));
        }
        if (fmt == "brep") {
            TopoDS_Shape shape;
            BRep_Builder b;
            if (!BRepTools::Read(shape, path.c_str(), b) || shape.IsNull())
                throw OpError("import_failed", "BREP reader returned failure", {{"path", path}});
            return name_bodies(split_import_bodies(shape));
        }
        if (fmt == "sldprt" || fmt == "sldasm") {
            // Portable modern-SolidWorks lane. Recover the saved display triangle strips, sew
            // each distinct component once, then instance its OCCT shape with the assembly's
            // composed transform. On Windows the session layer still prefers COM -> exact STEP;
            // this path is the cross-platform fallback and intentionally reports tessellation
            // fidelity to its caller.
            const bool assembly = fmt == "sldasm";
            const auto saved = read_solidworks_tessellation(path, assembly);
            std::unordered_map<const MeshData*, TopoDS_Shape> sewn;
            std::vector<std::pair<std::string, TopoDS_Shape>> out;
            for (const SolidWorksTessellationBody& item : saved) {
                if (!item.mesh || item.mesh->triangle_count() == 0) continue;
                const MeshData* key = item.mesh.get();
                auto cached = sewn.find(key);
                if (cached == sewn.end()) {
                    TopoDS_Shape base = mesh_to_shape(*item.mesh);
                    if (base.IsNull())
                        throw OpError("import_failed",
                                      "SolidWorks saved tessellation for '" + item.name +
                                          "' could not be sewn into a body",
                                      {{"path", path}, {"format", fmt}, {"component", item.name}});
                    cached = sewn.emplace(key, std::move(base)).first;
                }
                TopoDS_Shape placed = place_solidworks_body(cached->second, item.transform);
                const auto solids = split_import_bodies(placed);
                for (std::size_t i = 0; i < solids.size(); ++i) {
                    std::string name = item.name;
                    if (solids.size() > 1) name += "/Body" + std::to_string(i + 1);
                    out.emplace_back(std::move(name), solids[i]);
                }
            }
            if (out.empty())
                throw OpError("import_failed",
                              "SolidWorks file contains no usable saved tessellation",
                              {{"path", path}, {"format", fmt}});
            return out;
        }
        if (fmt == "stl" || fmt == "obj" || fmt == "ply" || fmt == "gltf" || fmt == "glb") {
            // Mesh lane: read the triangle soup, sew it into a shell/solid (the C++
            // analogue of the oracle's MeshBody + "as_solid" conversion). A closed mesh
            // becomes a SOLID with a real volume; an open mesh stays a shell.
            MeshData mesh = read_mesh(path, fmt);
            if (mesh.triangle_count() == 0)
                throw OpError("import_failed", "import: no triangles in '" + path + "'",
                              {{"path", path}, {"format", fmt}});
            TopoDS_Shape shape = mesh_to_shape(mesh);
            if (shape.IsNull())
                throw OpError("import_failed",
                              "import: mesh in '" + path + "' could not be sewn into a body",
                              {{"path", path}, {"format", fmt}});
            return name_bodies(split_import_bodies(shape));
        }
        if (fmt == "3mf") {
#ifdef CHUDWARE_WITH_LIB3MF
            // 3MF via lib3mf: one body per mesh object (matching _import_3mf), each
            // sewn into a shell/solid (the same as_solid path as the other mesh
            // formats). Body names follow the oracle: the 3MF object name, else
            // "<stem>_<idx>". Both the C++ and Python lib3mf paths convert the
            // model's declared length unit into Chudware millimetres.
            std::vector<NamedMesh> objs = read_3mf_objects(path);
            if (objs.empty())
                throw OpError("import_failed", "no mesh objects found in 3MF '" + path + "'",
                              {{"path", path}, {"format", fmt}});
            std::vector<std::pair<std::string, TopoDS_Shape>> out;
            for (const NamedMesh& nm : objs) {
                TopoDS_Shape shape = mesh_to_shape(nm.mesh);
                if (shape.IsNull())
                    throw OpError("import_failed",
                                  "3MF object '" + nm.name + "' could not be sewn into a body",
                                  {{"path", path}, {"format", fmt}});
                out.emplace_back(nm.name, shape);
            }
            return out;
#else
            throw OpError("import_failed",
                          "3MF import requires lib3mf — rebuild core with the SDK provisioned "
                          "(-DCHUDWARE_WITH_OCCT=ON pulls in ProvisionLib3mf).",
                          {{"format", fmt}, {"reason", "lib3mf not provisioned"}});
#endif
        }
        if (fmt == "3dm") {
#ifdef CHUDWARE_WITH_OPENNURBS
            // 3dm via openNURBS: one body per mesh object (matching _import_3dm),
            // each sewn into a shell/solid (the same as_solid path as the other mesh
            // formats). Body names follow the oracle: the object attribute name, else
            // "<stem>_<idx>". The raw vertex/triangle extraction is BIT-IDENTICAL to
            // the Python rhino3dm path (both read the SAME stored ON_Mesh m_V/m_F).
            std::vector<OnNamedMesh> objs = read_3dm_objects(path);
            if (objs.empty())
                throw OpError("import_failed", "no mesh objects found in 3DM '" + path + "'",
                              {{"path", path}, {"format", fmt}});
            std::vector<std::pair<std::string, TopoDS_Shape>> out;
            for (const OnNamedMesh& nm : objs) {
                TopoDS_Shape shape = mesh_to_shape(nm.mesh);
                if (shape.IsNull())
                    throw OpError("import_failed",
                                  "3DM object '" + nm.name + "' could not be sewn into a body",
                                  {{"path", path}, {"format", fmt}});
                out.emplace_back(nm.name, shape);
            }
            return out;
#else
            throw OpError("import_failed",
                          "3DM import requires openNURBS — rebuild core with the SDK provisioned "
                          "(-DCHUDWARE_WITH_OCCT=ON pulls in ProvisionOpenNURBS).",
                          {{"format", fmt}, {"reason", "openNURBS not provisioned"}});
#endif
        }
        if (fmt == "dxf") {
            // The 2D profile stays one Compound body (faces + free wires; splitting would drop
            // the free wires); 3D mesh entities add one body per layer; each ACIS entity
            // (inline SAT, or SAB in the R2013+ ACDSDATA section) is its own body.
            return dxf_bodies(path, path, "dxf", warnings, source_units);
        }
        if (fmt == "sat") return sat_bodies(path, warnings, source_units);
        if (fmt == "dwg") {
            // 2D entities: LibreDWG dwg2dxf (literal argv), then the DXF lane. ACIS bodies of
            // R2013+ files live in the DWG data storage, which dwg2dxf drops; the native reader
            // supplies them by entity handle. A data storage it cannot read leaves the 2D
            // import intact and names the loss.
            DwgNativeInfo native;
            try {
                native = read_dwg_native(path);
            } catch (const std::exception& e) {
                native.warnings.push_back(std::string("the DWG ACIS data storage could not be "
                                                      "read (") + e.what() +
                                          "); 3D solids may be missing");
            }
            if (libredwg_tool("dwg2dxf").empty() && !native.acis.empty())
                return dwg_native_only_bodies(native, path, warnings, source_units);
            std::string tmp_dxf = dwg_to_temp_dxf(path);
            struct TempDxfCleanup {
                const std::string& path;
                ~TempDxfCleanup() {
                    std::error_code ec;
                    std::filesystem::remove(path, ec);
                }
            } cleanup{tmp_dxf};
            return dxf_bodies(tmp_dxf, path, "dwg", warnings, source_units, &native);
        }
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& e) {
        throw OpError("import_failed", std::string("import of ") + fmt + " threw: " + e.what(),
                      {{"format", fmt}, {"path", path}});
    }
    throw OpError("import_failed",
                  "import format '" + fmt + "' is not supported by the C++ core "
                  "(Parasolid x_t/x_b must be converted through SolidWorks COM -> STEP)",
                  {{"format", fmt}, {"reason", "unsupported format"}});
}

// --------------------------------------------------------------------- BREP <-> base64

namespace {

const char* kB64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string b64_encode(const std::string& in) {
    std::string out;
    out.reserve(((in.size() + 2) / 3) * 4);
    std::size_t i = 0;
    for (; i + 2 < in.size(); i += 3) {
        unsigned n = (static_cast<unsigned char>(in[i]) << 16) |
                     (static_cast<unsigned char>(in[i + 1]) << 8) |
                     static_cast<unsigned char>(in[i + 2]);
        out += kB64[(n >> 18) & 63];
        out += kB64[(n >> 12) & 63];
        out += kB64[(n >> 6) & 63];
        out += kB64[n & 63];
    }
    if (i < in.size()) {
        unsigned n = static_cast<unsigned char>(in[i]) << 16;
        if (i + 1 < in.size()) n |= static_cast<unsigned char>(in[i + 1]) << 8;
        out += kB64[(n >> 18) & 63];
        out += kB64[(n >> 12) & 63];
        out += (i + 1 < in.size()) ? kB64[(n >> 6) & 63] : '=';
        out += '=';
    }
    return out;
}

std::string b64_decode(const std::string& in) {
    static int rev[256];
    static bool init = false;
    if (!init) {
        for (int k = 0; k < 256; ++k) rev[k] = -1;
        for (int k = 0; k < 64; ++k) rev[static_cast<unsigned char>(kB64[k])] = k;
        init = true;
    }
    std::string out;
    out.reserve(in.size() / 4 * 3);
    int val = 0, bits = -8;
    for (unsigned char c : in) {
        if (c == '=' || rev[c] < 0) {
            if (c == '=') break;
            continue;  // skip whitespace/newlines
        }
        val = (val << 6) | rev[c];
        bits += 6;
        if (bits >= 0) {
            out += static_cast<char>((val >> bits) & 0xFF);
            bits -= 8;
        }
    }
    return out;
}

}  // namespace

std::string shape_to_brep_b64(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return std::string();
    std::ostringstream oss;
    // Explicit flags, because the 2-argument alias writes triangulation but NOT normals
    // (theWithNormals defaults to FALSE) — and the triangulation is what carries the
    // SolidWorks per-vertex normals that make an imported part shade smoothly. Without
    // normals here the smoothing survived the import but not a .step+ save/reopen, since
    // the embedded blob is what document.cpp rebuilds the import feature from.
    BRepTools::Write(shape, oss, /*theWithTriangles=*/Standard_True,
                     /*theWithNormals=*/Standard_True, TopTools_FormatVersion_CURRENT);
    return b64_encode(oss.str());
}

TopoDS_Shape brep_b64_to_shape(const std::string& b64) {
    if (b64.empty()) return TopoDS_Shape();
    std::istringstream iss(b64_decode(b64));
    TopoDS_Shape shape;
    BRep_Builder builder;
    try {
        BRepTools::Read(shape, iss, builder);
    } catch (...) {
        return TopoDS_Shape();
    }
    return shape;
}

}  // namespace chudware::doc
