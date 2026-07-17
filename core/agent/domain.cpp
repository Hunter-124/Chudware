// core/agent/domain.cpp — see domain.h.
#include "agent/domain.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <utility>

namespace aicad {
namespace agent {

namespace {

std::string lower(const std::string& s) {
    std::string o = s;
    std::transform(o.begin(), o.end(), o.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return o;
}

bool has_word(const std::string& hay, const std::string& w) {
    size_t p = hay.find(w);
    while (p != std::string::npos) {
        const bool lok = (p == 0) || !std::isalnum((unsigned char)hay[p - 1]);
        const size_t e = p + w.size();
        const bool rok = (e >= hay.size()) || !std::isalnum((unsigned char)hay[e]);
        if (lok && rok) return true;
        p = hay.find(w, p + 1);
    }
    return false;
}

bool any_word(const std::string& hay, std::initializer_list<const char*> ws) {
    for (const char* w : ws)
        if (has_word(hay, w)) return true;
    return false;
}

}  // namespace

const char* DomainVerdict::label() const {
    switch (domain) {
        case Domain::Modeling: return "modeling";
        case Domain::Sheetmetal: return "sheetmetal";
        case Domain::Assembly: return "assembly";
        case Domain::Cam: return "cam";
        case Domain::Ecad: return "ecad";
        case Domain::Drawing: return "drawing";
        case Domain::Simulation: return "simulation";
        case Domain::Sourcing: return "sourcing";
        case Domain::Research: return "research";
    }
    return "modeling";
}

DomainVerdict classify_domain(const std::string& request) {
    const std::string r = lower(request);

    // Order matters: the most specific / highest-tooling-cost domains win first so
    // we never load their heavy toolsets for an unrelated request.
    if (any_word(r, {"pcb", "schematic", "circuit", "netlist", "footprint", "kicad",
                     "trace", "copper", "gerber", "erc", "drc", "resistor", "capacitor",
                     "voltage", "microcontroller", "mcu"}) ||
        r.find("printed circuit") != std::string::npos)
        return {Domain::Ecad, "electronics/PCB terms"};

    if (any_word(r, {"toolpath", "gcode", "g-code", "machining", "mill", "milling",
                     "lathe", "cnc", "post", "setup", "stock", "feeds", "speeds",
                     "endmill", "roughing", "finishing"}) ||
        r.find("cam job") != std::string::npos)
        return {Domain::Cam, "CAM/machining terms"};

    if (any_word(r, {"fea", "cfd", "stress", "strain", "thermal", "modal", "buckling",
                     "fatigue", "simulation", "simulate", "deflection", "displacement",
                     "airflow", "aerodynamic", "factor of safety"}))
        return {Domain::Simulation, "simulation/analysis terms"};

    if (any_word(r, {"drawing", "dimension", "gd&t", "gdt", "titleblock", "section view",
                     "detail view", "annotate", "tolerance callout"}) ||
        r.find("engineering drawing") != std::string::npos)
        return {Domain::Drawing, "2D drawing terms"};

    if (any_word(r, {"assembly", "assemble", "mate", "mates", "components", "fastened",
                     "bolt together", "linkage", "mechanism", "gearbox"}))
        return {Domain::Assembly, "assembly/mate terms"};

    if (any_word(r, {"sheetmetal", "sheet metal", "flange", "hem", "bend", "jog",
                     "flat pattern", "gauge"}) ||
        r.find("sheet-metal") != std::string::npos)
        return {Domain::Sheetmetal, "sheet-metal terms"};

    if (any_word(r, {"bom", "vendor", "sourcing", "purchase", "catalog", "part number",
                     "mpn", "datasheet", "off-the-shelf", "mcmaster"}))
        return {Domain::Sourcing, "sourcing/BOM terms"};

    if (any_word(r, {"research", "look up", "find out", "what is", "material properties",
                     "yield strength", "standard for", "specification"}) &&
        !any_word(r, {"model", "build", "make", "design", "part", "sketch"}))
        return {Domain::Research, "research/lookup, no geometry"};

    // Default: solid part modeling.
    return {Domain::Modeling, "solid modeling (default)"};
}

std::vector<std::string> tool_classes_for_domain(Domain d) {
    // Every agent gets the safe read/inspect + doc-query core; the domain adds its
    // own mutating class(es). "sourcing" is added where standard parts are relevant.
    std::vector<std::string> base = {"read", "inspect", "measure"};
    auto with = [&](std::initializer_list<const char*> extra) {
        for (const char* e : extra) base.push_back(e);
        return base;
    };
    switch (d) {
        case Domain::Modeling: return with({"modeling", "sketch", "sourcing"});
        case Domain::Sheetmetal: return with({"modeling", "sketch", "sheetmetal"});
        case Domain::Assembly: return with({"assembly", "modeling", "sourcing"});
        case Domain::Cam: return with({"cam"});
        case Domain::Ecad: return with({"ecad"});
        case Domain::Drawing: return with({"drawing"});
        case Domain::Simulation: return with({"simulation"});
        case Domain::Sourcing: return with({"sourcing"});
        case Domain::Research: return with({"research"});
    }
    return base;
}

std::vector<std::string> context_sheets_for_domain(Domain d) {
    switch (d) {
        case Domain::Modeling:
            return {"box", "cylinder", "extrude", "revolve", "loft", "sweep", "fillet",
                    "chamfer", "shell", "pattern", "mirror", "hole", "thread", "hardware"};
        case Domain::Sheetmetal: return {"sheetmetal", "box", "hole"};
        case Domain::Assembly: return {"hardware", "fits", "bearings", "threads"};
        case Domain::Cam: return {};        // CAM knowledge is in the CAM guide, not sheets
        case Domain::Ecad: return {"ecad"};
        case Domain::Drawing: return {"drawing"};
        case Domain::Simulation: return {"materials"};
        case Domain::Sourcing: return {"hardware", "fits", "threads", "bearings"};
        case Domain::Research: return {"research", "materials"};
    }
    return {};
}

}  // namespace agent
}  // namespace aicad
