// Portable SolidWorks display-tessellation reader.
//
// Modern (SolidWorks 2015+) .SLDPRT/.SLDASM files carry the exact triangle strips used by
// SolidWorks' saved display list.  This reader recovers those strips without SolidWorks or COM.
// The result is intentionally described as tessellation geometry: it is editable after Chudware
// bakes it into a native import feature, but it is not the original parametric feature tree/B-rep.
#pragma once

#include <array>
#include <memory>
#include <string>
#include <vector>

#include "formats.h"

namespace chudware::doc {

struct SolidWorksTessellationBody {
    std::string name;
    std::shared_ptr<const MeshData> mesh;
    // SolidWorks serializes an affine matrix for row vectors in metres.  The importer converts
    // translation to millimetres when it turns this instance into an OCCT shape.
    std::array<double, 16> transform{};
};

// Read a modern SolidWorks part or assembly into saved-display tessellations.  Parts return one
// identity-placed mesh.  Assemblies preserve the component hierarchy, instance names, and composed
// transforms while sharing each distinct component mesh across its instances.
// Throws std::runtime_error for legacy/OLE files, malformed streams, or missing saved tessellation.
std::vector<SolidWorksTessellationBody> read_solidworks_tessellation(
    const std::string& path, bool assembly);

}  // namespace chudware::doc
