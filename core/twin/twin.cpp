#include "twin/twin.h"
#include "twin/wide_math.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <charconv>
#include <cctype>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <tuple>
#include <utility>

namespace chudware::twin {
namespace {

constexpr std::uint64_t kFnvOffset = 14695981039346656037ull;
constexpr std::uint64_t kFnvPrime = 1099511628211ull;
constexpr std::array<std::int64_t, 4> kLegalDt{1'000'000, 500'000, 250'000, 100'000};

std::uint64_t fnv(const std::uint8_t* data, std::size_t size) noexcept {
    std::uint64_t value = kFnvOffset;
    for (std::size_t i = 0; i < size; ++i) {
        value ^= data[i];
        value *= kFnvPrime;
    }
    return value;
}

std::uint64_t fnv(std::string_view text) noexcept {
    return fnv(reinterpret_cast<const std::uint8_t*>(text.data()), text.size());
}

struct Writer {
    std::vector<std::uint8_t> data;

    template <typename T>
    void integer(T value) {
        using U = std::make_unsigned_t<T>;
        U bits = static_cast<U>(value);
        for (std::size_t i = 0; i < sizeof(T); ++i) data.push_back(static_cast<std::uint8_t>(bits >> (i * 8)));
    }
    void boolean(bool value) { integer<std::uint8_t>(value ? 1 : 0); }
    void string(std::string_view value) {
        integer<std::uint64_t>(value.size());
        data.insert(data.end(), value.begin(), value.end());
    }
};

struct Reader {
    const std::vector<std::uint8_t>& data;
    std::size_t offset = 0;
    bool ok = true;

    template <typename T>
    T integer() {
        using U = std::make_unsigned_t<T>;
        if (!ok || data.size() - std::min(data.size(), offset) < sizeof(T)) {
            ok = false;
            return {};
        }
        U bits = 0;
        for (std::size_t i = 0; i < sizeof(T); ++i) bits |= static_cast<U>(data[offset++]) << (i * 8);
        return static_cast<T>(bits);
    }
    bool boolean() {
        const auto value = integer<std::uint8_t>();
        if (value > 1) ok = false;
        return value == 1;
    }
    std::string string() {
        const auto size = integer<std::uint64_t>();
        if (!ok || size > data.size() - std::min(data.size(), offset)) {
            ok = false;
            return {};
        }
        std::string value(reinterpret_cast<const char*>(data.data() + offset), static_cast<std::size_t>(size));
        offset += static_cast<std::size_t>(size);
        return value;
    }
};


std::optional<std::int64_t> parseScaled(std::string_view text, std::int64_t scale) {
    return wide_math::parseScaledDecimal(text, scale);
}

std::string formatMm(Nm value) {
    const bool negative = value < 0;
    const std::uint64_t magnitude = negative ? static_cast<std::uint64_t>(-(value + 1)) + 1 : static_cast<std::uint64_t>(value);
    const auto milli_mm = (magnitude + 500) / 1'000;
    const auto whole = milli_mm / 1'000;
    const auto frac = milli_mm % 1'000;
    std::string result = negative ? "-" : "";
    result += std::to_string(whole);
    result += '.';
    result.push_back(static_cast<char>('0' + frac / 100));
    result.push_back(static_cast<char>('0' + (frac / 10) % 10));
    result.push_back(static_cast<char>('0' + frac % 10));
    return result;
}

HaltCause faultHalt(device::RunDiscontinuity value) noexcept {
    switch (value) {
        case device::RunDiscontinuity::Reset: return HaltCause::ControllerReset;
        case device::RunDiscontinuity::Alarm: return HaltCause::ControllerAlarm;
        case device::RunDiscontinuity::HardLimit: return HaltCause::HardLimit;
        case device::RunDiscontinuity::Disconnect: return HaltCause::TransportDisconnect;
        case device::RunDiscontinuity::PowerLoss: return HaltCause::PowerLoss;
        case device::RunDiscontinuity::None: return HaltCause::None;
    }
    return HaltCause::None;
}

bool contains(const std::vector<CollisionClass>& values, CollisionClass value) {
    return std::find(values.begin(), values.end(), value) != values.end();
}

std::string hex(const std::vector<std::uint8_t>& bytes) {
    static constexpr char digits[] = "0123456789abcdef";
    std::string result;
    result.resize(bytes.size() * 2);
    for (std::size_t i = 0; i < bytes.size(); ++i) {
        result[2 * i] = digits[bytes[i] >> 4];
        result[2 * i + 1] = digits[bytes[i] & 15];
    }
    return result;
}

std::vector<std::uint8_t> unhex(std::string_view text) {
    if (text.size() % 2 != 0) throw std::invalid_argument("trace snapshot has odd hex length");
    auto nibble = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    std::vector<std::uint8_t> bytes(text.size() / 2);
    for (std::size_t i = 0; i < bytes.size(); ++i) {
        const int high = nibble(text[2 * i]);
        const int low = nibble(text[2 * i + 1]);
        if (high < 0 || low < 0) throw std::invalid_argument("trace snapshot contains non-hex data");
        bytes[i] = static_cast<std::uint8_t>((high << 4) | low);
    }
    return bytes;
}

FidelityDeclaration makeFidelity() {
    FidelityDeclaration value;
    value.id = "twin/mill3-grbl11-heightfield/1";
    value.aspects = {
        {"clock", FidelityDeclaration::Level::Modeled, "fixed integer tick scheduling", "zero tick scheduling error"},
        {"controller_wire", FidelityDeclaration::Level::Modeled, "shared Grbl line and response codec", "serial byte timing is outside the model"},
        {"cartesian_motion", FidelityDeclaration::Level::Approximated, "three-axis integer path interpolation using configured feed and acceleration", "position rounding is at most one nanometre per axis; acceleration shape differs by up to one configured ramp interval"},
        {"stock_removal", FidelityDeclaration::Level::Approximated, "top-down integer heightfield updated once per motion tick", "lateral contact is bounded by one cell diagonal and vertical quantization by one nanometre"},
        {"collision", FidelityDeclaration::Level::Approximated, "integer stock, travel, and axis-aligned fixture envelope tests", "contact localization is bounded by one tick of travel plus one stock cell"},
        {"virtual_observation", FidelityDeclaration::Level::Approximated, "integer encoder samples and heightfield-derived grayscale frames", "sensor quantization is at most one count; optical appearance error has no physical bound"},
        {"faults", FidelityDeclaration::Level::Modeled, "device run-discontinuity types applied at exact ticks", "only enumerated discontinuities are represented"},
    };
    return value;
}

}  // namespace

struct DeterministicTwin::Impl {
    struct Motion {
        Vec3Nm start;
        Vec3Nm target;
        std::int64_t total_ticks = 0;
        std::int64_t elapsed_ticks = 0;
        bool rapid = true;
        bool active = false;
    };

