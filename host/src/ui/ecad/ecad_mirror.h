// AI-ECAD UI scaffold (Wave D) — NOT yet build-verified; see docs/ECAD_UI_INTEGRATION.md
//
// EcadMirror — a read-only, in-process mirror of one open ECAD document, built from the JSON the
// worker returns for `ecad_tree` / `ecad_apply_ops` (shape: {revision, kind, tree, compile}; see
// engine/ecad/session.py::tree and docs/contracts/ecad/ecad_rpc.md §1). It is the ECAD analogue of
// core::DocMirror — but where DocMirror keeps the raw doc JSON and lets the FeatureManager walk it,
// EcadMirror PARSES the schematic / board document into flat typed vectors with WORLD coordinates
// already computed, so the canvas can draw without re-deriving the rotate/mirror math every frame.
//
// The world-coordinate transforms (pinWorld / padWorld / localToWorld) are a faithful port of
// engine/ecad/schema.py (pin_world / pad_world / local_to_world): mirror, then rotate CCW by the
// placement angle, then translate. Keeping the math identical guarantees the canvas places pins/pads
// exactly where the engine's netlist/DRC believe they are.
//
// Coordinates here are ECAD-world millimetres, +x right, +y UP (math convention). The CANVAS (not
// this mirror) flips +y for screen — same split the Python contract documents. This mirror never
// touches ImGui or the renderer; it depends only on nlohmann::json so it stays trivially testable.
//
// Assumptions about host APIs (could not build-verify):
//   * nlohmann::json is available as <nlohmann/json.hpp> (confirmed: worker_client.h / app_state.h).
//   * WorkerClient::call(method, params, result, err, ...) is the RPC entry (confirmed in
//     worker_client.h). EcadMirror itself does NOT call the worker — the panel owns RPC and feeds
//     parsed JSON in via loadFromTree(); see ECAD_UI_INTEGRATION.md §(e) for the call pattern.
#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui {

// ---- shared 2D point (ECAD-world mm, +y up) ----
struct EcadPt {
    float x = 0.0f, y = 0.0f;
};

// =====================================================================================
// Schematic (aicad.sch/1) parsed entities
// =====================================================================================

// A symbol graphic primitive in SYMBOL-LOCAL mm (rect/line/circle/polyline/arc), as emitted by
// engine/ecad/library/starter.py (_rect/_line/_circle/_polyline). The canvas transforms each point
// through the owning symbol's placement before drawing.
struct EcadSymGraphic {
    enum Kind { Rect, Line, Circle, Polyline, Arc } kind = Line;
    std::vector<EcadPt> pts;   // rect: [p1,p2]; line: [p1,p2]; polyline: all pts; circle/arc: [center]
    float radius = 0.0f;       // circle / arc
    float a0 = 0.0f, a1 = 0.0f;  // arc sweep (deg CCW)
};

// A placed symbol pin with its WORLD root + WORLD connection-tip already computed.
struct EcadPin {
    std::string number;        // "1", "A1", …
    std::string name;          // "OUT", "~"
    std::string type;          // "passive" | "power_in" | … (drives ERC; cosmetic here)
    EcadPt root;               // world position where the pin meets the body
    EcadPt tip;                // world connection endpoint (what wires/netlist snap to)
    std::string net;           // resolved net name (from compile.netlist.by_pin), "" if floating
};

// A placed symbol instance (aicad.sch/1 §4) with its lib_cache body graphics resolved.
struct EcadSymbol {
    std::string id;            // "s1"
    std::string libid;         // "Device:R"
    std::string ref;           // "R1" / "R?"
    std::string value;         // "10k"
    EcadPt pos;                // placement origin (world == sheet-local in flat v1)
    float angle = 0.0f;        // deg CCW
    std::string mirror = "none";  // "none" | "x" | "y"
    bool power = false;        // lib_cache SymbolDef.power (VCC/GND etc.)
    std::string footprint;     // assigned footprint id ("Resistor_SMD:R_0603"); "" if unassigned
    bool dnp = false;          // do-not-populate flag (excluded from BOM/assembly)
    nlohmann::json simParams;  // free-form sim_params dict (raw JSON; null/absent if none)
    std::vector<EcadSymGraphic> body;  // local-frame body graphics (transform at draw time)
    std::vector<EcadPin> pins;         // world-space pins
};

// Orthogonal wire polyline (world == sheet-local).
struct EcadWire {
    std::string id;            // "w1"
    std::vector<EcadPt> pts;   // >= 2 points
    bool bus = false;
};

