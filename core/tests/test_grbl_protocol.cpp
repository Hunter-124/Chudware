#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "device/grbl_protocol.h"
#include "device/numeric_parse.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <fstream>
#include <locale>
#include <string>
#include <string_view>
#include <vector>

#ifndef CHUDWARE_SOURCE_DIR
#define CHUDWARE_SOURCE_DIR "."
#endif

namespace {
using namespace chudware::device;

class CommaDecimalPoint final : public std::numpunct<char> {
 protected:
  char do_decimal_point() const override { return ','; }
};

class ScopedGlobalLocale final {
 public:
  explicit ScopedGlobalLocale(const std::locale& locale)
      : previous_(std::locale()) {
    std::locale::global(locale);
  }

  ~ScopedGlobalLocale() { std::locale::global(previous_); }

  ScopedGlobalLocale(const ScopedGlobalLocale&) = delete;
  ScopedGlobalLocale& operator=(const ScopedGlobalLocale&) = delete;

 private:
  std::locale previous_;
};

TEST_CASE("finite double parsing is exact and independent of the process locale") {
  const ScopedGlobalLocale comma_locale{
      std::locale(std::locale::classic(), new CommaDecimalPoint)};

  double value = 0.0;
  REQUIRE(parseFiniteDouble("12.5", value));
  CHECK(value == doctest::Approx(12.5));
  CHECK_FALSE(parseFiniteDouble(" 12.5", value));
  CHECK_FALSE(parseFiniteDouble("12.5junk", value));
  CHECK_FALSE(parseFiniteDouble("+12.5", value));
  CHECK_FALSE(parseFiniteDouble("12,5", value));

  const std::string_view fields = "12.5e2,9";
  REQUIRE(parseFiniteDouble(fields.substr(0, 4), value));
  CHECK(value == doctest::Approx(12.5));
  std::size_t consumed = 0;
  REQUIRE(parseFiniteDoublePrefix(fields, value, consumed));
  CHECK(value == doctest::Approx(1250.0));
  CHECK(consumed == 6);
}

std::vector<std::string> readLines(std::string_view relative_path) {
  const std::string path = std::string(CHUDWARE_SOURCE_DIR) + "/" + std::string(relative_path);
  std::ifstream input(path);
  REQUIRE_MESSAGE(input.is_open(), "unable to open fixture: ", path);
  std::vector<std::string> lines;
  for (std::string line; std::getline(input, line);) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    lines.push_back(line);
  }
  return lines;
}

TEST_CASE("all Grbl machine states and substates map onto canonical DeviceState strings") {
  struct Case {
    std::string_view vendor;
    GrblState state;
    std::string_view canonical;
    int substate;
  };
  constexpr std::array cases{
      Case{"Idle", GrblState::idle, "idle", -1},
      Case{"Run", GrblState::run, "running", -1},
      Case{"Hold:0", GrblState::hold, "hold", 0},
      Case{"Hold:1", GrblState::hold, "hold", 1},
      Case{"Jog", GrblState::jog, "running", -1},
      Case{"Alarm", GrblState::alarm, "alarm", -1},
      Case{"Door:0", GrblState::door, "door", 0},
      Case{"Door:1", GrblState::door, "door", 1},
      Case{"Door:2", GrblState::door, "door", 2},
      Case{"Door:3", GrblState::door, "door", 3},
      Case{"Check", GrblState::check, "idle", -1},
      Case{"Home", GrblState::home, "homing", -1},
      Case{"Sleep", GrblState::sleep, "idle", -1},
  };

  for (const auto& test : cases) {
    const std::string frame = "<" + std::string(test.vendor) + "|MPos:0,0,0>";
    const auto result = parseStatusReport(frame);
    CAPTURE(frame);
    CAPTURE(result.reason);
    REQUIRE(result);
    CHECK(result.value.state == test.state);
    CHECK(canonicalDeviceState(result.value.state) == test.canonical);
    if (test.substate < 0) CHECK_FALSE(result.value.substate.has_value());
    else REQUIRE(result.value.substate.value() == test.substate);
    CHECK(result.raw == frame);
    CHECK(result.value.raw == frame);
  }
}

