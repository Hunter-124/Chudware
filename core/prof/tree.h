// Nested call-tree / phase-tree views for the sampling profiler + phase registry.
//
// WHY: flat tables and folded stacks answer "which frames are hot", but an agent reading
// a profile needs a FILE-TREE style walk — root + hot children first, then drill down by
// `path` without ingesting the whole trace. This module is pure: it builds a hierarchical
// tree from already-symbolized folded stacks (or from phase wall-clock rollups) and
// serializes a depth-capped, path-scoped subtree as compact JSON.
//
// FOLDED INPUT (root->leaf, Brendan-Gregg):
//   "main;foo;bar 10\nmain;foo;baz 5\n"
// becomes
//   root
//     main (total=15, self=0)
//       foo (total=15, self=0)
//         bar (total=10, self=10)
//         baz (total=5, self=5)
//
// PATH addressing uses '/' between frame names (e.g. "main/foo/bar"). Depth is measured
// from the selected node (0 = only that node, 1 = node + direct children, ...; <0 = full).
// Children are sorted by total samples (hot first). `top` caps how many children are
// emitted at each level (0 = unlimited).
//
// Byte-identical with the existing folded / speedscope / flat exports: this module never
// touches the sampler aggregation; callers feed it a snapshot (usually report()["folded"]).
#pragma once

#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware {
namespace prof {
namespace tree {

// One node in the call / phase tree.
struct Node {
    std::string name;
    unsigned long long self_samples = 0;   // exclusive (leaf) samples at this frame
    unsigned long long total_samples = 0;  // inclusive (self + all descendants)
    double pct_of_parent = 0.0;            // 100 * total / parent.total (100 at root)
    double self_ms = 0.0;                  // wall-clock exclusive (phase trees only)
    double total_ms = 0.0;                 // wall-clock inclusive (phase trees only)
    std::vector<Node> children;
};

// Parse Brendan-Gregg folded text into (root->leaf frames, count) pairs. Blank lines and
// lines without a trailing count are skipped. Deterministic order = input order.
std::vector<std::pair<std::vector<std::string>, unsigned long long>>
parse_folded(const std::string& folded);

// Build a hierarchical tree from root->leaf stacks. Root node's name is "" (synthetic);
// its children are the first frames of each stack. Self/total sample counts are exact.
Node build_tree(
    const std::vector<std::pair<std::vector<std::string>, unsigned long long>>& stacks);

// Convenience: parse folded text then build_tree.
Node build_tree_from_folded(const std::string& folded);

// Walk `path` ("a/b/c", empty = root) and return a pointer to that node, or nullptr.
const Node* find_path(const Node& root, const std::string& path);

// Serialize a (optionally path-scoped, depth-capped, top-N-children) subtree.
// opts (all optional):
//   path  : string, default ""
//   depth : int, default -1 (unlimited); 0 = selected node only
//   top   : int, default 0 (unlimited children per level)
//   mode  : "samples" (default) | "ms" — which counters to emit (samples always for
//           sample trees; phase trees emit both)
// Returns:
//   { "ok": true,
//     "path": "<resolved path>",
//     "depth": <applied depth>,
//     "top": <applied top>,
//     "root": { "name","self_samples","total_samples","pct_of_parent",
//               "self_ms"?,"total_ms"?,"path","children":[...]} }
// or { "ok": false, "error": "..." } when path is missing.
nlohmann::json tree_to_json(const Node& root, const nlohmann::json& opts = nlohmann::json::object());

// One-shot: folded text -> scoped JSON tree (combines build + to_json).
nlohmann::json from_folded(const std::string& folded,
                           const nlohmann::json& opts = nlohmann::json::object());

// Build a nested phase tree from a flat list of {name, ms, count} phases by splitting
// names on '.' (e.g. "solve.assemble" -> solve / assemble). `study_total_ms` is the
// study wall time used for root total_ms / pct. Self_ms at an interior node is
// max(0, own_ms - sum(children total_ms)) when the dotted name also has a direct entry;
// pure prefixes have own_ms=0 so self_ms = 0 and total_ms = sum(children).
Node build_phase_tree(
    const std::vector<std::pair<std::string, std::pair<double, unsigned long long>>>& phases,
    double study_total_ms);

}  // namespace tree
}  // namespace prof
}  // namespace chudware