struct EcadJunction { std::string id; EcadPt pos; };
struct EcadLabel    { std::string id; std::string text; std::string scope = "local"; EcadPt pos; float angle = 0.0f; };
struct EcadPower    { std::string id; std::string net;  std::string symbol; EcadPt pos; };

// =====================================================================================
// Board (aicad.pcb/1) parsed entities
// =====================================================================================

// A footprint pad with its WORLD centre already computed (rotate + side-mirror applied).
struct EcadPad {
    std::string number;        // "1"
    EcadPt pos;                // world centre
    float angle = 0.0f;        // world angle (footprint angle + local angle), deg CCW
    std::string shape = "rect";  // rect|roundrect|circle|oval|trapezoid|custom
    float w = 0.0f, h = 0.0f;  // size [w,h] mm
    float roundrectRatio = 0.0f;
    std::string type = "smd";  // smd|tht|np_tht|connector
    std::vector<std::string> layers;  // copper/tech layer ids this pad is on
    bool hasDrill = false;
    float drillDia = 0.0f;     // through-hole drill diameter (mm)
    std::string net;           // net name or ""
};

// A placed footprint (aicad.pcb/1 §4) with world pads + a world courtyard rect (from F/B.CrtYd
// graphics if present, else the pad bbox).
struct EcadFootprint {
    std::string id;            // "fp1"
    std::string fpid;          // "Resistor_SMD:R_0603"
    std::string ref;           // "R1"
    std::string value;         // "10k"
    EcadPt pos;                // placement origin (world)
    float angle = 0.0f;        // deg CCW
    std::string side = "top";  // "top" | "bottom" (bottom mirrors across x)
    std::vector<EcadPad> pads; // world-space pads
    // Axis-aligned world courtyard bbox (min/max). Valid when hasCourtyard.
    bool hasCourtyard = false;
    EcadPt courtyardMin, courtyardMax;
};

// A copper track segment / arc (world coords already; tracks store world points directly).
struct EcadTrack {
    std::string id;            // "t1"
    std::string net;           // net name or ""
    std::string layer;         // "F.Cu" | "In1.Cu" | "B.Cu" | …
    float width = 0.0f;        // mm
    std::string kind = "segment";  // "segment" | "arc"
    EcadPt p1, p2;             // segment endpoints (world)
    EcadPt center;             // arc center (world); valid for kind=="arc"
    float a0 = 0.0f, a1 = 0.0f;  // arc sweep (deg CCW)
};

struct EcadVia {
    std::string id;            // "v1"
    std::string net;
    EcadPt at;                 // world centre
    float drill = 0.0f, diameter = 0.0f;
    std::vector<std::string> layers;  // copper layers spanned
    std::string type = "through";
};

struct EcadZone {
    std::string id;            // "z1"
    std::string net;
    std::string layer;         // copper layer
    std::vector<EcadPt> polygon;  // world outline
    std::string fill = "solid";
};

// One straight outline segment on Edge.Cuts. Arcs/circles are flattened to short polylines on parse
// so the canvas only ever draws line lists (keeps the renderer trivial).
struct EcadOutlineSeg { EcadPt p1, p2; };

// A copper-layer descriptor parsed from the stackup, with a deterministic display index used to key
// the canvas's per-layer color (palette layer roles cycle by this index).
struct EcadLayer {
    std::string id;            // "F.Cu" / "In1.Cu" / "B.Cu"
    int copperIndex = 0;       // 0 = F.Cu (top), N-1 = B.Cu (bottom); -1 for tech layers
    bool visible = true;       // per-layer visibility toggle (owned by the panel, mirrored here)
};

// =====================================================================================
// Compile results (netlist / ERC / DRC / ratsnest)
// =====================================================================================

struct EcadAirwire { std::string net; EcadPt p1, p2; };  // unrouted ratsnest segment

// ERC / DRC violation (unified; ERC carries a single xy, DRC carries locs[] — we keep the first loc).
struct EcadViolation {
    std::string rule;          // "pin_not_connected" / "clearance" / …
    std::string severity;      // "error" | "warning"
    std::string net;           // "" if none
    std::string message;
    bool hasLoc = false;
    EcadPt loc;                // first reported location (world); for "zoom to violation"
    std::string layer;         // DRC loc layer; "" for ERC
};

// A net summary derived from compile.netlist.nets (sch) or doc.nets (pcb).
struct EcadNet {
    std::string name;
    int pinCount = 0;          // sch: number of pins on this net
};

// =====================================================================================
// EcadMirror
// =====================================================================================

class EcadMirror {
public:
    enum class Kind { None, Sch, Pcb };