TEST_CASE("unordered status golden covers every optional Grbl 1.1 field") {
  const auto lines = readLines("fixtures/mfg/gateway/grbl/unordered_full_status.txt");
  REQUIRE(lines.size() == 1);
  const auto parsed = parseStatusReport(lines.front());
  CAPTURE(parsed.reason);
  REQUIRE(parsed);
  const auto& status = parsed.value;
  CHECK(status.state == GrblState::run);
  REQUIRE(status.machine_position);
  CHECK(status.machine_position->x == doctest::Approx(10.0));
  CHECK(status.machine_position->y == doctest::Approx(20.0));
  CHECK(status.machine_position->z == doctest::Approx(-5.0));
  CHECK_FALSE(status.work_position);
  REQUIRE(status.work_coordinate_offset);
  CHECK(status.work_coordinate_offset->x == doctest::Approx(1.25));
  CHECK(status.work_coordinate_offset->y == doctest::Approx(-2.5));
  CHECK(status.work_coordinate_offset->z == doctest::Approx(3.75));
  REQUIRE(status.buffer);
  CHECK(status.buffer->planner_available == 12);
  CHECK(status.buffer->rx_available == 87);
  REQUIRE(status.rates);
  CHECK(status.rates->feed == doctest::Approx(600.5));
  REQUIRE(status.rates->spindle_rpm);
  CHECK(*status.rates->spindle_rpm == doctest::Approx(18000));
  REQUIRE(status.overrides);
  CHECK(status.overrides->feed_percent == 95);
  CHECK(status.overrides->rapid_percent == 50);
  CHECK(status.overrides->spindle_percent == 110);
  REQUIRE(status.pins);
  CHECK(status.pins->x_limit);
  CHECK_FALSE(status.pins->y_limit);
  CHECK(status.pins->z_limit);
  CHECK(status.pins->probe);
  CHECK(status.pins->door);
  CHECK_FALSE(status.pins->hold);
  CHECK_FALSE(status.pins->soft_reset);
  CHECK_FALSE(status.pins->cycle_start);
  REQUIRE(status.accessories);
  CHECK_FALSE(status.accessories->spindle_cw);
  CHECK_FALSE(status.accessories->spindle_ccw);
  CHECK(status.accessories->flood);
  CHECK(status.accessories->mist);
  REQUIRE(status.line_number);
  CHECK(*status.line_number == 429496);
}

TEST_CASE("work position and feed-only forms are supported independently") {
  const std::string line = "<Idle|F:12.75|WPos:-1.25,2,3>";
  const auto parsed = parseStatusReport(line);
  CAPTURE(parsed.reason);
  REQUIRE(parsed);
  REQUIRE(parsed.value.work_position);
  CHECK(parsed.value.work_position->x == doctest::Approx(-1.25));
  CHECK_FALSE(parsed.value.machine_position);
  REQUIRE(parsed.value.rates);
  CHECK(parsed.value.rates->feed == doctest::Approx(12.75));
  CHECK_FALSE(parsed.value.rates->spindle_rpm);
  CHECK_FALSE(parsed.value.work_coordinate_offset);
  CHECK_FALSE(parsed.value.buffer);
  CHECK_FALSE(parsed.value.overrides);
  CHECK_FALSE(parsed.value.pins);
  CHECK_FALSE(parsed.value.accessories);
  CHECK_FALSE(parsed.value.line_number);
}

