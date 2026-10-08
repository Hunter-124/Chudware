// HydraulicPanel central schematic, plots and native-viewport HUD.
#include "../hydraulic_panel.h"
#include "hydraulic_internal.h"

#include <algorithm>
#include <cfloat>
#include <cstddef>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <iterator>
#include <utility>

#include "../../core/app_state.h"
#include "../cam/cam_panel.h"
#include "../layout.h"
#include "../legend.h"
#include "../metrics.h"
#include "../property_manager.h"
#include "../sketch_mode.h"
#include "../sketch3d_mode.h"
#include "../viewport_panel.h"

namespace ui {
using namespace hydint;
namespace {

// ---- schematic symbols (ISO 1219 style, screen space) -----------------------------------------

struct Sym {
    ImDrawList* dl;
    ImVec2 c;
    float h;       // half-size in px
    ImU32 ink;
    float lw;
    ImVec2 at(float dx, float dy) const { return ImVec2(c.x + dx * h, c.y + dy * h); }
    void line(float x0, float y0, float x1, float y1) const { dl->AddLine(at(x0, y0), at(x1, y1), ink, lw); }
    void tri(float x0, float y0, float x1, float y1, float x2, float y2) const {
        dl->AddTriangleFilled(at(x0, y0), at(x1, y1), at(x2, y2), ink);
    }
    void arrow(float x0, float y0, float x1, float y1) const {
        const ImVec2 a = at(x0, y0), b = at(x1, y1);
        float dx = b.x - a.x, dy = b.y - a.y;
        const float len = std::sqrt(dx * dx + dy * dy);
        if (len < 1e-3f) return;
        dx /= len;
        dy /= len;
        const float hl = 0.22f * h, hw = 0.12f * h;
        const ImVec2 base(b.x - dx * hl, b.y - dy * hl);
        dl->AddLine(a, base, ink, lw);
        dl->AddTriangleFilled(b, ImVec2(base.x - dy * hw, base.y + dx * hw),
                              ImVec2(base.x + dy * hw, base.y - dx * hw), ink);
    }
    void dashed(float x0, float y0, float x1, float y1) const {
        const ImVec2 a = at(x0, y0), b = at(x1, y1);
        const int n = 6;
        for (int i = 0; i < n; i += 2) {
            const float t0 = (float)i / n, t1 = (float)(i + 1) / n;
            dl->AddLine(ImVec2(a.x + (b.x - a.x) * t0, a.y + (b.y - a.y) * t0),
                        ImVec2(a.x + (b.x - a.x) * t1, a.y + (b.y - a.y) * t1), ink, lw);
        }
    }
};

// Port anchor of `port` on a component symbol, in symbol units (multiply by h).
ImVec2 portOffset(const json& comp, const std::string& port) {
    const std::string type = jstr(comp, "type");
    if (type == "pump" || type == "motor_hydraulic" || type == "cylinder_rotary") {
        if (port == "P" || port == "A") return ImVec2(0.0f, -1.0f);
        if (port == "T" || port == "B") return ImVec2(0.0f, 1.0f);
        return ImVec2(1.0f, 0.0f);
    }
    if (type == "cylinder_linear") return port == "B" ? ImVec2(0.6f, 0.5f) : ImVec2(-1.2f, 0.5f);
    if (type == "valve_directional") {
        const int ways = (int)jnum(jobj(comp, "params"), "ways", 4.0);
        if (ways == 4) {
            if (port == "P") return ImVec2(-0.22f, 0.45f);
            if (port == "T") return ImVec2(0.22f, 0.45f);
            if (port == "A") return ImVec2(-0.22f, -0.45f);
            return ImVec2(0.22f, -0.45f);
        }
        if (port == "P") return ImVec2(-0.22f, 0.45f);
        if (port == "T") return ImVec2(0.22f, 0.45f);
        return ImVec2(0.0f, -0.45f);
    }
    if (type == "valve_relief") return port == "P" ? ImVec2(-0.6f, 0.0f) : ImVec2(0.0f, 0.6f);
    if (type == "accumulator") return ImVec2(0.0f, 0.8f);
    if (type == "reservoir") {
        static const char* const tp[] = {"T", "T2", "T3", "S"};
        const int i = indexOf(tp, 4, port);
        return ImVec2(-0.6f + 0.4f * (float)(i < 0 ? 1 : i), -0.5f);
    }
    return port == "OUT" ? ImVec2(0.7f, 0.0f) : ImVec2(-0.7f, 0.0f);
}

void drawSymbol(const Sym& s, const json& comp) {
    const std::string type = jstr(comp, "type");
    const json& p = jobj(comp, "params");
    if (type == "pump" || type == "motor_hydraulic" || type == "cylinder_rotary") {
        s.dl->AddCircle(s.c, s.h, s.ink, 0, s.lw);
        if (type == "pump") {
            s.tri(0.0f, -1.0f, -0.35f, -0.45f, 0.35f, -0.45f);  // outward: delivers flow
        } else {
            s.tri(0.0f, -0.4f, -0.35f, -0.93f, 0.35f, -0.93f);  // inward: consumes flow
            if (type == "cylinder_rotary") s.tri(0.0f, 0.4f, -0.35f, 0.93f, 0.35f, 0.93f);
        }
        s.line(1.0f, 0.0f, 1.45f, 0.0f);  // shaft
        s.line(1.45f, -0.12f, 1.45f, 0.12f);
        return;
    }
    if (type == "cylinder_linear") {
        s.dl->AddRect(s.at(-1.4f, -0.5f), s.at(0.8f, 0.5f), s.ink, 0.0f, 0, s.lw);
        s.line(-0.4f, -0.5f, -0.4f, 0.5f);                                             // piston
        s.dl->AddRect(s.at(-0.4f, -0.1f), s.at(1.7f, 0.1f), s.ink, 0.0f, 0, s.lw);  // rod
        if (jstr(p, "actuator_type") == "single_acting") {
            for (int i = 0; i < 4; ++i)  // return spring in the rod chamber
                s.line(-0.3f + 0.25f * i, i % 2 ? 0.35f : -0.35f, -0.175f + 0.25f * i, i % 2 ? -0.35f : 0.35f);
        }
        return;
    }
    if (type == "valve_directional") {
        const int positions = (int)jnum(p, "positions", 3.0) == 2 ? 2 : 3;
        const int ways = (int)jnum(p, "ways", 4.0);
        const float w = 0.9f;  // one envelope
        const float x0 = positions == 3 ? -1.5f * w : -w;
        for (int i = 0; i < positions; ++i)
            s.dl->AddRect(s.at(x0 + w * i, -0.45f), s.at(x0 + w * (i + 1), 0.45f), s.ink, 0.0f, 0, s.lw);
        // "a" envelope (left): straight paths P to A, B to T.
        const float ax = x0 + 0.5f * w;
        s.arrow(ax - 0.22f, 0.4f, ax - 0.22f, -0.4f);
        if (ways >= 3) s.arrow(ax + 0.22f, -0.4f, ax + 0.22f, 0.4f);
        // "b" envelope (right): crossed paths P to B, A to T.
        const float bx = x0 + (positions - 0.5f) * w;
        s.arrow(bx - 0.22f, 0.4f, bx + 0.22f, -0.4f);
        if (ways >= 3) s.arrow(bx - 0.22f, -0.4f, bx + 0.22f, 0.4f);
        if (positions == 3) {
            const std::string centre = jstr(p, "center_condition", "closed");
            auto stub = [&](float x, float y0, float y1) {
                s.line(x, y0, x, y1);
                s.line(x - 0.1f, y1, x + 0.1f, y1);
            };
            if (centre == "tandem") {
                s.line(-0.22f, 0.45f, -0.22f, 0.15f);
                s.line(-0.22f, 0.15f, 0.22f, 0.15f);
                s.line(0.22f, 0.15f, 0.22f, 0.45f);
                stub(-0.22f, -0.45f, -0.2f);
                stub(0.22f, -0.45f, -0.2f);
            } else if (centre == "open") {
                s.line(-0.22f, 0.45f, 0.22f, -0.45f);
                s.line(-0.22f, -0.45f, 0.22f, 0.45f);
            } else if (centre == "float") {
                stub(-0.22f, 0.45f, 0.2f);
                s.line(-0.22f, -0.45f, 0.22f, 0.45f);
                s.line(0.22f, -0.45f, 0.22f, 0.45f);
            } else {
                stub(-0.22f, 0.45f, 0.2f);
                stub(0.22f, 0.45f, 0.2f);
                stub(-0.22f, -0.45f, -0.2f);
                stub(0.22f, -0.45f, -0.2f);
            }
        }
        // Actuators: a solenoid box on each end (spring-centred).
        const float xl = x0, xr = x0 + positions * w;
        s.dl->AddRect(s.at(xl - 0.35f, -0.25f), s.at(xl, 0.25f), s.ink, 0.0f, 0, s.lw);
        s.line(xl - 0.35f, 0.25f, xl, -0.25f);
        s.dl->AddRect(s.at(xr, -0.25f), s.at(xr + 0.35f, 0.25f), s.ink, 0.0f, 0, s.lw);
        s.line(xr, 0.25f, xr + 0.35f, -0.25f);
        return;
    }
    if (type == "valve_relief") {
        // One envelope 1.2 h tall (taller than a directional-valve envelope, so it reads beside the
        // pump), normally closed (offset arrow), a full-height coil spring with its end plate on the
        // right, and the pilot line dashed from the inlet round to the spring's opposite face.
        s.dl->AddRect(s.at(-0.6f, -0.6f), s.at(0.6f, 0.6f), s.ink, 0.0f, 0, s.lw);
        s.arrow(-0.2f, 0.45f, -0.2f, -0.45f);
        constexpr int kCoils = 6;
        const float sx0 = 0.6f, sx1 = 1.2f, amp = 0.3f;
        for (int i = 0; i < kCoils; ++i) {
            const float xa = sx0 + (sx1 - sx0) * (float)i / kCoils, xb = sx0 + (sx1 - sx0) * (float)(i + 1) / kCoils;
            const float ya = i == 0 ? 0.0f : (i % 2 ? -amp : amp);
            const float yb = i + 1 == kCoils ? 0.0f : ((i + 1) % 2 ? -amp : amp);
            s.line(xa, ya, xb, yb);
        }
        s.line(sx1, -amp, sx1, amp);
        s.dashed(-0.6f, 0.0f, -0.9f, 0.0f);
        s.dashed(-0.9f, 0.0f, -0.9f, -0.9f);
        s.dashed(-0.9f, -0.9f, 0.0f, -0.9f);
        s.dashed(0.0f, -0.9f, 0.0f, -0.6f);
        return;
    }
    if (type == "valve_check") {
        s.line(-0.7f, 0.0f, -0.45f, 0.0f);
        s.dl->AddCircle(s.at(-0.2f, 0.0f), 0.25f * s.h, s.ink, 0, s.lw);
        s.line(-0.1f, -0.42f, 0.3f, 0.0f);
        s.line(-0.1f, 0.42f, 0.3f, 0.0f);
        s.line(0.3f, 0.0f, 0.7f, 0.0f);
        return;
    }
    if (type == "valve_flow_control") {
        s.line(-0.7f, 0.0f, 0.7f, 0.0f);
        s.dl->AddBezierQuadratic(s.at(-0.45f, -0.4f), s.at(0.0f, 0.05f), s.at(0.45f, -0.4f), s.ink, s.lw);
        s.dl->AddBezierQuadratic(s.at(-0.45f, 0.4f), s.at(0.0f, -0.05f), s.at(0.45f, 0.4f), s.ink, s.lw);
        s.arrow(-0.45f, 0.55f, 0.5f, -0.55f);
        return;
    }
    if (type == "accumulator") {
        s.dl->AddRect(s.at(-0.4f, -0.8f), s.at(0.4f, 0.8f), s.ink, 0.4f * s.h, 0, s.lw);
        s.line(-0.4f, 0.0f, 0.4f, 0.0f);
        return;
    }
    if (type == "reservoir") {
        s.line(-0.9f, -0.5f, -0.9f, 0.5f);
        s.line(-0.9f, 0.5f, 0.9f, 0.5f);
        s.line(0.9f, 0.5f, 0.9f, -0.5f);
        return;
    }
    // filter / heat exchanger: the ISO diamond.
    s.line(-0.6f, 0.0f, 0.0f, -0.6f);
    s.line(0.0f, -0.6f, 0.6f, 0.0f);
    s.line(0.6f, 0.0f, 0.0f, 0.6f);
    s.line(0.0f, 0.6f, -0.6f, 0.0f);
    if (type == "filter") {
        s.dashed(0.0f, -0.6f, 0.0f, 0.6f);
    } else {
        s.line(-0.6f, 0.0f, 0.6f, 0.0f);
        s.tri(0.0f, -0.95f, -0.12f, -0.7f, 0.12f, -0.7f);
        s.tri(0.0f, 0.95f, -0.12f, 0.7f, 0.12f, 0.7f);
    }
}

// Hit box of a component symbol, in symbol units around its centre (multiply by h).
struct SymBox {
    float x0, y0, x1, y1;
};
SymBox symBox(const json& comp) {
    const std::string type = jstr(comp, "type");
    if (type == "pump" || type == "motor_hydraulic" || type == "cylinder_rotary") return {-1.0f, -1.0f, 1.5f, 1.0f};
    if (type == "cylinder_linear") return {-1.4f, -0.5f, 1.7f, 0.5f};
    if (type == "valve_directional") {
        const bool three = (int)jnum(jobj(comp, "params"), "positions", 3.0) != 2;
        return three ? SymBox{-1.7f, -0.5f, 1.7f, 0.5f} : SymBox{-1.25f, -0.5f, 1.25f, 0.5f};
    }
    if (type == "valve_relief") return {-0.95f, -0.95f, 1.25f, 0.65f};
    if (type == "accumulator") return {-0.45f, -0.85f, 0.45f, 0.85f};
    if (type == "reservoir") return {-0.95f, -0.55f, 0.95f, 0.55f};
    return {-0.75f, -0.65f, 0.75f, 0.65f};
}

// The part of the current (central Viewport) window that is not under the pinned glass columns
// (FeatureManager left, Copilot right) or the doc-tab band, inset by the pane gutter. Columns that
// did not draw this frame publish no edge and the full width is used.
ImRect clearCentralRect() {
    const ImGuiWindow* win = ImGui::GetCurrentWindow();
    ImRect r = win->WorkRect;
    const float g = kit::gutter();
    const float left = overlayColumnEdge(0), right = overlayColumnEdge(1), top = docTabBandBottom();
    if (left >= 0.0f) r.Min.x = std::max(r.Min.x, left + g);
    if (right >= 0.0f) r.Max.x = std::min(r.Max.x, right - g);
    if (top >= 0.0f) r.Min.y = std::max(r.Min.y, top + g);
    r.Max.y -= g;
    const float minW = metrics::px(240.0f), minH = metrics::px(160.0f);
    if (r.GetWidth() < minW) r.Max.x = r.Min.x + minW;
    if (r.GetHeight() < minH) r.Max.y = r.Min.y + minH;
    return r;
}

// Unit suffixes of hydraulic_simulate series keys (position_mm, flow_lpm, ...) and their display
// units; the longest suffix of a family comes first ("_mms" before "_mm").
struct SeriesSuffix {
    const char* suffix;
    const char* unit;
};
const SeriesSuffix* seriesSuffix(const std::string& key) {
    static const SeriesSuffix units[] = {{"_mms", "mm/s"}, {"_mm", "mm"},  {"_lpm", "L/min"}, {"_bar", "bar"},
                                         {"_kw", "kW"},    {"_rpm", "rpm"}, {"_nm", "N\xc2\xb7m"}, {"_n", "N"},
                                         {"_deg", "\xc2\xb0"}, {"_l", "L"}, {"_kj", "kJ"}};
    for (const auto& u : units) {
        const std::size_t n = std::strlen(u.suffix);
        if (key.size() > n && key.compare(key.size() - n, n, u.suffix) == 0) return &u;
    }
    return nullptr;
}
// Display unit of a series key ("" when it carries no known suffix).
const char* seriesUnit(const std::string& key) {
    const SeriesSuffix* u = seriesSuffix(key);
    return u ? u->unit : "";
}
// Display word of a series key: "flow_lpm" -> "Flow", "chamber_a_pressure_bar" -> "Chamber a pressure".
std::string seriesName(const std::string& key) {
    const SeriesSuffix* u = seriesSuffix(key);
    return prettyChoice(u ? key.substr(0, key.size() - std::strlen(u->suffix)).c_str() : key.c_str());
}

std::vector<float> floats(const json& arr) {
    std::vector<float> out;
    if (!arr.is_array()) return out;
    out.reserve(arr.size());
    for (const auto& v : arr) out.push_back(v.is_number() ? (float)v.get<double>() : NAN);
    return out;
}


}  // namespace

std::pair<float, float> HydraulicPanel::schematicScreenPosition(double x, double y) const {
    return {viewMidX_ + (float)((x - viewCx_) * viewK_),
            viewMidY_ - (float)((y - viewCy_) * viewK_)};
}

std::pair<float, float> HydraulicPanel::portScreenPosition(const std::string& componentId,
                                                          const std::string& port) const {
    const int ci = componentIndex(componentId);
    if (ci < 0) return {0.0f, 0.0f};
    const json& comp = jarr(circuit_, "components")[ci];
    const json& p = comp.value("position", json::array({0.0, 0.0}));
    if (!p.is_array() || p.size() != 2 || !p[0].is_number() || !p[1].is_number()) return {0.0f, 0.0f};
    auto screen = schematicScreenPosition(p[0].get<double>(), p[1].get<double>());
    const auto o = portOffset(comp, port);
    screen.first += o.x * viewH_;
    screen.second += o.y * viewH_;
    return screen;
}

void HydraulicPanel::selectFromCanvas(int component, int hose) {
    selected_ = component;
    selectedHose_ = hose;
    if (component < 0 && hose < 0) return;
    // The side pane follows the canvas: the Circuit form's Components (symbol) or Hoses (hose)
    // page, scrolled to what was picked.
    setForm(Form::Circuit);
    setCircuitPage(component >= 0 ? CircuitPage::Components : CircuitPage::Hoses);
    focusEditor_ = component >= 0;
    focusHose_ = hose >= 0;
}

namespace {
float segDistance(ImVec2 p, ImVec2 a, ImVec2 b) {
    const float dx = b.x - a.x, dy = b.y - a.y;
    const float len2 = dx * dx + dy * dy;
    const float u = len2 > 1e-6f ? std::clamp(((p.x - a.x) * dx + (p.y - a.y) * dy) / len2, 0.0f, 1.0f) : 0.0f;
    return std::hypot(p.x - (a.x + u * dx), p.y - (a.y + u * dy));
}
// Screen-space rectangle (schematic obstacle or label slot).
struct Box {
    float x0, y0, x1, y1;
};
bool boxHit(const Box& a, const Box& b) { return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1; }
// An axis-aligned segment passing through the interior of `r`.
bool segHitsBox(ImVec2 a, ImVec2 b, const Box& r) {
    return std::min(a.x, b.x) < r.x1 && std::max(a.x, b.x) > r.x0 && std::min(a.y, b.y) < r.y1 &&
           std::max(a.y, b.y) > r.y0;
}
// Perpendicular axis-aligned segments crossing in both interiors.
bool segsCross(ImVec2 a, ImVec2 b, ImVec2 c, ImVec2 d) {
    if (std::fabs(a.x - b.x) >= 0.5f) {  // make a-b the vertical one
        std::swap(a, c);
        std::swap(b, d);
    }
    return c.y > std::min(a.y, b.y) && c.y < std::max(a.y, b.y) && a.x > std::min(c.x, d.x) &&
           a.x < std::max(c.x, d.x);
}
// Orthogonal polyline cleanup into `out`: drop repeated points, merge straight runs. False when
// the path doubles back on itself.
bool tidyPath(const ImVec2* in, int n, std::vector<ImVec2>& out) {
    out.clear();
    for (int i = 0; i < n; ++i) {
        const ImVec2 v = in[i];
        if (!out.empty() && std::fabs(v.x - out.back().x) + std::fabs(v.y - out.back().y) < 0.5f) continue;
        if (out.size() >= 2) {
            const ImVec2 a = out[out.size() - 2], b = out.back();
            const bool straight = (std::fabs(a.x - b.x) < 0.5f && std::fabs(b.x - v.x) < 0.5f) ||
                                  (std::fabs(a.y - b.y) < 0.5f && std::fabs(b.y - v.y) < 0.5f);
            if (straight) {
                if ((b.x - a.x) * (v.x - b.x) + (b.y - a.y) * (v.y - b.y) < 0.0f) return false;
                out.back() = v;
                continue;
            }
        }
        out.push_back(v);
    }
    return out.size() >= 2;
}
// Unit direction a hose leaves `port` along: the outward normal of the symbol-box face nearest it.
ImVec2 portExit(const json& comp, const std::string& port) {
    const ImVec2 o = portOffset(comp, port);
    const SymBox b = symBox(comp);
    const float d[4] = {o.x - b.x0, b.x1 - o.x, o.y - b.y0, b.y1 - o.y};
    int k = 0;
    for (int i = 1; i < 4; ++i)
        if (d[i] < d[k]) k = i;
    static const ImVec2 kNormal[4] = {ImVec2(-1.0f, 0.0f), ImVec2(1.0f, 0.0f), ImVec2(0.0f, -1.0f), ImVec2(0.0f, 1.0f)};
    return kNormal[k];
}

const char* componentPurpose(const std::string& type) {
    if (type == "pump") return "Supplies hydraulic flow from a rotating drive.";
    if (type == "motor_hydraulic") return "Turns hydraulic flow into shaft rotation.";
    if (type == "cylinder_linear") return "Turns hydraulic pressure into linear motion.";
    if (type == "cylinder_rotary") return "Turns hydraulic pressure into limited-angle rotation.";
    if (type == "valve_directional") return "Routes flow to extend, retract or stop an actuator.";
    if (type == "valve_relief") return "Limits pressure by returning excess flow to the tank.";
    if (type == "valve_check") return "Allows flow in one direction and blocks reverse flow.";
    if (type == "valve_flow_control") return "Sets flow to control actuator speed.";
    if (type == "accumulator") return "Stores pressurized fluid and cushions pressure changes.";
    if (type == "reservoir") return "Stores oil and supplies the pump's suction line.";
    if (type == "filter") return "Removes contamination from the hydraulic oil.";
    return "Transfers heat out of the hydraulic oil.";
}

std::string portName(const json& component, const std::string& port) {
    for (const auto& p : jarr(component, "ports"))
        if (jstr(p, "id") == port && !jstr(p, "name").empty()) return jstr(p, "name");
    const std::string type = jstr(component, "type");
    if (port == "L") return "Case drain";
    if (port == "S" || (type == "pump" && port == "T")) return "Suction";
    if (port == "P") return "Pressure";
    if (port == "A") return "Work port A";
    if (port == "B") return "Work port B";
    if (port == "IN") return "Inlet";
    if (port == "OUT") return "Outlet";
    if (port == "T2") return "Return 2";
    if (port == "T3") return "Return 3";
    return "Return";
}

std::string componentTip(const json& component) {
    const std::string type = jstr(component, "type");
    std::string text = jstr(component, "name", typeLabel(type)) + " · " + typeLabel(type);
    const auto* def = typeDef(type);
    if (!def) return text;
    const auto& params = jobj(component, "params");
    for (const auto& field : def->params) {
        if (field.optional && !jhas(params, field.key)) continue;
        std::string value;
        if (!field.choices.empty()) {
            if (field.intChoice)
                value = kit::fmtValue(jnum(params, field.key, std::atoi(field.choices[0])));
            else
                value = prettyChoice(jstr(params, field.key, field.choices[0]).c_str());
        } else {
            const double number = jnum(params, field.key, field.def);
            value = kit::fmtQuantity(field.ratio ? number * 100.0 : number,
                field.ratio ? kit::unitText(kit::Unit::Percent) : field.unit ? kit::unitText(field.unit) : nullptr);
        }
        text += "\n" + std::string(field.label) + ": " + value;
    }
    return text;
}

// Nodes may combine several authored hoses. Resolve every hose through its member endpoints,
// not only the first hose ID used as the native node key.
void collectHosePeaks(const json& result, const json& hoses, std::map<std::string, float>& out) {
    out.clear();
    const auto& nodes = jobj(result, "nodes");
    for (auto node = nodes.begin(); node != nodes.end(); ++node) {
        float peak = 0.0f;
        for (const auto& value : jarr(*node, "pressure_bar"))
            if (value.is_number() && std::isfinite(value.get<float>())) peak = std::max(peak, value.get<float>());
        for (const auto& hose : hoses) {
            const std::string id = jstr(hose, "id");
            bool belongs = id == node.key();
            for (const auto& end : jarr(hose, "connections")) {
                const std::string member = jstr(end, "component_id") + "." + jstr(end, "port");
                for (const auto& m : jarr(*node, "members")) belongs |= m.is_string() && m == member;
            }
            if (belongs) out[id] = peak;
        }
    }
}
}  // namespace

void HydraulicPanel::drawSchematic(AppState& st, float width, float height) {
    const ui::Palette& pal = ui::palette;
    const float lineH = ImGui::GetTextLineHeight();
    // Framed kit canvas: the interaction hint in its bottom strip, the port-marker key at the
    // strip's right end.
    const char* kOn = "connected";
    const char* kOpen = "open port";
    const float keyR = 0.3f * lineH;
    const float gapK = ImGui::GetStyle().ItemInnerSpacing.x;
    const float keyW = jarr(circuit_, "components").empty() ? 0.0f
                                     : ImGui::CalcTextSize(kOn).x + ImGui::CalcTextSize(kOpen).x + 4.0f * keyR +
                                           2.0f * gapK + kit::padX();
    ImGui::SetNextItemAllowOverlap();
    const kit::Canvas cv =
        kit::canvasFrame("schematic", ImVec2(width, height),
                         "Drag a tool to place · click two ports to connect", keyW);
    ImDrawList* dl = cv.dl;
    const auto& defs = typeDefs();
    const float toolGap = metrics::px(3.0f);
    const float toolSize = std::clamp((cv.inner.GetHeight() - 2.0f * kit::padY()) / (float)defs.size() - toolGap,
                                      metrics::px(22.0f), metrics::px(36.0f));
    const float railWidth = toolSize + 2.0f * toolGap + metrics::px(ui::kChatScrollbarLane);
    const float barSize = ImGui::GetFrameHeight();
    const float barWidth = 4.0f * barSize + 3.0f * toolGap;
    const ImVec2 c0(cv.inner.Min.x + railWidth + toolGap,
                    cv.inner.Min.y + barSize + kit::padY() +
                        (canvas_.connectArmed || canvas_.placement.is_object()
                             ? lineH + 2.0f * kit::padY() : 0.0f));
    const ImVec2 c1 = cv.inner.Max;
    const ImRect content(c0, c1);
    const bool hovered = content.Contains(ImGui::GetIO().MousePos) &&
        ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows | ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) &&
        !ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId);
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) canvas_.focused = hovered;
    const auto cancelGesture = [&] {
        endpointComponent_.clear();
        endpointPort_.clear();
        canvas_.placement = json();
        canvas_.portPressed = false;
        canvas_.pressedComponent.clear();
        canvas_.pressedPort.clear();
        canvas_.connectArmed = false;
    };
    const auto& io = ImGui::GetIO();
    const bool keys = (hovered || (canvas_.focused &&
        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows))) && !io.WantTextInput &&
        (GImGui->ActiveId == 0 || GImGui->ActiveId == cv.id) &&
        !(st.propertyManager && st.propertyManager->active()) &&
        !(st.sketchMode && st.sketchMode->active()) &&
        !(st.sketch3dMode && st.sketch3dMode->active()) && !st.sketchPlanePickActive &&
        !ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId);
    const auto pressed = [&](ImGuiKey key) {
        if (!ImGui::IsKeyPressed(key, ImGuiInputFlags_None, cv.id)) return false;
        ImGui::SetKeyOwner(key, cv.id, ImGuiInputFlags_LockThisFrame);
        return true;
    };
    if (keys) {
        if (pressed(ImGuiKey_Escape)) cancelGesture();
        else if ((io.KeyCtrl || io.KeySuper) && pressed(ImGuiKey_Z)) {
            cancelGesture();
            if (io.KeyShift) redo(st); else undo(st);
        } else if ((io.KeyCtrl || io.KeySuper) && pressed(ImGuiKey_Y)) {
            cancelGesture();
            redo(st);
        } else if (pressed(ImGuiKey_Delete) || pressed(ImGuiKey_Backspace)) {
            cancelGesture();
            deleteSelection(st);
        } else if (!io.KeyCtrl && !io.KeySuper && pressed(ImGuiKey_F)) {
            viewK_ = 0.0;
        }
    }
    json& comps = circuit_["components"];  // refresh after undo/delete may replace the circuit
    const auto scopedPlacement = [&](const json& p) {
        return p.is_object() && jstr(p, "doc_id") == store_.activeDocId(st) &&
            jstr(p, "study_id") == slot_.id && jstr(p, "circuit_name") == jstr(circuit_, "name") &&
            p.contains("circuit_generation") && p["circuit_generation"] == circuitGeneration_ &&
            typeDef(jstr(p, "type")) && p.contains("params") && p["params"].is_object();
    };
    if (canvas_.placement.is_object() && !scopedPlacement(canvas_.placement)) canvas_.placement = json();
    // Overlay controls draw last, above the schematic, but never share its hit region.
    const auto drawTools = [&] {
        const ImVec2 savedCursor = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(ImVec2(cv.inner.Min.x + toolGap, cv.inner.Min.y + kit::padY()));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(toolGap, toolGap));
        if (ImGui::BeginChild("hyd_canvas_palette", ImVec2(railWidth - toolGap, cv.inner.GetHeight() - 2.0f * kit::padY()),
                              ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar)) {
            static const std::vector<json> symbols = [] {
                std::vector<json> out;
                for (const auto& d : typeDefs()) out.push_back({{"type", d.type}, {"params", defaultParams(d)}});
                return out;
            }();
            for (int i = 0; i < (int)defs.size(); ++i) {
                const auto& def = defs[i];
                // ### preserves the square button's stable identity without drawing a caption.
                const std::string buttonId = std::string("###hyd_canvas_palette_") + def.type;
                const bool picked = kit::secondaryButton(buttonId.c_str(), !busy(),
                    "Wait for the current request to finish.", ImVec2(toolSize, toolSize));
                const ImVec2 a = ImGui::GetItemRectMin(), b = ImGui::GetItemRectMax();
                const bool chosen = jstr(canvas_.placement, "type") == def.type;
                drawSymbol({ImGui::GetWindowDrawList(), ImVec2((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f),
                            toolSize * 0.24f, ui::Palette::toU32(busy() ? pal.textDisabled : chosen ? pal.accent : pal.text),
                            metrics::stroke(1.2f)}, symbols[i]);
                const auto payload = [&] {
                    return json{{"doc_id", store_.activeDocId(st)}, {"study_id", slot_.id},
                                {"circuit_name", jstr(circuit_, "name")}, {"circuit_generation", circuitGeneration_},
                                {"type", def.type}, {"name", addName_},
                                {"params", i == addParamsType_ ? addParams_ : symbols[i]["params"]}};
                };
                if (picked) {
                    cancelGesture();
                    canvas_.placement = payload();
                    addType_ = i;
                    canvas_.focused = true;
                }
                if (!busy() && ImGui::BeginDragDropSource()) {
                    canvas_.placement = json();
                    const std::string text = payload().dump();
                    ImGui::SetDragDropPayload(kHydraulicComponentPayload, text.c_str(), text.size() + 1, ImGuiCond_Once);
                    kit::dimText(def.label);
                    ImGui::EndDragDropSource();
                }
                kit::itemTooltip((std::string(def.label) + "\n" + componentPurpose(def.type) +
                                  "\nDrag onto the canvas, or click then click to place.").c_str());
            }
            ui::drawWindowPillScrollbarV(ui::adaptivePanel(ui::panelId("hyd_canvas_palette"), cv.inner, ui::GlassGroup::Hud));
        }
        ImGui::EndChild();
        ImGui::PopStyleVar(2);
        ImGui::SetCursorScreenPos(ImVec2(cv.inner.Max.x - barWidth - kit::padX(), cv.inner.Min.y + kit::padY()));
        if (kit::iconButton("hyd_canvas_undo", "undo", "Undo circuit edit (Ctrl+Z)", canUndo(), kit::Tone::Neutral, barSize)) {
            cancelGesture();
            undo(st);
        }
        ImGui::SameLine(0.0f, toolGap);
        if (kit::iconButton("hyd_canvas_redo", "redo", "Redo circuit edit (Ctrl+Shift+Z / Ctrl+Y)", canRedo(), kit::Tone::Neutral, barSize)) {
            cancelGesture();
            redo(st);
        }
        ImGui::SameLine(0.0f, toolGap);
        if (kit::iconButton("hyd_canvas_fit", "zoom_fit", "Fit circuit in view (F)", true, kit::Tone::Neutral, barSize))
            viewK_ = 0.0;
        ImGui::SameLine(0.0f, toolGap);
        if (kit::iconButton("hyd_canvas_connect", "relations", canvas_.connectArmed ? "Stop connecting ports (Esc)" : "Connect ports",
                            !busy(), canvas_.connectArmed ? kit::Tone::Accent : kit::Tone::Neutral, barSize)) {
            const bool arm = !canvas_.connectArmed;
            cancelGesture();
            canvas_.connectArmed = arm;
        }
        const char* hint = canvas_.connectArmed ? "Click a port, then another port to connect · Esc to stop"
            : canvas_.placement.is_object() ? "Click the canvas to place the component · Esc to cancel" : nullptr;
        if (hint) {
            const std::string text = kit::elide(hint, content.GetWidth() - 2.0f * kit::padX());
            const float chipWidth = ImGui::CalcTextSize(text.c_str()).x + 2.0f * kit::padX();
            ImGui::SetCursorScreenPos(ImVec2((c0.x + c1.x - chipWidth) * 0.5f,
                                            cv.inner.Min.y + barSize + kit::padY()));
            kit::chip(text.c_str(), kit::Tone::Accent, false, hint);
        }
        ImGui::SetCursorScreenPos(savedCursor);
    };

    // World (mm, y up) to canvas: fit every component with room for its symbol and label, frozen
    // while a symbol is dragged. The symbol half-size follows the scale (a fifth of the 150 mm
    // placement pitch) within readable bounds.
    if ((dragging_ < 0 && endpointComponent_.empty() && !canvas_.placement.is_object() && !ImGui::GetDragDropPayload()) ||
        viewK_ <= 0.0) {
        double lo[2] = {1e30, 1e30}, hi[2] = {-1e30, -1e30};
        for (const auto& c : comps) {
            const json& pos = c.value("position", json());
            if (!pos.is_array() || pos.size() != 2 || !pos[0].is_number() || !pos[1].is_number()) continue;
            for (int k = 0; k < 2; ++k) {
                lo[k] = std::min(lo[k], pos[k].get<double>());
                hi[k] = std::max(hi[k], pos[k].get<double>());
            }
        }
        if (lo[0] > hi[0]) lo[0] = hi[0] = lo[1] = hi[1] = 0.0;
        const double spanX = std::max(1.0, hi[0] - lo[0]), spanY = std::max(1.0, hi[1] - lo[1]);
        const float hMin = metrics::px(10.0f), hMax = metrics::px(34.0f);
        float h = hMax;
        double k = 0.0;
        for (int it = 0; it < 4; ++it) {
            const double mx = 2.0 * h, my = 1.6 * h + lineH;
            const double sx = ((c1.x - c0.x) - 2.0 * mx) / spanX;
            const double sy = ((c1.y - c0.y) - 2.0 * my) / spanY;
            k = std::max(0.02, std::min({sx, sy, (double)metrics::px(3.0f)}));
            h = std::clamp((float)(k * 30.0), hMin, hMax);
        }
        viewK_ = k;
        viewH_ = h;
        viewCx_ = 0.5 * (lo[0] + hi[0]);
        viewCy_ = 0.5 * (lo[1] + hi[1]);
    }
    const float h = viewH_;
    const ImVec2 mid(0.5f * (c0.x + c1.x), 0.5f * (c0.y + c1.y));
    viewMidX_ = mid.x;
    viewMidY_ = mid.y;
    const auto place = [&](const json& p, ImVec2 point) {
        if (busy() || !content.Contains(point) || !scopedPlacement(p)) return false;
        return addComponent(st, jstr(p, "type"), jstr(p, "name"),
            std::round((viewCx_ + (point.x - mid.x) / viewK_) / 10.0) * 10.0,
            std::round((viewCy_ - (point.y - mid.y) / viewK_) / 10.0) * 10.0, p["params"]);
    };
    if (!busy() && ImGui::BeginDragDropTargetCustom(content, cv.id)) {
        if (const ImGuiPayload* payload = ImGui::AcceptDragDropPayload(kHydraulicComponentPayload)) {
            if (payload->IsDelivery() && payload->DataSize > 1 &&
                static_cast<const char*>(payload->Data)[payload->DataSize - 1] == '\0') {
                const char* data = static_cast<const char*>(payload->Data);
                place(json::parse(data, data + payload->DataSize - 1, nullptr, false), io.MousePos);
            }
        }
        ImGui::EndDragDropTarget();
    }
    const bool placing = canvas_.placement.is_object();
    if (placing && hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && place(canvas_.placement, io.MousePos))
        canvas_.placement = json();
    if (comps.empty()) {
        kit::Canvas empty = cv;
        empty.inner = content;
        kit::canvasEmpty(empty, "schematic", "No components yet",
                         "Pick a starting template in the side panel, or drag a component from the toolbar");
        drawTools();
        return;
    }
    auto worldToScreen = [&](double x, double y) {
        return ImVec2(mid.x + (float)((x - viewCx_) * viewK_), mid.y - (float)((y - viewCy_) * viewK_));
    };
    auto toScreen = [&](const json& c) {
        const json& pos = c.value("position", json());
        double x = 0.0, y = 0.0;
        if (pos.is_array() && pos.size() == 2 && pos[0].is_number() && pos[1].is_number()) {
            x = pos[0].get<double>();
            y = pos[1].get<double>();
        }
        return worldToScreen(x, y);
    };
    std::vector<ImVec2> centres;
    centres.reserve(comps.size());
    for (const auto& c : comps) centres.push_back(toScreen(c));
    auto anchor = [&](int ci, const std::string& port) {
        const ImVec2 o = portOffset(comps[ci], port);
        return ImVec2(centres[ci].x + o.x * h, centres[ci].y + o.y * h);
    };

    // Hose routing. Each end leaves its port along the face normal for a stub, then the cheapest
    // orthogonal path joins them: symbols are walls; running alongside another hose closer than a
    // lane costs per pixel, so hoses from adjacent ports take separate parallel lanes; crossings
    // and bends cost a little. Two ends take a 3-segment shape, or a 5-segment detour when every
    // such shape runs into a symbol; three or more ends meet at a junction dot.
    const float lane = std::max(metrics::px(6.0f), 0.3f * h);
    const float stub = std::max(metrics::px(8.0f), 0.45f * h);
    const float clear = std::max(metrics::px(3.0f), 0.15f * h);
    std::vector<Box> boxes;  // symbol boxes grown by `clear`
    boxes.reserve(comps.size());
    for (int i = 0; i < (int)comps.size(); ++i) {
        const SymBox b = symBox(comps[i]);
        boxes.push_back({centres[i].x + b.x0 * h - clear, centres[i].y + b.y0 * h - clear,
                         centres[i].x + b.x1 * h + clear, centres[i].y + b.y1 * h + clear});
    }
    struct HoseEnd {
        ImVec2 p, d;  // port anchor, exit direction
        int comp;
    };
    auto& segs = schematicSegs_;
    auto& routes = schematicRoutes_;
    auto& junctions = schematicJunctions_;
    auto& connected = schematicConnected_;
    const bool reroute = schematicRevision_ != circuitGeneration_ || dragging_ >= 0 ||
        schematicH_ != h || schematicRect_[0] != c0.x || schematicRect_[1] != c0.y ||
        schematicRect_[2] != c1.x || schematicRect_[3] != c1.y;
    if (reroute) {
    segs.clear(); routes.clear(); junctions.clear(); connected.clear();
    constexpr float kWall = 1e5f;
    // Cost of path `p` for `hose`, abandoned past `limit`. Its first / last segment may cross the
    // symbol it leaves / enters (ports sit just inside the symbol box).
    auto pathCost = [&](const std::vector<ImVec2>& p, int hose, int compA, int compB, float limit) {
        const int n = (int)p.size() - 1;
        float cost = (float)(n - 1) * 2.0f * lane;
        for (int i = 0; i < n && cost < limit; ++i) {
            const ImVec2 a = p[i], b = p[i + 1];
            cost += std::fabs(b.x - a.x) + std::fabs(b.y - a.y);
            if (std::min(a.x, b.x) < c0.x || std::max(a.x, b.x) > c1.x || std::min(a.y, b.y) < c0.y ||
                std::max(a.y, b.y) > c1.y)
                cost += kWall;
            for (int c = 0; c < (int)boxes.size(); ++c)
                if (!(i == 0 && c == compA) && !(i == n - 1 && c == compB) && segHitsBox(a, b, boxes[c]))
                    cost += kWall;
            const bool vert = std::fabs(a.x - b.x) < 0.5f;
            for (const auto& s : segs) {
                if (s.hose == hose) continue;
                if (vert != (std::fabs(s.a.x - s.b.x) < 0.5f)) {
                    if (segsCross(a, b, s.a, s.b)) cost += 3.0f * lane;
                    continue;
                }
                const float gap = vert ? std::fabs(a.x - s.a.x) : std::fabs(a.y - s.a.y);
                if (gap >= 1.5f * lane) continue;
                const float o0 = vert ? std::max(std::min(a.y, b.y), std::min(s.a.y, s.b.y))
                                      : std::max(std::min(a.x, b.x), std::min(s.a.x, s.b.x));
                const float o1 = vert ? std::min(std::max(a.y, b.y), std::max(s.a.y, s.b.y))
                                      : std::min(std::max(a.x, b.x), std::max(s.a.x, s.b.x));
                if (o1 > o0) {
                    // Collinear overlap is not a cheaper version of two visible lanes.
                    if (gap < metrics::stroke(1.0f)) cost += kWall;
                    cost += (o1 - o0) * (gap < 0.75f * lane ? 8.0f : 1.0f);
                }
            }
        }
        return cost;
    };
    // Channel candidates along one axis: between the two ends, at each end, beside every symbol,
    // each also shifted by up to two lanes either way.
    std::vector<float> ys, xs;
    auto channels = [&](bool yAxis, float a, float b, std::vector<float>& out) {
        static const int kLanes[] = {0, 1, -1, 2, -2};
        out.clear();
        auto add = [&](float v) {
            for (int k : kLanes) out.push_back(v + (float)k * lane);
        };
        add(0.5f * (a + b));
        add(a);
        add(b);
        for (const auto& bx : boxes) {
            add((yAxis ? bx.y0 : bx.x0) - lane);
            add((yAxis ? bx.y1 : bx.x1) + lane);
        }
        for (const auto& s : segs) {
            const bool vertical = std::fabs(s.a.x - s.b.x) < 0.5f;
            if (vertical != yAxis) {
                const float axis = yAxis ? s.a.y : s.a.x;
                out.push_back(axis - lane);
                out.push_back(axis + lane);
            }
        }
    };
    std::vector<ImVec2> scratch;
    auto routeTwo = [&](int hose, const HoseEnd& e0, const HoseEnd& e1, std::vector<ImVec2>& out) {
        const ImVec2 a0 = e0.p, a1 = e1.p;
        const ImVec2 q0(a0.x + e0.d.x * stub, a0.y + e0.d.y * stub), q1(a1.x + e1.d.x * stub, a1.y + e1.d.y * stub);
        float best = FLT_MAX;
        out.clear();
        auto tryPath = [&](std::initializer_list<ImVec2> pts) {
            if (!tidyPath(pts.begin(), (int)pts.size(), scratch)) return;
            const float c = pathCost(scratch, hose, e0.comp, e1.comp, best);
            if (c < best) {
                best = c;
                out = scratch;
            }
        };
        channels(true, q0.y, q1.y, ys);
        channels(false, q0.x, q1.x, xs);
        for (float y : ys) tryPath({a0, q0, ImVec2(q0.x, y), ImVec2(q1.x, y), q1, a1});
        for (float x : xs) tryPath({a0, q0, ImVec2(x, q0.y), ImVec2(x, q1.y), q1, a1});
        tryPath({a0, q0, ImVec2(q0.x, q1.y), q1, a1});
        tryPath({a0, q0, ImVec2(q1.x, q0.y), q1, a1});
        if (best >= 1000.0f) {  // blocked or crowded: detour along a side channel
            for (int i = 0; i < 3; ++i)
                for (int j = 0; j < 3; ++j) {
                    const float ya = q0.y + e0.d.y * (float)i * lane, yb = q1.y + e1.d.y * (float)j * lane;
                    const float xa = q0.x + e0.d.x * (float)i * lane, xb = q1.x + e1.d.x * (float)j * lane;
                    for (float x : xs)
                        tryPath({a0, q0, ImVec2(q0.x, ya), ImVec2(x, ya), ImVec2(x, yb), ImVec2(q1.x, yb), q1, a1});
                    for (float y : ys)
                        tryPath({a0, q0, ImVec2(xa, q0.y), ImVec2(xa, y), ImVec2(xb, y), ImVec2(xb, q1.y), q1, a1});
                }
        }
        if (out.empty()) out = {a0, ImVec2(a0.x, a1.y), a1};
    };
    std::vector<std::vector<ImVec2>> branches, trial;
    auto routeTee = [&](int hose, const std::vector<HoseEnd>& es, ImVec2& junction) {
        const int n = (int)es.size();
        ImVec2 avg(0.0f, 0.0f);
        for (const auto& e : es) {
            avg.x += (e.p.x + e.d.x * stub) / (float)n;
            avg.y += (e.p.y + e.d.y * stub) / (float)n;
        }
        branches.assign(n, std::vector<ImVec2>());
        trial.assign(n, std::vector<ImVec2>());
        float bestTotal = FLT_MAX;
        auto tryJunction = [&](ImVec2 j) {
            float total = 0.0f;
            for (const auto& b : boxes)
                if (j.x > b.x0 && j.x < b.x1 && j.y > b.y0 && j.y < b.y1) total += kWall;
            for (int e = 0; e < n && total < bestTotal; ++e) {
                const ImVec2 p = es[e].p, q(p.x + es[e].d.x * stub, p.y + es[e].d.y * stub);
                const ImVec2 viaV[] = {p, q, ImVec2(q.x, j.y), j};
                const ImVec2 viaH[] = {p, q, ImVec2(j.x, q.y), j};
                const ImVec2* vias[] = {viaV, viaH};
                float best = FLT_MAX;
                trial[e].clear();
                for (const ImVec2* via : vias) {
                    if (!tidyPath(via, 4, scratch)) continue;
                    const float c = pathCost(scratch, hose, es[e].comp, -1, best);
                    if (c < best) {
                        best = c;
                        trial[e] = scratch;
                    }
                }
                if (trial[e].empty()) {
                    trial[e] = {p, ImVec2(p.x, j.y), j};
                    best = kWall;
                }
                total += best;
            }
            if (total < bestTotal) {
                bestTotal = total;
                junction = j;
                branches.swap(trial);
            }
        };
        tryJunction(avg);
        for (const auto& e : es) {
            const ImVec2 q(e.p.x + e.d.x * stub, e.p.y + e.d.y * stub);
            tryJunction(ImVec2(q.x, avg.y));
            tryJunction(ImVec2(avg.x, q.y));
        }
    };
    const json& hoses = jarr(circuit_, "hoses");
    std::vector<HoseEnd> ends;
    std::vector<ImVec2> path;
    // The router's finite candidate set can still leave a coincident run. Give that run its own
    // deterministic screen-space lane, retaining the real port/junction anchors at both ends.
    const auto separateLanes = [&](std::vector<ImVec2>& points, int hose) {
        for (std::size_t i = 0; i + 1 < points.size(); ++i) {
            const ImVec2 a = points[i], b = points[i + 1];
            const bool vertical = std::fabs(a.x - b.x) < 0.5f;
            const float low = vertical ? std::min(a.y, b.y) : std::min(a.x, b.x);
            const float high = vertical ? std::max(a.y, b.y) : std::max(a.x, b.x);
            const float axis = vertical ? a.x : a.y;
            const auto occupied = [&](float coordinate) {
                for (const auto& s : segs) {
                    if (s.hose == hose || vertical != (std::fabs(s.a.x - s.b.x) < 0.5f)) continue;
                    if (std::fabs(coordinate - (vertical ? s.a.x : s.a.y)) >= 0.8f * lane) continue;
                    const float overlap = std::min(high, vertical ? std::max(s.a.y, s.b.y) : std::max(s.a.x, s.b.x)) -
                        std::max(low, vertical ? std::min(s.a.y, s.b.y) : std::min(s.a.x, s.b.x));
                    if (overlap > metrics::stroke(1.0f)) return true;
                }
                return false;
            };
            if (!occupied(axis)) continue;
            float offset = 0.0f;
            for (std::size_t step = 1; step <= segs.size() + 1; ++step) {
                const float positive = axis + (float)step * lane, negative = axis - (float)step * lane;
                const float minAxis = vertical ? c0.x : c0.y, maxAxis = vertical ? c1.x : c1.y;
                if (positive < maxAxis && !occupied(positive)) { offset = positive - axis; break; }
                if (negative > minAxis && !occupied(negative)) { offset = negative - axis; break; }
            }
            if (offset == 0.0f) continue;
            ImVec2 from = a, to = b;
            if (vertical) { from.x += offset; to.x += offset; }
            else { from.y += offset; to.y += offset; }
            // Moving an interior corner keeps both neighboring orthogonal runs connected. Port
            // and tee endpoints instead retain a short normal stub before entering the lane.
            if (i == 0) {
                const float fraction = std::min(stub / std::max(high - low, stub), 0.25f);
                ImVec2 knee(a.x + (b.x - a.x) * fraction, a.y + (b.y - a.y) * fraction);
                from = knee;
                if (vertical) from.x += offset; else from.y += offset;
                points.insert(points.begin() + 1, {knee, from});
                i += 2;
            } else {
                points[i] = from;
            }
            if (i + 2 == points.size()) {
                const float fraction = std::min(stub / std::max(high - low, stub), 0.25f);
                ImVec2 knee(b.x + (a.x - b.x) * fraction, b.y + (a.y - b.y) * fraction);
                to = knee;
                if (vertical) to.x += offset; else to.y += offset;
                points.insert(points.begin() + (std::ptrdiff_t)i + 1, {to, knee});
                i += 2;
            } else {
                points[i + 1] = to;
            }
        }
    };
    for (int hi = 0; hi < (int)hoses.size(); ++hi) {
        ends.clear();
        for (const auto& c : jarr(hoses[hi], "connections")) {
            const int ci = componentIndex(jstr(c, "component_id"));
            if (ci < 0) continue;
            const std::string port = jstr(c, "port");
            ends.push_back({anchor(ci, port), portExit(comps[ci], port), ci});
            connected.push_back({jstr(c, "component_id"), port});
        }
        const std::size_t first = routes.size();
        if (ends.size() == 2) {
            routeTwo(hi, ends[0], ends[1], path);
            separateLanes(path, hi);
            routes.push_back({path, hi});
        } else if (ends.size() > 2) {
            ImVec2 j = ends[0].p;
            routeTee(hi, ends, j);
            for (auto& b : branches) {
                separateLanes(b, hi);
                routes.push_back({b, hi});
            }
            junctions.push_back({j, hi});
        }
        for (std::size_t r = first; r < routes.size(); ++r)
            for (std::size_t k = 0; k + 1 < routes[r].pts.size(); ++k)
                segs.push_back({routes[r].pts[k], routes[r].pts[k + 1], hi});
    }
    schematicRevision_ = circuitGeneration_;
    schematicH_ = h;
    schematicRect_[0] = c0.x; schematicRect_[1] = c0.y;
    schematicRect_[2] = c1.x; schematicRect_[3] = c1.y;
    }
    const json& hoses = jarr(circuit_, "hoses");
    if (schematicPressureRevision_ != resultGeneration_) {
        collectHosePeaks(sim_, hoses, schematicPressurePeaks_);
        schematicPressureRevision_ = resultGeneration_;
    }
    const auto hoseForPort = [&](const std::string& id, const std::string& port) {
        for (int i = 0; i < (int)hoses.size(); ++i)
            for (const auto& end : jarr(hoses[i], "connections"))
                if (jstr(end, "component_id") == id && jstr(end, "port") == port) return i;
        return -1;
    };
    const int sourceHose = hoseForPort(endpointComponent_, endpointPort_);
    const auto compatible = [&](const std::string& id, const std::string& port) {
        return !endpointComponent_.empty() && (id != endpointComponent_ || port != endpointPort_) &&
            (sourceHose < 0 || sourceHose != hoseForPort(id, port));
    };

    // Interaction: click selects a symbol (else a hose; empty canvas clears), drag moves the
    // symbol (snapped to 10 mm on release). The side pane follows the selection.
    const ImVec2 mouse = ImGui::GetIO().MousePos;
    auto symbolAt = [&](ImVec2 m) {
        int hit = -1;
        float best = FLT_MAX;
        const float pad = 0.25f * h;
        for (int i = 0; i < (int)centres.size(); ++i) {
            const SymBox bx = symBox(comps[i]);
            const float dx = m.x - centres[i].x, dy = m.y - centres[i].y;
            if (dx < bx.x0 * h - pad || dx > bx.x1 * h + pad || dy < bx.y0 * h - pad || dy > bx.y1 * h + pad) continue;
            const float d = std::hypot(dx, dy);
            if (d < best) {
                best = d;
                hit = i;
            }
        }
        return hit;
    };
    auto hoseAt = [&](ImVec2 m) {
        int hit = -1;
        float best = std::max(0.3f * h, metrics::px(5.0f));
        for (const auto& s : segs) {
            const float d = segDistance(m, s.a, s.b);
            if (d < best) {
                best = d;
                hit = s.hose;
            }
        }
        return hit;
    };
    End hoveredPort;
    float nearestPort = std::max(metrics::px(7.0f), 0.22f * h);
    if (hovered && dragging_ < 0 && !placing && !ImGui::GetDragDropPayload())
        for (int ci = 0; ci < (int)comps.size(); ++ci) {
            const auto ports = portsOf(comps[ci]);
            for (int pi = 0; pi < (int)ports.size(); ++pi) {
                const auto point = anchor(ci, ports[pi]);
                const float distance = std::hypot(mouse.x - point.x, mouse.y - point.y);
                if (distance < nearestPort) {
                    nearestPort = distance;
                    hoveredPort = {ci, pi};
                }
            }
        }
    const int hoverSym = hovered && dragging_ < 0 && hoveredPort.comp < 0 ? symbolAt(mouse) : -1;
    const int hoverHose = hovered && dragging_ < 0 && hoverSym < 0 && hoveredPort.comp < 0 ? hoseAt(mouse) : -1;
    if (hoveredPort.comp >= 0 && !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
        const auto& component = comps[hoveredPort.comp];
        const auto ports = portsOf(component);
        const std::string& port = ports[hoveredPort.port];
        const bool on = hoseForPort(jstr(component, "id"), port) >= 0;
        const bool required = !portOptional(jstr(component, "type"), port);
        const std::string tip = componentName(jstr(component, "id")) + " · " + portName(component, port) +
            "\n" + (on ? "Connected" : "Not connected") + " · " + (required ? "Required port" : "Optional port") +
            "\n" + (endpointComponent_.empty() ? "Click or drag to start a connection."
                 : compatible(jstr(component, "id"), port) ? "Click or release here to connect."
                 : "Already on the selected net; choose another port.") + "\nPort: " + port;
        kit::tooltip(tip.c_str());
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    } else if (hoverSym >= 0 && !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
        kit::tooltip(componentTip(comps[hoverSym]).c_str());
    } else if (hoverHose >= 0 && !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
        const auto& hose = hoses[hoverHose];
        std::string tip = jstr(hose, "net_name", "Hose") +
            "\nLength: " + qty(hose, "length_mm", "mm") +
            "\nInner diameter: " + qty(hose, "id_mm", "mm") +
            "\nRated pressure: " + qty(hose, "rating_bar", "bar");
        const auto peak = schematicPressurePeaks_.find(jstr(hose, "id"));
        if (peak != schematicPressurePeaks_.end())
            tip += "\nPeak pressure: " + kit::fmtQuantity(peak->second, kit::unitText(kit::Unit::bar));
        tip += "\nHose ID: " + jstr(hose, "id");
        kit::tooltip(tip.c_str());
    }
    std::string connectA, connectPortA, connectB, connectPortB;
    const auto pickPort = [&](int ci, const std::string& port) {
        const std::string id = jstr(comps[ci], "id");
        const std::string source = endpointComponent_, sourcePort = endpointPort_;
        if (!source.empty() && (source != id || sourcePort != port)) {
            if (!compatible(id, port)) return;
            if (selectPortEndpoint(st, id, port)) {
                connectA = source;
                connectPortA = sourcePort;
                connectB = id;
                connectPortB = port;
            }
        } else {
            selectPortEndpoint(st, id, port);
        }
    };
    if (!busy() && hovered && !placing && !ImGui::GetDragDropPayload() &&
        ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        if (hoveredPort.comp >= 0) {
            const auto ports = portsOf(comps[hoveredPort.comp]);
            const bool starting = endpointComponent_.empty();
            pickPort(hoveredPort.comp, ports[hoveredPort.port]);
            canvas_.portPressed = starting;
            canvas_.pressedComponent = jstr(comps[hoveredPort.comp], "id");
            canvas_.pressedPort = ports[hoveredPort.port];
        } else {
            endpointComponent_.clear();
            endpointPort_.clear();
            canvas_.portPressed = false;
            selectFromCanvas(hoverSym, hoverSym < 0 ? hoverHose : -1);
            dragging_ = hoverSym;
            dragMoved_ = false;
        }
    }
    if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
        if (!busy() && canvas_.portPressed && !endpointComponent_.empty() && hoveredPort.comp >= 0) {
            const auto ports = portsOf(comps[hoveredPort.comp]);
            if (canvas_.pressedComponent != jstr(comps[hoveredPort.comp], "id") ||
                canvas_.pressedPort != ports[hoveredPort.port])
                pickPort(hoveredPort.comp, ports[hoveredPort.port]);
        } else if (canvas_.portPressed && hoveredPort.comp < 0) {
            const ImVec2 delta = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
            if (delta.x != 0.0f || delta.y != 0.0f) {
                endpointComponent_.clear();
                endpointPort_.clear();
            }
        }
        canvas_.portPressed = false;
    }
    if (!busy() && dragging_ >= 0 && dragging_ < (int)comps.size()) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeAll);
        if (ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            const ImVec2 d = ImGui::GetIO().MouseDelta;
            if ((d.x != 0.0f || d.y != 0.0f) && viewK_ > 0.0) {
                json& pos = comps[dragging_]["position"];
                if (pos.is_array() && pos.size() == 2 && pos[0].is_number() && pos[1].is_number()) {
                    pos[0] = pos[0].get<double>() + d.x / viewK_;
                    pos[1] = pos[1].get<double>() - d.y / viewK_;
                    dragMoved_ = true;
                }
            }
        } else {
            if (dragMoved_) {
                json& pos = comps[dragging_]["position"];
                if (pos.is_array() && pos.size() == 2 && pos[0].is_number() && pos[1].is_number()) {
                    pos[0] = std::round(pos[0].get<double>() / 10.0) * 10.0;
                    pos[1] = std::round(pos[1].get<double>() / 10.0) * 10.0;
                }
                commitCircuitMutation(st, false);  // one undo step; placement keeps simulation valid
            }
            dragging_ = -1;
        }
    } else if (hoverSym >= 0 || hoverHose >= 0) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    }

    dl->PushClipRect(c0, c1, true);
    // Placement grid: a light line every 50 mm when that is far enough apart to read.
    const double pitch = 50.0;
    if (viewK_ * pitch >= metrics::px(14.0f)) {
        float gridCol[4];
        ui::Palette::withAlpha(pal.border, gridCol, 0.35f);
        const ImU32 grid = ui::Palette::toU32(gridCol);
        const double wx0 = viewCx_ + (c0.x - mid.x) / viewK_, wx1 = viewCx_ + (c1.x - mid.x) / viewK_;
        const double wy0 = viewCy_ - (c1.y - mid.y) / viewK_, wy1 = viewCy_ - (c0.y - mid.y) / viewK_;
        for (double x = std::ceil(wx0 / pitch) * pitch; x <= wx1; x += pitch) {
            const float sx = worldToScreen(x, 0.0).x;
            dl->AddLine(ImVec2(sx, c0.y), ImVec2(sx, c1.y), grid, metrics::stroke(1.0f));
        }
        for (double y = std::ceil(wy0 / pitch) * pitch; y <= wy1; y += pitch) {
            const float sy = worldToScreen(0.0, y).y;
            dl->AddLine(ImVec2(c0.x, sy), ImVec2(c1.x, sy), grid, metrics::stroke(1.0f));
        }
    }
    const float lw = std::max(metrics::stroke(1.5f), 0.075f * h);
    // Hoses under the symbols; the selected / hovered one last so it reads on top.
    for (int pass = 0; pass < 2; ++pass)
        for (const auto& r : routes) {
            const bool hot = r.hose == selectedHose_ || r.hose == hoverHose;
            if (hot != (pass == 1)) continue;
            dl->AddPolyline(r.pts.data(), (int)r.pts.size(), ui::Palette::toU32(hot ? pal.accent : pal.accent2), 0,
                            hot ? lw * 1.6f : lw);
        }
    std::vector<Box> dots;  // port and junction dots: label slots avoid them
    for (const auto& j : junctions) {
        dl->AddCircleFilled(j.first, 0.18f * h, ui::Palette::toU32(j.second == selectedHose_ ? pal.accent : pal.accent2));
        dots.push_back({j.first.x - 0.25f * h, j.first.y - 0.25f * h, j.first.x + 0.25f * h, j.first.y + 0.25f * h});
    }
    // Symbols and port dots (hollow amber = a required port with no hose).
    for (int i = 0; i < (int)comps.size(); ++i) {
        const json& c = comps[i];
        const float* ink = i == selected_ ? pal.accent : (i == hoverSym ? pal.accent2 : pal.text);
        Sym s{dl, centres[i], h, ui::Palette::toU32(ink), lw};
        drawSymbol(s, c);
        const std::string id = jstr(c, "id");
        const std::string type = jstr(c, "type");
        const auto ports = portsOf(c);
        for (const auto& port : ports) {
            bool on = false;
            for (const auto& e : connected) on = on || (e.first == id && e.second == port);
            const ImVec2 a = anchor(i, port);
            dots.push_back({a.x - 0.2f * h, a.y - 0.2f * h, a.x + 0.2f * h, a.y + 0.2f * h});
            if (on)
                dl->AddCircleFilled(a, 0.13f * h, ui::Palette::toU32(pal.accent2));
            else if (!portOptional(type, port))
                dl->AddCircle(a, 0.16f * h, ui::Palette::toU32(pal.statusWarn), 0, std::max(metrics::stroke(1.0f), 0.06f * h));
            else
                dl->AddCircle(a, 0.13f * h, ui::Palette::toU32(pal.textDim), 0, metrics::stroke(1.0f));
            const bool pending = endpointComponent_ == id && endpointPort_ == port;
            const bool portHovered = hoveredPort.comp == i && hoveredPort.port >= 0 &&
                hoveredPort.port < (int)ports.size() && ports[hoveredPort.port] == port;
            if (pending || portHovered || compatible(id, port))
                dl->AddCircle(a, std::max(metrics::px(portHovered ? 6.0f : 4.5f), (portHovered ? 0.3f : 0.24f) * h),
                              ui::Palette::toU32(pending || portHovered ? pal.accent : pal.accent2),
                              0, metrics::stroke(portHovered || pending ? 2.0f : 1.0f));
        }
    }
    // Labels: component names first, then net names, each in its cheapest free slot. A slot over
    // a symbol, another label or past the canvas edge is never used; a hose or dot under it costs
    // more than any placement preference. Each label sits on a canvas-coloured plate.
    struct Label {
        Box r;
        ImVec2 pos;
        std::string text;
        ImU32 col;
    };
    std::vector<Label> labels;
    const float gap = metrics::px(4.0f), plate = metrics::px(3.0f);
    constexpr float kCovered = 100.0f;
    auto plateAt = [&](ImVec2 pos, ImVec2 ts) {
        return Box{pos.x - plate, pos.y - plate, pos.x + ts.x + plate, pos.y + ts.y + plate};
    };
    auto slotCost = [&](const Box& r) {
        if (r.x0 < c0.x || r.y0 < c0.y || r.x1 > c1.x || r.y1 > c1.y) return FLT_MAX;
        for (const auto& b : boxes)
            if (boxHit(r, b)) return FLT_MAX;
        for (const auto& l : labels)
            if (boxHit(r, l.r)) return FLT_MAX;
        float cost = 0.0f;
        for (const auto& b : dots)
            if (boxHit(r, b)) cost += kCovered;
        for (const auto& s : segs)
            if (segHitsBox(s.a, s.b, r)) cost += kCovered;
        return cost;
    };
    // Component names: below, above, beside, then the corners; one is always placed.
    for (int i = 0; i < (int)comps.size(); ++i) {
        const std::string text = kit::elide(jstr(comps[i], "name", jstr(comps[i], "id")).c_str(), 7.0f * h);
        const ImVec2 ts = ImGui::CalcTextSize(text.c_str());
        const Box& b = boxes[i];
        const float below = b.y1 + gap + plate, above = b.y0 - gap - plate - ts.y;
        const float right = b.x1 + gap + plate, left = b.x0 - gap - plate - ts.x;
        const float centred = centres[i].x - 0.5f * ts.x, midY = centres[i].y - 0.5f * ts.y;
        const float toRight = centres[i].x + lane, toLeft = centres[i].x - lane - ts.x;
        const ImVec2 cand[] = {{centred, below},  {centred, above},     {right, midY},      {left, midY},
                               {toRight, below},  {toLeft, below},      {toRight, above},   {toLeft, above},
                               {right, b.y0},     {right, b.y1 - ts.y}, {left, b.y0},       {left, b.y1 - ts.y},
                               {right, below},    {left, below},        {right, above},     {left, above}};
        float best = FLT_MAX;
        int pick = 0;
        for (int k = 0; k < (int)std::size(cand); ++k) {
            const float c = slotCost(plateAt(cand[k], ts));
            if (c < FLT_MAX && c + (float)k < best) {
                best = c + (float)k;
                pick = k;
            }
        }
        labels.push_back({plateAt(cand[pick], ts), cand[pick], text,
                          ui::Palette::toU32(i == selected_ ? pal.accent : pal.textDim)});
    }
    // Net names beside their own hose, with the node's peak pressure once a simulation ran (nodes
    // are keyed by their first hose's id). Without a clean slot the text shortens
    // (`name · peak X bar` -> `name · X bar` -> name only); with none at all the name is left to the side pane.
    if (h >= metrics::px(14.0f)) {
        static const float kAlong[] = {0.5f, 0.3f, 0.7f, 0.15f, 0.85f};
        std::string variants[3];
        for (int hi = 0; hi < (int)hoses.size(); ++hi) {
            const json& hose = hoses[hi];
            const std::string name = jstr(hose, "net_name", jstr(hose, "id"));
            const auto pressure = schematicPressurePeaks_.find(jstr(hose, "id"));
            const float peak = pressure == schematicPressurePeaks_.end() ? 0.0f : pressure->second;
            int nv = 1;
            variants[0] = name;
            // An unloaded net (return / drain lines at ~0 bar) carries just its name.
            if (peak >= 0.05f) {
                const std::string pk = kit::fmtQuantity(peak, kit::unitText(kit::Unit::bar), 3);
                variants[0] = name + " \xc2\xb7 peak " + pk;
                variants[1] = name + " \xc2\xb7 " + pk;
                variants[2] = name;
                nv = 3;
            }
            for (int v = 0; v < nv; ++v) {
                const ImVec2 ts = ImGui::CalcTextSize(variants[v].c_str());
                float best = FLT_MAX;
                ImVec2 pos;
                for (const auto& r : routes) {
                    if (r.hose != hi) continue;
                    for (std::size_t si = 0; si + 1 < r.pts.size(); ++si) {
                        const ImVec2 a = r.pts[si], b = r.pts[si + 1];
                        const bool vert = std::fabs(a.x - b.x) < 0.5f;
                        for (int t = 0; t < (int)std::size(kAlong); ++t) {
                            const ImVec2 m(a.x + (b.x - a.x) * kAlong[t], a.y + (b.y - a.y) * kAlong[t]);
                            ImVec2 side[2];
                            if (vert) {
                                side[0] = ImVec2(m.x + gap + plate, m.y - 0.5f * ts.y);
                                side[1] = ImVec2(m.x - gap - plate - ts.x, m.y - 0.5f * ts.y);
                            } else {
                                side[0] = ImVec2(m.x - 0.5f * ts.x, m.y - gap - plate - ts.y);
                                side[1] = ImVec2(m.x - 0.5f * ts.x, m.y + gap + plate);
                            }
                            for (int k = 0; k < 2; ++k) {
                                const float c = slotCost(plateAt(side[k], ts));
                                if (c < FLT_MAX && c + 2.0f * (float)t + (float)k < best) {
                                    best = c + 2.0f * (float)t + (float)k;
                                    pos = side[k];
                                }
                            }
                        }
                    }
                }
                if (best < kCovered) {
                    labels.push_back({plateAt(pos, ts), pos, variants[v],
                                      ui::Palette::toU32(hi == selectedHose_ ? pal.accent : pal.textDim)});
                    break;
                }
            }
        }
    }
    float plateCol[4];
    ui::Palette::withAlpha(pal.frameBg, plateCol, 0.92f);
    const ImU32 plateU = ui::Palette::toU32(plateCol);
    for (const auto& l : labels) {
        dl->AddRectFilled(ImVec2(l.r.x0, l.r.y0), ImVec2(l.r.x1, l.r.y1), plateU, metrics::rounding(3.0f));
        dl->AddText(l.pos, l.col, l.text.c_str());
    }
    if (!endpointComponent_.empty()) {
        const auto start = portScreenPosition(endpointComponent_, endpointPort_);
        dl->AddLine(ImVec2(start.first, start.second), mouse, ui::Palette::toU32(pal.accent), metrics::stroke(1.5f));
    }
    if (canvas_.placement.is_object() && hovered) {
        const double x = std::round((viewCx_ + (mouse.x - mid.x) / viewK_) / 10.0) * 10.0;
        const double y = std::round((viewCy_ - (mouse.y - mid.y) / viewK_) / 10.0) * 10.0;
        drawSymbol({dl, worldToScreen(x, y), h, ui::Palette::toU32(pal.accent), lw}, canvas_.placement);
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    }
    dl->PopClipRect();

    // Port-marker key in the canvas strip (the kit drew the hint left of it).
    const float ty = cv.strip.Min.y + 0.5f * (cv.strip.GetHeight() - lineH);
    const ImU32 dim = ui::Palette::toU32(pal.textDim);
    float kx = cv.keyX;
    dl->AddCircleFilled(ImVec2(kx + keyR, ty + 0.5f * lineH), 0.8f * keyR, ui::Palette::toU32(pal.accent2));
    kx += 2.0f * keyR + gapK;
    dl->AddText(ImVec2(kx, ty), dim, kOn);
    kx += ImGui::CalcTextSize(kOn).x + kit::padX();
    dl->AddCircle(ImVec2(kx + keyR, ty + 0.5f * lineH), keyR, ui::Palette::toU32(pal.statusWarn), 0, metrics::stroke(1.5f));
    kx += 2.0f * keyR + gapK;
    dl->AddText(ImVec2(kx, ty), dim, kOpen);
    if (!connectA.empty() && !connectPorts(st, connectA, connectPortA, connectB, connectPortB)) {
        endpointComponent_ = connectA;
        endpointPort_ = connectPortA;
    }
    drawTools();
}

