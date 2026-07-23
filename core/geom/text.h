// Text feature geometry (emboss / engrave a string onto a planar face).
//
// A `text` document feature (core/doc/document.cpp) marks a planar face with a string:
// it renders the glyph outlines to real B-rep faces (OCCT StdPrs_BRepFont +
// StdPrs_BRepTextBuilder over the bundled TrueType fonts), places those flat faces on
// the target face's plane, prisms them along the face normal, and booleans the prism
// onto the solid — FUSED proud of the surface (emboss / raised) or CUT into it
// (engrave / recessed). This is how a person stamps a part number, a logo line, or a
// "THIS SIDE UP" onto a model.
//
// WHY a separate translation unit: like thread.cpp (helical cuts) and curves.cpp (spine
// wires), this is a self-contained shape -> shape builder with no feature-tree
// knowledge. The document compiler resolves the target planar face (origin + normal +
// reading direction) and the font file path, then hands this builder the raw numbers.
// Keeping the font/freetype + boolean fragility in one auditable place mirrors the
// thread split.
//
// ROBUSTNESS CONTRACT (font rendering + booleans are both fragile): on ANY failure — a
// font that won't load, an empty glyph shape (e.g. an all-space string or a glyph the
// font lacks), an empty prism, a thrown kernel exception, a boolean that fails or whose
// result fails a BRepCheck validity test, or a boolean that does not actually change the
// volume — emboss_text NEVER returns a corrupt body. It returns the ORIGINAL body
// unchanged with fell_back=true and a human-readable warning, so a label that can't be
// marked degrades to the plain part rather than breaking it.
#pragma once

#include <string>

#include <TopoDS_Shape.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

namespace geom {

struct TextResult {
    TopoDS_Shape shape;        // the modified body (or the original on a fall-back)
    bool ok = false;           // false only on a truly unusable input (e.g. null body)
    bool fell_back = false;    // true: geometry failed, `shape` is the body UNMARKED
    std::string warning;       // non-empty on a fall-back (and some best-effort notes)
    double volume_delta = 0.0; // signed volume change actually applied (informational)
};

// Emboss (raise) or engrave (recess) `text` onto `body` at a planar-face placement.
//   face_origin  the anchor point ON the face where the text is centred/aligned (the
//                document handler computes this from the face centroid + any offset).
//   normal       unit face normal pointing OUT of the solid at that face.
//   xdir         unit reading direction of the text within the face plane.
//   font_path    FULL path to an existing .ttf/.otf file (the caller resolves & checks).
//   height       cap/ascender height of the text (mm).
//   depth        emboss height / engrave depth (mm, > 0).
//   engrave      true = cut INward (recessed mark); false = raise OUTward (proud).
//   halign       0 left, 1 centre, 2 right (about face_origin along xdir).
//   valign       0 baseline/bottom, 1 centre, 2 top (about face_origin along the
//                in-plane up direction).
//
// Never throws. See the ROBUSTNESS CONTRACT above.
TextResult emboss_text(const TopoDS_Shape& body, const gp_Pnt& face_origin,
                       const gp_Dir& normal, const gp_Dir& xdir, const std::string& text,
                       const std::string& font_path, double height, double depth,
                       bool engrave, int halign, int valign);

// Resolve a usable .ttf path for the `text` feature. `req` is the caller's optional
// `font` param: an absolute path to a font file (used verbatim if it exists), or a
// friendly name ("rubik"/"sans", "mono"/"jetbrains", "bold", "medium") mapped to a
// BUNDLED font. Searches, in order: an explicit existing path; the assets/fonts dir
// beside the loaded libchudware module; $CHUDWARE_DIST/assets/fonts; the repo host/assets/
// fonts (dev); and finally a Windows system font (arial/segoeui). Returns "" if nothing
// is found (emboss_text then falls back gracefully and leaves the part unmarked).
std::string resolve_font_path(const std::string& req);

}  // namespace geom
