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
    // Clear parsed geometry but keep caller-owned visibility/color state and the active schematic
    // sheet candidate across rebuilds. parseSch() keeps that sheet only when it still exists.
    revision_ = -1;
    name_.clear();
    sheets_.clear(); symbols_.clear(); wires_.clear(); buses_.clear(); busEntries_.clear();
    junctions_.clear(); labels_.clear(); powers_.clear(); noConnects_.clear(); texts_.clear();
    graphics_.clear(); sheetSymbols_.clear();
    titleBlock_ = {};
    grid_ = 2.54f;
    objectNet_.clear();
    footprints_.clear(); tracks_.clear(); vias_.clear(); zones_.clear(); outline_.clear();
    layers_.clear();
    ratsnest_.clear(); erc_.clear(); drc_.clear(); nets_.clear();

    if (!resp.is_object()) {
        kind_ = Kind::None;
        activeSheet_.clear();
        return;
    }

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
        activeSheet_.clear();
        parsePcb(tree, compile);
    } else {
        kind_ = Kind::None;
        activeSheet_.clear();
    }
    parseCompileShared(compile);
}

// ---- schematic --------------------------------------------------------------------------------

void EcadMirror::parseSch(const nlohmann::json& tree, const nlohmann::json& compile) {
    const nlohmann::json& libCache = jobj(tree, "lib_cache");
    const nlohmann::json& netlist = jobj(compile, "netlist");
    const nlohmann::json& byPin = jobj(netlist, "by_pin");
    const nlohmann::json& byObject = jobj(netlist, "by_object");
    const nlohmann::json& compiledBuses = jobj(netlist, "buses");
    const nlohmann::json& netInfo = jobj(netlist, "net_info");

    for (auto it = byObject.begin(); it != byObject.end(); ++it)
        if (it.value().is_string()) objectNet_[it.key()] = it.value().get<std::string>();

    const nlohmann::json& units = jobj(tree, "units");
    grid_ = (float)jnum(units, "grid", 2.54);

    for (const auto& s : jarr(tree, "sheets")) {
        if (!s.is_object()) continue;
        EcadSheet sheet;
        sheet.id = jstr(s, "id");
        sheet.name = jstr(s, "name");
        auto sizeIt = s.find("size");
        if (sizeIt != s.end() && sizeIt->is_array() && sizeIt->size() >= 2 &&
            (*sizeIt)[0].is_number() && (*sizeIt)[1].is_number()) {
            const EcadPt size = jpt(*sizeIt);
            sheet.w = size.x;
            sheet.h = size.y;
        }
        sheets_.push_back(std::move(sheet));
    }
    if (sheets_.empty()) sheets_.push_back({"sh1", "Root", 297.0f, 210.0f});

    const bool activeStillExists =
        std::any_of(sheets_.begin(), sheets_.end(), [&](const EcadSheet& sheet) {
            return sheet.id == activeSheet_;
        });
    if (!activeStillExists) activeSheet_ = sheets_.front().id;

    const nlohmann::json& title = jobj(tree, "title_block");
    titleBlock_.title = jstr(title, "title");
    titleBlock_.company = jstr(title, "company");
    titleBlock_.rev = jstr(title, "rev");
    titleBlock_.date = jstr(title, "date");
    titleBlock_.comment1 = jstr(title, "comment1");
    titleBlock_.comment2 = jstr(title, "comment2");
    titleBlock_.comment3 = jstr(title, "comment3");
    titleBlock_.comment4 = jstr(title, "comment4");
    titleBlock_.paper = jstr(title, "paper", "A4");

    auto objectNet = [&](const std::string& id) -> std::string {
        const auto it = objectNet_.find(id);
        return it == objectNet_.end() ? std::string() : it->second;
    };

    auto parseGraphic = [](const nlohmann::json& g) -> EcadSymGraphic {
        EcadSymGraphic out;
        std::string t = jstr(g, "kind");
        if (t.empty()) t = jstr(g, "type", "line");
        if (t == "rect") {
            out.kind = EcadSymGraphic::Rect;
            const nlohmann::json& pts = jarr(g, "pts");
            out.pts = pts.size() >= 2 ? std::vector<EcadPt>{jpt(pts[0]), jpt(pts[1])}
                                      : std::vector<EcadPt>{jptKey(g, "p1"), jptKey(g, "p2")};
        } else if (t == "circle") {
            out.kind = EcadSymGraphic::Circle;
            const nlohmann::json& pts = jarr(g, "pts");
            out.pts = {pts.empty() ? jptKey(g, "center") : jpt(pts[0])};
            out.radius = (float)jnum(g, "radius");
        } else if (t == "arc") {
            out.kind = EcadSymGraphic::Arc;
            const nlohmann::json& pts = jarr(g, "pts");
            out.pts = {pts.empty() ? jptKey(g, "center") : jpt(pts[0])};
            out.radius = (float)jnum(g, "radius");
            out.a0 = (float)jnum(g, "a0");
            out.a1 = (float)jnum(g, "a1");
        } else if (t == "polyline") {
            out.kind = EcadSymGraphic::Polyline;
            for (const auto& p : jarr(g, "pts")) out.pts.push_back(jpt(p));
        } else {
            out.kind = EcadSymGraphic::Line;
            const nlohmann::json& pts = jarr(g, "pts");
            out.pts = pts.size() >= 2 ? std::vector<EcadPt>{jpt(pts[0]), jpt(pts[1])}
                                      : std::vector<EcadPt>{jptKey(g, "p1"), jptKey(g, "p2")};
        }
        return out;
    };

    for (const auto& s : jarr(tree, "symbols")) {
        if (!s.is_object()) continue;
        EcadSymbol sym;
        sym.id = jstr(s, "id");
        sym.libid = jstr(s, "libid");
        sym.ref = jstr(s, "ref");
        sym.value = jstr(s, "value");
        sym.sheet = jstr(s, "sheet", "sh1");
        sym.pos = jptKey(s, "pos");
        sym.angle = (float)jnum(s, "angle");
        sym.mirror = jstr(s, "mirror", "none");
        sym.unit = (int)jnum(s, "unit", 1);
        sym.footprint = jstr(s, "footprint");
        sym.datasheet = jstr(s, "datasheet");
        sym.dnp = jbool(s, "dnp");
        sym.inBom = jbool(s, "in_bom", true);
        sym.onBoard = jbool(s, "on_board", true);
        sym.fields = nlohmann::json::object();
        auto fieldsIt = s.find("fields");
        if (fieldsIt != s.end() && fieldsIt->is_object()) sym.fields = *fieldsIt;
        auto simIt = s.find("sim_params");
        if (simIt != s.end() && simIt->is_object()) sym.simParams = *simIt;

        auto defIt = libCache.find(sym.libid);
        if (defIt != libCache.end() && defIt->is_object()) {
            const nlohmann::json& sdef = *defIt;
            sym.power = jbool(sdef, "power");
            sym.generated = jbool(sdef, "generated");
            sym.unitCount = std::max(1, (int)jnum(sdef, "units_count", 1));
            for (const auto& g : jarr(sdef, "graphics")) {
                const int graphicUnit = (int)jnum(g, "unit", 0);
                if (graphicUnit != 0 && graphicUnit != sym.unit) continue;
                sym.body.push_back(parseGraphic(g));
            }

            for (const auto& pin : jarr(sdef, "pins")) {
                const int pinUnit = (int)jnum(pin, "unit", 1);
                if (pinUnit != sym.unit && pinUnit != 0) continue;
                EcadPin ep;
                ep.number = jstr(pin, "number");
                ep.name = jstr(pin, "name");
                ep.type = jstr(pin, "type", "passive");
                ep.shape = jstr(pin, "shape", "line");
                ep.unit = pinUnit;
                ep.hidden = jbool(pin, "hidden");

                const EcadPt rootLocal = jptKey(pin, "pos");
                const float pinLen = (float)jnum(pin, "length", 2.54);
                const float pinAngle = (float)jnum(pin, "angle");
                const float pa = pinAngle * kPi / 180.0f;
                const EcadPt tipLocal{rootLocal.x + pinLen * std::cos(pa),
                                      rootLocal.y + pinLen * std::sin(pa)};
                ep.root = localToWorld(rootLocal, sym.pos, sym.angle, sym.mirror);
                ep.tip = localToWorld(tipLocal, sym.pos, sym.angle, sym.mirror);

                const std::string key = sym.id + "." + ep.number;
                auto netIt = byPin.find(key);
                if (netIt != byPin.end() && netIt->is_string())
                    ep.net = netIt->get<std::string>();
                sym.pins.push_back(std::move(ep));
            }
        }
        symbols_.push_back(std::move(sym));
    }

    for (const auto& w : jarr(tree, "wires")) {
        if (!w.is_object()) continue;
        EcadWire wire;
        wire.id = jstr(w, "id");
        wire.sheet = jstr(w, "sheet", "sh1");
        wire.bus = jbool(w, "bus");
        wire.net = objectNet(wire.id);
        for (const auto& p : jarr(w, "pts")) wire.pts.push_back(jpt(p));
        if (wire.pts.size() >= 2) wires_.push_back(std::move(wire));
    }

    for (const auto& b : jarr(tree, "buses")) {
        if (!b.is_object()) continue;
        EcadBus bus;
        bus.id = jstr(b, "id");
        bus.sheet = jstr(b, "sheet", "sh1");
        bus.name = jstr(b, "name");
        for (const auto& p : jarr(b, "pts")) bus.pts.push_back(jpt(p));
        const auto compiledIt = compiledBuses.find(bus.name);
        if (compiledIt != compiledBuses.end()) {
            bus.members = jstrList(*compiledIt);
            if (bus.members.empty() && compiledIt->is_object())
                bus.members = jstrList(jarr(*compiledIt, "members"));
        }
        buses_.push_back(std::move(bus));
    }

    for (const auto& be : jarr(tree, "bus_entries")) {
        if (!be.is_object()) continue;
        EcadBusEntry entry;
        entry.id = jstr(be, "id");
        entry.sheet = jstr(be, "sheet", "sh1");
        entry.pos = jptKey(be, "pos");
        auto sizeIt = be.find("size");
        if (sizeIt != be.end()) entry.size = jpt(*sizeIt);
        entry.bus = jstr(be, "bus");
        entry.member = jstr(be, "member");
        entry.net = objectNet(entry.id);
        busEntries_.push_back(std::move(entry));
    }

    for (const auto& j : jarr(tree, "junctions")) {
        if (!j.is_object()) continue;
        EcadJunction junction;
        junction.id = jstr(j, "id");
        junction.sheet = jstr(j, "sheet", "sh1");
        junction.pos = jptKey(j, "pos");
        junction.net = objectNet(junction.id);
        junctions_.push_back(std::move(junction));
    }
    for (const auto& l : jarr(tree, "labels")) {
        if (!l.is_object()) continue;
        EcadLabel label;
        label.id = jstr(l, "id");
        label.sheet = jstr(l, "sheet", "sh1");
        label.text = jstr(l, "text");
        label.scope = jstr(l, "scope", "local");
        label.pos = jptKey(l, "pos");
        label.angle = (float)jnum(l, "angle");
        label.net = objectNet(label.id);
        labels_.push_back(std::move(label));
    }
    for (const auto& p : jarr(tree, "power")) {
        if (!p.is_object()) continue;
        EcadPower power;
        power.id = jstr(p, "id");
        power.sheet = jstr(p, "sheet", "sh1");
        power.net = jstr(p, "net");
        power.symbol = jstr(p, "symbol");
        power.pos = jptKey(p, "pos");
        power.angle = (float)jnum(p, "angle");
        powers_.push_back(std::move(power));
    }
    for (const auto& nc : jarr(tree, "no_connect")) {
        if (!nc.is_object()) continue;
        EcadNoConnect noConnect;
        noConnect.id = jstr(nc, "id");
        noConnect.sheet = jstr(nc, "sheet", "sh1");
        noConnect.pos = jptKey(nc, "pos");
        noConnects_.push_back(std::move(noConnect));
    }
    for (const auto& t : jarr(tree, "texts")) {
        if (!t.is_object()) continue;
        EcadText text;
        text.id = jstr(t, "id");
        text.sheet = jstr(t, "sheet", "sh1");
        text.text = jstr(t, "text");
        text.pos = jptKey(t, "pos");
        text.size = (float)jnum(t, "size", 1.27);
        text.angle = (float)jnum(t, "angle");
        texts_.push_back(std::move(text));
    }

    for (const auto& g : jarr(tree, "graphics")) {
        if (!g.is_object()) continue;
        EcadGraphic graphic;
        graphic.id = jstr(g, "id");
        graphic.sheet = jstr(g, "sheet", "sh1");
        const std::string kind = jstr(g, "kind", "line");
        if (kind == "rect") graphic.kind = EcadGraphic::Rect;
        else if (kind == "circle") graphic.kind = EcadGraphic::Circle;
        else if (kind == "arc") graphic.kind = EcadGraphic::Arc;
        else if (kind == "polyline") graphic.kind = EcadGraphic::Polyline;
        else graphic.kind = EcadGraphic::Line;
        for (const auto& p : jarr(g, "pts")) graphic.pts.push_back(jpt(p));
        graphic.radius = (float)jnum(g, "radius");
        graphic.a0 = (float)jnum(g, "a0");
        graphic.a1 = (float)jnum(g, "a1");
        graphic.width = (float)jnum(g, "width", 0.15);
        graphic.filled = jbool(g, "filled");
        graphics_.push_back(std::move(graphic));
    }

    std::unordered_map<std::string, std::string> sheetNames;
    for (const auto& sheet : sheets_) sheetNames[sheet.id] = sheet.name;
    for (const auto& ss : jarr(tree, "sheet_symbols")) {
        if (!ss.is_object()) continue;
        EcadSheetSymbol sheetSymbol;
        sheetSymbol.id = jstr(ss, "id");
        sheetSymbol.sheet = jstr(ss, "sheet", "sh1");
        sheetSymbol.child = jstr(ss, "child");
        sheetSymbol.name = jstr(ss, "name");
        if (sheetSymbol.name.empty()) {
            const auto nameIt = sheetNames.find(sheetSymbol.child);
            if (nameIt != sheetNames.end()) sheetSymbol.name = nameIt->second;
        }
        sheetSymbol.pos = jptKey(ss, "pos");
        auto sizeIt = ss.find("size");
        if (sizeIt != ss.end()) sheetSymbol.size = jpt(*sizeIt);
        for (const auto& p : jarr(ss, "pins")) {
            if (!p.is_object()) continue;
            EcadSheetPin pin;
            pin.name = jstr(p, "name");
            pin.pos = jptKey(p, "pos");
            pin.shape = jstr(p, "shape", "bidirectional");
            pin.net = objectNet(sheetSymbol.id + "." + pin.name);
            sheetSymbol.pins.push_back(std::move(pin));
        }
        sheetSymbols_.push_back(std::move(sheetSymbol));
    }

    const nlohmann::json& nets = jobj(netlist, "nets");
    for (auto it = nets.begin(); it != nets.end(); ++it) {
        EcadNet net;
        net.name = it.key();
        net.pinCount = it.value().is_array() ? (int)it.value().size() : 0;
        const auto infoIt = netInfo.find(net.name);
        if (infoIt != netInfo.end() && infoIt->is_object()) {
            net.sheets = jstrList(jarr(*infoIt, "sheets"));
            net.bus = jstr(*infoIt, "bus");
            net.driven = jbool(*infoIt, "has_power_source");
            const auto driversIt = infoIt->find("drivers");
            if (driversIt != infoIt->end() && driversIt->is_array() && !driversIt->empty())
                net.driven = true;
        }
        nets_.push_back(std::move(net));
    }
    std::sort(nets_.begin(), nets_.end(),
              [](const EcadNet& a, const EcadNet& b) { return a.name < b.name; });
}

