// Implementation of the nested call-tree / phase-tree builder. See tree.h.
#include "prof/tree.h"

#include <algorithm>
#include <cctype>
#include <functional>
#include <sstream>

using nlohmann::json;

namespace aicad {
namespace prof {
namespace tree {

namespace {

// Split path "a/b/c" into {"a","b","c"}; empty path -> empty vector. Consecutive '/' and
// leading/trailing '/' are ignored so "a//b/" == "a/b".
std::vector<std::string> split_path(const std::string& path) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : path) {
        if (c == '/') {
            if (!cur.empty()) {
                out.push_back(std::move(cur));
                cur.clear();
            }
        } else {
            cur.push_back(c);
        }
    }
    if (!cur.empty()) out.push_back(std::move(cur));
    return out;
}

// Find-or-create a child by name; returns reference into parent.children.
Node& child_of(Node& parent, const std::string& name) {
    for (auto& c : parent.children) {
        if (c.name == name) return c;
    }
    parent.children.push_back(Node{});
    parent.children.back().name = name;
    return parent.children.back();
}

void sort_children_hot_first(Node& n) {
    std::sort(n.children.begin(), n.children.end(), [](const Node& a, const Node& b) {
        // Prefer wall-clock when either node has it (phase trees); else sample totals.
        if (a.total_ms > 0.0 || b.total_ms > 0.0) {
            if (a.total_ms != b.total_ms) return a.total_ms > b.total_ms;
        }
        if (a.total_samples != b.total_samples) return a.total_samples > b.total_samples;
        return a.name < b.name;  // stable for ties (deterministic)
    });
    for (auto& c : n.children) sort_children_hot_first(c);
}

// Recompute pct_of_parent from parent totals. Prefer wall-clock ms when present
// (phase trees); fall back to sample counts (call trees).
void fill_pct(Node& n, double parent_total_samples, double parent_total_ms) {
    if (parent_total_ms > 0.0) {
        n.pct_of_parent = 100.0 * n.total_ms / parent_total_ms;
    } else if (parent_total_samples > 0.0) {
        n.pct_of_parent = 100.0 * double(n.total_samples) / parent_total_samples;
    } else {
        n.pct_of_parent = 100.0;
    }
    for (auto& c : n.children)
        fill_pct(c, double(n.total_samples), n.total_ms);
}

// Serialize one node + descendants. depth_left < 0 means unlimited.
// path_so_far is the absolute path of this node ("" for synthetic root).
json emit_node(const Node& n, const std::string& path_so_far, int depth_left, int top,
               bool emit_ms) {
    json children = json::array();
    if (depth_left != 0) {
        const int next_depth = depth_left < 0 ? -1 : depth_left - 1;
        int emitted = 0;
        for (const auto& c : n.children) {
            if (top > 0 && emitted >= top) break;
            std::string child_path =
                path_so_far.empty() ? c.name : (path_so_far + "/" + c.name);
            children.push_back(emit_node(c, child_path, next_depth, top, emit_ms));
            ++emitted;
        }
    }
    json j{{"name", n.name},
           {"self_samples", n.self_samples},
           {"total_samples", n.total_samples},
           {"pct_of_parent", n.pct_of_parent},
           {"path", path_so_far},
           {"children", std::move(children)}};
    if (emit_ms) {
        j["self_ms"] = n.self_ms;
        j["total_ms"] = n.total_ms;
    }
    // How many children were elided by top-N at this level (informative for drill-down).
    if (top > 0 && static_cast<int>(n.children.size()) > top && depth_left != 0) {
        j["children_elided"] = static_cast<int>(n.children.size()) - top;
    }
    return j;
}

}  // namespace

std::vector<std::pair<std::vector<std::string>, unsigned long long>>
parse_folded(const std::string& folded) {
    std::vector<std::pair<std::vector<std::string>, unsigned long long>> out;
    std::istringstream iss(folded);
    std::string line;
    while (std::getline(iss, line)) {
        if (line.empty()) continue;
        // Trim trailing CR (Windows folded files).
        if (!line.empty() && line.back() == '\r') line.pop_back();
        // Last space-separated token is the count; the rest is the stack.
        auto sp = line.find_last_of(' ');
        if (sp == std::string::npos) continue;
        std::string stack = line.substr(0, sp);
        std::string count_s = line.substr(sp + 1);
        if (stack.empty() || count_s.empty()) continue;
        bool all_digit = true;
        for (char c : count_s) {
            if (!std::isdigit(static_cast<unsigned char>(c))) {
                all_digit = false;
                break;
            }
        }
        if (!all_digit) continue;
        unsigned long long count = 0;
        try {
            count = std::stoull(count_s);
        } catch (...) {
            continue;
        }
        std::vector<std::string> frames;
        std::string cur;
        for (char c : stack) {
            if (c == ';') {
                if (!cur.empty()) frames.push_back(std::move(cur));
                cur.clear();
            } else {
                cur.push_back(c);
            }
        }
        if (!cur.empty()) frames.push_back(std::move(cur));
        if (frames.empty() || count == 0) continue;
        out.emplace_back(std::move(frames), count);
    }
    return out;
}

Node build_tree(
    const std::vector<std::pair<std::vector<std::string>, unsigned long long>>& stacks) {
    Node root;
    root.name = "";  // synthetic root
    for (const auto& st : stacks) {
        if (st.first.empty() || st.second == 0) continue;
        Node* cur = &root;
        cur->total_samples += st.second;
        for (size_t i = 0; i < st.first.size(); ++i) {
            Node& child = child_of(*cur, st.first[i]);
            child.total_samples += st.second;
            if (i + 1 == st.first.size()) {
                child.self_samples += st.second;
            }
            cur = &child;
        }
    }
    // Root self = samples whose stack was empty (none) — leave 0.
    sort_children_hot_first(root);
    fill_pct(root, double(root.total_samples), 0.0);
    root.pct_of_parent = 100.0;
    return root;
}

Node build_tree_from_folded(const std::string& folded) {
    return build_tree(parse_folded(folded));
}

const Node* find_path(const Node& root, const std::string& path) {
    auto segs = split_path(path);
    const Node* cur = &root;
    for (const auto& s : segs) {
        const Node* next = nullptr;
        for (const auto& c : cur->children) {
            if (c.name == s) {
                next = &c;
                break;
            }
        }
        if (!next) return nullptr;
        cur = next;
    }
    return cur;
}

json tree_to_json(const Node& root, const json& opts) {
    const std::string path = opts.value("path", std::string());
    const int depth = opts.value("depth", -1);
    const int top = opts.value("top", 0);
    const std::string mode = opts.value("mode", std::string("samples"));
    const bool emit_ms = (mode == "ms" || mode == "both" || root.total_ms > 0.0);

    const Node* selected = find_path(root, path);
    if (!selected) {
        return json{{"ok", false},
                    {"error", "path not found: " + path},
                    {"path", path}};
    }
    // When path selects a non-root node, emit that node as the response root with its
    // absolute path so the reader can keep drilling.
    std::string abs_path = path;
    // Normalize empty path for synthetic root to "".
    if (!split_path(path).empty()) {
        // rebuild clean path
        auto segs = split_path(path);
        abs_path.clear();
        for (size_t i = 0; i < segs.size(); ++i) {
            if (i) abs_path += '/';
            abs_path += segs[i];
        }
    } else {
        abs_path.clear();
    }

    json node = emit_node(*selected, abs_path, depth, top, emit_ms);
    return json{{"ok", true},
                {"path", abs_path},
                {"depth", depth},
                {"top", top},
                {"root", std::move(node)}};
}

json from_folded(const std::string& folded, const json& opts) {
    Node root = build_tree_from_folded(folded);
    return tree_to_json(root, opts);
}

Node build_phase_tree(
    const std::vector<std::pair<std::string, std::pair<double, unsigned long long>>>& phases,
    double study_total_ms) {
    // First pass: insert every dotted name as a path, accumulating ms/count at the leaf
    // segment. Interior prefixes get total_ms rolled up in a second pass.
    Node root;
    root.name = "";
    root.total_ms = study_total_ms;

    // Track direct (own) ms at every full path so self_ms = own - sum(children).
    // Keyed by absolute path string ("" for root).
    // We store own_ms on nodes temporarily in self_ms, then convert.
    for (const auto& ph : phases) {
        const std::string& name = ph.first;
        const double ms = ph.second.first;
        const unsigned long long count = ph.second.second;
        if (name.empty()) continue;

        // Split on '.'.
        std::vector<std::string> segs;
        std::string cur;
        for (char c : name) {
            if (c == '.') {
                if (!cur.empty()) segs.push_back(std::move(cur));
                cur.clear();
            } else {
                cur.push_back(c);
            }
        }
        if (!cur.empty()) segs.push_back(std::move(cur));
        if (segs.empty()) continue;

        Node* n = &root;
        for (size_t i = 0; i < segs.size(); ++i) {
            Node& child = child_of(*n, segs[i]);
            // Inclusive rollup: every ancestor sees this phase's ms/count.
            child.total_ms += ms;
            child.total_samples += count;  // reuse sample counter as "count"
            if (i + 1 == segs.size()) {
                // Direct entry at this leaf path: own wall time.
                child.self_ms += ms;
                child.self_samples += count;
            }
            n = &child;
        }
        root.total_samples += count;
    }

    // If study_total_ms was 0, fall back to sum of top-level children.
    if (root.total_ms <= 0.0) {
        double sum = 0.0;
        for (const auto& c : root.children) sum += c.total_ms;
        root.total_ms = sum;
    }

    // Convert self_ms: currently holds OWN direct ms; exclusive self =
    // max(0, own - sum(children totals)). For pure interior prefixes own=0 so self=0.
    // For a leaf with no children, self = own = total.
    std::function<void(Node&)> finalize = [&](Node& n) {
        double child_sum = 0.0;
        for (auto& c : n.children) {
            finalize(c);
            child_sum += c.total_ms;
        }
        // n.self_ms currently = direct own ms recorded at this exact name.
        const double own = n.self_ms;
        // Exclusive: direct own not explained by children, or 0 for pure parents.
        // When own was only recorded at leaves, interior self stays 0 and total is rollup.
        // When a name appears both as a leaf AND as a prefix (rare), own may exceed children.
        n.self_ms = own > child_sum ? own - child_sum : (n.children.empty() ? own : 0.0);
        // If leaf with no children: self_ms should equal total_ms.
        if (n.children.empty() && n.self_ms <= 0.0 && n.total_ms > 0.0) {
            n.self_ms = n.total_ms;
        }
    };
    finalize(root);
    // Root exclusive = study wall not covered by top-level phases.
    {
        double child_sum = 0.0;
        for (const auto& c : root.children) child_sum += c.total_ms;
        root.self_ms = root.total_ms > child_sum ? root.total_ms - child_sum : 0.0;
    }

    sort_children_hot_first(root);
    fill_pct(root, double(root.total_samples), root.total_ms);
    root.pct_of_parent = 100.0;
    return root;
}

}  // namespace tree
}  // namespace prof
}  // namespace aicad