TEST_CASE("all documented pin and accessory codes parse in arbitrary order") {
  const auto pins = parseStatusReport("<Idle|Pn:SRHY|MPos:0,0,0>");
  CAPTURE(pins.reason);
  REQUIRE(pins);
  REQUIRE(pins.value.pins);
  CHECK_FALSE(pins.value.pins->x_limit);
  CHECK(pins.value.pins->y_limit);
  CHECK_FALSE(pins.value.pins->z_limit);
  CHECK_FALSE(pins.value.pins->probe);
  CHECK_FALSE(pins.value.pins->door);
  CHECK(pins.value.pins->hold);
  CHECK(pins.value.pins->soft_reset);
  CHECK(pins.value.pins->cycle_start);

  const auto clockwise = parseStatusReport("<Run|A:S|MPos:0,0,0>");
  CAPTURE(clockwise.reason);
  REQUIRE(clockwise);
  REQUIRE(clockwise.value.accessories);
  CHECK(clockwise.value.accessories->spindle_cw);
  CHECK_FALSE(clockwise.value.accessories->spindle_ccw);

  const auto counterclockwise = parseStatusReport("<Run|MPos:0,0,0|A:C>");
  CAPTURE(counterclockwise.reason);
  REQUIRE(counterclockwise);
  REQUIRE(counterclockwise.value.accessories);
  CHECK_FALSE(counterclockwise.value.accessories->spindle_cw);
  CHECK(counterclockwise.value.accessories->spindle_ccw);
}

TEST_CASE("all response and alarm codes use the Grbl 1.1 tables") {
  constexpr std::array error_codes{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
                                   20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34,
                                   35, 36, 37, 38};
  for (int code : error_codes) {
    const std::string line = "error:" + std::to_string(code);
    const auto parsed = parsePushMessage(line);
    CAPTURE(line);
    CAPTURE(parsed.reason);
    REQUIRE(parsed);
    CHECK(parsed.value.kind == PushKind::error);
    CHECK(parsed.value.code == code);
    CHECK_FALSE(parsed.value.text.empty());
    CHECK(parsed.value.text == errorText(code));
    CHECK(parsed.value.raw == line);
  }

  for (int code = 1; code <= 10; ++code) {
    const std::string line = "ALARM:" + std::to_string(code);
    const auto parsed = parsePushMessage(line);
    CAPTURE(line);
    CAPTURE(parsed.reason);
    REQUIRE(parsed);
    CHECK(parsed.value.kind == PushKind::alarm);
    CHECK(parsed.value.code == code);
    CHECK_FALSE(parsed.value.text.empty());
    CHECK(parsed.value.text == alarmText(code));
  }
  CHECK(classifyAlarm(1) == AlarmClass::position_lost);
  CHECK(classifyAlarm(3) == AlarmClass::position_lost);
  CHECK(classifyAlarm(4) == AlarmClass::probe_fail);
  CHECK(classifyAlarm(5) == AlarmClass::probe_fail);
  CHECK(classifyAlarm(2) == AlarmClass::soft_limit);
  CHECK(classifyAlarm(8) == AlarmClass::limit);
  CHECK(classifyAlarm(9) == AlarmClass::limit);
  CHECK(classifyAlarm(10) == AlarmClass::limit);
  CHECK(classifyAlarm(6) == AlarmClass::other);
}

