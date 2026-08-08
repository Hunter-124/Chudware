#include "firmware/sim_sensor_bridge.h"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <map>
#include <stdexcept>
#include <set>
#include <utility>
#include <vector>

#include "firmware/pyfmt.h"
#include "firmware/scenario.h"

namespace chudware::firmware {
namespace {

// Default 7-bit addresses matching common breakout / flight-computer wiring.
const std::map<std::string, int> kDefaultAddr = {
    {"mpu6050", 0x68}, {"bmp280", 0x76}, {"lis3mdl", 0x1C},
    {"pitot", 0x28},   {"neo6m", 0x42},
};
constexpr size_t kMaxScenarioSamples = 10000;

std::string alias_device(const std::string& name) {
    std::string n = name;
    std::transform(n.begin(), n.end(), n.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    static const std::map<std::string, std::string> kAliases = {
        {"imu", "mpu6050"},       {"mpu-6050", "mpu6050"}, {"mpu6000", "mpu6050"},
        {"baro", "bmp280"},       {"barometer", "bmp280"}, {"bmp-280", "bmp280"},
        {"mag", "lis3mdl"},       {"magnetometer", "lis3mdl"},
        {"gnss", "neo6m"},        {"gps", "neo6m"},        {"neo-6m", "neo6m"},
        {"airspeed", "pitot"},
    };
    auto it = kAliases.find(n);
    return it != kAliases.end() ? it->second : n;
}

// Map a thin CFD/flight-speed summary onto a FlightProfile dict shape, then parse.
FlightProfile cfd_to_profile(const nlohmann::json& d) {
    std::vector<double> t;
    for (const char* key : {"t_s", "time_s"}) {
        if (!d.contains(key) || d[key].is_null()) continue;
        if (!d[key].is_array() || d[key].empty())
            throw std::invalid_argument(std::string(key) + " must be a non-empty array");
        for (const auto& value : d[key]) {
            if (!value.is_number())
                throw std::invalid_argument(std::string(key) + " must contain numeric seconds");
            t.push_back(value.get<double>());
        }
        break;
    }
    if (t.empty()) t = {0.0};
    const size_t n = t.size();
    if (n > 100000)
        throw std::invalid_argument("CFD profile exceeds the 100000-sample input limit");

    auto series = [&](const char* primary, const char* alternate,
                      double dflt) -> std::vector<double> {
        const nlohmann::json* value = nullptr;
        if (d.contains(primary)) value = &d[primary];
        else if (alternate && d.contains(alternate)) value = &d[alternate];
        if (!value || value->is_null()) return std::vector<double>(n, dflt);
        if (value->is_number()) return std::vector<double>(n, value->get<double>());
        if (!value->is_array() || value->size() != n)
            throw std::invalid_argument(std::string(primary) + " length must match t_s");
        std::vector<double> out;
        out.reserve(n);
        for (const auto& item : *value) {
            if (!item.is_number())
                throw std::invalid_argument(std::string(primary) + " must contain numbers");
            out.push_back(item.get<double>());
        }
        return out;
    };
    std::vector<double> speeds = series("airspeed_m_s", "velocity_m_s", 0.0);
    std::vector<double> alts = series("altitude_m", nullptr, 0.0);
    std::vector<double> headings = series("heading_rad", "yaw_rad", 0.0);
    for (size_t i = 0; i < n; ++i) {
        if (!std::isfinite(t[i]) || t[i] < 0.0 ||
            (i > 0 && t[i] <= t[i - 1]))
            throw std::invalid_argument("CFD profile times must be finite, non-negative, and strictly increasing");
        if (!std::isfinite(speeds[i]) || speeds[i] < 0.0 ||
            !std::isfinite(alts[i]) || !std::isfinite(headings[i]))
            throw std::invalid_argument("CFD speed, altitude, and heading must be finite; speed must be non-negative");
    }

    const double origin_alt_m = d.value("origin_alt_m", 0.0);
    std::vector<std::array<double, 3>> velocities(n);
    for (size_t i = 0; i < n; ++i) {
        double vertical = 0.0;
        if (n > 1) {
            const size_t a = i == 0 ? 0 : i - 1;
            const size_t b = i == 0 ? 1 : i;
            vertical = -(alts[b] - alts[a]) / (t[b] - t[a]);
        }
        velocities[i] = {
            speeds[i] * std::cos(headings[i]),
            speeds[i] * std::sin(headings[i]),
            vertical,
        };
    }

    nlohmann::json jd = nlohmann::json::object();
    jd["t_s"] = t;
    nlohmann::json pos = nlohmann::json::array(), vel = nlohmann::json::array(),
                  accel = nlohmann::json::array(), quat = nlohmann::json::array(),
                  omega = nlohmann::json::array();
    double north_m = 0.0, east_m = 0.0;
    for (size_t i = 0; i < n; ++i) {
        if (i > 0) {
            const double dt = t[i] - t[i - 1];
            north_m += 0.5 * (velocities[i - 1][0] + velocities[i][0]) * dt;
            east_m += 0.5 * (velocities[i - 1][1] + velocities[i][1]) * dt;
        }
        std::array<double, 3> acceleration{0.0, 0.0, 0.0};
        double yaw_rate = 0.0;
        if (n > 1) {
            const size_t a = i == 0 ? 0 : i - 1;
            const size_t b = i == 0 ? 1 : i;
            const double dt = t[b] - t[a];
            for (size_t axis = 0; axis < acceleration.size(); ++axis)
                acceleration[axis] = (velocities[b][axis] - velocities[a][axis]) / dt;
            const double delta = std::atan2(
                std::sin(headings[b] - headings[a]), std::cos(headings[b] - headings[a]));
            yaw_rate = delta / dt;
        }
        pos.push_back({north_m, east_m, origin_alt_m - alts[i]});
        vel.push_back({velocities[i][0], velocities[i][1], velocities[i][2]});
        accel.push_back({acceleration[0], acceleration[1], acceleration[2]});
        auto q = quat_from_euler_zyx(headings[i], 0.0, 0.0);
        quat.push_back({q[0], q[1], q[2], q[3]});
        omega.push_back({0.0, 0.0, yaw_rate});
    }
    jd["pos_ned_m"] = pos;
    jd["vel_ned_m_s"] = vel;
    jd["accel_ned_m_s2"] = accel;
    jd["quat_body_from_ned"] = quat;
    jd["omega_body_rad_s"] = omega;
    jd["airspeed_m_s"] = speeds;
    jd["origin_lat_deg"] = d.value("origin_lat_deg", 37.0);
    jd["origin_lon_deg"] = d.value("origin_lon_deg", -122.0);
    jd["origin_alt_m"] = origin_alt_m;
    jd["temperature_c"] = d.value("temperature_c", 25.0);
    return FlightProfile::from_dict(jd);
}

FlightProfile as_profile(const nlohmann::json& profile) {
    if (!profile.is_object() && !profile.is_array())
        throw std::invalid_argument(
            "sim_result_or_profile must be a FlightProfile, trajectory dict "
            "(t_s + kinematics), list[TrajectorySample], or CFD summary dict");
    if (profile.is_array())
        return FlightProfile::from_dict({{"samples", profile}});
    if (profile.contains("profile") && profile["profile"].is_object())
        return FlightProfile::from_dict(profile["profile"]);
    if (profile.contains("trajectory") && profile["trajectory"].is_object())
        return FlightProfile::from_dict(profile["trajectory"]);
    if (profile.contains("samples") || profile.contains("pos_ned_m") ||
        profile.contains("position_ned_m") || profile.contains("vel_ned_m_s") ||
        profile.contains("accel_ned_m_s2") || profile.contains("quat_body_from_ned") ||
        profile.contains("omega_body_rad_s"))
        return FlightProfile::from_dict(profile);
    if (profile.contains("velocity_m_s") || profile.contains("airspeed_m_s"))
        return cfd_to_profile(profile);
    if (profile.contains("t_s") || profile.contains("time_s") ||
        profile.contains("altitude_m"))
        return FlightProfile::from_dict(profile);
    throw std::invalid_argument(
        "sim_result_or_profile must be a FlightProfile, trajectory dict "
        "(t_s + kinematics), list[TrajectorySample], or CFD summary dict");
}

// Nearest-neighbour sample-and-hold onto a uniform grid (deterministic).
std::vector<TrajectorySample> resample(const FlightProfile& profile,
                                       const nlohmann::json& duration_s, double dt_s) {
    if (profile.samples.empty()) throw std::runtime_error("flight profile has no samples");
    if (!std::isfinite(dt_s) || dt_s <= 0.0)
        throw std::invalid_argument("dt_s must be finite and greater than zero");
    const auto& src = profile.samples;
    const double t0 = src.front().t_s;
    double t1 = src.back().t_s;
    if (!duration_s.is_null()) {
        if (!duration_s.is_number())
            throw std::invalid_argument("duration_s must be a number of seconds");
        const double duration = duration_s.get<double>();
        if (!std::isfinite(duration) || duration < 0.0)
            throw std::invalid_argument("duration_s must be finite and non-negative");
        if (duration > 0.0) t1 = t0 + duration;
    }
    const long double count =
        std::floor(static_cast<long double>(t1 - t0) / dt_s + 1e-9L) + 1.0L;
    if (count > static_cast<long double>(kMaxScenarioSamples))
        throw std::invalid_argument(
            "sensor scenario exceeds the 10000-sample limit; increase dt_s or reduce duration_s");
    const size_t n = static_cast<size_t>(std::max(1.0L, count));
    std::vector<TrajectorySample> out;
    out.reserve(n);
    size_t j = 0;
    for (size_t i = 0; i < n; ++i) {
        const double time_s = t0 + static_cast<double>(i) * dt_s;
        if (time_s > t1 + 1e-12) break;
        while (j + 1 < src.size() && src[j + 1].t_s <= time_s) ++j;
        TrajectorySample sample = src[j];
        sample.t_s = time_s;
        out.push_back(sample);
    }
    return out;
}

std::optional<std::vector<double>> airspeed_series(const FlightProfile& profile,
                                                   const std::vector<TrajectorySample>& samples) {
    if (!profile.airspeed_m_s) return std::nullopt;
    std::vector<double> src_t;
    for (const auto& s : profile.samples) src_t.push_back(s.t_s);
    std::vector<double> out;
    size_t j = 0;
    for (const auto& s : samples) {
        while (j + 1 < src_t.size() && src_t[j + 1] <= s.t_s) ++j;
        out.push_back((*profile.airspeed_m_s)[std::min(j, profile.airspeed_m_s->size() - 1)]);
    }
    return out;
}

nlohmann::json stream_track(const std::string& device, const std::string& bus, int address,
                            const nlohmann::json& series, const nlohmann::json& name) {
    nlohmann::json samples = nlohmann::json::array();
    for (const auto& row : series) {
        nlohmann::json resp = nlohmann::json::array();
        for (const auto& b : row["response"]) resp.push_back(b.get<long long>() & 0xFF);
        nlohmann::json sample = {
            {"t_ms", row["t_ms"].get<double>()},
            {"response", std::move(resp)},
        };
        // Register-addressed drivers select bytes by register, not by read order.
        // Preserve each model's complete map beside its timestamp; the emulator
        // must consume the sample atomically or capability-refuse the stream.
        if (row.contains("registers")) sample["registers"] = row["registers"];
        samples.push_back(std::move(sample));
    }
    nlohmann::json tr = {
        {"kind", "i2c_register_stream"},
        {"bus", bus},
        {"address", address},
        {"device", device},
        {"samples", samples},
    };
    if (name.is_string() && !name.get<std::string>().empty()) tr["name"] = name;
    return tr;
}

struct SensorEntry {
    std::string device;
    std::string kind;
    std::string bus;
    int address;
    nlohmann::json uart;
    nlohmann::json name;
};

std::vector<SensorEntry> normalize_sensor_map(const nlohmann::json& sensor_map) {
    if (!sensor_map.is_array() || sensor_map.empty())
        throw std::runtime_error("sensor_map must list at least one device");
    if (sensor_map.size() > 32)
        throw std::invalid_argument("sensor_map exceeds the 32-device limit");
    std::vector<SensorEntry> out;
    std::set<std::string> endpoints;
    for (size_t i = 0; i < sensor_map.size(); ++i) {
        const auto& entry = sensor_map[i];
        const std::string prefix = "sensor_map[" + std::to_string(i) + "]";
        if (!entry.is_object())
            throw std::invalid_argument(prefix + " must be a dict");
        if (entry.contains("device") && !entry["device"].is_null() &&
            entry.contains("model") && !entry["model"].is_null())
            throw std::invalid_argument(prefix + " must provide device or model, not both");
        nlohmann::json raw_device =
            entry.contains("device") ? entry["device"] : entry.value("model", nlohmann::json());
        if (!raw_device.is_string() || raw_device.get<std::string>().empty())
            throw std::invalid_argument(prefix + ".device must be a non-empty string");
        const std::string dev = alias_device(raw_device.get<std::string>());
        if (!kDefaultAddr.count(dev))
            throw std::runtime_error(
                prefix + " unknown device " + py_repr(raw_device) +
                " (supported: mpu6050/bmp280/lis3mdl/neo6m/pitot + aliases)");

        SensorEntry sensor;
        sensor.device = dev;
        if (entry.contains("kind") && !entry["kind"].is_null()) {
            if (!entry["kind"].is_string())
                throw std::invalid_argument(prefix + ".kind must be a string");
            sensor.kind = entry["kind"].get<std::string>();
        }
        const bool named_uart =
            entry.contains("uart") && entry["uart"].is_string() &&
            !entry["uart"].get<std::string>().empty();
        if (sensor.kind.empty())
            sensor.kind = (dev == "neo6m" && named_uart) ? "uart_rx"
                                                         : "i2c_register_stream";
        if (sensor.kind != "i2c_register_stream" && sensor.kind != "uart_rx")
            throw std::invalid_argument(
                prefix + ".kind must be 'i2c_register_stream' or 'uart_rx'");
        if (sensor.kind == "uart_rx" && dev != "neo6m")
            throw std::invalid_argument(prefix + ": only neo6m supports uart_rx");

        if (entry.contains("bus") && !entry["bus"].is_null()) {
            if (!entry["bus"].is_string() || entry["bus"].get<std::string>().empty())
                throw std::invalid_argument(prefix + ".bus must be a non-empty string");
            sensor.bus = entry["bus"].get<std::string>();
        } else {
            sensor.bus = "i2c1";
        }
        if (entry.contains("address") && !entry["address"].is_null()) {
            if (!entry["address"].is_number_integer())
                throw std::invalid_argument(prefix + ".address must be int");
            sensor.address = entry["address"].get<int>();
        } else {
            sensor.address = kDefaultAddr.at(dev);
        }
        if (sensor.kind == "i2c_register_stream" &&
            (sensor.address < 0x08 || sensor.address > 0x77))
            throw std::invalid_argument(prefix + ".address must be an assignable 7-bit I2C address (0x08..0x77)");

        sensor.uart = entry.value("uart", nlohmann::json());
        if (!sensor.uart.is_null() &&
            (!sensor.uart.is_string() || sensor.uart.get<std::string>().empty()))
            throw std::invalid_argument(prefix + ".uart must be a non-empty string");
        sensor.name = entry.value("name", nlohmann::json());
        if (!sensor.name.is_null() && !sensor.name.is_string())
            throw std::invalid_argument(prefix + ".name must be a string");

        const std::string endpoint =
            sensor.kind == "uart_rx"
                ? "uart:" + (sensor.uart.is_string() ? sensor.uart.get<std::string>() : "default")
                : "i2c:" + sensor.bus + ":" + std::to_string(sensor.address);
        if (!endpoints.insert(endpoint).second)
            throw std::invalid_argument(prefix + " conflicts with another sensor at " + endpoint);
        out.push_back(std::move(sensor));
    }
    return out;
}

}  // namespace

nlohmann::json build_sensor_scenario(const nlohmann::json& sim_result_or_profile,
                                     const nlohmann::json& sensor_map,
                                     const nlohmann::json& duration_s, double dt_s,
                                     const std::string& name) {
    FlightProfile profile = as_profile(sim_result_or_profile);
    auto samples = resample(profile, duration_s, dt_s);
    auto sensors = normalize_sensor_map(sensor_map);
    auto airspeed = airspeed_series(profile, samples);

    nlohmann::json tracks = nlohmann::json::array();
    for (const auto& ent : sensors) {
        const std::string& dev = ent.device;
        if (dev == "mpu6050") {
            tracks.push_back(stream_track(dev, ent.bus, ent.address, mpu6050_series(samples),
                                          ent.name));
        } else if (dev == "bmp280") {
            tracks.push_back(stream_track(
                dev, ent.bus, ent.address,
                bmp280_series(samples, profile.origin_alt_m), ent.name));
        } else if (dev == "lis3mdl") {
            tracks.push_back(stream_track(dev, ent.bus, ent.address, lis3mdl_series(samples),
                                          ent.name));
        } else if (dev == "pitot") {
            tracks.push_back(stream_track(
                dev, ent.bus, ent.address,
                pitot_series(samples, airspeed, profile.origin_alt_m), ent.name));
        } else if (dev == "neo6m") {
            nlohmann::json series = gnss_series(samples, profile.origin_lat_deg,
                                                profile.origin_lon_deg, profile.origin_alt_m);
            if (ent.kind == "uart_rx") {
                nlohmann::json events = nlohmann::json::array();
                for (const auto& row : series)
                    events.push_back(
                        {{"t_ms", row["t_ms"].get<double>()}, {"text", row["uart_text"]}});
                nlohmann::json tr = {{"kind", "uart_rx"}, {"events", events},
                                     {"device", "neo6m"}};
                if (ent.uart.is_string()) tr["uart"] = ent.uart;
                tracks.push_back(tr);
            } else {
                tracks.push_back(stream_track(dev, ent.bus, ent.address, series, ent.name));
            }
        }
    }

    nlohmann::json devices = nlohmann::json::array();
    for (const auto& e : sensors) devices.push_back(e.device);
    nlohmann::json scenario = {
        {"schema", kScenarioSchema},
        {"name", name.empty() ? "sim_sensor_bridge" : name},
        {"tracks", tracks},
        {"meta",
         {{"source", "sim_sensor_bridge"},
          {"n_samples", samples.size()},
          {"dt_s", dt_s},
          {"t0_s", samples.empty() ? 0.0 : samples.front().t_s},
          {"t1_s", samples.empty() ? 0.0 : samples.back().t_s},
          {"devices", devices}}},
    };
    auto errs = validate_scenario(scenario);
    if (!errs.empty()) {
        std::string joined;
        for (const auto& e : errs) {
            if (!joined.empty()) joined += "; ";
            joined += e;
        }
        throw std::runtime_error("bridge produced invalid fwscenario: " + joined);
    }
    return scenario;
}

}  // namespace chudware::firmware
