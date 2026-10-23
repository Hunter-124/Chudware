// Physics-accurate multi-axis sensor models for firmware emulation stimulus
// (C++ port of engine_oracle/firmware/sensor_models.py). Pure, deterministic:
// kinematic time-series → raw device register bytes for MPU6050 / BMP280 /
// LIS3MDL / NEO-6M (NMEA) / pitot. See the Python module docstring.
#pragma once

#include <array>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

constexpr double kG0 = 9.80665;
constexpr double kREarth = 6371000.0;
constexpr double kDeg2Rad = 3.14159265358979323846 / 180.0;
constexpr double kRad2Deg = 180.0 / 3.14159265358979323846;

constexpr double kIsaP0Pa = 101325.0;
constexpr double kIsaT0K = 288.15;
constexpr double kIsaL = 0.0065;
constexpr double kIsaR = 287.05287;
constexpr double kIsaExp = kG0 / (kIsaR * kIsaL);

constexpr double kMpu6050AccelLsbPerG = 16384.0;
constexpr double kMpu6050GyroLsbPerDps = 131.0;
constexpr double kLis3mdlLsbPerGauss = 6842.0;
constexpr double kEarthFieldGauss = 0.50;

constexpr double kPitotRho0 = 1.225;
constexpr double kPitotQFullscalePa = 2500.0;

struct TrajectorySample {
    double t_s = 0.0;
    std::array<double, 3> pos_ned_m{0.0, 0.0, 0.0};
    std::array<double, 3> vel_ned_m_s{0.0, 0.0, 0.0};
    std::array<double, 3> accel_ned_m_s2{0.0, 0.0, 0.0};
    std::array<double, 4> quat_body_from_ned{1.0, 0.0, 0.0, 0.0};
    std::array<double, 3> omega_body_rad_s{0.0, 0.0, 0.0};
    double temperature_c = 25.0;
};

struct FlightProfile {
    std::vector<TrajectorySample> samples;
    std::optional<std::vector<double>> airspeed_m_s;
    double origin_lat_deg = 37.0;
    double origin_lon_deg = -122.0;
    double origin_alt_m = 0.0;

    // From a dict of parallel time-series arrays (mirrors
    // sensor_models.FlightProfile.from_dict). Throws std::invalid_argument /
    // std::runtime_error on malformed input (the RPC maps these to
    // invalid_request, like Python's TypeError/ValueError).
    static FlightProfile from_dict(const nlohmann::json& d);
};

// Quaternion helpers (w,x,y,z).
std::array<double, 4> quat_normalize(const std::array<double, 4>& q);
std::array<double, 4> quat_from_euler_zyx(double yaw_rad, double pitch_rad, double roll_rad);
std::array<double, 3> quat_rotate(const std::array<double, 4>& q,
                                  const std::array<double, 3>& v);
std::array<double, 4> quat_conj(const std::array<double, 4>& q);
std::array<double, 3> ned_to_body(const std::array<double, 4>& q_body_from_ned,
                                  const std::array<double, 3>& v_ned);

// ISA troposphere.
double isa_pressure_pa(double altitude_m);
double isa_temperature_c(double altitude_m);

// Per-sample sensor series (each row is a dict with t_s/t_ms/device/response
// plus model-specific fields). Barometer/pitot convert NED down to absolute
// altitude using origin_alt_m; pitot density follows ISA pressure and sample temperature.
nlohmann::json mpu6050_series(const std::vector<TrajectorySample>& samples);
nlohmann::json bmp280_series(const std::vector<TrajectorySample>& samples,
                             double origin_alt_m = 0.0);
nlohmann::json lis3mdl_series(const std::vector<TrajectorySample>& samples);
nlohmann::json gnss_series(const std::vector<TrajectorySample>& samples,
                           double origin_lat_deg, double origin_lon_deg, double origin_alt_m);
nlohmann::json pitot_series(const std::vector<TrajectorySample>& samples,
                            const std::optional<std::vector<double>>& airspeed_m_s,
                            double origin_alt_m = 0.0);

}  // namespace chudware::firmware
