#include "geom/assembly_clearance.h"

#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCone.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Standard_Failure.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pln.hxx>
#include <gp_Vec.hxx>
#include <cmath>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace chudware::geom::clearance {
namespace {
using json = nlohmann::json;

void require(bool condition, const std::string& message) {
    if (!condition) throw std::invalid_argument(message);
}
double number(const json& value, const char* label) {
    require(value.is_number(), std::string(label) + " must be a number");
    const double n = value.get<double>();
    require(std::isfinite(n), std::string(label) + " must be finite");
    return n;
}
double positive(const json& value, const char* label) {
    double n = number(value, label);
    require(n > 0, std::string(label) + " must be positive");
    return n;
}
gp_Vec vector3(const json& value, const char* label) {
    require(value.is_array() && value.size() == 3, std::string(label) + " must contain three numbers");
    return gp_Vec(number(value[0], label), number(value[1], label), number(value[2], label));
}
gp_Pnt point3(const json& value, const char* label) {
    const gp_Vec v = vector3(value, label);
    return gp_Pnt(v.X(), v.Y(), v.Z());
}
gp_Dir direction(const json& value, const char* label) {
    const gp_Vec v = vector3(value, label);
    require(v.Magnitude() > 1e-12, std::string(label) + " must be nonzero");
    return gp_Dir(v);
}
std::set<std::string> ignored(const json& params, const std::vector<Member>& members) {
    std::set<std::string> result;
    if (!params.contains("ignore")) return result;
    require(params["ignore"].is_array() && params["ignore"].size() <= 256, "ignore must be an array of at most 256 IDs");
    for (const auto& item : params["ignore"]) {
        require(item.is_string(), "ignore must contain IDs");
        const auto id = item.get<std::string>();
        bool found = false;
        for (const auto& m : members) found |= m.id == id;
        require(found, "unknown ignored component: " + id);
        result.insert(id);
    }
    return result;
}
double threshold(const json& params) {
    double t = params.contains("min_overlap_mm3") ? number(params["min_overlap_mm3"], "min_overlap_mm3") : 1e-6;
    require(t >= 0, "min_overlap_mm3 must be nonnegative");
    return t;
}
bool has_solid(const TopoDS_Shape& shape) {
    return !shape.IsNull() && TopExp_Explorer(shape, TopAbs_SOLID).More();
}
double volume(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return 0;
    GProp_GProps props;
    BRepGProp::VolumeProperties(shape, props);
    const double result = std::abs(props.Mass());
    if (!std::isfinite(result)) throw std::runtime_error("volume_failed");
    return result;
}
TopoDS_Shape fuse(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    BRepAlgoAPI_Fuse op(a, b);
    op.Build();
    if (!op.IsDone() || op.Shape().IsNull()) throw std::runtime_error("sweep_fuse_failed");
    if (!BRepCheck_Analyzer(op.Shape()).IsValid()) throw std::runtime_error("sweep_fuse_invalid");
    return op.Shape();
}
double overlap(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    BRepAlgoAPI_Common op(a, b);
    op.Build();
    if (!op.IsDone() || op.Shape().IsNull()) throw std::runtime_error("common_failed");
    return volume(op.Shape());
}
// The exact sweep is the original solid union every boundary-face prism.
// Only planar faces exactly parallel to the displacement have zero swept
// volume; curved faces must not be filtered using sampled normals.
TopoDS_Shape sweep(const TopoDS_Shape& shape, const gp_Vec& displacement) {
    if (!has_solid(shape)) throw std::runtime_error("missing_solid");
    TopoDS_Shape result = shape;
    for (TopExp_Explorer it(shape, TopAbs_FACE); it.More(); it.Next()) {
        const TopoDS_Face face = TopoDS::Face(it.Current());
        BRepAdaptor_Surface surface(face);
        if (surface.GetType() == GeomAbs_Plane &&
            gp_Vec(surface.Plane().Axis().Direction()).Dot(displacement) == 0.0) continue;
        BRepPrimAPI_MakePrism prism(face, displacement, Standard_False, Standard_True);
        if (!prism.IsDone() || prism.Shape().IsNull()) throw std::runtime_error("face_prism_failed");
        const TopoDS_Shape piece = prism.Shape();
        if (!has_solid(piece)) throw std::runtime_error("face_prism_not_solid");
        if (!BRepCheck_Analyzer(piece).IsValid()) throw std::runtime_error("face_prism_invalid");
        result = fuse(result, piece);
    }
    return result;
}
std::string error_message(const Standard_Failure& e) {
    return e.GetMessageString() ? e.GetMessageString() : "kernel_failure";
}
const Member& find_member(const std::vector<Member>& members, const std::string& id) {
    for (const auto& m : members) if (m.id == id) return m;
    throw std::invalid_argument("unknown component: " + id);
}
TopoDS_Shape make_envelope(const json& spec) {
    require(spec.is_object(), "envelope must be an object");
    const int types = int(spec.contains("box")) + int(spec.contains("cylinder")) + int(spec.contains("frustum"));
    require(types == 1, "envelope must have exactly one of box, cylinder or frustum");
    if (spec.contains("box")) {
        const auto& b = spec.at("box");
        require(b.is_object() && b.contains("min") && b.contains("max"), "box requires min and max");
        const gp_Pnt lo = point3(b.at("min"), "min"), hi = point3(b.at("max"), "max");
        require(hi.X() > lo.X() && hi.Y() > lo.Y() && hi.Z() > lo.Z(), "box max must exceed min on each axis");
        return BRepPrimAPI_MakeBox(lo, hi).Shape();
    }
    const bool cylinder = spec.contains("cylinder");
    const auto& s = spec.at(cylinder ? "cylinder" : "frustum");
    require(s.is_object() && s.contains("origin") && s.contains("axis") && s.contains("length"), "envelope requires origin, axis and length");
    const gp_Ax2 ax(point3(s.at("origin"), "origin"), direction(s.at("axis"), "axis"));
    const double length = positive(s.at("length"), "length");
    if (cylinder) {
        require(s.contains("radius"), "cylinder requires radius");
        return BRepPrimAPI_MakeCylinder(ax, positive(s.at("radius"), "radius"), length).Shape();
    }
    require(s.contains("r0") && s.contains("r1"), "frustum requires r0 and r1");
    const double r0 = number(s.at("r0"), "r0"), r1 = number(s.at("r1"), "r1");
    require(r0 >= 0 && r1 >= 0 && (r0 > 0 || r1 > 0), "frustum radii must be nonnegative and not both zero");
    if (r0 == r1) return BRepPrimAPI_MakeCylinder(ax, r0, length).Shape();
    return BRepPrimAPI_MakeCone(ax, r0, r1, length).Shape();
}
}  // namespace

