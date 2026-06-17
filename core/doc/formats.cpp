// File import/export implementation — see formats.h.
//
// Ports the neutral-B-rep lanes of engine/doc/formats.py onto OCCT's data-exchange
// toolkits. The Python oracle reaches the SAME OCCT readers/writers through OCP /
// build123d, so this is a direct API translation; the parity gate re-imports a
// C++-written file through the Python oracle and compares body count + volume (never a
// byte compare — OCCT 7.9.3 output differs from the oracle's 7.8.x).

#include "doc/formats.h"

#include <algorithm>
#include <filesystem>

#include <BRepBuilderAPI_Sewing.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRep_Builder.hxx>
#include <BRepTools.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <IGESControl_Reader.hxx>
#include <IGESControl_Writer.hxx>
#include <STEPControl_Reader.hxx>
#include <STEPControl_Writer.hxx>
#include <ShapeFix_Shape.hxx>
#include <StlAPI_Writer.hxx>
#include <TopAbs.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>

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

std::string export_bodies(const std::vector<TopoDS_Shape>& bodies, const std::string& path,
                          const std::string& fmt) {
    if (bodies.empty())
        throw OpError("export_failed", "export: no bodies to write", {{"format", fmt}});
    const std::string out = abs_path(path);
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
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& e) {
        throw OpError("export_failed", std::string("export to ") + fmt + " threw: " + e.what(),
                      {{"format", fmt}, {"path", out}});
    }
    throw OpError("export_failed",
                  "export format '" + fmt + "' is not yet supported by the C++ core "
                  "(neutral B-rep + STL only; obj/ply/gltf/3mf/3dm/COM pending)",
                  {{"format", fmt}, {"reason", "unsupported format"}});
}

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
    } catch (const OpError&) {
        throw;
    } catch (const std::exception& e) {
        throw OpError("import_failed", std::string("import of ") + fmt + " threw: " + e.what(),
                      {{"format", fmt}, {"path", path}});
    }
    throw OpError("import_failed",
                  "import format '" + fmt + "' is not yet supported by the C++ core "
                  "(neutral B-rep only; mesh/3mf/3dm/COM pending)",
                  {{"format", fmt}, {"reason", "unsupported format"}});
}

}  // namespace aicad::doc
