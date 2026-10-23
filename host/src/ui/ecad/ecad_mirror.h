// EcadMirror — a read-only, in-process mirror of one open ECAD document, built from the JSON the
// worker returns for `ecad_tree` / `ecad_apply_ops` (shape: {revision, kind, tree, compile}; see
// engine/ecad/session.py::tree and contracts/ecad/ecad_rpc.md §1). It is the ECAD analogue of
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

#include <array>
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
    std::string type;          // "passive" | "power_in" | … (drives ERC)
    std::string shape;         // "line" | "inverted" | "clock" | "inverted_clock" | …
    int  unit = 1;             // owning unit of a multi-unit symbol
    bool hidden = false;       // hidden pins (power rails on many ICs) — drawn dimmed
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
    std::string sheet = "sh1"; // owning sheet id
    EcadPt pos;                // placement origin
    float angle = 0.0f;        // deg CCW
    std::string mirror = "none";  // "none" | "x" | "y"
    bool power = false;        // lib_cache SymbolDef.power (VCC/GND etc.)
    bool generated = false;    // def was fabricated because the libid is unknown — draw a warn badge
    int  unit = 1;             // which unit of a multi-unit part this instance is
    int  unitCount = 1;        // total units in the definition
    std::string footprint;     // assigned footprint id ("Resistor_SMD:R_0603"); "" if unassigned
    std::string datasheet;     // first-class Datasheet property
    bool dnp = false;          // do-not-populate (excluded from BOM/assembly)
    bool inBom = true;
    bool onBoard = true;
    nlohmann::json fields;     // user fields (MPN, Tolerance, …); object, may be empty
    nlohmann::json simParams;  // free-form sim_params dict (raw JSON; null/absent if none)
    std::vector<EcadSymGraphic> body;  // local-frame body graphics (transform at draw time)
    std::vector<EcadPin> pins;         // world-space pins (already filtered to this unit)
};

// Orthogonal wire polyline.
struct EcadWire {
    std::string id;            // "w1"
    std::string sheet = "sh1";
    std::vector<EcadPt> pts;   // >= 2 points
    bool bus = false;          // legacy per-wire bus flag (distinct from EcadBus)
    std::string net;           // resolved net (compile.netlist.by_object), "" if unresolved
};

// A first-class bus polyline (doc `buses[]`), separate from the legacy wire.bus flag.
struct EcadBus {
    std::string id;            // "b1"
    std::string sheet = "sh1";
    std::string name;          // "D[0..7]"
    std::vector<EcadPt> pts;
    std::vector<std::string> members;  // expanded members from the compile ("D0","D1",…)
};

// A bus entry: the short diagonal that taps one member out of a bus.
struct EcadBusEntry {
    std::string id;            // "be1"
    std::string sheet = "sh1";
    EcadPt pos;                // wire-side endpoint
    EcadPt size{2.54f, 2.54f}; // delta to the bus-side endpoint
    std::string bus;           // owning bus id ("" = resolve geometrically)
    std::string member;        // explicit member name ("" = resolve from the attached label)
    std::string net;           // resolved member net
};

struct EcadJunction  { std::string id; std::string sheet = "sh1"; EcadPt pos; std::string net; };
struct EcadLabel     { std::string id; std::string sheet = "sh1"; std::string text;
                       std::string scope = "local"; EcadPt pos; float angle = 0.0f;
                       std::string net; };
struct EcadPower     { std::string id; std::string sheet = "sh1"; std::string net;
                       std::string symbol; EcadPt pos; float angle = 0.0f; };
// An explicit no-connect flag placed on a pin — the AUTHORITATIVE X mark. Never infer
// this from an ERC violation: a correct no-connect suppresses `pin_not_connected`, so
// deriving the glyph from ERC draws it in exactly the wrong places.
struct EcadNoConnect { std::string id; std::string sheet = "sh1"; EcadPt pos; };
struct EcadText      { std::string id; std::string sheet = "sh1"; std::string text; EcadPt pos;
                       float size = 1.27f; float angle = 0.0f; };

