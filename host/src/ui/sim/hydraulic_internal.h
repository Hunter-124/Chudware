// HydraulicPanel internals shared by the core, canvas and form translation units.
// Type/parameter tables and port lookup have one definition in hydraulic_panel.cpp;
// the small formatting helpers are inline. Single-TU helpers stay file-local.
#pragma once

#include <string>
#include <vector>

#include "sim_common.h"

namespace ui::hydint {

using json = nlohmann::json;
using sim::countOf;
using sim::fmtText;
using sim::indexOf;
using sim::jarr;
using sim::jbool;
using sim::jhas;
using sim::jnum;
using sim::jobj;
using sim::jstr;
using sim::stripAdvice;
using sim::warningsList;

inline constexpr const char* kDash = "\xe2\x80\x94";
inline constexpr const char* kHydraulicComponentPayload = "CHUDWARE_HYD_COMPONENT";

// Numeric member `k` (times `scale`) as "value unit" with significant digits, or an em dash when
// it is absent. `unit` is any kit::unitText spelling ("bar", "L/min", "cc/rev").
inline std::string qty(const json& o, const char* k, const char* unit, double scale = 1.0, int sigFigs = 4) {
    if (!jhas(o, k)) return kDash;
    return kit::fmtQuantity(jnum(o, k) * scale, kit::unitText(unit), sigFigs);
}

// Display units: the shared ui::sim converter (sim_common.h). The circuit, its results and every
// stored member stay SI (bar, L/min, mm, ...); the pane, canvas and plots show and take input in
// the Simulation pane's unit system (SimManager::unitSys(st).us).
using sim::Disp;
using sim::disp;
using sim::fieldU;
using sim::fmtPower;
using sim::fmtU;
using sim::powerQty;
using sim::qtyU;
using sim::toDisplay;

struct ParamDef {
    const char* key;
    const char* label;
    const char* unit;                  // kit::unitText spelling; null = dimensionless
    double def;
    std::vector<const char*> choices;  // non-empty: a choice parameter
    bool intChoice;                    // choices are integers stored as numbers
    bool optional;                     // absent = engine default
    bool ratio;                        // 0..1, shown as a percentage
    const char* tip;
};

// Display conversion of a component parameter. Kv is a valve flow COEFFICIENT (m³/h at a 1 bar
// drop), not a flow: its US counterpart Cv is a different coefficient, so it is never converted.
inline Disp paramDisp(const ParamDef& pd, bool us) {
    if (pd.unit && std::string(pd.key) == "Kv") return {kit::unitText(pd.unit), 1.0, 0.0};
    return disp(pd.unit, us);
}
struct TypeDef {
    const char* type;
    const char* label;
    std::vector<ParamDef> params;
};

const std::vector<TypeDef>& typeDefs();
const TypeDef* typeDef(const std::string& type);
std::string typeLabel(const std::string& type);
json defaultParams(const TypeDef& d);
bool validComponentParams(const std::string& type, const json& params, std::string& error);
std::vector<std::string> portsOf(const json& comp);
bool portOptional(const std::string& type, const std::string& port);
std::vector<std::string> openRequiredPorts(const json& circuit, const json& component);
std::string portMeaning(const std::string& type, const std::string& port);

// "piston_axial" -> "Piston axial": the display word of a choice key.
inline std::string prettyChoice(const char* key) {
    std::string s = key;
    for (char& ch : s)
        if (ch == '_') ch = ' ';
    if (!s.empty() && s[0] >= 'a' && s[0] <= 'z') s[0] = (char)(s[0] - 'a' + 'A');
    return s;
}

}  // namespace ui::hydint
