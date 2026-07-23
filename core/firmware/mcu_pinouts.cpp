#include "firmware/mcu_pinouts.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <regex>
#include <sstream>

#include "ecad/container.h"
#include "ecad/schema.h"
#include "ecad/session.h"

namespace chudware::firmware {
namespace {

// Prefix (case-insensitive substring) → firmware mcu_family. Order matters:
// first hit wins (mirrors mcu_pinouts._FAMILY_PREFIXES).
const std::vector<std::pair<std::string, std::string>> kFamilyPrefixes = {
    {"stm32f4", "stm32f4"},
    {"stm32f1", "stm32f1"},
    {"nrf52", "nrf52"},
    {"rp2040", "rp2040"},
    {"stm32", "cortex-m4"},
    {"esp32", "xtensa-lx6"},
    {"atmega", "avr"},
    {"attiny", "avr"},
};

const char* kMpnFieldKeys[] = {"MPN", "mpn", "Mpn", "MFR_PN", "MfrPN",
                               "manufacturer_part", "PartNumber"};

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

std::string upper(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
    return s;
}

std::string trim(const std::string& s) {
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return "";
    size_t e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

std::vector<std::string> component_part_strings(const nlohmann::json& comp) {
    std::vector<std::string> out;
    if (comp.contains("fields") && comp["fields"].is_object()) {
        for (const char* k : kMpnFieldKeys) {
            auto it = comp["fields"].find(k);
            if (it != comp["fields"].end() && it->is_string() && !it->get<std::string>().empty())
                out.push_back(it->get<std::string>());
        }
    }
    for (const char* k : {"value", "libid", "fpid"}) {
        auto it = comp.find(k);
        if (it != comp.end() && it->is_string() && !it->get<std::string>().empty())
            out.push_back(it->get<std::string>());
    }
    return out;
}

// Boot-strap / reset net names per family (mirrors _STRAP_PIN_NAMES).
const std::vector<std::pair<std::string, std::vector<std::string>>> kStrapPins = {
    {"cortex-m0", {"BOOT0", "BOOT1", "NRST"}},
    {"cortex-m3", {"BOOT0", "BOOT1", "NRST"}},
    {"cortex-m4", {"BOOT0", "BOOT1", "NRST"}},
    {"cortex-m7", {"BOOT0", "BOOT1", "NRST"}},
    {"stm32f4", {"BOOT0", "BOOT1", "NRST"}},
    {"stm32f1", {"BOOT0", "BOOT1", "NRST"}},
    {"xtensa-lx6", {"EN", "GPIO0", "GPIO2", "GPIO12", "GPIO15"}},
    {"avr", {"RESET", "NRESET", "PC6"}},
};

}  // namespace

std::optional<std::string> mpn_to_family(const std::string& mpn) {
    std::string s = lower(trim(mpn));
    if (s.empty()) return std::nullopt;
    for (const auto& [prefix, family] : kFamilyPrefixes) {
        if (s.find(prefix) != std::string::npos) return family;
    }
    return std::nullopt;
}

std::optional<std::string> component_family(const nlohmann::json& comp) {
    if (!comp.is_object()) return std::nullopt;
    for (const auto& s : component_part_strings(comp)) {
        if (auto fam = mpn_to_family(s)) return fam;
    }
    return std::nullopt;
}

std::vector<nlohmann::json> iter_bom_components(const nlohmann::json& doc) {
    std::vector<nlohmann::json> out;
    if (!doc.is_object()) return out;
    const nlohmann::json* items = nullptr;
    if (chudware::ecad::schema::is_board(doc) && doc.contains("footprints"))
        items = &doc["footprints"];
    else if (chudware::ecad::schema::is_schematic(doc) && doc.contains("symbols"))
        items = &doc["symbols"];
    else if (doc.contains("footprints") && doc["footprints"].is_array())
        items = &doc["footprints"];
    else if (doc.contains("symbols") && doc["symbols"].is_array())
        items = &doc["symbols"];
    if (items && items->is_array()) {
        for (const auto& c : *items) {
            if (c.is_object()) out.push_back(c);
        }
    }
    return out;
}

std::optional<std::string> select_mcu_family(const nlohmann::json& doc) {
    if (!doc.is_object()) return std::nullopt;
    for (const auto& comp : iter_bom_components(doc)) {
        if (comp.value("dnp", false)) continue;
        if (auto fam = component_family(comp)) return fam;
    }
    return std::nullopt;
}

nlohmann::json resolve_board_doc(const std::string& ref) {
    if (ref.empty()) return nlohmann::json();
    // 1) open ECAD session registry (doc_id) — ecad_session_doc throws on a miss.
    try {
        nlohmann::json doc = chudware::ecad::ecad_session_doc(ref);
        if (doc.is_object()) return doc;
    } catch (...) {
    }
    // 2) file path (.ecad+ container or plain JSON document).
    try {
        std::error_code ec;
        if (std::filesystem::exists(ref, ec)) {
            if (ref.size() >= 6 && ref.compare(ref.size() - 6, 6, ".ecad+") == 0) {
                auto opened = chudware::ecad::ecad_container_open(ref);
                if (opened.doc.is_object()) return opened.doc;
            } else {
                std::ifstream in(ref);
                std::stringstream ss;
                ss << in.rdbuf();
                nlohmann::json doc = nlohmann::json::parse(ss.str(), nullptr, false);
                if (doc.is_object()) return doc;
            }
        }
    } catch (...) {
    }
    return nlohmann::json();
}

std::vector<std::string> strap_pins(const std::string& mcu_family) {
    std::string fam = lower(trim(mcu_family));
    for (const auto& [key, pins] : kStrapPins) {
        if (key == fam) return pins;
    }
    return {};
}

bool is_strap_net(const std::string& net_name, const std::string& mcu_family) {
    if (net_name.empty()) return false;
    std::string up = upper(trim(net_name));
    for (const auto& p : strap_pins(mcu_family)) {
        if (p == up) return true;
    }
    return false;
}

bool is_input_only_pin(const std::string& net_name, const std::string& mcu_family) {
    if (net_name.empty()) return false;
    std::string fam = lower(trim(mcu_family));
    if (fam != "xtensa-lx6") return false;
    std::string up = upper(trim(net_name));
    for (int n = 34; n <= 39; ++n) {
        if (up == "GPIO" + std::to_string(n)) return true;
    }
    return false;
}

std::optional<int> resolve_pin(const std::string& net_name, const std::string& /*mcu_family*/) {
    std::string s = trim(net_name);
    if (s.empty()) return std::nullopt;
    static const std::regex kStm32Re(R"(^P([A-Z])(\d{1,2})$)", std::regex::icase);
    static const std::regex kGpioRe(R"(^(?:GPIO|IO)(\d{1,3})$)", std::regex::icase);
    static const std::regex kDpinRe(R"(^D(\d{1,3})$)", std::regex::icase);
    std::smatch m;
    if (std::regex_match(s, m, kStm32Re)) {
        char port = static_cast<char>(std::toupper(m[1].str()[0]));
        return (port - 'A') * 16 + std::stoi(m[2].str());
    }
    if (std::regex_match(s, m, kGpioRe)) return std::stoi(m[1].str());
    if (std::regex_match(s, m, kDpinRe)) return std::stoi(m[1].str());
    return std::nullopt;
}

}  // namespace chudware::firmware