json removal_report(const std::vector<Member>& members, const json& params) {
    require(params.is_object(), "params must be an object");
    require(members.size() <= 256, "at most 256 members supported");
    require(params.contains("queries") && params["queries"].is_array() && !params["queries"].empty() && params["queries"].size() <= 32, "queries must contain 1 to 32 entries");
    const auto ignore = ignored(params, members);
    const double limit = threshold(params);
    require(!params.contains("stop_at_first_hit") || params["stop_at_first_hit"].is_boolean(), "stop_at_first_hit must be boolean");
    const bool stop = params.value("stop_at_first_hit", true);
    json results = json::array();
    bool complete = true;
    for (const auto& q : params["queries"]) {
        require(q.is_object() && q.contains("component") && q["component"].is_string(), "query requires component ID");
        const auto& moving = find_member(members, q["component"].get<std::string>());
        require(q.contains("directions") && q["directions"].is_array() && !q["directions"].empty() && q["directions"].size() <= 16, "directions must contain 1 to 16 vectors");
        require(q.contains("distance_mm") || params.contains("distance_mm"), "distance_mm is required");
        const double distance = positive(q.contains("distance_mm") ? q["distance_mm"] : params["distance_mm"], "distance_mm");
        for (const auto& dir_json : q["directions"]) {
            const gp_Dir dir = direction(dir_json, "direction");
            json row = {{"component", moving.id}, {"name", moving.name}, {"direction", {dir.X(), dir.Y(), dir.Z()}}, {"distance_mm", distance}, {"blockers", json::array()}, {"failed", json::array()}};
            TopoDS_Shape swept;
            try { swept = sweep(moving.shape, gp_Vec(dir) * distance); }
            catch (const Standard_Failure& e) { row["failed"].push_back({{"component", moving.id}, {"reason", error_message(e)}}); }
            catch (const std::exception& e) { row["failed"].push_back({{"component", moving.id}, {"reason", e.what()}}); }
            if (!swept.IsNull()) for (const auto& other : members) {
                if (other.id == moving.id || ignore.count(other.id)) continue;
                try {
                    if (!has_solid(other.shape)) throw std::runtime_error("missing_solid");
                    const double v = overlap(swept, other.shape);
                    if (v > limit) {
                        row["blockers"].push_back({{"component", other.id}, {"name", other.name}, {"overlap_mm3", v}});
                        if (stop) { row["stopped_at_first_hit"] = true; break; }
                    }
                } catch (const Standard_Failure& e) { row["failed"].push_back({{"component", other.id}, {"reason", error_message(e)}}); }
                  catch (const std::exception& e) { row["failed"].push_back({{"component", other.id}, {"reason", e.what()}}); }
            }
            if (!row["failed"].empty()) complete = false;
            if (!row["blockers"].empty()) row["clear"] = false;
            else if (!row["failed"].empty()) row["clear"] = nullptr;
            else row["clear"] = true;
            row["status"] = row["failed"].empty() ? "evaluated" : "not_evaluated";
            results.push_back(std::move(row));
        }
    }
    return {{"ok", complete}, {"complete", complete}, {"min_overlap_mm3", limit},
            {"assumptions", {"Translation only; initial overlap counts as obstruction.",
                             "Only overlap volume above the threshold is a hit."}},
            {"results", std::move(results)}};
}

