// See op_registry.h — ECAD closed vocabulary + suggestion scoring.
#include "ecad/op_registry.h"

#include <algorithm>
#include <cctype>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace aicad::ecad {
namespace {

// SINGLE SOURCE for ECAD op kinds. Keep ops.cpp::apply_one in lockstep.
const char* const kEcadOpKinds[] = {
    "sch_add_symbol",
    "sch_edit_symbol",
    "sch_delete_symbol",
    "sch_add_wire",
    "sch_edit_wire",
    "sch_delete_wire",
    "sch_add_junction",
    "sch_delete_junction",
    "sch_add_label",
    "sch_edit_label",
    "sch_delete_label",
    "sch_add_power",
    "sch_add_no_connect",
    "sch_connect",
    "sch_annotate",
    "sch_set_grid",
    "sch_add_sheet",
    "sch_add_sheet_symbol",
    "sch_add_sheet_pin",
    "sch_import_kicad",
    "pcb_set_stackup",
    "pcb_set_outline",
    "pcb_add_footprint",
    "pcb_edit_footprint",
    "pcb_delete_footprint",
    "pcb_add_track",
    "pcb_add_tracks",
    "pcb_delete_track",
    "pcb_add_via",
    "pcb_delete_via",
    "pcb_add_zone",
    "pcb_edit_zone",
    "pcb_delete_zone",
    "pcb_set_rules",
    "pcb_assign_net",
    "pcb_update_from_sch",
    "pcb_autoplace",
    "rename_doc",
    "set_field",
    "nop",
};

std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

std::string family(const std::string& s) {
    const auto pos = s.find('_');
    if (pos == std::string::npos || pos == 0) return s;
    return s.substr(0, pos);
}

int levenshtein(const std::string& a, const std::string& b) {
    const std::size_t n = a.size(), m = b.size();
    if (n == 0) return static_cast<int>(m);
    if (m == 0) return static_cast<int>(n);
    std::vector<int> prev(m + 1), cur(m + 1);
    for (std::size_t j = 0; j <= m; ++j) prev[j] = static_cast<int>(j);
    for (std::size_t i = 1; i <= n; ++i) {
        cur[0] = static_cast<int>(i);
        for (std::size_t j = 1; j <= m; ++j) {
            const int cost = (a[i - 1] == b[j - 1]) ? 0 : 1;
            cur[j] = std::min({prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost});
        }
        prev.swap(cur);
    }
    return prev[m];
}

int score_match(const std::string& q, const std::string& cand) {
    if (q.empty()) return 0;
    if (cand == q) return 100000;
    if (cand.size() >= q.size() && cand.compare(0, q.size(), q) == 0)
        return 50000 + static_cast<int>(q.size()) * 10;
    if (q.size() >= cand.size() && q.compare(0, cand.size(), cand) == 0)
        return 45000 + static_cast<int>(cand.size()) * 10;
    const std::string qf = family(q);
    const std::string cf = family(cand);
    if (qf.size() >= 2 && qf == cf) {
        const std::string qrest = q.size() > qf.size() + 1 ? q.substr(qf.size() + 1) : "";
        const std::string crest = cand.size() > cf.size() + 1 ? cand.substr(cf.size() + 1) : "";
        int bonus = 0;
        if (!qrest.empty() && crest.compare(0, qrest.size(), qrest) == 0)
            bonus = 2000 + static_cast<int>(qrest.size()) * 10;
        else if (!qrest.empty())
            bonus = 1000 - levenshtein(qrest, crest) * 20;
        return 30000 + bonus;
    }
    if (cand.find(q) != std::string::npos) return 20000 + static_cast<int>(q.size());
    if (q.find(cand) != std::string::npos) return 15000 + static_cast<int>(cand.size());
    return 10000 - levenshtein(q, cand) * 100;
}

}  // namespace

std::vector<std::string> ecad_op_kinds() {
    std::vector<std::string> out;
    out.reserve(sizeof(kEcadOpKinds) / sizeof(kEcadOpKinds[0]));
    for (const char* k : kEcadOpKinds) out.emplace_back(k);
    return out;
}

std::vector<std::string> suggest_ecad_ops(const std::string& unknown, std::size_t n) {
    if (n == 0) return {};
    const std::string q = lower(unknown);
    const auto kinds = ecad_op_kinds();
    std::vector<std::pair<int, std::string>> scored;
    scored.reserve(kinds.size());
    for (const auto& k : kinds) scored.emplace_back(score_match(q, lower(k)), k);
    std::stable_sort(scored.begin(), scored.end(),
                     [](const auto& a, const auto& b) {
                         if (a.first != b.first) return a.first > b.first;
                         return a.second < b.second;
                     });
    std::vector<std::string> out;
    for (const auto& p : scored) {
        out.push_back(p.second);
        if (out.size() >= n) break;
    }
    return out;
}

std::string unknown_ecad_op_message(int index, const std::string& k) {
    std::ostringstream oss;
    oss << "op[" << index << "]: unknown op '" << k << "'";
    const auto closest = suggest_ecad_ops(k, 5);
    if (!closest.empty()) {
        oss << ". closest: ";
        for (std::size_t i = 0; i < closest.size(); ++i) {
            if (i) oss << ", ";
            oss << closest[i];
        }
    }
    oss << "; see cad_guide()";
    return oss.str();
}

}  // namespace aicad::ecad
