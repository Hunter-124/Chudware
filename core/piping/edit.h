#pragma once

#include "piping/pyutil.h"

namespace chudware::piping {
// Atomic, engine-owned network editing shared by the viewport and agent tools.
pyutil::ojson piping_edit(const pyutil::ojson& params);
}  // namespace chudware::piping
