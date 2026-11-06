// Included inside chudware::doc by picks.cpp; evidence is derived only from exact B-rep.
namespace {
using GeometryJson = nlohmann::json;
GeometryJson xyz(const gp_Pnt& p) { return {p.X(), p.Y(), p.Z()}; }
GeometryJson xyz(const gp_Dir& d) { return {d.X(), d.Y(), d.Z()}; }
bool fastener_coaxial(const gp_Ax1& a, const gp_Ax1& b, double tol) {
    return a.Direction().IsParallel(b.Direction(), 1e-8) &&
           gp_Lin(a).Distance(b.Location()) <= tol;
}
GeometryJson fastener_neighbors(const TopoDS_Shape& shape, const TopTools_IndexedMapOfShape& faces,
        const TopTools_IndexedDataMapOfShapeListOfShape& incidence, const TopoDS_Face& face,
        const TopoDS_Edge& edge, bool& complete) {
    GeometryJson out = GeometryJson::array();
    std::set<int> seen;
    if (!incidence.Contains(edge)) { complete = false; return out; }
    for (TopTools_ListIteratorOfListOfShape it(incidence.FindFromKey(edge)); it.More(); it.Next()) {
        if (it.Value().IsSame(face)) continue;
        int id = faces.FindIndex(it.Value());
        if (!id || !seen.insert(id).second) continue;
        if (out.size() == 64) { complete = false; break; }
        const auto sel = make_pick_selector(shape, "face", id, std::nullopt);
        out.push_back({{"topo", id}, {"sid", sel.value("sid", std::string())},
                       {"type", sel.value("surf", GeometryJson(nullptr))}});
    }
    if (out.size() != 1) complete = false; // open or non-manifold incidence
    return out;
}
GeometryJson fastener_annulus(const TopoDS_Shape& shape, const TopoDS_Face& face,
        const TopTools_IndexedMapOfShape& faces,
        const TopTools_IndexedDataMapOfShapeListOfShape& incidence) {
    BRepAdaptor_Surface surface(face);
    if (surface.GetType() != GeomAbs_Plane) return nullptr;
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(face, TopAbs_EDGE, edges);
    if (edges.Extent() != 2) return nullptr;
    gp_Circ circles[2];
    for (int i = 1; i <= 2; ++i) {
        BRepAdaptor_Curve c(TopoDS::Edge(edges(i)));
        if (c.GetType() != GeomAbs_Circle ||
            std::abs(std::abs(c.LastParameter() - c.FirstParameter()) - 2.0 * M_PI) > 1e-7)
            return nullptr;
        circles[i - 1] = c.Circle();
    }
    const double tol = std::max(1e-7, std::max(circles[0].Radius(), circles[1].Radius()) * 1e-8);
    if (circles[0].Location().Distance(circles[1].Location()) > tol ||
        !circles[0].Axis().Direction().IsParallel(circles[1].Axis().Direction(), 1e-8) ||
        !circles[0].Axis().Direction().IsParallel(surface.Plane().Axis().Direction(), 1e-8))
        return nullptr;
    const double inner = std::min(circles[0].Radius(), circles[1].Radius());
    const double outer = std::max(circles[0].Radius(), circles[1].Radius());
    GProp_GProps props;
    BRepGProp::SurfaceProperties(face, props);
    const double expected = M_PI * (outer * outer - inner * inner);
    if (inner <= tol || expected <= 0 || std::abs(props.Mass() - expected) > expected * 1e-7)
        return nullptr;
    gp_Dir normal = surface.Plane().Axis().Direction();
    if (face.Orientation() == TopAbs_REVERSED) normal.Reverse();
    else if (face.Orientation() != TopAbs_FORWARD) return nullptr;
    GeometryJson adjacent = GeometryJson::array();
    bool complete = true;
    for (int i = 1; i <= 2; ++i)
        for (auto& n : fastener_neighbors(shape, faces, incidence, face, TopoDS::Edge(edges(i)), complete))
            adjacent.push_back(n);
    return {{"center", xyz(circles[0].Location())}, {"normal", xyz(normal)},
            {"inner_radius", inner}, {"outer_radius", outer},
            {"adjacent_faces", adjacent}, {"adjacency_complete", complete}};
}
GeometryJson fastener_cylinder(const TopoDS_Shape& shape, const TopoDS_Face& face,
        const TopTools_IndexedMapOfShape& faces,
        const TopTools_IndexedDataMapOfShapeListOfShape& incidence) {
    BRepAdaptor_Surface surface(face);
    if (surface.GetType() != GeomAbs_Cylinder) return nullptr;
    const gp_Cylinder cylinder = surface.Cylinder();
    const gp_Ax1 axis = cylinder.Axis();
    const double radius = cylinder.Radius();
    const double tol = std::max(1e-7, radius * 1e-8);
    GeometryJson out = {{"axis_point", xyz(axis.Location())}, {"axis", xyz(axis.Direction())},
        {"radius", radius}, {"axial_start", nullptr}, {"axial_end", nullptr},
        {"complete_cylinder", nullptr}, {"status", "unsupported_trim"},
        {"ends", GeometryJson::array({
            {{"classification", "unknown"}, {"adjacent_faces", GeometryJson::array()}, {"adjacency_complete", false}},
            {{"classification", "unknown"}, {"adjacent_faces", GeometryJson::array()}, {"adjacency_complete", false}}})}};
    const auto concave = cylinder_face_concave(face);
    out["kind"] = concave ? GeometryJson(*concave ? "bore" : "boss") : GeometryJson(nullptr);
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(face, TopAbs_EDGE, edges);
    struct End { double t; double angle; TopoDS_Edge edge; };
    std::vector<End> ends;
    std::vector<std::pair<double, double>> sides;
    auto parameter = [&](const gp_Pnt& p) {
        return gp_Vec(axis.Location(), p).Dot(gp_Vec(axis.Direction()));
    };
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        if (BRep_Tool::IsClosed(edge, face)) continue;
        BRepAdaptor_Curve curve(edge);
        if (curve.GetType() == GeomAbs_Line) {
            if (!curve.Line().Direction().IsParallel(axis.Direction(), 1e-8)) return out;
            double a = parameter(curve.Value(curve.FirstParameter()));
            double b = parameter(curve.Value(curve.LastParameter()));
            if (b < a) std::swap(a, b);
            sides.emplace_back(a, b);
            continue;
        }
        if (curve.GetType() != GeomAbs_Circle) return out;
        const gp_Circ c = curve.Circle();
        const double angle = std::abs(curve.LastParameter() - curve.FirstParameter());
        if (angle <= 1e-8 || angle > 2.0 * M_PI + 1e-7 ||
            std::abs(c.Radius() - radius) > tol || !fastener_coaxial(axis, c.Axis(), tol))
            return out;
        ends.push_back({parameter(c.Location()), angle, edge});
    }
    if (ends.size() != 2) return out;
    if (ends[1].t < ends[0].t) std::swap(ends[0], ends[1]);
    const double length = ends[1].t - ends[0].t;
    const bool full = std::abs(ends[0].angle - 2.0 * M_PI) < 1e-7 &&
                      std::abs(ends[1].angle - 2.0 * M_PI) < 1e-7;
    if (std::abs(ends[0].angle - ends[1].angle) > 1e-7 ||
        (full ? !sides.empty() : sides.size() != 2)) return out;
    for (const auto& side : sides)
        if (std::abs(side.first - ends[0].t) > tol ||
            std::abs(side.second - ends[1].t) > tol) return out;
    GProp_GProps props;
    BRepGProp::SurfaceProperties(face, props);
    const double expected = ends[0].angle * radius * length;
    if (length <= tol || expected <= 0 || std::abs(props.Mass() - expected) > expected * 1e-7)
        return out;
    out["complete_cylinder"] = full;
    out["status"] = full ? "complete" : "partial_cylinder";
    for (int i = 0; i < 2; ++i) {
        const gp_Pnt p = axis.Location().Translated(gp_Vec(axis.Direction()) * ends[i].t);
        out[i == 0 ? "axial_start" : "axial_end"] = xyz(p);
        bool complete = true;
        auto adjacent = fastener_neighbors(shape, faces, incidence, face, ends[i].edge, complete);
        GeometryJson& end = out["ends"][i];
        end["adjacent_faces"] = adjacent;
        end["adjacency_complete"] = complete;
        if (!full || !concave || !*concave || !complete || adjacent.size() != 1) continue;
        const TopoDS_Face next = TopoDS::Face(faces(adjacent[0]["topo"].get<int>()));
        BRepAdaptor_Surface ns(next);
        if (ns.GetType() != GeomAbs_Plane) continue;
        BRepClass_FaceClassifier classifier(next, p, tol);
        if (classifier.State() == TopAbs_IN) {
            end["classification"] = "closed_bottom";
            continue;
        }
        const GeometryJson ring = fastener_annulus(shape, next, faces, incidence);
        if (ring.is_object() && ring.value("adjacency_complete", false)) {
            for (const auto& ref : ring["adjacent_faces"]) {
                const int id = ref["topo"].get<int>();
                const TopoDS_Face other = TopoDS::Face(faces(id));
                if (other.IsSame(face)) continue;
                BRepAdaptor_Surface other_surface(other);
                if (other_surface.GetType() != GeomAbs_Cylinder) continue;
                const gp_Cylinder other_cylinder = other_surface.Cylinder();
                const auto other_concave = cylinder_face_concave(other);
                if (other_concave && *other_concave &&
                    std::abs(other_cylinder.Radius() - radius) > tol &&
                    fastener_coaxial(axis, other_cylinder.Axis(), tol)) {
                    end["classification"] = "coaxial_transition";
                    break;
                }
            }
        }
        if (end["classification"] != "unknown") continue;
        // Only certify an opening when the end is at an extreme plane of the
        // whole body along the bore axis. A clear centerline alone would miss a
        // narrower bore or other obstruction around that line.
        gp_Trsf local;
        local.SetTransformation(gp_Ax3(axis.Location(), axis.Direction()));
        const TopoDS_Shape aligned = BRepBuilderAPI_Transform(shape, local, true).Shape();
        Bnd_Box box;
        box.SetGap(0.0);
        BRepBndLib::AddOptimal(aligned, box, false, false);
        if (box.IsVoid() || box.IsOpen()) continue;
        double xmin, ymin, zmin, xmax, ymax, zmax;
        box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
        if (std::abs(ends[i].t - (i == 0 ? zmin : zmax)) > tol) continue;
        bool outside = true;
        int solids = 0;
        for (TopExp_Explorer ex(shape, TopAbs_SOLID); ex.More(); ex.Next()) {
            ++solids;
            BRepClass3d_SolidClassifier solid(ex.Current(), p, tol);
            if (solid.State() != TopAbs_OUT) { outside = false; break; }
        }
        if (outside && solids) end["classification"] = "open_to_exterior";
    }
    return out;
}
} // namespace

void add_fastener_geometry(const TopoDS_Shape& shape, QueryPicks& picks) {
    if (picks.faces.empty()) return;
    TopTools_IndexedMapOfShape faces;
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    TopTools_IndexedDataMapOfShapeListOfShape incidence;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, incidence);
    for (std::size_t i = 0; i < picks.faces.size(); ++i) {
        auto& row = picks.selectors[i];
        row["cylinder"] = nullptr;
        row["bearing_annulus"] = nullptr;
        try {
            row["cylinder"] = fastener_cylinder(shape, picks.faces[i], faces, incidence);
            row["bearing_annulus"] = fastener_annulus(shape, picks.faces[i], faces, incidence);
        } catch (const Standard_Failure&) {
            row["geometry_error"] = "OCCT could not determine finite face geometry";
        }
    }
}
