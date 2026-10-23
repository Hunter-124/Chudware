// Embedded firmware project templates (engine_oracle/firmware/templates port).
//
// The Python engine ships the cmake_arm template tree on disk next to the
// module; the C++ core embeds the same files (templates_embedded.cpp, generated
// from the Python tree when the module was ported) so libchudware has no runtime data
// path to resolve.
#pragma once

#include <string>
#include <vector>

namespace chudware::firmware {

struct TemplateFile {
    std::string relpath;  // POSIX-style path relative to the project root
    std::string contents;
};

// The cmake_arm template tree (CMakeLists.txt, toolchain file, linker script,
// src/, include/) — the seed for every new firmware project regardless of
// toolchain (engine_oracle/firmware/project.py::_TEMPLATE_FOR maps all three
// toolchains onto cmake_arm today).
const std::vector<TemplateFile>& cmake_arm_template_files();

// The bundled Renode platform descriptions (templates/renode_platforms —
// stm32f4_discovery, stm32f103, nrf52840, rp2040, esp32_stub,
// cortex_m_generic + LICENSE). platform_db materializes them to a cache dir
// so `machine LoadPlatformDescription @<file>` gets a real path.
const std::vector<TemplateFile>& renode_platform_files();

}  // namespace chudware::firmware
