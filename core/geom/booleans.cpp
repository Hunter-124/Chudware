#include "geom/booleans.h"

#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>

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

TopoDS_Shape common(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    BRepAlgoAPI_Common op(a, b);
    op.Build();
    if (!op.IsDone()) return TopoDS_Shape();
    return op.Shape();
}

}  // namespace geom
