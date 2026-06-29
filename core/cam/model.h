// core/cam/model.h — the CAM (computer-aided machining) data model.
//
// This is the shared contract for the whole `aicad::cam` subsystem: stock, work
// coordinate systems, machine/platform definitions, the tool library, cutting
// data, machining operations, the generated toolpaths (classified moves), and the
// Job/Setup tree that ties them together.
//
// Design notes
//  - OCCT-FREE. Like aicad::sim and aicad::ecad, the CAM core is pure numerics +
//    JSON so it builds in the fast standalone config and is unit-testable headless.
//    Geometry to machine (regions, section profiles, drill points, a triangle mesh)
//    is supplied to operations as plain data; a separate OCCT extraction adapter
//    pulls that data out of a live document for the product.
//  - Everything (de)serializes through nlohmann::json: the same shape persists in
//    the .cam+ document, crosses the libaicad C ABI, and round-trips in tests.
//  - All lengths are millimetres, angles degrees, feeds mm/min, speeds rev/min,
//    times seconds, unless a field says otherwise.
#pragma once

#include <array>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad {
namespace cam {

using nlohmann::json;

// ───────────────────────────────────────────────────────────── geometry atoms ──
struct Vec2 {
    double x = 0, y = 0;
    Vec2() = default;
    Vec2(double x_, double y_) : x(x_), y(y_) {}
};
struct Vec3 {
    double x = 0, y = 0, z = 0;
    Vec3() = default;
    Vec3(double x_, double y_, double z_) : x(x_), y(y_), z(z_) {}
};

// A polyline: open (a path) or closed (a contour, no repeated first/last point).
using Path2 = std::vector<Vec2>;

// A planar machinable region: one outer boundary (CCW) and zero+ island holes (CW).
struct Region {
    Path2 outer;
    std::vector<Path2> holes;
};

// A triangle soup for 3D operations (heightfield sampling) and collision checks.
struct TriMesh {
    std::vector<Vec3> verts;
    std::vector<std::array<int, 3>> tris;
};

// ──────────────────────────────────────────────────────────────────── stock ──
enum class StockType {
    BoxOffset,      // model bbox grown by per-axis offsets (the common default)
    Box,            // explicit min/max corners
    CylinderOffset, // model bbox -> cylinder grown by radial/axial offsets (turning)
    Cylinder,       // explicit cylinder
    FromSolid,      // an arbitrary solid (a casting/forging) supplied as a mesh
};

struct Stock {
    StockType type = StockType::BoxOffset;
    // Box / BoxOffset: world-space bounds (resolved). For BoxOffset these are the
    // model bbox + offsets after resolve_stock(); before resolve they are unused.
    Vec3 min{0, 0, 0};
    Vec3 max{0, 0, 0};
    double off_xy = 0;      // BoxOffset: radial (X/Y) grow
    double off_ztop = 0;    // BoxOffset: top grow
    double off_zbot = 0;    // BoxOffset: bottom grow
    // Cylinder / CylinderOffset: axis is +Z through `base`.
    Vec3 base{0, 0, 0};
    double radius = 0;
    double height = 0;
    double off_radial = 0;  // CylinderOffset
    double off_axial = 0;   // CylinderOffset (per end)
    // FromSolid: the stock body as a mesh (e.g. a near-net casting).
    TriMesh solid;
    // material being machined (cutting-data DB id; drives feeds & speeds).
    std::string material = "aluminum";

    json to_json() const;
    static Stock from_json(const json&);
};

// ────────────────────────────────────────────────────────── work coordinates ──
struct WCS {
    Vec3 origin{0, 0, 0};   // world coords of program zero (G54 origin)
    Vec3 x_dir{1, 0, 0};    // +X of the work frame (unit)
    Vec3 z_dir{0, 0, 1};    // +Z = tool axis (unit)
    int offset = 54;        // work offset register: 54..59 (-> 54.1..59.x via offset_sub)
    int offset_sub = 0;     // extended offset (G54.1 P<n>); 0 = none

