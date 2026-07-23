#include "firmware/sensor_models.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <map>
#include <stdexcept>

namespace chudware::firmware {
namespace {

// MPU6050 registers.
constexpr int kMpuWhoAmI = 0x68;
constexpr int kMpuRegAccelXoutH = 0x3B;
constexpr int kMpuRegTempOutH = 0x41;
constexpr int kMpuRegGyroXoutH = 0x43;
constexpr int kMpuRegWhoAmI = 0x75;

// BMP280.
constexpr int kBmpChipId = 0x58;
constexpr int kBmpRegId = 0xD0;
constexpr int kBmpRegPressMsb = 0xF7;
constexpr int kBmpRegTempMsb = 0xFA;

// Fixed synthetic calibration (deterministic; representative Bosch sample).
struct BmpCal {
    int dig_T1 = 27504, dig_T2 = 26435, dig_T3 = -1000;
    int dig_P1 = 36477, dig_P2 = -10685, dig_P3 = 3024;
    int dig_P4 = 2855, dig_P5 = 140, dig_P6 = -7;
    int dig_P7 = 15500, dig_P8 = -14600, dig_P9 = 6000;
};
const BmpCal kBmpCal;

// LIS3MDL.
constexpr int kLisWhoAmI = 0x3D;
constexpr int kLisRegWhoAmI = 0x0F;
constexpr int kLisRegOutXL = 0x28;

int clamp_i16(double v) {
    return std::max(-32768, std::min(32767, static_cast<int>(std::lround(v))));
}

std::pair<int, int> i16_le(double v) {
    int i = clamp_i16(v);
    if (i < 0) i = (1 << 16) + i;
    return {i & 0xFF, (i >> 8) & 0xFF};
}

std::array<int, 3> u24_be(long long v) {
    v = std::max(0LL, std::min(0xFFFFFFLL, v));
    return {static_cast<int>((v >> 16) & 0xFF), static_cast<int>((v >> 8) & 0xFF),
            static_cast<int>(v & 0xFF)};
}

// ── IMU (MPU6050) ───────────────────────────────────────────────────────────

std::array<double, 3> imu_specific_force_body_g(const TrajectorySample& s) {
    std::array<double, 3> proper = {s.accel_ned_m_s2[0], s.accel_ned_m_s2[1],
                                    s.accel_ned_m_s2[2] - kG0};
    auto fb = ned_to_body(s.quat_body_from_ned, proper);
    return {fb[0] / kG0, fb[1] / kG0, fb[2] / kG0};
}

std::array<double, 3> imu_gyro_body_dps(const TrajectorySample& s) {
    return {s.omega_body_rad_s[0] * kRad2Deg, s.omega_body_rad_s[1] * kRad2Deg,
            s.omega_body_rad_s[2] * kRad2Deg};
}

int mpu6050_temp_raw(double temp_c) { return clamp_i16((temp_c - 36.53) * 340.0); }

std::map<int, int> mpu6050_registers(const TrajectorySample& s) {
    auto [ax, ay, az] = imu_specific_force_body_g(s);
    auto [gx, gy, gz] = imu_gyro_body_dps(s);
    std::map<int, int> regs;
    regs[kMpuRegWhoAmI] = kMpuWhoAmI;
    const std::pair<int, double> pairs[7] = {
        {kMpuRegAccelXoutH, ax * kMpu6050AccelLsbPerG},
        {kMpuRegAccelXoutH + 2, ay * kMpu6050AccelLsbPerG},
        {kMpuRegAccelXoutH + 4, az * kMpu6050AccelLsbPerG},
        {kMpuRegTempOutH, static_cast<double>(mpu6050_temp_raw(s.temperature_c))},
        {kMpuRegGyroXoutH, gx * kMpu6050GyroLsbPerDps},
        {kMpuRegGyroXoutH + 2, gy * kMpu6050GyroLsbPerDps},
        {kMpuRegGyroXoutH + 4, gz * kMpu6050GyroLsbPerDps},
    };
    for (const auto& [reg, raw] : pairs) {
        int raw_i = reg == kMpuRegTempOutH ? static_cast<int>(raw) : clamp_i16(raw);
        if (raw_i < 0) raw_i = (1 << 16) + raw_i;
        regs[reg] = (raw_i >> 8) & 0xFF;
        regs[reg + 1] = raw_i & 0xFF;
    }
    return regs;
}

// ── BMP280 ──────────────────────────────────────────────────────────────────

std::pair<long long, double> bmp280_tfine(long long adc_t, const BmpCal& cal) {
    double var1 = ((adc_t / 16384.0) - (cal.dig_T1 / 1024.0)) * cal.dig_T2;
    double var2 = (((adc_t / 131072.0) - (cal.dig_T1 / 8192.0)) *
                   ((adc_t / 131072.0) - (cal.dig_T1 / 8192.0))) *
                  cal.dig_T3;
    long long t_fine = static_cast<long long>(var1 + var2);
    return {t_fine, t_fine / 5120.0};
}

double bmp280_compensate_p(long long adc_p, long long t_fine, const BmpCal& cal) {
    double var1 = t_fine / 2.0 - 64000.0;
    double var2 = var1 * var1 * cal.dig_P6 / 32768.0;
    var2 = var2 + var1 * cal.dig_P5 * 2.0;
    var2 = var2 / 4.0 + cal.dig_P4 * 65536.0;
    var1 = (cal.dig_P3 * var1 * var1 / 524288.0 + cal.dig_P2 * var1) / 524288.0;
    var1 = (1.0 + var1 / 32768.0) * cal.dig_P1;
    if (std::fabs(var1) < 1e-12) return 0.0;
    double p = 1048576.0 - adc_p;
    p = (p - (var2 / 4096.0)) * 6250.0 / var1;
    var1 = cal.dig_P9 * p * p / 2147483648.0;
    var2 = p * cal.dig_P8 / 32768.0;
    return p + (var1 + var2 + cal.dig_P7) / 16.0;
}

// Search adc_P (and a matching adc_T) so compensate ≈ target (deterministic).
std::pair<long long, long long> bmp280_find_adc(double pressure_pa, double temp_c,
                                                const BmpCal& cal) {
    long long lo = 0, hi = 1LL << 20;
    for (int i = 0; i < 24; ++i) {
        long long mid = (lo + hi) / 2;
        auto [tf, tc] = bmp280_tfine(mid, cal);
        if (tc < temp_c) lo = mid;
        else hi = mid;
    }
    long long adc_t = (lo + hi) / 2;
    auto [t_fine, _tc] = bmp280_tfine(adc_t, cal);

    lo = 0;
    hi = 1LL << 20;
    for (int i = 0; i < 28; ++i) {
        long long mid = (lo + hi) / 2;
        double p = bmp280_compensate_p(mid, t_fine, cal);
        if (p > pressure_pa) lo = mid;
        else hi = mid;
    }
    return {(lo + hi) / 2, adc_t};
}

std::map<int, int> bmp280_registers(double altitude_m, double temperature_c, const BmpCal& cal) {
    double p_pa = isa_pressure_pa(altitude_m);
    auto [adc_p, adc_t] = bmp280_find_adc(p_pa, temperature_c, cal);
    std::map<int, int> regs;
    regs[kBmpRegId] = kBmpChipId;
    auto pb = u24_be(adc_p << 4);
    for (int i = 0; i < 3; ++i) regs[kBmpRegPressMsb + i] = pb[i];
    auto tb = u24_be(adc_t << 4);
    for (int i = 0; i < 3; ++i) regs[kBmpRegTempMsb + i] = tb[i];
    return regs;
}

// ── LIS3MDL ─────────────────────────────────────────────────────────────────

std::array<double, 3> mag_field_body_gauss(double heading_rad, double pitch_rad,
                                           double roll_rad) {
    constexpr double inclination_rad = 60.0 * kDeg2Rad;
    double h = kEarthFieldGauss * std::cos(inclination_rad);
    double v = kEarthFieldGauss * std::sin(inclination_rad);
    auto q = quat_from_euler_zyx(heading_rad, pitch_rad, roll_rad);
    return ned_to_body(q, {h, 0.0, v});
}

std::map<int, int> lis3mdl_registers(double heading_rad, double pitch_rad, double roll_rad) {
    auto [bx, by, bz] = mag_field_body_gauss(heading_rad, pitch_rad, roll_rad);
    std::map<int, int> regs;
    regs[kLisRegWhoAmI] = kLisWhoAmI;
    const double raws[3] = {bx, by, bz};
    for (int i = 0; i < 3; ++i) {
        auto [lo, hi] = i16_le(raws[i] * kLis3mdlLsbPerGauss);
        regs[kLisRegOutXL + 2 * i] = lo;
        regs[kLisRegOutXL + 2 * i + 1] = hi;
    }
    return regs;
}

// ── GNSS (NEO-6M NMEA) ─────────────────────────────────────────────────────

std::string nmea_checksum(const std::string& body) {
    unsigned c = 0;
    for (char ch : body) c ^= static_cast<unsigned char>(ch);
    char buf[4];
    std::snprintf(buf, sizeof(buf), "%02X", c);
    return buf;
}

std::pair<std::string, std::string> nmea_lat(double lat_deg) {
    std::string hemi = lat_deg >= 0 ? "N" : "S";
    double lat = std::fabs(lat_deg);
    int deg = static_cast<int>(lat);
    double minutes = (lat - deg) * 60.0;
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%02d%07.4f", deg, minutes);
    return {buf, hemi};
}

std::pair<std::string, std::string> nmea_lon(double lon_deg) {
    std::string hemi = lon_deg >= 0 ? "E" : "W";
    double lon = std::fabs(lon_deg);
    int deg = static_cast<int>(lon);
    double minutes = (lon - deg) * 60.0;
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%03d%07.4f", deg, minutes);
    return {buf, hemi};
}

nlohmann::json gnss_fix(const TrajectorySample& s, double origin_lat_deg,
                        double origin_lon_deg, double origin_alt_m) {
    double dn = s.pos_ned_m[0], de = s.pos_ned_m[1], dd = s.pos_ned_m[2];
    double lat = origin_lat_deg + (dn / kREarth) * kRad2Deg;
    double lon = origin_lon_deg +
                 (de / (kREarth * std::cos(origin_lat_deg * kDeg2Rad))) * kRad2Deg;
    double alt = origin_alt_m - dd;
    double vn = s.vel_ned_m_s[0], ve = s.vel_ned_m_s[1], vd = s.vel_ned_m_s[2];
    double sog = std::hypot(vn, ve);
    double cog = std::fmod(std::atan2(ve, vn) * kRad2Deg, 360.0);
    if (cog < 0) cog += 360.0;
    return {
        {"t_s", s.t_s},       {"lat_deg", lat}, {"lon_deg", lon}, {"alt_m", alt},
        {"vel_ned_m_s", {vn, ve, vd}},          {"sog_m_s", sog}, {"sog_knots", sog * 1.943844},
        {"cog_deg", cog},
    };
}

std::vector<std::string> gnss_nmea_sentences(const nlohmann::json& fix,
                                             const std::string& utc = "120000.00") {
    auto [lat_s, ns] = nmea_lat(fix["lat_deg"].get<double>());
    auto [lon_s, ew] = nmea_lon(fix["lon_deg"].get<double>());
    char buf[256];
    std::snprintf(buf, sizeof(buf), "GPGGA,%s,%s,%s,%s,%s,1,08,1.0,%.1f,M,0.0,M,,",
                  utc.c_str(), lat_s.c_str(), ns.c_str(), lon_s.c_str(), ew.c_str(),
                  fix["alt_m"].get<double>());
    std::string gga = buf;
    std::snprintf(buf, sizeof(buf), "GPRMC,%s,A,%s,%s,%s,%s,%.2f,%.2f,010126,,,A", utc.c_str(),
                  lat_s.c_str(), ns.c_str(), lon_s.c_str(), ew.c_str(),
                  fix["sog_knots"].get<double>(), fix["cog_deg"].get<double>());
    std::string rmc = buf;
    return {"$" + gga + "*" + nmea_checksum(gga), "$" + rmc + "*" + nmea_checksum(rmc)};
}

// ── pitot ───────────────────────────────────────────────────────────────────

double airspeed_from_ned(const TrajectorySample& s) {
    return std::sqrt(s.vel_ned_m_s[0] * s.vel_ned_m_s[0] +
                     s.vel_ned_m_s[1] * s.vel_ned_m_s[1] +
                     s.vel_ned_m_s[2] * s.vel_ned_m_s[2]);
}

double pitot_dynamic_pressure_pa(double airspeed_m_s, double rho = kPitotRho0) {
    return 0.5 * rho * airspeed_m_s * airspeed_m_s;
}

int pitot_raw_counts(double airspeed_m_s) {
    double q = pitot_dynamic_pressure_pa(airspeed_m_s);
    return std::max(0, std::min(4095, static_cast<int>(
                       std::lround(4095.0 * q / kPitotQFullscalePa))));
}

}  // namespace

std::array<double, 4> quat_normalize(const std::array<double, 4>& q) {
    double n = std::sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
    if (n < 1e-15) return {1.0, 0.0, 0.0, 0.0};
    return {q[0] / n, q[1] / n, q[2] / n, q[3] / n};
}

std::array<double, 4> quat_from_euler_zyx(double yaw_rad, double pitch_rad, double roll_rad) {
    double cy = std::cos(yaw_rad * 0.5), sy = std::sin(yaw_rad * 0.5);
    double cp = std::cos(pitch_rad * 0.5), sp = std::sin(pitch_rad * 0.5);
    double cr = std::cos(roll_rad * 0.5), sr = std::sin(roll_rad * 0.5);
    return quat_normalize({cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy,
                           cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy});
}

std::array<double, 3> quat_rotate(const std::array<double, 4>& q_in,
                                  const std::array<double, 3>& v) {
    auto q = quat_normalize(q_in);
    double w = q[0], x = q[1], y = q[2], z = q[3];
    double vx = v[0], vy = v[1], vz = v[2];
    double tx = 2.0 * (y * vz - z * vy);
    double ty = 2.0 * (z * vx - x * vz);
    double tz = 2.0 * (x * vy - y * vx);
    return {vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz),
            vz + w * tz + (x * ty - y * tx)};
}

