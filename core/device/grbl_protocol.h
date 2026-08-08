#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string_view>

namespace chudware::device {

// Parse results borrow the input line. The caller must keep it alive while raw or any
// payload string_view is used. raw is populated on both success and failure.
template <typename T>
struct ParseResult {
  bool ok = false;
  T value{};
  std::string_view raw{};
  std::string_view reason{};

  explicit constexpr operator bool() const noexcept { return ok; }
};

struct Vec3 {
  double x = 0.0;
  double y = 0.0;
  double z = 0.0;

  constexpr bool operator==(const Vec3&) const = default;
};

enum class GrblState {
  idle,
  run,
  hold,
  jog,
  alarm,
  door,
  check,
  home,
  sleep,
};

struct GrblStateMapping {
  GrblState grbl;
  std::string_view canonical_device_state;
};

// Pure adapter binding table. Check and Sleep are quiescent controller modes and map
// to canonical "idle". Run during an active G38.x is promoted to "probing" by the
// adapter, because the status frame alone does not identify the executing command.
// disconnected/connecting/identifying/fault/unknown are transport/adapter states.
inline constexpr std::array<GrblStateMapping, 9> kGrblStateMappings{{
    {GrblState::idle, "idle"},
    {GrblState::run, "running"},
    {GrblState::hold, "hold"},
    {GrblState::jog, "running"},
    {GrblState::alarm, "alarm"},
    {GrblState::door, "door"},
    {GrblState::check, "idle"},
    {GrblState::home, "homing"},
    {GrblState::sleep, "idle"},
}};

std::string_view canonicalDeviceState(GrblState state) noexcept;

struct GrblBufferState {
  std::uint32_t planner_available = 0;
  std::uint32_t rx_available = 0;
};

struct GrblRates {
  double feed = 0.0;
  std::optional<double> spindle_rpm;
};

struct GrblOverrides {
  std::uint32_t feed_percent = 0;
  std::uint32_t rapid_percent = 0;
  std::uint32_t spindle_percent = 0;
};

struct GrblPins {
  bool x_limit = false;
  bool y_limit = false;
  bool z_limit = false;
  bool probe = false;
  bool door = false;
  bool hold = false;
  bool soft_reset = false;
  bool cycle_start = false;
};

struct GrblAccessories {
  bool spindle_cw = false;
  bool spindle_ccw = false;
  bool flood = false;
  bool mist = false;
};

struct GrblStatus {
  GrblState state = GrblState::idle;
  std::optional<std::uint8_t> substate;
  std::optional<Vec3> machine_position;
  std::optional<Vec3> work_position;
  std::optional<Vec3> work_coordinate_offset;
  std::optional<GrblBufferState> buffer;
  std::optional<GrblRates> rates;
  std::optional<GrblOverrides> overrides;
  std::optional<GrblPins> pins;
  std::optional<GrblAccessories> accessories;
  std::optional<std::uint32_t> line_number;
  std::string_view raw{};
};

ParseResult<GrblStatus> parseStatusReport(std::string_view line) noexcept;

enum class AlarmClass {
  position_lost,
  probe_fail,
  limit,
  soft_limit,
  other,
};

AlarmClass classifyAlarm(int code) noexcept;
std::string_view errorText(int code) noexcept;
std::string_view alarmText(int code) noexcept;

struct GrblBuildOptions {
  std::string_view flags{};
  std::uint32_t planner_blocks = 0;
  std::uint32_t rx_buffer_bytes = 0;
};

struct GrblSettingInfo {
  int id = -1;
  std::string_view name{};
  std::string_view unit{};
};

std::optional<GrblSettingInfo> settingInfo(int id) noexcept;

struct GrblSetting {
  GrblSettingInfo info{};
  double value = 0.0;
};

enum class PushKind {
  acknowledgment,
  error,
  alarm,
  message,
  gcode_state,
  coordinate_offset,
  probe,
  version,
  options,
  setting,
  welcome,
  help,
  echo,
  tool_length_offset,
  startup,
};

struct PushMessage {
  PushKind kind = PushKind::acknowledgment;
  std::string_view raw{};
  std::string_view tag{};
  std::string_view text{};
  int code = 0;
  AlarmClass alarm_class = AlarmClass::other;
  Vec3 coordinates{};
  bool probe_success = false;
  GrblBuildOptions options{};
  GrblSetting setting{};
  double tool_length_offset = 0.0;
};

ParseResult<PushMessage> parsePushMessage(std::string_view line) noexcept;

enum class RealtimeCmd {
  status_report,
  cycle_start,
  feed_hold,
  soft_reset,
  safety_door,
  jog_cancel,
  feed_override_reset,
  feed_override_plus_10,
  feed_override_minus_10,
  feed_override_plus_1,
  feed_override_minus_1,
  rapid_override_reset,
  rapid_override_50,
  rapid_override_25,
  spindle_override_reset,
  spindle_override_plus_10,
  spindle_override_minus_10,
  spindle_override_plus_1,
  spindle_override_minus_1,
  spindle_stop_toggle,
  flood_coolant_toggle,
  mist_coolant_toggle,
};

std::uint8_t encodeRealtime(RealtimeCmd command) noexcept;

struct LineBuffer {
  char* data = nullptr;
  std::size_t capacity = 0;
  std::size_t size = 0;
};

struct EncodeLineResult {
  bool ok = false;
  std::string_view line{};
  std::string_view reason{};

  explicit constexpr operator bool() const noexcept { return ok; }
};

// Copies gcode into caller-owned storage and appends the Grbl command terminator '\r'.
EncodeLineResult encodeLine(std::string_view gcode, LineBuffer& buffer) noexcept;

struct AccountingResult {
  bool ok = false;
  std::string_view reason{};

  explicit constexpr operator bool() const noexcept { return ok; }
};

// Pure character-counting stream state. It tracks complete encoded line lengths in
// FIFO order; status reports and other push messages never release RX capacity.
// An ok or error response releases the oldest line. It deliberately does not consult
// the optional Bf field, whose value may already be stale when received.
class GrblRxAccounting {
 public:
  static constexpr std::size_t kDefaultRxCapacity = 128;
  static constexpr std::size_t kMaxQueuedLines = 128;

  explicit GrblRxAccounting(std::size_t rx_capacity = kDefaultRxCapacity) noexcept;

  [[nodiscard]] bool canSend(std::size_t encoded_characters) const noexcept;
  [[nodiscard]] AccountingResult accountSent(std::size_t encoded_characters) noexcept;
  [[nodiscard]] AccountingResult acknowledge(const PushMessage& response) noexcept;

  [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }
  [[nodiscard]] std::size_t outstandingCharacters() const noexcept { return outstanding_characters_; }
  [[nodiscard]] std::size_t availableCharacters() const noexcept {
    return capacity_ - outstanding_characters_;
  }
  [[nodiscard]] std::size_t outstandingLines() const noexcept { return count_; }
  [[nodiscard]] std::uint64_t sentLines() const noexcept { return sent_lines_; }
  [[nodiscard]] std::uint64_t acknowledgedLines() const noexcept { return acknowledged_lines_; }

 private:
  std::array<std::size_t, kMaxQueuedLines> line_lengths_{};
  std::size_t capacity_ = kDefaultRxCapacity;
  std::size_t outstanding_characters_ = 0;
  std::size_t head_ = 0;
  std::size_t count_ = 0;
  std::uint64_t sent_lines_ = 0;
  std::uint64_t acknowledged_lines_ = 0;
};

}  // namespace chudware::device
