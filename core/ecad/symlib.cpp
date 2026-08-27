#include "ecad/symlib.h"

#include "ecad/kicad.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <fstream>
#include <iomanip>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace chudware::ecad::symlib {

using nlohmann::json;

namespace {

constexpr double kGrid = 2.54;

json point(double x, double y) {
    return json::array({x, y});
}

json pin(const std::string& number, const std::string& name, double x, double y,
         double angle, const std::string& type, int unit = 1,
         const std::string& shape = "line", double length = kGrid) {
    return json{{"number", number}, {"name", name}, {"pos", point(x, y)},
                {"angle", angle}, {"length", length}, {"type", type},
                {"shape", shape}, {"unit", unit}};
}

json line(double x1, double y1, double x2, double y2) {
    return json{{"kind", "line"}, {"pts", json::array({point(x1, y1), point(x2, y2)})}};
}

json rect(double x1, double y1, double x2, double y2) {
    return json{{"kind", "rect"}, {"pts", json::array({point(x1, y1), point(x2, y2)})}};
}

json circle(double x, double y, double radius) {
    return json{{"kind", "circle"}, {"pts", json::array({point(x, y)})}, {"radius", radius}};
}

json polyline(std::initializer_list<std::pair<double, double>> points) {
    json pts = json::array();
    for (const auto& p : points) pts.push_back(point(p.first, p.second));
    return json{{"kind", "polyline"}, {"pts", std::move(pts)}};
}

json arc(double x, double y, double radius, double a0, double a1) {
    return json{{"kind", "arc"}, {"pts", json::array({point(x, y)})},
                {"radius", radius}, {"a0", a0}, {"a1", a1}};
}

json text(double x, double y, const std::string& value, double size = 1.27) {
    return json{{"kind", "text"}, {"pts", json::array({point(x, y)})},
                {"text", value}, {"size", size}};
}

json for_unit(json graphic, int unit) {
    graphic["unit"] = unit;
    return graphic;
}

json symbol(const std::string& libid, const std::string& name,
            const std::string& description, std::vector<std::string> keywords,
            const std::string& prefix, json pins, json graphics,
            const std::string& default_footprint = std::string(), bool power = false,
            int units = 1, json sim = json(),
            std::vector<std::string> footprint_filters = {}) {
    json def{{"libid", libid},
             {"name", name},
             {"description", description},
             {"keywords", std::move(keywords)},
             {"prefix", prefix},
             {"reference_prefix", prefix},
             {"value", name},
             {"power", power},
             {"units", units},
             {"units_count", units},
             {"default_footprint", default_footprint},
             {"footprint_filters", std::move(footprint_filters)},
             {"datasheet", ""},
             {"pins", std::move(pins)},
             {"graphics", std::move(graphics)},
             {"fields", json::object()}};
    if (!sim.is_null()) def["sim"] = std::move(sim);
    return def;
}

json resistor(const std::string& libid, const std::string& name,
              const std::string& description) {
    return symbol(
        libid, name, description, {"res", "resistor", "passive"}, "R",
        json::array({pin("1", "~", -kGrid, 0.0, 180.0, "passive"),
                     pin("2", "~", kGrid, 0.0, 0.0, "passive")}),
        json::array({rect(-1.016, -0.635, 1.016, 0.635),
                     line(-kGrid, 0.0, -1.016, 0.0), line(1.016, 0.0, kGrid, 0.0)}),
        "Resistor_SMD:R_0603", false, 1,
        json{{"model", "R"}, {"pin_order", json::array({"1", "2"})}}, {"R_*"});
}

json capacitor(const std::string& libid, const std::string& name,
               const std::string& description, bool polarized) {
    json graphics = json::array({line(-2.0, 0.5, 2.0, 0.5),
                                 line(-2.0, -0.5, 2.0, -0.5),
                                 line(0.0, kGrid, 0.0, 0.5),
                                 line(0.0, -0.5, 0.0, -kGrid)});
    if (polarized) {
        graphics.push_back(line(-0.5, 1.3, 0.5, 1.3));
        graphics.push_back(line(0.0, 0.8, 0.0, 1.8));
    }
    return symbol(
        libid, name, description,
        polarized ? std::vector<std::string>{"cap", "capacitor", "polarized", "electrolytic"}
                  : std::vector<std::string>{"cap", "capacitor", "passive"},
        "C",
        json::array({pin("1", polarized ? "+" : "~", 0.0, kGrid, 90.0, "passive"),
                     pin("2", polarized ? "-" : "~", 0.0, -kGrid, 270.0, "passive")}),
        std::move(graphics), "Capacitor_SMD:C_0603", false, 1,
        json{{"model", "C"}, {"pin_order", json::array({"1", "2"})}}, {"C_*"});
}

json inductor(const std::string& libid, const std::string& name,
              const std::string& description, bool bead = false) {
    json graphics = json::array({line(0.0, kGrid, 0.0, 1.9),
                                 arc(0.0, 1.27, 0.635, 90.0, -90.0),
                                 arc(0.0, 0.0, 0.635, 90.0, -90.0),
                                 arc(0.0, -1.27, 0.635, 90.0, -90.0),
                                 line(0.0, -1.9, 0.0, -kGrid)});
    if (bead) graphics.push_back(rect(-1.0, -1.8, 1.0, 1.8));
    return symbol(
        libid, name, description, {"ind", "inductor", bead ? "ferrite" : "coil", "choke"}, "L",
        json::array({pin("1", "~", 0.0, kGrid, 90.0, "passive"),
                     pin("2", "~", 0.0, -kGrid, 270.0, "passive")}),
        std::move(graphics), "", false, 1,
        json{{"model", "L"}, {"pin_order", json::array({"1", "2"})}}, {"L_*"});
}

json diode(const std::string& libid, const std::string& name,
           const std::string& description, const std::string& variant,
           const std::string& footprint = "Diode_SMD:D_SMA") {
    json graphics = json::array({polyline({{-1.27, -1.27}, {-1.27, 1.27}, {1.27, 0.0}, {-1.27, -1.27}}),
                                 line(-kGrid, 0.0, -1.27, 0.0),
                                 line(1.27, 0.0, kGrid, 0.0)});
    if (variant == "zener" || variant == "tvs") {
        graphics.push_back(polyline({{1.27, -1.27}, {0.95, -1.6}, {1.27, -1.27},
                                      {1.27, 1.27}, {1.59, 1.6}}));
        if (variant == "tvs")
            graphics.push_back(polyline({{-1.27, -1.27}, {-0.95, -1.6}, {-1.27, -1.27},
                                          {-1.27, 1.27}, {-1.59, 1.6}}));
    } else if (variant == "schottky") {
        graphics.push_back(line(1.27, -1.27, 1.27, 1.27));
        graphics.push_back(line(0.8, -1.27, 1.27, -1.27));
        graphics.push_back(line(1.27, 1.27, 1.74, 1.27));
    } else {
        graphics.push_back(line(1.27, -1.27, 1.27, 1.27));
    }
    if (variant == "led") {
        graphics.push_back(polyline({{0.4, 1.5}, {1.5, 2.6}, {1.1, 2.55}}));
        graphics.push_back(polyline({{-0.1, 1.8}, {1.0, 2.9}, {0.6, 2.85}}));
    }
    return symbol(
        libid, name, description, {"diode", variant, "semiconductor"}, "D",
        json::array({pin("K", "K", kGrid, 0.0, 0.0, "passive"),
                     pin("A", "A", -kGrid, 0.0, 180.0, "passive")}),
        std::move(graphics), footprint, false, 1,
        json{{"model", "D"}, {"pin_order", json::array({"A", "K"})}}, {"D_*"});
}

json transistor(const std::string& libid, const std::string& name,
                const std::string& description, bool fet, bool p_channel) {
    json pins = json::array();
    if (fet) {
        pins.push_back(pin("1", "G", -kGrid, 0.0, 180.0, "input"));
        pins.push_back(pin("2", "D", kGrid, 1.27, 0.0, "output"));
        pins.push_back(pin("3", "S", kGrid, -1.27, 0.0, "output"));
    } else {
        pins.push_back(pin("1", "B", -kGrid, 0.0, 180.0, "input"));
        pins.push_back(pin("2", "C", kGrid, 1.27, 0.0, "output"));
        pins.push_back(pin("3", "E", kGrid, -1.27, 0.0, "output"));
    }
    json graphics = json::array({circle(0.0, 0.0, 1.9), line(-kGrid, 0.0, -0.75, 0.0),
                                 line(-0.75, -1.4, -0.75, 1.4),
                                 line(-0.75, 0.7, kGrid, 1.27),
                                 line(-0.75, -0.7, kGrid, -1.27)});
    graphics.push_back(p_channel
        ? polyline({{0.5, -0.85}, {-0.1, -0.65}, {0.35, -0.25}})
        : polyline({{0.0, -0.35}, {0.55, -0.85}, {-0.1, -0.7}}));
    const std::string model = fet ? (p_channel ? "PMOS" : "NMOS")
                                  : (p_channel ? "PNP" : "NPN");
    return symbol(libid, name, description,
                  {fet ? "mosfet" : "bjt", p_channel ? "p-channel" : "n-channel", "transistor"},
                  "Q", std::move(pins), std::move(graphics), "Package_TO_SOT_SMD:SOT-23",
                  false, 1,
                  json{{"model", model}, {"pin_order", json::array({"2", "1", "3"})}},
                  {"SOT-23*"});
}

void add_power(std::map<std::string, json>& defs, const std::string& libid,
               const std::string& name, const std::string& description,
               const std::string& glyph, bool drive) {
    json graphics = json::array();
    double angle = 270.0;
    if (glyph == "ground") {
        angle = 90.0;
        graphics = json::array({line(0.0, 0.0, 0.0, -1.27),
                                line(-1.27, -1.27, 1.27, -1.27),
                                line(-0.762, -1.905, 0.762, -1.905),
                                line(-0.254, -2.54, 0.254, -2.54)});
    } else if (glyph == "analog_ground") {
        angle = 90.0;
        graphics = json::array({line(0.0, 0.0, 0.0, -0.8),
                                polyline({{-1.5, -0.8}, {1.5, -0.8}, {0.0, -2.54}, {-1.5, -0.8}})});
    } else if (glyph == "digital_ground") {
        angle = 90.0;
        graphics = json::array({line(0.0, 0.0, 0.0, -1.0),
                                rect(-1.27, -2.0, 1.27, -1.0), text(0.0, -1.5, "D", 0.8)});
    } else if (glyph == "flag") {
        angle = 90.0;
        graphics = json::array({line(0.0, 0.0, 0.0, -2.54),
                                polyline({{0.0, -2.54}, {1.9, -1.9}, {0.0, -1.27}})});
    } else if (glyph == "negative") {
        angle = 90.0;
        graphics = json::array({line(0.0, 0.0, 0.0, -1.27),
                                polyline({{-0.7, -0.7}, {0.0, -1.4}, {0.7, -0.7}})});
    } else {
        graphics = json::array({line(0.0, 0.0, 0.0, 1.27),
                                polyline({{-0.7, 0.7}, {0.0, 1.4}, {0.7, 0.7}})});
    }
    defs[libid] = symbol(
        libid, name, description, {"power", "supply", name}, "#PWR",
        json::array({pin("1", name, 0.0, 0.0, angle, drive ? "power_out" : "power_in")}),
        std::move(graphics), "", true, 1,
        glyph == "ground" ? json{{"model", "GND"}} : json());
}

json dual_analog(const std::string& libid, const std::string& name,
                 const std::string& description, bool open_collector) {
    json pins = json::array({
        pin("1", "OUTA", kGrid, 0.0, 0.0, open_collector ? "open_collector" : "output", 1),
        pin("2", "INA-", -kGrid, -1.27, 180.0, "input", 1),
        pin("3", "INA+", -kGrid, 1.27, 180.0, "input", 1),
        pin("7", "OUTB", kGrid, 0.0, 0.0, open_collector ? "open_collector" : "output", 2),
        pin("6", "INB-", -kGrid, -1.27, 180.0, "input", 2),
        pin("5", "INB+", -kGrid, 1.27, 180.0, "input", 2),
        pin("8", "V+", 0.0, kGrid, 90.0, "power_in", 3),
        pin("4", "V-", 0.0, -kGrid, 270.0, "power_in", 3)});
    json tri = polyline({{-kGrid, -2.0}, {-kGrid, 2.0}, {kGrid, 0.0}, {-kGrid, -2.0}});
    json graphics = json::array({for_unit(tri, 1), for_unit(tri, 2),
                                 for_unit(rect(-kGrid, -kGrid, kGrid, kGrid), 3),
                                 for_unit(text(0.0, 0.7, "V+", 0.8), 3),
                                 for_unit(text(0.0, -0.7, "V-", 0.8), 3)});
    return symbol(libid, name, description,
                  open_collector ? std::vector<std::string>{"comparator", "dual", "analog"}
                                 : std::vector<std::string>{"opamp", "operational", "amplifier", "dual", "analog"},
                  "U", std::move(pins), std::move(graphics), "Package_SO:SOIC-8",
                  false, 3, json(), {"SOIC-8*", "DIP-8*"});
}

void add_logic_array(std::map<std::string, json>& defs, const std::string& libid,
                     const std::string& name, const std::string& gate_name,
                     const std::vector<std::array<int, 3>>& gates, bool inverted,
                     bool schmitt) {
    json pins = json::array();
    json graphics = json::array();
    int unit = 1;
    for (const auto& gate : gates) {
        pins.push_back(pin(std::to_string(gate[0]), "A", -kGrid, 1.27, 180.0, "input", unit,
                           schmitt ? "clock" : "line"));
        if (gate[1] > 0)
            pins.push_back(pin(std::to_string(gate[1]), "B", -kGrid, -1.27, 180.0, "input", unit));
        pins.push_back(pin(std::to_string(gate[2]), "Y", kGrid, 0.0, 0.0, "output", unit,
                           inverted ? "inverted" : "line"));
        graphics.push_back(for_unit(rect(-kGrid, -1.9, kGrid, 1.9), unit));
        graphics.push_back(for_unit(text(0.0, 0.0, gate_name, 0.9), unit));
        ++unit;
    }
    pins.push_back(pin("14", "VCC", 0.0, kGrid, 90.0, "power_in", unit));
    pins.push_back(pin("7", "GND", 0.0, -kGrid, 270.0, "power_in", unit));
    graphics.push_back(for_unit(rect(-kGrid, -kGrid, kGrid, kGrid), unit));
    defs[libid] = symbol(libid, name, name + " " + gate_name + " logic",
                         {"logic", "74hc", gate_name, schmitt ? "schmitt" : "gate"}, "U",
                         std::move(pins), std::move(graphics), "Package_SO:SOIC-14",
                         false, unit, json(), {"SOIC-14*", "DIP-14*"});
}

void add_connector_family(std::map<std::string, json>& defs) {
    for (int count = 2; count <= 8; ++count) {
        std::ostringstream suffix;
        suffix << "Conn_01x" << std::setw(2) << std::setfill('0') << count;
        const std::string name = suffix.str();
        const std::string libid = "Connector_Generic:" + name;
        json pins = json::array();
        json graphics = json::array();
        const double top = (static_cast<double>(count) - 1.0) * kGrid / 2.0;
        for (int i = 0; i < count; ++i) {
            const double y = top - static_cast<double>(i) * kGrid;
            pins.push_back(pin(std::to_string(i + 1), "Pin_" + std::to_string(i + 1),
                               -kGrid, y, 180.0, "passive"));
            graphics.push_back(circle(-1.27, y, 0.35));
        }
        graphics.push_back(rect(-kGrid, -top - 1.0, kGrid, top + 1.0));
        const std::string fp = count == 4
            ? "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical" : "";
        defs[libid] = symbol(libid, name, "Generic " + std::to_string(count) + "-pin connector",
                             {"connector", "header", "1x" + std::to_string(count)}, "J",
                             std::move(pins), std::move(graphics), fp, false, 1, json(),
                             {"PinHeader_1x*", "Connector*"});
    }
}

void add_spice_source(std::map<std::string, json>& defs, const std::string& libid,
                      const std::string& name, const std::string& prefix) {
    defs[libid] = symbol(
        libid, name, "SPICE " + name + " independent source",
        {"spice", "simulation", prefix == "V" ? "voltage" : "current", "source"}, prefix,
        json::array({pin("1", "+", 0.0, kGrid, 90.0, "power_out"),
                     pin("2", "-", 0.0, -kGrid, 270.0, "passive")}),
        json::array({circle(0.0, 0.0, 1.5), text(0.0, 0.55, "+", 0.8),
                     text(0.0, -0.55, "-", 0.8), line(0.0, kGrid, 0.0, 1.5),
                     line(0.0, -1.5, 0.0, -kGrid)}),
        "", false, 1,
        json{{"model", prefix}, {"pin_order", json::array({"1", "2"})}});
}

std::map<std::string, json> make_catalog() {
    std::map<std::string, json> defs;

    defs["Device:R"] = resistor("Device:R", "R", "Resistor");
    defs["Device:R_Small"] = resistor("Device:R_Small", "R_Small", "Small resistor symbol");
    defs["Device:R_Potentiometer"] = symbol(
        "Device:R_Potentiometer", "R_Potentiometer", "Potentiometer",
        {"resistor", "potentiometer", "variable", "wiper"}, "R",
        json::array({pin("1", "1", -kGrid, 0.0, 180.0, "passive"),
                     pin("2", "W", 0.0, kGrid, 90.0, "passive"),
                     pin("3", "3", kGrid, 0.0, 0.0, "passive")}),
        json::array({rect(-1.016, -0.635, 1.016, 0.635),
                     line(-kGrid, 0.0, -1.016, 0.0), line(1.016, 0.0, kGrid, 0.0),
                     line(0.0, kGrid, 0.0, 1.0),
                     polyline({{-0.35, 1.35}, {0.0, 0.8}, {0.35, 1.35}})}),
        "Resistor_SMD:R_0603", false, 1, json(), {"R_*"});
    defs["Device:C"] = capacitor("Device:C", "C", "Capacitor, non-polarized", false);
    defs["Device:C_Small"] = capacitor("Device:C_Small", "C_Small", "Small capacitor symbol", false);
    defs["Device:C_Polarized"] = capacitor("Device:C_Polarized", "C_Polarized", "Polarized capacitor", true);
    defs["Device:L"] = inductor("Device:L", "L", "Inductor");
    defs["Device:L_Small"] = inductor("Device:L_Small", "L_Small", "Small inductor symbol");
    defs["Device:Ferrite_Bead"] = inductor("Device:Ferrite_Bead", "Ferrite_Bead", "Ferrite bead", true);

    defs["Device:Crystal"] = symbol(
        "Device:Crystal", "Crystal", "Two-terminal crystal resonator", {"crystal", "resonator", "clock"}, "Y",
        json::array({pin("1", "X1", -kGrid, 0.0, 180.0, "passive"),
                     pin("2", "X2", kGrid, 0.0, 0.0, "passive")}),
        json::array({rect(-0.9, -1.6, 0.9, 1.6), line(-1.4, -1.6, -1.4, 1.6),
                     line(1.4, -1.6, 1.4, 1.6), line(-kGrid, 0.0, -1.4, 0.0),
                     line(1.4, 0.0, kGrid, 0.0)}));
    defs["Device:Crystal_GND24"] = symbol(
        "Device:Crystal_GND24", "Crystal_GND24", "Four-pad crystal with grounded case pads",
        {"crystal", "resonator", "grounded", "4pad"}, "Y",
        json::array({pin("1", "X1", -kGrid, 0.0, 180.0, "passive"),
                     pin("2", "GND", 0.0, -kGrid, 270.0, "power_in"),
                     pin("3", "X2", kGrid, 0.0, 0.0, "passive"),
                     pin("4", "GND", 0.0, kGrid, 90.0, "power_in")}),
        json::array({rect(-1.27, -1.27, 1.27, 1.27), line(-kGrid, 0.0, -1.27, 0.0),
                     line(1.27, 0.0, kGrid, 0.0), line(0.0, -kGrid, 0.0, -1.27),
                     line(0.0, 1.27, 0.0, kGrid)}));
    defs["Device:Fuse"] = symbol(
        "Device:Fuse", "Fuse", "Fuse", {"fuse", "protection", "passive"}, "F",
        json::array({pin("1", "1", -kGrid, 0.0, 180.0, "passive"),
                     pin("2", "2", kGrid, 0.0, 0.0, "passive")}),
        json::array({rect(-1.27, -0.55, 1.27, 0.55), line(-kGrid, 0.0, -1.27, 0.0),
                     line(1.27, 0.0, kGrid, 0.0), arc(0.0, 0.0, 0.65, 180.0, 0.0)}));
    defs["Device:Thermistor_NTC"] = resistor("Device:Thermistor_NTC", "Thermistor_NTC", "NTC thermistor");
    defs["Device:Thermistor_NTC"]["keywords"] = json::array({"thermistor", "ntc", "temperature", "resistor"});
    defs["Device:Thermistor_NTC"]["graphics"].push_back(line(-1.4, 1.2, 1.4, -1.2));
    defs["Device:Thermistor_NTC"]["graphics"].push_back(text(1.1, 1.0, "-t", 0.7));
    defs["Device:Varistor"] = resistor("Device:Varistor", "Varistor", "Voltage-dependent resistor");
    defs["Device:Varistor"]["keywords"] = json::array({"varistor", "mov", "surge", "protection"});
    defs["Device:Varistor"]["graphics"].push_back(polyline({{-1.5, 1.2}, {-0.8, 0.7}, {0.8, -0.7}, {1.5, -1.2}}));

    defs["Device:D"] = diode("Device:D", "D", "Rectifier diode", "rectifier");
    defs["Device:D_Schottky"] = diode("Device:D_Schottky", "D_Schottky", "Schottky diode", "schottky");
    defs["Device:D_Zener"] = diode("Device:D_Zener", "D_Zener", "Zener diode", "zener");
    defs["Device:D_TVS"] = diode("Device:D_TVS", "D_TVS", "Transient-voltage suppressor diode", "tvs");
    defs["Device:LED"] = diode("Device:LED", "LED", "Light-emitting diode", "led", "LED_SMD:LED_0805");
    defs["Device:D_Bridge_+-AA"] = symbol(
        "Device:D_Bridge_+-AA", "D_Bridge_+-AA", "Full-wave bridge rectifier",
        {"diode", "bridge", "rectifier", "ac"}, "D",
        json::array({pin("1", "+", 0.0, kGrid, 90.0, "passive"),
                     pin("2", "-", 0.0, -kGrid, 270.0, "passive"),
                     pin("3", "AC", -kGrid, 0.0, 180.0, "passive"),
                     pin("4", "AC", kGrid, 0.0, 0.0, "passive")}),
        json::array({circle(0.0, 0.0, 1.8), polyline({{0.0, 1.8}, {1.8, 0.0}, {0.0, -1.8}, {-1.8, 0.0}, {0.0, 1.8}}),
                     text(0.0, 0.8, "+", 0.8), text(0.0, -0.8, "-", 0.8)}));

    defs["Device:Q_NPN_BCE"] = transistor("Device:Q_NPN_BCE", "Q_NPN_BCE", "NPN bipolar transistor, BCE pin order", false, false);
    defs["Device:Q_PNP_BCE"] = transistor("Device:Q_PNP_BCE", "Q_PNP_BCE", "PNP bipolar transistor, BCE pin order", false, true);
    defs["Device:Q_NMOS_GDS"] = transistor("Device:Q_NMOS_GDS", "Q_NMOS_GDS", "N-channel MOSFET, GDS pin order", true, false);
    defs["Device:Q_PMOS_GDS"] = transistor("Device:Q_PMOS_GDS", "Q_PMOS_GDS", "P-channel MOSFET, GDS pin order", true, true);

    add_power(defs, "power:GND", "GND", "Ground power symbol", "ground", false);
    add_power(defs, "power:GNDA", "GNDA", "Analog ground power symbol", "analog_ground", false);
    add_power(defs, "power:GNDD", "GNDD", "Digital ground power symbol", "digital_ground", false);
    add_power(defs, "power:VCC", "VCC", "Positive logic supply", "positive", false);
    add_power(defs, "power:VDD", "VDD", "Positive device supply", "positive", false);
    add_power(defs, "power:VSS", "VSS", "Negative or ground device supply", "negative", false);
    add_power(defs, "power:+3V3", "+3V3", "+3.3 volt supply", "positive", false);
    add_power(defs, "power:+5V", "+5V", "+5 volt supply", "positive", false);
    add_power(defs, "power:+12V", "+12V", "+12 volt supply", "positive", false);
    add_power(defs, "power:-12V", "-12V", "-12 volt supply", "negative", false);
    add_power(defs, "power:VBUS", "VBUS", "USB bus power supply", "positive", false);
    add_power(defs, "power:PWR_FLAG", "PWR_FLAG", "Explicit ERC power source flag", "flag", true);

    defs["Amplifier_Operational:LM358"] = dual_analog(
        "Amplifier_Operational:LM358", "LM358", "LM358 dual operational amplifier (two opamp units plus power unit)", false);
    defs["Amplifier_Operational:OPAMP_Dual_Generic"] = dual_analog(
        "Amplifier_Operational:OPAMP_Dual_Generic", "OPAMP_Dual_Generic", "Generic dual opamp (two amplifier units plus power unit)", false);
    defs["Comparator:LM393"] = dual_analog(
        "Comparator:LM393", "LM393", "LM393 dual open-collector comparator (two comparator units plus power unit)", true);
    defs["Timer:NE555"] = symbol(
        "Timer:NE555", "NE555", "NE555 timer", {"timer", "555", "oscillator", "monostable"}, "U",
        json::array({pin("1", "GND", 0.0, -kGrid, 270.0, "power_in"),
                     pin("2", "TR", -kGrid, 1.27, 180.0, "input"),
                     pin("3", "Q", kGrid, 1.27, 0.0, "output"),
                     pin("4", "R", -kGrid, -1.27, 180.0, "input"),
                     pin("5", "CV", kGrid, -1.27, 0.0, "input"),
                     pin("6", "THR", -kGrid, 0.0, 180.0, "input"),
                     pin("7", "DISCH", kGrid, 0.0, 0.0, "open_collector"),
                     pin("8", "VCC", 0.0, kGrid, 90.0, "power_in")}),
        json::array({rect(-kGrid, -kGrid, kGrid, kGrid), text(0.0, 0.0, "555", 1.0)}),
        "Package_SO:SOIC-8", false, 1, json(), {"SOIC-8*", "DIP-8*"});
    defs["Reference:LM4040"] = symbol(
        "Reference:LM4040", "LM4040", "Precision shunt voltage reference", {"reference", "shunt", "voltage", "precision"}, "U",
        json::array({pin("1", "K", kGrid, 0.0, 0.0, "passive"),
                     pin("2", "A", -kGrid, 0.0, 180.0, "passive"),
                     pin("3", "NC", 0.0, -kGrid, 270.0, "no_connect")}),
        json::array({polyline({{-1.27, -1.27}, {-1.27, 1.27}, {1.27, 0.0}, {-1.27, -1.27}}),
                     polyline({{1.27, -1.27}, {0.95, -1.6}, {1.27, -1.27}, {1.27, 1.27}, {1.59, 1.6}})}),
        "Package_TO_SOT_SMD:SOT-23");

    auto regulator = [&](const std::string& libid, const std::string& name, bool tab) {
        json pins = json::array({pin("1", "GND", 0.0, -kGrid, 270.0, "power_in"),
                                 pin("2", "OUT", kGrid, 0.0, 0.0, "power_out"),
                                 pin("3", "IN", -kGrid, 0.0, 180.0, "power_in")});
        if (tab) pins.push_back(pin("4", "OUT", kGrid, -1.27, 0.0, "power_out"));
        defs[libid] = symbol(libid, name, name + " linear voltage regulator",
                             {"regulator", "linear", "ldo", name}, "U", std::move(pins),
                             json::array({rect(-kGrid, -kGrid, kGrid, kGrid), text(0.0, 0.0, name, 0.65)}));
    };
    regulator("Regulator_Linear:AMS1117-3.3", "AMS1117-3.3", true);
    regulator("Regulator_Linear:LM7805_TO220", "LM7805_TO220", false);
    regulator("Regulator_Linear:LD1117S33TR_SOT223", "LD1117S33TR_SOT223", true);
    defs["Regulator_Switching:Generic_Buck"] = symbol(
        "Regulator_Switching:Generic_Buck", "Generic_Buck", "Generic switching buck regulator",
        {"regulator", "switching", "buck", "dc-dc"}, "U",
        json::array({pin("1", "VIN", -kGrid, 1.27, 180.0, "power_in"),
                     pin("2", "GND", 0.0, -kGrid, 270.0, "power_in"),
                     pin("3", "EN", -kGrid, -1.27, 180.0, "input"),
                     pin("4", "SW", kGrid, 1.27, 0.0, "output"),
                     pin("5", "FB", kGrid, -1.27, 0.0, "input"),
                     pin("6", "VOUT", kGrid, 0.0, 0.0, "power_out")}),
        json::array({rect(-kGrid, -kGrid, kGrid, kGrid), text(0.0, 0.0, "BUCK", 0.75)}));

    const std::vector<std::array<int, 3>> quad_gates{{{1, 2, 3}}, {{4, 5, 6}},
                                                      {{9, 10, 8}}, {{12, 13, 11}}};
    const std::vector<std::array<int, 3>> hex_gates{{{1, 0, 2}}, {{3, 0, 4}}, {{5, 0, 6}},
                                                     {{9, 0, 8}}, {{11, 0, 10}}, {{13, 0, 12}}};
    add_logic_array(defs, "74xx:74HC00", "74HC00", "NAND", quad_gates, true, false);
    add_logic_array(defs, "74xx:74HC08", "74HC08", "AND", quad_gates, false, false);
    add_logic_array(defs, "74xx:74HC04", "74HC04", "NOT", hex_gates, true, false);
    add_logic_array(defs, "74xx:74HC14", "74HC14", "NOT", hex_gates, true, true);
    defs["74xx:74HC595"] = symbol(
        "74xx:74HC595", "74HC595", "8-bit serial-in parallel-out shift register",
        {"logic", "shift", "register", "serial", "parallel"}, "U",
        json::array({pin("1", "QB", kGrid, 3.81, 0.0, "output"),
                     pin("2", "QC", kGrid, 2.54, 0.0, "output"),
                     pin("3", "QD", kGrid, 1.27, 0.0, "output"),
                     pin("4", "QE", kGrid, 0.0, 0.0, "output"),
                     pin("5", "QF", kGrid, -1.27, 0.0, "output"),
                     pin("6", "QG", kGrid, -2.54, 0.0, "output"),
                     pin("7", "QH", kGrid, -3.81, 0.0, "output"),
                     pin("8", "GND", 0.0, -5.08, 270.0, "power_in"),
                     pin("9", "QH'", kGrid, -5.08, 0.0, "output"),
                     pin("10", "~SRCLR", -kGrid, -2.54, 180.0, "input", 1, "inverted"),
                     pin("11", "SRCLK", -kGrid, 0.0, 180.0, "input", 1, "clock"),
                     pin("12", "RCLK", -kGrid, 1.27, 180.0, "input", 1, "clock"),
                     pin("13", "~OE", -kGrid, -3.81, 180.0, "input", 1, "inverted"),
                     pin("14", "SER", -kGrid, 2.54, 180.0, "input"),
                     pin("15", "QA", kGrid, 5.08, 0.0, "output"),
                     pin("16", "VCC", 0.0, 5.08, 90.0, "power_in")}),
        json::array({rect(-kGrid, -5.08, kGrid, 5.08), text(0.0, 0.0, "595", 0.9)}));

    add_connector_family(defs);
    {
        json pins = json::array();
        json graphics = json::array({rect(-kGrid, -5.8, kGrid, 5.8)});
        for (int row = 0; row < 5; ++row) {
            const double y = 5.08 - row * kGrid;
            const int odd = row * 2 + 1;
            const int even = odd + 1;
            pins.push_back(pin(std::to_string(odd), "Pin_" + std::to_string(odd), -kGrid, y, 180.0, "passive"));
            pins.push_back(pin(std::to_string(even), "Pin_" + std::to_string(even), kGrid, y, 0.0, "passive"));
            graphics.push_back(circle(-1.0, y, 0.3));
            graphics.push_back(circle(1.0, y, 0.3));
        }
        defs["Connector_Generic:Conn_02x05_Odd_Even"] = symbol(
            "Connector_Generic:Conn_02x05_Odd_Even", "Conn_02x05_Odd_Even",
            "Two-row 10-pin connector, odd/even numbering", {"connector", "header", "2x5", "odd", "even"},
            "J", std::move(pins), std::move(graphics));
    }
    defs["Connector:USB_B_Micro"] = symbol(
        "Connector:USB_B_Micro", "USB_B_Micro", "USB Micro-B receptacle", {"connector", "usb", "micro", "data"}, "J",
        json::array({pin("1", "VBUS", -kGrid, 2.54, 180.0, "power_in"),
                     pin("2", "D-", -kGrid, 1.27, 180.0, "bidirectional"),
                     pin("3", "D+", -kGrid, 0.0, 180.0, "bidirectional"),
                     pin("4", "ID", -kGrid, -1.27, 180.0, "passive"),
                     pin("5", "GND", -kGrid, -2.54, 180.0, "power_in")}),
        json::array({rect(-kGrid, -3.2, kGrid, 3.2), polyline({{0.0, 1.5}, {0.0, -1.5}, {-0.6, -0.9}, {0.0, -1.5}, {0.6, -0.9}})}));
    defs["Connector:Barrel_Jack"] = symbol(
        "Connector:Barrel_Jack", "Barrel_Jack", "DC barrel jack with switched contact", {"connector", "barrel", "dc", "power"}, "J",
        json::array({pin("1", "CENTER", -kGrid, 1.27, 180.0, "passive"),
                     pin("2", "SLEEVE", -kGrid, -1.27, 180.0, "passive"),
                     pin("3", "SW", kGrid, -1.27, 0.0, "passive")}),
        json::array({circle(0.0, 0.0, 1.5), circle(0.0, 0.0, 0.45), line(0.0, -1.5, 0.0, -kGrid)}));
    defs["Connector:TestPoint"] = symbol(
        "Connector:TestPoint", "TestPoint", "Single electrical test point", {"connector", "test", "probe", "point"}, "TP",
        json::array({pin("1", "1", 0.0, -kGrid, 270.0, "passive")}),
        json::array({circle(0.0, 0.0, 0.8), line(0.0, -0.8, 0.0, -kGrid)}));
    defs["Connector:Screw_Terminal_01x02"] = symbol(
        "Connector:Screw_Terminal_01x02", "Screw_Terminal_01x02", "Two-position screw terminal", {"connector", "screw", "terminal", "2pin"}, "J",
        json::array({pin("1", "1", -kGrid, 1.27, 180.0, "passive"),
                     pin("2", "2", -kGrid, -1.27, 180.0, "passive")}),
        json::array({rect(-kGrid, -2.0, kGrid, 2.0), circle(0.0, 1.27, 0.7),
                     circle(0.0, -1.27, 0.7), line(-0.45, 0.82, 0.45, 1.72),
                     line(-0.45, -1.72, 0.45, -0.82)}));

    defs["Switch:SW_Push"] = symbol(
        "Switch:SW_Push", "SW_Push", "Momentary normally-open push switch", {"switch", "push", "momentary", "normally-open"}, "SW",
        json::array({pin("1", "1", -kGrid, 0.0, 180.0, "passive"),
                     pin("2", "2", kGrid, 0.0, 0.0, "passive")}),
        json::array({circle(-1.0, 0.0, 0.25), circle(1.0, 0.0, 0.25),
                     line(-1.0, 0.0, 0.75, 0.8), line(0.0, 1.6, 0.0, 0.8), line(-0.8, 1.6, 0.8, 1.6)}));
    defs["Switch:SW_SPDT"] = symbol(
        "Switch:SW_SPDT", "SW_SPDT", "Single-pole double-throw switch", {"switch", "spdt", "changeover"}, "SW",
        json::array({pin("1", "A", kGrid, 1.27, 0.0, "passive"),
                     pin("2", "COM", -kGrid, 0.0, 180.0, "passive"),
                     pin("3", "B", kGrid, -1.27, 0.0, "passive")}),
        json::array({circle(-1.0, 0.0, 0.25), circle(1.0, 1.27, 0.25), circle(1.0, -1.27, 0.25),
                     line(-1.0, 0.0, 0.75, 1.05)}));
    defs["Relay:Relay_SPDT"] = symbol(
        "Relay:Relay_SPDT", "Relay_SPDT", "Single-pole double-throw electromechanical relay", {"relay", "spdt", "coil", "electromechanical"}, "K",
        json::array({pin("A1", "COIL+", -kGrid, 2.54, 180.0, "passive"),
                     pin("A2", "COIL-", -kGrid, 0.0, 180.0, "passive"),
                     pin("11", "COM", -kGrid, -2.54, 180.0, "passive"),
                     pin("12", "NC", kGrid, -1.27, 0.0, "passive"),
                     pin("14", "NO", kGrid, -3.81, 0.0, "passive")}),
        json::array({rect(-1.6, 0.3, 1.6, 2.3), arc(0.0, 0.8, 0.45, 0.0, 180.0),
                     arc(0.0, 1.7, 0.45, 180.0, 360.0), circle(-1.0, -2.54, 0.25),
                     circle(1.0, -1.27, 0.25), circle(1.0, -3.81, 0.25), line(-1.0, -2.54, 0.75, -1.5)}));

    auto audio_two_pin = [&](const std::string& libid, const std::string& name,
                             const std::string& description, const std::string& prefix) {
        defs[libid] = symbol(libid, name, description, {"audio", name, "transducer"}, prefix,
                             json::array({pin("1", "+", -kGrid, 0.0, 180.0, "passive"),
                                          pin("2", "-", kGrid, 0.0, 0.0, "passive")}),
                             json::array({circle(0.0, 0.0, 1.4), polyline({{0.7, -0.8}, {1.7, -1.5}, {1.7, 1.5}, {0.7, 0.8}})}));
    };
    audio_two_pin("Device:Buzzer", "Buzzer", "Piezoelectric buzzer", "BZ");
    audio_two_pin("Device:Speaker", "Speaker", "Loudspeaker", "LS");
    defs["Device:Battery_Cell"] = symbol(
        "Device:Battery_Cell", "Battery_Cell", "Single battery cell", {"battery", "cell", "power", "dc"}, "BT",
        json::array({pin("1", "+", 0.0, kGrid, 90.0, "power_out"),
                     pin("2", "-", 0.0, -kGrid, 270.0, "passive")}),
        json::array({line(-1.4, 0.5, 1.4, 0.5), line(-0.8, -0.5, 0.8, -0.5),
                     line(0.0, kGrid, 0.0, 0.5), line(0.0, -0.5, 0.0, -kGrid), text(-1.0, 1.1, "+", 0.7)}));
    defs["Device:Transformer_1P_1S"] = symbol(
        "Device:Transformer_1P_1S", "Transformer_1P_1S", "Single-primary single-secondary transformer", {"transformer", "isolation", "coil", "magnetic"}, "T",
        json::array({pin("1", "P1", -kGrid, 1.27, 180.0, "passive"),
                     pin("2", "P2", -kGrid, -1.27, 180.0, "passive"),
                     pin("3", "S1", kGrid, 1.27, 0.0, "passive"),
                     pin("4", "S2", kGrid, -1.27, 0.0, "passive")}),
        json::array({arc(-0.8, 0.65, 0.6, 90.0, -90.0), arc(-0.8, -0.65, 0.6, 90.0, -90.0),
                     arc(0.8, 0.65, 0.6, 90.0, 270.0), arc(0.8, -0.65, 0.6, 90.0, 270.0),
                     line(-0.2, -1.8, -0.2, 1.8), line(0.2, -1.8, 0.2, 1.8)}));
    defs["Mechanical:MountingHole"] = symbol(
        "Mechanical:MountingHole", "MountingHole", "Mechanical mounting hole", {"mechanical", "mounting", "hole", "screw"}, "H",
        json::array(), json::array({circle(0.0, 0.0, 1.5), circle(0.0, 0.0, 0.7)}));
    defs["Device:Opto_Isolator"] = symbol(
        "Device:Opto_Isolator", "Opto_Isolator", "Generic phototransistor optocoupler", {"opto", "isolator", "phototransistor", "led"}, "U",
        json::array({pin("1", "A", -kGrid, 1.27, 180.0, "passive"),
                     pin("2", "K", -kGrid, -1.27, 180.0, "passive"),
                     pin("3", "E", kGrid, -1.27, 0.0, "passive"),
                     pin("4", "C", kGrid, 1.27, 0.0, "passive")}),
        json::array({rect(-kGrid, -2.0, kGrid, 2.0), polyline({{-1.2, -0.8}, {-1.2, 0.8}, {-0.1, 0.0}, {-1.2, -0.8}}),
                     line(-0.1, -0.8, -0.1, 0.8), line(0.2, 0.7, 1.2, 1.2), line(0.2, -0.1, 1.2, 0.4)}));
    defs["Isolator:Generic_Optocoupler"] = defs["Device:Opto_Isolator"];
    defs["Isolator:Generic_Optocoupler"]["libid"] = "Isolator:Generic_Optocoupler";
    defs["Isolator:Generic_Optocoupler"]["name"] = "Generic_Optocoupler";
    defs["Isolator:Generic_Optocoupler"]["value"] = "Generic_Optocoupler";

    {
        json pins = json::array();
        json graphics = json::array({rect(-kGrid, -12.0, kGrid, 12.0), text(0.0, 0.0, "MCU", 1.0)});
        for (int number = 1; number <= 20; ++number) {
            const bool left = number <= 10;
            const int row = left ? number - 1 : 20 - number;
            const double y = 11.43 - static_cast<double>(row) * kGrid;
            std::string name = "GPIO" + std::to_string(number - 2);
            std::string type = "bidirectional";
            if (number == 1) { name = "VCC"; type = "power_in"; }
            else if (number == 2) { name = "GND"; type = "power_in"; }
            else if (number == 19) { name = "RESET"; type = "input"; }
            else if (number == 20) { name = "CLK"; type = "input"; }
            pins.push_back(pin(std::to_string(number), name, left ? -kGrid : kGrid, y,
                               left ? 180.0 : 0.0, type));
        }
        defs["MCU_Module:Generic_MCU_20"] = symbol(
            "MCU_Module:Generic_MCU_20", "Generic_MCU_20", "Generic 20-pin microcontroller module",
            {"mcu", "microcontroller", "module", "gpio", "20pin"}, "U", std::move(pins), std::move(graphics));
    }

    add_spice_source(defs, "Simulation_SPICE:V_source", "V_source", "V");
    add_spice_source(defs, "Simulation_SPICE:VDC", "VDC", "V");
    add_spice_source(defs, "Simulation_SPICE:VAC", "VAC", "V");
    add_spice_source(defs, "Simulation_SPICE:VPULSE", "VPULSE", "V");
    add_spice_source(defs, "Simulation_SPICE:ISOURCE", "ISOURCE", "I");

    return defs;
}

const std::map<std::string, json>& builtins() {
    static std::once_flag once;
    static std::map<std::string, json> defs;
    std::call_once(once, [] { defs = make_catalog(); });
    return defs;
}

std::mutex& registry_mutex() {
    static std::mutex mutex;
    return mutex;
}

std::map<std::string, json>& registry() {
    static std::map<std::string, json> symbols;
    return symbols;
}

std::string lowercase(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

std::string string_field(const json& def, const char* key, const std::string& fallback = {}) {
    auto it = def.find(key);
    return it != def.end() && it->is_string() ? it->get<std::string>() : fallback;
}

std::vector<std::string> keywords_of(const json& def) {
    std::vector<std::string> words;
    auto it = def.find("keywords");
    if (it == def.end()) return words;
    if (it->is_array()) {
        for (const json& word : *it)
            if (word.is_string()) words.push_back(word.get<std::string>());
    } else if (it->is_string()) {
        std::istringstream stream(it->get<std::string>());
        std::string word;
        while (stream >> word) words.push_back(word);
    }
    return words;
}

std::map<std::string, json> merged_symbols() {
    std::map<std::string, json> merged = builtins();
    std::lock_guard<std::mutex> lock(registry_mutex());
    for (const auto& entry : registry()) merged[entry.first] = entry.second;
    return merged;
}

json catalog_entry(const std::string& libid, const json& def) {
    const std::string name = string_field(def, "name", libid.substr(libid.find(':') == std::string::npos
                                                                    ? 0 : libid.find(':') + 1));
    std::string prefix = string_field(def, "prefix");
    if (prefix.empty()) prefix = string_field(def, "reference_prefix");
    const json* pins = nullptr;
    auto pin_it = def.find("pins");
    if (pin_it != def.end() && pin_it->is_array()) pins = &*pin_it;
    return json{{"libid", libid},
                {"name", name},
                {"description", string_field(def, "description")},
                {"keywords", keywords_of(def)},
                {"prefix", prefix},
                {"pin_count", pins ? pins->size() : 0},
                {"default_footprint", string_field(def, "default_footprint")},
                {"power", def.contains("power") && def["power"].is_boolean()
                              ? def["power"].get<bool>() : false}};
}

}  // namespace

json resolve_symbol(const std::string& libid) {
    {
        std::lock_guard<std::mutex> lock(registry_mutex());
        auto it = registry().find(libid);
        if (it != registry().end()) return it->second;
    }
    const auto& defs = builtins();
    auto it = defs.find(libid);
    return it == defs.end() ? json() : it->second;
}

std::vector<std::string> symbol_libids() {
    std::set<std::string> ids;
    for (const auto& entry : builtins()) ids.insert(entry.first);
    {
        std::lock_guard<std::mutex> lock(registry_mutex());
        for (const auto& entry : registry()) ids.insert(entry.first);
    }
    return std::vector<std::string>(ids.begin(), ids.end());
}

json symbol_catalog() {
    json out = json::array();
    for (const auto& entry : merged_symbols()) out.push_back(catalog_entry(entry.first, entry.second));
    return out;
}

json search_symbols(const std::string& query, int limit) {
    struct Hit {
        int score;
        std::string libid;
        json value;
    };
    const std::string needle = lowercase(query);
    std::vector<Hit> hits;
    for (const auto& entry : merged_symbols()) {
        const json summary = catalog_entry(entry.first, entry.second);
        const std::string libid = lowercase(entry.first);
        const std::string name = lowercase(string_field(summary, "name"));
        const std::string description = lowercase(string_field(summary, "description"));
        int score = 0;
        if (needle.empty()) {
            score = 1;
        } else if (libid == needle) {
            score = 500;
        } else if (libid.find(needle) != std::string::npos) {
            score = 400;
        } else if (name.find(needle) != std::string::npos) {
            score = 300;
        } else {
            for (const std::string& keyword : keywords_of(summary)) {
                if (lowercase(keyword).find(needle) != std::string::npos) {
                    score = 200;
                    break;
                }
            }
            if (score == 0 && description.find(needle) != std::string::npos) score = 100;
        }
        if (score == 0) continue;
        json value = summary;
        value["score"] = score;
        hits.push_back(Hit{score, entry.first, std::move(value)});
    }
    std::sort(hits.begin(), hits.end(), [](const Hit& a, const Hit& b) {
        if (a.score != b.score) return a.score > b.score;
        return a.libid < b.libid;
    });
    if (limit > 0 && hits.size() > static_cast<std::size_t>(limit))
        hits.resize(static_cast<std::size_t>(limit));
    json symbols = json::array();
    for (Hit& hit : hits) symbols.push_back(std::move(hit.value));
    return json{{"ok", true}, {"symbols", std::move(symbols)}, {"count", hits.size()}};
}

void register_symbol(const std::string& libid, const json& def) {
    json copy = def;
    copy["libid"] = libid;
    std::lock_guard<std::mutex> lock(registry_mutex());
    registry()[libid] = std::move(copy);
}

json load_kicad_sym_file(const std::string& path) {
    json warnings = json::array();
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        warnings.push_back("cannot read KiCad symbol library: " + path);
        return json{{"ok", false}, {"added", json::array()}, {"warnings", std::move(warnings)}};
    }
    std::ostringstream contents;
    contents << file.rdbuf();
    if (file.bad()) {
        warnings.push_back("failed while reading KiCad symbol library: " + path);
        return json{{"ok", false}, {"added", json::array()}, {"warnings", std::move(warnings)}};
    }
    try {
        const json parsed = chudware::ecad::kicad::parse_kicad_sym_library(contents.str());
        auto warning_it = parsed.find("warnings");
        if (warning_it != parsed.end() && warning_it->is_array())
            for (const json& warning : *warning_it) warnings.push_back(warning);
        auto symbols_it = parsed.find("symbols");
        if (symbols_it == parsed.end() || !symbols_it->is_object()) {
            warnings.push_back("KiCad symbol parser returned no symbol map");
            return json{{"ok", false}, {"added", json::array()}, {"warnings", std::move(warnings)}};
        }
        json added = json::array();
        for (auto it = symbols_it->begin(); it != symbols_it->end(); ++it) {
            if (!it.value().is_object()) {
                warnings.push_back("ignored non-object symbol definition: " + it.key());
                continue;
            }
            register_symbol(it.key(), it.value());
            added.push_back(it.key());
        }
        return json{{"ok", true}, {"added", std::move(added)}, {"warnings", std::move(warnings)}};
    } catch (const std::exception& error) {
        warnings.push_back("failed to parse KiCad symbol library " + path + ": " + error.what());
        return json{{"ok", false}, {"added", json::array()}, {"warnings", std::move(warnings)}};
    }
}

}  // namespace chudware::ecad::symlib
