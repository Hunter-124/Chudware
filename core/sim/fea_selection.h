// A fixture or load SELECTION must not span a contact interface.
//
// Every boundary condition in a study is a geometric face query resolved to mesh
// nodes by a TOLERANCE, and across a contact interface the two facts collide: the
// clearance a contact study exists to open and close is routinely an order of
// magnitude SMALLER than the tolerance that finds the nodes of a face. A cylinder
// or plane filter carrying the default perp_tol_mm of 1.5 mm, pointed at one side
// of a 0.35 mm fit, reaches straight through it and resolves the MATING body's
// face as well.
//
// The live case (FRAME-PEDAL-BRAKE keyway isolate, 0.35 mm radial clearance): one
// fixture face-set with a cylinder filter at perp_tol_mm = 1.5 mm captured the
// bore nodes of BOTH bodies. Both bodies were therefore grounded, the contact
// pairs had no load to carry, and the study reported a CONVERGED result with a
// 0.0% equilibrium error — a non-answer, because every other gate in the packet
// is about the solve and this defect is in the selection. Nothing downstream can
// see it: the mechanism gate is happy (both components reach a fixture), the
// resolution guard is happy (the interface is meshed fine), equilibrium is
// perfect (the fixtures react everything).
//
// So it is decided HERE, on the resolved nodes, before the solve: one selector
// that lands on both bodies of a declared contact pair is refused, naming the
// selector, the bodies, the count per body and the remedy.
#pragma once

#include <string>
#include <vector>

#include "sim/fea_contact.h"

namespace chudware::sim {

struct SelectionSpan {
    bool spans = false;
    // The two component ordinals of the declared contact pair this ONE selection
    // captured (ascending), how many of its nodes landed on each, and how many
    // nodes each body has in total — the scale that says whether the stray
    // capture was a whole face or a handful of nodes. Either way it grounds the
    // body, which is why the count is evidence and not a threshold.
    int body_a = -1;
    int body_b = -1;
    int captured_a = 0;
    int captured_b = 0;
    int body_a_nodes = 0;
    int body_b_nodes = 0;
    // The refusal text while `spans`; empty otherwise.
    std::string message;
};

// `kind` is "fixture" or "load", `selector` the label the caller can act on (the
// face query / sid, not a mesh ordinal), `node_ids` the nodes ONE selector
// resolved to, and `contact` the candidate set whose `component` labels the
// bodies and whose `group_components` says which body pairs the study declared
// as contact. Cheap: one pass over `node_ids` and, only on a hit, one pass over
// `contact.component`.
SelectionSpan selection_spans_contact(const std::string& kind,
                                      const std::string& selector,
                                      const std::vector<int>& node_ids,
                                      const ContactCandidateSet& contact);

}  // namespace chudware::sim