std::array<double, 4> quat_conj(const std::array<double, 4>& q) {
    auto n = quat_normalize(q);
    return {n[0], -n[1], -n[2], -n[3]};
}

std::array<double, 3> ned_to_body(const std::array<double, 4>& q_body_from_ned,
                                  const std::array<double, 3>& v_ned) {
    return quat_rotate(quat_conj(q_body_from_ned), v_ned);
}

FlightProfile FlightProfile::from_dict(const nlohmann::json& d) {
    if (!d.is_object())
        throw std::invalid_argument("flight profile must be a dict or FlightProfile");
    FlightProfile out;
    auto origins = [&]() {
        out.origin_lat_deg = d.value("origin_lat_deg", 37.0);
        out.origin_lon_deg = d.value("origin_lon_deg", -122.0);
        out.origin_alt_m = d.value("origin_alt_m", 0.0);
    };
    std::vector<double> t;
    for (const char* key : {"t_s", "time_s"}) {
        if (d.contains(key) && d[key].is_array() && !d[key].empty()) {
            for (const auto& v : d[key]) t.push_back(v.get<double>());
            break;
        }
    }
    if (t.empty()) throw std::runtime_error("flight profile needs non-empty 't_s' (or 'samples')");
    size_t n = t.size();

    auto col = [&](std::initializer_list<const char*> keys,
                   std::initializer_list<const char*> alt_keys) -> std::vector<nlohmann::json> {
        std::vector<const char*> all(keys);
        all.insert(all.end(), alt_keys.begin(), alt_keys.end());
        for (const char* k : all) {
            if (d.contains(k) && !d[k].is_null()) {
                const nlohmann::json& vals = d[k];
                if (vals.is_number())
                    return std::vector<nlohmann::json>(n, vals.get<double>());
                if (!vals.is_array() || vals.size() != n)
                    throw std::runtime_error("profile '" + std::string(k) + "' length " +
                                             std::to_string(vals.is_array() ? vals.size() : 0) +
                                             " != len(t_s)=" + std::to_string(n));
                return vals.get<std::vector<nlohmann::json>>();
            }
        }
        return {};
    };

    auto vec3 = [](const nlohmann::json& v, const std::array<double, 3>& dflt) {
        if (!v.is_array() || v.size() < 3) return dflt;
        return std::array<double, 3>{v[0].get<double>(), v[1].get<double>(), v[2].get<double>()};
    };
    auto vec4 = [](const nlohmann::json& v) {
        if (!v.is_array() || v.size() < 4)
            return std::array<double, 4>{1.0, 0.0, 0.0, 0.0};
        return std::array<double, 4>{v[0].get<double>(), v[1].get<double>(), v[2].get<double>(),
                                     v[3].get<double>()};
    };

    auto pos = col({"pos_ned_m"}, {"position_ned_m", "pos"});
    // altitude-only profiles: pos_ned = (0,0,-alt)
    if (d.contains("altitude_m") && !d.contains("pos_ned_m") && !d.contains("position_ned_m")) {
        pos.clear();
        const nlohmann::json& alts = d["altitude_m"];
        if (alts.is_number()) {
            for (size_t i = 0; i < n; ++i)
                pos.push_back({0.0, 0.0, -alts.get<double>()});
        } else {
            for (const auto& a : alts) pos.push_back({0.0, 0.0, -a.get<double>()});
        }
    }
    auto vel = col({"vel_ned_m_s"}, {"velocity_ned_m_s", "vel"});
    auto acc = col({"accel_ned_m_s2"}, {"acceleration_ned_m_s2"});
    auto omega = col({"omega_body_rad_s"}, {"omega_rad_s", "gyro_rad_s"});
    auto temp = col({"temperature_c"}, {"temp_c"});

    std::vector<nlohmann::json> quats;
    if (d.contains("quat_body_from_ned") || d.contains("quat")) {
        quats = col({"quat_body_from_ned"}, {"quat"});
    } else {
        auto yaw = col({"yaw_rad"}, {"heading_rad"});
        auto pitch = col({"pitch_rad"}, {});
        auto roll = col({"roll_rad"}, {});
        auto deg_col = [&](const char* key, std::vector<nlohmann::json>& c) {
            if (!d.contains(key)) return;
            c.clear();
            const nlohmann::json& vals = d[key];
            if (vals.is_number()) {
                for (size_t i = 0; i < n; ++i) c.push_back(vals.get<double>() * kDeg2Rad);
            } else {
                for (const auto& v : vals) c.push_back(v.get<double>() * kDeg2Rad);
            }
        };
        deg_col("yaw_deg", yaw);
        deg_col("pitch_deg", pitch);
        deg_col("roll_deg", roll);
        auto scalar_at = [](const std::vector<nlohmann::json>& c, size_t i, double dflt) {
            if (c.empty()) return dflt;
            return c[i].is_number() ? c[i].get<double>() : dflt;
        };
        for (size_t i = 0; i < n; ++i) {
            auto q = quat_from_euler_zyx(scalar_at(yaw, i, 0.0), scalar_at(pitch, i, 0.0),
                                         scalar_at(roll, i, 0.0));
            quats.push_back({q[0], q[1], q[2], q[3]});
        }
    }

    auto scalar_at = [](const std::vector<nlohmann::json>& c, size_t i, double dflt) {
        if (c.empty()) return dflt;
        if (c[i].is_number()) return c[i].get<double>();
        if (c[i].is_array() && !c[i].empty()) return c[i][0].get<double>();
        return dflt;
    };
    for (size_t i = 0; i < n; ++i) {
        TrajectorySample s;
        s.t_s = t[i];
        s.pos_ned_m = vec3(pos.empty() ? nlohmann::json() : pos[i], {0.0, 0.0, 0.0});
        s.vel_ned_m_s = vec3(vel.empty() ? nlohmann::json() : vel[i], {0.0, 0.0, 0.0});
        s.accel_ned_m_s2 = vec3(acc.empty() ? nlohmann::json() : acc[i], {0.0, 0.0, 0.0});
        s.quat_body_from_ned = quats.empty() ? std::array<double, 4>{1, 0, 0, 0}
                                             : vec4(quats[i]);
        s.omega_body_rad_s = vec3(omega.empty() ? nlohmann::json() : omega[i], {0.0, 0.0, 0.0});
        s.temperature_c = scalar_at(temp, i, 25.0);
        out.samples.push_back(s);
    }
    if (d.contains("airspeed_m_s") && !d["airspeed_m_s"].is_null()) {
        std::vector<double> airspeed;
        const nlohmann::json& a = d["airspeed_m_s"];
        if (a.is_number()) {
            airspeed.assign(n, a.get<double>());
        } else {
            for (const auto& v : a) airspeed.push_back(v.get<double>());
        }
        out.airspeed_m_s = airspeed;
    }
    origins();
    return out;
}