    struct State {
        Tick tick = 0;
        Vec3Nm position;
        Vec3Nm velocity;
        std::vector<Nm> stock_heights;
        std::vector<TwinAction> pending;
        std::vector<std::string> command_queue;
        Motion motion;
        bool absolute = true;
        bool inch_units = false;
        bool modal_rapid = true;
        bool spindle_on = false;
        NmPerS feed_nm_s = 1;
        bool halted = false;
        HaltCause halt = HaltCause::None;
        bool program_seen = false;
        bool explicit_end = false;
        std::array<bool, 3> collision_latched{};
        std::uint64_t next_submission = 0;
    };

    TwinConfig config;
    std::uint64_t config_hash = 0;
    FidelityDeclaration fidelity = makeFidelity();
    State state;
    bool configured = false;

    std::vector<std::uint8_t> stateBytes(const State& source) const {
        Writer writer;
        writer.integer<std::int64_t>(source.tick);
        writer.integer<Nm>(source.position.x); writer.integer<Nm>(source.position.y); writer.integer<Nm>(source.position.z);
        writer.integer<Nm>(source.velocity.x); writer.integer<Nm>(source.velocity.y); writer.integer<Nm>(source.velocity.z);
        writer.integer<std::uint64_t>(source.stock_heights.size());
        for (const auto height : source.stock_heights) writer.integer<Nm>(height);
        writer.integer<std::uint64_t>(source.pending.size());
        for (const auto& action : source.pending) {
            writer.integer<std::uint8_t>(static_cast<std::uint8_t>(action.kind));
            writer.integer<Tick>(action.at_tick);
            writer.integer<std::uint64_t>(action.submission_order);
            writer.string(action.line);
            writer.integer<Tick>(action.fault.at_tick);
            writer.integer<std::uint8_t>(static_cast<std::uint8_t>(action.fault.type));
        }
        writer.integer<std::uint64_t>(source.command_queue.size());
        for (const auto& line : source.command_queue) writer.string(line);
        writer.integer<Nm>(source.motion.start.x); writer.integer<Nm>(source.motion.start.y); writer.integer<Nm>(source.motion.start.z);
        writer.integer<Nm>(source.motion.target.x); writer.integer<Nm>(source.motion.target.y); writer.integer<Nm>(source.motion.target.z);
        writer.integer<std::int64_t>(source.motion.total_ticks);
        writer.integer<std::int64_t>(source.motion.elapsed_ticks);
        writer.boolean(source.motion.rapid); writer.boolean(source.motion.active);
        writer.boolean(source.absolute); writer.boolean(source.inch_units); writer.boolean(source.modal_rapid);
        writer.boolean(source.spindle_on); writer.integer<NmPerS>(source.feed_nm_s);
        writer.boolean(source.halted); writer.integer<std::uint8_t>(static_cast<std::uint8_t>(source.halt));
        writer.boolean(source.program_seen); writer.boolean(source.explicit_end);
        for (const bool latched : source.collision_latched) writer.boolean(latched);
        writer.integer<std::uint64_t>(source.next_submission);
        return writer.data;
    }

    std::optional<State> decodeState(const std::vector<std::uint8_t>& bytes) const {
        Reader reader{bytes};
        State result;
        result.tick = reader.integer<Tick>();
        result.position.x = reader.integer<Nm>(); result.position.y = reader.integer<Nm>(); result.position.z = reader.integer<Nm>();
        result.velocity.x = reader.integer<Nm>(); result.velocity.y = reader.integer<Nm>(); result.velocity.z = reader.integer<Nm>();
        const auto heights = reader.integer<std::uint64_t>();
        if (heights > static_cast<std::uint64_t>(config.stock.cells_x) * config.stock.cells_y) return std::nullopt;
        result.stock_heights.resize(static_cast<std::size_t>(heights));
        for (auto& height : result.stock_heights) height = reader.integer<Nm>();
        const auto actions = reader.integer<std::uint64_t>();
        if (actions > 1'000'000) return std::nullopt;
        result.pending.resize(static_cast<std::size_t>(actions));
        for (auto& action : result.pending) {
            const auto kind = reader.integer<std::uint8_t>();
            if (kind > static_cast<std::uint8_t>(TwinAction::Kind::Fault)) return std::nullopt;
            action.kind = static_cast<TwinAction::Kind>(kind);
            action.at_tick = reader.integer<Tick>();
            action.submission_order = reader.integer<std::uint64_t>();
            action.line = reader.string();
            action.fault.at_tick = reader.integer<Tick>();
            const auto fault = reader.integer<std::uint8_t>();
            if (fault > static_cast<std::uint8_t>(device::RunDiscontinuity::PowerLoss)) return std::nullopt;
            action.fault.type = static_cast<device::RunDiscontinuity>(fault);
        }
        const auto commands = reader.integer<std::uint64_t>();
        if (commands > 1'000'000) return std::nullopt;
        result.command_queue.resize(static_cast<std::size_t>(commands));
        for (auto& line : result.command_queue) line = reader.string();
        result.motion.start.x = reader.integer<Nm>(); result.motion.start.y = reader.integer<Nm>(); result.motion.start.z = reader.integer<Nm>();
        result.motion.target.x = reader.integer<Nm>(); result.motion.target.y = reader.integer<Nm>(); result.motion.target.z = reader.integer<Nm>();
        result.motion.total_ticks = reader.integer<std::int64_t>(); result.motion.elapsed_ticks = reader.integer<std::int64_t>();
        result.motion.rapid = reader.boolean(); result.motion.active = reader.boolean();
        result.absolute = reader.boolean(); result.inch_units = reader.boolean(); result.modal_rapid = reader.boolean();
        result.spindle_on = reader.boolean(); result.feed_nm_s = reader.integer<NmPerS>();
        result.halted = reader.boolean();
        const auto halt = reader.integer<std::uint8_t>();
        if (halt > static_cast<std::uint8_t>(HaltCause::ProgramEnd)) return std::nullopt;
        result.halt = static_cast<HaltCause>(halt);
        result.program_seen = reader.boolean(); result.explicit_end = reader.boolean();
        for (auto& latched : result.collision_latched) latched = reader.boolean();
        result.next_submission = reader.integer<std::uint64_t>();
        const auto expected_heights = static_cast<std::uint64_t>(config.stock.cells_x) * config.stock.cells_y;
        if (!reader.ok || reader.offset != bytes.size() || heights != expected_heights || result.tick < 0 ||
            result.feed_nm_s <= 0 || (result.motion.active && (result.motion.total_ticks <= 0 ||
            result.motion.elapsed_ticks < 0 || result.motion.elapsed_ticks > result.motion.total_ticks)) ||
            (!result.halted && result.halt != HaltCause::None) ||
            (result.halted && result.halt == HaltCause::None)) return std::nullopt;
        for (const auto height : result.stock_heights)
            if (height < config.stock.bottom_nm || height > config.stock.top_nm) return std::nullopt;
        for (const auto& action : result.pending)
            if (action.at_tick <= result.tick || (action.kind == TwinAction::Kind::GcodeLine &&
                (action.line.empty() || action.line.back() != '\r'))) return std::nullopt;
        for (const auto& line : result.command_queue)
            if (line.empty() || line.back() != '\r') return std::nullopt;
        return result;
    }

