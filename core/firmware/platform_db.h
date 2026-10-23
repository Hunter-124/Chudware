// MCU family → Renode platform file lookup + capability matrix (C++ port of
// engine_oracle/firmware/platform_db.py). The .repl files ship embedded
// (firmware/templates_embedded.cpp); get_platform_file materializes them to a
// cache dir so Renode's `LoadPlatformDescription @<file>` gets a real path.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

// Map a free-form mcu_family string to a canonical capability key
// (stm32f4/stm32f1/nrf52/rp2040/esp32/cortex-m — unknown → cortex-m).
std::string canonical_family(const std::string& mcu_family);

// The capability entry for mcu_family (PLATFORM_CAPS keys + "family" +
// "gpio_regmap" — json null when the family has no ODR map). Never null.
nlohmann::json get_capabilities(const std::string& mcu_family);

// Full queryable matrix: {family: {file, emulation, uart, gpio, gpio_observe,
// adc, i2c, spi, timers, notes}} — the fw_status "platforms" payload.
nlohmann::json capability_matrix();

// Absolute path to the bundled .repl for mcu_family (materialized from the
// embedded copy on first use), or "" when unavailable. Falls back to the
// generic Cortex-M platform exactly like the Python module.
std::string get_platform_file(const std::string& mcu_family);

}  // namespace chudware::firmware
