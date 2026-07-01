#include "worker_types.h"

#include <cstdio>

std::vector<PartInfo> parseParts(const nlohmann::json& arr) {
    std::vector<PartInfo> out;
    if (!arr.is_array()) return out;
    for (auto& p : arr) {
        if (!p.is_object()) continue;
        PartInfo pi;
        pi.name = jstr(p, "name");
        pi.stlPath = jstr(p, "stl_path");
        pi.volume = jnum(p, "volume");
        auto bb = p.value("bbox", nlohmann::json::object());
        if (bb.is_object() && bb.contains("size") && bb["size"].is_array() && bb["size"].size() == 3) {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "%.1f x %.1f x %.1f mm", bb["size"][0].get<double>(),
                          bb["size"][1].get<double>(), bb["size"][2].get<double>());
            pi.dims = buf;
        }
        out.push_back(std::move(pi));
    }
    return out;
}

FitReport parseFit(const nlohmann::json& f) {
    FitReport r;
    if (!f.is_object()) return r;
    r.present = true;
    r.ok = f.value("ok", true);
    r.summary = jstr(f, "summary");
    auto pairs = [](const nlohmann::json& arr, const char* valKey) {
        std::vector<FitPair> v;
        if (arr.is_array())
            for (auto& e : arr) {
                if (!e.is_object()) continue;
                FitPair fp;
                fp.a = jstr(e, "a");
                fp.b = jstr(e, "b");
                fp.value = jnum(e, valKey);
                v.push_back(std::move(fp));
            }
        return v;
    };
    if (f.contains("interferences")) r.interferences = pairs(f["interferences"], "volume");
    if (f.contains("clearances")) r.clearances = pairs(f["clearances"], "distance");
    return r;
}

PlanInfo parsePlan(const nlohmann::json& p) {
    PlanInfo pi;
    if (!p.is_object()) return pi;
    pi.present = true;
    pi.name = jstr(p, "name");
    pi.summary = jstr(p, "summary");
    pi.fit = jstr(p, "fit");
    if (auto it = p.find("parts"); it != p.end() && it->is_array())
        for (auto& q : *it) {
            if (!q.is_object()) continue;
            PlanPart pp;
            pp.name = jstr(q, "name");
            pp.purpose = jstr(q, "purpose");
            pp.dims = jstr(q, "dimensions");
            if (auto f = q.find("features"); f != q.end() && f->is_array())
                for (auto& s : *f)
                    if (s.is_string()) pp.features.push_back(s.get<std::string>());
            pi.parts.push_back(std::move(pp));
        }
    return pi;
}
