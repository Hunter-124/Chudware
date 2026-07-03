#include "ecad_palette.h"

namespace ui::ecad {

static EcadPalette gEcadPal;

EcadPalette& ecadPalette() { return gEcadPal; }

}  // namespace ui::ecad