    std::uint64_t hashState() const { const auto bytes = stateBytes(state); return fnv(bytes.data(), bytes.size()); }

    std::optional<std::size_t> stockCell(Nm x, Nm y) const {
        if (x < config.stock.origin_nm.x || y < config.stock.origin_nm.y) return std::nullopt;
        const Nm local_x = x - config.stock.origin_nm.x;
        const Nm local_y = y - config.stock.origin_nm.y;
        const auto ix = static_cast<std::uint64_t>(local_x / config.stock.resolution_nm);
        const auto iy = static_cast<std::uint64_t>(local_y / config.stock.resolution_nm);
        if (ix >= config.stock.cells_x || iy >= config.stock.cells_y) return std::nullopt;
        return static_cast<std::size_t>(iy * config.stock.cells_x + ix);
    }

    void applyRemoval() {
        if (!state.motion.active || state.motion.rapid || !state.spindle_on) return;
        const Nm radius = config.tool.cutter_radius_nm;
        const Nm res = config.stock.resolution_nm;
        const auto min_ix = std::max<std::int64_t>(0, (state.position.x - radius - config.stock.origin_nm.x) / res);
        const auto max_ix = std::min<std::int64_t>(config.stock.cells_x - 1, (state.position.x + radius - config.stock.origin_nm.x) / res);
        const auto min_iy = std::max<std::int64_t>(0, (state.position.y - radius - config.stock.origin_nm.y) / res);
        const auto max_iy = std::min<std::int64_t>(config.stock.cells_y - 1, (state.position.y + radius - config.stock.origin_nm.y) / res);
        if (min_ix > max_ix || min_iy > max_iy) return;
        for (std::int64_t iy = min_iy; iy <= max_iy; ++iy) {
            for (std::int64_t ix = min_ix; ix <= max_ix; ++ix) {
                const Nm cx = config.stock.origin_nm.x + ix * res + res / 2;
                const Nm cy = config.stock.origin_nm.y + iy * res + res / 2;
                if (!wide_math::withinSquaredRadius(
                        cx, cy, state.position.x, state.position.y, radius))
                    continue;
                auto& height = state.stock_heights[static_cast<std::size_t>(iy * config.stock.cells_x + ix)];
                const Nm cut_height = std::max(config.stock.bottom_nm, state.position.z);
                if (cut_height < height) height = cut_height;
            }
        }
    }

    void emitCollision(CollisionClass type, Nm penetration, std::uint32_t fixture, StepResult& output) {
        const auto index = static_cast<std::size_t>(type);
        if (!state.collision_latched[index]) {
            output.collisions.push_back({state.tick, type, state.position, fixture, std::max<Nm>(0, penetration)});
            state.collision_latched[index] = true;
        }
        if (contains(config.halt_on, type)) {
            state.halted = true;
            state.halt = HaltCause::Collision;
            state.motion.active = false;
            state.velocity = {};
        }
    }

    void collisions(StepResult& output) {
        bool rapid_stock = false;
        Nm rapid_penetration = 0;
        if (state.motion.active && state.motion.rapid) {
            if (const auto cell = stockCell(state.position.x, state.position.y)) {
                const Nm top = state.stock_heights[*cell];
                if (state.position.z < top && state.position.z >= config.stock.bottom_nm) {
                    rapid_stock = true;
                    rapid_penetration = top - state.position.z;
                }
            }
        }
        if (rapid_stock) emitCollision(CollisionClass::RapidIntoStock, rapid_penetration, 0, output);
        else state.collision_latched[static_cast<std::size_t>(CollisionClass::RapidIntoStock)] = false;

        bool holder_fixture = false;
        Nm holder_penetration = 0;
        std::uint32_t holder_fixture_id = 0;
        const Nm holder_min_z = state.position.z + config.tool.holder_bottom_from_tip_nm;
        const Nm holder_max_z = holder_min_z + config.tool.holder_height_nm;
        for (const auto& fixture : config.fixtures) {
            const Nm closest_x = std::clamp(state.position.x, fixture.min_nm.x, fixture.max_nm.x);
            const Nm closest_y = std::clamp(state.position.y, fixture.min_nm.y, fixture.max_nm.y);
            const bool radial = wide_math::withinSquaredRadius(
                state.position.x, state.position.y, closest_x, closest_y,
                config.tool.holder_radius_nm);
            const bool vertical = holder_min_z < fixture.max_nm.z && holder_max_z > fixture.min_nm.z;
            if (radial && vertical) {
                holder_fixture = true;
                holder_fixture_id = fixture.id;
                holder_penetration = std::min(holder_max_z, fixture.max_nm.z) - std::max(holder_min_z, fixture.min_nm.z);
                break;
            }
        }
        if (holder_fixture) emitCollision(CollisionClass::ToolHolderIntoFixture, holder_penetration, holder_fixture_id, output);
        else state.collision_latched[static_cast<std::size_t>(CollisionClass::ToolHolderIntoFixture)] = false;

        bool travel = false;
        Nm excess = 0;
        const std::array<Nm, 3> positions{state.position.x, state.position.y, state.position.z};
        for (std::size_t axis = 0; axis < 3; ++axis) {
            if (positions[axis] < config.machine.axes[axis].travel_min_nm) {
                travel = true;
                excess = std::max(excess, config.machine.axes[axis].travel_min_nm - positions[axis]);
            } else if (positions[axis] > config.machine.axes[axis].travel_max_nm) {
                travel = true;
                excess = std::max(excess, positions[axis] - config.machine.axes[axis].travel_max_nm);
            }
        }
        if (travel) emitCollision(CollisionClass::TravelLimitViolation, excess, 0, output);
        else state.collision_latched[static_cast<std::size_t>(CollisionClass::TravelLimitViolation)] = false;
    }

