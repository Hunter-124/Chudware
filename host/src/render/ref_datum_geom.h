// GL-free builder for the reference-datum overlay geometry (datum axes / planes / points /
// coordinate systems / curves).
//
// The datum overlays are CAMERA-INDEPENDENT: every vertex is world space, so the whole set can be
// built once into a single vertex array with a merged draw list and re-used until the datum set,
// the scene extent, the selection, or the palette changes. The renderer used to upload one tiny
// vertex range and issue one glDrawArrays PER PRIMITIVE every frame (per dash, per cone triangle,
// per octahedron face — hundreds of upload+draw pairs for the origin marker alone).
//
// This translation unit deliberately has NO GL, ImGui, or theme dependency so the build is unit
// testable without a GL context (host/tests/test_ref_datum_geom.cpp). Renderer aliases RefDatum
// from here (`Renderer::RefDatum`), so there is exactly one definition of the datum record.
#pragma once
#include <string>
#include <unordered_set>
#include <vector>

// Header-relative: this header is reached by targets that do NOT put host/src on the
// include path (test_aicm pulls it in through renderer.h), and the release preset builds
// that target — a bare "vec_math.h" failed only there.
#include "../vec_math.h"

namespace render {

// A datum feature builds NO body, so the engine ships its resolved geometry out-of-band
// (doc_mirror::refDatums); the host draws these as reference overlays after the model, the way
// SolidWorks shows a created Axis/Plane/Point.
struct RefDatum {
    enum class Kind { Axis, Plane, Point, Csys, Curve };
    Kind kind = Kind::Axis;
    Vec3 origin{0, 0, 0};   // axis: a point on the line; plane/csys: origin; point: the point
    Vec3 dir{0, 0, 1};      // axis: unit direction; plane/csys: unit normal; (point: unused)
    Vec3 xdir{1, 0, 0};     // plane/csys: in-plane +U  (unused for axis / point)
    Vec3 ydir{0, 1, 0};     // plane/csys: in-plane +V  (unused for axis / point)
    // curve (helix / spiral / spline path): the sampled world-space polyline.
    // A curve feature builds no body — this is what the host draws AND hit-tests
    // so the user can pick the helix as a sweep path.
    std::vector<Vec3> points;
    std::string id;
    std::string name;
};

// Primitive modes, numerically equal to GL_LINES / GL_LINE_LOOP / GL_TRIANGLES so a batch entry
// can be handed straight to glDrawArrays. renderer.cpp static_asserts the match, so a drifting
// value is a compile error rather than garbled geometry.
constexpr unsigned int kModeLines = 0x0001;
constexpr unsigned int kModeLineLoop = 0x0002;
constexpr unsigned int kModeTriangles = 0x0004;

// One batched draw: a contiguous vertex range of `mode` primitives sharing one color and line
// width. Consecutive same-state ranges of independent primitives (GL_LINES / GL_TRIANGLES) are
// merged by the builder; GL_LINE_LOOP is never merged (its closing segment makes it order-bound).
struct RefDatumDraw {
    unsigned int mode = kModeLines;
    int first = 0;               // first vertex index into RefDatumBatch::verts / 3
    int count = 0;               // vertex count
    float rgba[4] = {0, 0, 0, 1};
    float lineWidth = 0.0f;      // 0 for triangle ranges (line width does not apply there)
};

// The built geometry: one vertex array (xyz triples, in draw order) plus the two draw lists the
// renderer issues — non-axis datums first, then axes LAST so dashed axes composite over the
// translucent plane fills (same two-pass contract as the old per-primitive path).
struct RefDatumBatch {
    std::vector<float> verts;
    std::vector<RefDatumDraw> nonAxis;
    std::vector<RefDatumDraw> axis;
    void clear() {
        verts.clear();
        nonAxis.clear();
        axis.clear();
    }
};

// The palette roles the datum colors read (ui::palette.axisX/axisY/axisZ/accent), copied in by
// the caller so this TU stays free of the theme + ImGui headers.
struct RefDatumPalette {
    float axisX[4] = {0.910f, 0.380f, 0.380f, 1.0f};
    float axisY[4] = {0.470f, 0.820f, 0.450f, 1.0f};
    float axisZ[4] = {0.420f, 0.620f, 0.960f, 1.0f};
    float accent[4] = {0.949f, 0.510f, 0.086f, 1.0f};
};
bool operator==(const RefDatumPalette& a, const RefDatumPalette& b);
inline bool operator!=(const RefDatumPalette& a, const RefDatumPalette& b) { return !(a == b); }

// Drawn/picked half-length of a reference axis (world units). Default + user axes span the
// origin-plane half-extent `extent` = sceneRadius*1.25; plane-derived auto axes (id prefix
// "axis.auto:") span the user-plane quad half-extent extent*0.55 so they terminate on the plane
// they were born from. Draw and hit-test MUST share this, never a fixed L.
float refAxisHalfLength(const std::string& id, float extent);

// Build both passes of the datum overlay geometry. `extent` is the origin-plane half-extent
// (sceneRadius * 1.25); `hidden` are the FeatureManager-hidden datum ids; `selectedId` /
// `editingId` are the emphasized datum and the datum plane open in the PropertyManager.
// `out` is cleared first and reused across rebuilds (its capacity is retained).
void buildRefDatumBatch(const std::vector<RefDatum>& datums,
                        const std::unordered_set<std::string>& hidden,
                        const std::string& selectedId, const std::string& editingId, float extent,
                        const RefDatumPalette& pal, RefDatumBatch& out);

}  // namespace render