TEST_CASE("queried feedback and parameter maps are typed") {
  const auto ok = parsePushMessage("ok");
  REQUIRE(ok);
  CHECK(ok.value.kind == PushKind::acknowledgment);
  CHECK(ok.value.text.find("not implied") != std::string_view::npos);

  const auto gc = parsePushMessage("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]");
  REQUIRE(gc);
  CHECK(gc.value.kind == PushKind::gcode_state);
  CHECK(gc.value.text.starts_with("G0 G54"));

  const auto msg = parsePushMessage("[MSG:Check Limits]");
  REQUIRE(msg);
  CHECK(msg.value.kind == PushKind::message);
  CHECK(msg.value.text == "Check Limits");

  const auto version = parsePushMessage("[VER:v1.1f.20170131:Shapeoko]");
  REQUIRE(version);
  CHECK(version.value.kind == PushKind::version);
  CHECK(version.value.text == "v1.1f.20170131:Shapeoko");

  const auto options = parsePushMessage("[OPT:VL+,15,128]");
  REQUIRE(options);
  CHECK(options.value.kind == PushKind::options);
  CHECK(options.value.options.flags == "VL+");
  CHECK(options.value.options.planner_blocks == 15);
  CHECK(options.value.options.rx_buffer_bytes == 128);

  constexpr std::array coordinate_tags{"G54", "G55", "G56", "G57", "G58", "G59", "G28", "G30", "G92"};
  for (std::string_view tag : coordinate_tags) {
    const std::string line = "[" + std::string(tag) + ":1.25,-2,3.5]";
    const auto coordinate = parsePushMessage(line);
    CAPTURE(line);
    CAPTURE(coordinate.reason);
    REQUIRE(coordinate);
    CHECK(coordinate.value.kind == PushKind::coordinate_offset);
    CHECK(coordinate.value.tag == tag);
    CHECK(coordinate.value.coordinates == Vec3{1.25, -2.0, 3.5});
  }

  const auto failed_probe = parsePushMessage("[PRB:4.5,5.5,6.5:0]");
  REQUIRE(failed_probe);
  CHECK(failed_probe.value.kind == PushKind::probe);
  CHECK_FALSE(failed_probe.value.probe_success);
  CHECK(failed_probe.value.coordinates == Vec3{4.5, 5.5, 6.5});

  const auto successful_probe = parsePushMessage("[PRB:4.5,5.5,6.5:1]");
  REQUIRE(successful_probe);
  CHECK(successful_probe.value.probe_success);

  const auto tlo = parsePushMessage("[TLO:-1.125]");
  REQUIRE(tlo);
  CHECK(tlo.value.kind == PushKind::tool_length_offset);
  CHECK(tlo.value.tool_length_offset == doctest::Approx(-1.125));

  const auto help = parsePushMessage("[HLP:$$ $# $G $I $N]");
  REQUIRE(help);
  CHECK(help.value.kind == PushKind::help);
  const auto echo = parsePushMessage("[echo:G1X1Y2]");
  REQUIRE(echo);
  CHECK(echo.value.kind == PushKind::echo);
  const auto startup = parsePushMessage(">G54G20:ok");
  REQUIRE(startup);
  CHECK(startup.value.kind == PushKind::startup);
  CHECK(startup.value.text == "G54G20");
}

TEST_CASE("every Grbl 1.1 dollar setting id has metadata and parses") {
  constexpr std::array ids{0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 20, 21, 22, 23, 24, 25,
                           26, 27, 30, 31, 32, 100, 101, 102, 110, 111, 112, 120, 121, 122,
                           130, 131, 132};
  for (int id : ids) {
    const auto info = settingInfo(id);
    CAPTURE(id);
    REQUIRE(info);
    CHECK(info->id == id);
    CHECK_FALSE(info->name.empty());
    CHECK_FALSE(info->unit.empty());
    const std::string line = "$" + std::to_string(id) + "=12.5";
    const auto parsed = parsePushMessage(line);
    CAPTURE(parsed.reason);
    REQUIRE(parsed);
    CHECK(parsed.value.kind == PushKind::setting);
    CHECK(parsed.value.setting.info.id == id);
    CHECK(parsed.value.setting.value == doctest::Approx(12.5));
    CHECK(parsed.raw == line);
  }
  CHECK_FALSE(settingInfo(7));
}

TEST_CASE("welcome to status to ok transcript replays from golden fixture") {
  const auto lines = readLines("fixtures/mfg/gateway/grbl/welcome_status_ok.txt");
  REQUIRE(lines.size() == 3);

  const auto welcome = parsePushMessage(lines[0]);
  CAPTURE(welcome.reason);
  REQUIRE(welcome);
  CHECK(welcome.value.kind == PushKind::welcome);
  CHECK(welcome.value.text == "1.1h");
  CHECK(welcome.value.raw == lines[0]);

  const auto status = parseStatusReport(lines[1]);
  CAPTURE(status.reason);
  REQUIRE(status);
  CHECK(status.value.state == GrblState::idle);
  REQUIRE(status.value.machine_position);
  REQUIRE(status.value.rates);
  REQUIRE(status.value.work_coordinate_offset);
  REQUIRE(status.value.overrides);
  CHECK(status.value.raw == lines[1]);

  const auto acknowledged = parsePushMessage(lines[2]);
  REQUIRE(acknowledged);
  CHECK(acknowledged.value.kind == PushKind::acknowledgment);
}