double isa_pressure_pa(double altitude_m) {
    double h = std::max(-500.0, std::min(11000.0, altitude_m));
    return kIsaP0Pa * std::pow(1.0 - kIsaL * h / kIsaT0K, kIsaExp);
}

double isa_temperature_c(double altitude_m) {
    double h = std::max(-500.0, std::min(11000.0, altitude_m));
    return (kIsaT0K - kIsaL * h) - 273.15;
}

nlohmann::json mpu6050_series(const std::vector<TrajectorySample>& samples) {
    nlohmann::json out = nlohmann::json::array();
    for (const auto& s : samples) {
        auto regs = mpu6050_registers(s);
        nlohmann::json registers = nlohmann::json::object();
        for (const auto& [k, v] : regs) registers[std::to_string(k)] = v;
        nlohmann::json response = nlohmann::json::array();
        for (int i = 0; i < 14; ++i) response.push_back(regs[kMpuRegAccelXoutH + i]);
        auto [ax, ay, az] = imu_specific_force_body_g(s);
        auto [gx, gy, gz] = imu_gyro_body_dps(s);
        out.push_back({
            {"t_s", s.t_s},           {"t_ms", s.t_s * 1000.0},
            {"device", "mpu6050"},    {"registers", registers},
            {"response", response},   {"accel_g", {ax, ay, az}},
            {"gyro_dps", {gx, gy, gz}},
        });
    }
    return out;
}