    bool startLine(std::string encoded, StepResult& output) {
        if (encoded.empty() || encoded.back() != '\r') return false;
        encoded.pop_back();
        std::string clean;
        clean.reserve(encoded.size());
        bool comment = false;
        for (char c : encoded) {
            if (c == ';') break;
            if (c == '(') { comment = true; continue; }
            if (c == ')') { comment = false; continue; }
            if (!comment && !std::isspace(static_cast<unsigned char>(c))) clean.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
        }
        struct Word { char letter; std::string_view number; };
        std::vector<Word> words;
        for (std::size_t pos = 0; pos < clean.size();) {
            if (!std::isalpha(static_cast<unsigned char>(clean[pos]))) return false;
            const char letter = clean[pos++];
            const std::size_t begin = pos;
            if (pos < clean.size() && (clean[pos] == '+' || clean[pos] == '-')) ++pos;
            while (pos < clean.size() && (std::isdigit(static_cast<unsigned char>(clean[pos])) || clean[pos] == '.')) ++pos;
            if (begin == pos) return false;
            words.push_back({letter, std::string_view(clean).substr(begin, pos - begin)});
        }
        bool movement_word = false;
        bool has_axis = false;
        bool program_end = false;
        for (const auto& word : words) {
            if (word.letter == 'G') {
                const auto code = parseScaled(word.number, 1);
                if (!code) return false;
                if (*code == 0 || *code == 1) { state.modal_rapid = *code == 0; movement_word = true; }
                else if (*code == 20) state.inch_units = true;
                else if (*code == 21) state.inch_units = false;
                else if (*code == 90) state.absolute = true;
                else if (*code == 91) state.absolute = false;
                else return false;
            } else if (word.letter == 'M') {
                const auto code = parseScaled(word.number, 1);
                if (!code) return false;
                if (*code == 3 || *code == 4) state.spindle_on = true;
                else if (*code == 5) state.spindle_on = false;
                else if (*code == 2 || *code == 30) program_end = true;
                else return false;
            } else if (word.letter == 'X' || word.letter == 'Y' || word.letter == 'Z') {
                has_axis = true;
            } else if (word.letter != 'F') {
                return false;
            }
        }
        const std::int64_t unit_scale = state.inch_units ? 25'400'000 : kNmPerMm;
        Vec3Nm target = state.position;
        for (const auto& word : words) {
            if (word.letter == 'F') {
                const auto per_min = parseScaled(word.number, unit_scale);
                if (!per_min || *per_min <= 0) return false;
                state.feed_nm_s = std::max<std::int64_t>(1, (*per_min + 30) / 60);
            } else if (word.letter == 'X' || word.letter == 'Y' || word.letter == 'Z') {
                const auto coordinate = parseScaled(word.number, unit_scale);
                if (!coordinate) return false;
                Nm* component = word.letter == 'X' ? &target.x : word.letter == 'Y' ? &target.y : &target.z;
                *component = state.absolute ? *coordinate : *component + *coordinate;
            }
        }
        output.grbl_wire.emplace_back("ok");
        const auto parsed_ack = device::parsePushMessage(output.grbl_wire.back());
        if (!parsed_ack || parsed_ack.value.kind != device::PushKind::acknowledgment) return false;
        state.program_seen = true;
        if (program_end) {
            state.explicit_end = true;
            state.command_queue.clear();
            return true;
        }
        if (!movement_word && !has_axis) return true;
        const std::array<Nm, 3> starts{
            state.position.x, state.position.y, state.position.z};
        const std::array<Nm, 3> destinations{target.x, target.y, target.z};
        const std::array<NmPerS, 3> caps{
            config.machine.axes[0].max_feed_nm_s,
            config.machine.axes[1].max_feed_nm_s,
            config.machine.axes[2].max_feed_nm_s,
        };
        std::int64_t travel_ticks = 1;
        for (std::size_t axis = 0; axis < 3; ++axis) {
            const auto distance =
                wide_math::absoluteDistance(destinations[axis], starts[axis]);
            const NmPerS speed = state.modal_rapid ? caps[axis] : std::min(caps[axis], state.feed_nm_s);
            if (distance > 0)
                travel_ticks = std::max(
                    travel_ticks,
                    wide_math::ceilDivProductsPositive(
                        distance, 1'000'000'000,
                        static_cast<std::uint64_t>(speed),
                        static_cast<std::uint64_t>(config.dt_ns)));
        }
        std::int64_t ramp_ticks = 0;
        for (std::size_t axis = 0; axis < 3; ++axis) {
            const auto distance =
                wide_math::absoluteDistance(destinations[axis], starts[axis]);
            if (distance == 0) continue;
            const auto velocity = wide_math::ceilDivProductsPositive(
                distance, 1'000'000'000,
                static_cast<std::uint64_t>(travel_ticks),
                static_cast<std::uint64_t>(config.dt_ns));
            ramp_ticks = std::max(
                ramp_ticks,
                wide_math::ceilDivProductsPositive(
                    static_cast<std::uint64_t>(velocity), 1'000'000'000,
                    static_cast<std::uint64_t>(config.machine.axes[axis].accel_nm_s2),
                    static_cast<std::uint64_t>(config.dt_ns)));
        }
        state.motion = {state.position, target, travel_ticks + ramp_ticks, 0, state.modal_rapid, true};
        return true;
    }

