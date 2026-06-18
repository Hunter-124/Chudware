// ECAD starter footprint library — see library.h. Port of the FOOTPRINTS table
// from engine/ecad/library/starter.py (_make_footprints + _smd_pad / _tht_pad /
// _fp / _silk_box / _crtyd_box). Built once into a static map; resolve_footprint
// returns a deep copy so callers can mutate freely (sync deep-copies pads anyway).

#include "ecad/library.h"

#include <map>
#include <mutex>
#include <vector>

namespace aicad::ecad::library {

using nlohmann::json;

namespace {

// engine/ecad/library/starter.py::_smd_pad
json smd_pad(const std::string& pid, const std::string& number, double x, double y,
             double w, double h, double rr = 0.0) {
    std::string shape = rr > 0.0 ? "roundrect" : "rect";
    return json{{"id", pid},
                {"number", number},
                {"pos", json::array({x, y})},
                {"angle", 0.0},
                {"shape", shape},
                {"size", json::array({w, h})},
                {"roundrect_ratio", rr},
                {"type", "smd"},
                {"layers", json::array({"F.Cu", "F.Mask", "F.Paste"})},
                {"drill", nullptr},
                {"net", nullptr},
                {"pin", number}};
}

// engine/ecad/library/starter.py::_tht_pad
json tht_pad(const std::string& pid, const std::string& number, double x, double y,
             double drill, double pad_w, double pad_h, const std::string& shape = "circle") {
    return json{{"id", pid},
                {"number", number},
                {"pos", json::array({x, y})},
                {"angle", 0.0},
                {"shape", shape},
                {"size", json::array({pad_w, pad_h})},
                {"roundrect_ratio", 0.0},
                {"type", "tht"},
                {"layers", json::array({"F.Cu", "B.Cu", "F.Mask", "B.Mask"})},
                {"drill", json{{"diameter", drill}, {"shape", "round"},
                               {"offset", json::array({0.0, 0.0})}}},
                {"net", nullptr},
                {"pin", number}};
}

// engine/ecad/library/starter.py::_silk_box (four-line rectangle on a layer)
json silk_box(double x1, double y1, double x2, double y2, const std::string& layer = "F.SilkS") {
    return json::array({
        json{{"type", "line"}, {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y1})}, {"layer", layer}},
        json{{"type", "line"}, {"p1", json::array({x2, y1})}, {"p2", json::array({x2, y2})}, {"layer", layer}},
        json{{"type", "line"}, {"p1", json::array({x2, y2})}, {"p2", json::array({x1, y2})}, {"layer", layer}},
        json{{"type", "line"}, {"p1", json::array({x1, y2})}, {"p2", json::array({x1, y1})}, {"layer", layer}},
    });
}

json crtyd_box(double x1, double y1, double x2, double y2) {
    return silk_box(x1, y1, x2, y2, "F.CrtYd");
}

// Concatenate two graphics arrays (silk box + courtyard box), like Python's `+`.
// NOTE: the params are taken BY VALUE deliberately. With `const json&` params,
// MSVC /O2 miscompiles this (the returned silk_box/crtyd_box temporaries alias
// through the references) and throws json error 302; a by-value copy of each
// argument breaks the aliasing and is robust under optimization.
json concat(json a, json b) {
    json out = json::array();
    for (std::size_t i = 0; i < a.size(); ++i) {
        json e = a.at(i);
        out.push_back(e);
    }
    for (std::size_t i = 0; i < b.size(); ++i) {
        json e = b.at(i);
        out.push_back(e);
    }
    return out;
}

// engine/ecad/library/starter.py::_fp
json fp(const std::string& fpid, json pads, json graphics, const std::string& attr = "smd",
        const std::string& description = "") {
    return json{{"fpid", fpid},
                {"pads", std::move(pads)},
                {"graphics", std::move(graphics)},
                {"model_3d", nullptr},
                {"model_3d_transform", nullptr},
                {"attr", attr},
                {"description", description}};
}

