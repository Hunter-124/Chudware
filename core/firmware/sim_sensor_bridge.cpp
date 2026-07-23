#include "firmware/sim_sensor_bridge.h"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <map>
#include <stdexcept>
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
        if (d.contains(key) && d[key].is_array() && !d[key].empty()) {
            for (const auto& v : d[key]) t.push_back(v.get<double>());
            break;
        }
    }
    if (t.empty()) t = {0.0};
    size_t n = t.size();

    auto series = [&](const char* a, const char* b, double dflt) -> std::vector<double> {
        const nlohmann::json* v = nullptr;
        if (d.contains(a)) v = &d[a];
        else if (b && d.contains(b)) v = &d[b];
        if (!v || v->is_null()) return std::vector<double>(n, dflt);
        if (v->is_number()) return std::vector<double>(n, v->get<double>());
        std::vector<double> out;
        for (const auto& x : *v) out.push_back(x.get<double>());
        return out;
    };
    std::vector<double> speeds = series("airspeed_m_s", "velocity_m_s", 0.0);
    if (speeds.size() != n)
        throw std::runtime_error("airspeed/velocity length must match t_s");
    std::vector<double> alts = series("altitude_m", nullptr, 0.0);
    std::vector<double> headings = series("heading_rad", "yaw_rad", 0.0);

    nlohmann::json jd = nlohmann::json::object();
    jd["t_s"] = t;
    nlohmann::json pos = nlohmann::json::array(), vel = nlohmann::json::array(),
                  quat = nlohmann::json::array(), omega = nlohmann::json::array();
    double n_pos = 0.0, e_pos = 0.0;
    for (size_t i = 0; i < n; ++i) {
        double h = headings[i], v = speeds[i];
        double vn = v * std::cos(h), ve = v * std::sin(h);
        if (i > 0) {
            double dt = t[i] - t[i - 1];
            n_pos += vn * dt;
            e_pos += ve * dt;
        }
        pos.push_back({n_pos, e_pos, -alts[i]});
        vel.push_back({vn, ve, 0.0});
        auto q = quat_from_euler_zyx(h, 0.0, 0.0);
        quat.push_back({q[0], q[1], q[2], q[3]});
        omega.push_back({0.0, 0.0, 0.0});
    }
    jd["pos_ned_m"] = pos;
    jd["vel_ned_m_s"] = vel;
    jd["quat_body_from_ned"] = quat;
    jd["omega_body_rad_s"] = omega;
    jd["airspeed_m_s"] = speeds;
    jd["origin_lat_deg"] = d.value("origin_lat_deg", 37.0);
    jd["origin_lon_deg"] = d.value("origin_lon_deg", -122.0);
    jd["origin_alt_m"] = d.value("origin_alt_m", 0.0);
    jd["temperature_c"] = d.value("temperature_c", 25.0);
    return FlightProfile::from_dict(jd);
}

FlightProfile as_profile(const nlohmann::json& p) {
    if (!p.is_object() && !p.is_array())
        throw std::invalid_argument(
            "sim_result_or_profile must be a FlightProfile, trajectory dict "
            "(t_s + kinematics), list[TrajectorySample], or CFD summary dict");
    if (p.is_object()) {
        if (p.contains("profile") && p["profile"].is_object())
            return FlightProfile::from_dict(p["profile"]);
        if (p.contains("trajectory") && p["trajectory"].is_object())
            return FlightProfile::from_dict(p["trajectory"]);
        if (p.contains("samples") || p.contains("t_s") || p.contains("time_s") ||
            p.contains("altitude_m"))
            return FlightProfile::from_dict(p);
        if (p.contains("velocity_m_s") || p.contains("airspeed_m_s"))
            return cfd_to_profile(p);
    }
    throw std::invalid_argument(
        "sim_result_or_profile must be a FlightProfile, trajectory dict "
        "(t_s + kinematics), list[TrajectorySample], or CFD summary dict");
}

// Nearest-neighbour sample-and-hold onto a uniform grid (deterministic).
std::vector<TrajectorySample> resample(const FlightProfile& profile,
                                       const nlohmann::json& duration_s, double dt_s) {
    if (profile.samples.empty()) throw std::runtime_error("flight profile has no samples");
    auto src = profile.samples;
    std::sort(src.begin(), src.end(), [](const auto& a, const auto& b) { return a.t_s < b.t_s; });
    double t0 = src.front().t_s;
    double t1 = src.back().t_s;
    if (duration_s.is_number()) t1 = t0 + duration_s.get<double>();
    if (t1 < t0) t1 = t0;
    double dt = std::max(1e-4, dt_s);
    size_t n = std::max(1, static_cast<int>(std::floor((t1 - t0) / dt + 1e-9)) + 1);
    std::vector<TrajectorySample> out;
    size_t j = 0;
    for (size_t i = 0; i < n; ++i) {
        double t = t0 + i * dt;
        if (t > t1 + 1e-12) break;
        while (j + 1 < src.size() && src[j + 1].t_s <= t) ++j;
        TrajectorySample s = src[j];
        s.t_s = t;
        out.push_back(s);
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
        samples.push_back({{"t_ms", row["t_ms"].get<double>()}, {"response", resp}});
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
    std::vector<SensorEntry> out;
    int i = 0;
    for (const auto& entry : sensor_map) {
        if (!entry.is_object())
            throw std::invalid_argument("sensor_map[" + std::to_string(i) + "] must be a dict");
        std::string dev = alias_device(entry.value("device", entry.value("model", std::string())));
        if (!kDefaultAddr.count(dev))
            throw std::runtime_error(
                "sensor_map[" + std::to_string(i) + "] unknown device " +
                py_repr(entry.value("device", nlohmann::json())) +
                " (supported: mpu6050/bmp280/lis3mdl/neo6m/pitot + aliases)");
        SensorEntry e;
        e.device = dev;
        e.kind = entry.value("kind", std::string());
        if (e.kind.empty())
            e.kind = (dev == "neo6m" && entry.contains("uart")) ? "uart_rx"
                                                                : "i2c_register_stream";
        e.bus = entry.value("bus", std::string("i2c1"));
        if (entry.contains("address") && !entry["address"].is_null()) {
            if (!entry["address"].is_number_integer())
                throw std::invalid_argument("sensor_map[" + std::to_string(i) +
                                            "].address must be int");
            e.address = entry["address"].get<int>();
        } else {
            e.address = kDefaultAddr.at(dev);
        }
        e.uart = entry.value("uart", nlohmann::json());
        e.name = entry.value("name", nlohmann::json());
        out.push_back(e);
        ++i;
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
            tracks.push_back(stream_track(dev, ent.bus, ent.address, bmp280_series(samples),
                                          ent.name));
        } else if (dev == "lis3mdl") {
            tracks.push_back(stream_track(dev, ent.bus, ent.address, lis3mdl_series(samples),
                                          ent.name));
        } else if (dev == "pitot") {
            tracks.push_back(stream_track(dev, ent.bus, ent.address,
                                          pitot_series(samples, airspeed), ent.name));
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