TEST_CASE("realtime commands encode to the Grbl 1.1 byte assignments") {
  constexpr std::array expected{
      std::pair{RealtimeCmd::status_report, std::uint8_t{'?'}},
      std::pair{RealtimeCmd::cycle_start, std::uint8_t{'~'}},
      std::pair{RealtimeCmd::feed_hold, std::uint8_t{'!'}},
      std::pair{RealtimeCmd::soft_reset, std::uint8_t{0x18}},
      std::pair{RealtimeCmd::safety_door, std::uint8_t{0x84}},
      std::pair{RealtimeCmd::jog_cancel, std::uint8_t{0x85}},
      std::pair{RealtimeCmd::feed_override_reset, std::uint8_t{0x90}},
      std::pair{RealtimeCmd::feed_override_plus_10, std::uint8_t{0x91}},
      std::pair{RealtimeCmd::feed_override_minus_10, std::uint8_t{0x92}},
      std::pair{RealtimeCmd::feed_override_plus_1, std::uint8_t{0x93}},
      std::pair{RealtimeCmd::feed_override_minus_1, std::uint8_t{0x94}},
      std::pair{RealtimeCmd::rapid_override_reset, std::uint8_t{0x95}},
      std::pair{RealtimeCmd::rapid_override_50, std::uint8_t{0x96}},
      std::pair{RealtimeCmd::rapid_override_25, std::uint8_t{0x97}},
      std::pair{RealtimeCmd::spindle_override_reset, std::uint8_t{0x99}},
      std::pair{RealtimeCmd::spindle_override_plus_10, std::uint8_t{0x9A}},
      std::pair{RealtimeCmd::spindle_override_minus_10, std::uint8_t{0x9B}},
      std::pair{RealtimeCmd::spindle_override_plus_1, std::uint8_t{0x9C}},
      std::pair{RealtimeCmd::spindle_override_minus_1, std::uint8_t{0x9D}},
      std::pair{RealtimeCmd::spindle_stop_toggle, std::uint8_t{0x9E}},
      std::pair{RealtimeCmd::flood_coolant_toggle, std::uint8_t{0xA0}},
      std::pair{RealtimeCmd::mist_coolant_toggle, std::uint8_t{0xA1}},
  };
  for (const auto& [command, byte] : expected) CHECK(encodeRealtime(command) == byte);
}

TEST_CASE("line encoding uses caller storage and refuses truncation or embedded terminators") {
  std::array<char, 16> storage{};
  LineBuffer buffer{storage.data(), storage.size(), 0};
  const auto encoded = encodeLine("G1 X1", buffer);
  REQUIRE(encoded);
  CHECK(encoded.line == std::string_view("G1 X1\r", 6));
  CHECK(buffer.size == 6);

  CHECK_FALSE(encodeLine("", buffer));
  CHECK_FALSE(encodeLine("G0X0\nG0X1", buffer));
  std::array<char, 3> small_storage{};
  LineBuffer small{small_storage.data(), small_storage.size(), 0};
  const auto too_large = encodeLine("G0X0", small);
  CHECK_FALSE(too_large);
  CHECK_FALSE(too_large.reason.empty());
  CHECK(small.size == 0);
}