bool HydraulicPanel::drawViewToggle() {
    static const char* const kViews[] = {"Schematic", "Plots", "3D"};
    static const char* const kTips[] = {
        "The ISO 1219 circuit schematic: click a symbol or hose to edit it, drag a symbol to move it.",
        "Pressure, actuator position and speed over time from the last simulation.",
        "Shaded circuit layout: select a component, then Place in 3D on a working plane."};
    int v = (int)central_;
    if (!kit::segmented("hyd_view", kViews, 3, &v, 0.0f, kTips) || v == (int)central_) return false;
    setCentralView((CentralView)v);
    endpointComponent_.clear();
    endpointPort_.clear();
    canvas_.placement = json();
    canvas_.portPressed = false;
    canvas_.connectArmed = false;
    canvas_.focused = false;
    return true;
}

void HydraulicPanel::drawCentralView(AppState& st) {
    ImRect r = clearCentralRect();
    r.Min.y = std::min(std::max(r.Min.y, ImGui::GetCursorScreenPos().y), r.Max.y - metrics::px(160.0f));
    ImGui::SetCursorScreenPos(r.Min);
    if (ImGui::BeginChild("hyd_central", r.GetSize(), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse |
                              ImGuiWindowFlags_NoBackground)) {
        drawViewToggle();
        std::string title = circuit_.is_object() ? jstr(circuit_, "name", "Hydraulic circuit") : std::string("Hydraulics");
        title += central_ == CentralView::Plots ? " \xc2\xb7 simulation results" : " \xc2\xb7 ISO 1219 schematic";
        ImGui::SameLine(0.0f, 2.0f * kit::padX());
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(kit::elide(title.c_str(), ImGui::GetContentRegionAvail().x).c_str());
        kit::gapRow();
        const ImVec2 avail = ImGui::GetContentRegionAvail();
        if (!circuit_.is_object()) {
            if (store_.inflight() > 0)
                kit::spinner("Creating the circuit\xe2\x80\xa6");
            else if (kit::emptyState("study_hydraulics", "No hydraulic circuit yet",
                                     "Start a circuit: its ISO 1219 schematic draws here.", "New circuit"))
                handleCommand(st, "sim_hyd_new");
        } else if (central_ == CentralView::Plots) {
            drawPlots(st, avail.x, avail.y);
        } else {
            drawSchematic(st, avail.x, avail.y);
        }
    }
    ImGui::EndChild();
}

