// See op_registry.h — closed vocabulary + suggestion scoring for unknown ops.
#include "doc/op_registry.h"
#include "doc/validate.h"

#include <algorithm>
#include <cctype>
#include <limits>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace chudware::doc {
namespace {

// SINGLE SOURCE for document op kinds. Keep session.cpp::apply_one handlers in
// lockstep — test_op_registry enforces both directions of coverage.
const char* const kDocOpKinds[] = {
    "add_feature",
    "edit_feature",
    "delete_feature",
    "reorder_feature",
    "set_suppressed",
    "set_folder",
    "rename",
    "set_units",
    "set_material",
    "set_appearance",
    "set_rollback",
    "eq_set",
    "eq_delete",
    "sketch_add_entity",
    "sketch_add_constraint",
    "sketch_edit_entity",
    "sketch_delete_entity",
    "sketch_delete_constraint",
    "sketch_set_dim",
    "sketch_toggle_construction",
    "sketch_convert",
    "sketch_fully_define",
    "sketch_break_external_ref",
    "sketch_trim",
    "sketch_extend",
    "sketch_offset",
    "sketch_fillet",
    "sketch_chamfer",
    "sketch_mirror_entities",
    "sketch_pattern_entities",
    "sketch_move",
    "sketch_rotate",
    "asm_insert_component",
    "asm_edit_component",
    "asm_delete_component",
    "asm_add_mate",
    "asm_edit_mate",
    "asm_delete_mate",
    "asm_set_explode",
    "copy_feature",
    "paste_feature",
    "paste_feature_as_derived",
    "sketch_paste_entities",
};

std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

// Token family: prefix before first '_', or whole string if none.
// Hierarchical vocab: sketch_*, asm_*, eq_*, set_*, paste_*, etc.
std::string family(const std::string& s) {
    const auto pos = s.find('_');
    if (pos == std::string::npos || pos == 0) return s;
    return s.substr(0, pos);
}

// Classic Levenshtein; small strings only (op names).
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

// The words a model reaches for when it means "remove material". None of them is an op;
// every one of them is add_feature + params.mode:"cut". Keep in lockstep with
// services/mcp/op_registry.py::_REMOVAL_WORDS.
bool is_removal_word(const std::string& q) {
    static const char* kWords[] = {
        "cut", "pocket", "subtract", "remove", "difference", "hollow_out", "mill",
        "carve", "excavate", "recess", "cutout", "cut_out", "notch",
    };
    for (const char* w : kWords)
        if (q == w) return true;
    return false;
}

// Higher is better. Prefix / token-family beat pure edit distance.
int score_match(const std::string& q, const std::string& cand) {
    if (q.empty()) return 0;
    if (cand == q) return 100000;
    // Prefix of candidate or query.
    if (cand.size() >= q.size() && cand.compare(0, q.size(), q) == 0)
        return 50000 + static_cast<int>(q.size()) * 10;
    if (q.size() >= cand.size() && q.compare(0, cand.size(), cand) == 0)
        return 45000 + static_cast<int>(cand.size()) * 10;
    // Token family (sketch_*, asm_*, …).
    const std::string qf = family(q);
    const std::string cf = family(cand);
    if (qf.size() >= 2 && qf == cf) {
        // Shared family + remaining-token prefix/edit bonus.
        const std::string qrest = q.size() > qf.size() + 1 ? q.substr(qf.size() + 1) : "";
        const std::string crest = cand.size() > cf.size() + 1 ? cand.substr(cf.size() + 1) : "";
        int bonus = 0;
        if (!qrest.empty() && crest.compare(0, qrest.size(), qrest) == 0)
            bonus = 2000 + static_cast<int>(qrest.size()) * 10;
        else if (!qrest.empty())
            bonus = 1000 - levenshtein(qrest, crest) * 20;
        return 30000 + bonus;
    }
    // Substring containment.
    if (cand.find(q) != std::string::npos) return 20000 + static_cast<int>(q.size());
    if (q.find(cand) != std::string::npos) return 15000 + static_cast<int>(cand.size());
    // Final resort: a plain typo. A short query is within a handful of edits of almost every
    // short op name ("cut" -> "eq_set" is 6), so an edit distance that large is NOISE, not a
    // suggestion — it is what made a live unknown-op report read
    // "closest: eq_set, rename, set_units, eq_delete, set_folder" for op:"cut". Only a
    // distance small relative to the words being compared counts as a relation; anything
    // else scores 0 and suggest_doc_ops drops it entirely.
    // Keep in lockstep with services/mcp/op_registry.py::_score_match — this C++ copy is
    // what the LIVE apply path uses, and only the Python one had been fixed.
    const int d = levenshtein(q, cand);
    const int floor_d = std::max<int>(
        1, static_cast<int>(std::min(q.size(), cand.size())) / 3);
    if (d > floor_d) return 0;
    return 10000 - d * 100;
}

}  // namespace

std::vector<std::string> doc_op_kinds() {
    std::vector<std::string> out;
    out.reserve(sizeof(kDocOpKinds) / sizeof(kDocOpKinds[0]));
    for (const char* k : kDocOpKinds) out.emplace_back(k);
    return out;
}