    json to_json() const;
    static WCS from_json(const json&);
};

// ─────────────────────────────────────────────────────── machine / platform ──
enum class MachineKind {
    Mill3,     // 3-axis vertical mill
    Mill4,     // 3+1 (single rotary)
    Mill5,     // 5-axis (3+2 positional and/or simultaneous)
    Router,    // gantry router
    Lathe,     // turning centre
    MillTurn,  // lathe with live tooling / C-axis
    Laser,     // laser cutter/engraver
    Plasma,    // plasma cutter
    Waterjet,  // waterjet cutter
};

struct Machine {
    MachineKind kind = MachineKind::Mill3;
    std::string name = "Generic 3-axis mill";
    Vec3 travel{300, 300, 150};  // working envelope (mm)
    double rapid_xy = 5000;      // rapid traverse rate, X/Y plane (mm/min)
    double rapid_z = 3000;       // rapid traverse rate, Z (mm/min)
    double max_rpm = 24000;
    double min_rpm = 0;
    double max_feed = 10000;     // controller feed clamp (mm/min)
    double tool_change_s = 6.0;  // dwell budget per tool change (cycle-time estimate)
    double accel = 0;            // axis accel (mm/s^2); 0 = ignore in time estimate
    bool has_coolant = true;
    bool has_flood = true;
    bool has_mist = true;
    bool has_through = false;     // through-spindle coolant
    // 2D cutting heads
    double kerf = 0;             // laser/plasma/waterjet kerf width (mm)
    // rotary / multi-axis
    std::string rotary_axis = "A";   // Mill4 rotary letter (A/B/C)
    double rotary_center = 0;        // rotary axis offset along the linear axis (mm)

    json to_json() const;
    static Machine from_json(const json&);
};

// ──────────────────────────────────────────────────────────────────── tools ──
enum class ToolType {
    // milling / drilling
    EndmillFlat, EndmillBall, EndmillBull, ChamferMill, FaceMill, VBit, SlotMill,
    DovetailMill, ThreadMill, Drill, SpotDrill, CenterDrill, Tap, Reamer, BoringBar,
    // turning
    TurningGeneral, GroovingTool, ThreadingTool, PartingTool, BoringBarID,
    // 2D heads
    LaserHead, PlasmaTorch, WaterjetHead,
    // metrology
    Probe,
};

struct Tool {
    int number = 1;             // T-word
    std::string name;
    ToolType type = ToolType::EndmillFlat;
    double diameter = 6.0;
    double corner_radius = 0.0; // bull-nose tip radius (= diameter/2 for a ball)
    double tip_angle = 0.0;     // included angle (deg): drill/spot/vbit/chamfer
    int flutes = 2;
    double flute_length = 20;   // length of cut (LOC)
    double shoulder_length = 25;
    double overall_length = 60;
    double shank_diameter = 6;
    double taper_angle = 0;     // tapered wall tools (deg per side)
    double thread_pitch = 0;    // tap / thread-mill pitch (mm)
    // holder geometry (collision)
    double holder_diameter = 32;
    double holder_length = 45;
    int pocket = 0;             // ATC pocket / turret station
    std::string tool_material = "carbide"; // hss | cobalt | carbide | ceramic
    // optional per-tool cutting-data overrides, keyed by material group:
    //   {"aluminum": {"sfm": 800, "fz": 0.05, "stepover": 0.5, "stepdown": 1.0}, ...}
    json cutting_overrides = json::object();

    double effective_radius() const { return diameter * 0.5; }
    json to_json() const;
    static Tool from_json(const json&);
};

struct ToolLibrary {
    std::vector<Tool> tools;
    const Tool* find(int number) const;
    json to_json() const;
    static ToolLibrary from_json(const json&);
};

// ───────────────────────────────────────────────────── heights / leads / ramp ──
// Per-operation Z planes, as absolute Z in the operation's WCS (mm).
struct Heights {
    double clearance = 15;  // rapid (safe) plane above all clamps
    double retract = 5;     // feed-rate retract between passes
    double top = 0;         // top of the material to cut
    double bottom = -5;     // final depth

    json to_json() const;
    static Heights from_json(const json&);
};

enum class LeadType { None, Line, Arc, Tangent };
struct Lead {
    LeadType type = LeadType::Arc;
    double radius = 2.0;   // arc lead radius (mm)
    double length = 2.0;   // line lead length / arc subtend chord (mm)
    double angle = 90.0;   // arc sweep (deg)
    double feed_scale = 1.0; // lead feed = feed * feed_scale

    json to_json() const;
    static Lead from_json(const json&);
};

enum class RampType { Plunge, ZigZag, Helix, Profile };
struct Ramp {
    RampType type = RampType::Helix;
    double angle = 2.0;     // ramp/helix descent angle (deg)
    double diameter = 0;    // helix diameter (0 = auto from pocket / tool)
    double clearance = 0.5; // helix wall clearance (mm)

