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
#include "doc/solidworks_tessellation.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <random>

#ifdef _WIN32
#include <process.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>
#endif
#include <unordered_map>

#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeSolid.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepBuilderAPI_Sewing.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <BRepTools.hxx>
#include <GC_MakeArcOfCircle.hxx>
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
#include <StlAPI_Writer.hxx>
#include <TColStd_IndexedDataMapOfStringString.hxx>
#include <TDF_LabelSequence.hxx>
#include <TDocStd_Document.hxx>
#include <TopAbs.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopTools_FormatVersion.hxx>
#include <TopTools_HSequenceOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shell.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <Standard_Failure.hxx>
#include <XCAFApp_Application.hxx>
#include <XCAFDoc_DocumentTool.hxx>
#include <XCAFDoc_ShapeTool.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>
#include <gp_Trsf.hxx>

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

// Tessellate a shape in place at the requested quality so a mesh writer has a
// triangulation to emit. The prior triangulation is cleared first so the exact
// requested deflection is honored even if the shape was already meshed at another
// quality (e.g. a finer dialog "estimate" preceding a coarser export).
void mesh_shape(const TopoDS_Shape& shape, const TessOptions& tess = TessOptions{}) {
    BRepTools::Clean(shape);
    BRepMesh_IncrementalMesh m(shape, tess.lin_deflection, tess.relative ? Standard_True : Standard_False,
                               tess.ang_deflection, Standard_True);
    m.Perform();
}

std::string abs_path(const std::string& path) {
    std::error_code ec;
    std::filesystem::path p = std::filesystem::absolute(path, ec);
    return ec ? path : p.string();
}

#ifdef CHUDWARE_WITH_LIB3MF
// A named mesh extracted from a 3MF mesh object — the C++ analogue of the
// oracle's (name, MeshBody(verts, tris)) tuple. Vertices are float32 (Lib3MF
// sPosition.m_Coordinates is Lib3MF_single == float), triangle indices uint32
// (sTriangle.m_Indices is Lib3MF_uint32) — IDENTICAL element types to the Python
// _import_3mf path, which builds np.float32 verts + np.uint32 tris from the very
// same lib3mf structs.
struct NamedMesh {
    std::string name;
    MeshData mesh;  // verts stored as doubles, but the source floats round-trip exactly
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

// Read every mesh object out of a .3mf, BIT-IDENTICAL to engine/doc/formats.py
// ::_import_3mf: iterate the model's mesh objects in file order, skip empty ones,
// pull GetVertices()/GetTriangleIndices(), name = GetName() or "<stem>_<idx>".
std::vector<NamedMesh> read_3mf_objects(const std::string& path) {
    Lib3MF::PWrapper wrapper = load_lib3mf();
    Lib3MF::PModel model = wrapper->CreateModel();
    Lib3MF::PReader reader = model->QueryReader("3mf");
    reader->ReadFromFile(path);

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
            // m_Coordinates is float32; widen to double for MeshData — the value is
            // preserved exactly (every float is representable as a double), so a
            // float32 reconstruction on the gate side is bit-identical to numpy's.
            nm.mesh.verts.push_back(static_cast<double>(v.m_Coordinates[0]));
            nm.mesh.verts.push_back(static_cast<double>(v.m_Coordinates[1]));
            nm.mesh.verts.push_back(static_cast<double>(v.m_Coordinates[2]));
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
        t.value = trim(value_line);
        tags.push_back(std::move(t));
    }
    return tags;
}

