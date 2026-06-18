// File import/export implementation — see formats.h.
//
// Ports the neutral-B-rep AND the mesh lanes of engine/doc/formats.py onto OCCT's
// data-exchange + mesh toolkits. The Python oracle reaches the same readers/writers for
// the B-rep formats (OCP/build123d) and uses trimesh/lib3mf/rhino3dm for the mesh
// formats; this C++ side reaches OCCT's RWStl / RWObj / RWGltf instead. The parity gate
// re-imports a C++-written file through the Python oracle (trimesh) and compares the
// ROBUST mesh invariant (vertex/triangle count + bbox + volume) — never a byte compare.

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

// Tessellate a shape in place at the Python defaults (tessellate.py: 0.05mm linear,
// 0.35rad angular) so a mesh writer has a triangulation to emit.
void mesh_shape(const TopoDS_Shape& shape) {
    BRepMesh_IncrementalMesh m(shape, 0.05, Standard_False, 0.35, Standard_True);
    m.Perform();
}

std::string abs_path(const std::string& path) {
    std::error_code ec;
    std::filesystem::path p = std::filesystem::absolute(path, ec);
    return ec ? path : p.string();
}

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

// --------------------------------------------------------------------------- mesh soup

MeshData shape_to_mesh(const TopoDS_Shape& shape) {
    MeshData mesh;
    if (shape.IsNull()) return mesh;
    mesh_shape(shape);
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

int type_size(const std::string& t) {
    if (t == "char" || t == "uchar" || t == "int8" || t == "uint8") return 1;
    if (t == "short" || t == "ushort" || t == "int16" || t == "uint16") return 2;
    if (t == "int" || t == "uint" || t == "int32" || t == "uint32" || t == "float" ||
        t == "float32")
        return 4;
    if (t == "double" || t == "float64" || t == "int64" || t == "uint64") return 8;
    return 0;  // unknown
}

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

void write_gltf(const std::vector<TopoDS_Shape>& bodies, const std::string& path, bool binary) {
    Handle(TDocStd_Document) doc = new_xcaf_doc();
    Handle(XCAFDoc_ShapeTool) tool = XCAFDoc_DocumentTool::ShapeTool(doc->Main());
    for (const TopoDS_Shape& s : bodies) {
        if (s.IsNull()) continue;
        mesh_shape(s);  // glTF writer needs precomputed triangulation
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
    throw OpError("import_failed", "read_mesh: unsupported mesh format '" + fmt + "'",
                  {{"format", fmt}, {"path", path}});
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
                          const std::string& fmt) {
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
            mesh_shape(shape);
            StlAPI_Writer w;
            if (!w.Write(shape, out.c_str()))
                throw OpError("export_failed", "STL writer returned failure", {{"path", out}});
            return out;
        }
        if (fmt == "obj") {
            write_obj(shape_to_mesh(shape), out);
            return out;
        }
        if (fmt == "ply") {
            write_ply(shape_to_mesh(shape), out);
            return out;
        }
        if (fmt == "gltf") {
            write_gltf(bodies, out, /*binary=*/false);
            return out;
        }
        if (fmt == "glb") {
            write_gltf(bodies, out, /*binary=*/true);
            return out;
        }
        if (fmt == "3mf")
            throw OpError("export_failed",
                          "3MF export is not supported by the C++ core: OCCT 7.9.3 ships no "
                          "3MF writer; the Python oracle uses lib3mf. Export STL/OBJ/PLY/glTF "
                          "instead.",
                          {{"format", fmt}, {"reason", "no OCCT 3MF support"}});
        if (fmt == "3dm")
            throw OpError("export_failed",
                          "3DM (Rhino) export is not supported by the C++ core: OCCT 7.9.3 ships "
                          "no OpenNURBS writer; the Python oracle uses rhino3dm. Export STEP/STL "
                          "instead.",
                          {{"format", fmt}, {"reason", "no OCCT 3dm support"}});
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
        if (fmt == "3mf")
            throw OpError("import_failed",
                          "3MF import is not supported by the C++ core: OCCT 7.9.3 ships no "
                          "3MF reader; the Python oracle uses lib3mf.",
                          {{"format", fmt}, {"reason", "no OCCT 3MF support"}});
        if (fmt == "3dm")
            throw OpError("import_failed",
                          "3DM (Rhino) import is not supported by the C++ core: OCCT 7.9.3 ships "
                          "no OpenNURBS reader; the Python oracle uses rhino3dm.",
                          {{"format", fmt}, {"reason", "no OCCT 3dm support"}});
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
