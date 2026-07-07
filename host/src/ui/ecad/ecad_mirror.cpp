// AI-ECAD UI scaffold (Wave D) — NOT yet build-verified; see docs/ECAD_UI_INTEGRATION.md
#include "ecad_mirror.h"

#include <algorithm>
#include <cmath>

namespace ui {

namespace {

constexpr float kPi = 3.14159265358979323846f;

// ---- tolerant JSON readers (mirror app_state.h jstr/jnum style, local to this TU) ----
std::string jstr(const nlohmann::json& o, const char* k, const char* dflt = "") {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string(dflt);
}
double jnum(const nlohmann::json& o, const char* k, double dflt = 0.0) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : dflt;
}
bool jbool(const nlohmann::json& o, const char* k, bool dflt = false) {
    auto it = o.find(k);
    return (it != o.end() && it->is_boolean()) ? it->get<bool>() : dflt;
}
const nlohmann::json& jarr(const nlohmann::json& o, const char* k) {
    static const nlohmann::json kEmpty = nlohmann::json::array();
    auto it = o.find(k);
    return (it != o.end() && it->is_array()) ? *it : kEmpty;
}
const nlohmann::json& jobj(const nlohmann::json& o, const char* k) {
    static const nlohmann::json kEmpty = nlohmann::json::object();
    auto it = o.find(k);
    return (it != o.end() && it->is_object()) ? *it : kEmpty;
}

// Read a [x,y] pair (tolerant of arrays shorter than 2 / non-numeric entries).
EcadPt jpt(const nlohmann::json& v) {
    EcadPt p;
    if (v.is_array() && v.size() >= 2 && v[0].is_number() && v[1].is_number()) {
        p.x = (float)v[0].get<double>();
        p.y = (float)v[1].get<double>();
    }
    return p;
}
EcadPt jptKey(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end()) ? jpt(*it) : EcadPt{};
}

std::vector<std::string> jstrList(const nlohmann::json& v) {
    std::vector<std::string> out;
    if (v.is_array())
        for (const auto& e : v)
            if (e.is_string()) out.push_back(e.get<std::string>());
    return out;
}

}  // namespace

// ---- transform port of engine/ecad/schema.py::local_to_world ----
// mirror (x|y), THEN rotate CCW by angleDeg, THEN translate by pos. Matches schema.py exactly so
// pins/pads land where the netlist/DRC expect them.
EcadPt EcadMirror::localToWorld(const EcadPt& p, const EcadPt& pos, float angleDeg,
                                const std::string& mirror) {
    float x = p.x, y = p.y;
    if (mirror == "x") x = -x;
    else if (mirror == "y") y = -y;
    const float r = angleDeg * kPi / 180.0f;
    const float c = std::cos(r), s = std::sin(r);
    const float rx = x * c - y * s;
    const float ry = x * s + y * c;
    return EcadPt{rx + pos.x, ry + pos.y};
}

void EcadMirror::loadFromTree(const nlohmann::json& resp) {
    // Clear parsed geometry but keep layerVis_/docId_ across rebuilds (see reset() note).
    revision_ = -1;
    name_.clear();
    symbols_.clear(); wires_.clear(); junctions_.clear(); labels_.clear(); powers_.clear();
    footprints_.clear(); tracks_.clear(); vias_.clear(); zones_.clear(); outline_.clear();
    layers_.clear();
    ratsnest_.clear(); erc_.clear(); drc_.clear(); nets_.clear();

    if (!resp.is_object()) { kind_ = Kind::None; return; }

    revision_ = (int)jnum(resp, "revision", -1);
    const std::string kindStr = jstr(resp, "kind");
    const nlohmann::json& tree    = jobj(resp, "tree");
    const nlohmann::json& compile = jobj(resp, "compile");
    name_ = jstr(tree, "name");

    // Prefer the explicit kind field; fall back to the doc's schema tag.
    const std::string schemaTag = jstr(tree, "schema");
    if (kindStr == "sch" || schemaTag == "aicad.sch/1") {
        kind_ = Kind::Sch;
        parseSch(tree, compile);
    } else if (kindStr == "pcb" || schemaTag == "aicad.pcb/1") {
        kind_ = Kind::Pcb;
        parsePcb(tree, compile);
    } else {
        kind_ = Kind::None;
    }
    parseCompileShared(compile);
}