void HydraulicPanel::drawViewToggleHud(AppState& st) {
    (void)st;
    if (!open_ || central_ != CentralView::View3D) return;
    ImRect r = clearCentralRect();
    ImVec2 clearMin, clearMax;
    if (viewportClearRect(clearMin, clearMax)) r = ImRect(clearMin, clearMax);
    // The view switch belongs to the central scene, not the side pane's placement form.
    ImGui::SetNextWindowPos(ImVec2(r.Min.x + kit::gutter(), r.Min.y + kit::gutter()), ImGuiCond_Always);
    ui::pushGlassWindowStyle();
    const ImGuiWindowFlags switchFlags = ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
        ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoDocking |
        ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_AlwaysAutoResize;
    bool switched = false;
    if (ImGui::Begin("##hyd_scene_views", nullptr, switchFlags)) switched = drawViewToggle();
    ImGui::End();
    ui::popGlassWindowStyle();
    if (switched) return;
    const char* hint = placing3d_ ? "Click plane to place\nTab plane · Esc/Enter finish"
                                 : "Click a component to edit · double-click to place\nDelete removes · Ctrl+Z undoes";
    const char* legend = "Hose colour: peak pressure";
    const bool haveResults = sim_.is_object() && !sim_.empty();
    const float legendWidth = legendReserveWidth();
    if (legendWidth > 0 && r.GetWidth() - legendWidth >= metrics::px(200))
        r.Max.x -= legendWidth;
    const float contentWidth = std::max(ImGui::CalcTextSize(hint).x,
        haveResults ? ImGui::CalcTextSize(legend).x + 4.0f * kit::padX() : 0.0f);
    const float width = std::min(contentWidth + 2.0f * kit::padX(),
                                 std::max(r.GetWidth() - 2.0f * kit::gutter(), 1.0f));
    ImGui::SetNextWindowPos(ImVec2((r.Min.x + r.Max.x) * 0.5f, r.Max.y - kit::gutter()),
                            ImGuiCond_Always, ImVec2(0.5f, 1.0f));
    ImGui::SetNextWindowSize(ImVec2(width, 0.0f));
    ui::pushGlassWindowStyle();
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(kit::padX(), kit::padY()));
    const ImGuiWindowFlags flags = ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                                   ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoDocking |
                                   ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_NoInputs |
                                   ImGuiWindowFlags_AlwaysAutoResize;
    if (ImGui::Begin("##hyd_view_hud", nullptr, flags)) {
        const float textWidth = ImGui::CalcTextSize(hint).x;
        ImGui::SetCursorPosX(std::max(kit::padX(), (width - textWidth) * 0.5f));
        kit::dimText(hint);
        if (haveResults) {
            ImGui::SetCursorPosX(std::max(kit::padX(),
                (width - ImGui::CalcTextSize(legend).x - 3.0f * kit::padX()) * 0.5f));
            kit::chip(legend, kit::Tone::Neutral, true);
        }
        const ImVec2 pos = ImGui::GetWindowPos(), size = ImGui::GetWindowSize();
        publishViewportHudRect(pos.x, pos.y, pos.x + size.x, pos.y + size.y);
    }
    ImGui::End();
    ImGui::PopStyleVar();
    ui::popGlassWindowStyle();
}

