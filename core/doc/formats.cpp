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
// bit-identical). Guarded by AICAD_WITH_LIB3MF so a build without the SDK
// provisioned still compiles (the 3MF paths then fall through to the loud "not
// supported" errors below).
//
// IMPORTANT: <windows.h> must be included FULL (no WIN32_LEAN_AND_MEAN) and
// BEFORE the OCCT headers, because (1) the binding calls MultiByteToWideChar /
// CP_UTF8 (winnls.h), which the lean macro strips, and (2) OCCT headers define
// WIN32_LEAN_AND_MEAN, which would poison a later include. Pulling it in first
// guarantees the full Win32 surface for the binding.
#ifdef AICAD_WITH_LIB3MF
#ifdef _WIN32
// NOMINMAX: windows.h otherwise defines min()/max() macros that collide with
// std::max({...}) in the PLY reader below.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
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
// AICAD_WITH_OPENNURBS. opennurbs.h is the single umbrella header; it pulls in
// <windows.h> itself on Win32. NOMINMAX (so its includes don't redefine
// min/max) is set above when lib3mf is enabled; set it here too for an
// openNURBS-only build. The static lib (opennurbsStatic) carries the bundled
// zlib + Win32 system libs. We read the STORED ON_Mesh (m_V float32 / m_F
// quad/tri) — the SAME data the rhino3dm wheel exposes — so extraction is
// bit-identical to the Python oracle.
#ifdef AICAD_WITH_OPENNURBS
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

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <unordered_map>

#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeSolid.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Sewing.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <BRepTools.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <IGESControl_Reader.hxx>
#include <IGESControl_Writer.hxx>
#include <Message_ProgressRange.hxx>
#include <Poly_Triangulation.hxx>
#include <RWGltf_CafReader.hxx>
#include <RWGltf_CafWriter.hxx>
#include <RWObj.hxx>
#include <RWStl.hxx>
#include <STEPControl_Reader.hxx>
#include <STEPControl_Writer.hxx>
#include <ShapeFix_Shape.hxx>
#include <ShapeFix_Solid.hxx>
#include <StlAPI_Writer.hxx>
#include <TColStd_IndexedDataMapOfStringString.hxx>
#include <TDF_LabelSequence.hxx>
#include <TDocStd_Document.hxx>
#include <TopAbs.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
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
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

#include "doc/session.h"  // OpError