nlohmann::json bmp280_series(const std::vector<TrajectorySample>& samples) {
    nlohmann::json out = nlohmann::json::array();
    for (const auto& s : samples) {
        double alt = -s.pos_ned_m[2];  // NED down → altitude up
        double p = isa_pressure_pa(alt);
        auto regs = bmp280_registers(alt, s.temperature_c, kBmpCal);
        nlohmann::json registers = nlohmann::json::object();
        for (const auto& [k, v] : regs) registers[std::to_string(k)] = v;
        nlohmann::json response = nlohmann::json::array();
        for (int i = 0; i < 6; ++i) response.push_back(regs[kBmpRegPressMsb + i]);
        out.push_back({
            {"t_s", s.t_s},        {"t_ms", s.t_s * 1000.0}, {"device", "bmp280"},
            {"altitude_m", alt},   {"pressure_pa", p},       {"registers", registers},
            {"response", response},
        });
    }
    return out;
}

nlohmann::json lis3mdl_series(const std::vector<TrajectorySample>& samples) {
    nlohmann::json out = nlohmann::json::array();
    for (const auto& s : samples) {
        auto q = quat_normalize(s.quat_body_from_ned);
        double w = q[0], x = q[1], y = q[2], z = q[3];
        double sinp = std::max(-1.0, std::min(1.0, 2.0 * (w * y - z * x)));
        double pitch = std::asin(sinp);
        double roll = std::atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y));
        double yaw = std::atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z));
        auto field = mag_field_body_gauss(yaw, pitch, roll);
        auto regs = lis3mdl_registers(yaw, pitch, roll);
        nlohmann::json registers = nlohmann::json::object();
        for (const auto& [k, v] : regs) registers[std::to_string(k)] = v;
        nlohmann::json response = nlohmann::json::array();
        for (int i = 0; i < 6; ++i) response.push_back(regs[kLisRegOutXL + i]);
        out.push_back({
            {"t_s", s.t_s},        {"t_ms", s.t_s * 1000.0},
            {"device", "lis3mdl"}, {"heading_rad", yaw},
            {"field_gauss", {field[0], field[1], field[2]}},
            {"registers", registers},
            {"response", response},
        });
    }
    return out;
}

