// core/cam/model.cpp — CAM data-model serialization + enum tables.
#include "cam/model.h"

#include <algorithm>
#include <cstring>
#include <unordered_map>

namespace aicad {
namespace cam {

// ─────────────────────────────────────────────────────── enum string tables ──
namespace {
template <typename E>
const char* lookup(const std::vector<std::pair<E, const char*>>& tbl, E v) {
    for (auto& e : tbl) if (e.first == v) return e.second;
    return tbl.empty() ? "" : tbl.front().second;
}
template <typename E>
E lookup_rev(const std::vector<std::pair<E, const char*>>& tbl, const std::string& s, E dflt) {
    for (auto& e : tbl) if (s == e.second) return e.first;
    return dflt;
}

const std::vector<std::pair<StockType, const char*>> kStock = {
    {StockType::BoxOffset, "box_offset"}, {StockType::Box, "box"},
    {StockType::CylinderOffset, "cylinder_offset"}, {StockType::Cylinder, "cylinder"},
    {StockType::FromSolid, "from_solid"}};

const std::vector<std::pair<MachineKind, const char*>> kMachine = {
    {MachineKind::Mill3, "mill3"}, {MachineKind::Mill4, "mill4"}, {MachineKind::Mill5, "mill5"},
    {MachineKind::Router, "router"}, {MachineKind::Lathe, "lathe"},
    {MachineKind::MillTurn, "millturn"}, {MachineKind::Laser, "laser"},
    {MachineKind::Plasma, "plasma"}, {MachineKind::Waterjet, "waterjet"}};

const std::vector<std::pair<ToolType, const char*>> kTool = {
    {ToolType::EndmillFlat, "endmill_flat"}, {ToolType::EndmillBall, "endmill_ball"},
    {ToolType::EndmillBull, "endmill_bull"}, {ToolType::ChamferMill, "chamfer_mill"},
    {ToolType::FaceMill, "face_mill"}, {ToolType::VBit, "vbit"}, {ToolType::SlotMill, "slot_mill"},
    {ToolType::DovetailMill, "dovetail_mill"}, {ToolType::ThreadMill, "thread_mill"},
    {ToolType::Drill, "drill"}, {ToolType::SpotDrill, "spot_drill"},
    {ToolType::CenterDrill, "center_drill"}, {ToolType::Tap, "tap"}, {ToolType::Reamer, "reamer"},
    {ToolType::BoringBar, "boring_bar"}, {ToolType::TurningGeneral, "turning_general"},
    {ToolType::GroovingTool, "grooving_tool"}, {ToolType::ThreadingTool, "threading_tool"},
    {ToolType::PartingTool, "parting_tool"}, {ToolType::BoringBarID, "boring_bar_id"},
    {ToolType::LaserHead, "laser_head"}, {ToolType::PlasmaTorch, "plasma_torch"},
    {ToolType::WaterjetHead, "waterjet_head"}, {ToolType::Probe, "probe"}};

const std::vector<std::pair<OpType, const char*>> kOp = {
    {OpType::Face, "face"}, {OpType::Contour2D, "contour2d"}, {OpType::Pocket2D, "pocket2d"},
    {OpType::Slot, "slot"}, {OpType::Adaptive2D, "adaptive2d"}, {OpType::Engrave, "engrave"},
    {OpType::Chamfer2D, "chamfer2d"}, {OpType::Trace, "trace"}, {OpType::Bore2D, "bore2d"},
    {OpType::Drill, "drill"}, {OpType::ThreadMill, "thread_mill"},
    {OpType::AdaptiveClear3D, "adaptive_clear3d"}, {OpType::Pocket3D, "pocket3d"},
    {OpType::Parallel3D, "parallel3d"}, {OpType::Contour3D, "contour3d"},
    {OpType::Scallop3D, "scallop3d"}, {OpType::Pencil3D, "pencil3d"},
    {OpType::Radial3D, "radial3d"}, {OpType::SpiralFinish3D, "spiral_finish3d"},
    {OpType::TurnFace, "turn_face"}, {OpType::TurnRough, "turn_rough"},
    {OpType::TurnFinish, "turn_finish"}, {OpType::TurnGroove, "turn_groove"},
    {OpType::TurnThread, "turn_thread"}, {OpType::TurnPart, "turn_part"},
    {OpType::TurnDrill, "turn_drill"}, {OpType::Cut2D, "cut2d"}, {OpType::Probe, "probe"}};

const std::vector<std::pair<MoveKind, const char*>> kMove = {
    {MoveKind::Rapid, "rapid"}, {MoveKind::Lead, "lead"}, {MoveKind::Cut, "cut"},
    {MoveKind::Plunge, "plunge"}, {MoveKind::Arc, "arc"}, {MoveKind::Retract, "retract"},
    {MoveKind::Dwell, "dwell"}, {MoveKind::ToolChange, "tool_change"}};

const std::vector<std::pair<LeadType, const char*>> kLead = {
    {LeadType::None, "none"}, {LeadType::Line, "line"}, {LeadType::Arc, "arc"},
    {LeadType::Tangent, "tangent"}};

const std::vector<std::pair<RampType, const char*>> kRamp = {
    {RampType::Plunge, "plunge"}, {RampType::ZigZag, "zigzag"}, {RampType::Helix, "helix"},
    {RampType::Profile, "profile"}};

const std::vector<std::pair<CutPattern, const char*>> kPattern = {
    {CutPattern::Zigzag, "zigzag"}, {CutPattern::OneWay, "oneway"}, {CutPattern::Spiral, "spiral"},
    {CutPattern::Concentric, "concentric"}, {CutPattern::MorphedSpiral, "morphed_spiral"},
    {CutPattern::Radial, "radial"}};

const std::vector<std::pair<CutDirection, const char*>> kDir = {
    {CutDirection::Climb, "climb"}, {CutDirection::Conventional, "conventional"}};

const std::vector<std::pair<CompSide, const char*>> kComp = {
    {CompSide::On, "on"}, {CompSide::Left, "left"}, {CompSide::Right, "right"}};
}  // namespace

const char* to_string(StockType v) { return lookup(kStock, v); }
const char* to_string(MachineKind v) { return lookup(kMachine, v); }
const char* to_string(ToolType v) { return lookup(kTool, v); }
const char* to_string(OpType v) { return lookup(kOp, v); }
const char* to_string(MoveKind v) { return lookup(kMove, v); }
const char* to_string(LeadType v) { return lookup(kLead, v); }
const char* to_string(RampType v) { return lookup(kRamp, v); }
const char* to_string(CutPattern v) { return lookup(kPattern, v); }
const char* to_string(CutDirection v) { return lookup(kDir, v); }
const char* to_string(CompSide v) { return lookup(kComp, v); }

StockType stock_type_from_string(const std::string& s) { return lookup_rev(kStock, s, StockType::BoxOffset); }
MachineKind machine_kind_from_string(const std::string& s) { return lookup_rev(kMachine, s, MachineKind::Mill3); }
ToolType tool_type_from_string(const std::string& s) { return lookup_rev(kTool, s, ToolType::EndmillFlat); }
OpType op_type_from_string(const std::string& s) { return lookup_rev(kOp, s, OpType::Contour2D); }
MoveKind move_kind_from_string(const std::string& s) { return lookup_rev(kMove, s, MoveKind::Cut); }
LeadType lead_type_from_string(const std::string& s) { return lookup_rev(kLead, s, LeadType::Arc); }
RampType ramp_type_from_string(const std::string& s) { return lookup_rev(kRamp, s, RampType::Helix); }
CutPattern cut_pattern_from_string(const std::string& s) { return lookup_rev(kPattern, s, CutPattern::Zigzag); }
CutDirection cut_direction_from_string(const std::string& s) { return lookup_rev(kDir, s, CutDirection::Climb); }
CompSide comp_side_from_string(const std::string& s) { return lookup_rev(kComp, s, CompSide::On); }

std::string tool_class(ToolType t) {
    switch (t) {
        case ToolType::EndmillBall: return "ballmill";
        case ToolType::EndmillBull: return "bullmill";
        case ToolType::FaceMill: case ToolType::SlotMill: return "face";
        case ToolType::ChamferMill: case ToolType::VBit: case ToolType::DovetailMill: return "chamfer";
        case ToolType::ThreadMill: return "thread";
        case ToolType::Drill: case ToolType::SpotDrill: case ToolType::CenterDrill:
        case ToolType::Reamer: case ToolType::BoringBar: return "drill";
        case ToolType::Tap: return "tap";
        case ToolType::TurningGeneral: case ToolType::GroovingTool: case ToolType::ThreadingTool:
        case ToolType::PartingTool: case ToolType::BoringBarID: return "turning";
        case ToolType::LaserHead: return "laser";
        case ToolType::PlasmaTorch: return "plasma";
        case ToolType::WaterjetHead: return "waterjet";
        case ToolType::Probe: return "probe";
        default: return "endmill";
    }
}

// ───────────────────────────────────────────────────────── geometry helpers ──
static double num(const json& j, double d = 0) { return j.is_number() ? j.get<double>() : d; }

Vec2 vec2_from_json(const json& j) {
    if (j.is_array() && j.size() >= 2) return {num(j[0]), num(j[1])};
    if (j.is_object()) return {num(j.value("x", json(0.0))), num(j.value("y", json(0.0)))};
    return {};
}
Vec3 vec3_from_json(const json& j) {
    if (j.is_array() && j.size() >= 3) return {num(j[0]), num(j[1]), num(j[2])};
    if (j.is_array() && j.size() == 2) return {num(j[0]), num(j[1]), 0};
    if (j.is_object())
        return {num(j.value("x", json(0.0))), num(j.value("y", json(0.0))), num(j.value("z", json(0.0)))};
    return {};
}
json to_json(const Vec2& v) { return json::array({v.x, v.y}); }
json to_json(const Vec3& v) { return json::array({v.x, v.y, v.z}); }

Path2 path2_from_json(const json& j) {
    Path2 p;
    if (j.is_array()) for (auto& e : j) p.push_back(vec2_from_json(e));
    return p;
}
json to_json(const Path2& p) {
    json a = json::array();
    for (auto& v : p) a.push_back(to_json(v));
    return a;
}
Region region_from_json(const json& j) {
    Region r;
    if (j.is_object()) {
        r.outer = path2_from_json(j.value("outer", json::array()));
        if (j.contains("holes")) for (auto& h : j["holes"]) r.holes.push_back(path2_from_json(h));
    } else if (j.is_array()) {
        r.outer = path2_from_json(j);  // bare polygon = region with no holes
    }
    return r;
}
json to_json(const Region& r) {
    json holes = json::array();
    for (auto& h : r.holes) holes.push_back(to_json(h));
    return json{{"outer", to_json(r.outer)}, {"holes", holes}};
}
TriMesh trimesh_from_json(const json& j) {
    TriMesh m;
    if (!j.is_object()) return m;
    // verts: flat [x,y,z,...] or [[x,y,z],...]
    if (j.contains("verts")) {
        const json& v = j["verts"];
        if (v.is_array() && !v.empty() && v[0].is_number()) {
            for (size_t i = 0; i + 2 < v.size(); i += 3) m.verts.push_back({num(v[i]), num(v[i + 1]), num(v[i + 2])});
        } else if (v.is_array()) {
            for (auto& e : v) m.verts.push_back(vec3_from_json(e));
        }
    }
    if (j.contains("tris")) {
        const json& t = j["tris"];
        if (t.is_array() && !t.empty() && t[0].is_number()) {
            for (size_t i = 0; i + 2 < t.size(); i += 3)
                m.tris.push_back({(int)t[i].get<int>(), (int)t[i + 1].get<int>(), (int)t[i + 2].get<int>()});
        } else if (t.is_array()) {
            for (auto& e : t)
                if (e.is_array() && e.size() >= 3) m.tris.push_back({e[0].get<int>(), e[1].get<int>(), e[2].get<int>()});
        }
    }
    return m;
}
json to_json(const TriMesh& m) {
    json v = json::array(), t = json::array();
    for (auto& p : m.verts) { v.push_back(p.x); v.push_back(p.y); v.push_back(p.z); }
    for (auto& tr : m.tris) { t.push_back(tr[0]); t.push_back(tr[1]); t.push_back(tr[2]); }
    return json{{"verts", v}, {"tris", t}};
}

// ────────────────────────────────────────────────────────────────── Stock ──
json Stock::to_json() const {
    json j{{"type", cam::to_string(type)}, {"min", cam::to_json(min)}, {"max", cam::to_json(max)},
           {"off_xy", off_xy}, {"off_ztop", off_ztop}, {"off_zbot", off_zbot},
           {"base", cam::to_json(base)}, {"radius", radius}, {"height", height},
           {"off_radial", off_radial}, {"off_axial", off_axial}, {"material", material}};
    if (!solid.verts.empty()) j["solid"] = cam::to_json(solid);
    return j;
}
Stock Stock::from_json(const json& j) {
    Stock s;
    s.type = stock_type_from_string(j.value("type", std::string("box_offset")));
    if (j.contains("min")) s.min = vec3_from_json(j["min"]);
    if (j.contains("max")) s.max = vec3_from_json(j["max"]);
    s.off_xy = num(j.value("off_xy", json(0.0)));
    s.off_ztop = num(j.value("off_ztop", json(0.0)));
    s.off_zbot = num(j.value("off_zbot", json(0.0)));
    if (j.contains("base")) s.base = vec3_from_json(j["base"]);
    s.radius = num(j.value("radius", json(0.0)));
    s.height = num(j.value("height", json(0.0)));
    s.off_radial = num(j.value("off_radial", json(0.0)));
    s.off_axial = num(j.value("off_axial", json(0.0)));
    if (j.contains("solid")) s.solid = trimesh_from_json(j["solid"]);
    s.material = j.value("material", std::string("aluminum"));
    return s;
}

// ──────────────────────────────────────────────────────────────────── WCS ──
json WCS::to_json() const {
    return json{{"origin", cam::to_json(origin)}, {"x_dir", cam::to_json(x_dir)},
                {"z_dir", cam::to_json(z_dir)}, {"offset", offset}, {"offset_sub", offset_sub}};
}
WCS WCS::from_json(const json& j) {
    WCS w;
    if (j.contains("origin")) w.origin = vec3_from_json(j["origin"]);
    if (j.contains("x_dir")) w.x_dir = vec3_from_json(j["x_dir"]);
    if (j.contains("z_dir")) w.z_dir = vec3_from_json(j["z_dir"]);
    w.offset = j.value("offset", 54);
    w.offset_sub = j.value("offset_sub", 0);
    return w;
}

// ────────────────────────────────────────────────────────────────── Machine ──
json Machine::to_json() const {
    return json{{"kind", cam::to_string(kind)}, {"name", name}, {"travel", cam::to_json(travel)},
                {"rapid_xy", rapid_xy}, {"rapid_z", rapid_z}, {"max_rpm", max_rpm},
                {"min_rpm", min_rpm}, {"max_feed", max_feed}, {"tool_change_s", tool_change_s},
                {"accel", accel}, {"has_coolant", has_coolant}, {"has_flood", has_flood},
                {"has_mist", has_mist}, {"has_through", has_through}, {"kerf", kerf},
                {"rotary_axis", rotary_axis}, {"rotary_center", rotary_center}};
}
Machine Machine::from_json(const json& j) {
    Machine m;
    m.kind = machine_kind_from_string(j.value("kind", std::string("mill3")));
    m.name = j.value("name", m.name);
    if (j.contains("travel")) m.travel = vec3_from_json(j["travel"]);
    m.rapid_xy = num(j.value("rapid_xy", json(m.rapid_xy)), m.rapid_xy);
    m.rapid_z = num(j.value("rapid_z", json(m.rapid_z)), m.rapid_z);
    m.max_rpm = num(j.value("max_rpm", json(m.max_rpm)), m.max_rpm);
    m.min_rpm = num(j.value("min_rpm", json(m.min_rpm)), m.min_rpm);
    m.max_feed = num(j.value("max_feed", json(m.max_feed)), m.max_feed);
    m.tool_change_s = num(j.value("tool_change_s", json(m.tool_change_s)), m.tool_change_s);
    m.accel = num(j.value("accel", json(0.0)));
    m.has_coolant = j.value("has_coolant", true);
    m.has_flood = j.value("has_flood", true);
    m.has_mist = j.value("has_mist", true);
    m.has_through = j.value("has_through", false);
    m.kerf = num(j.value("kerf", json(0.0)));
    m.rotary_axis = j.value("rotary_axis", std::string("A"));
    m.rotary_center = num(j.value("rotary_center", json(0.0)));
    return m;
}

// ────────────────────────────────────────────────────────────────────── Tool ──
json Tool::to_json() const {
    return json{{"number", number}, {"name", name}, {"type", cam::to_string(type)},
                {"diameter", diameter}, {"corner_radius", corner_radius}, {"tip_angle", tip_angle},
                {"flutes", flutes}, {"flute_length", flute_length},
                {"shoulder_length", shoulder_length}, {"overall_length", overall_length},
                {"shank_diameter", shank_diameter}, {"taper_angle", taper_angle},
                {"thread_pitch", thread_pitch}, {"holder_diameter", holder_diameter},
                {"holder_length", holder_length}, {"pocket", pocket},
                {"tool_material", tool_material}, {"cutting_overrides", cutting_overrides}};
}
Tool Tool::from_json(const json& j) {
    Tool t;
    t.number = j.value("number", 1);
    t.name = j.value("name", std::string());
    t.type = tool_type_from_string(j.value("type", std::string("endmill_flat")));
    t.diameter = num(j.value("diameter", json(t.diameter)), t.diameter);
    t.corner_radius = num(j.value("corner_radius", json(0.0)));
    t.tip_angle = num(j.value("tip_angle", json(0.0)));
    t.flutes = j.value("flutes", 2);
    t.flute_length = num(j.value("flute_length", json(t.flute_length)), t.flute_length);
    t.shoulder_length = num(j.value("shoulder_length", json(t.shoulder_length)), t.shoulder_length);
    t.overall_length = num(j.value("overall_length", json(t.overall_length)), t.overall_length);
    t.shank_diameter = num(j.value("shank_diameter", json(t.shank_diameter)), t.shank_diameter);
    t.taper_angle = num(j.value("taper_angle", json(0.0)));
    t.thread_pitch = num(j.value("thread_pitch", json(0.0)));
    t.holder_diameter = num(j.value("holder_diameter", json(t.holder_diameter)), t.holder_diameter);
    t.holder_length = num(j.value("holder_length", json(t.holder_length)), t.holder_length);
    t.pocket = j.value("pocket", 0);
    t.tool_material = j.value("tool_material", std::string("carbide"));
    if (j.contains("cutting_overrides") && j["cutting_overrides"].is_object())
        t.cutting_overrides = j["cutting_overrides"];
    return t;
}

const Tool* ToolLibrary::find(int number) const {
    for (auto& t : tools) if (t.number == number) return &t;
    return nullptr;
}
json ToolLibrary::to_json() const {
    json a = json::array();
    for (auto& t : tools) a.push_back(t.to_json());
    return json{{"tools", a}};
}
ToolLibrary ToolLibrary::from_json(const json& j) {
    ToolLibrary lib;
    const json& a = j.is_array() ? j : j.value("tools", json::array());
    for (auto& e : a) lib.tools.push_back(Tool::from_json(e));
    return lib;
}

// ─────────────────────────────────────────────────────── Heights/Lead/Ramp ──
json Heights::to_json() const {
    return json{{"clearance", clearance}, {"retract", retract}, {"top", top}, {"bottom", bottom}};
}
Heights Heights::from_json(const json& j) {
    Heights h;
    h.clearance = num(j.value("clearance", json(h.clearance)), h.clearance);
    h.retract = num(j.value("retract", json(h.retract)), h.retract);
    h.top = num(j.value("top", json(h.top)), h.top);
    h.bottom = num(j.value("bottom", json(h.bottom)), h.bottom);
    return h;
}
json Lead::to_json() const {
    return json{{"type", cam::to_string(type)}, {"radius", radius}, {"length", length},
                {"angle", angle}, {"feed_scale", feed_scale}};
}
Lead Lead::from_json(const json& j) {
    Lead l;
    l.type = lead_type_from_string(j.value("type", std::string("arc")));
    l.radius = num(j.value("radius", json(l.radius)), l.radius);
    l.length = num(j.value("length", json(l.length)), l.length);
    l.angle = num(j.value("angle", json(l.angle)), l.angle);
    l.feed_scale = num(j.value("feed_scale", json(1.0)), 1.0);
    return l;
}
json Ramp::to_json() const {
    return json{{"type", cam::to_string(type)}, {"angle", angle}, {"diameter", diameter},
                {"clearance", clearance}};
}
Ramp Ramp::from_json(const json& j) {
    Ramp r;
    r.type = ramp_type_from_string(j.value("type", std::string("helix")));
    r.angle = num(j.value("angle", json(r.angle)), r.angle);
    r.diameter = num(j.value("diameter", json(0.0)));
    r.clearance = num(j.value("clearance", json(r.clearance)), r.clearance);
    return r;
}

// ─────────────────────────────────────────────────────────────── Operation ──
json Operation::to_json() const {
    json j{
        {"id", id}, {"name", name}, {"type", cam::to_string(type)}, {"suppressed", suppressed},
        {"tool_number", tool_number}, {"heights", heights.to_json()},
        {"stepover", stepover}, {"stepdown", stepdown}, {"stock_to_leave", stock_to_leave},
        {"stock_to_leave_axial", stock_to_leave_axial}, {"tolerance", tolerance},
        {"pattern", cam::to_string(pattern)}, {"direction", cam::to_string(direction)},
        {"angle", angle}, {"multi_depth", multi_depth},
        {"lead_in", lead_in.to_json()}, {"lead_out", lead_out.to_json()}, {"ramp", ramp.to_json()},
        {"rpm", rpm}, {"feed", feed}, {"plunge", plunge}, {"lead_feed", lead_feed},
        {"ramp_feed", ramp_feed}, {"spindle_dir", spindle_dir}, {"coolant", coolant},
        {"surface_speed_mode", surface_speed_mode},
        {"comp_side", cam::to_string(comp_side)}, {"comp_in_control", comp_in_control},
        {"tab_count", tab_count}, {"tab_height", tab_height}, {"tab_width", tab_width},
        {"finish_passes", finish_passes}, {"roughing_passes_step", roughing_passes_step},
        {"cycle", cycle}, {"peck", peck}, {"dwell", dwell}, {"break_chip", break_chip},
        {"tip_through", tip_through},
        {"css", css}, {"max_turn_rpm", max_turn_rpm}, {"turn_clearance", turn_clearance},
        {"infeed", infeed}, {"thread_pitch", thread_pitch}, {"thread_depth", thread_depth},
        {"thread_passes", thread_passes}, {"id_op", id_op},
        {"passes", passes}, {"pierce_delay", pierce_delay}, {"pierce_height", pierce_height},
        {"cut_height", cut_height}, {"kerf_override", kerf_override}, {"power", power},
        {"feed_rate", feed_rate},
        {"geometry", geometry}, {"extra", extra}};
    if (wcs) j["wcs"] = wcs->to_json();
    return j;
}
Operation Operation::from_json(const json& j) {
    Operation o;
    o.id = j.value("id", std::string());
    o.name = j.value("name", std::string());
    o.type = op_type_from_string(j.value("type", std::string("contour2d")));
    o.suppressed = j.value("suppressed", false);
    o.tool_number = j.value("tool_number", 1);
    if (j.contains("wcs")) o.wcs = WCS::from_json(j["wcs"]);
    if (j.contains("heights")) o.heights = Heights::from_json(j["heights"]);
    o.stepover = num(j.value("stepover", json(0.0)));
    o.stepdown = num(j.value("stepdown", json(0.0)));
    o.stock_to_leave = num(j.value("stock_to_leave", json(0.0)));
    o.stock_to_leave_axial = num(j.value("stock_to_leave_axial", json(0.0)));
    o.tolerance = num(j.value("tolerance", json(o.tolerance)), o.tolerance);
    o.pattern = cut_pattern_from_string(j.value("pattern", std::string("zigzag")));
    o.direction = cut_direction_from_string(j.value("direction", std::string("climb")));
    o.angle = num(j.value("angle", json(0.0)));
    o.multi_depth = j.value("multi_depth", true);
    if (j.contains("lead_in")) o.lead_in = Lead::from_json(j["lead_in"]);
    if (j.contains("lead_out")) o.lead_out = Lead::from_json(j["lead_out"]);
    if (j.contains("ramp")) o.ramp = Ramp::from_json(j["ramp"]);
    o.rpm = num(j.value("rpm", json(0.0)));
    o.feed = num(j.value("feed", json(0.0)));
    o.plunge = num(j.value("plunge", json(0.0)));
    o.lead_feed = num(j.value("lead_feed", json(0.0)));
    o.ramp_feed = num(j.value("ramp_feed", json(0.0)));
    o.spindle_dir = j.value("spindle_dir", 1);
    o.coolant = j.value("coolant", std::string("flood"));
    o.surface_speed_mode = j.value("surface_speed_mode", std::string("rpm"));
    o.comp_side = comp_side_from_string(j.value("comp_side", std::string("on")));
    o.comp_in_control = j.value("comp_in_control", false);
    o.tab_count = j.value("tab_count", 0);
    o.tab_height = num(j.value("tab_height", json(0.0)));
    o.tab_width = num(j.value("tab_width", json(0.0)));
    o.finish_passes = j.value("finish_passes", 0);
    o.roughing_passes_step = num(j.value("roughing_passes_step", json(0.0)));
    o.cycle = j.value("cycle", std::string("drill"));
    o.peck = num(j.value("peck", json(0.0)));
    o.dwell = num(j.value("dwell", json(0.0)));
    o.break_chip = num(j.value("break_chip", json(0.0)));
    o.tip_through = num(j.value("tip_through", json(0.0)));
    o.css = num(j.value("css", json(0.0)));
    o.max_turn_rpm = num(j.value("max_turn_rpm", json(o.max_turn_rpm)), o.max_turn_rpm);
    o.turn_clearance = num(j.value("turn_clearance", json(o.turn_clearance)), o.turn_clearance);
    o.infeed = num(j.value("infeed", json(0.0)));
    o.thread_pitch = num(j.value("thread_pitch", json(0.0)));
    o.thread_depth = num(j.value("thread_depth", json(0.0)));
    o.thread_passes = j.value("thread_passes", 0);
    o.id_op = j.value("id_op", false);
    o.passes = j.value("passes", 1);
    o.pierce_delay = num(j.value("pierce_delay", json(0.0)));
    o.pierce_height = num(j.value("pierce_height", json(0.0)));
    o.cut_height = num(j.value("cut_height", json(0.0)));
    o.kerf_override = num(j.value("kerf_override", json(0.0)));
    o.power = num(j.value("power", json(0.0)));
    o.feed_rate = num(j.value("feed_rate", json(0.0)));
    if (j.contains("geometry") && j["geometry"].is_object()) o.geometry = j["geometry"];
    if (j.contains("extra") && j["extra"].is_object()) o.extra = j["extra"];
    return o;
}

// ──────────────────────────────────────────────────────────── Move/Toolpath ──
json Move::to_json() const {
    json j{{"kind", cam::to_string(kind)}, {"to", cam::to_json(to)}};
    if (kind == MoveKind::Arc) { j["ccw"] = ccw; j["center"] = cam::to_json(center); }
    if (feed > 0) j["feed"] = feed;
    if (spindle_rpm > 0) j["rpm"] = spindle_rpm;
    if (dwell_s > 0) j["dwell_s"] = dwell_s;
    return j;
}
Move Move::from_json(const json& j) {
    Move m;
    m.kind = move_kind_from_string(j.value("kind", std::string("cut")));
    if (j.contains("to")) m.to = vec3_from_json(j["to"]);
    m.ccw = j.value("ccw", false);
    if (j.contains("center")) m.center = vec3_from_json(j["center"]);
    m.feed = num(j.value("feed", json(0.0)));
    m.spindle_rpm = j.value("rpm", 0);
    m.dwell_s = num(j.value("dwell_s", json(0.0)));
    return m;
}
json Toolpath::to_json() const {
    json mv = json::array();
    for (auto& m : moves) mv.push_back(m.to_json());
    return json{{"op_id", op_id}, {"tool_number", tool_number}, {"moves", mv},
                {"cut_length", cut_length}, {"rapid_length", rapid_length},
                {"cycle_time_s", cycle_time_s}, {"bbox_min", cam::to_json(bbox_min)},
                {"bbox_max", cam::to_json(bbox_max)}, {"warnings", warnings}};
}
Toolpath Toolpath::from_json(const json& j) {
    Toolpath t;
    t.op_id = j.value("op_id", std::string());
    t.tool_number = j.value("tool_number", 0);
    if (j.contains("moves")) for (auto& m : j["moves"]) t.moves.push_back(Move::from_json(m));
    t.cut_length = num(j.value("cut_length", json(0.0)));
    t.rapid_length = num(j.value("rapid_length", json(0.0)));
    t.cycle_time_s = num(j.value("cycle_time_s", json(0.0)));
    if (j.contains("bbox_min")) t.bbox_min = vec3_from_json(j["bbox_min"]);
    if (j.contains("bbox_max")) t.bbox_max = vec3_from_json(j["bbox_max"]);
    if (j.contains("warnings")) for (auto& w : j["warnings"]) t.warnings.push_back(w.get<std::string>());
    return t;
}

// ───────────────────────────────────────────────────────────── Setup/Job ──
json Setup::to_json() const {
    json ops = json::array();
    for (auto& o : operations) ops.push_back(o.to_json());
    return json{{"id", id}, {"name", name}, {"machine", machine.to_json()},
                {"stock", stock.to_json()}, {"wcs", wcs.to_json()},
                {"model_bodies", model_bodies}, {"operations", ops}};
}
Setup Setup::from_json(const json& j) {
    Setup s;
    s.id = j.value("id", std::string());
    s.name = j.value("name", std::string("Setup1"));
    if (j.contains("machine")) s.machine = Machine::from_json(j["machine"]);
    if (j.contains("stock")) s.stock = Stock::from_json(j["stock"]);
    if (j.contains("wcs")) s.wcs = WCS::from_json(j["wcs"]);
    if (j.contains("model_bodies")) for (auto& b : j["model_bodies"]) s.model_bodies.push_back(b.get<std::string>());
    if (j.contains("operations")) for (auto& o : j["operations"]) s.operations.push_back(Operation::from_json(o));
    return s;
}
json Job::to_json() const {
    json sj = json::array();
    for (auto& s : setups) sj.push_back(s.to_json());
    return json{{"id", id}, {"name", name}, {"units", units}, {"tools", tools.to_json()},
                {"setups", sj}, {"post", post}, {"post_options", post_options}};
}
Job Job::from_json(const json& j) {
    Job job;
    job.id = j.value("id", std::string());
    job.name = j.value("name", std::string("CAM Job"));
    job.units = j.value("units", std::string("mm"));
    if (j.contains("tools")) job.tools = ToolLibrary::from_json(j["tools"]);
    if (j.contains("setups")) for (auto& s : j["setups"]) job.setups.push_back(Setup::from_json(s));
    job.post = j.value("post", std::string("grbl"));
    if (j.contains("post_options") && j["post_options"].is_object()) job.post_options = j["post_options"];
    return job;
}

}  // namespace cam
}  // namespace aicad