// ---- schematic --------------------------------------------------------------------------------

void EcadMirror::parseSch(const nlohmann::json& tree, const nlohmann::json& compile) {
    const nlohmann::json& libCache = jobj(tree, "lib_cache");

    // by_pin map: "<symId>.<pinNumber>" -> net name (compile.netlist.by_pin). Tolerant of absence.
    const nlohmann::json& netlist = jobj(compile, "netlist");
    const nlohmann::json& byPin   = jobj(netlist, "by_pin");

    auto parseGraphic = [](const nlohmann::json& g) -> EcadSymGraphic {
        EcadSymGraphic out;
        const std::string t = jstr(g, "type", "line");
        if (t == "rect") {
            out.kind = EcadSymGraphic::Rect;
            out.pts = {jptKey(g, "p1"), jptKey(g, "p2")};
        } else if (t == "circle") {
            out.kind = EcadSymGraphic::Circle;
            out.pts = {jptKey(g, "center")};
            out.radius = (float)jnum(g, "radius");
        } else if (t == "arc") {
            out.kind = EcadSymGraphic::Arc;
            out.pts = {jptKey(g, "center")};
            out.radius = (float)jnum(g, "radius");
            out.a0 = (float)jnum(g, "a0");
            out.a1 = (float)jnum(g, "a1");
        } else if (t == "polyline") {
            out.kind = EcadSymGraphic::Polyline;
            for (const auto& p : jarr(g, "pts")) out.pts.push_back(jpt(p));
        } else {  // "line" and anything unknown -> a 2-point segment
            out.kind = EcadSymGraphic::Line;
            out.pts = {jptKey(g, "p1"), jptKey(g, "p2")};
        }
        return out;
    };

    for (const auto& s : jarr(tree, "symbols")) {
        EcadSymbol sym;
        sym.id     = jstr(s, "id");
        sym.libid  = jstr(s, "libid");
        sym.ref    = jstr(s, "ref");
        sym.value  = jstr(s, "value");
        sym.pos    = jptKey(s, "pos");
        sym.angle  = (float)jnum(s, "angle");
        sym.mirror = jstr(s, "mirror", "none");
        sym.footprint = jstr(s, "footprint");  // "" if unassigned
        sym.dnp    = jbool(s, "dnp");
        {  // sim_params: keep the raw dict (free-form); leave as null if missing/non-object.
            auto spIt = s.find("sim_params");
            if (spIt != s.end() && spIt->is_object()) sym.simParams = *spIt;
        }
        const int unit = (int)jnum(s, "unit", 1);

        // Resolve the lib_cache SymbolDef for body graphics + pins.
        auto defIt = libCache.find(sym.libid);
        if (defIt != libCache.end() && defIt->is_object()) {
            const nlohmann::json& sdef = *defIt;
            sym.power = jbool(sdef, "power");
            for (const auto& g : jarr(sdef, "graphics"))
                sym.body.push_back(parseGraphic(g));

            for (const auto& pin : jarr(sdef, "pins")) {
                const int pinUnit = (int)jnum(pin, "unit", 1);
                if (pinUnit != unit && pinUnit != 0) continue;  // multi-unit filter (schema.py)
                EcadPin ep;
                ep.number = jstr(pin, "number");
                ep.name   = jstr(pin, "name");
                ep.type   = jstr(pin, "type", "passive");

                // pin root in symbol-local frame, then placed by the symbol transform.
                const EcadPt rootLocal = jptKey(pin, "pos");
                const float pinLen   = (float)jnum(pin, "length", 2.54);
                const float pinAngle = (float)jnum(pin, "angle");
                const float pa = pinAngle * kPi / 180.0f;
                // tip = root + length*dir(angle) in local frame (schema.py::pin_world).
                const EcadPt tipLocal{rootLocal.x + pinLen * std::cos(pa),
                                      rootLocal.y + pinLen * std::sin(pa)};
                ep.root = localToWorld(rootLocal, sym.pos, sym.angle, sym.mirror);
                ep.tip  = localToWorld(tipLocal,  sym.pos, sym.angle, sym.mirror);

                // net from compile.netlist.by_pin keyed "<symId>.<pinNumber>".
                const std::string key = sym.id + "." + ep.number;
                auto netIt = byPin.find(key);
                if (netIt != byPin.end() && netIt->is_string()) ep.net = netIt->get<std::string>();
                sym.pins.push_back(std::move(ep));
            }
        }
        symbols_.push_back(std::move(sym));
    }

    for (const auto& w : jarr(tree, "wires")) {
        EcadWire ew;
        ew.id = jstr(w, "id");
        ew.bus = jbool(w, "bus");
        for (const auto& p : jarr(w, "pts")) ew.pts.push_back(jpt(p));
        if (ew.pts.size() >= 2) wires_.push_back(std::move(ew));
    }
    for (const auto& j : jarr(tree, "junctions"))
        junctions_.push_back({jstr(j, "id"), jptKey(j, "pos")});
    for (const auto& l : jarr(tree, "labels"))
        labels_.push_back({jstr(l, "id"), jstr(l, "text"), jstr(l, "scope", "local"),
                           jptKey(l, "pos"), (float)jnum(l, "angle")});
    for (const auto& p : jarr(tree, "power"))
        powers_.push_back({jstr(p, "id"), jstr(p, "net"), jstr(p, "symbol"), jptKey(p, "pos")});

    // Net summaries from compile.netlist.nets ({name: [members…]}).
    const nlohmann::json& nets = jobj(netlist, "nets");
    for (auto it = nets.begin(); it != nets.end(); ++it) {
        EcadNet n;
        n.name = it.key();
        n.pinCount = it.value().is_array() ? (int)it.value().size() : 0;
        nets_.push_back(std::move(n));
    }
    std::sort(nets_.begin(), nets_.end(),
              [](const EcadNet& a, const EcadNet& b) { return a.name < b.name; });
}