    void observe(StepResult& output) {
        const std::string status = std::string(state.motion.active ? "<Run|MPos:" : "<Idle|MPos:") +
            formatMm(state.position.x) + ',' + formatMm(state.position.y) + ',' + formatMm(state.position.z) + ">";
        const auto parsed = device::parseStatusReport(status);
        if (parsed) {
            VirtualObservation observation;
            observation.tick = state.tick;
            observation.kind = ObservationKind::Controller;
            observation.src = "simulated";
            observation.values_q = {static_cast<std::int64_t>(parsed.value.state), state.position.x, state.position.y, state.position.z};
            observation.content_hash = fnv(status);
            output.observations.push_back(std::move(observation));
            output.grbl_wire.push_back(status);
        }
        for (const auto& sensor : config.sensors) {
            if (state.tick % sensor.period_ticks != 0) continue;
            VirtualObservation observation;
            observation.tick = state.tick;
            observation.kind = ObservationKind::Sensor;
            observation.entity_id = sensor.id;
            observation.src = "simulated";
            observation.values_q = {state.position.x, state.position.y, state.position.z};
            Writer bytes;
            for (const auto value : observation.values_q) bytes.integer<std::int64_t>(value);
            observation.content_hash = fnv(bytes.data.data(), bytes.data.size());
            output.observations.push_back(std::move(observation));
        }
        for (const auto& camera : config.cameras) {
            if (state.tick % camera.period_ticks != 0) continue;
            VirtualObservation observation;
            observation.tick = state.tick;
            observation.kind = ObservationKind::Camera;
            observation.entity_id = camera.id;
            observation.src = "simulated";
            observation.image.resize(static_cast<std::size_t>(camera.width) * camera.height);
            for (std::uint32_t y = 0; y < camera.height; ++y) {
                for (std::uint32_t x = 0; x < camera.width; ++x) {
                    const std::uint32_t sx = std::min(config.stock.cells_x - 1, x * config.stock.cells_x / camera.width);
                    const std::uint32_t sy = std::min(config.stock.cells_y - 1, y * config.stock.cells_y / camera.height);
                    const Nm height = state.stock_heights[static_cast<std::size_t>(sy) * config.stock.cells_x + sx];
                    const Nm range = config.stock.top_nm - config.stock.bottom_nm;
                    const auto gray = range == 0 ? 0 : std::clamp<std::int64_t>((height - config.stock.bottom_nm) * 255 / range, 0, 255);
                    observation.image[static_cast<std::size_t>(y) * camera.width + x] = static_cast<std::uint8_t>(gray);
                }
            }
            observation.content_hash = fnv(observation.image.data(), observation.image.size());
            output.observations.push_back(std::move(observation));
        }
    }

