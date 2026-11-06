// Editable native profiles for structural sections from the core section catalog.
#pragma once

#include <string>

#include "parts/parts.h"

namespace chudware::parts {

// `size` is "<standard>:<designation>", e.g. "AISC360:C6x13" or
// "NDS:2x4". A zero length is the internal absent-value sentinel: the length
// defaults to three times the section depth (or the larger lumber dimension).
// Positive finite lengths are used verbatim; negative and non-finite values,
// unknown sections, missing dimensions and unsupported profiles throw
// std::invalid_argument. Callers must reject an explicitly supplied zero.
//
// Steel is an idealized sharp-cornered profile using catalog d/bf/tf/tw, not a
// complete rolled-shape definition: the catalog does not supply rolling
// radii or flange taper. Lumber uses the catalog's rounded dressed dimensions.
// The returned metadata identifies these limitations and the source catalog.
// Link structural/sections.cpp alongside this implementation.
PartBuild build_section_geometry(const std::string& size, double length_mm = 0.0);

}  // namespace chudware::parts