// ---- board ------------------------------------------------------------------------------------

void EcadMirror::parsePcb(const nlohmann::json& tree, const nlohmann::json& compile) {
    (void)compile;

    // Stackup -> ordered copper layer list (F.Cu … B.Cu). Mirrors schema.py::copper_layer_ids.
    const nlohmann::json& stackup = jobj(tree, "stackup");
    int nCu = (int)jnum(stackup, "copper_layers", 2);
    if (nCu < 1) nCu = 1;
    std::vector<std::string> copperIds;
    if (nCu == 1) {
        copperIds = {"F.Cu"};
    } else {
        copperIds.push_back("F.Cu");
        for (int i = 1; i < nCu - 1; ++i) copperIds.push_back("In" + std::to_string(i) + ".Cu");
        copperIds.push_back("B.Cu");
    }
    for (int i = 0; i < (int)copperIds.size(); ++i) {
        EcadLayer ly;
        ly.id = copperIds[i];
        ly.copperIndex = i;
        ly.visible = layerVisible(ly.id);  // honour any prior toggle
        layers_.push_back(std::move(ly));
    }

    // Outline shapes -> flat line list (arcs/circles flattened).
    const nlohmann::json& outline = jobj(tree, "outline");
    auto flattenArc = [&](EcadPt c, float r, float a0, float a1) {
        const int seg = 24;
        float span = a1 - a0;
        EcadPt prev;
        for (int i = 0; i <= seg; ++i) {
            const float a = (a0 + span * (float)i / (float)seg) * kPi / 180.0f;
            EcadPt p{c.x + r * std::cos(a), c.y + r * std::sin(a)};
            if (i > 0) outline_.push_back({prev, p});
            prev = p;
        }
    };
    for (const auto& shp : jarr(outline, "shapes")) {
        const std::string t = jstr(shp, "type", "line");
        if (t == "line") {
            outline_.push_back({jptKey(shp, "p1"), jptKey(shp, "p2")});
        } else if (t == "arc") {
            flattenArc(jptKey(shp, "center"), (float)jnum(shp, "radius"),
                       (float)jnum(shp, "a0"), (float)jnum(shp, "a1"));
        } else if (t == "circle") {
            flattenArc(jptKey(shp, "center"), (float)jnum(shp, "radius"), 0.0f, 360.0f);
        }
    }

    // Footprints + world pads.
    for (const auto& fp : jarr(tree, "footprints")) {
        EcadFootprint efp;
        efp.id    = jstr(fp, "id");
        efp.fpid  = jstr(fp, "fpid");
        efp.ref   = jstr(fp, "ref");
        efp.value = jstr(fp, "value");
        efp.pos   = jptKey(fp, "pos");
        efp.angle = (float)jnum(fp, "angle");
        efp.side  = jstr(fp, "side", "top");
        const std::string fpMirror = (efp.side == "bottom") ? "x" : "none";  // schema.py::pad_world

        bool haveBox = false;
        EcadPt bmin{}, bmax{};
        auto growBox = [&](EcadPt p) {
            if (!haveBox) { bmin = bmax = p; haveBox = true; return; }
            bmin.x = std::min(bmin.x, p.x); bmin.y = std::min(bmin.y, p.y);
            bmax.x = std::max(bmax.x, p.x); bmax.y = std::max(bmax.y, p.y);
        };

        for (const auto& pad : jarr(fp, "pads")) {
            EcadPad ep;
            ep.number = jstr(pad, "number");
            const EcadPt localPos = jptKey(pad, "pos");
            ep.pos = localToWorld(localPos, efp.pos, efp.angle, fpMirror);
            ep.angle = efp.angle + (float)jnum(pad, "angle");
            ep.shape = jstr(pad, "shape", "rect");
            const EcadPt size = jptKey(pad, "size");
            ep.w = size.x; ep.h = size.y;
            ep.roundrectRatio = (float)jnum(pad, "roundrect_ratio");
            ep.type   = jstr(pad, "type", "smd");
            ep.layers = jstrList(jarr(pad, "layers"));
            ep.net    = jstr(pad, "net");
            auto drillIt = pad.find("drill");
            if (drillIt != pad.end() && drillIt->is_object()) {
                ep.hasDrill = true;
                ep.drillDia = (float)jnum(*drillIt, "diameter");
            }
            growBox(ep.pos);
            efp.pads.push_back(std::move(ep));
        }
        // Courtyard: pad bbox grown by a small margin (no per-graphic courtyard parse in v1 scaffold).
        if (haveBox) {
            const float m = 0.25f;  // mm margin
            efp.courtyardMin = {bmin.x - m, bmin.y - m};
            efp.courtyardMax = {bmax.x + m, bmax.y + m};
            efp.hasCourtyard = true;
        }
        footprints_.push_back(std::move(efp));
    }

    // Tracks (already world coords).
    for (const auto& t : jarr(tree, "tracks")) {
        EcadTrack et;
        et.id    = jstr(t, "id");
        et.net   = jstr(t, "net");
        et.layer = jstr(t, "layer", "F.Cu");
        et.width = (float)jnum(t, "width");
        et.kind  = jstr(t, "kind", "segment");
        et.p1    = jptKey(t, "p1");
        et.p2    = jptKey(t, "p2");
        if (et.kind == "arc") {
            et.center = jptKey(t, "center");
            et.a0 = (float)jnum(t, "a0");
            et.a1 = (float)jnum(t, "a1");
        }
        tracks_.push_back(std::move(et));
    }

    for (const auto& v : jarr(tree, "vias")) {
        EcadVia ev;
        ev.id       = jstr(v, "id");
        ev.net      = jstr(v, "net");
        ev.at       = jptKey(v, "at");
        ev.drill    = (float)jnum(v, "drill");
        ev.diameter = (float)jnum(v, "diameter");
        ev.layers   = jstrList(jarr(v, "layers"));
        ev.type     = jstr(v, "type", "through");
        vias_.push_back(std::move(ev));
    }

    for (const auto& z : jarr(tree, "zones")) {
        EcadZone ez;
        ez.id    = jstr(z, "id");
        ez.net   = jstr(z, "net");
        ez.layer = jstr(z, "layer", "F.Cu");
        ez.fill  = jstr(z, "fill", "solid");
        for (const auto& p : jarr(z, "polygon")) ez.polygon.push_back(jpt(p));
        zones_.push_back(std::move(ez));
    }

    // Net registry: doc.nets ([{name, class}]).
    for (const auto& n : jarr(tree, "nets")) {
        EcadNet en;
        en.name = jstr(n, "name");
        if (!en.name.empty()) nets_.push_back(std::move(en));
    }
}