// A free graphic on the sheet (notes, boxes, dividers) — doc `graphics[]`.
struct EcadGraphic {
    enum Kind { Line, Rect, Circle, Arc, Polyline } kind = Line;
    std::string id;            // "g1"
    std::string sheet = "sh1";
    std::vector<EcadPt> pts;   // rect/line: [p1,p2]; circle/arc: [center]; polyline: all
    float radius = 0.0f;
    float a0 = 0.0f, a1 = 0.0f;
    float width = 0.15f;
    bool  filled = false;
};

// One named connection point on a sheet symbol (the parent-side port of a child sheet).
struct EcadSheetPin {
    std::string name;
    EcadPt pos;                // world
    std::string shape = "bidirectional";
    std::string net;
};

// The parent-side instantiation of a child sheet — drawn as a named box with its pins.
struct EcadSheetSymbol {
    std::string id;            // "ss1"
    std::string sheet = "sh1"; // the PARENT sheet this box sits on
    std::string child;         // the child sheet id it instantiates
    std::string name;          // display name (child sheet's name)
    EcadPt pos;                // top-left
    EcadPt size{25.4f, 25.4f};
    std::vector<EcadSheetPin> pins;
};

// One sheet (page) of a schematic. A document always has at least one.
struct EcadSheet {
    std::string id;            // "sh1"
    std::string name;          // "Root"
    float w = 297.0f, h = 210.0f;  // page size in mm (A4 landscape default)
};

// Sheet-frame metadata rendered into the title block and carried into plots.
struct EcadTitleBlock {
    std::string title, company, rev, date;
    std::string comment1, comment2, comment3, comment4;
    std::string paper = "A4";
};

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

// ERC / DRC violation (unified; ERC carries a single xy, DRC carries locs[]).
struct EcadViolation {
    std::string rule;          // "pin_not_connected" / "clearance" / …
    std::string severity;      // "error" | "warning"
    std::string net;           // "" if none
    std::string message;
    bool hasLoc = false;
    EcadPt loc;                // first reported location (world); for "zoom to violation"
    // ALL reported DRC locations (world); locs[0] duplicates loc. A clearance-style
    // violation is BETWEEN two shapes, so it reports two — the canvas marks both and
    // joins them with a hairline. Empty for ERC (single-xy) violations.
    std::vector<EcadPt> locs;
    std::string layer;         // DRC loc layer; "" for ERC
};

// A net summary derived from compile.netlist (sch) or doc.nets (pcb).
struct EcadNet {
    std::string name;
    int pinCount = 0;                  // sch: number of pins on this net
    std::vector<std::string> sheets;   // sheets this net appears on
    std::string bus;                   // owning bus name when this net is a bus member
    bool driven = false;               // compile said the net has a driver / power source
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
    const std::vector<EcadSheet>&       sheets()       const { return sheets_; }
    const std::vector<EcadSymbol>&      symbols()      const { return symbols_; }
    const std::vector<EcadWire>&        wires()        const { return wires_; }
    const std::vector<EcadBus>&         buses()        const { return buses_; }
    const std::vector<EcadBusEntry>&    busEntries()   const { return busEntries_; }
    const std::vector<EcadJunction>&    junctions()    const { return junctions_; }
    const std::vector<EcadLabel>&       labels()       const { return labels_; }
    const std::vector<EcadPower>&       powers()       const { return powers_; }
    const std::vector<EcadNoConnect>&   noConnects()   const { return noConnects_; }
    const std::vector<EcadText>&        texts()        const { return texts_; }
    const std::vector<EcadGraphic>&     graphics()     const { return graphics_; }
    const std::vector<EcadSheetSymbol>& sheetSymbols() const { return sheetSymbols_; }
    const EcadTitleBlock&               titleBlock()   const { return titleBlock_; }

    // Document edit grid in mm (schema `units.grid`, default 2.54). Every placement and
    // move MUST snap to this, not to a hard-coded constant.
    float grid() const { return grid_; }