nlohmann::json gnss_series(const std::vector<TrajectorySample>& samples,
                           double origin_lat_deg, double origin_lon_deg, double origin_alt_m) {
    nlohmann::json out = nlohmann::json::array();
    for (const auto& s : samples) {
        nlohmann::json fix = gnss_fix(s, origin_lat_deg, origin_lon_deg, origin_alt_m);
        auto sentences = gnss_nmea_sentences(fix);
        std::string uart_text;
        for (const auto& sent : sentences) uart_text += sent + "\r\n";
        nlohmann::json response = nlohmann::json::array();
        for (unsigned char ch : uart_text) response.push_back(static_cast<int>(ch));
        out.push_back({
            {"t_s", s.t_s},      {"t_ms", s.t_s * 1000.0}, {"device", "neo6m"},
            {"fix", fix},        {"nmea", sentences},      {"response", response},
            {"uart_text", uart_text},
        });
    }
    return out;
}

nlohmann::json pitot_series(const std::vector<TrajectorySample>& samples,
                            const std::optional<std::vector<double>>& airspeed_m_s) {
    nlohmann::json out = nlohmann::json::array();
    for (size_t i = 0; i < samples.size(); ++i) {
        const auto& s = samples[i];
        double tas = airspeed_m_s ? (*airspeed_m_s)[std::min(i, airspeed_m_s->size() - 1)]
                                  : airspeed_from_ned(s);
        int counts = pitot_raw_counts(tas);
        int raw16 = counts << 4;
        int b0 = (raw16 >> 8) & 0xFF, b1 = raw16 & 0xFF;
        out.push_back({
            {"t_s", s.t_s},      {"t_ms", s.t_s * 1000.0},  {"device", "pitot"},
            {"airspeed_m_s", tas},
            {"q_pa", pitot_dynamic_pressure_pa(tas)},
            {"counts", counts},  {"response", {b0, b1}},
            {"registers", {{"0", b0}, {"1", b1}}},
        });
    }
    return out;
}

}  // namespace chudware::firmware