// ---- board ------------------------------------------------------------------------------------

void EcadMirror::parsePcb(const nlohmann::json& tree, const nlohmann::json& compile) {
    (void)compile;
    hasBoardContent_ = !jarr(jobj(tree, "outline"), "shapes").empty();
    for (const char* key : {"footprints", "tracks", "vias", "zones", "graphics", "nets"})
        hasBoardContent_ = hasBoardContent_ || !jarr(tree, key).empty();

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
            const float angle = ep.angle * kPi / 180.0f;
            const float ex = std::fabs(std::cos(angle)) * ep.w * 0.5f +
                             std::fabs(std::sin(angle)) * ep.h * 0.5f;
            const float ey = std::fabs(std::sin(angle)) * ep.w * 0.5f +
                             std::fabs(std::cos(angle)) * ep.h * 0.5f;
            growBox({ep.pos.x - ex, ep.pos.y - ey});
            growBox({ep.pos.x + ex, ep.pos.y + ey});
            efp.pads.push_back(std::move(ep));
        }
        // Courtyard fallback encloses whole rotated pads, not just their centers.
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

    // DRC (pcb): [{rule, severity, locs:[[x,y,layer]], items, message}]. Keep EVERY located
    // point (a clearance violation reports one per offending shape); loc/layer stay the first
    // entry for "zoom to violation".
    for (const auto& v : jarr(compile, "drc")) {
        EcadViolation ev;
        ev.rule     = jstr(v, "rule");
        ev.severity = jstr(v, "severity", "warning");
        ev.message  = jstr(v, "message");
        for (const auto& l : jarr(v, "locs")) {
            if (!l.is_array() || l.size() < 2) continue;
            EcadPt pt{(float)l[0].get<double>(), (float)l[1].get<double>()};
            if (!ev.hasLoc) {
                ev.loc = pt;
                ev.hasLoc = true;
                if (l.size() >= 3 && l[2].is_string())
                    ev.layer = l[2].get<std::string>();
            }
            ev.locs.push_back(pt);
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

void EcadMirror::setActiveSheet(const std::string& id) {
    const auto it = std::find_if(sheets_.begin(), sheets_.end(),
                                 [&](const EcadSheet& sheet) { return sheet.id == id; });
    if (it != sheets_.end()) activeSheet_ = id;
}

const std::string& EcadMirror::netOfObject(const std::string& objId) const {
    static const std::string kEmpty;
    const auto it = objectNet_.find(objId);
    return it == objectNet_.end() ? kEmpty : it->second;
}

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
        if (!have) {
            outMin = outMax = p;
            have = true;
            return;
        }
        outMin.x = std::min(outMin.x, p.x);
        outMin.y = std::min(outMin.y, p.y);
        outMax.x = std::max(outMax.x, p.x);
        outMax.y = std::max(outMax.y, p.y);
    };
    auto growCircle = [&](EcadPt center, float radius) {
        const float r = std::fabs(radius);
        grow({center.x - r, center.y - r});
        grow({center.x + r, center.y + r});
    };

    // Schematic geometry is sheet-local. Only the currently displayed sheet contributes.
    for (const auto& symbol : symbols_) {
        if (!onActiveSheet(symbol.sheet)) continue;
        grow(symbol.pos);
        for (const auto& graphic : symbol.body) {
            switch (graphic.kind) {
                case EcadSymGraphic::Rect:
                    if (graphic.pts.size() >= 2) {
                        const EcadPt a = graphic.pts[0];
                        const EcadPt b = graphic.pts[1];
                        grow(localToWorld(a, symbol.pos, symbol.angle, symbol.mirror));
                        grow(localToWorld({a.x, b.y}, symbol.pos, symbol.angle, symbol.mirror));
                        grow(localToWorld(b, symbol.pos, symbol.angle, symbol.mirror));
                        grow(localToWorld({b.x, a.y}, symbol.pos, symbol.angle, symbol.mirror));
                    }
                    break;
                case EcadSymGraphic::Circle:
                case EcadSymGraphic::Arc:
                    if (!graphic.pts.empty()) {
                        const EcadPt center =
                            localToWorld(graphic.pts[0], symbol.pos, symbol.angle, symbol.mirror);
                        growCircle(center, graphic.radius);
                    }
                    break;
                case EcadSymGraphic::Line:
                case EcadSymGraphic::Polyline:
                    for (const EcadPt& p : graphic.pts)
                        grow(localToWorld(p, symbol.pos, symbol.angle, symbol.mirror));
                    break;
            }
        }
        for (const auto& pin : symbol.pins) {
            grow(pin.root);
            grow(pin.tip);
        }
    }
    for (const auto& wire : wires_)
        if (onActiveSheet(wire.sheet))
            for (const EcadPt& p : wire.pts) grow(p);
    for (const auto& bus : buses_)
        if (onActiveSheet(bus.sheet))
            for (const EcadPt& p : bus.pts) grow(p);
    for (const auto& entry : busEntries_) {
        if (!onActiveSheet(entry.sheet)) continue;
        grow(entry.pos);
        grow({entry.pos.x + entry.size.x, entry.pos.y + entry.size.y});
    }
    for (const auto& junction : junctions_)
        if (onActiveSheet(junction.sheet)) grow(junction.pos);
    for (const auto& label : labels_)
        if (onActiveSheet(label.sheet)) grow(label.pos);
    for (const auto& power : powers_)
        if (onActiveSheet(power.sheet)) grow(power.pos);
    for (const auto& noConnect : noConnects_)
        if (onActiveSheet(noConnect.sheet)) grow(noConnect.pos);
    for (const auto& text : texts_)
        if (onActiveSheet(text.sheet)) grow(text.pos);
    for (const auto& graphic : graphics_) {
        if (!onActiveSheet(graphic.sheet)) continue;
        if ((graphic.kind == EcadGraphic::Circle || graphic.kind == EcadGraphic::Arc) &&
            !graphic.pts.empty()) {
            growCircle(graphic.pts.front(), graphic.radius);
        } else {
            for (const EcadPt& p : graphic.pts) grow(p);
        }
    }
    for (const auto& sheetSymbol : sheetSymbols_) {
        if (!onActiveSheet(sheetSymbol.sheet)) continue;
        grow(sheetSymbol.pos);
        grow({sheetSymbol.pos.x + sheetSymbol.size.x, sheetSymbol.pos.y});
        grow({sheetSymbol.pos.x + sheetSymbol.size.x,
              sheetSymbol.pos.y - sheetSymbol.size.y});
        grow({sheetSymbol.pos.x, sheetSymbol.pos.y - sheetSymbol.size.y});
        for (const auto& pin : sheetSymbol.pins) grow(pin.pos);
    }
    for (const auto& violation : erc_)
        if (violation.hasLoc) grow(violation.loc);

    // Preserve the PCB fit behavior.
    for (const auto& o : outline_) {
        grow(o.p1);
        grow(o.p2);
    }
    for (const auto& footprint : footprints_) {
        grow(footprint.pos);
        for (const auto& pad : footprint.pads) grow(pad.pos);
    }
    for (const auto& track : tracks_) {
        grow(track.p1);
        grow(track.p2);
    }
    for (const auto& via : vias_) grow(via.at);
    for (const auto& zone : zones_)
        for (const EcadPt& p : zone.polygon) grow(p);
    return have;
}

}  // namespace ui
