// context_ref.cpp — see context_ref.h. GL-free.
#include "core/context_ref.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <random>

namespace core {
namespace {

using nlohmann::json;

// One random value per process: the `epoch` stamped on every persisted ref.
const std::string& processEpoch() {
    static const std::string epoch = [] {
        std::random_device rd;
        const unsigned long long v =
            ((unsigned long long)rd() << 32) ^ (unsigned long long)rd();
        char buf[24];
        std::snprintf(buf, sizeof(buf), "%016llx", v);
        return std::string(buf);
    }();
    return epoch;
}

std::string str(const json& j, const char* key) {
    auto it = j.find(key);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

uint32_t u32(const json& j, const char* key) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_number_integer()) return 0;
    const long long v = it->get<long long>();
    return (v > 0 && v <= 0xFFFFFFFFLL) ? (uint32_t)v : 0;
}

void putIf(json& o, const char* key, const std::string& v) {
    if (!v.empty()) o[key] = v;
}

std::string num(double v) {
    if (!std::isfinite(v)) return "?";
    double r = std::round(v * 1000.0) / 1000.0;
    if (r == 0.0) r = 0.0;  // no "-0"
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%g", r);
    return buf;
}

bool vec3(const json& sel, const char* key, double out[3]) {
    auto it = sel.find(key);
    if (it == sel.end() || !it->is_array() || it->size() != 3) return false;
    for (int i = 0; i < 3; ++i) {
        if (!(*it)[i].is_number()) return false;
        out[i] = (*it)[i].get<double>();
    }
    return true;
}

std::string vecText(const double v[3]) {
    return "[" + num(v[0]) + ", " + num(v[1]) + ", " + num(v[2]) + "]";
}

bool number(const json& sel, const char* key, double& out) {
    auto it = sel.find(key);
    if (it == sel.end() || !it->is_number()) return false;
    out = it->get<double>();
    return std::isfinite(out);
}

}  // namespace

bool contextRefIsTopology(const ContextRef& ref) {
    return ref.kind == "face" || ref.kind == "edge" || ref.kind == "vertex";
}

bool sameContextEntity(const ContextRef& a, const ContextRef& b) {
    if (a.kind != b.kind || a.docId != b.docId) return false;
    if (a.bodyId != 0 && a.topoId != 0 && a.bodyId == b.bodyId && a.topoId == b.topoId &&
        a.revision == b.revision)
        return true;
    return !a.id.empty() && a.id == b.id;
}

bool toggleContextRef(std::vector<ContextRef>& refs, ContextRef ref) {
    if (ref.id.empty() && !(ref.bodyId != 0 && ref.topoId != 0)) return false;
    for (auto it = refs.begin(); it != refs.end(); ++it) {
        if (sameContextEntity(*it, ref)) {
            refs.erase(it);
            return false;
        }
    }
    refs.push_back(std::move(ref));
    return true;
}

json contextRefToJson(const ContextRef& ref) {
    json o = {{"kind", ref.kind}, {"id", ref.id}, {"label", ref.label},
              {"epoch", processEpoch()}};
    putIf(o, "doc_id", ref.docId);
    putIf(o, "doc_name", ref.docName);
    putIf(o, "doc_path", ref.docPath);
    putIf(o, "type", ref.type);
    putIf(o, "owner_feature", ref.owner);
    putIf(o, "owner_name", ref.ownerName);
    putIf(o, "component", ref.component);
    putIf(o, "component_name", ref.componentName);
    if (ref.selector.is_object()) {
        o["selector"] = ref.selector;
        const std::string sid = str(ref.selector, "sid");
        putIf(o, "sid", sid);
    }
    if (ref.bodyId != 0) o["body_id"] = ref.bodyId;
    if (ref.topoId != 0) o["topo_id"] = ref.topoId;
    if (ref.revision >= 0) o["revision"] = ref.revision;
    return o;
}

bool contextRefFromJson(const json& j, ContextRef& out) {
    out = ContextRef{};
    if (!j.is_object()) return false;
    out.kind = str(j, "kind");
    out.id = str(j, "id");
    out.label = str(j, "label");
    out.docId = str(j, "doc_id");
    if (out.docId.empty()) out.docId = str(j, "docId");  // pre-2026-09-27 drafts
    out.docName = str(j, "doc_name");
    out.docPath = str(j, "doc_path");
    out.type = str(j, "type");
    out.owner = str(j, "owner_feature");
    out.ownerName = str(j, "owner_name");
    out.component = str(j, "component");
    out.componentName = str(j, "component_name");
    auto sel = j.find("selector");
    if (sel != j.end() && sel->is_object()) out.selector = *sel;
    out.bodyId = u32(j, "body_id");
    out.topoId = u32(j, "topo_id");
    auto rev = j.find("revision");
    if (rev != j.end() && rev->is_number_integer()) out.revision = rev->get<int>();
    if (str(j, "epoch") != processEpoch()) {
        // Written by another process: its doc id may now name a different document.
        out.docId.clear();
        out.bodyId = out.topoId = 0;
        out.revision = -1;
    }
    return !(out.id.empty() && out.kind.empty() && out.label.empty());
}

