#include "geom/booleans.h"

#include <BRepAlgoAPI_Common.hxx>
#include <BRepBuilderAPI_Copy.hxx>
#include <BRepTools_History.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <ShapeUpgrade_UnifySameDomain.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopTools_ListOfShape.hxx>

#include "geom/op_history.h"

namespace geom {

// All three follow the same OCCT recipe: seed the arguments + tools, Build() to run it,
// then gate on IsDone() before handing back the result. IsDone()==false means the kernel
// could not produce a valid result (e.g. non-manifold inputs, an empty intersection); we
// return a default-constructed (null) TopoDS_Shape so the caller can detect emptiness
// instead of crashing — the same contained-failure contract boolean.py enforces.
//
// PARALLELISM: OCCT booleans are single-threaded by default. We drive them through the
// DEFERRED API (default-construct → SetArguments/SetTools → SetRunParallel → Build)
// rather than the eager 2-arg constructor, because the constructor runs the op immediately
// so a later SetRunParallel would be a no-op. Parallel mode lets the BOPDS face-face
// intersection use the OCCT thread pool — a real win for tool-heavy ops like a bolt-circle
// cut. SMALL ops (box×box, single hole) stay serial: multi-agent boxes oversubscribe TBB
// when every tiny cut spins a pool (PERF_AUDIT 2026-07-11 P1 — ~35% self-time in
// syscall/lock/yield under load-16). Gate on total face count / tool count.
//
// NON-DESTRUCTIVE: without it an OCCT BOP may mutate its ARGUMENT shapes in place
// (tolerance refinement on the shared TShapes) while building the result. The compile
// checkpoints (document.h CompileCache) and the body tessellation cache both hold live
// TShapes across compiles, so an in-place tolerance bump would silently edit cached
// inputs. SetNonDestructive(true) makes the op copy-on-touch instead — the RESULT shape
// is geometrically identical either way; only the inputs are left untouched.
namespace {
// Count the solids in a shape (a single solid → 1, a disjoint compound → N).
int solid_count(const TopoDS_Shape& s) {
    int n = 0;
    for (TopExp_Explorer ex(s, TopAbs_SOLID); ex.More(); ex.Next()) ++n;
    return n;
}

int face_count_for_parallel(const TopoDS_Shape& s) {
    if (s.IsNull()) return 0;
    TopTools_IndexedMapOfShape m;
    TopExp::MapShapes(s, TopAbs_FACE, m);
    return m.Extent();
}

// 16 faces ≈ a box (6) + a modest tool. Bolt-circle tool unions and dense fillet
// bodies are well above this; pure box×box / single-hole cuts stay serial.
bool want_run_parallel(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    return face_count_for_parallel(a) + face_count_for_parallel(b) >= 16;
}

bool want_run_parallel_list(const TopTools_ListOfShape& args,
                            const TopTools_ListOfShape& tls) {
    // Multi-tool ops (fuse_many/cut_many with ≥4 tools) almost always benefit.
    if (tls.Extent() >= 4) return true;
    int n = 0;
    for (TopTools_ListIteratorOfListOfShape it(args); it.More(); it.Next())
        n += face_count_for_parallel(it.Value());
    for (TopTools_ListIteratorOfListOfShape it(tls); it.More(); it.Next())
        n += face_count_for_parallel(it.Value());
    return n >= 16;
}

// Run a two-operand boolean in (optionally) parallel deferred form.
template <class Op>
TopoDS_Shape run_pair(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    if (a.IsNull()) return TopoDS_Shape();
    TopTools_ListOfShape args, tls;
    args.Append(a);
    if (!b.IsNull()) tls.Append(b);
    Op op;
    op.SetArguments(args);
    op.SetTools(tls);
    op.SetRunParallel(want_run_parallel(a, b) ? Standard_True : Standard_False);
    op.SetNonDestructive(Standard_True);  // never mutate cached argument TShapes (see above)
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    if (op_history_recording()) op_history_record(op.History());
    return op.Shape();
}
}  // namespace

TopoDS_Shape fuse(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    if (b.IsNull()) return a;
    return run_pair<BRepAlgoAPI_Fuse>(a, b);
}

TopoDS_Shape fuse_unify(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    // T-PERF10: run fuse with the deferred API so we can inspect SectionEdges
    // before paying for UnifySameDomain (no-op / expensive walk when disjoint).
    if (b.IsNull()) return a;
    if (a.IsNull()) return TopoDS_Shape();
    TopTools_ListOfShape args, tls;
    args.Append(a);
    tls.Append(b);
    BRepAlgoAPI_Fuse op;
    op.SetArguments(args);
    op.SetTools(tls);
    op.SetRunParallel(want_run_parallel(a, b) ? Standard_True : Standard_False);
    op.SetNonDestructive(Standard_True);
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    if (op_history_recording()) op_history_record(op.History());
    TopoDS_Shape out = op.Shape();
    // T-PERF10 skipped unify when SectionEdges() was empty, but that shortcut misses the
    // FLUSH-WELD case: two solids sharing a coincident coplanar face (e.g. mirroring a body
    // about its own +X face) merge into one solid yet produce NO section edges — coincident
    // faces are "same domain", not a transversal intersection. That is exactly the case whose
    // now-coplanar side faces need collapsing. Gate on whether a weld actually happened: unify
    // iff the fuse reduced the solid count (flush contact OR overlap) or reported section
    // edges. Genuinely disjoint operands keep their solid count (compound of N) and still skip
    // the unify walk, so T-PERF10's perf win survives.
    const TopTools_ListOfShape& se = op.SectionEdges();
    const bool merged = solid_count(out) < solid_count(a) + solid_count(b);
    if (se.IsEmpty() && !merged) return out;
    return unify_same_domain(out);
}

TopoDS_Shape cut(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    // Cut is asymmetric: the result is a's material with b removed (a − b).
    if (b.IsNull()) return a;
    return run_pair<BRepAlgoAPI_Cut>(a, b);
}

TopoDS_Shape cut_unify(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    // A cut tool flush with an existing face (a pocket floor level with a step, a slot
    // through a wall joint) leaves the survivors of that face split along the tool's
    // boundary; build123d's BuildPart subtract runs clean() for the same reason.
    const TopoDS_Shape out = cut(a, b);
    return out.IsNull() ? out : unify_same_domain(out);
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
    op.SetRunParallel(want_run_parallel_list(args, tls) ? Standard_True : Standard_False);
    op.SetNonDestructive(Standard_True);  // never mutate cached argument TShapes (see above)
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    if (op_history_recording()) op_history_record(op.History());
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
    op.SetRunParallel(want_run_parallel_list(args, tls) ? Standard_True : Standard_False);
    op.SetNonDestructive(Standard_True);  // never mutate cached argument TShapes (see above)
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    if (op_history_recording()) op_history_record(op.History());
    return op.Shape();
}

TopoDS_Shape common(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    if (a.IsNull() || b.IsNull()) return TopoDS_Shape();
    return run_pair<BRepAlgoAPI_Common>(a, b);
}

TopoDS_Shape unify_same_domain(const TopoDS_Shape& s) {
    if (s.IsNull()) return s;
    try {
        // UnifySameDomain edits sub-shapes of its INPUT in place even in its default "safe
        // input" mode: it hangs temporary pcurves on the existing edges of the faces it
        // merges and re-ranges them (BRep_Builder::UpdateEdge/Range), and can re-seat a
        // reference face's surface. A NonDestructive boolean shares every untouched
        // face/edge TShape with its argument, i.e. with the compiler's cached checkpoints,
        // so unifying a boolean's output in place rewrote cached prefix shapes. c259
        // (6-axis base_revD, 2026-10-12): a slot cut, then deleting it, restored a
        // BRepCheck-invalid prefix 9 cm^3 larger, which the agent then saved. Unify a
        // topology copy; geometry handles stay shared (the algorithm copies curves before
        // transforming them).
        BRepBuilderAPI_Copy copier(s, Standard_False, Standard_False);
        const TopoDS_Shape work = copier.Shape();
        if (work.IsNull()) return s;
        if (op_history_recording()) {
            TopTools_ListOfShape args;
            args.Append(s);
            op_history_record(new BRepTools_History(args, copier));
        }
        // Unify faces AND edges sharing a domain (concatBSplines=false): the seam between
        // two flush coplanar faces collapses to one face, matching build123d's clean().
        ShapeUpgrade_UnifySameDomain up(work, Standard_True, Standard_True, Standard_False);
        up.Build();
        TopoDS_Shape r = up.Shape();
        if (r.IsNull()) return s;
        // UnifySameDomain rebuilds every face it merges; its history names the pieces.
        if (op_history_recording()) op_history_record(up.History());
        return r;
    } catch (...) {
        return s;  // best-effort: keep the un-unified shape on any kernel hiccup
    }
}

}  // namespace geom
