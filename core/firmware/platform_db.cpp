#include "firmware/platform_db.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <regex>
#include <vector>

#include "firmware/private_storage.h"
#include "firmware/templates.h"
#include "firmware/project.h"

namespace chudware::firmware {
namespace {

// mcu_family substring patterns → canonical family key (first match wins).
const std::vector<std::pair<std::string, std::string>> kFamilyPatterns = {
    {R"(stm32f4)", "stm32f4"},
    {R"(stm32f1)", "stm32f1"},
    {R"(nrf52)", "nrf52"},
    {R"(rp2040|pico)", "rp2040"},
    {R"(esp32)", "esp32"},
    {R"(cortex-m)", "cortex-m"},
};

// Per-family capability matrix (mirrors platform_db.PLATFORM_CAPS — including
// the internal gpio_regmap the fw_status payload drops).
nlohmann::json caps_table() {
    nlohmann::json t;
    t["stm32f4"] = {
        {"file", "stm32f4_discovery.repl"}, {"emulation", "renode"},
        {"uart", true}, {"gpio", true}, {"gpio_observe", true},
        {"adc", false}, {"i2c", true}, {"spi", true}, {"timers", true},
        {"can", true}, {"timed_bus", true}, {"electrical_bus", true},
        {"bus_models", {"stm32f4-i2c", "stm32f4-uart-ttl", "stm32f4-spi-master", "stm32f4-bxcan"}},
        {"gpio_regmap", {{"base", 0x40020000}, {"stride", 0x400}, {"odr_offset", 0x14}}},
        {"notes", "USART1/2 TTL, I2C1-3, SPI1-3 master and CAN1/2 use timed STM32F4 models. Explicit bus_links enables linear RC/lossless-line electrical sampling. Default peripheral clock is reset 16MHz HSI; RCC clock changes are not modeled. GPIO A-E observed final state; TIM1-5. No ADC model upstream."},
    };
    t["stm32f1"] = {
        {"file", "stm32f103.repl"}, {"emulation", "renode"},
        {"uart", true}, {"gpio", true}, {"gpio_observe", true},
        {"adc", false}, {"i2c", true}, {"spi", false}, {"timers", true},
        {"gpio_regmap", {{"base", 0x40010800}, {"stride", 0x400}, {"odr_offset", 0x0C}}},
        {"notes", "Upstream stm32f103.repl: USART1-5, GPIO A-G (observed), I2C1-2, TIM1-14, DMA. No ADC/SPI model."},
    };
    t["nrf52"] = {
        {"file", "nrf52840.repl"}, {"emulation", "renode"},
        {"uart", true}, {"gpio", true}, {"gpio_observe", false},
        {"adc", false}, {"i2c", true}, {"spi", true}, {"timers", true},
        {"gpio_regmap", nullptr},
        {"notes", "Upstream nrf52840.repl: UART0/1, GPIO0/1+GPIOTE, TWI0/1, SPI2, TIMER0-4/RTC. "
                  "gpio assertions unsupported (observation wiring is STM32-only)."},
    };
    t["rp2040"] = {
        {"file", "rp2040.repl"}, {"emulation", "renode-minimal"},
        {"uart", true}, {"gpio", false}, {"gpio_observe", false},
        {"adc", false}, {"i2c", false}, {"spi", false}, {"timers", false},
        {"gpio_regmap", nullptr},
        {"notes", "Minimal local platform (stock Renode has no RP2040 model): Cortex-M0+ + PL011 "
                  "UART0/1 only. Bare-metal UART works; pico-sdk stdio / GPIO / other peripherals do not."},
    };
    t["esp32"] = {
        {"file", "esp32_stub.repl"}, {"emulation", "none"},
        {"uart", false}, {"gpio", false}, {"gpio_observe", false},
        {"adc", false}, {"i2c", false}, {"spi", false}, {"timers", false},
        {"gpio_regmap", nullptr},
        {"notes", "NO EMULATION — Renode ESP32 support is experimental with no usable peripherals. "
                  "Build (idf.py) + flash (esptool) only."},
    };
    t["cortex-m"] = {
        {"file", "cortex_m_generic.repl"}, {"emulation", "renode"},
        {"uart", false}, {"gpio", false}, {"gpio_observe", false},
        {"adc", false}, {"i2c", false}, {"spi", false}, {"timers", false},
        {"gpio_regmap", nullptr},
        {"notes", "Generic fallback: CPU+NVIC+memory only; no peripherals. UART/gpio assertions "
                  "need a specific family platform and a real firmware UART driver."},
    };
    for(auto& [family,caps]:t.items()) if(family!="stm32f4") {
        caps["can"]=false;caps["timed_bus"]=false;caps["electrical_bus"]=false;
        caps["bus_models"]=nlohmann::json::array();
    }
    return t;
}

}  // namespace

std::string canonical_family(const std::string& mcu_family) {
    std::string key = mcu_family;
    std::transform(key.begin(), key.end(), key.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    for (const auto& [pattern, family] : kFamilyPatterns) {
        if (std::regex_search(key, std::regex(pattern))) return family;
    }
    return "cortex-m";
}

nlohmann::json get_capabilities(const std::string& mcu_family) {
    std::string family = canonical_family(mcu_family);
    nlohmann::json caps = caps_table()[family];
    caps["family"] = family;
    return caps;
}

nlohmann::json capability_matrix() {
    // json-safe view (drops gpio_regmap internals).
    nlohmann::json table = caps_table();
    nlohmann::json out;
    for (auto& [family, caps] : table.items()) {
        nlohmann::json view;
        for (const char* k : {"file", "emulation", "uart", "gpio", "gpio_observe",
                              "adc", "i2c", "spi", "can", "timed_bus", "electrical_bus", "bus_models", "timers", "notes"}) {
            view[k] = caps[k];
        }
        out[family] = view;
    }
    return out;
}

std::string get_platform_file(const std::string& mcu_family) {
    // Materialize the embedded .repl files to a cache dir (rewritten when the
    // contents differ) — Renode's LoadPlatformDescription needs a real path.
    namespace fs = std::filesystem;
    std::string family = canonical_family(mcu_family);
    std::string want = caps_table()[family]["file"].get<std::string>();
    const fs::path dir = private_directory(firmware_root() / "platforms");
    for (const auto& tf : renode_platform_files()) {
        const fs::path out = dir / tf.relpath;
        std::string existing;
        if (!private_storage_detail::read_file(out, existing, 1024 * 1024) || existing != tf.contents)
            private_storage_detail::atomic_write(out, tf.contents);
    }
    return (dir / want).string();
}

}  // namespace chudware::firmware
