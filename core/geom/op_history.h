#pragma once

// Operation-history recording for feature face/edge ownership (the host owner_map).
//
// WHY: a feature that MODIFIES a body (boolean + UnifySameDomain, fillet, chamfer) rebuilds
// most of the body's faces, so "is this face the same TShape as one in the source body?"
// hands every face to the last dress-up feature (a final chamfer owned all 89 faces of the
// user's turntable part). The geom ops that rebuild faces know which output face is a
// MODIFIED image of which input face; they record that here, and the document compiler traces
// each source face through the recorded histories to find the output faces it became.
//
// Recording is opt-in and per thread: the compiler opens an OpHistoryScope around one
// feature; ops running on that thread append to the innermost open scope. With no open
// scope every hook is a no-op, so geometry-only callers pay nothing.

#include <string>
#include <vector>

#include <BRepBuilderAPI_MakeShape.hxx>
#include <BRepTools_History.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS_Shape.hxx>

namespace geom {

class OpHistoryScope {
public:
    // `active == false` opens nothing (ops keep not recording).
    explicit OpHistoryScope(bool active);
    ~OpHistoryScope();
    OpHistoryScope(const OpHistoryScope&) = delete;
    OpHistoryScope& operator=(const OpHistoryScope&) = delete;

    // Histories recorded on this thread while this scope was the innermost one, in op order.
    const std::vector<Handle(BRepTools_History)>& histories() const { return histories_; }

private:
    friend void op_history_record(const Handle(BRepTools_History)& history);
    bool active_;
    std::vector<Handle(BRepTools_History)> histories_;
};

// True when an OpHistoryScope is open on this thread (ops should record).
bool op_history_recording();

// Append `history` to the innermost open scope on this thread. Null / no scope -> no-op.
void op_history_record(const Handle(BRepTools_History)& history);

// Fork-safe provenance of a local operation (fillet/chamfer): for every face and edge of
// `input`, the indices (TopExp::MapShapes order) of the `result` faces/edges that are the
// same shape or a Modified() image of it. Indices, not TShapes, so the encoding survives the
// out-of-process build in util::isolate_shape_build (the parent re-reads the result, which
// has fresh TShapes but the same MapShapes order). Generated shapes are not listed: they
// belong to the operation's own feature.
std::string op_provenance_encode(const TopoDS_Shape& input, const TopoDS_Shape& result,
                                 BRepBuilderAPI_MakeShape& algo);

// Same encoding for an operation that ran as a chain of recorded histories (e.g. several
// sequential booleans): an input shape's images are everything op_history_images reaches.
std::string op_provenance_encode(const TopoDS_Shape& input, const TopoDS_Shape& result,
                                 const std::vector<Handle(BRepTools_History)>& histories);

// Every shape reachable from `initial` through Modified() in ANY of `histories`, plus
// `initial` itself (an untouched shape is its own image). Reachability, not sequential
// composition, so a feature that tries an alternative boolean on the same input (and keeps
// only one result) still traces: the caller keeps only images present in the final shape.
void op_history_images(const TopoDS_Shape& initial,
                       const std::vector<Handle(BRepTools_History)>& histories,
                       TopTools_ListOfShape& images);

// Decode `encoded` against the SAME `input` and the (possibly re-read) `result` and record it
// as a BRepTools_History. A face/edge count mismatch means the encoding does not describe this
// result: nothing is recorded.
void op_provenance_record(const TopoDS_Shape& input, const TopoDS_Shape& result,
                          const std::string& encoded);

}  // namespace geom