    StepResult oneTick() {
        StepResult output;
        if (state.halted) {
            output.tick = state.tick;
            output.sim_ns = state.tick * config.dt_ns;
            output.halted = true;
            output.halt = state.halt;
            output.state_hash = hashState();
            return output;
        }
        ++state.tick;
        for (std::size_t i = 0; i < state.pending.size();) {
            if (state.pending[i].at_tick > state.tick) { ++i; continue; }
            TwinAction action = std::move(state.pending[i]);
            state.pending.erase(state.pending.begin() + static_cast<std::ptrdiff_t>(i));
            if (action.kind == TwinAction::Kind::GcodeLine) state.command_queue.push_back(std::move(action.line));
            else {
                const HaltCause cause = faultHalt(action.fault.type);
                if (cause != HaltCause::None) {
                    output.faults.push_back({state.tick, action.fault.type, cause});
                    state.halted = true;
                    state.halt = cause;
                    state.motion.active = false;
                    state.velocity = {};
                }
            }
        }
        if (!state.halted && !state.motion.active && !state.command_queue.empty()) {
            std::string line = std::move(state.command_queue.front());
            state.command_queue.erase(state.command_queue.begin());
            if (!startLine(std::move(line), output)) {
                output.grbl_wire.emplace_back("error:20");
                const auto parsed_error = device::parsePushMessage(output.grbl_wire.back());
                if (parsed_error && parsed_error.value.kind == device::PushKind::error) {
                    state.halted = true;
                    state.halt = HaltCause::ControllerAlarm;
                }
            }
        }
        if (!state.halted && state.motion.active) {
            const Vec3Nm previous = state.position;
            ++state.motion.elapsed_ticks;
            const auto interpolate = [&](Nm start, Nm end) {
                return wide_math::interpolate(
                    start, end, state.motion.elapsed_ticks, state.motion.total_ticks);
            };
            state.position = {
                interpolate(state.motion.start.x, state.motion.target.x),
                interpolate(state.motion.start.y, state.motion.target.y),
                interpolate(state.motion.start.z, state.motion.target.z),
            };
            state.velocity = {
                wide_math::scaleDifference(
                    state.position.x, previous.x, 1'000'000'000, config.dt_ns),
                wide_math::scaleDifference(
                    state.position.y, previous.y, 1'000'000'000, config.dt_ns),
                wide_math::scaleDifference(
                    state.position.z, previous.z, 1'000'000'000, config.dt_ns),
            };
            collisions(output);
            if (!state.halted) applyRemoval();
            if (!state.halted && state.motion.elapsed_ticks == state.motion.total_ticks) {
                state.motion.active = false;
                state.velocity = {};
            }
        } else if (!state.halted) {
            state.velocity = {};
            collisions(output);
        }
        observe(output);
        const bool pending_gcode = std::any_of(state.pending.begin(), state.pending.end(), [](const TwinAction& action) { return action.kind == TwinAction::Kind::GcodeLine; });
        output.program_complete = state.program_seen && !state.motion.active && state.command_queue.empty() && !pending_gcode;
        output.tick = state.tick;
        output.sim_ns = state.tick * config.dt_ns;
        output.halted = state.halted;
        output.halt = state.halt;
        output.state_hash = hashState();
        return output;
    }
};

DeterministicTwin::DeterministicTwin() : impl_(std::make_unique<Impl>()) {}
DeterministicTwin::~DeterministicTwin() = default;
DeterministicTwin::DeterministicTwin(DeterministicTwin&&) noexcept = default;
DeterministicTwin& DeterministicTwin::operator=(DeterministicTwin&&) noexcept = default;

ResetReport DeterministicTwin::reset(const TwinConfig& input) {
    ResetReport report;
    impl_->configured = false;
    auto refuse = [&](std::string reason) {
        report.refusal = std::move(reason);
        return report;
    };
    if (input.schema != 1) return refuse("unsupported twin schema");
    if (std::find(kLegalDt.begin(), kLegalDt.end(), input.dt_ns) == kLegalDt.end()) return refuse("tick interval is not supported");
    if (input.artifact_hash.empty()) return refuse("artifact hash is required");
    if (input.machine.id.empty()) return refuse("machine identity is required");
    for (const auto& axis : input.machine.axes) {
        if (axis.name.empty() || axis.travel_min_nm >= axis.travel_max_nm || axis.start_nm < axis.travel_min_nm || axis.start_nm > axis.travel_max_nm || axis.max_feed_nm_s <= 0 || axis.accel_nm_s2 <= 0)
            return refuse("axis profile is invalid");
    }
    if (input.tool.id.empty() || input.tool.cutter_radius_nm <= 0 || input.tool.flute_length_nm <= 0 || input.tool.holder_radius_nm < input.tool.cutter_radius_nm || input.tool.holder_height_nm <= 0)
        return refuse("tool geometry is invalid");
    if (input.stock.cells_x == 0 || input.stock.cells_y == 0 || input.stock.resolution_nm <= 0 || input.stock.bottom_nm >= input.stock.top_nm)
        return refuse("stock heightfield is invalid");
    if (static_cast<std::uint64_t>(input.stock.cells_x) * input.stock.cells_y > 100'000'000) return refuse("stock heightfield exceeds the bounded cell count");
    impl_->config = input;
    auto duplicateIds = [](const auto& values) {
        for (std::size_t i = 1; i < values.size(); ++i) if (values[i - 1].id == values[i].id) return true;
        return false;
    };
    std::sort(impl_->config.fixtures.begin(), impl_->config.fixtures.end(), [](const auto& a, const auto& b) { return std::tie(a.id, a.name) < std::tie(b.id, b.name); });
    std::sort(impl_->config.sensors.begin(), impl_->config.sensors.end(), [](const auto& a, const auto& b) { return std::tie(a.id, a.name) < std::tie(b.id, b.name); });
    std::sort(impl_->config.cameras.begin(), impl_->config.cameras.end(), [](const auto& a, const auto& b) { return std::tie(a.id, a.name) < std::tie(b.id, b.name); });
    std::sort(impl_->config.faults.begin(), impl_->config.faults.end(), [](const auto& a, const auto& b) { return std::tie(a.at_tick, a.type) < std::tie(b.at_tick, b.type); });
    std::sort(impl_->config.halt_on.begin(), impl_->config.halt_on.end());
    impl_->config.halt_on.erase(std::unique(impl_->config.halt_on.begin(), impl_->config.halt_on.end()), impl_->config.halt_on.end());
    if (duplicateIds(impl_->config.fixtures) || duplicateIds(impl_->config.sensors) || duplicateIds(impl_->config.cameras)) return refuse("world entity ids must be unique within each kind");
    for (const auto& fixture : impl_->config.fixtures) if (fixture.name.empty() || fixture.min_nm.x >= fixture.max_nm.x || fixture.min_nm.y >= fixture.max_nm.y || fixture.min_nm.z >= fixture.max_nm.z) return refuse("fixture envelope is invalid");
    for (const auto& sensor : impl_->config.sensors) if (sensor.name.empty() || sensor.period_ticks <= 0) return refuse("sensor schedule is invalid");
    for (const auto& camera : impl_->config.cameras) if (camera.name.empty() || camera.period_ticks <= 0 || camera.width == 0 || camera.height == 0 || static_cast<std::uint64_t>(camera.width) * camera.height > 16'777'216) return refuse("camera schedule is invalid");
    for (const auto& fault : impl_->config.faults) if (fault.at_tick <= 0 || fault.type == device::RunDiscontinuity::None) return refuse("fault schedule is invalid");

    Writer config_bytes;
    config_bytes.integer<std::uint32_t>(impl_->config.schema); config_bytes.integer<std::int64_t>(impl_->config.dt_ns); config_bytes.integer<std::uint64_t>(impl_->config.seed);
    config_bytes.string(impl_->config.artifact_hash); config_bytes.string(impl_->config.machine.id);
    for (const auto& axis : impl_->config.machine.axes) {
        config_bytes.string(axis.name); config_bytes.integer<Nm>(axis.start_nm); config_bytes.integer<Nm>(axis.travel_min_nm); config_bytes.integer<Nm>(axis.travel_max_nm); config_bytes.integer<NmPerS>(axis.max_feed_nm_s); config_bytes.integer<NmPerS2>(axis.accel_nm_s2);
    }
    config_bytes.string(impl_->config.tool.id); config_bytes.integer<Nm>(impl_->config.tool.cutter_radius_nm); config_bytes.integer<Nm>(impl_->config.tool.flute_length_nm); config_bytes.integer<Nm>(impl_->config.tool.holder_radius_nm); config_bytes.integer<Nm>(impl_->config.tool.holder_bottom_from_tip_nm); config_bytes.integer<Nm>(impl_->config.tool.holder_height_nm);
    config_bytes.integer<std::uint32_t>(impl_->config.stock.cells_x); config_bytes.integer<std::uint32_t>(impl_->config.stock.cells_y); config_bytes.integer<Nm>(impl_->config.stock.resolution_nm); config_bytes.integer<Nm>(impl_->config.stock.origin_nm.x); config_bytes.integer<Nm>(impl_->config.stock.origin_nm.y); config_bytes.integer<Nm>(impl_->config.stock.origin_nm.z); config_bytes.integer<Nm>(impl_->config.stock.bottom_nm); config_bytes.integer<Nm>(impl_->config.stock.top_nm);
    config_bytes.integer<std::uint64_t>(impl_->config.fixtures.size());
    for (const auto& value : impl_->config.fixtures) { config_bytes.integer<std::uint32_t>(value.id); config_bytes.string(value.name); config_bytes.integer<Nm>(value.min_nm.x); config_bytes.integer<Nm>(value.min_nm.y); config_bytes.integer<Nm>(value.min_nm.z); config_bytes.integer<Nm>(value.max_nm.x); config_bytes.integer<Nm>(value.max_nm.y); config_bytes.integer<Nm>(value.max_nm.z); }
    config_bytes.integer<std::uint64_t>(impl_->config.sensors.size()); for (const auto& value : impl_->config.sensors) { config_bytes.integer<std::uint32_t>(value.id); config_bytes.string(value.name); config_bytes.integer<std::int64_t>(value.period_ticks); }
    config_bytes.integer<std::uint64_t>(impl_->config.cameras.size()); for (const auto& value : impl_->config.cameras) { config_bytes.integer<std::uint32_t>(value.id); config_bytes.string(value.name); config_bytes.integer<std::int64_t>(value.period_ticks); config_bytes.integer<std::uint32_t>(value.width); config_bytes.integer<std::uint32_t>(value.height); }
    config_bytes.integer<std::uint64_t>(impl_->config.faults.size()); for (const auto& value : impl_->config.faults) { config_bytes.integer<Tick>(value.at_tick); config_bytes.integer<std::uint8_t>(static_cast<std::uint8_t>(value.type)); }
    config_bytes.integer<std::uint64_t>(impl_->config.halt_on.size()); for (const auto value : impl_->config.halt_on) config_bytes.integer<std::uint8_t>(static_cast<std::uint8_t>(value));
    impl_->config_hash = fnv(config_bytes.data.data(), config_bytes.data.size());
    impl_->state = {};
    impl_->state.position = {impl_->config.machine.axes[0].start_nm, impl_->config.machine.axes[1].start_nm, impl_->config.machine.axes[2].start_nm};
    impl_->state.feed_nm_s = std::min({impl_->config.machine.axes[0].max_feed_nm_s, impl_->config.machine.axes[1].max_feed_nm_s, impl_->config.machine.axes[2].max_feed_nm_s});
    impl_->state.stock_heights.assign(static_cast<std::size_t>(impl_->config.stock.cells_x) * impl_->config.stock.cells_y, impl_->config.stock.top_nm);
    for (const auto& fault : impl_->config.faults) {
        TwinAction action;
        action.kind = TwinAction::Kind::Fault;
        action.at_tick = fault.at_tick;
        action.fault = fault;
        action.submission_order = impl_->state.next_submission++;
        impl_->state.pending.push_back(std::move(action));
    }
    impl_->configured = true;
    report.ok = true;
    report.config_hash = impl_->config_hash;
    report.state_hash = impl_->hashState();
    report.fidelity = impl_->fidelity;
    return report;
}

bool DeterministicTwin::submit(TwinAction action) {
    if (!impl_->configured || impl_->state.halted) return false;
    if (action.at_tick < 0) action.at_tick = impl_->state.tick + 1;
    if (action.at_tick <= impl_->state.tick) return false;
    if (action.kind == TwinAction::Kind::GcodeLine) {
        std::array<char, 1024> storage{};
        device::LineBuffer buffer{storage.data(), storage.size(), 0};
        const auto encoded = device::encodeLine(action.line, buffer);
        if (!encoded) return false;
        action.line.assign(encoded.line);
        action.fault = {};
    } else {
        if (action.fault.type == device::RunDiscontinuity::None) return false;
        action.fault.at_tick = action.at_tick;
    }
    action.submission_order = impl_->state.next_submission++;
    impl_->state.pending.push_back(std::move(action));
    std::stable_sort(impl_->state.pending.begin(), impl_->state.pending.end(), [](const auto& a, const auto& b) {
        return std::tie(a.at_tick, a.kind, a.submission_order) < std::tie(b.at_tick, b.kind, b.submission_order);
    });
    return true;
}

bool DeterministicTwin::submitGcode(std::string_view line, Tick at_tick) {
    TwinAction action;
    action.kind = TwinAction::Kind::GcodeLine;
    action.at_tick = at_tick;
    action.line = std::string(line);
    return submit(std::move(action));
}

bool DeterministicTwin::injectFault(device::RunDiscontinuity type, Tick at_tick) {
    TwinAction action;
    action.kind = TwinAction::Kind::Fault;
    action.at_tick = at_tick;
    action.fault = {at_tick, type};
    return submit(std::move(action));
}

StepResult DeterministicTwin::step(std::int64_t ticks) {
    if (ticks < 1) throw std::invalid_argument("step count must be positive");
    if (!impl_->configured) throw std::logic_error("twin must be reset before stepping");
    StepResult aggregate;
    for (std::int64_t i = 0; i < ticks; ++i) {
        auto current = impl_->oneTick();
        aggregate.tick = current.tick;
        aggregate.sim_ns = current.sim_ns;
        aggregate.state_hash = current.state_hash;
        aggregate.halted = current.halted;
        aggregate.halt = current.halt;
        aggregate.program_complete = current.program_complete;
        aggregate.collisions.insert(aggregate.collisions.end(), current.collisions.begin(), current.collisions.end());
        aggregate.faults.insert(aggregate.faults.end(), current.faults.begin(), current.faults.end());
        aggregate.observations.insert(aggregate.observations.end(), std::make_move_iterator(current.observations.begin()), std::make_move_iterator(current.observations.end()));
        aggregate.grbl_wire.insert(aggregate.grbl_wire.end(), std::make_move_iterator(current.grbl_wire.begin()), std::make_move_iterator(current.grbl_wire.end()));
        if (aggregate.halted) break;
    }
    return aggregate;
}

TwinSnapshot DeterministicTwin::snapshot() const {
    if (!impl_->configured) return {};
    TwinSnapshot snapshot;
    snapshot.config_hash = impl_->config_hash;
    Writer writer;
    writer.string("CHUDWARE_TWIN_STATE");
    writer.integer<std::uint32_t>(snapshot.schema);
    writer.integer<std::uint64_t>(snapshot.config_hash);
    const auto state = impl_->stateBytes(impl_->state);
    writer.integer<std::uint64_t>(state.size());
    writer.data.insert(writer.data.end(), state.begin(), state.end());
    snapshot.bytes = std::move(writer.data);
    return snapshot;
}

bool DeterministicTwin::restore(const TwinSnapshot& snapshot_value) {
    if (!impl_->configured || snapshot_value.schema != 1 || snapshot_value.config_hash != impl_->config_hash) return false;
    Reader reader{snapshot_value.bytes};
    if (reader.string() != "CHUDWARE_TWIN_STATE" || reader.integer<std::uint32_t>() != snapshot_value.schema || reader.integer<std::uint64_t>() != snapshot_value.config_hash) return false;
    const auto size = reader.integer<std::uint64_t>();
    if (!reader.ok || size != snapshot_value.bytes.size() - reader.offset) return false;
    std::vector<std::uint8_t> state_bytes(snapshot_value.bytes.begin() + static_cast<std::ptrdiff_t>(reader.offset), snapshot_value.bytes.end());
    auto decoded = impl_->decodeState(state_bytes);
    if (!decoded) return false;
    impl_->state = std::move(*decoded);
    return true;
}

std::uint64_t DeterministicTwin::stateHash() const { return impl_->configured ? impl_->hashState() : 0; }
Tick DeterministicTwin::tick() const { return impl_->state.tick; }
Vec3Nm DeterministicTwin::toolTip() const { return impl_->state.position; }
std::int64_t DeterministicTwin::stockHeightSumNm() const {
    return wide_math::saturatingDifferenceSum(
        impl_->state.stock_heights, impl_->config.stock.bottom_nm);
}
const FidelityDeclaration& DeterministicTwin::fidelity() const { return impl_->fidelity; }
ProceduralFact DeterministicTwin::proceduralFact(std::string check_name) const {
    return {std::move(check_name), impl_->config.artifact_hash, impl_->config_hash, stateHash(), "named check ran against the bound artifact, configuration, and snapshot"};
}

std::uint64_t TwinTrace::determinismHash() const { const auto text = serialize(); return fnv(text); }

std::string TwinTrace::serialize() const {
    nlohmann::json root;
    root["schema"] = schema;
    root["initial"] = {{"schema", initial.schema}, {"config_hash", initial.config_hash}, {"bytes_hex", hex(initial.bytes)}};
    root["actions"] = nlohmann::json::array();
    for (const auto& action : actions) {
        root["actions"].push_back({
            {"at_tick", action.at_tick},
            {"kind", action.kind == TwinAction::Kind::GcodeLine ? "gcode" : "fault"},
            {"line", action.line},
            {"fault", static_cast<std::uint8_t>(action.fault.type)},
        });
    }
    root["tick_hashes"] = tick_hashes;
    root["final_hash"] = final_hash;
    return root.dump();
}

TwinTrace TwinTrace::deserialize(std::string_view data) {
    const auto root = nlohmann::json::parse(data);
    TwinTrace result;
    result.schema = root.at("schema").get<std::uint32_t>();
    if (result.schema != 1) throw std::invalid_argument("unsupported trace schema");
    const auto& initial_json = root.at("initial");
    result.initial.schema = initial_json.at("schema").get<std::uint32_t>();
    result.initial.config_hash = initial_json.at("config_hash").get<std::uint64_t>();
    result.initial.bytes = unhex(initial_json.at("bytes_hex").get<std::string>());
    for (const auto& value : root.at("actions")) {
        TwinAction action;
        action.at_tick = value.at("at_tick").get<Tick>();
        const std::string kind = value.at("kind").get<std::string>();
        if (kind == "gcode") action.kind = TwinAction::Kind::GcodeLine;
        else if (kind == "fault") action.kind = TwinAction::Kind::Fault;
        else throw std::invalid_argument("trace action kind is invalid");
        action.line = value.at("line").get<std::string>();
        const auto fault = value.at("fault").get<std::uint8_t>();
        if (fault > static_cast<std::uint8_t>(device::RunDiscontinuity::PowerLoss)) throw std::invalid_argument("trace fault type is invalid");
        action.fault = {action.at_tick, static_cast<device::RunDiscontinuity>(fault)};
        result.actions.push_back(std::move(action));
    }
    result.tick_hashes = root.at("tick_hashes").get<std::vector<std::uint64_t>>();
    result.final_hash = root.at("final_hash").get<std::uint64_t>();
    return result;
}

TwinTrace record(DeterministicTwin& twin, std::vector<TwinAction> actions, std::int64_t ticks) {
    if (ticks < 0) throw std::invalid_argument("record tick count must not be negative");
    TwinTrace trace;
    trace.initial = twin.snapshot();
    trace.actions = actions;
    for (auto action : actions) if (!twin.submit(std::move(action))) throw std::invalid_argument("record action was rejected");
    for (std::int64_t i = 0; i < ticks; ++i) {
        const auto result = twin.step();
        trace.tick_hashes.push_back(result.state_hash);
        if (result.halted) break;
    }
    trace.final_hash = twin.stateHash();
    return trace;
}

ReplayResult replay(DeterministicTwin& twin, const TwinTrace& trace) {
    ReplayResult result;
    if (!twin.restore(trace.initial)) return result;
    for (auto action : trace.actions) if (!twin.submit(std::move(action))) return result;
    for (std::size_t i = 0; i < trace.tick_hashes.size(); ++i) {
        const auto actual = twin.step().state_hash;
        if (actual != trace.tick_hashes[i]) {
            result.divergent_tick = twin.tick();
            result.expected = trace.tick_hashes[i];
            result.actual = actual;
            return result;
        }
    }
    result.actual = twin.stateHash();
    result.expected = trace.final_hash;
    result.matched = result.actual == result.expected;
    return result;
}

TwinEnv::TwinEnv(TwinConfig config, Tick tick_cap) : config_(std::move(config)), tick_cap_(tick_cap) {
    if (tick_cap_ <= 0) throw std::invalid_argument("environment tick cap must be positive");
}
void TwinEnv::seed(std::uint64_t value) { config_.seed = value; }
TwinEnv::Observation TwinEnv::reset() {
    const auto report = twin_.reset(config_);
    if (!report.ok) throw std::invalid_argument(report.refusal);
    return observe();
}
TwinEnv::StepOut TwinEnv::step(TwinAction action) {
    action.at_tick = twin_.tick() + 1;
    if (!twin_.submit(std::move(action))) throw std::invalid_argument("environment action was rejected");
    const auto result = twin_.step();
    return {observe(), result.halted || result.program_complete, twin_.tick() >= tick_cap_ && !result.halted && !result.program_complete, result.halt};
}
TwinEnv::Observation TwinEnv::observe() const {
    return {twin_.tick(), twin_.toolTip(), twin_.stockHeightSumNm(), twin_.stateHash(), "simulated"};
}
const FidelityDeclaration& TwinEnv::fidelity() const { return twin_.fidelity(); }

std::string_view to_string(CollisionClass value) noexcept {
    switch (value) {
        case CollisionClass::RapidIntoStock: return "rapid_into_stock";
        case CollisionClass::ToolHolderIntoFixture: return "tool_holder_into_fixture";
        case CollisionClass::TravelLimitViolation: return "travel_limit_violation";
    }
    return "invalid_collision";
}
std::string_view to_string(HaltCause value) noexcept {
    switch (value) {
        case HaltCause::None: return "none";
        case HaltCause::Collision: return "collision";
        case HaltCause::ControllerReset: return "controller_reset";
        case HaltCause::ControllerAlarm: return "controller_alarm";
        case HaltCause::HardLimit: return "hard_limit";
        case HaltCause::TransportDisconnect: return "transport_disconnect";
        case HaltCause::PowerLoss: return "power_loss";
        case HaltCause::ProgramEnd: return "program_end";
    }
    return "invalid_halt";
}

}  // namespace chudware::twin
