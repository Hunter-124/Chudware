#include "geom/booleans.h"

#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <ShapeUpgrade_UnifySameDomain.hxx>
#include <TopTools_ListOfShape.hxx>

namespace geom {

// All three follow the same OCCT recipe: construct the boolean algo (which seeds the
// arguments + tools), Build() to actually run it, then gate on IsDone() before handing
// back the result. IsDone()==false means the kernel could not produce a valid result
// (e.g. non-manifold inputs, an empty intersection); we return a default-constructed
// (null) TopoDS_Shape so the caller can detect emptiness instead of crashing — the
// same contained-failure contract boolean.py enforces with its try/except wrapper.
//
// NOTE: the modern BRepAlgoAPI_* constructors run the op eagerly, but calling Build()
// explicitly is harmless and keeps the code robust across OCCT versions (older ones
// require it). IsDone() is then authoritative either way.

TopoDS_Shape fuse(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    BRepAlgoAPI_Fuse op(a, b);
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    return op.Shape();
}

TopoDS_Shape cut(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    // Cut is asymmetric: the result is a's material with b removed (a − b).
    BRepAlgoAPI_Cut op(a, b);
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    return op.Shape();
}

// Multi-argument fuse/cut. The modern BRepAlgoAPI_BooleanOperation API takes the operands
// as two lists (Arguments + Tools) and runs ONE BOPDS intersection over the whole set —
// the union/difference is computed in a single pass instead of N pairwise rebuilds. We put
// `base` in Arguments and every replicated instance in Tools: Fuse(args, tools) = the union
// of all; Cut(args, tools) = base minus the union of all tools. Both are exactly what the
// pairwise loop produced, just without the O(N²) re-traversal of the growing accumulator.
TopoDS_Shape fuse_many(const TopoDS_Shape& base, const std::vector<TopoDS_Shape>& tools) {
    TopTools_ListOfShape args, tls;
    args.Append(base);
    for (const auto& t : tools)
        if (!t.IsNull()) tls.Append(t);
    if (tls.IsEmpty()) return base;  // nothing to add
    BRepAlgoAPI_Fuse op;
    op.SetArguments(args);
    op.SetTools(tls);
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    return op.Shape();
}

TopoDS_Shape cut_many(const TopoDS_Shape& base, const std::vector<TopoDS_Shape>& tools) {
    TopTools_ListOfShape args, tls;
    args.Append(base);
    for (const auto& t : tools)
        if (!t.IsNull()) tls.Append(t);
    if (tls.IsEmpty()) return base;  // nothing to remove
    BRepAlgoAPI_Cut op;
    op.SetArguments(args);
    op.SetTools(tls);
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    return op.Shape();
}

TopoDS_Shape common(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    BRepAlgoAPI_Common op(a, b);
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    return op.Shape();
}

TopoDS_Shape unify_same_domain(const TopoDS_Shape& s) {
    if (s.IsNull()) return s;
    try {
        // Unify faces AND edges sharing a domain (concatBSplines=false): the seam between
        // two flush coplanar faces collapses to one face, matching build123d's clean().
        ShapeUpgrade_UnifySameDomain up(s, Standard_True, Standard_True, Standard_False);
        up.Build();
        TopoDS_Shape r = up.Shape();
        return r.IsNull() ? s : r;
    } catch (...) {
        return s;  // best-effort: keep the un-unified shape on any kernel hiccup
    }
}

}  // namespace geom