TEST_CASE("character counting fills the RX window and accounts a long program by FIFO response") {
  GrblRxAccounting accounting;
  const auto ok = parsePushMessage("ok");
  REQUIRE(ok);
  std::size_t maximum_outstanding_lines = 0;

  for (std::size_t line = 0; line < 5000; ++line) {
    const std::size_t encoded_length = 8 + (line % 31);
    while (!accounting.canSend(encoded_length)) {
      REQUIRE(accounting.acknowledge(ok.value));
    }
    REQUIRE(accounting.accountSent(encoded_length));
    CHECK(accounting.outstandingCharacters() <= accounting.capacity());
    maximum_outstanding_lines = std::max(maximum_outstanding_lines, accounting.outstandingLines());
  }
  CHECK(maximum_outstanding_lines > 1);
  CHECK(accounting.sentLines() == 5000);

  while (accounting.outstandingLines() != 0) REQUIRE(accounting.acknowledge(ok.value));
  CHECK(accounting.outstandingCharacters() == 0);
  CHECK(accounting.acknowledgedLines() == 5000);
  CHECK_FALSE(accounting.acknowledge(ok.value));
  CHECK_FALSE(accounting.accountSent(129));
}

TEST_CASE("only ok and error release character-counted RX capacity") {
  GrblRxAccounting accounting;
  REQUIRE(accounting.accountSent(40));
  REQUIRE(accounting.accountSent(50));
  CHECK(accounting.outstandingCharacters() == 90);

  const auto status = parseStatusReport("<Run|MPos:0,0,0|Bf:15,128>");
  REQUIRE(status);
  const auto alarm = parsePushMessage("ALARM:1");
  REQUIRE(alarm);
  CHECK_FALSE(accounting.acknowledge(alarm.value));
  CHECK(accounting.outstandingCharacters() == 90);

  const auto error = parsePushMessage("error:20");
  REQUIRE(error);
  REQUIRE(accounting.acknowledge(error.value));
  CHECK(accounting.outstandingCharacters() == 50);
  const auto ok = parsePushMessage("ok");
  REQUIRE(ok);
  REQUIRE(accounting.acknowledge(ok.value));
  CHECK(accounting.outstandingCharacters() == 0);
}

TEST_CASE("malformed and unknown lines fail explicitly without partial state") {
  constexpr std::array malformed_status{
      "", "Idle|MPos:0,0,0", "<Idle>", "<Mystery|MPos:0,0,0>",
      "<Hold|MPos:0,0,0>", "<Hold:2|MPos:0,0,0>", "<Door:4|MPos:0,0,0>",
      "<Run:0|MPos:0,0,0>", "<Idle|MPos:0,0>", "<Idle|MPos:nan,0,0>",
      "<Idle|MPos:0,0,0|WPos:0,0,0>", "<Idle|WCO:0,0,0>",
      "<Idle|MPos:0,0,0|Bf:1>", "<Idle|MPos:0,0,0|F:-1>",
      "<Idle|MPos:0,0,0|FS:1,-1>", "<Idle|MPos:0,0,0|Ov:100,100>",
      "<Idle|MPos:0,0,0|Pn:Q>", "<Idle|MPos:0,0,0|Pn:XX>",
      "<Idle|MPos:0,0,0|A:SC>", "<Idle|MPos:0,0,0|Bogus:1>",
      "<Idle|MPos:0,0,0|MPos:1,1,1>", "<Idle|MPos:0,0,0|>",
  };
  for (std::string_view line : malformed_status) {
    const auto parsed = parseStatusReport(line);
    CAPTURE(line);
    CHECK_FALSE(parsed);
    CHECK_FALSE(parsed.reason.empty());
    CHECK(parsed.raw == line);
    CHECK(parsed.value.raw.empty());
  }

  constexpr std::array malformed_push{
      "", "OK", "error:", "error:18", "error:99", "ALARM:0", "ALARM:11",
      "Grbl 0.9j ['$' for help]", "Grbl 1.1h broken", "$7=1", "$10=nope",
      "[GC:]", "[OPT:V,0,128]", "[OPT:Q,15,128]", "[G54:1,2]",
      "[PRB:1,2,3:2]", "[TLO:nan]", "[UNKNOWN:value]", "not grbl",
  };
  for (std::string_view line : malformed_push) {
    const auto parsed = parsePushMessage(line);
    CAPTURE(line);
    CHECK_FALSE(parsed);
    CHECK_FALSE(parsed.reason.empty());
    CHECK(parsed.raw == line);
    CHECK(parsed.value.raw.empty());
  }
}

}  // namespace
