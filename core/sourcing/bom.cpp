// core/sourcing/bom.cpp — unified made/bought BOM roll-up. Port of engine_oracle/bom.py:
// component classification, board (ECAD) expansion via the fab-BOM grouping, price
// resolution against the provider chain. Exact `to_dicts` line shape (aicad.sourcing/1 §3
// + the additive mpn/description fields). Degradation mirrors the oracle: unpriced lines
// keep unit_cost/ext_cost null — never a fabricated number.
#include "sourcing/sourcing.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <unordered_map>

namespace aicad {
namespace sourcing {

namespace {

// ---- component field readers (tolerant of the aicad.asm/1 component shape) -----------------

std::string as_string(const json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
    if (v.is_number_float()) return std::to_string(v.get<double>());
    if (v.is_boolean()) return v.get<bool>() ? "true" : "false";
    return std::string();
}

std::string component_source_path(const json& comp) {
    if (comp.contains("source") && comp["source"].is_object()) {
        const json p = comp["source"].value("path", json());
        if (!p.is_null()) return as_string(p);
    } else {
        const json p = comp.value("source_path", comp.value("path", json()));
        if (!p.is_null()) return as_string(p);
    }
    return std::string();
}

// Python: first truthy of comp[mpn|part_number|vendor_part_number], then specs[mpn|part_number].
json component_mpn(const json& comp) {
    for (const char* k : {"mpn", "part_number", "vendor_part_number"}) {
        const json v = comp.value(k, json());
        if (!v.is_null() && !(v.is_string() && v.get<std::string>().empty())) return v;
    }
    const json specs = comp.value("specs", json());
    if (specs.is_object()) {
        for (const char* k : {"mpn", "part_number"}) {
            const json v = specs.value(k, json());
            if (!v.is_null() && !(v.is_string() && v.get<std::string>().empty())) return v;
        }
    }
    return json();
}

json component_supplier(const json& comp) {
    for (const char* k : {"supplier", "vendor_supplier"}) {
        const json v = comp.value(k, json());
        if (!v.is_null() && !(v.is_string() && v.get<std::string>().empty())) return v;
    }
    return json();
}

std::string component_description(const json& comp) {
    for (const char* k : {"description", "name"}) {
        const json v = comp.value(k, json());
        if (!v.is_null() && !(v.is_string() && v.get<std::string>().empty()))
            return as_string(v);
    }
    return component_source_path(comp);
}

bool is_board(const std::string& source_path) {
    if (source_path.empty()) return false;
    std::string low = source_path;
    std::transform(low.begin(), low.end(), low.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    const auto slash = low.find_last_of("/\\");
    const auto dot = low.find_last_of('.');
    const std::string ext =
        (dot != std::string::npos && (slash == std::string::npos || dot > slash))
            ? low.substr(dot)
            : std::string();
    return ext == ".ecad" || ext == ".ecad+" || ext == ".kicad_pcb" ||
           [&] {
               const std::string suffix = ".ecad+";
               return low.size() >= suffix.size() &&
                      low.compare(low.size() - suffix.size(), suffix.size(), suffix) == 0;
           }();
}

// Python int(value): truncating for floats, strict optional-sign digits for strings.
int as_qty(const json& v) {
    long long q = 1;
    if (v.is_number_integer()) q = v.get<long long>();
    else if (v.is_number_unsigned()) q = static_cast<long long>(v.get<unsigned long long>());
    else if (v.is_number_float()) q = static_cast<long long>(std::trunc(v.get<double>()));
    else if (v.is_boolean()) q = v.get<bool>() ? 1 : 0;
    else if (v.is_string()) {
        std::string s = v.get<std::string>();
        const auto b = s.find_first_not_of(" \t\r\n");
        const auto e = s.find_last_not_of(" \t\r\n");
        s = (b == std::string::npos) ? std::string() : s.substr(b, e - b + 1);
        size_t i = 0;
        if (i < s.size() && (s[i] == '+' || s[i] == '-')) ++i;
        bool digits = i < s.size();
        for (size_t j = i; j < s.size(); ++j)
            if (s[j] < '0' || s[j] > '9') digits = false;
        q = 1;
        if (digits) {
            try { q = std::stoll(s); } catch (...) { q = 1; }
        }
    }
    return q >= 1 ? static_cast<int>(q) : 1;
}

// ---- BomLine shape (dataclasses.asdict of engine.bom.BomLine) --------------------------------

json make_line(const std::string& line_id, const std::string& kind, int qty,
               const json& part_ref, const json& vendor_part, const json& mpn,
               const std::string& description) {
    json line;
    line["line_id"] = line_id;
    line["kind"] = kind;
    line["quantity"] = qty;
    line["unit"] = "ea";
    line["part_ref"] = part_ref;
    line["vendor_part"] = vendor_part;
    line["cost_model"] = nullptr;
    line["unit_cost"] = nullptr;
    line["ext_cost"] = nullptr;
    line["currency"] = "USD";
    line["mpn"] = mpn;
    line["description"] = description;
    return line;
}

json vendor_key(const json& supplier, const json& part_number) {
    if (supplier.is_null() || part_number.is_null()) return json();
    const std::string s = as_string(supplier), p = as_string(part_number);
    if (s.empty() || p.empty()) return json();
    return json(vendor_slug(s) + "::" + vendor_slug(p));
}

// ---- natural ref ordering (engine/ecad/fab/bom.py::_natural_key) ------------------------------
// re.split(r"(\d+)", ref) yields alternating text/num segments, ALWAYS starting and
// ending with a text segment (possibly empty): "R1" -> ["r",1,""], "1R" -> ["",1,"r"].
struct NatSeg {
    bool num;
    std::string text;   // lowercased when !num
    long long n = 0;
};

std::vector<NatSeg> natural_key(const std::string& ref) {
    std::vector<NatSeg> segs;
    bool in_num = false;
    std::string cur;
    auto flush = [&]() {
        if (in_num) {
            segs.push_back(NatSeg{true, std::string(), cur.empty() ? 0 : std::stoll(cur)});
        } else {
            std::string low = cur;
            std::transform(low.begin(), low.end(), low.begin(),
                           [](unsigned char c) { return (char)std::tolower(c); });
            segs.push_back(NatSeg{false, low, 0});
        }
        cur.clear();
    };
    for (char c : ref) {
        const bool d = c >= '0' && c <= '9';
        if (d != in_num) {
            flush();
            in_num = d;
        }
        cur += c;
    }
    flush();
    return segs;
}

int nat_compare(const std::string& a, const std::string& b) {
    const std::vector<NatSeg> ka = natural_key(a), kb = natural_key(b);
    // Segment types always align (both alternate text/num from a text start), so a
    // position-wise tuple compare mirrors Python exactly; a strict prefix sorts first.
    const size_t n = std::min(ka.size(), kb.size());
    for (size_t i = 0; i < n; ++i) {
        if (ka[i].num) {
            if (ka[i].n != kb[i].n) return ka[i].n < kb[i].n ? -1 : 1;
        } else if (ka[i].text != kb[i].text) {
            return ka[i].text < kb[i].text ? -1 : 1;
        }
    }
    if (ka.size() != kb.size()) return ka.size() < kb.size() ? -1 : 1;
    return 0;
}

// ---- ECAD board BOM rows (engine/ecad/fab/bom.py::_group_footprints + bom.py MPN enrich) -----

bool json_truthy(const json& v) {
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_string()) return !v.get<std::string>().empty();
    if (v.is_number_integer()) return v.get<long long>() != 0;
    if (v.is_number_float()) return v.get<double>() != 0.0;
    if (v.is_array() || v.is_object()) return !v.empty();
    return false;
}

std::string trim(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\r\n");
    const auto e = s.find_last_not_of(" \t\r\n");
    return b == std::string::npos ? std::string() : s.substr(b, e - b + 1);
}

// Grouped rows for one board: [{"Refs","Qty","Value","Footprint","MPN", <user fields>}, ...].
json ecad_bom_rows(const json& board) {
    static const std::set<std::string> kStdLower = {
        "ref", "value", "footprint", "mpn", "dnp", "attr", "model_3d", "id", "fpid",
        "linked_symbol", "pads", "graphics", "pos", "angle", "side"};

    struct Group {
        std::string value, fpid, mpn;
        std::map<std::string, std::string> fields;
        std::vector<std::string> refs;
    };
    std::vector<Group> groups;  // insertion order (Python dict)

    // ref -> MPN index for the post-grouping enrichment (bom.py::_footprint_mpn_index).
    std::map<std::string, std::string> ref_to_mpn;

    for (const json& fp : board.value("footprints", json::array())) {
        if (!fp.is_object()) continue;
        const std::string ref = as_string(fp.value("ref", json("?")));
        const std::string value = as_string(fp.value("value", json("")));
        const std::string fpid = as_string(fp.value("fpid", json("")));
        const std::string attr = as_string(fp.value("attr", json("smd")));

        std::map<std::string, std::string> fields;
        std::string raw_mpn;
        const json raw_fields = fp.value("fields", json());
        if (raw_fields.is_object()) {
            for (auto it = raw_fields.begin(); it != raw_fields.end(); ++it) {
                std::string klow = it.key();
                std::transform(klow.begin(), klow.end(), klow.begin(),
                               [](unsigned char c) { return (char)std::tolower(c); });
                if (klow == "mpn" && raw_mpn.empty()) raw_mpn = as_string(it.value());
                if (kStdLower.count(klow)) continue;
                fields[it.key()] = as_string(it.value());
            }
        }
        // bom.py::_footprint_mpn_index: fields.MPN/mpn first, then fp.mpn.
        {
            std::string idx_mpn;
            if (raw_fields.is_object()) {
                const json a = raw_fields.value("MPN", json());
                const json b = raw_fields.value("mpn", json());
                idx_mpn = !a.is_null() ? as_string(a) : (!b.is_null() ? as_string(b) : "");
            }
            if (idx_mpn.empty()) {
                const json m = fp.value("mpn", json());
                if (!m.is_null()) idx_mpn = as_string(m);
            }
            if (!ref.empty() && !idx_mpn.empty()) ref_to_mpn[ref] = idx_mpn;
        }

        const bool dnp = json_truthy(fp.value("dnp", json(false))) || attr == "virtual";
        if (dnp) continue;  // exclude_dnp=True (the write_bom default the oracle uses)

        // MPN read from the footprint top-level first, then the raw fields (case-insens.).
        std::string mpn;
        {
            const json m = fp.value("mpn", json());
            if (!m.is_null()) mpn = as_string(m);
            if (mpn.empty()) mpn = raw_mpn;
        }

        Group* hit = nullptr;
        for (Group& g : groups)
            if (g.value == value && g.fpid == fpid && g.mpn == mpn && g.fields == fields) {
                hit = &g;
                break;
            }
        if (!hit) {
            groups.push_back(Group{value, fpid, mpn, fields, {}});
            hit = &groups.back();
        }
        hit->refs.push_back(ref);
    }

    std::vector<json> rows;
    for (Group& g : groups) {
        std::stable_sort(g.refs.begin(), g.refs.end(),
                         [](const std::string& a, const std::string& b) {
                             return nat_compare(a, b) < 0;
                         });
        std::string refs;
        for (size_t i = 0; i < g.refs.size(); ++i) {
            if (i) refs += ",";
            refs += g.refs[i];
        }
        json row;
        row["Refs"] = refs;
        row["Qty"] = static_cast<long long>(g.refs.size());
        row["Value"] = g.value;
        row["Footprint"] = g.fpid;
        row["MPN"] = g.mpn;
        for (const auto& kv : g.fields) row[kv.first] = kv.second;
        rows.push_back(std::move(row));
    }
    std::stable_sort(rows.begin(), rows.end(), [](const json& a, const json& b) {
        const std::string ra = a["Refs"].get<std::string>();
        const std::string rb = b["Refs"].get<std::string>();
        return nat_compare(ra.substr(0, ra.find(',')), rb.substr(0, rb.find(','))) < 0;
    });

    // bom.py::_ecad_bom_rows MPN enrichment: an empty row MPN is recovered from the
    // board footprints keyed by the row's first ref.
    for (json& row : rows) {
        if (!trim(row["MPN"].get<std::string>()).empty()) continue;
        const std::string refs = row["Refs"].get<std::string>();
        const std::string first_ref = trim(refs.substr(0, refs.find(',')));
        const auto it = ref_to_mpn.find(first_ref);
        if (it != ref_to_mpn.end() && !it->second.empty()) row["MPN"] = it->second;
    }
    return rows;
}

}  // namespace

std::string vendor_slug(const std::string& v) {
    // Exact mirror of engine/vendor_parts.py::slug (see also doc/session.cpp::vendor_slug).
    const size_t b = v.find_first_not_of(" \t\r\n");
    const size_t e = v.find_last_not_of(" \t\r\n");
    const std::string s = (b == std::string::npos) ? std::string() : v.substr(b, e - b + 1);
    std::string t;
    bool prev_dash = false;
    for (char c : s) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                        (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-';
        if (ok) { t += c; prev_dash = false; }
        else if (!prev_dash) { t += '-'; prev_dash = true; }
    }
    const size_t lb = t.find_first_not_of('-');
    const size_t le = t.find_last_not_of('-');
    t = (lb == std::string::npos) ? std::string() : t.substr(lb, le - lb + 1);
    for (char& c : t) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return t.empty() ? std::string("part") : t;
}

json bom_rollup_lines(const json& asm_tree, const Seams& seams) {
    json node = asm_tree;
    if (asm_tree.is_object() && asm_tree.contains("tree") && asm_tree["tree"].is_object())
        node = asm_tree["tree"];

    std::vector<json> lines;
    std::unordered_map<std::string, size_t> groups;  // gkey -> index into lines
    int next_id = 0;
    std::vector<std::string> board_paths;

    const json comps = node.is_object() ? node.value("components", json()) : json();
    if (comps.is_array()) {
        for (const json& comp : comps) {
            if (!comp.is_object()) continue;
            const std::string source_path = component_source_path(comp);
            if (is_board(source_path)) {
                board_paths.push_back(source_path);
                continue;
            }
            const int qty = as_qty(comp.value("quantity", json()));
            const json mpn = component_mpn(comp);
            const json supplier = component_supplier(comp);
            const json rec = seams.vendor_lookup ? seams.vendor_lookup(comp) : json();
            // Python truthiness: an empty dict from vendor_lookup is falsy.
            const bool rec_truthy = rec.is_object() && !rec.empty();
            const bool is_bought = !mpn.is_null() || !supplier.is_null() || rec_truthy;

            if (is_bought) {
                json part_number = mpn;
                if (part_number.is_null() && rec.is_object())
                    part_number = rec.value("part_number", json());
                json sup = supplier;
                if (sup.is_null() && rec.is_object()) sup = rec.value("supplier", json());
                const json part_ref = vendor_key(sup, part_number);
                const std::string gkey =
                    "bought:" + (!part_ref.is_null() ? part_ref.get<std::string>()
                                : !mpn.is_null()    ? as_string(mpn)
                                : !source_path.empty() ? source_path
                                                     : component_description(comp));
                const auto it = groups.find(gkey);
                if (it != groups.end()) {
                    lines[it->second]["quantity"] =
                        lines[it->second]["quantity"].get<int>() + qty;
                } else {
                    json line_mpn = mpn;
                    if (line_mpn.is_null() && !part_number.is_null())
                        line_mpn = json(as_string(part_number));
                    groups[gkey] = lines.size();
                    lines.push_back(make_line("L" + std::to_string(++next_id), "bought", qty,
                                              part_ref, rec, line_mpn,
                                              component_description(comp)));
                }
            } else {
                const std::string gkey =
                    "made:" + (!source_path.empty() ? source_path
                                                    : component_description(comp));
                const auto it = groups.find(gkey);
                if (it != groups.end()) {
                    lines[it->second]["quantity"] =
                        lines[it->second]["quantity"].get<int>() + qty;
                } else {
                    groups[gkey] = lines.size();
                    lines.push_back(make_line("L" + std::to_string(++next_id), "made", qty,
                                              json(), json(), json(),
                                              component_description(comp)));
                }
            }
        }
    }

    // Merge each linked board's electronic BOM (one bought line per grouped footprint).
    for (const std::string& bp : board_paths) {
        if (!seams.load_board) continue;
        const json board = seams.load_board(bp);
        if (!board.is_object()) continue;
        for (const json& row : ecad_bom_rows(board)) {
            const std::string mpn_s = trim(as_string(row.value("MPN", json(""))));
            const std::string value = trim(as_string(row.value("Value", json(""))));
            const std::string footprint = trim(as_string(row.value("Footprint", json(""))));
            const std::string refs = trim(as_string(row.value("Refs", json(""))));
            std::string desc;
            if (!value.empty()) desc = value;
            if (!footprint.empty()) desc += (desc.empty() ? "" : " ") + footprint;
            if (desc.empty()) desc = !refs.empty() ? refs : "component";
            lines.push_back(make_line("L" + std::to_string(++next_id), "bought",
                                      as_qty(row.value("Qty", json())), json(), json(),
                                      mpn_s.empty() ? json() : json(mpn_s), desc));
        }
    }
    return lines;
}

json unit_price_for_qty(const json& rec, int quantity) {
    std::vector<json> breaks;
    const json pb = rec.value("price_breaks", json());
    if (pb.is_array())
        for (const json& b : pb)
            if (b.is_object()) breaks.push_back(b);
    std::stable_sort(breaks.begin(), breaks.end(), [](const json& a, const json& b) {
        const json qa = a.value("qty", json(0));
        const json qb = b.value("qty", json(0));
        const long long ia = qa.is_number() ? qa.get<long long>() : 0;
        const long long ib = qb.is_number() ? qb.get<long long>() : 0;
        return ia < ib;
    });
    json best;
    for (const json& b : breaks) {
        const json q = b.value("qty", json(0));
        const long long iq = q.is_number() ? q.get<long long>() : 0;
        const json up = b.value("unit_price", json());
        if (iq <= quantity && !up.is_null()) best = up;
    }
    if (!best.is_null()) return json(best.get<double>());
    const json up = rec.value("unit_price", json());
    return up.is_null() ? json() : json(up.get<double>());
}

void resolve_prices(json& bom, const Seams& seams) {
    if (!bom.is_array()) return;
    for (json& line : bom) {
        if (line.value("kind", std::string()) != "bought") continue;
        const json mpn = line.value("mpn", json());
        if (mpn.is_null() || (mpn.is_string() && mpn.get<std::string>().empty())) continue;
        const json rec = seams.price_fetch ? seams.price_fetch(as_string(mpn))
                                           : fetch_price(as_string(mpn), seams);
        if (!rec.is_object() || rec.empty()) continue;  // price unknown — never guessed
        line["vendor_part"] = rec;
        const json key = vendor_key(rec.value("supplier", json()),
                                    rec.value("part_number", json()));
        if (!key.is_null()) line["part_ref"] = key;
        const json unit = unit_price_for_qty(rec, line.value("quantity", 1));
        if (!unit.is_null()) {
            const double u = unit.get<double>();
            line["unit_cost"] = u;
            line["ext_cost"] = u * line.value("quantity", 1);
            const json cur = rec.value("currency", json());
            if (!cur.is_null() && !(cur.is_string() && cur.get<std::string>().empty()))
                line["currency"] = as_string(cur);
        }
    }
}

json bom_price_lines(const json& asm_tree, const Seams& seams) {
    json bom = bom_rollup_lines(asm_tree, seams);
    resolve_prices(bom, seams);
    return bom;
}

}  // namespace sourcing
}  // namespace aicad