json contextRefsToJson(const std::vector<ContextRef>& refs) {
    json arr = json::array();
    for (const auto& r : refs) arr.push_back(contextRefToJson(r));
    return arr;
}

std::vector<ContextRef> contextRefsFromJson(const json& arr) {
    std::vector<ContextRef> out;
    if (!arr.is_array()) return out;
    for (const auto& j : arr) {
        ContextRef r;
        if (contextRefFromJson(j, r)) out.push_back(std::move(r));
    }
    return out;
}

std::string contextRefGeometry(const ContextRef& ref) {
    const json& s = ref.selector;
    if (!s.is_object()) return {};
    const std::string kind = str(s, "kind");
    std::string surf = str(s, "surf");
    if (surf.empty()) surf = "other";
    double c[3], ax[3], nrm[3], m = 0.0, r = 0.0;
    const bool hasC = vec3(s, "centroid", c), hasAx = vec3(s, "axis", ax),
               hasN = vec3(s, "normal", nrm), hasM = number(s, "area_or_len", m),
               hasR = number(s, "radius", r);
    const std::string side = str(s, "side");
    std::string out;
    auto add = [&](const std::string& bit) { out += (out.empty() ? "" : ", ") + bit; };
    if (kind == "face") {
        add(surf + " face");
        if (surf == "plane" && (hasN || hasAx))
            add("normal " + vecText(hasN ? nrm : ax) + (side.empty() ? "" : " (" + side + ")"));
        else {
            if (hasAx) add("axis " + vecText(ax));
            if (hasN) add("outward " + vecText(nrm) + (side.empty() ? "" : " (" + side + ")"));
        }
        if (hasR && r > 0) add("radius " + num(r) + " mm");
        const std::string conc = str(s, "concavity");
        if (!conc.empty()) add(conc);
        if (hasM && m > 0) add("area " + num(m) + " mm\xC2\xB2");
        if (hasC) add("centroid " + vecText(c));
    } else if (kind == "edge") {
        add(surf + " edge");
        if (hasM && m > 0) add("length " + num(m) + " mm");
        if (surf == "line" && hasAx && hasC && hasM) {
            const double a[3] = {c[0] - ax[0] * m / 2, c[1] - ax[1] * m / 2, c[2] - ax[2] * m / 2};
            const double b[3] = {c[0] + ax[0] * m / 2, c[1] + ax[1] * m / 2, c[2] + ax[2] * m / 2};
            add("from " + vecText(a) + " to " + vecText(b));
        } else {
            const bool full = surf == "circle" && hasR && hasM &&
                              std::fabs(m - 6.283185307179586 * r) <= 1e-3 * std::max(1.0, m);
            if (hasR && r > 0) add("radius " + num(r) + " mm" + (full ? " (full circle)" : ""));
            if (hasAx) add("axis " + vecText(ax));
            if (hasC) add(std::string(full ? "center " : "centroid ") + vecText(c));
        }
    } else if (hasC) {
        add("at " + vecText(c));
    }
    return out;
}

std::string contextRefsPromptText(const std::vector<ContextRef>& refs) {
    if (refs.empty()) return {};
    std::string out = "Context references (picked by the user in the model):";
    for (const auto& r : refs) {
        const std::string sid = r.selector.is_object() ? str(r.selector, "sid") : std::string();
        out += "\n- " + (r.kind.empty() ? std::string("ref") : r.kind) + " " +
               (sid.empty() ? (r.id.empty() ? std::string("(unresolved)") : r.id) : sid);
        if (!r.label.empty()) out += " \"" + r.label + "\"";
        if (!r.type.empty()) out += " (" + r.type + ")";
        if (!r.owner.empty())
            out += "; owner feature " + r.owner + (r.ownerName.empty() ? "" : " \"" + r.ownerName + "\"");
        if (!r.component.empty())
            out += "; component " + r.component +
                   (r.componentName.empty() ? "" : " \"" + r.componentName + "\"");
        if (!r.docId.empty())
            out += "; doc " + r.docId + (r.docName.empty() ? "" : " (" + r.docName + ")");
        const std::string geom = contextRefGeometry(r);
        if (!geom.empty()) out += "; " + geom;
    }
    return out;
}

}  // namespace core
