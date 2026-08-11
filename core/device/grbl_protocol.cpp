#include "device/grbl_protocol.h"

#include <charconv>
#include <cmath>
#include <limits>
#include <locale>
#include <sstream>

namespace chudware::device {
namespace {

template <typename T>
ParseResult<T> failure(std::string_view raw, std::string_view reason) noexcept {
  ParseResult<T> result;
  result.raw = raw;
  result.reason = reason;
  return result;
}

template <typename T>
ParseResult<T> success(std::string_view raw, T value) noexcept {
  ParseResult<T> result;
  result.ok = true;
  result.value = value;
  result.raw = raw;
  return result;
}

bool parseUnsigned(std::string_view text, std::uint32_t& value) noexcept {
  if (text.empty()) return false;
  const char* begin = text.data();
  const char* end = begin + text.size();
  const auto parsed = std::from_chars(begin, end, value);
  return parsed.ec == std::errc{} && parsed.ptr == end;
}

bool parseInt(std::string_view text, int& value) noexcept {
  if (text.empty()) return false;
  const char* begin = text.data();
  const char* end = begin + text.size();
  const auto parsed = std::from_chars(begin, end, value);
  return parsed.ec == std::errc{} && parsed.ptr == end;
}

bool parseDouble(std::string_view text, double& value) noexcept {
  if (text.empty() || text.front() == '+') return false;

  // Apple libc++ still lacks floating-point from_chars on some toolchains.
  // The classic locale fixes '.' as the decimal separator, while noskipws and
  // the EOF check retain from_chars' leading-whitespace and trailing-junk rules.
  std::istringstream input{std::string(text)};
  input.imbue(std::locale::classic());
  input >> std::noskipws >> value;
  return input && input.peek() == std::char_traits<char>::eof() && std::isfinite(value);
}

bool split2(std::string_view text, char delimiter, std::string_view& first,
            std::string_view& second) noexcept {
  const auto at = text.find(delimiter);
  if (at == std::string_view::npos || text.find(delimiter, at + 1) != std::string_view::npos) {
    return false;
  }
  first = text.substr(0, at);
  second = text.substr(at + 1);
  return true;
}

bool split3(std::string_view text, char delimiter, std::string_view& first,
            std::string_view& second, std::string_view& third) noexcept {
  const auto one = text.find(delimiter);
  if (one == std::string_view::npos) return false;
  const auto two = text.find(delimiter, one + 1);
  if (two == std::string_view::npos || text.find(delimiter, two + 1) != std::string_view::npos) {
    return false;
  }
  first = text.substr(0, one);
  second = text.substr(one + 1, two - one - 1);
  third = text.substr(two + 1);
  return true;
}

bool parseVec3(std::string_view text, Vec3& value) noexcept {
  std::string_view x;
  std::string_view y;
  std::string_view z;
  return split3(text, ',', x, y, z) && parseDouble(x, value.x) &&
         parseDouble(y, value.y) && parseDouble(z, value.z);
}

bool validOptionFlags(std::string_view flags) noexcept {
  constexpr std::string_view valid = "VNMCPZHTD0SRA+*$#IEWL";
  std::uint32_t seen_low = 0;
  std::uint32_t seen_high = 0;
  for (char flag : flags) {
    const auto at = valid.find(flag);
    if (at == std::string_view::npos) return false;
    std::uint32_t& seen = at < 16 ? seen_low : seen_high;
    const auto bit = std::uint32_t{1} << (at < 16 ? at : at - 16);
    if ((seen & bit) != 0) return false;
    seen |= bit;
  }
  return true;
}

bool isCoordinateTag(std::string_view tag) noexcept {
  return tag == "G54" || tag == "G55" || tag == "G56" || tag == "G57" ||
         tag == "G58" || tag == "G59" || tag == "G28" || tag == "G30" ||
         tag == "G92";
}

bool isKnownError(int code) noexcept { return !errorText(code).empty(); }
bool isKnownAlarm(int code) noexcept { return !alarmText(code).empty(); }

}  // namespace

std::string_view canonicalDeviceState(GrblState state) noexcept {
  for (const auto& mapping : kGrblStateMappings) {
    if (mapping.grbl == state) return mapping.canonical_device_state;
  }
  return "unknown";
}

ParseResult<GrblStatus> parseStatusReport(std::string_view line) noexcept {
  if (line.size() < 3 || line.front() != '<' || line.back() != '>') {
    return failure<GrblStatus>(line, "status report must be enclosed by '<' and '>'");
  }
  const auto body = line.substr(1, line.size() - 2);
  if (body.empty()) return failure<GrblStatus>(line, "status report has no machine state");

  GrblStatus status;
  status.raw = line;
  const auto first_pipe = body.find('|');
  const auto state_field = body.substr(0, first_pipe);
  std::string_view state_name = state_field;
  std::string_view substate_text;
  const auto state_colon = state_field.find(':');
  if (state_colon != std::string_view::npos) {
    state_name = state_field.substr(0, state_colon);
    substate_text = state_field.substr(state_colon + 1);
    if (substate_text.empty() || substate_text.find(':') != std::string_view::npos) {
      return failure<GrblStatus>(line, "invalid machine substate");
    }
  }

  if (state_name == "Idle") status.state = GrblState::idle;
  else if (state_name == "Run") status.state = GrblState::run;
  else if (state_name == "Hold") status.state = GrblState::hold;
  else if (state_name == "Jog") status.state = GrblState::jog;
  else if (state_name == "Alarm") status.state = GrblState::alarm;
  else if (state_name == "Door") status.state = GrblState::door;
  else if (state_name == "Check") status.state = GrblState::check;
  else if (state_name == "Home") status.state = GrblState::home;
  else if (state_name == "Sleep") status.state = GrblState::sleep;
  else return failure<GrblStatus>(line, "unknown Grbl machine state");

  const bool needs_substate = status.state == GrblState::hold || status.state == GrblState::door;
  if (needs_substate != !substate_text.empty()) {
    return failure<GrblStatus>(line, needs_substate ? "Hold and Door require a substate"
                                                   : "machine state does not allow a substate");
  }
  if (needs_substate) {
    std::uint32_t parsed_substate = 0;
    const std::uint32_t maximum = status.state == GrblState::hold ? 1U : 3U;
    if (!parseUnsigned(substate_text, parsed_substate) || parsed_substate > maximum) {
      return failure<GrblStatus>(line, "machine substate is outside the Grbl 1.1 range");
    }
    status.substate = static_cast<std::uint8_t>(parsed_substate);
  }

  if (first_pipe == std::string_view::npos) {
    return failure<GrblStatus>(line, "status report is missing its current position");
  }

  bool saw_mpos = false;
  bool saw_wpos = false;
  bool saw_wco = false;
  bool saw_buffer = false;
  bool saw_rates = false;
  bool saw_overrides = false;
  bool saw_pins = false;
  bool saw_accessories = false;
  bool saw_line = false;

  std::size_t offset = first_pipe + 1;
  while (offset <= body.size()) {
    const auto next = body.find('|', offset);
    const auto field = body.substr(offset, next == std::string_view::npos ? body.size() - offset
                                                                         : next - offset);
    if (field.empty()) return failure<GrblStatus>(line, "status report contains an empty field");
    const auto colon = field.find(':');
    if (colon == std::string_view::npos || colon == 0 || colon + 1 == field.size()) {
      return failure<GrblStatus>(line, "status field must have a non-empty type and value");
    }
    const auto key = field.substr(0, colon);
    const auto payload = field.substr(colon + 1);

    if (key == "MPos" || key == "WPos" || key == "WCO") {
      bool* seen = key == "MPos" ? &saw_mpos : key == "WPos" ? &saw_wpos : &saw_wco;
      if (*seen) return failure<GrblStatus>(line, "status report contains a duplicate field");
      Vec3 position;
      if (!parseVec3(payload, position)) {
        return failure<GrblStatus>(line, "position field must contain exactly three finite numbers");
      }
      *seen = true;
      if (key == "MPos") status.machine_position = position;
      else if (key == "WPos") status.work_position = position;
      else status.work_coordinate_offset = position;
    } else if (key == "Bf") {
      if (saw_buffer) return failure<GrblStatus>(line, "status report contains a duplicate Bf field");
      std::string_view planner;
      std::string_view rx;
      GrblBufferState buffer;
      if (!split2(payload, ',', planner, rx) || !parseUnsigned(planner, buffer.planner_available) ||
          !parseUnsigned(rx, buffer.rx_available)) {
        return failure<GrblStatus>(line, "Bf must contain planner and RX available counts");
      }
      saw_buffer = true;
      status.buffer = buffer;
    } else if (key == "Ln") {
      if (saw_line) return failure<GrblStatus>(line, "status report contains a duplicate Ln field");
      std::uint32_t line_number = 0;
      if (!parseUnsigned(payload, line_number)) {
        return failure<GrblStatus>(line, "Ln must be an unsigned integer");
      }
      saw_line = true;
      status.line_number = line_number;
    } else if (key == "F" || key == "FS") {
      if (saw_rates) return failure<GrblStatus>(line, "status report contains duplicate rate fields");
      GrblRates rates;
      if (key == "F") {
        if (!parseDouble(payload, rates.feed) || rates.feed < 0.0) {
          return failure<GrblStatus>(line, "F must be a non-negative finite number");
        }
      } else {
        std::string_view feed;
        std::string_view spindle;
        double spindle_rpm = 0.0;
        if (!split2(payload, ',', feed, spindle) || !parseDouble(feed, rates.feed) ||
            !parseDouble(spindle, spindle_rpm) || rates.feed < 0.0 || spindle_rpm < 0.0) {
          return failure<GrblStatus>(line, "FS must contain non-negative feed and spindle rates");
        }
        rates.spindle_rpm = spindle_rpm;
      }
      saw_rates = true;
      status.rates = rates;
    } else if (key == "Ov") {
      if (saw_overrides) return failure<GrblStatus>(line, "status report contains a duplicate Ov field");
      std::string_view feed;
      std::string_view rapid;
      std::string_view spindle;
      GrblOverrides overrides;
      if (!split3(payload, ',', feed, rapid, spindle) ||
          !parseUnsigned(feed, overrides.feed_percent) ||
          !parseUnsigned(rapid, overrides.rapid_percent) ||
          !parseUnsigned(spindle, overrides.spindle_percent)) {
        return failure<GrblStatus>(line, "Ov must contain three unsigned percentages");
      }
      saw_overrides = true;
      status.overrides = overrides;
    } else if (key == "Pn") {
      if (saw_pins) return failure<GrblStatus>(line, "status report contains a duplicate Pn field");
      GrblPins pins;
      std::uint16_t seen = 0;
      constexpr std::string_view valid = "XYZPDHRS";
      for (char pin : payload) {
        const auto at = valid.find(pin);
        if (at == std::string_view::npos || (seen & (std::uint16_t{1} << at)) != 0) {
          return failure<GrblStatus>(line, "Pn contains an unknown or duplicate pin code");
        }
        seen |= std::uint16_t{1} << at;
        if (pin == 'X') pins.x_limit = true;
        else if (pin == 'Y') pins.y_limit = true;
        else if (pin == 'Z') pins.z_limit = true;
        else if (pin == 'P') pins.probe = true;
        else if (pin == 'D') pins.door = true;
        else if (pin == 'H') pins.hold = true;
        else if (pin == 'R') pins.soft_reset = true;
        else if (pin == 'S') pins.cycle_start = true;
      }
      saw_pins = true;
      status.pins = pins;
    } else if (key == "A") {
      if (saw_accessories) return failure<GrblStatus>(line, "status report contains a duplicate A field");
      GrblAccessories accessories;
      std::uint8_t seen = 0;
      constexpr std::string_view valid = "SCFM";
      for (char accessory : payload) {
        const auto at = valid.find(accessory);
        if (at == std::string_view::npos || (seen & (std::uint8_t{1} << at)) != 0) {
          return failure<GrblStatus>(line, "A contains an unknown or duplicate accessory code");
        }
        seen |= std::uint8_t{1} << at;
        if (accessory == 'S') accessories.spindle_cw = true;
        else if (accessory == 'C') accessories.spindle_ccw = true;
        else if (accessory == 'F') accessories.flood = true;
        else if (accessory == 'M') accessories.mist = true;
      }
      if (accessories.spindle_cw && accessories.spindle_ccw) {
        return failure<GrblStatus>(line, "A cannot report clockwise and counterclockwise spindle together");
      }
      saw_accessories = true;
      status.accessories = accessories;
    } else {
      return failure<GrblStatus>(line, "unknown Grbl 1.1 status field");
    }

    if (next == std::string_view::npos) break;
    offset = next + 1;
  }

  if (saw_mpos == saw_wpos) {
    return failure<GrblStatus>(line, "status report must contain exactly one of MPos or WPos");
  }
  return success(line, status);
}

AlarmClass classifyAlarm(int code) noexcept {
  if (code == 1 || code == 3) return AlarmClass::position_lost;
  if (code == 4 || code == 5) return AlarmClass::probe_fail;
  if (code == 2) return AlarmClass::soft_limit;
  if (code == 8 || code == 9 || code == 10) return AlarmClass::limit;
  return AlarmClass::other;
}

std::string_view errorText(int code) noexcept {
  switch (code) {
    case 1: return "Expected a command letter";
    case 2: return "Bad numeric value format";
    case 3: return "Invalid statement";
    case 4: return "Negative value received where positive was required";
    case 5: return "Homing cycle is not enabled";
    case 6: return "Minimum step pulse time must be greater than 3 microseconds";
    case 7: return "EEPROM read failed; defaults restored";
    case 8: return "Command requires the controller to be idle";
    case 9: return "G-code commands are locked out during alarm or jog state";
    case 10: return "Soft limits require homing to be enabled";
    case 11: return "Maximum characters per line exceeded";
    case 12: return "Setting would exceed the maximum supported step rate";
    case 13: return "Safety door is open";
    case 14: return "Build info or startup line exceeds EEPROM line length";
    case 15: return "Jog target exceeds machine travel";
    case 16: return "Invalid jog command";
    case 17: return "Laser mode requires PWM output";
    case 20: return "Unsupported or invalid G-code command";
    case 21: return "More than one command from the same modal group";
    case 22: return "Feed rate is undefined";
    case 23: return "G-code command requires an integer value";
    case 24: return "Conflicting axis commands in the same block";
    case 25: return "Repeated G-code word in the same block";
    case 26: return "Required axis words are missing";
    case 27: return "Invalid line number";
    case 28: return "Required value word is missing";
    case 29: return "Unsupported work coordinate system";
    case 30: return "G53 requires G0 or G1 motion mode";
    case 31: return "Axis words are unused by the active command";
    case 32: return "Arc requires an in-plane axis word";
    case 33: return "Invalid motion target";
    case 34: return "Invalid arc radius";
    case 35: return "Arc requires an in-plane offset word";
    case 36: return "Unused value words in block";
    case 37: return "G43.1 offset is not assigned to the configured tool axis";
    case 38: return "Tool number exceeds the maximum supported value";
    default: return {};
  }
}

std::string_view alarmText(int code) noexcept {
  switch (code) {
    case 1: return "Hard limit triggered; machine position is likely lost and re-homing is required";
    case 2: return "Motion target exceeds machine travel; machine position was retained";
    case 3: return "Reset occurred during motion; machine position is likely lost and re-homing is required";
    case 4: return "Probe was not in the expected initial state before the probe cycle";
    case 5: return "Probe did not contact within the programmed travel";
    case 6: return "Homing failed because reset was issued during the homing cycle";
    case 7: return "Homing failed because the safety door opened during the homing cycle";
    case 8: return "Homing failed to clear a limit switch during pull-off";
    case 9: return "Homing failed to find a limit switch within the search distance";
    case 10: return "Homing failed to find the second limit switch for a self-squaring dual axis";
    default: return {};
  }
}

std::optional<GrblSettingInfo> settingInfo(int id) noexcept {
  switch (id) {
    case 0: return GrblSettingInfo{id, "Step pulse time", "microseconds"};
    case 1: return GrblSettingInfo{id, "Step idle delay", "milliseconds"};
    case 2: return GrblSettingInfo{id, "Step pulse invert", "mask"};
    case 3: return GrblSettingInfo{id, "Step direction invert", "mask"};
    case 4: return GrblSettingInfo{id, "Invert step enable pin", "boolean"};
    case 5: return GrblSettingInfo{id, "Invert limit pins", "boolean"};
    case 6: return GrblSettingInfo{id, "Invert probe pin", "boolean"};
    case 10: return GrblSettingInfo{id, "Status report options", "mask"};
    case 11: return GrblSettingInfo{id, "Junction deviation", "millimeters"};
    case 12: return GrblSettingInfo{id, "Arc tolerance", "millimeters"};
    case 13: return GrblSettingInfo{id, "Report in inches", "boolean"};
    case 20: return GrblSettingInfo{id, "Soft limits", "boolean"};
    case 21: return GrblSettingInfo{id, "Hard limits", "boolean"};
    case 22: return GrblSettingInfo{id, "Homing cycle", "boolean"};
    case 23: return GrblSettingInfo{id, "Homing direction invert", "mask"};
    case 24: return GrblSettingInfo{id, "Homing locate feed rate", "millimeters per minute"};
    case 25: return GrblSettingInfo{id, "Homing search seek rate", "millimeters per minute"};
    case 26: return GrblSettingInfo{id, "Homing switch debounce delay", "milliseconds"};
    case 27: return GrblSettingInfo{id, "Homing switch pull-off distance", "millimeters"};
    case 30: return GrblSettingInfo{id, "Maximum spindle speed", "revolutions per minute"};
    case 31: return GrblSettingInfo{id, "Minimum spindle speed", "revolutions per minute"};
    case 32: return GrblSettingInfo{id, "Laser mode", "boolean"};
    case 100: return GrblSettingInfo{id, "X-axis travel resolution", "steps per millimeter"};
    case 101: return GrblSettingInfo{id, "Y-axis travel resolution", "steps per millimeter"};
    case 102: return GrblSettingInfo{id, "Z-axis travel resolution", "steps per millimeter"};
    case 110: return GrblSettingInfo{id, "X-axis maximum rate", "millimeters per minute"};
    case 111: return GrblSettingInfo{id, "Y-axis maximum rate", "millimeters per minute"};
    case 112: return GrblSettingInfo{id, "Z-axis maximum rate", "millimeters per minute"};
    case 120: return GrblSettingInfo{id, "X-axis acceleration", "millimeters per second squared"};
    case 121: return GrblSettingInfo{id, "Y-axis acceleration", "millimeters per second squared"};
    case 122: return GrblSettingInfo{id, "Z-axis acceleration", "millimeters per second squared"};
    case 130: return GrblSettingInfo{id, "X-axis maximum travel", "millimeters"};
    case 131: return GrblSettingInfo{id, "Y-axis maximum travel", "millimeters"};
    case 132: return GrblSettingInfo{id, "Z-axis maximum travel", "millimeters"};
    default: return std::nullopt;
  }
}

ParseResult<PushMessage> parsePushMessage(std::string_view line) noexcept {
  if (line.empty()) return failure<PushMessage>(line, "push message is empty");
  PushMessage message;
  message.raw = line;

  if (line == "ok") {
    message.kind = PushKind::acknowledgment;
    message.text = "Command parsed and queued; physical execution is not implied";
    return success(line, message);
  }

  constexpr std::string_view error_prefix = "error:";
  constexpr std::string_view alarm_prefix = "ALARM:";
  if (line.starts_with(error_prefix) || line.starts_with(alarm_prefix)) {
    const bool is_error = line.starts_with(error_prefix);
    int code = 0;
    if (!parseInt(line.substr(is_error ? error_prefix.size() : alarm_prefix.size()), code)) {
      return failure<PushMessage>(line, is_error ? "error response requires an integer code"
                                                : "alarm response requires an integer code");
    }
    if (is_error ? !isKnownError(code) : !isKnownAlarm(code)) {
      return failure<PushMessage>(line, is_error ? "unknown Grbl 1.1 error code"
                                                : "unknown Grbl 1.1 alarm code");
    }
    message.kind = is_error ? PushKind::error : PushKind::alarm;
    message.code = code;
    message.text = is_error ? errorText(code) : alarmText(code);
    if (!is_error) message.alarm_class = classifyAlarm(code);
    return success(line, message);
  }

  constexpr std::string_view welcome_prefix = "Grbl ";
  if (line.starts_with(welcome_prefix)) {
    const auto rest = line.substr(welcome_prefix.size());
    const auto space = rest.find(' ');
    const auto version = rest.substr(0, space);
    if (!version.starts_with("1.1") || version.size() < 4 ||
        (space != std::string_view::npos && rest.substr(space) != " ['$' for help]")) {
      return failure<PushMessage>(line, "malformed or non-1.1 Grbl welcome banner");
    }
    message.kind = PushKind::welcome;
    message.tag = "VER";
    message.text = version;
    return success(line, message);
  }

  if (line.front() == '$') {
    const auto equals = line.find('=');
    if (equals == std::string_view::npos || equals == 1 || equals + 1 == line.size() ||
        line.find('=', equals + 1) != std::string_view::npos) {
      return failure<PushMessage>(line, "setting report must have the form $N=value");
    }
    int id = -1;
    double value = 0.0;
    if (!parseInt(line.substr(1, equals - 1), id) || !parseDouble(line.substr(equals + 1), value)) {
      return failure<PushMessage>(line, "setting id and value must be finite numbers");
    }
    const auto info = settingInfo(id);
    if (!info) return failure<PushMessage>(line, "unknown Grbl 1.1 setting id");
    message.kind = PushKind::setting;
    message.tag = line.substr(0, equals);
    message.setting = GrblSetting{*info, value};
    return success(line, message);
  }

  if (line.front() == '>') {
    const auto ok_at = line.rfind(":ok");
    const auto error_at = line.rfind(":error:");
    if (ok_at != std::string_view::npos && ok_at + 3 == line.size()) {
      message.kind = PushKind::startup;
      message.text = line.substr(1, ok_at - 1);
      return success(line, message);
    }
    if (error_at != std::string_view::npos) {
      int code = 0;
      if (error_at + 7 >= line.size() || !parseInt(line.substr(error_at + 7), code) ||
          !isKnownError(code)) {
        return failure<PushMessage>(line, "startup execution has an invalid error code");
      }
      message.kind = PushKind::startup;
      message.text = line.substr(1, error_at - 1);
      message.code = code;
      return success(line, message);
    }
    return failure<PushMessage>(line, "malformed startup execution message");
  }

  if (line.front() != '[' || line.back() != ']') {
    return failure<PushMessage>(line, "unknown Grbl 1.1 message type");
  }
  const auto body = line.substr(1, line.size() - 2);
  const auto colon = body.find(':');
  if (colon == std::string_view::npos || colon == 0) {
    return failure<PushMessage>(line, "bracketed message requires a tag and colon");
  }
  const auto tag = body.substr(0, colon);
  const auto payload = body.substr(colon + 1);
  message.tag = tag;

  if (tag == "MSG") {
    message.kind = PushKind::message;
    message.text = payload;
  } else if (tag == "GC") {
    if (payload.empty()) return failure<PushMessage>(line, "GC payload is empty");
    message.kind = PushKind::gcode_state;
    message.text = payload;
  } else if (tag == "HLP") {
    if (payload.empty()) return failure<PushMessage>(line, "HLP payload is empty");
    message.kind = PushKind::help;
    message.text = payload;
  } else if (tag == "VER") {
    if (payload.empty()) return failure<PushMessage>(line, "VER payload is empty");
    message.kind = PushKind::version;
    message.text = payload;
  } else if (tag == "echo") {
    if (payload.empty()) return failure<PushMessage>(line, "echo payload is empty");
    message.kind = PushKind::echo;
    message.text = payload;
  } else if (tag == "OPT") {
    std::string_view flags;
    std::string_view planner;
    std::string_view rx;
    GrblBuildOptions options;
    if (!split3(payload, ',', flags, planner, rx) || !validOptionFlags(flags) ||
        !parseUnsigned(planner, options.planner_blocks) ||
        !parseUnsigned(rx, options.rx_buffer_bytes) || options.planner_blocks == 0 ||
        options.rx_buffer_bytes == 0) {
      return failure<PushMessage>(line, "OPT requires valid flags and positive planner/RX sizes");
    }
    options.flags = flags;
    message.kind = PushKind::options;
    message.options = options;
  } else if (isCoordinateTag(tag)) {
    if (!parseVec3(payload, message.coordinates)) {
      return failure<PushMessage>(line, "coordinate parameter requires exactly three finite numbers");
    }
    message.kind = PushKind::coordinate_offset;
  } else if (tag == "PRB") {
    const auto separator = payload.rfind(':');
    if (separator == std::string_view::npos ||
        !parseVec3(payload.substr(0, separator), message.coordinates)) {
      return failure<PushMessage>(line, "PRB requires three coordinates and a success flag");
    }
    const auto probe_flag = payload.substr(separator + 1);
    if (probe_flag != "0" && probe_flag != "1") {
      return failure<PushMessage>(line, "PRB success flag must be 0 or 1");
    }
    message.kind = PushKind::probe;
    message.probe_success = probe_flag == "1";
  } else if (tag == "TLO") {
    if (!parseDouble(payload, message.tool_length_offset)) {
      return failure<PushMessage>(line, "TLO requires one finite number");
    }
    message.kind = PushKind::tool_length_offset;
  } else {
    return failure<PushMessage>(line, "unknown Grbl 1.1 bracketed message tag");
  }

  return success(line, message);
}

std::uint8_t encodeRealtime(RealtimeCmd command) noexcept {
  switch (command) {
    case RealtimeCmd::status_report: return '?';
    case RealtimeCmd::cycle_start: return '~';
    case RealtimeCmd::feed_hold: return '!';
    case RealtimeCmd::soft_reset: return 0x18;
    case RealtimeCmd::safety_door: return 0x84;
    case RealtimeCmd::jog_cancel: return 0x85;
    case RealtimeCmd::feed_override_reset: return 0x90;
    case RealtimeCmd::feed_override_plus_10: return 0x91;
    case RealtimeCmd::feed_override_minus_10: return 0x92;
    case RealtimeCmd::feed_override_plus_1: return 0x93;
    case RealtimeCmd::feed_override_minus_1: return 0x94;
    case RealtimeCmd::rapid_override_reset: return 0x95;
    case RealtimeCmd::rapid_override_50: return 0x96;
    case RealtimeCmd::rapid_override_25: return 0x97;
    case RealtimeCmd::spindle_override_reset: return 0x99;
    case RealtimeCmd::spindle_override_plus_10: return 0x9A;
    case RealtimeCmd::spindle_override_minus_10: return 0x9B;
    case RealtimeCmd::spindle_override_plus_1: return 0x9C;
    case RealtimeCmd::spindle_override_minus_1: return 0x9D;
    case RealtimeCmd::spindle_stop_toggle: return 0x9E;
    case RealtimeCmd::flood_coolant_toggle: return 0xA0;
    case RealtimeCmd::mist_coolant_toggle: return 0xA1;
  }
  return 0;
}

EncodeLineResult encodeLine(std::string_view gcode, LineBuffer& buffer) noexcept {
  buffer.size = 0;
  if (gcode.empty()) return {false, {}, "G-code line is empty"};
  if (gcode.find('\r') != std::string_view::npos || gcode.find('\n') != std::string_view::npos) {
    return {false, {}, "G-code line contains an embedded terminator"};
  }
  if (buffer.data == nullptr) return {false, {}, "line buffer has no storage"};
  if (gcode.size() == std::numeric_limits<std::size_t>::max() ||
      gcode.size() + 1 > buffer.capacity) {
    return {false, {}, "line buffer is too small"};
  }
  for (std::size_t i = 0; i < gcode.size(); ++i) buffer.data[i] = gcode[i];
  buffer.data[gcode.size()] = '\r';
  buffer.size = gcode.size() + 1;
  return {true, std::string_view(buffer.data, buffer.size), {}};
}

GrblRxAccounting::GrblRxAccounting(std::size_t rx_capacity) noexcept
    : capacity_(rx_capacity == 0 ? kDefaultRxCapacity : rx_capacity) {}

bool GrblRxAccounting::canSend(std::size_t encoded_characters) const noexcept {
  return encoded_characters != 0 && encoded_characters <= capacity_ - outstanding_characters_ &&
         count_ < kMaxQueuedLines;
}

AccountingResult GrblRxAccounting::accountSent(std::size_t encoded_characters) noexcept {
  if (encoded_characters == 0) return {false, "encoded line length must be non-zero"};
  if (encoded_characters > capacity_) return {false, "encoded line exceeds Grbl RX capacity"};
  if (count_ == kMaxQueuedLines) return {false, "character-counting FIFO is full"};
  if (encoded_characters > capacity_ - outstanding_characters_) {
    return {false, "insufficient Grbl RX capacity"};
  }
  const auto tail = (head_ + count_) % kMaxQueuedLines;
  line_lengths_[tail] = encoded_characters;
  outstanding_characters_ += encoded_characters;
  ++count_;
  ++sent_lines_;
  return {true, {}};
}

AccountingResult GrblRxAccounting::acknowledge(const PushMessage& response) noexcept {
  if (response.kind != PushKind::acknowledgment && response.kind != PushKind::error) {
    return {false, "message is not an ok or error streaming response"};
  }
  if (count_ == 0) return {false, "streaming response has no outstanding line"};
  outstanding_characters_ -= line_lengths_[head_];
  head_ = (head_ + 1) % kMaxQueuedLines;
  --count_;
  ++acknowledged_lines_;
  return {true, {}};
}

}  // namespace chudware::device
