// core/piping/pyutil.h — Python-faithful scalar/JSON helpers shared by the
// piping + hydraulic C++ ports. The parity bar against the Python oracle is
// BYTE-IDENTICAL envelopes, so every place the oracle formats a number with
// round()/str()/repr()/f-string spec or renders a value with repr() goes
// through one of these helpers. Header-only; used by core/piping and
// core/hydraulic (which links chudware_piping).
#pragma once

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <random>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware::pyutil {

// The ports preserve key-insertion order (Python dict semantics) so dict
// repr()s inside error strings match the oracle byte-for-byte.
using ojson = nlohmann::ordered_json;

// Python round(v, ndigits): the correctly-rounded decimal (ties-to-even) of
// the exact binary value, returned as the nearest double. glibc printf rounds
// the exact value to-nearest-even, matching CPython's dtoa-based round.
inline double pyround(double v, int ndigits) {
    if (!std::isfinite(v)) return v;
    char fmt[16];
    std::snprintf(fmt, sizeof(fmt), "%%.%df", ndigits);
    char buf[320];
    std::snprintf(buf, sizeof(buf), fmt, v);
    return std::strtod(buf, nullptr);
}

// Python f"{v:.Nf}".
inline std::string fmt_f(double v, int ndigits) {
    char fmt[16];
    std::snprintf(fmt, sizeof(fmt), "%%.%df", ndigits);
    char buf[320];
    std::snprintf(buf, sizeof(buf), fmt, v);
    return buf;
}

// Python f"{v:.Ng}" == C %.*g (N significant digits, trailing zeros stripped).
inline std::string fmt_g(double v, int ndigits) {
    char fmt[16];
    std::snprintf(fmt, sizeof(fmt), "%%.%dg", ndigits);
    char buf[64];
    std::snprintf(buf, sizeof(buf), fmt, v);
    return buf;
}

// Python str()/repr() of a float: shortest round-trip decimal, integral
// floats keep a trailing ".0" (nlohmann's double serializer does exactly this).
inline std::string py_float(double v) { return ojson(v).dump(); }

// Python str() of a JSON scalar inside an f-string: null -> "None",
// bool -> "True"/"False", string -> itself, number -> str(number).
inline std::string py_str(const ojson& v) {
    if (v.is_null()) return "None";
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_string()) return v.get<std::string>();
    return v.dump();
}

// Python repr() of a string: single quotes by default, double quotes when the
// string contains ' but not ".
inline std::string py_repr_str(const std::string& inner) {
    const bool has_sq = inner.find('\'') != std::string::npos;
    const bool has_dq = inner.find('"') != std::string::npos;
    const char q = (has_sq && !has_dq) ? '"' : '\'';
    std::string out(1, q);
    for (char c : inner) {
        if (c == q || c == '\\') {
            out += '\\';
            out += c;
        } else if (c == '\n') {
            out += "\\n";
        } else if (c == '\t') {
            out += "\\t";
        } else if (c == '\r') {
            out += "\\r";
        } else {
            out += c;
        }
    }
    out += q;
    return out;
}

// Python repr() of a JSON value (dicts render in insertion order — ojson).
inline std::string py_repr(const ojson& v) {
    if (v.is_null()) return "None";
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_number()) return v.dump();
    if (v.is_string()) return py_repr_str(v.get<std::string>());
    if (v.is_array()) {
        std::string out = "[";
        bool first = true;
        for (const auto& e : v) {
            if (!first) out += ", ";
            out += py_repr(e);
            first = false;
        }
        return out + "]";
    }
    if (v.is_object()) {
        std::string out = "{";
        bool first = true;
        for (auto it = v.begin(); it != v.end(); ++it) {
            if (!first) out += ", ";
            out += py_repr_str(it.key());
            out += ": ";
            out += py_repr(it.value());
            first = false;
        }
        return out + "}";
    }
    return "None";
}

// Python truthiness of a JSON value.
inline bool py_truthy(const ojson& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number()) return v.get<double>() != 0.0;
    if (v.is_string()) return !v.get<std::string>().empty();
    return !v.empty();  // arrays + objects
}

// Contract-shaped error: {"ok": false, "reason": ...} (+ "hint" when
// non-empty), mirroring the _err() helpers across the oracle modules.
inline ojson jerr(const std::string& reason, const std::string& hint = "") {
    ojson out = ojson::object();
    out["ok"] = false;
    out["reason"] = reason;
    if (!hint.empty()) out["hint"] = hint;
    return out;
}

// dict.update() semantics: overwrite existing keys in place, append new keys
// at the end (insertion order preserved).
inline void update(ojson& dst, const ojson& src) {
    if (!src.is_object()) return;
    for (auto it = src.begin(); it != src.end(); ++it) dst[it.key()] = it.value();
}

// Python float(v) for a JSON scalar (throws like float() on bad input).
inline double to_double(const ojson& v) {
    if (v.is_number()) return v.get<double>();
    if (v.is_boolean()) return v.get<bool>() ? 1.0 : 0.0;
    if (v.is_string()) return std::stod(v.get<std::string>());
    throw std::runtime_error("float() argument must be a number");
}

// A random RFC-4122 v4 uuid (str(uuid.uuid4()) shape) — document/envelope ids.
inline std::string uuid4() {
    static std::mt19937_64 rng{std::random_device{}()};
    unsigned char b[16];
    for (auto& c : b) c = static_cast<unsigned char>(rng() & 0xFF);
    b[6] = static_cast<unsigned char>((b[6] & 0x0F) | 0x40);  // version 4
    b[8] = static_cast<unsigned char>((b[8] & 0x3F) | 0x80);  // variant 1
    char out[37];
    std::snprintf(out, sizeof(out),
                  "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                  b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11],
                  b[12], b[13], b[14], b[15]);
    return out;
}

// UTC "%Y-%m-%dT%H:%M:%SZ" (simhandoff _now_iso).
inline std::string now_iso() {
    const std::time_t t = std::time(nullptr);
    std::tm tm{};
#if defined(_WIN32)
    gmtime_s(&tm, &t);
#else
    gmtime_r(&t, &tm);
#endif
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ", tm.tm_year + 1900,
                  tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec);
    return buf;
}

}  // namespace chudware::pyutil