// ---- shared compile ---------------------------------------------------------------------------

void EcadMirror::parseCompileShared(const nlohmann::json& compile) {
    if (!compile.is_object()) return;

    // ERC (sch): [{rule, severity, net, pins, xy, message}].
    for (const auto& v : jarr(compile, "erc")) {
        EcadViolation ev;
        ev.rule     = jstr(v, "rule");
        ev.severity = jstr(v, "severity", "warning");
        ev.net      = jstr(v, "net");
        ev.message  = jstr(v, "message");
        auto xyIt = v.find("xy");
        if (xyIt != v.end() && xyIt->is_array() && xyIt->size() >= 2) {
            ev.loc = jpt(*xyIt);
            ev.hasLoc = true;
        }
        erc_.push_back(std::move(ev));
    }

    // DRC (pcb): [{rule, severity, locs:[[x,y,layer]], items, message}].
    for (const auto& v : jarr(compile, "drc")) {
        EcadViolation ev;
        ev.rule     = jstr(v, "rule");
        ev.severity = jstr(v, "severity", "warning");
        ev.message  = jstr(v, "message");
        const nlohmann::json& locs = jarr(v, "locs");
        if (!locs.empty() && locs[0].is_array() && locs[0].size() >= 2) {
            ev.loc.x = (float)locs[0][0].get<double>();
            ev.loc.y = (float)locs[0][1].get<double>();
            ev.hasLoc = true;
            if (locs[0].size() >= 3 && locs[0][2].is_string())
                ev.layer = locs[0][2].get<std::string>();
        }
        drc_.push_back(std::move(ev));
    }

    // Ratsnest (pcb): [{net, p1:[x,y], p2:[x,y]}].
    for (const auto& r : jarr(compile, "ratsnest")) {
        EcadAirwire aw;
        aw.net = jstr(r, "net");
        aw.p1  = jptKey(r, "p1");
        aw.p2  = jptKey(r, "p2");
        ratsnest_.push_back(std::move(aw));
    }
}

