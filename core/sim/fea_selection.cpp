#include "sim/fea_selection.h"

#include <algorithm>
#include <cstdio>
#include <map>
#include <set>
#include <utility>

namespace chudware::sim {

SelectionSpan selection_spans_contact(const std::string& kind,
                                      const std::string& selector,
                                      const std::vector<int>& node_ids,
                                      const ContactCandidateSet& contact) {
    SelectionSpan out;
    const std::size_t n = contact.component.size();
    if (n == 0 || contact.group_components.empty() || node_ids.size() < 2) return out;

    // Which bodies this ONE selection captured, and how many of its nodes each got.
    std::map<int, int> captured;
    for (int nid : node_ids) {
        if (nid < 0 || static_cast<std::size_t>(nid) >= n) continue;
        const int c = contact.component[static_cast<std::size_t>(nid)];
        if (c >= 0) ++captured[c];
    }
    if (captured.size() < 2) return out;

    // Only a pair the STUDY DECLARED as contact makes a multi-body selection a
    // spanned interface. Two other shapes are legitimate and must not be caught:
    // a selector that reaches a mounting pad on each of two bodies that never
    // touch each other (no declared pair joins them), and one fixture per body
    // of a contacting pair (two selectors, each holding its own side — that is a
    // properly grounded assembly whose interface still transmits whatever the
    // relative stiffness dictates). What is always wrong is ONE selector holding
    // both sides of an interface it was meant to hold one side of.
    std::set<std::pair<int, int>> declared;
    for (const auto& gc : contact.group_components)
        declared.emplace(std::min(gc[0], gc[1]), std::max(gc[0], gc[1]));

    for (auto a = captured.begin(); a != captured.end(); ++a) {
        for (auto b = std::next(a); b != captured.end(); ++b) {
            if (declared.find({a->first, b->first}) == declared.end()) continue;
            out.spans = true;
            out.body_a = a->first;
            out.captured_a = a->second;
            out.body_b = b->first;
            out.captured_b = b->second;
            for (int c : contact.component) {
                if (c == out.body_a) ++out.body_a_nodes;
                else if (c == out.body_b) ++out.body_b_nodes;
            }
            char text[1200];
            std::snprintf(
                text, sizeof(text),
                "%s %s SPANS the contact interface: its resolved nodes land on BOTH "
                "bodies of a pair this study declared as contact — mesh body %d "
                "(%d of %d node(s) captured) and mesh body %d (%d of %d). One "
                "selection cannot hold both sides of an interface: it grounds (or "
                "loads) the joint through the SELECTION instead of through the "
                "contact, so the pairs carry next to nothing and the study still "
                "converges, with a 0%% equilibrium error, on a non-answer. The "
                "selection reached across the interface's own %.4g mm median "
                "candidate gap (closest approach %.4g mm). Remedy: tighten the face "
                "filter's perp_tol_mm below that clearance — a plane/cylinder filter "
                "left at the 1.5 mm default reaches straight through a 0.35 mm fit "
                "into the mating body's face — or select by body/face id so the "
                "selection cannot leave its own body.",
                kind.c_str(), selector.c_str(), out.body_a, out.captured_a,
                out.body_a_nodes, out.body_b, out.captured_b, out.body_b_nodes,
                contact.median_gap0_mm, contact.min_gap0_mm);
            out.message = text;
            return out;
        }
    }
    return out;
}

}  // namespace chudware::sim