namespace {
// Rows kit::linePlot's wrapping legend takes for these series labels across `width` (0 for a
// single series, which has no legend). Mirrors its greedy layout: swatch + gap + text + spacing.
int legendRows(const std::vector<std::string>& labels, float width) {
    if (labels.size() < 2) return 0;
    int rows = 1;
    float x = 0.0f;
    for (const auto& l : labels) {
        const float w = ImGui::CalcTextSize(l.c_str(), nullptr, true).x + metrics::U(9.0f);
        if (x > 0.0f && x + w > width) {
            ++rows;
            x = 0.0f;
        }
        x += w;
    }
    return rows;
}
}  // namespace

void HydraulicPanel::drawPlots(AppState& st, float width, float height) {
    if (!sim_.is_object() || sim_.empty()) {
        if (store_.inflight() > 0)
            kit::spinner("Simulating\xe2\x80\xa6");
        else if (kit::emptyState("plots_view", "No simulation results yet",
                                 "Set the valve schedule and loads in the side pane's Simulate form, then "
                                 "Simulate: node pressures, actuator positions and speeds plot here.",
                                 "Open Simulate"))
            handleCommand(st, "sim_hyd_simulate");
        return;
    }
    const float top = ImGui::GetCursorScreenPos().y;
    // Plots only: the outcome numbers and verdict live once, in the side pane's Outcome card.
    const double relief = reliefPressureBar();

    if (plotResultRevision_ != resultGeneration_ || plotCircuitRevision_ != circuitGeneration_ ||
        plotPick_ != seriesPick_) {
    plotTime_ = floats(sim_.value("time_s", json()));
    plotCells_.clear();
    plotKeys_.clear();
    auto& cells = plotCells_;
    using Cell = PlotCell;
    if (plotTime_.size() < 2) {
        kit::inlineStatus("The simulation returned no time series to plot.", kit::Tone::Warn);
        return;
    }
    // Node pressures, with the relief setting as the reference line. A node is keyed by its first
    // hose's id: it reads as that hose's net name, else its first member port ("Pump P").
    {
        Cell c{"Pressure per node", "load_pressure", "plot_p", "Pressure", "bar"};
        c.reliefRef = true;
        const json& nodes = jobj(sim_, "nodes");
        const json& hoses = jarr(circuit_, "hoses");
        int ci = 0;
        for (auto it = nodes.begin(); it != nodes.end(); ++it) {
            std::string label;
            for (const auto& hz : hoses)
                if (jstr(hz, "id") == it.key()) label = jstr(hz, "net_name");
            const json& members = jarr(*it, "members");
            if (label.empty() && !members.empty() && members[0].is_string()) {
                const std::string m = members[0].get<std::string>();
                const std::size_t dot = m.find('.');
                label = dot == std::string::npos ? componentName(m)
                                                 : componentName(m.substr(0, dot)) + " " + m.substr(dot + 1);
            }
            if (label.empty()) label = it.key();
            c.add(std::move(label), floats(it->value("pressure_bar", json())), ci++);
        }
        if (!c.ys.empty()) cells.push_back(std::move(c));
    }
    // Actuators: position and speed (cylinders), speed (motors), one color per component.
    const json& simComps = jobj(sim_, "components");
    {
        Cell pos{"Actuator position", "cylinder_feature", "plot_x", "Position", "mm"};
        Cell vel{"Actuator speed", "flow_arrow", "plot_v", "Speed", "mm/s"};
        Cell rpm{"Motor speed", "rotate_component", "plot_n", "Speed", "rpm"};
        int ci = 0;
        for (auto it = simComps.begin(); it != simComps.end(); ++it) {
            const json& series = jobj(*it, "series");
            const std::string label = componentName(it.key());
            const int ramp = ci++;
            if (series.contains("position_mm")) pos.add(label, floats(series["position_mm"]), ramp);
            if (series.contains("velocity_mms")) vel.add(label, floats(series["velocity_mms"]), ramp);
            if (series.contains("speed_rpm")) rpm.add(label, floats(series["speed_rpm"]), ramp);
        }
        for (Cell* c : {&pos, &vel, &rpm})
            if (!c->ys.empty()) cells.push_back(std::move(*c));
    }
    // Any other component series, picked by the user ("Pump · Flow").
    auto& keys = plotKeys_;
    std::vector<std::pair<std::string, std::string>> refs;
    for (auto it = simComps.begin(); it != simComps.end(); ++it) {
        const json& series = jobj(*it, "series");
        const std::string who = componentName(it.key());
        for (auto sit = series.begin(); sit != series.end(); ++sit) {
            keys.push_back(who + " \xc2\xb7 " + seriesName(sit.key()));
            refs.push_back({it.key(), sit.key()});
        }
    }
    if (!keys.empty()) {
        const int pick = std::clamp(seriesPick_, 0, (int)keys.size() - 1);
        const auto& ref = refs[pick];
        const json& arr = jobj(jobj(simComps, ref.first.c_str()), "series").value(ref.second, json());
        Cell c{"Component series", "results", "plot_any", seriesName(ref.second), seriesUnit(ref.second)};
        c.picker = true;
        c.add(keys[pick], floats(arr), 0);
        cells.push_back(std::move(c));
    }
    plotResultRevision_ = resultGeneration_;
    plotCircuitRevision_ = circuitGeneration_;
    plotPick_ = seriesPick_;
    }
    const auto& t = plotTime_;
    const auto& cells = plotCells_;
    const auto& keys = plotKeys_;
    if (cells.empty()) {
        kit::inlineStatus("The simulation returned no node or actuator series to plot.", kit::Tone::Warn);
        return;
    }

    // Grid of full-size plots filling the rest of the view: two columns when there is room. Every
    // cell has the same anatomy and heights: title row, plot, then one band under the plot that
    // names its series — the wrapping legend of a multi-series plot, or the labelled Series picker
    // of the component-series plot. The band is as tall as the tallest cell needs.
    const float gap = 2.0f * kit::rowGap();
    const int n = (int)cells.size();
    const int cols = n > 1 && width >= metrics::px(560.0f) ? 2 : 1;
    const int rows = (n + cols - 1) / cols;
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const float remH = height - (origin.y - top);
    const float cellW = (width - gap * (cols - 1)) / cols;
    const float cellH = std::max((remH - gap * (rows - 1)) / rows, metrics::px(140.0f));
    const float headerH = std::max(ImGui::GetTextLineHeight(), metrics::iconHeader()) + kit::headGap();
    const float spacingY = ImGui::GetStyle().ItemSpacing.y;
    float bandH = 0.0f;
    for (const PlotCell& c : cells) {
        const float need = c.picker ? ImGui::GetFrameHeight() + spacingY
                                    : (float)legendRows(c.labels, cellW) * ImGui::GetTextLineHeightWithSpacing();
        bandH = std::max(bandH, need);
    }
    const float minPlotH = 6.0f * ImGui::GetTextLineHeight();
    for (int i = 0; i < n; ++i) {
        const PlotCell& c = cells[i];
        const int row = i / cols, col = i % cols;
        ImGui::SetCursorScreenPos(ImVec2(origin.x + col * (cellW + gap), origin.y + row * (cellH + gap)));
        if (ImGui::BeginChild(c.id, ImVec2(cellW, cellH), ImGuiChildFlags_None,
                              ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse |
                                  ImGuiWindowFlags_NoBackground)) {
            const ImVec2 cellMin = ImGui::GetCursorScreenPos();
            kit::sectionHeader(c.title, c.icon, nullptr, -1, true);
            ImGui::SetCursorScreenPos(ImVec2(cellMin.x, std::max(ImGui::GetCursorScreenPos().y, cellMin.y + headerH)));
            kit::PlotSpec p;
            p.x = t;
            p.xAxis.title = "Time";
            p.xAxis.unit = kit::unitText(kit::Unit::s);
            p.yAxis.title = c.yTitle.c_str();
            p.yAxis.unit = c.unit.empty() ? nullptr : kit::unitText(c.unit.c_str());
            for (std::size_t k = 0; k < c.ys.size(); ++k) {
                kit::PlotSeries ps;
                ps.label = c.labels[k].c_str();
                ps.y = c.ys[k];
                ps.ramp = c.ramps[k];
                p.series.push_back(ps);
            }
            if (c.reliefRef && relief > 0.0) p.refs.push_back(kit::PlotRef::h(relief, "Relief setting"));
            p.legend = true;  // drawn under the plot whenever it has two or more series
            // Size from where the header actually left the cursor, so the band under the plot
            // (legend rows or the Series picker) always fits inside the cell instead of clipping.
            const float avail = cellMin.y + cellH - ImGui::GetCursorScreenPos().y;
            p.height = std::max(avail - bandH - spacingY, minPlotH);
            p.emptyText = "No samples in this series.";
            kit::linePlot(c.id, p);
            if (c.picker) {
                kit::FieldGroup g("series_pick", "Series");
                int pick = std::clamp(seriesPick_, 0, (int)keys.size() - 1);
                if (kit::fieldCombo("series", "Series", &pick, keys,
                                    kit::FieldOpts().withTip("Any series the simulator returned for a component.")))
                    seriesPick_ = pick;
            }
        }
        ImGui::EndChild();
    }
}

}  // namespace ui