// ---- queries ----------------------------------------------------------------------------------

std::vector<const EcadTrack*> EcadMirror::tracksOnLayer(const std::string& layerId) const {
    std::vector<const EcadTrack*> out;
    for (const auto& t : tracks_)
        if (t.layer == layerId) out.push_back(&t);
    return out;
}

bool EcadMirror::layerVisible(const std::string& layerId) const {
    auto it = layerVis_.find(layerId);
    return it == layerVis_.end() ? true : it->second;
}

void EcadMirror::setLayerVisible(const std::string& layerId, bool vis) {
    layerVis_[layerId] = vis;
    for (auto& ly : layers_)
        if (ly.id == layerId) ly.visible = vis;
}

bool EcadMirror::netVisible(const std::string& net) const {
    if (net.empty()) return true;
    auto it = netVis_.find(net);
    return it == netVis_.end() ? true : it->second;
}

void EcadMirror::setNetVisible(const std::string& net, bool vis) {
    if (!net.empty()) netVis_[net] = vis;
}

bool EcadMirror::layerColor(const std::string& layerId, float out[3]) const {
    auto it = layerColor_.find(layerId);
    if (it == layerColor_.end()) return false;
    out[0] = it->second[0];
    out[1] = it->second[1];
    out[2] = it->second[2];
    return true;
}

void EcadMirror::setLayerColor(const std::string& layerId, float r, float g, float b) {
    if (!layerId.empty()) layerColor_[layerId] = {r, g, b};
}

bool EcadMirror::worldBounds(EcadPt& outMin, EcadPt& outMax) const {
    bool have = false;
    auto grow = [&](EcadPt p) {
        if (!have) { outMin = outMax = p; have = true; return; }
        outMin.x = std::min(outMin.x, p.x); outMin.y = std::min(outMin.y, p.y);
        outMax.x = std::max(outMax.x, p.x); outMax.y = std::max(outMax.y, p.y);
    };
    // sch
    for (const auto& s : symbols_) {
        grow(s.pos);
        for (const auto& p : s.pins) { grow(p.root); grow(p.tip); }
    }
    for (const auto& w : wires_) for (const auto& p : w.pts) grow(p);
    for (const auto& l : labels_) grow(l.pos);
    for (const auto& p : powers_) grow(p.pos);
    // pcb
    for (const auto& o : outline_) { grow(o.p1); grow(o.p2); }
    for (const auto& f : footprints_) {
        grow(f.pos);
        for (const auto& pad : f.pads) grow(pad.pos);
    }
    for (const auto& t : tracks_) { grow(t.p1); grow(t.p2); }
    for (const auto& v : vias_) grow(v.at);
    for (const auto& z : zones_) for (const auto& p : z.polygon) grow(p);
    return have;
}

}  // namespace ui
