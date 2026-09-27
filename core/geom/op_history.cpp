#include "geom/op_history.h"

#include <functional>
#include <sstream>
#include <utility>

#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopTools_MapOfShape.hxx>

namespace geom {

namespace {
thread_local std::vector<OpHistoryScope*> t_scopes;

using ImagesFn = std::function<void(const TopoDS_Shape&, TopTools_ListOfShape&)>;

// One kind's provenance: for each input index (1-based) with images, the result indices.
void encode_kind(std::ostringstream& out, const char* tag, const TopoDS_Shape& input,
                 const TopoDS_Shape& result, TopAbs_ShapeEnum kind, const ImagesFn& images_of) {
    TopTools_IndexedMapOfShape in, res;
    TopExp::MapShapes(input, kind, in);
    TopExp::MapShapes(result, kind, res);
    std::vector<std::pair<int, std::vector<int>>> rows;
    for (int i = 1; i <= in.Extent(); ++i) {
        TopTools_ListOfShape candidates;
        images_of(in.FindKey(i), candidates);
        std::vector<int> images;
        for (TopTools_ListIteratorOfListOfShape it(candidates); it.More(); it.Next())
            if (const int j = res.FindIndex(it.Value()); j > 0) images.push_back(j);
        if (!images.empty()) rows.emplace_back(i, std::move(images));
    }
    out << tag << ' ' << in.Extent() << ' ' << res.Extent() << ' ' << rows.size() << '\n';
    for (const auto& [i, images] : rows) {
        out << i << ' ' << images.size();
        for (int j : images) out << ' ' << j;
        out << '\n';
    }
}

std::string encode(const TopoDS_Shape& input, const TopoDS_Shape& result,
                   const ImagesFn& images_of) {
    if (input.IsNull() || result.IsNull()) return {};
    std::ostringstream out;
    encode_kind(out, "F", input, result, TopAbs_FACE, images_of);
    encode_kind(out, "E", input, result, TopAbs_EDGE, images_of);
    return out.str();
}

// Returns false on a malformed or mismatched section (the caller then records nothing).
bool decode_kind(std::istringstream& in, const char* tag, const TopoDS_Shape& input,
                 const TopoDS_Shape& result, TopAbs_ShapeEnum kind, BRepTools_History& hist) {
    std::string t;
    int nin = 0, nres = 0;
    std::size_t rows = 0;
    if (!(in >> t >> nin >> nres >> rows) || t != tag) return false;
    TopTools_IndexedMapOfShape inm, resm;
    TopExp::MapShapes(input, kind, inm);
    TopExp::MapShapes(result, kind, resm);
    if (inm.Extent() != nin || resm.Extent() != nres) return false;
    for (std::size_t r = 0; r < rows; ++r) {
        int i = 0;
        std::size_t m = 0;
        if (!(in >> i >> m) || i < 1 || i > nin) return false;
        const TopoDS_Shape& s = inm.FindKey(i);
        for (std::size_t k = 0; k < m; ++k) {
            int j = 0;
            if (!(in >> j) || j < 1 || j > nres) return false;
            const TopoDS_Shape& image = resm.FindKey(j);
            // An untouched shape needs no entry: tracing already keeps the shape itself.
            if (!image.IsSame(s)) hist.AddModified(s, image);
        }
    }
    return true;
}
}  // namespace

OpHistoryScope::OpHistoryScope(bool active) : active_(active) {
    if (active_) t_scopes.push_back(this);
}

OpHistoryScope::~OpHistoryScope() {
    if (active_ && !t_scopes.empty() && t_scopes.back() == this) t_scopes.pop_back();
}

bool op_history_recording() { return !t_scopes.empty(); }

void op_history_record(const Handle(BRepTools_History)& history) {
    if (history.IsNull() || t_scopes.empty()) return;
    t_scopes.back()->histories_.push_back(history);
}

void op_history_images(const TopoDS_Shape& initial,
                       const std::vector<Handle(BRepTools_History)>& histories,
                       TopTools_ListOfShape& images) {
    TopTools_MapOfShape seen;
    TopTools_ListOfShape frontier;
    seen.Add(initial);
    frontier.Append(initial);
    images.Append(initial);
    while (!frontier.IsEmpty()) {
        const TopoDS_Shape s = frontier.First();
        frontier.RemoveFirst();
        for (const Handle(BRepTools_History)& h : histories) {
            if (h.IsNull() || !BRepTools_History::IsSupportedType(s)) continue;
            for (TopTools_ListIteratorOfListOfShape it(h->Modified(s)); it.More(); it.Next())
                if (seen.Add(it.Value())) {
                    frontier.Append(it.Value());
                    images.Append(it.Value());
                }
        }
    }
}

std::string op_provenance_encode(const TopoDS_Shape& input, const TopoDS_Shape& result,
                                 BRepBuilderAPI_MakeShape& algo) {
    return encode(input, result, [&algo](const TopoDS_Shape& s, TopTools_ListOfShape& out) {
        out.Append(s);
        for (TopTools_ListIteratorOfListOfShape it(algo.Modified(s)); it.More(); it.Next())
            out.Append(it.Value());
    });
}

std::string op_provenance_encode(const TopoDS_Shape& input, const TopoDS_Shape& result,
                                 const std::vector<Handle(BRepTools_History)>& histories) {
    return encode(input, result, [&histories](const TopoDS_Shape& s, TopTools_ListOfShape& out) {
        op_history_images(s, histories, out);
    });
}

void op_provenance_record(const TopoDS_Shape& input, const TopoDS_Shape& result,
                          const std::string& encoded) {
    if (!op_history_recording() || encoded.empty() || input.IsNull() || result.IsNull()) return;
    Handle(BRepTools_History) hist = new BRepTools_History();
    std::istringstream in(encoded);
    if (!decode_kind(in, "F", input, result, TopAbs_FACE, *hist)) return;
    if (!decode_kind(in, "E", input, result, TopAbs_EDGE, *hist)) return;
    op_history_record(hist);
}

}  // namespace geom