    json to_json() const;
    static Ramp from_json(const json&);
};

// ───────────────────────────────────────────────────────────── operations ──
enum class OpType {
    // 2.5D milling
    Face, Contour2D, Pocket2D, Slot, Adaptive2D, Engrave, Chamfer2D, Trace, Bore2D,
    // hole-making (the cycle is in Operation::cycle)
    Drill, ThreadMill,
    // 3D milling
    AdaptiveClear3D, Pocket3D, Parallel3D, Contour3D, Scallop3D, Pencil3D, Radial3D,
    SpiralFinish3D,
    // turning
    TurnFace, TurnRough, TurnFinish, TurnGroove, TurnThread, TurnPart, TurnDrill,
    // 2D cutting heads
    Cut2D,
    // metrology
    Probe,
};

// Cutting-strategy enums shared by several ops.
enum class CutPattern { Zigzag, OneWay, Spiral, Concentric, MorphedSpiral, Radial };
enum class CutDirection { Climb, Conventional };
enum class CompSide { On, Left, Right };  // tool comp: centre-on / outside-left / inside-right

struct Operation {
    std::string id;
    std::string name;
    OpType type = OpType::Contour2D;
    bool suppressed = false;
    int tool_number = 1;

    // Per-op frame + Z planes (default inherits the setup's WCS at generate time).
    std::optional<WCS> wcs;
    Heights heights;

    // Common machining parameters
    double stepover = 0;             // radial (mm); 0 -> derived from feeds/tool
    double stepdown = 0;             // axial per pass (mm); 0 -> single full-depth
    double stock_to_leave = 0;       // radial finish allowance (mm)
    double stock_to_leave_axial = 0; // floor finish allowance (mm)
    double tolerance = 0.01;         // chord tolerance for arc/curve discretization (mm)
    CutPattern pattern = CutPattern::Zigzag;
    CutDirection direction = CutDirection::Climb;
    double angle = 0;                // raster/zigzag angle (deg)
    bool multi_depth = true;         // step down in stepdown increments

    Lead lead_in;
    Lead lead_out;
    Ramp ramp;

    // Feeds & speeds (0 -> resolved from the cutting-data DB at generate time).
    double rpm = 0;
    double feed = 0;       // cutting feed (mm/min)
    double plunge = 0;     // Z plunge feed (mm/min)
    double lead_feed = 0;  // lead-in/out feed (mm/min)
    double ramp_feed = 0;  // ramp/helix feed (mm/min)
    int spindle_dir = 1;   // +1 = M3 (CW), -1 = M4 (CCW)
    std::string coolant = "flood"; // off | flood | mist | air | through
    std::string surface_speed_mode = "rpm"; // rpm | css (constant surface speed, turning)

    // Contour / profile
    CompSide comp_side = CompSide::On;
    bool comp_in_control = false;  // emit G41/G42 (true) vs computer-comp centreline
    int tab_count = 0;             // holding tabs around the perimeter
    double tab_height = 0;
    double tab_width = 0;
    int finish_passes = 0;         // spring/finish passes at final size
    double roughing_passes_step = 0; // radial roughing increments before finish

    // Drilling / boring (cycle: drill|peck|chipbreak|tap|rigidtap|ream|bore|spot|backbore)
    std::string cycle = "drill";
    double peck = 0;        // peck increment (mm)
    double dwell = 0;       // dwell at depth (s)
    double break_chip = 0;  // chip-break retract (mm)
    double tip_through = 0; // break-through allowance added to depth (mm)

    // Turning
    double css = 0;             // constant surface speed (m/min) when surface_speed_mode=css
    double max_turn_rpm = 4000; // CSS rpm clamp
    double turn_clearance = 1;  // X/Z clearance off the profile (mm)
    double infeed = 0;          // grooving/threading infeed per pass (mm)
    double thread_pitch = 0;    // turning thread pitch (mm)
    double thread_depth = 0;    // total thread depth (mm)
    int thread_passes = 0;      // 0 -> auto from depth/infeed
    bool id_op = false;         // internal (ID) vs external (OD) turning

    // 2D cutting (laser/plasma/waterjet)
    int passes = 1;             // repeated cut passes (plasma/multi-pass)
    double pierce_delay = 0;    // dwell before cut (s)
    double pierce_height = 0;   // standoff at pierce (mm)
    double cut_height = 0;      // standoff while cutting (mm)
    double kerf_override = 0;   // override machine kerf (mm); 0 = use machine
    double power = 0;           // 0..1 laser power / amperage scale
    double feed_rate = 0;       // cut feed for the head (mm/min); 0 -> feed