// feature_type_names() is implemented in validate.cpp (owns FEATURE_TYPES).

std::vector<std::string> suggest_doc_ops(const std::string& unknown, std::size_t n) {
    if (n == 0) return {};
    const std::string q = lower(unknown);
    const auto kinds = doc_op_kinds();
    std::vector<std::pair<int, std::string>> scored;
    scored.reserve(kinds.size());
    for (const auto& k : kinds) {
        scored.emplace_back(score_match(q, lower(k)), k);
    }

    // If the unknown looks like a feature type, pin add_feature at the top.
    const auto ftypes = feature_type_names();
    bool is_feature_type = false;
    for (const auto& ft : ftypes) {
        if (lower(ft) == q) {
            is_feature_type = true;
            break;
        }
        // close: box-ish / cylinder-ish
        if (score_match(q, lower(ft)) >= 20000) {
            is_feature_type = true;
            break;
        }
    }
    if (is_feature_type) {
        // Force add_feature to win.
        for (auto& p : scored) {
            if (p.second == "add_feature") p.first = 200000;
        }
    }

    std::stable_sort(scored.begin(), scored.end(),
                     [](const auto& a, const auto& b) {
                         if (a.first != b.first) return a.first > b.first;
                         return a.second < b.second;
                     });

    std::vector<std::string> out;
    out.reserve(n);
    for (const auto& p : scored) {
        // `&& !out.empty()` used to force one suggestion out even when nothing scored —
        // that is how a genuinely unrelated word still got a "closest:" list. No relation
        // now means no list, and unknown_doc_op_message teaches the shape instead.
        if (p.first <= 0) break;
        out.push_back(p.second);
        if (out.size() >= n) break;
    }
    return out;
}

std::string unknown_doc_op_message(std::size_t idx, const std::string& k) {
    std::ostringstream oss;
    oss << "op[" << idx << "]: unknown op '" << k << "'";

    // Feature-type confusion is the mega-7 thrash mode (box/cylinder/sketch as ops).
    const std::string q = lower(k);
    std::string matched_ft;
    for (const auto& ft : feature_type_names()) {
        if (lower(ft) == q) {
            matched_ft = ft;
            break;
        }
    }
    if (!matched_ft.empty()) {
        oss << ". '" << matched_ft
            << "' is a feature TYPE, not an op — use "
               "{\"op\":\"add_feature\",\"feature\":{\"type\":\""
            << matched_ft << "\",...}}";
    }

    const auto closest = suggest_doc_ops(k, 5);
    if (!closest.empty()) {
        oss << ". closest: ";
        for (std::size_t i = 0; i < closest.size(); ++i) {
            if (i) oss << ", ";
            oss << closest[i];
        }
    } else if (matched_ft.empty()) {
        // Nothing in the vocabulary is even close, so the model is not making a typo — it is
        // writing in the wrong vocabulary. Teach the shape instead of guessing.
        // Mirrors services/mcp/op_registry.py::unknown_doc_op_message.
        oss << ". Ops are add_feature, edit_feature, delete_feature, set_material, rename, "
               "eq_set, sketch_* — geometry is never an op name: a shape is a feature TYPE "
               "under add_feature ({\"op\":\"add_feature\",\"feature\":{\"type\":\"extrude\",...}}), "
               "and a material-REMOVING operation is that same add_feature with "
               "params.mode:\"cut\"";
        if (is_removal_word(q)) {
            oss << " — so \"" << k << "\" is {\"op\":\"add_feature\",\"feature\":"
                   "{\"type\":\"extrude\",\"name\":\"Pocket\",\"refs\":{\"profile\":[...]},"
                   "\"params\":{\"mode\":\"cut\",\"depth\":10}}}";
        }
    }
    oss << "; see cad_guide()";
    return oss.str();
}

std::string doc_ops_cheatsheet_snippet() {
    std::ostringstream oss;
    oss << "[op vocabulary] Document ops (field op=...):\n  ";
    const auto kinds = doc_op_kinds();
    for (std::size_t i = 0; i < kinds.size(); ++i) {
        if (i) oss << ", ";
        oss << kinds[i];
        // Keep the inject compact — models only need the head of the list for recovery.
        if (i >= 24) {
            oss << ", …";
            break;
        }
    }
    oss << "\nPrimitives/solids are feature TYPES under add_feature, NOT top-level ops. "
           "Common types: ";
    const char* common[] = {"box", "cylinder", "sphere", "sketch", "extrude",
                            "revolve", "fillet", "chamfer", "hole", "mirror",
                            "linear_pattern", "circular_pattern", "standard_part"};
    for (std::size_t i = 0; i < sizeof(common) / sizeof(common[0]); ++i) {
        if (i) oss << ", ";
        oss << common[i];
    }
    oss << ".\nExample: {\"op\":\"add_feature\",\"feature\":{\"type\":\"box\",\"name\":\"B\","
           "\"params\":{\"length\":{\"value\":40,\"expr\":null},"
           "\"width\":{\"value\":30,\"expr\":null},"
           "\"height\":{\"value\":20,\"expr\":null}}}}\n"
           "Call cad_guide() for the full vocabulary + worked examples.";
    return oss.str();
}

}  // namespace chudware::doc
