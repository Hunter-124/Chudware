#include "firmware/pyfmt.h"

#include <cstdio>

namespace chudware::firmware {

std::string fmt_g(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%g", v);
    return buf;
}

std::string fmt_3f(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3f", v);
    return buf;
}

std::string fmt_hex(long long v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "0x%llx", v);
    return buf;
}

std::string py_repr(const nlohmann::json& v) {
    if (v.is_null()) return "None";
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_number_integer() || v.is_number_unsigned()) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%lld", v.get<long long>());
        return buf;
    }
    if (v.is_number()) {
        double d = v.get<double>();
        // Python float repr: integral floats keep a trailing ".0".
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.17g", d);
        std::string s = buf;
        // Shortest-roundtrip is close enough for the firmware messages; ensure
        // a decimal point or exponent so 5.0 never renders as "5".
        if (s.find('.') == std::string::npos && s.find('e') == std::string::npos &&
            s.find("inf") == std::string::npos && s.find("nan") == std::string::npos) {
            // %.17g may give "5.0000000000000001" for 5.0 — prefer %g then.
            std::snprintf(buf, sizeof(buf), "%g", d);
            s = buf;
            if (s.find('.') == std::string::npos && s.find('e') == std::string::npos)
                s += ".0";
        }
        return s;
    }
    if (v.is_string()) {
        std::string inner = v.get<std::string>();
        std::string out = "'";
        for (char c : inner) {
            if (c == '\'') out += "\\'";
            else if (c == '\\') out += "\\\\";
            else out += c;
        }
        out += "'";
        return out;
    }
    if (v.is_array()) {
        std::string out = "[";
        bool first = true;
        for (const auto& e : v) {
            if (!first) out += ", ";
            out += py_repr(e);
            first = false;
        }
        out += "]";
        return out;
    }
    if (v.is_object()) {
        std::string out = "{";
        bool first = true;
        for (auto it = v.begin(); it != v.end(); ++it) {
            if (!first) out += ", ";
            out += py_repr(it.key());
            out += ": ";
            out += py_repr(it.value());
            first = false;
        }
        out += "}";
        return out;
    }
    return "None";
}

}  // namespace chudware::firmware
