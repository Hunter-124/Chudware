// core/piping/simhandoff.cpp — port of services/mcp/simhandoff.py.
// _clean() drops keys whose value is None; insertion order follows the
// oracle's builder dicts exactly.
#include "piping/simhandoff.h"

#include <set>
#include <stdexcept>

namespace chudware::piping::simhandoff {
namespace {

using pyutil::py_repr;

void put(ojson& obj, const char* key, const ojson& value) {
    if (!value.is_null()) obj[key] = value;
}

void req_str(const ojson& env, const char* key, const std::string& path,
             std::vector<std::string>& errs) {
    const ojson v = env.value(key, ojson(nullptr));
    if (!v.is_string() || v.get<std::string>().empty()) {
        errs.push_back(path + "." + key + ": required non-empty string");
    }
}

void validate_study_ref(const ojson& ref, const std::string& path,
                        std::vector<std::string>& errs) {
    if (!ref.is_object()) {
        errs.push_back(path + ": required study-ref object");
        return;
    }
    req_str(ref, "domain", path, errs);
    req_str(ref, "tool", path, errs);
}

bool is_num(const ojson& v) { return v.is_number() && !v.is_boolean(); }

}  // namespace

ojson study_ref(const std::string& domain, const std::string& tool, const ojson& doc_id,
                const ojson& study, const ojson& handoff_id, const ojson& result_digest) {
    ojson out = ojson::object();
    out["domain"] = domain;
    out["tool"] = tool;
    put(out, "doc_id", doc_id);
    put(out, "study", study);
    put(out, "handoff_id", handoff_id);
    put(out, "result_digest", result_digest);
    return out;
}

ojson geometry_ref(const std::string& role, const std::string& kind, const ojson& path,
                   const ojson& doc_id, const ojson& inline_doc, const ojson& derived_by,
                   const std::string& units, const ojson& meta) {
    ojson out = ojson::object();
    out["role"] = role;
    out["kind"] = kind;
    put(out, "path", path);
    put(out, "doc_id", doc_id);
    put(out, "inline", inline_doc);
    put(out, "derived_by", derived_by);
    out["units"] = units;
    put(out, "meta", meta);
    return out;
}

ojson condition(const std::string& name, const std::string& kind, const ojson& value,
                const std::string& units, const std::string& provenance,
                const ojson& applied_to, const ojson& case_) {
    ojson out = ojson::object();
    out["name"] = name;
    out["kind"] = kind;
    out["value"] = value;
    out["units"] = units;
    put(out, "applied_to", applied_to);
    put(out, "case", case_);
    out["provenance"] = provenance;
    return out;
}

ojson expectation(const std::string& quantity, const ojson& value,
                  const std::string& units, const std::string& provenance) {
    ojson out = ojson::object();
    out["quantity"] = quantity;
    out["value"] = value;
    out["units"] = units;
    out["provenance"] = provenance;
    return out;
}

ojson build_handoff(const std::string& coupling, const ojson& source, const ojson& target,
                    const ojson& geometry, const ojson& conditions,
                    const ojson& expectations, const ojson& warnings, const ojson& meta) {
    ojson out = ojson::object();
    out["schema"] = SCHEMA;
    out["kind"] = "handoff";
    out["id"] = pyutil::uuid4();
    out["created"] = pyutil::now_iso();
    out["coupling"] = coupling;
    out["source"] = source;
    out["target"] = target;
    out["geometry"] = geometry.is_array() ? geometry : ojson::array();
    out["conditions"] = conditions.is_array() ? conditions : ojson::array();
    if (pyutil::py_truthy(expectations)) out["expectations"] = expectations;
    if (pyutil::py_truthy(warnings)) out["warnings"] = warnings;
    put(out, "meta", meta);
    return out;
}

ojson check(const std::string& quantity, const std::string& units, const ojson& source_value,
            const ojson& target_value, const ojson& deviation_pct,
            const ojson& tolerance_pct, bool passed) {
    ojson out = ojson::object();
    out["quantity"] = quantity;
    out["units"] = units;
    out["source_value"] = source_value;
    out["target_value"] = target_value;
    out["deviation_pct"] = deviation_pct;
    out["tolerance_pct"] = tolerance_pct;
    out["pass"] = passed;
    return out;
}

ojson build_verify(const std::string& coupling, const ojson& source, const ojson& target,
                   const ojson& checks, const std::string& verdict,
                   const ojson& handoff_id, const ojson& advisories,
                   const ojson& warnings) {
    const ojson cl = checks.is_array() ? checks : ojson::array();
    bool pass = !cl.empty();
    for (const auto& c : cl) pass = pass && c.value("pass", false);
    ojson out = ojson::object();
    out["schema"] = SCHEMA;
    out["kind"] = "verify";
    out["id"] = pyutil::uuid4();
    out["created"] = pyutil::now_iso();
    out["coupling"] = coupling;
    out["source"] = source;
    out["target"] = target;
    put(out, "handoff_id", handoff_id);
    out["checks"] = cl;
    out["pass"] = pass;
    out["verdict"] = verdict;
    if (pyutil::py_truthy(advisories)) out["advisories"] = advisories;
    if (pyutil::py_truthy(warnings)) out["warnings"] = warnings;
    return out;
}

std::vector<std::string> validate_envelope(const ojson& env) {
    std::vector<std::string> errs;
    if (!env.is_object()) return {"$: envelope must be an object"};
    if (env.value("schema", ojson(nullptr)) != SCHEMA) {
        errs.push_back(std::string("$.schema: must be ") + py_repr(SCHEMA));
    }
    const ojson kind = env.value("kind", ojson(nullptr));
    if (kind != "handoff" && kind != "verify") {
        errs.push_back("$.kind: must be 'handoff' or 'verify'");
        return errs;
    }
    req_str(env, "id", "$", errs);
    req_str(env, "created", "$", errs);
    req_str(env, "coupling", "$", errs);
    validate_study_ref(env.value("source", ojson(nullptr)), "$.source", errs);
    validate_study_ref(env.value("target", ojson(nullptr)), "$.target", errs);

    if (kind == "handoff") {
        const ojson geom = env.value("geometry", ojson(nullptr));
        if (!geom.is_array()) {
            errs.push_back("$.geometry: required list (may be empty for doc-to-doc)");
        } else {
            for (size_t i = 0; i < geom.size(); ++i) {
                const std::string p = "$.geometry[" + std::to_string(i) + "]";
                const ojson& g = geom[i];
                if (!g.is_object()) {
                    errs.push_back(p + ": must be an object");
                    continue;
                }
                req_str(g, "role", p, errs);
                req_str(g, "kind", p, errs);
                if (g.value("path", ojson(nullptr)).is_null() &&
                    g.value("doc_id", ojson(nullptr)).is_null() &&
                    g.value("inline", ojson(nullptr)).is_null()) {
                    errs.push_back(p + ": needs at least one of path/doc_id/inline");
                }
            }
        }
        const ojson conds = env.value("conditions", ojson(nullptr));
        if (!conds.is_array()) {
            errs.push_back("$.conditions: required list");
        } else {
            std::set<std::string> seen;
            for (size_t i = 0; i < conds.size(); ++i) {
                const std::string p = "$.conditions[" + std::to_string(i) + "]";
                const ojson& c = conds[i];
                if (!c.is_object()) {
                    errs.push_back(p + ": must be an object");
                    continue;
                }
                req_str(c, "name", p, errs);
                req_str(c, "kind", p, errs);
                req_str(c, "units", p, errs);
                req_str(c, "provenance", p, errs);
                if (c.value("value", ojson(nullptr)).is_null()) {
                    errs.push_back(p + ".value: required");
                }
                const ojson n = c.value("name", ojson(nullptr));
                const std::string key = n.is_string() ? n.get<std::string>() : py_repr(n);
                if (seen.count(key)) {
                    errs.push_back(p + ".name: duplicate name " + py_repr(n));
                }
                seen.insert(key);
            }
        }
        const ojson exps = env.value("expectations", ojson(nullptr));
        if (!exps.is_null()) {
            if (!exps.is_array()) {
                errs.push_back("$.expectations: must be a list when present");
            } else {
                for (size_t i = 0; i < exps.size(); ++i) {
                    const std::string p = "$.expectations[" + std::to_string(i) + "]";
                    const ojson& e = exps[i];
                    if (!e.is_object()) {
                        errs.push_back(p + ": must be an object");
                        continue;
                    }
                    req_str(e, "quantity", p, errs);
                    req_str(e, "units", p, errs);
                    req_str(e, "provenance", p, errs);
                    if (e.value("value", ojson(nullptr)).is_null()) {
                        errs.push_back(p + ".value: required");
                    }
                }
            }
        }
    } else {  // verify
        ojson checks = env.value("checks", ojson(nullptr));
        if (!checks.is_array() || checks.empty()) {
            errs.push_back("$.checks: required non-empty list");
            checks = ojson::array();
        }
        for (size_t i = 0; i < checks.size(); ++i) {
            const std::string p = "$.checks[" + std::to_string(i) + "]";
            const ojson& c = checks[i];
            if (!c.is_object()) {
                errs.push_back(p + ": must be an object");
                continue;
            }
            req_str(c, "quantity", p, errs);
            req_str(c, "units", p, errs);
            for (const char* k :
                 {"source_value", "target_value", "deviation_pct", "tolerance_pct"}) {
                if (!is_num(c.value(k, ojson(nullptr)))) {
                    errs.push_back(p + "." + k + ": required number");
                }
            }
            if (!c.value("pass", ojson(nullptr)).is_boolean()) {
                errs.push_back(p + ".pass: required boolean");
            }
        }
        const ojson pass = env.value("pass", ojson(nullptr));
        if (!pass.is_boolean()) {
            errs.push_back("$.pass: required boolean");
        } else if (!checks.empty()) {
            bool all_bool = true, all_pass = true;
            for (const auto& c : checks) {
                if (!c.is_object() || !c.value("pass", ojson(nullptr)).is_boolean()) {
                    all_bool = false;
                    all_pass = false;
                    continue;
                }
                if (!c["pass"].get<bool>()) all_pass = false;
            }
            if (all_bool && pass.get<bool>() != all_pass) {
                errs.push_back("$.pass: must equal the AND of all check passes");
            }
        }
        req_str(env, "verdict", "$", errs);
    }
    return errs;
}

ojson attach(ojson result, const ojson& env) {
    const auto errs = validate_envelope(env);
    if (!errs.empty()) {
        std::string msg = std::string(SCHEMA) + " envelope invalid:";
        for (const auto& e : errs) msg += "; " + e;
        throw std::runtime_error(msg);
    }
    result["simhandoff"] = env;
    return result;
}

}  // namespace chudware::piping::simhandoff