    // Rebuild every parsed vector from a worker `ecad_tree` / ops-response payload. Accepts the
    // FULL response object ({revision, kind, tree, compile, …}); tolerant of missing sub-objects so
    // it can be fed a half-built response. Clears prior state first. Cheap enough to call on every
    // successful mutation.
    void loadFromTree(const nlohmann::json& resp);

    // ---- top-level state ----
    Kind        kind()     const { return kind_; }
    bool        isSch()    const { return kind_ == Kind::Sch; }
    bool        isPcb()    const { return kind_ == Kind::Pcb; }
    int         revision() const { return revision_; }
    bool        hasDoc()   const { return kind_ != Kind::None; }
    const std::string& docId() const { return docId_; }
    void        setDocId(std::string id) { docId_ = std::move(id); }
    const std::string& name() const { return name_; }

    // ---- schematic accessors ----
    const std::vector<EcadSymbol>&   symbols()   const { return symbols_; }
    const std::vector<EcadWire>&     wires()      const { return wires_; }
    const std::vector<EcadJunction>& junctions()  const { return junctions_; }
    const std::vector<EcadLabel>&    labels()     const { return labels_; }
    const std::vector<EcadPower>&    powers()     const { return powers_; }

    // ---- board accessors ----
    const std::vector<EcadFootprint>& footprints() const { return footprints_; }
    const std::vector<EcadTrack>&     tracks()      const { return tracks_; }
    const std::vector<EcadVia>&       vias()        const { return vias_; }
    const std::vector<EcadZone>&      zones()       const { return zones_; }
    const std::vector<EcadOutlineSeg>& outline()    const { return outline_; }
    const std::vector<EcadLayer>&     layers()       const { return layers_; }

    // Tracks filtered to one copper layer (the canvas draws layer-by-layer back-to-front so the top
    // copper paints last). Rebuilds a small vector; cheap per layer per frame.
    std::vector<const EcadTrack*> tracksOnLayer(const std::string& layerId) const;

    // Per-layer visibility (mutated by the panel's layer toggles; the canvas honours it). Defaults to
    // visible. Unknown ids are treated as visible.
    bool layerVisible(const std::string& layerId) const;
    void setLayerVisible(const std::string& layerId, bool vis);

    // Per-net visibility (mutated by the panel's Nets eye toggles; the canvas hides that net's
    // tracks + ratsnest). Defaults to visible; unknown/empty net names are treated as visible.
    bool netVisible(const std::string& net) const;
    void setNetVisible(const std::string& net, bool vis);

    // ---- compile accessors (both kinds) ----
    const std::vector<EcadAirwire>&   ratsnest()  const { return ratsnest_; }
    const std::vector<EcadViolation>& erc()       const { return erc_; }
    const std::vector<EcadViolation>& drc()       const { return drc_; }
    const std::vector<EcadNet>&       nets()      const { return nets_; }
    int  unroutedCount() const { return (int)ratsnest_.size(); }

    // ---- world bbox of everything drawn (for "zoom to fit"); returns false when empty ----
    bool worldBounds(EcadPt& outMin, EcadPt& outMax) const;

    // ---- transform helpers (PORT of engine/ecad/schema.py — public so tests/picking can reuse) ----
    // local point -> world: mirror, then rotate CCW by angle(deg), then translate by pos.
    static EcadPt localToWorld(const EcadPt& p, const EcadPt& pos, float angleDeg,
                               const std::string& mirror);

private:
    void parseSch(const nlohmann::json& tree, const nlohmann::json& compile);
    void parsePcb(const nlohmann::json& tree, const nlohmann::json& compile);
    void parseCompileShared(const nlohmann::json& compile);

    Kind kind_ = Kind::None;
    int  revision_ = -1;
    std::string docId_;
    std::string name_;

    // sch
    std::vector<EcadSymbol>   symbols_;
    std::vector<EcadWire>     wires_;
    std::vector<EcadJunction> junctions_;
    std::vector<EcadLabel>    labels_;
    std::vector<EcadPower>    powers_;

    // pcb
    std::vector<EcadFootprint>  footprints_;
    std::vector<EcadTrack>      tracks_;
    std::vector<EcadVia>        vias_;
    std::vector<EcadZone>       zones_;
    std::vector<EcadOutlineSeg> outline_;
    std::vector<EcadLayer>      layers_;
    std::unordered_map<std::string, bool> layerVis_;  // id -> visible (panel-owned)
    std::unordered_map<std::string, bool> netVis_;    // net name -> visible (panel-owned)

    // compile
    std::vector<EcadAirwire>   ratsnest_;
    std::vector<EcadViolation> erc_;
    std::vector<EcadViolation> drc_;
    std::vector<EcadNet>       nets_;
};

}  // namespace ui
