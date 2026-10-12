// AutoCAD DWG: native R2000 (AC1015) writing, and LibreDWG's `dwg2dxf` for the 2D entities
// of a DWG being read.
//
// Writing is Chudware's own encoder (dwg_write.h) and needs no external tool. LibreDWG is
// GPL-3.0, so Chudware never links or bundles it (contracts/licenses.md): `dwg2dxf` runs as a
// subprocess with literal argv, found on PATH first, then in the per-user managed install
// that scripts/provision_libredwg.py builds, mirroring services/formats/libredwg.py::tool.
#pragma once

#include <string>
#include <vector>

#include "doc/dxf.h"

namespace chudware::doc {

// Absolute path of LibreDWG tool `name` (e.g. "dwg2dxf"): PATH, then
// <appdata>/Chudware/tools/libredwg/bin. Empty when neither has it.
std::string libredwg_tool(const std::string& name);

// Run LibreDWG tool `name` with literal arguments (never a shell); output is discarded and a
// run past `timeout_s` is killed. False when the tool is missing, fails or times out.
bool run_libredwg(const std::string& name, const std::vector<std::string>& args,
                  int timeout_s = 300);

// Actionable install hint appended to every "dwg2dxf missing" import error.
extern const char* const kLibredwgInstallHint;

// Write `doc` as an AutoCAD R2000 DWG with the native encoder (see dwg_write.h for the
// entity mapping: LWPOLYLINE, ARC/CIRCLE, LINE, 3DFACE with edge visibility, SOLID, aligned
// TEXT, 3DSOLID/BODY with inline SAT; layers keep name, colour and CONTINUOUS / DASHED /
// CENTER linetype). The file is written beside `path`; when `dwg2dxf` is installed the
// staged file is re-read with it and every entity must come back, then it is renamed into
// place. Throws std::runtime_error (nothing to export, input R2000 cannot carry, write or
// re-read failure); returns the absolute path.
std::string write_dwg(const std::string& path, const DxfDocument& doc);

}  // namespace chudware::doc
