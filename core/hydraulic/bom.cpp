// core/hydraulic/bom.cpp — port of engine_oracle/hydraulic/bom.py.
#include "hydraulic/bom.h"

namespace chudware::hydraulic {

ojson hydraulic_bom_lines(const ojson& doc) {
    ojson lines = ojson::array();
    for (const auto& comp : doc.value("components", ojson::array())) {
        if (!comp.is_object()) continue;
        if (comp.value("suppressed", false)) continue;
        const ojson sourcing =
            comp.value("sourcing", ojson(nullptr)).is_object()
                ? comp["sourcing"]
                : ojson::object();
        ojson line = ojson::object();
        line["id"] = comp.value("id", ojson(nullptr));
        line["type"] = comp.value("type", ojson(nullptr));
        const ojson name = comp.value("name", ojson(nullptr));
        line["name"] = pyutil::py_truthy(name) ? name : comp.value("type", ojson(nullptr));
        line["mpn"] = sourcing.value("mpn", ojson(nullptr));
        line["manufacturer"] = sourcing.value("manufacturer", ojson(nullptr));
        line["description"] = sourcing.value("description", ojson(nullptr));
        line["quantity"] = sourcing.value("quantity", ojson(1));
        line["unit"] = sourcing.value("unit", ojson("ea"));
        line["category"] = sourcing.value("category", ojson(nullptr));
        lines.push_back(std::move(line));
    }
    return lines;
}

ojson sourcing_summary(const ojson& doc) {
    const ojson lines = hydraulic_bom_lines(doc);
    ojson by_type = ojson::object();
    ojson missing_mpn = ojson::array();
    int sourced = 0;

    for (const auto& line : lines) {
        const ojson ctype = line.value("type", ojson(nullptr));
        const std::string key =
            pyutil::py_truthy(ctype) ? pyutil::py_str(ctype) : "unknown";
        by_type[key] = by_type.value(key, 0) + 1;
        if (!pyutil::py_truthy(line.value("mpn", ojson(nullptr)))) {
            missing_mpn.push_back(line["id"]);
        } else {
            ++sourced;
        }
    }

    ojson out = ojson::object();
    out["total"] = static_cast<int>(lines.size());
    out["by_type"] = std::move(by_type);
    out["missing_mpn"] = std::move(missing_mpn);
    out["sourced"] = sourced;
    return out;
}

}  // namespace chudware::hydraulic