std::map<std::string, json> make_footprints() {
    std::map<std::string, json> fps;

    // R_0402
    fps["Resistor_SMD:R_0402"] = fp(
        "Resistor_SMD:R_0402",
        json::array({smd_pad("p1", "1", -0.5, 0.0, 0.5, 0.5, 0.25),
                     smd_pad("p2", "2", 0.5, 0.0, 0.5, 0.5, 0.25)}),
        concat(silk_box(-0.6, -0.3, 0.6, 0.3), crtyd_box(-0.9, -0.45, 0.9, 0.45)),
        "smd", "Resistor 0402");

    // R_0603
    fps["Resistor_SMD:R_0603"] = fp(
        "Resistor_SMD:R_0603",
        json::array({smd_pad("p1", "1", -0.8, 0.0, 0.9, 0.9, 0.25),
                     smd_pad("p2", "2", 0.8, 0.0, 0.9, 0.9, 0.25)}),
        concat(silk_box(-1.0, -0.5, 1.0, 0.5), crtyd_box(-1.3, -0.7, 1.3, 0.7)),
        "smd", "Resistor 0603");

    // R_0805
    fps["Resistor_SMD:R_0805"] = fp(
        "Resistor_SMD:R_0805",
        json::array({smd_pad("p1", "1", -1.0, 0.0, 1.15, 1.45, 0.25),
                     smd_pad("p2", "2", 1.0, 0.0, 1.15, 1.45, 0.25)}),
        concat(silk_box(-1.2, -0.7, 1.2, 0.7), crtyd_box(-1.7, -1.0, 1.7, 1.0)),
        "smd", "Resistor 0805");

    // R_1206
    fps["Resistor_SMD:R_1206"] = fp(
        "Resistor_SMD:R_1206",
        json::array({smd_pad("p1", "1", -1.6, 0.0, 1.60, 1.80, 0.25),
                     smd_pad("p2", "2", 1.6, 0.0, 1.60, 1.80, 0.25)}),
        concat(silk_box(-1.8, -0.9, 1.8, 0.9), crtyd_box(-2.3, -1.2, 2.3, 1.2)),
        "smd", "Resistor 1206");

    // C_0402
    fps["Capacitor_SMD:C_0402"] = fp(
        "Capacitor_SMD:C_0402",
        json::array({smd_pad("p1", "1", -0.5, 0.0, 0.5, 0.5, 0.25),
                     smd_pad("p2", "2", 0.5, 0.0, 0.5, 0.5, 0.25)}),
        concat(silk_box(-0.6, -0.3, 0.6, 0.3), crtyd_box(-0.9, -0.45, 0.9, 0.45)),
        "smd", "Capacitor 0402");

    // C_0603
    fps["Capacitor_SMD:C_0603"] = fp(
        "Capacitor_SMD:C_0603",
        json::array({smd_pad("p1", "1", -0.8, 0.0, 0.9, 0.9, 0.25),
                     smd_pad("p2", "2", 0.8, 0.0, 0.9, 0.9, 0.25)}),
        concat(silk_box(-1.0, -0.5, 1.0, 0.5), crtyd_box(-1.3, -0.7, 1.3, 0.7)),
        "smd", "Capacitor 0603");

    // C_0805
    fps["Capacitor_SMD:C_0805"] = fp(
        "Capacitor_SMD:C_0805",
        json::array({smd_pad("p1", "1", -1.0, 0.0, 1.15, 1.45, 0.25),
                     smd_pad("p2", "2", 1.0, 0.0, 1.15, 1.45, 0.25)}),
        concat(silk_box(-1.2, -0.7, 1.2, 0.7), crtyd_box(-1.7, -1.0, 1.7, 1.0)),
        "smd", "Capacitor 0805");

    // C_1206
    fps["Capacitor_SMD:C_1206"] = fp(
        "Capacitor_SMD:C_1206",
        json::array({smd_pad("p1", "1", -1.6, 0.0, 1.60, 1.80, 0.25),
                     smd_pad("p2", "2", 1.6, 0.0, 1.60, 1.80, 0.25)}),
        concat(silk_box(-1.8, -0.9, 1.8, 0.9), crtyd_box(-2.3, -1.2, 2.3, 1.2)),
        "smd", "Capacitor 1206");

    // SOT-23 (3 pads)
    fps["Package_TO_SOT_SMD:SOT-23"] = fp(
        "Package_TO_SOT_SMD:SOT-23",
        json::array({smd_pad("p1", "1", -0.95, -1.3, 0.55, 0.85, 0.25),
                     smd_pad("p2", "2", 0.95, -1.3, 0.55, 0.85, 0.25),
                     smd_pad("p3", "3", 0.0, 1.3, 0.55, 0.85, 0.25)}),
        concat(silk_box(-1.5, -1.9, 1.5, 1.9), crtyd_box(-1.8, -2.2, 1.8, 2.2)),
        "smd", "SOT-23 3-lead");

    // SOIC-8
    {
        json soic8 = json::array();
        for (int i = 0; i < 4; ++i) {
            double y = (1.5 - i) * 1.27;
            soic8.push_back(smd_pad("p" + std::to_string(i + 1), std::to_string(i + 1), -2.7, y, 0.6, 1.55, 0.25));
        }
        for (int i = 0; i < 4; ++i) {
            double y = (i - 1.5) * 1.27;
            soic8.push_back(smd_pad("p" + std::to_string(i + 5), std::to_string(i + 5), 2.7, y, 0.6, 1.55, 0.25));
        }
        fps["Package_SO:SOIC-8"] = fp(
            "Package_SO:SOIC-8", soic8,
            concat(silk_box(-2.0, -2.7, 2.0, 2.7), crtyd_box(-3.4, -3.1, 3.4, 3.1)),
            "smd", "SOIC-8");
    }

    // SOIC-14
    {
        json soic14 = json::array();
        for (int i = 0; i < 7; ++i) {
            double y = (3.0 - i) * 1.27;
            soic14.push_back(smd_pad("p" + std::to_string(i + 1), std::to_string(i + 1), -2.7, y, 0.6, 1.55, 0.25));
        }
        for (int i = 0; i < 7; ++i) {
            double y = (i - 3.0) * 1.27;
            soic14.push_back(smd_pad("p" + std::to_string(i + 8), std::to_string(i + 8), 2.7, y, 0.6, 1.55, 0.25));
        }
        fps["Package_SO:SOIC-14"] = fp(
            "Package_SO:SOIC-14", soic14,
            concat(silk_box(-2.0, -4.5, 2.0, 4.5), crtyd_box(-3.4, -4.9, 3.4, 4.9)),
            "smd", "SOIC-14");
    }

    // TO-92 (3-lead THT)
    fps["Package_TO_SOT_THT:TO-92"] = fp(
        "Package_TO_SOT_THT:TO-92",
        json::array({tht_pad("p1", "1", -2.54, 0.0, 0.72, 1.5, 1.5),
                     tht_pad("p2", "2", 0.0, 0.0, 0.72, 1.5, 1.5),
                     tht_pad("p3", "3", 2.54, 0.0, 0.72, 1.5, 1.5)}),
        concat(silk_box(-3.2, -1.5, 3.2, 1.5), crtyd_box(-3.7, -2.0, 3.7, 2.0)),
        "tht", "TO-92 3-lead");

    // DIP-8 (THT)
    {
        json dip8 = json::array();
        for (int i = 0; i < 4; ++i) {
            double y = (1.5 - i) * 2.54;
            dip8.push_back(tht_pad("p" + std::to_string(i + 1), std::to_string(i + 1), -3.81, y, 0.8, 1.6, 1.6));
        }
        for (int i = 0; i < 4; ++i) {
            double y = (i - 1.5) * 2.54;
            dip8.push_back(tht_pad("p" + std::to_string(i + 5), std::to_string(i + 5), 3.81, y, 0.8, 1.6, 1.6));
        }
        fps["Package_DIP:DIP-8"] = fp(
            "Package_DIP:DIP-8", dip8,
            concat(silk_box(-4.8, -5.1, 4.8, 5.1), crtyd_box(-5.4, -5.7, 5.4, 5.7)),
            "tht", "DIP-8 through-hole");
    }

    // DIP-14 (THT)
    {
        json dip14 = json::array();
        for (int i = 0; i < 7; ++i) {
            double y = (3.0 - i) * 2.54;
            dip14.push_back(tht_pad("p" + std::to_string(i + 1), std::to_string(i + 1), -3.81, y, 0.8, 1.6, 1.6));
        }
        for (int i = 0; i < 7; ++i) {
            double y = (i - 3.0) * 2.54;
            dip14.push_back(tht_pad("p" + std::to_string(i + 8), std::to_string(i + 8), 3.81, y, 0.8, 1.6, 1.6));
        }
        fps["Package_DIP:DIP-14"] = fp(
            "Package_DIP:DIP-14", dip14,
            concat(silk_box(-4.8, -8.9, 4.8, 8.9), crtyd_box(-5.4, -9.5, 5.4, 9.5)),
            "tht", "DIP-14 through-hole");
    }

    // Header_1x4_P2.54
    {
        json h1x4 = json::array();
        for (int i = 0; i < 4; ++i) {
            double y = (1.5 - i) * 2.54;
            h1x4.push_back(tht_pad("p" + std::to_string(i + 1), std::to_string(i + 1), 0.0, y, 1.0, 1.7, 1.7));
        }
        const std::string vert = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical";
        fps[vert] = fp(
            vert, h1x4,
            concat(silk_box(-1.0, -5.1, 1.0, 5.1), crtyd_box(-1.6, -5.7, 1.6, 5.7)),
            "tht", "1x4 pin header 2.54mm pitch");
        // alias shorter name (dict copy with fpid overwritten)
        json alias = fps[vert];
        alias["fpid"] = "Connector_PinHeader_2.54mm:Header_1x4";
        fps["Connector_PinHeader_2.54mm:Header_1x4"] = std::move(alias);
    }

    // LED_0805 SMD
    fps["LED_SMD:LED_0805"] = fp(
        "LED_SMD:LED_0805",
        json::array({smd_pad("p1", "K", -1.0, 0.0, 1.15, 1.45, 0.25),
                     smd_pad("p2", "A", 1.0, 0.0, 1.15, 1.45, 0.25)}),
        concat(silk_box(-1.2, -0.7, 1.2, 0.7), crtyd_box(-1.7, -1.0, 1.7, 1.0)),
        "smd", "LED 0805");

    // D_SMA SMD
    fps["Diode_SMD:D_SMA"] = fp(
        "Diode_SMD:D_SMA",
        json::array({smd_pad("p1", "K", -2.6, 0.0, 1.7, 2.4, 0.1),
                     smd_pad("p2", "A", 2.6, 0.0, 1.7, 2.4, 0.1)}),
        concat(silk_box(-3.0, -1.5, 3.0, 1.5), crtyd_box(-3.6, -2.0, 3.6, 2.0)),
        "smd", "Diode SMA");

    // D_SMB SMD
    fps["Diode_SMD:D_SMB"] = fp(
        "Diode_SMD:D_SMB",
        json::array({smd_pad("p1", "K", -3.3, 0.0, 2.2, 2.8, 0.1),
                     smd_pad("p2", "A", 3.3, 0.0, 2.2, 2.8, 0.1)}),
        concat(silk_box(-4.0, -1.8, 4.0, 1.8), crtyd_box(-4.6, -2.3, 4.6, 2.3)),
        "smd", "Diode SMB");

    return fps;
}

const std::map<std::string, json>& footprints() {
    static const std::map<std::string, json> kFps = make_footprints();
    return kFps;
}

}  // namespace

json resolve_footprint(const std::string& fpid) {
    const auto& fps = footprints();
    auto it = fps.find(fpid);
    if (it == fps.end()) return json(nullptr);
    return it->second;  // returns a copy (nlohmann::json value semantics)
}

}  // namespace aicad::ecad::library