json envelope_report(const std::vector<Member>& members, const json& params) {
    require(params.is_object(), "params must be an object");
    require(members.size() <= 256, "at most 256 members supported");
    require(params.contains("solids") && params["solids"].is_array() && !params["solids"].empty() && params["solids"].size() <= 64, "solids must contain 1 to 64 envelopes");
    const auto ignore = ignored(params, members);
    const double limit = threshold(params);
    json results = json::array();
    bool complete = true;
    std::size_t index = 0;
    for (const auto& spec : params["solids"]) {
        require(spec.is_object(), "envelope must be an object");
        require(!spec.contains("id") || spec["id"].is_string(), "envelope id must be a string");
        json row = {{"id", spec.value("id", std::to_string(index++))}, {"hits", json::array()}, {"failed", json::array()}};
        TopoDS_Shape envelope;
        try { envelope = make_envelope(spec); }
        catch (const Standard_Failure& e) { row["failed"].push_back({{"reason", error_message(e)}}); }
        if (envelope.IsNull() && row["failed"].empty()) row["failed"].push_back({{"reason", "envelope_construction_failed"}});
        if (!envelope.IsNull()) for (const auto& member : members) {
            if (ignore.count(member.id)) continue;
            try {
                if (!has_solid(member.shape)) throw std::runtime_error("missing_solid");
                const double v = overlap(envelope, member.shape);
                if (v > limit) row["hits"].push_back({{"component", member.id}, {"name", member.name}, {"overlap_mm3", v}});
            } catch (const Standard_Failure& e) { row["failed"].push_back({{"component", member.id}, {"reason", error_message(e)}}); }
              catch (const std::exception& e) { row["failed"].push_back({{"component", member.id}, {"reason", e.what()}}); }
        }
        if (!row["failed"].empty()) complete = false;
        row["status"] = row["failed"].empty() ? "evaluated" : "not_evaluated";
        results.push_back(std::move(row));
    }
    return {{"ok", complete}, {"complete", complete}, {"min_overlap_mm3", limit},
            {"assumptions", {"Only overlap volume above the threshold is a hit."}},
            {"results", std::move(results)}};
}
}  // namespace chudware::geom::clearance
