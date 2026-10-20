// Standalone production SAT tokenizer regressions; no OCCT, daemon, or geometry.
#include <array>
#include <charconv>
#include <cctype>
#include <cstdint>
#include <iostream>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace chudware::doc {
#include "../doc/acis_tokens.inc"
}

namespace {
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
void regressions() {
    using namespace chudware::doc;
    const std::string prefix = "700 0 1 0\n1 1e-6 1e-10\nbody ";
    for (const std::string& token : {"@18446744073709551594 x", "@184467440737095516160 x",
                                   "@99 x", "@1x", "@999999999999999999999999 x"}) {
        bool refused = false;
        try {
            // NativeInputs #2's tiny nonadvancing-loop fixture now refuses outright.
            (void)parse_sat(prefix + token + " #\n");
        } catch (const std::runtime_error& error) {
            refused = true;
            require(std::string(error.what()).find("SAT counted string") != std::string::npos,
                    "malformed counted length not diagnosed at parsing");
        }
        require(refused, "malformed SAT counted length accepted");
    }
    for (const std::string& value : {"", "x", "x # y", "#", "line\nbreak", "@18446744073709551594 "}) {
        const auto model = parse_sat(prefix + "@" + std::to_string(value.size()) + " " + value + " #\n");
        require(model.recs.size() == 1, "valid counted string split records");
        require(model.recs[0].t.size() == 1, "valid counted string split tokens");
        require(model.recs[0].t[0].k == TK::Str && model.recs[0].t[0].s == value,
                "counted string changed its validated payload");
    }
    // The count can consume the last byte without ever stepping past the buffer.
    bool missing_record_end = false;
    try {
        (void)parse_sat(prefix + "@1 x");
    } catch (const std::runtime_error& error) {
        missing_record_end = std::string(error.what()).find("is not terminated by '#'") != std::string::npos;
    }
    require(missing_record_end, "exact-end string did not reach bounded record-end validation");
}
}  // namespace

int main() {
    try {
        regressions();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