// Pull every ENTITIES-section entity out of the tag stream. Only code-0 tags whose value
// names a known/unknown entity type start a new entity; everything up to the next code-0
// (or ENDSEC) belongs to it. Sections other than ENTITIES are skipped entirely.
std::vector<Entity> parse_entities(const std::vector<Tag>& tags) {
    std::vector<Entity> out;
    bool in_entities = false;
    std::string pending_section;
    bool expect_section_name = false;
    Entity* cur = nullptr;

    for (const Tag& t : tags) {
        if (t.code == 0) {
            if (t.value == "SECTION") {
                pending_section.clear();
                expect_section_name = true;
                in_entities = false;
                cur = nullptr;
                continue;
            }
            if (t.value == "ENDSEC") {
                in_entities = false;
                cur = nullptr;
                continue;
            }
            if (t.value == "EOF") break;
            if (in_entities) {
                out.emplace_back();
                out.back().type = t.value;
                cur = &out.back();
            } else {
                cur = nullptr;
            }
            continue;
        }
        if (expect_section_name && t.code == 2) {
            pending_section = t.value;
            in_entities = (pending_section == "ENTITIES");
            expect_section_name = false;
            continue;
        }
        if (cur != nullptr) cur->tags.push_back(t);
    }
    return out;
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
    gp_Ax2 ax(gp_Pnt(cx, cy, cz), gp_Dir(0.0, 0.0, 1.0), gp_Dir(mx, my, mz));
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

// POLYLINE (old-style) + its trailing VERTEX/SEQEND entities → line/bulge-arc edges.
// `i` indexes the POLYLINE entity in `entities`; advances `i` past the VERTEX/SEQEND run it
// consumes so the caller's loop continues after SEQEND.
void polyline_old_style_to_edges(const std::vector<dxf::Entity>& entities, std::size_t& i,
                                 std::vector<TopoDS_Edge>& out) {
    const dxf::Entity& head = entities[i];
    const long flags = dxf::get_i(head, 70, 0);
    const bool closed = (flags & 0x1) != 0;
    std::vector<dxf::PolyVertex> pts;
    std::size_t j = i + 1;
    for (; j < entities.size(); ++j) {
        const dxf::Entity& e = entities[j];
        if (e.type == "SEQEND") { ++j; break; }
        if (e.type != "VERTEX") continue;  // tolerate stray entities before SEQEND
        dxf::PolyVertex v;
        v.x = dxf::get_d(e, 10, 0.0);
        v.y = dxf::get_d(e, 20, 0.0);
        v.z = dxf::get_d(e, 30, 0.0);
        v.bulge = dxf::get_d(e, 42, 0.0);
        pts.push_back(v);
    }
    dxf::polyline_vertices_to_edges(pts, closed, out);
    i = j - 1;  // caller's for-loop does ++i
}

// Entities → OCCT edges (port of _entities_to_edges' core-entity subset). Each malformed
// entity is wrapped so one bad entity cannot abort the whole import; `skipped` counts every
// entity that produced no edge (unsupported type, or a construction failure).
std::vector<TopoDS_Edge> dxf_entities_to_edges(const std::vector<dxf::Entity>& entities,
                                               int& skipped) {
    std::vector<TopoDS_Edge> edges;
    for (std::size_t i = 0; i < entities.size(); ++i) {
        const dxf::Entity& e = entities[i];
        const std::size_t before = edges.size();
        try {
            if (e.type == "LINE") {
                const double x1 = dxf::get_d(e, 10), y1 = dxf::get_d(e, 20), z1 = dxf::get_d(e, 30);
                const double x2 = dxf::get_d(e, 11), y2 = dxf::get_d(e, 21), z2 = dxf::get_d(e, 31);
                gp_Pnt p1(x1, y1, z1), p2(x2, y2, z2);
                if (p1.Distance(p2) > 1e-9) edges.push_back(BRepBuilderAPI_MakeEdge(p1, p2));
            } else if (e.type == "CIRCLE") {
                const double cx = dxf::get_d(e, 10), cy = dxf::get_d(e, 20), cz = dxf::get_d(e, 30);
                const double r = dxf::get_d(e, 40);
                gp_Circ c(gp_Ax2(gp_Pnt(cx, cy, cz), gp_Dir(0, 0, 1)), r);
                edges.push_back(BRepBuilderAPI_MakeEdge(c));
            } else if (e.type == "ARC") {
                const double cx = dxf::get_d(e, 10), cy = dxf::get_d(e, 20), cz = dxf::get_d(e, 30);
                const double r = dxf::get_d(e, 40);
                double a1 = dxf::get_d(e, 50) * kPi / 180.0;
                double a2 = dxf::get_d(e, 51) * kPi / 180.0;
                if (a2 <= a1) a2 += 2.0 * kPi;
                gp_Circ c(gp_Ax2(gp_Pnt(cx, cy, cz), gp_Dir(0, 0, 1)), r);
                edges.push_back(BRepBuilderAPI_MakeEdge(c, a1, a2));
            } else if (e.type == "LWPOLYLINE") {
                dxf::lwpolyline_to_edges(e, edges);
            } else if (e.type == "POLYLINE") {
                polyline_old_style_to_edges(entities, i, edges);
            } else if (e.type == "SPLINE") {
                TopoDS_Edge se = dxf::spline_edge(e);
                if (!se.IsNull()) edges.push_back(se);
            } else if (e.type == "ELLIPSE") {
                TopoDS_Edge ee = dxf::ellipse_edge(e);
                if (!ee.IsNull()) edges.push_back(ee);
            } else {
                // INSERT / TEXT / DIMENSION / HATCH / … — out of scope for this increment
                // (see the file-level comment); counted as skipped by the size check below.
            }
        } catch (const Standard_Failure&) {
            // one bad entity must not abort the whole import
        } catch (const std::exception&) {
            // ditto for any non-OCCT exception (e.g. a malformed numeric tag)
        }
        if (edges.size() == before) ++skipped;
    }
    return edges;
}

// Stitch loose edges into connected wires; closed wires → Faces (kept as a Wire when the
// face build fails, e.g. non-planar/self-intersecting), open wires stay as reference wires.
// Port of _assemble_wires.
void dxf_assemble_wires(const std::vector<TopoDS_Edge>& edges, TopoDS_Compound& out) {
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
        if (w.Closed()) {
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

// Parse an ASCII DXF file and return ONE Compound of faces (closed regions) + free wires
// (open) — one file, one body, mirroring the mesh importers and formats_dxf.py::
// bodies_from_doc. Throws OpError("import_failed") when the file carries no importable
// core-entity geometry (LINE/CIRCLE/ARC/LWPOLYLINE/POLYLINE) — ELLIPSE/SPLINE/INSERT/text/
// dimensions/hatches count toward the "skipped" total in that message but are not
// themselves imported yet.
TopoDS_Shape read_dxf_shape(const std::string& path) {
    std::vector<dxf::Tag> tags = dxf::read_tags(path);
    std::vector<dxf::Entity> entities = dxf::parse_entities(tags);
    int skipped = 0;
    std::vector<TopoDS_Edge> edges = dxf_entities_to_edges(entities, skipped);
    if (edges.empty())
        throw OpError("import_failed",
                      "DXF '" + path + "' has no importable 2D geometry (" +
                          std::to_string(skipped) +
                          " unsupported/empty entity(ies) skipped — text, dimensions, "
                          "hatches, ELLIPSE/SPLINE/INSERT and 3D mesh entities are not "
                          "imported by this importer).",
                      {{"path", path}, {"format", "dxf"}});
    TopoDS_Compound comp;
    dxf_assemble_wires(edges, comp);
    return comp;
}

// --------------------------------------------------------------------------- DWG import
//
// DWG is a proprietary binary format; we do NOT parse it in-process (license/complexity
// isolation, same spirit as the SolidWorks COM lane). LibreDWG's GPL `dwg2dxf` remains
// fully out-of-process. Arguments are passed as an argv vector, never through a shell.
bool run_dwg2dxf(const std::string& output, const std::string& input) {
#ifdef _WIN32
    const char* argv[] = {"dwg2dxf", "-o", output.c_str(), input.c_str(), nullptr};
    return ::_spawnvp(_P_WAIT, argv[0], argv) == 0;
#else
    const pid_t pid = ::fork();
    if (pid < 0) return false;
    if (pid == 0) {
        const int null_fd = ::open("/dev/null", O_WRONLY);
        if (null_fd >= 0) {
            (void)::dup2(null_fd, STDOUT_FILENO);
            (void)::dup2(null_fd, STDERR_FILENO);
            ::close(null_fd);
        }
        const char* argv[] = {"dwg2dxf", "-o", output.c_str(), input.c_str(), nullptr};
        ::execvp(argv[0], const_cast<char* const*>(argv));
        ::_exit(127);
    }
    int status = 0;
    while (::waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return false;
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
#endif
}

}  // namespace

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

    const bool converted = run_dwg2dxf(out_dxf.string(), path);
    if (converted && std::filesystem::exists(out_dxf, ec) &&
        std::filesystem::file_size(out_dxf, ec) > 0 && !ec)
        return out_dxf.string();

    std::filesystem::remove(out_dxf, ec);
    throw OpError("import_failed",
                  "DWG import needs a DWG->DXF converter (LibreDWG dwg2dxf) on PATH; none "
                  "found or conversion failed. Or convert to DXF/STEP first.",
                  {{"path", path}, {"format", "dwg"}});
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
        {".dxf", "dxf"}, {".dwg", "dwg"},
    };
    for (const auto& [e, f] : kMap)
        if (ext == e) return f;
    throw OpError("invalid_op", "cannot detect format from extension '" + ext + "'",
                  {{"reason", "unknown extension"}, {"path", path}});
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
        // DXF (2D vector drawings) — import-only; 2D-projection-from-3D DXF export is
        // a separate follow-up, so can_export is false (oracle comment carried over).
        {"dxf",    "AutoCAD DXF",         {".dxf"},          "brep",        true,  false},
        // DWG (AutoCAD binary) — import-only via an external DWG->DXF converter; never
        // written.
        {"dwg",    "AutoCAD DWG",         {".dwg"},          "brep",        true,  false},
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

// ---- PLY reader: ASCII + binary_little_endian (trimesh's default writer) -----
// OCCT 7.9.3 has no PLY *reader* (TKDESTL ships only RWPly_CafWriter). The oracle
// (trimesh) writes binary_little_endian PLY by default, so the C++ reader must handle
// both encodings. We parse the standard layout: a `vertex` element with float x/y/z
// (plus possible extra per-vertex props we skip by byte-size) and a `face` element with
// a `list <countT> <idxT> vertex_indices` property. Big-endian + non-standard layouts
// are deferred (raise import_failed) — trimesh never emits them for our meshes.
namespace ply {

struct Prop {
    std::string name;
    std::string type;      // scalar type (or list-value type for a list prop)
    bool is_list = false;
    std::string count_type;  // list count scalar type
};

// Read one scalar of the given PLY type from a little-endian byte stream.
double read_le_scalar(const unsigned char*& p, const std::string& t) {
    auto rd = [&](int n) {
        uint64_t v = 0;
        for (int i = 0; i < n; ++i) v |= static_cast<uint64_t>(p[i]) << (8 * i);
        p += n;
        return v;
    };
    if (t == "char" || t == "int8") return static_cast<double>(static_cast<int8_t>(rd(1)));
    if (t == "uchar" || t == "uint8") return static_cast<double>(static_cast<uint8_t>(rd(1)));
    if (t == "short" || t == "int16") return static_cast<double>(static_cast<int16_t>(rd(2)));
    if (t == "ushort" || t == "uint16") return static_cast<double>(static_cast<uint16_t>(rd(2)));
    if (t == "int" || t == "int32") return static_cast<double>(static_cast<int32_t>(rd(4)));
    if (t == "uint" || t == "uint32") return static_cast<double>(static_cast<uint32_t>(rd(4)));
    if (t == "float" || t == "float32") {
        uint32_t b = static_cast<uint32_t>(rd(4));
        float f;
        std::memcpy(&f, &b, 4);
        return static_cast<double>(f);
    }
    if (t == "double" || t == "float64") {
        uint64_t b = rd(8);
        double d;
        std::memcpy(&d, &b, 8);
        return d;
    }
    if (t == "int64" || t == "uint64") return static_cast<double>(rd(8));
    return 0.0;
}

}  // namespace ply

MeshData read_ply(const std::string& path) {
    std::ifstream is(path, std::ios::in | std::ios::binary);
    if (!is) throw OpError("import_failed", "PLY: cannot open input", {{"path", path}});
    std::string line;
    std::getline(is, line);
    if (line.rfind("ply", 0) != 0)
        throw OpError("import_failed", "PLY: missing magic header", {{"path", path}});
    enum Enc { ASCII, BIN_LE } enc = ASCII;
    bool have_format = false;
    std::size_t n_vert = 0, n_face = 0;
    std::vector<ply::Prop> vprops, fprops;
    enum { NONE, VERTEX, FACE } cur = NONE;
    std::size_t header_end = 0;
    while (std::getline(is, line)) {
        std::istringstream ls(line);
        std::string tok;
        ls >> tok;
        if (tok == "format") {
            std::string f;
            ls >> f;
            have_format = true;
            if (f == "ascii") enc = ASCII;
            else if (f == "binary_little_endian") enc = BIN_LE;
            else
                throw OpError("import_failed",
                              "PLY: unsupported encoding '" + f +
                                  "' (ascii / binary_little_endian only)",
                              {{"path", path}});
        } else if (tok == "element") {
            std::string e;
            std::size_t n;
            ls >> e >> n;
            if (e == "vertex") { n_vert = n; cur = VERTEX; }
            else if (e == "face") { n_face = n; cur = FACE; }
            else { cur = NONE; }
        } else if (tok == "property" && cur != NONE) {
            ply::Prop pr;
            std::string t;
            ls >> t;
            if (t == "list") {
                pr.is_list = true;
                ls >> pr.count_type >> pr.type >> pr.name;
            } else {
                pr.type = t;
                ls >> pr.name;
            }
            (cur == VERTEX ? vprops : fprops).push_back(pr);
        } else if (tok == "end_header") {
            header_end = static_cast<std::size_t>(is.tellg());
            break;
        }
    }
    (void)have_format;
    int vx = -1, vy = -1, vz = -1;
    for (std::size_t i = 0; i < vprops.size(); ++i) {
        if (vprops[i].name == "x") vx = static_cast<int>(i);
        else if (vprops[i].name == "y") vy = static_cast<int>(i);
        else if (vprops[i].name == "z") vz = static_cast<int>(i);
    }
    if (vx < 0 || vy < 0 || vz < 0)
        throw OpError("import_failed", "PLY: missing x/y/z vertex properties", {{"path", path}});

    MeshData mesh;
    mesh.verts.reserve(n_vert * 3);
    mesh.tris.reserve(n_face * 3);

    auto emit_face = [&](const std::vector<int>& idx) {
        for (std::size_t j = 1; j + 1 < idx.size(); ++j) {  // fan-triangulate (matches trimesh)
            mesh.tris.push_back(static_cast<uint32_t>(idx[0]));
            mesh.tris.push_back(static_cast<uint32_t>(idx[j]));
            mesh.tris.push_back(static_cast<uint32_t>(idx[j + 1]));
        }
    };

    if (enc == ASCII) {
        for (std::size_t i = 0; i < n_vert; ++i) {
            if (!std::getline(is, line))
                throw OpError("import_failed", "PLY: truncated vertex block", {{"path", path}});
            std::istringstream ls(line);
            std::vector<double> vals;
            double d;
            while (ls >> d) vals.push_back(d);
            if (static_cast<int>(vals.size()) <= std::max({vx, vy, vz}))
                throw OpError("import_failed", "PLY: short vertex line", {{"path", path}});
            mesh.verts.push_back(vals[vx]);
            mesh.verts.push_back(vals[vy]);
            mesh.verts.push_back(vals[vz]);
        }
        for (std::size_t i = 0; i < n_face; ++i) {
            if (!std::getline(is, line))
                throw OpError("import_failed", "PLY: truncated face block", {{"path", path}});
            std::istringstream ls(line);
            int k = 0;
            ls >> k;
            std::vector<int> idx(k);
            for (int j = 0; j < k; ++j) ls >> idx[j];
            emit_face(idx);
        }
    } else {  // BIN_LE — slurp the body and walk it with the per-property layout.
        is.clear();
        is.seekg(static_cast<std::streamoff>(header_end), std::ios::beg);
        std::vector<unsigned char> buf((std::istreambuf_iterator<char>(is)),
                                       std::istreambuf_iterator<char>());
        const unsigned char* p = buf.data();
        const unsigned char* end = p + buf.size();
        for (std::size_t i = 0; i < n_vert; ++i) {
            double vxv = 0, vyv = 0, vzv = 0;
            for (std::size_t k = 0; k < vprops.size(); ++k) {
                if (p >= end) throw OpError("import_failed", "PLY: truncated binary vertex data",
                                            {{"path", path}});
                double val = ply::read_le_scalar(p, vprops[k].type);
                if (static_cast<int>(k) == vx) vxv = val;
                else if (static_cast<int>(k) == vy) vyv = val;
                else if (static_cast<int>(k) == vz) vzv = val;
            }
            mesh.verts.push_back(vxv);
            mesh.verts.push_back(vyv);
            mesh.verts.push_back(vzv);
        }
        for (std::size_t i = 0; i < n_face; ++i) {
            std::vector<int> idx;
            for (const ply::Prop& pr : fprops) {
                if (p >= end) throw OpError("import_failed", "PLY: truncated binary face data",
                                            {{"path", path}});
                if (pr.is_list) {
                    int k = static_cast<int>(ply::read_le_scalar(p, pr.count_type));
                    idx.resize(k);
                    for (int j = 0; j < k; ++j) idx[j] = static_cast<int>(
                        ply::read_le_scalar(p, pr.type));
                } else {
                    ply::read_le_scalar(p, pr.type);  // skip a scalar face prop
                }
            }
            emit_face(idx);
        }
    }
    return mesh;
}

// ---- glTF / GLB via OCCT's XCAF-backed reader/writer ------------------------
Handle(TDocStd_Document) new_xcaf_doc() {
    Handle(XCAFApp_Application) app = XCAFApp_Application::GetApplication();
    Handle(TDocStd_Document) doc;
    app->NewDocument("BinXCAF", doc);
    return doc;
}

void write_gltf(const std::vector<TopoDS_Shape>& bodies, const std::string& path, bool binary,
                const TessOptions& tess = TessOptions{}) {
    Handle(TDocStd_Document) doc = new_xcaf_doc();
    Handle(XCAFDoc_ShapeTool) tool = XCAFDoc_DocumentTool::ShapeTool(doc->Main());
    for (const TopoDS_Shape& s : bodies) {
        if (s.IsNull()) continue;
        mesh_shape(s, tess);  // glTF writer needs precomputed triangulation
        tool->AddShape(s, /*makeAssembly=*/Standard_False);
    }
    TColStd_IndexedDataMapOfStringString file_info;
    RWGltf_CafWriter writer(path.c_str(), binary);
    if (!writer.Perform(doc, file_info, Message_ProgressRange()))
        throw OpError("export_failed", "glTF writer returned failure", {{"path", path}});
}

MeshData read_gltf(const std::string& path) {
    RWGltf_CafReader reader;
    Handle(TDocStd_Document) doc = new_xcaf_doc();
    reader.SetDocument(doc);
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
// The mesh lane sews one planar FACE per triangle, so the display tessellator
// (core/geom/tessellate.cpp) would otherwise compute a single flat normal per triangle and
// a curved import renders as discrete facets. Authored per-vertex normals are NOT used to
// fix that: the SolidWorks saved-display normal arrays measured WRONG on real parts (a
// turned spacer carried nothing but (0,0,±1) on all 2208 vertices; on another part the
// authored sign disagreed with the triangle winding for 1040 of 1176 triangles), so they
// are no longer parsed at all. These normals come from the geometry instead.
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

}  // namespace

TopoDS_Shape mesh_to_shape(const MeshData& mesh) {
    if (mesh.triangle_count() == 0) return TopoDS_Shape();
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
    attach_mesh_smooth_normals(result, mesh, compute_mesh_smooth_normals(mesh));
    return result;
}

// --------------------------------------------------------------------------- export

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
            // sheet/shell surface body) is transferred as-is so surfaces still export.
            std::vector<TopoDS_Shape> units;
            for (const TopoDS_Shape& b : bodies) {
                if (b.IsNull()) continue;
                bool any_solid = false;
                for (TopExp_Explorer ex(b, TopAbs_SOLID); ex.More(); ex.Next()) {
                    units.push_back(ex.Current());
                    any_solid = true;
                }
                if (!any_solid) units.push_back(b);
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
            StlAPI_Writer w;
            w.ASCIIMode() = tess.ascii ? Standard_True : Standard_False;
            if (!w.Write(shape, out.c_str()))
                throw OpError("export_failed", "STL writer returned failure", {{"path", out}});
            return out;
        }
        if (fmt == "obj") {
            write_obj(shape_to_mesh(shape, tess), out);
            return out;
        }
        if (fmt == "ply") {
            write_ply(shape_to_mesh(shape, tess), out);
            return out;
        }
        if (fmt == "gltf") {
            write_gltf(bodies, out, /*binary=*/false, tess);
            return out;
        }
        if (fmt == "glb") {
            write_gltf(bodies, out, /*binary=*/true, tess);
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

// All solids in a shape, or the shape itself when it carries none (a single solid is
// often returned bare; a multi-body STEP returns a compound of solids).
std::vector<TopoDS_Shape> split_solids(const TopoDS_Shape& shape) {
    std::vector<TopoDS_Shape> solids;
    for (TopExp_Explorer ex(shape, TopAbs_SOLID); ex.More(); ex.Next())
        solids.push_back(ex.Current());
    if (solids.empty() && !shape.IsNull()) solids.push_back(shape);
    return solids;
}

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

std::vector<std::pair<std::string, TopoDS_Shape>> import_bodies(const std::string& path,
                                                                const std::string& fmt) {
    if (!std::filesystem::exists(path))
        throw OpError("import_failed", "import: file not found: " + path, {{"path", path}});
    try {
        if (fmt == "step") {
            STEPControl_Reader r;
            if (r.ReadFile(path.c_str()) != IFSelect_RetDone)
                throw OpError("import_failed", "STEP reader: ReadFile failed", {{"path", path}});
            r.TransferRoots();
            TopoDS_Shape shape = r.OneShape();
            if (shape.IsNull())
                throw OpError("import_failed", "STEP reader: no shape transferred", {{"path", path}});
            return name_bodies(split_solids(shape));
        }
        if (fmt == "iges") {
            IGESControl_Reader r;
            if (r.ReadFile(path.c_str()) != IFSelect_RetDone)
                throw OpError("import_failed", "IGES reader: ReadFile failed", {{"path", path}});
            r.TransferRoots();
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
            // Promote any closed shells to solids (same pattern as mesh_to_shape).
            // Without this step, IGES returns a surface shell with volume=0 even for
            // geometrically-closed bodies, because split_solids only finds TopAbs_SOLID.
            TopoDS_Shape promoted = fixed;
            for (TopExp_Explorer ex(fixed, TopAbs_SHELL); ex.More(); ex.Next()) {
                TopoDS_Shell shell = TopoDS::Shell(ex.Current());
                if (BRep_Tool::IsClosed(shell)) {
                    try {
                        BRepBuilderAPI_MakeSolid ms(shell);
                        if (ms.IsDone()) {
                            ShapeFix_Solid sfix;
                            TopoDS_Shape solid = sfix.SolidFromShell(shell);
                            promoted = solid.IsNull() ? TopoDS_Shape(ms.Solid()) : solid;
                        }
                    } catch (const Standard_Failure&) {
                        // leave as shell — truly open surfaces stay shells
                    }
                    break;  // single closed shell is the common IGES case
                }
            }
            return name_bodies(split_solids(promoted));
        }
        if (fmt == "brep") {
            TopoDS_Shape shape;
            BRep_Builder b;
            if (!BRepTools::Read(shape, path.c_str(), b) || shape.IsNull())
                throw OpError("import_failed", "BREP reader returned failure", {{"path", path}});
            return name_bodies(split_solids(shape));
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
                const auto solids = split_solids(placed);
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
            return name_bodies(split_solids(shape));
        }
        if (fmt == "3mf") {
#ifdef CHUDWARE_WITH_LIB3MF
            // 3MF via lib3mf: one body per mesh object (matching _import_3mf), each
            // sewn into a shell/solid (the same as_solid path as the other mesh
            // formats). Body names follow the oracle: the 3MF object name, else
            // "<stem>_<idx>". The raw vertex/triangle extraction is BIT-IDENTICAL to
            // the Python lib3mf path (both wrap lib3mf 2.5.0).
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
            // 2D lane: one Compound body (faces + free wires), no split_solids — a DXF
            // profile has no solids, and splitting would silently drop the free wires.
            TopoDS_Shape shape = read_dxf_shape(path);
            std::string stem = std::filesystem::path(path).stem().string();
            return {{stem.empty() ? std::string("dxf") : stem, shape}};
        }
        if (fmt == "dwg") {
            // DWG has no in-process reader here (proprietary binary) — run LibreDWG's
            // dwg2dxf with literal argv, then reuse the DXF lane above.
            std::string tmp_dxf = dwg_to_temp_dxf(path);
            TopoDS_Shape shape = read_dxf_shape(tmp_dxf);
            std::error_code ec;
            std::filesystem::remove(tmp_dxf, ec);
            std::string stem = std::filesystem::path(path).stem().string();
            return {{stem.empty() ? std::string("dwg") : stem, shape}};
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