    // The sheet the canvas is currently showing. Entities carry a `sheet` id and MUST be
    // filtered by it — a hierarchical document otherwise draws every page on top of
    // itself. Defaults to the first sheet; setting an unknown id is ignored.
    const std::string& activeSheet() const { return activeSheet_; }
    void setActiveSheet(const std::string& id);
    // Convenience: is this entity on the sheet currently being shown?
    bool onActiveSheet(const std::string& sheetId) const {
        return activeSheet_.empty() || sheetId.empty() || sheetId == activeSheet_;
    }

    // Resolved net for one document entity id (wire/bus/bus entry/junction/label/power),
    // from compile.netlist.by_object. Empty when the compile did not resolve it.
    const std::string& netOfObject(const std::string& objId) const;

    // ---- board accessors ----
    const std::vector<EcadFootprint>& footprints() const { return footprints_; }
    const std::vector<EcadTrack>&     tracks()      const { return tracks_; }
    const std::vector<EcadVia>&       vias()        const { return vias_; }
    const std::vector<EcadZone>&      zones()       const { return zones_; }
    const std::vector<EcadOutlineSeg>& outline()    const { return outline_; }
    // Authored board content, including graphics that are not yet drawn by the canvas.
    // Stackup/rule defaults alone do not make the automatic starter a design.
    bool hasBoardContent() const { return isPcb() && hasBoardContent_; }
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

    // Per-layer color override (RGB 0..1; panel-owned). When no override is set for a layer id,
    // callers fall back to the copperIndex-derived default (layerSwatch / copperColor). Returns
    // true and fills out[3] when an override exists; false otherwise.
    bool layerColor(const std::string& layerId, float out[3]) const;
    void setLayerColor(const std::string& layerId, float r, float g, float b);

    // ---- compile accessors (both kinds) ----
    const std::vector<EcadAirwire>&   ratsnest()  const { return ratsnest_; }
    const std::vector<EcadViolation>& erc()       const { return erc_; }
    const std::vector<EcadViolation>& drc()       const { return drc_; }
    const std::vector<EcadNet>&       nets()      const { return nets_; }
    int  unroutedCount() const { return (int)ratsnest_.size(); }

    // ---- world bbox of everything drawn on the ACTIVE sheet (for "zoom to fit").
    // Covers symbol bodies and pin tips, wires, buses, bus entries, junctions, labels,
    // power ports, no-connects, texts, graphics, sheet symbols and located ERC markers.
    // Returns false when the sheet is empty.
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
    std::vector<EcadSheet>       sheets_;
    std::vector<EcadSymbol>      symbols_;
    std::vector<EcadWire>        wires_;
    std::vector<EcadBus>         buses_;
    std::vector<EcadBusEntry>    busEntries_;
    std::vector<EcadJunction>    junctions_;
    std::vector<EcadLabel>       labels_;
    std::vector<EcadPower>       powers_;
    std::vector<EcadNoConnect>   noConnects_;
    std::vector<EcadText>        texts_;
    std::vector<EcadGraphic>     graphics_;
    std::vector<EcadSheetSymbol> sheetSymbols_;
    EcadTitleBlock               titleBlock_;
    float                        grid_ = 2.54f;
    std::string                  activeSheet_;
    std::unordered_map<std::string, std::string> objectNet_;  // entity id -> net name

    // pcb
    std::vector<EcadFootprint>  footprints_;
    std::vector<EcadTrack>      tracks_;
    std::vector<EcadVia>        vias_;
    std::vector<EcadZone>       zones_;
    std::vector<EcadOutlineSeg> outline_;
    bool hasBoardContent_ = false;
    std::vector<EcadLayer>      layers_;
    std::unordered_map<std::string, bool> layerVis_;  // id -> visible (panel-owned)
    std::unordered_map<std::string, bool> netVis_;    // net name -> visible (panel-owned)
    std::unordered_map<std::string, std::array<float, 3>> layerColor_;  // id -> RGB override (panel-owned)

    // compile
    std::vector<EcadAirwire>   ratsnest_;
    std::vector<EcadViolation> erc_;
    std::vector<EcadViolation> drc_;
    std::vector<EcadNet>       nets_;
};

}  // namespace ui