namespace aicad::doc {

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

#ifdef AICAD_WITH_LIB3MF
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
// pass the DLL beside THIS module (libaicad.dll) so the staged copy is found
// regardless of the process CWD. Falls back to the bare name (PATH search).
Lib3MF::PWrapper load_lib3mf() {
    std::string dll_path = "lib3mf.dll";
#ifdef _WIN32
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
#endif
    try {
        return Lib3MF::CWrapper::loadLibrary(dll_path);
    } catch (const std::exception& e) {
        throw OpError("import_failed",
                      std::string("3MF: could not load lib3mf.dll (") + e.what() + ")",
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
#endif  // AICAD_WITH_LIB3MF

#ifdef AICAD_WITH_OPENNURBS
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
#endif  // AICAD_WITH_OPENNURBS

}  // namespace

std::string detect_format(const std::string& path) {
    std::string ext = lower(std::filesystem::path(path).extension().string());
    static const std::pair<const char*, const char*> kMap[] = {
        {".step", "step"}, {".stp", "step"}, {".igs", "iges"}, {".iges", "iges"},
        {".brep", "brep"}, {".stl", "stl"},  {".3mf", "3mf"},  {".obj", "obj"},
        {".ply", "ply"},   {".gltf", "gltf"},{".glb", "glb"},  {".3dm", "3dm"},
        {".sldprt", "sldprt"}, {".sldasm", "sldasm"}, {".x_t", "x_t"}, {".x_b", "x_b"},
    };
    for (const auto& [e, f] : kMap)
        if (ext == e) return f;
    throw OpError("invalid_op", "cannot detect format from extension '" + ext + "'",
                  {{"reason", "unknown extension"}, {"path", path}});
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
    os << "# aicad OBJ export\n";
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
    os << "ply\nformat ascii 1.0\ncomment aicad PLY export\n";
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
#ifdef AICAD_WITH_LIB3MF
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
                      "(-DAICAD_WITH_OCCT=ON pulls in ProvisionLib3mf).",
                      {{"format", fmt}, {"path", path}});
#endif
    }
    if (fmt == "3dm") {
#ifdef AICAD_WITH_OPENNURBS
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
                      "(-DAICAD_WITH_OCCT=ON pulls in ProvisionOpenNURBS).",
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
#ifdef AICAD_WITH_LIB3MF
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
#ifdef AICAD_WITH_OPENNURBS
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
        if (BRep_Tool::IsClosed(shell)) {
            try {
                BRepBuilderAPI_MakeSolid ms(shell);
                if (ms.IsDone()) {
                    ShapeFix_Solid fix;
                    TopoDS_Shape solid = fix.SolidFromShell(shell);
                    result = solid.IsNull() ? TopoDS_Shape(ms.Solid()) : solid;
                }
            } catch (const Standard_Failure&) {
                result = sewn;
            }
            break;  // single closed shell is the common case
        }
    }
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
            if (w.Transfer(shape, STEPControl_AsIs) != IFSelect_RetDone)
                throw OpError("export_failed", "STEP writer: transfer failed", {{"format", fmt}});
            if (w.Write(out.c_str()) != IFSelect_RetDone)
                throw OpError("export_failed", "STEP writer: write failed", {{"path", out}});
            return out;
        }
        if (fmt == "iges") {
            IGESControl_Writer w;
            w.AddShape(shape);
            w.ComputeModel();
            if (!w.Write(out.c_str()))
                throw OpError("export_failed", "IGES writer returned failure", {{"path", out}});
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
#ifdef AICAD_WITH_LIB3MF
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
                          "(-DAICAD_WITH_OCCT=ON pulls in ProvisionLib3mf).",
                          {{"format", fmt}, {"reason", "lib3mf not provisioned"}});
#endif
        }
        if (fmt == "3dm") {
#ifdef AICAD_WITH_OPENNURBS
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
                          "(-DAICAD_WITH_OCCT=ON pulls in ProvisionOpenNURBS).",
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
            return name_bodies(split_solids(fixed.IsNull() ? raw : fixed));
        }
        if (fmt == "brep") {
            TopoDS_Shape shape;
            BRep_Builder b;
            if (!BRepTools::Read(shape, path.c_str(), b) || shape.IsNull())
                throw OpError("import_failed", "BREP reader returned failure", {{"path", path}});
            return name_bodies(split_solids(shape));
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
#ifdef AICAD_WITH_LIB3MF
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
                          "(-DAICAD_WITH_OCCT=ON pulls in ProvisionLib3mf).",
                          {{"format", fmt}, {"reason", "lib3mf not provisioned"}});
#endif
        }
        if (fmt == "3dm") {
#ifdef AICAD_WITH_OPENNURBS
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
                          "(-DAICAD_WITH_OCCT=ON pulls in ProvisionOpenNURBS).",
                          {{"format", fmt}, {"reason", "openNURBS not provisioned"}});
#endif
        }
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& e) {
        throw OpError("import_failed", std::string("import of ") + fmt + " threw: " + e.what(),
                      {{"format", fmt}, {"path", path}});
    }
    throw OpError("import_failed",
                  "import format '" + fmt + "' is not supported by the C++ core "
                  "(sldprt/sldasm/x_t/x_b need the COM/Parasolid lanes — deferred)",
                  {{"format", fmt}, {"reason", "unsupported format"}});
}

}  // namespace aicad::doc