    // Geometry inputs the operation machines. Resolved either inline (tests / MCP)
    // or by the OCCT extraction adapter. Shape:
    //   {"regions":[Region...], "contours":[Path2...], "points":[[x,y,z]...],
    //    "axes":[[x,y,z]...], "profile":[[z,x]...], "mesh":{verts,tris},
    //    "faces":[selectorTokens...]}
    json geometry = json::object();

    // Op-specific overflow that does not warrant a typed field yet.
    json extra = json::object();

    json to_json() const;
    static Operation from_json(const json&);
};

// ───────────────────────────────────────────────────── generated toolpath ──
enum class MoveKind {
    Rapid,       // G0 traverse
    Lead,        // lead-in/out feed move
    Cut,         // linear cutting feed (G1)
    Plunge,      // Z plunge feed
    Arc,         // circular feed (G2/G3)
    Retract,     // feed/rapid up to a safe plane
    Dwell,       // G4 pause
    ToolChange,  // marks a tool change boundary
};

struct Move {
    MoveKind kind = MoveKind::Cut;
    Vec3 to{0, 0, 0};        // target point (WCS mm)
    bool ccw = false;        // Arc: G3 (true) vs G2 (false)
    Vec3 center{0, 0, 0};    // Arc: arc centre (WCS mm)
    double feed = 0;         // mm/min (0 for Rapid/Dwell/ToolChange)
    int spindle_rpm = 0;     // 0 = unchanged from prior state
    double dwell_s = 0;      // Dwell seconds

    json to_json() const;
    static Move from_json(const json&);
};

struct Toolpath {
    std::string op_id;
    int tool_number = 0;
    std::vector<Move> moves;
    // statistics (filled by the generator / time estimator)
    double cut_length = 0;
    double rapid_length = 0;
    double cycle_time_s = 0;
    Vec3 bbox_min{0, 0, 0};
    Vec3 bbox_max{0, 0, 0};
    std::vector<std::string> warnings;

    void update_stats(const Machine& m);
    json to_json() const;
    static Toolpath from_json(const json&);
};

// ─────────────────────────────────────────────────────────── job / setup ──
struct Setup {
    std::string id;
    std::string name = "Setup1";
    Machine machine;
    Stock stock;
    WCS wcs;
    std::vector<std::string> model_bodies;  // doc body ids machined in this setup
    std::vector<Operation> operations;

    json to_json() const;
    static Setup from_json(const json&);
};

struct Job {
    std::string id;
    std::string name = "CAM Job";
    std::string units = "mm";
    ToolLibrary tools;
    std::vector<Setup> setups;
    std::string post = "grbl";   // default post dialect id
    json post_options = json::object();

    json to_json() const;
    static Job from_json(const json&);
};

// ─────────────────────────────────────────────────────── enum <-> string ──
const char* to_string(StockType);
const char* to_string(MachineKind);
const char* to_string(ToolType);
const char* to_string(OpType);
const char* to_string(MoveKind);
const char* to_string(LeadType);
const char* to_string(RampType);
const char* to_string(CutPattern);
const char* to_string(CutDirection);
const char* to_string(CompSide);

StockType stock_type_from_string(const std::string&);
MachineKind machine_kind_from_string(const std::string&);
ToolType tool_type_from_string(const std::string&);
OpType op_type_from_string(const std::string&);
MoveKind move_kind_from_string(const std::string&);
LeadType lead_type_from_string(const std::string&);
RampType ramp_type_from_string(const std::string&);
CutPattern cut_pattern_from_string(const std::string&);
CutDirection cut_direction_from_string(const std::string&);
CompSide comp_side_from_string(const std::string&);

// Tool-class grouping for cutting-data lookup ("endmill"|"ballmill"|"drill"|"tap"|
// "face"|"chamfer"|"thread"|"turning"|"laser"|"plasma"|"waterjet"|"probe").
std::string tool_class(ToolType);

// Small JSON helpers (tolerant readers used across the subsystem).
Vec2 vec2_from_json(const json&);
Vec3 vec3_from_json(const json&);
json to_json(const Vec2&);
json to_json(const Vec3&);
Path2 path2_from_json(const json&);
json to_json(const Path2&);
Region region_from_json(const json&);
json to_json(const Region&);
TriMesh trimesh_from_json(const json&);
json to_json(const TriMesh&);

}  // namespace cam
}  // namespace aicad
