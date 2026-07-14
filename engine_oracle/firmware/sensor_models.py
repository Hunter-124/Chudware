"""Physics-accurate multi-axis sensor models for firmware emulation stimulus.

Pure, deterministic functions that turn a time-series of physical state (kinematic
trajectory, altitude profile, CFD/flight velocity) into raw device register bytes
for the flight-computer parts pack sensors:

- MPU6050 class 6-axis IMU (accel + gyro)
- BMP280 class barometer (pressure + temperature)
- LIS3MDL class magnetometer
- NEO-6M class GNSS fix (UART NMEA payload; also packable as i2c response bytes)
- Pitot / airspeed (differential-pressure style raw counts)

Each model returns time-stamped register maps / response byte lists with correct
device scaling and word order (big-endian for MPU6050/BMP280; little-endian for
LIS3MDL). No I/O — the sim→firmware bridge (:mod:`engine.firmware.sim_sensor_bridge`)
compiles the outputs into ``aicad.fwscenario/1`` tracks.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

G0 = 9.80665  # m/s² standard gravity
R_EARTH = 6_371_000.0  # mean Earth radius m (GNSS delta lat/lon)
DEG2RAD = math.pi / 180.0
RAD2DEG = 180.0 / math.pi

# ISA troposphere (sea level → 11 km)
ISA_P0_PA = 101_325.0
ISA_T0_K = 288.15
ISA_L = 0.0065  # K/m lapse rate
ISA_R = 287.05287  # J/(kg·K)
ISA_G = G0
ISA_EXP = ISA_G / (ISA_R * ISA_L)  # ≈ 5.25588

# MPU6050 (±2 g / ±250 °/s full-scale — power-on defaults)
MPU6050_ACCEL_LSB_PER_G = 16384.0
MPU6050_GYRO_LSB_PER_DPS = 131.0
MPU6050_WHO_AM_I = 0x68
MPU6050_REG_ACCEL_XOUT_H = 0x3B
MPU6050_REG_TEMP_OUT_H = 0x41
MPU6050_REG_GYRO_XOUT_H = 0x43
MPU6050_REG_WHO_AM_I = 0x75

# BMP280
BMP280_CHIP_ID = 0x58
BMP280_REG_ID = 0xD0
BMP280_REG_PRESS_MSB = 0xF7
BMP280_REG_TEMP_MSB = 0xFA
# Fixed synthetic calibration so compensate(raw) is invertible in tests.
# Dig_T1..P9 from a representative Bosch sample (kept constant → deterministic).
BMP280_CALIB = {
    "dig_T1": 27504, "dig_T2": 26435, "dig_T3": -1000,
    "dig_P1": 36477, "dig_P2": -10685, "dig_P3": 3024,
    "dig_P4": 2855, "dig_P5": 140, "dig_P6": -7,
    "dig_P7": 15500, "dig_P8": -14600, "dig_P9": 6000,
}

# LIS3MDL
LIS3MDL_WHO_AM_I = 0x3D
LIS3MDL_REG_WHO_AM_I = 0x0F
LIS3MDL_REG_OUT_X_L = 0x28
# ±4 gauss FS → 6842 LSB/gauss (datasheet)
LIS3MDL_LSB_PER_GAUSS = 6842.0
# Earth field magnitude ~0.5 gauss at mid-latitudes
EARTH_FIELD_GAUSS = 0.50

# Pitot (generic 12-bit ADC counts for dynamic pressure; 0..4095)
PITOT_RHO0 = 1.225  # kg/m³ ISA sea-level
PITOT_Q_FULLSCALE_PA = 2500.0  # ~ full-scale dynamic pressure (~64 m/s / 125 kt)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _clamp_i16(v: float) -> int:
    return max(-32768, min(32767, int(round(v))))


def _clamp_u16(v: float) -> int:
    return max(0, min(65535, int(round(v))))


def _i16_be(v: int) -> tuple[int, int]:
    v = _clamp_i16(v)
    if v < 0:
        v = (1 << 16) + v
    return ((v >> 8) & 0xFF, v & 0xFF)


def _i16_le(v: int) -> tuple[int, int]:
    v = _clamp_i16(v)
    if v < 0:
        v = (1 << 16) + v
    return (v & 0xFF, (v >> 8) & 0xFF)


def _u24_be(v: int) -> tuple[int, int, int]:
    v = max(0, min(0xFFFFFF, int(v)))
    return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)


def quat_normalize(q: Sequence[float]) -> tuple[float, float, float, float]:
    """Return unit quaternion (w, x, y, z). Zero input → identity."""
    w, x, y, z = (float(q[0]), float(q[1]), float(q[2]), float(q[3]))
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-15:
        return (1.0, 0.0, 0.0, 0.0)
    return (w / n, x / n, y / n, z / n)


def quat_from_euler_zyx(yaw_rad: float, pitch_rad: float, roll_rad: float
                        ) -> tuple[float, float, float, float]:
    """Body←NED rotation as quaternion (w,x,y,z) from ZYX yaw/pitch/roll (rad)."""
    cy, sy = math.cos(yaw_rad * 0.5), math.sin(yaw_rad * 0.5)
    cp, sp = math.cos(pitch_rad * 0.5), math.sin(pitch_rad * 0.5)
    cr, sr = math.cos(roll_rad * 0.5), math.sin(roll_rad * 0.5)
    w = cr * cp * cy + sr * sp * sy
    x = sr * cp * cy - cr * sp * sy
    y = cr * sp * cy + sr * cp * sy
    z = cr * cp * sy - sr * sp * cy
    return quat_normalize((w, x, y, z))


def quat_rotate(q: Sequence[float], v: Sequence[float]
                ) -> tuple[float, float, float]:
    """Rotate vector *v* by unit quaternion *q* (w,x,y,z)."""
    w, x, y, z = quat_normalize(q)
    vx, vy, vz = float(v[0]), float(v[1]), float(v[2])
    # q * (0,v) * q_conj
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def quat_conj(q: Sequence[float]) -> tuple[float, float, float, float]:
    w, x, y, z = quat_normalize(q)
    return (w, -x, -y, -z)


def ned_to_body(q_body_from_ned: Sequence[float], v_ned: Sequence[float]
                ) -> tuple[float, float, float]:
    """Express a NED vector in the body frame. *q* rotates body→NED, so body = q* · v_ned."""
    return quat_rotate(quat_conj(q_body_from_ned), v_ned)


# ---------------------------------------------------------------------------
# Trajectory / profile containers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrajectorySample:
    """One kinematic sample.

    Frames (aerospace convention):
    - NED position/velocity: +N, +E, +D (down positive).
    - Body: +X forward, +Y right, +Z down.
    - ``quat_body_from_ned`` rotates a body vector into NED (body→NED).
    - ``omega_body_rad_s``: body angular rate (rad/s).
    - ``accel_ned_m_s2``: inertial acceleration of the origin in NED (m/s²),
      NOT specific force — gravity is applied by the IMU model.
    """
    t_s: float
    pos_ned_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    vel_ned_m_s: tuple[float, float, float] = (0.0, 0.0, 0.0)
    accel_ned_m_s2: tuple[float, float, float] = (0.0, 0.0, 0.0)
    quat_body_from_ned: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    omega_body_rad_s: tuple[float, float, float] = (0.0, 0.0, 0.0)
    temperature_c: float = 25.0


@dataclass
class FlightProfile:
    """Analytic / tabulated flight profile used by the bridge.

    Accepts either a list of :class:`TrajectorySample` or a dict of parallel
    time-series arrays (the shape CFD / motion solvers typically emit)::

        {
          "t_s": [...],
          "pos_ned_m": [[n,e,d], ...],          # or altitude_m + lat/lon
          "vel_ned_m_s": [[vn,ve,vd], ...],
          "accel_ned_m_s2": [[an,ae,ad], ...],  # optional
          "quat_body_from_ned": [[w,x,y,z], ...],  # or yaw/pitch/roll_rad
          "omega_body_rad_s": [[p,q,r], ...],
          "temperature_c": [...] | scalar,
          "airspeed_m_s": [...],                # optional pitot override
        }
    """
    samples: list[TrajectorySample] = field(default_factory=list)
    airspeed_m_s: Optional[list[float]] = None
    origin_lat_deg: float = 37.0
    origin_lon_deg: float = -122.0
    origin_alt_m: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "FlightProfile":
        if not isinstance(d, dict):
            raise TypeError("flight profile must be a dict or FlightProfile")
        if "samples" in d and d["samples"] and isinstance(d["samples"][0], TrajectorySample):
            return cls(
                samples=list(d["samples"]),
                airspeed_m_s=d.get("airspeed_m_s"),
                origin_lat_deg=float(d.get("origin_lat_deg", 37.0)),
                origin_lon_deg=float(d.get("origin_lon_deg", -122.0)),
                origin_alt_m=float(d.get("origin_alt_m", 0.0)),
            )
        t = list(d.get("t_s") or d.get("time_s") or [])
        if not t:
            raise ValueError("flight profile needs non-empty 't_s' (or 'samples')")
        n = len(t)

        def _col(key: str, default: Sequence[float], alt_keys: Sequence[str] = ()) -> list:
            for k in (key, *alt_keys):
                if k in d and d[k] is not None:
                    vals = d[k]
                    # Scalars broadcast to the full timeline for any channel
                    # (heading, temperature, airspeed-as-scalar, …).
                    if isinstance(vals, (int, float)):
                        return [float(vals)] * n
                    if len(vals) != n:
                        raise ValueError(f"profile '{k}' length {len(vals)} != len(t_s)={n}")
                    return list(vals)
            return [default] * n

        pos = _col("pos_ned_m", (0.0, 0.0, 0.0), ("position_ned_m", "pos"))
        # altitude-only profiles: pos_ned = (0,0,-alt) if alt given as positive-up
        if "altitude_m" in d and "pos_ned_m" not in d and "position_ned_m" not in d:
            alts = d["altitude_m"]
            if isinstance(alts, (int, float)):
                alts = [float(alts)] * n
            pos = [(0.0, 0.0, -float(a)) for a in alts]

        vel = _col("vel_ned_m_s", (0.0, 0.0, 0.0), ("velocity_ned_m_s", "vel"))
        acc = _col("accel_ned_m_s2", (0.0, 0.0, 0.0), ("acceleration_ned_m_s2",))
        omega = _col("omega_body_rad_s", (0.0, 0.0, 0.0), ("omega_rad_s", "gyro_rad_s"))
        temp = _col("temperature_c", 25.0, ("temp_c",))

        if "quat_body_from_ned" in d or "quat" in d:
            quats = _col("quat_body_from_ned", (1.0, 0.0, 0.0, 0.0), ("quat",))
        else:
            yaw = _col("yaw_rad", 0.0, ("heading_rad",))
            pitch = _col("pitch_rad", 0.0)
            roll = _col("roll_rad", 0.0)
            # allow degrees
            if "yaw_deg" in d:
                yaw = [float(v) * DEG2RAD for v in (
                    d["yaw_deg"] if not isinstance(d["yaw_deg"], (int, float))
                    else [d["yaw_deg"]] * n)]
            if "pitch_deg" in d:
                pitch = [float(v) * DEG2RAD for v in (
                    d["pitch_deg"] if not isinstance(d["pitch_deg"], (int, float))
                    else [d["pitch_deg"]] * n)]
            if "roll_deg" in d:
                roll = [float(v) * DEG2RAD for v in (
                    d["roll_deg"] if not isinstance(d["roll_deg"], (int, float))
                    else [d["roll_deg"]] * n)]
            quats = [quat_from_euler_zyx(float(yaw[i]), float(pitch[i]), float(roll[i]))
                     for i in range(n)]

        samples: list[TrajectorySample] = []
        for i in range(n):
            p = pos[i]
            v = vel[i]
            a = acc[i]
            q = quats[i]
            o = omega[i]
            tc = temp[i]
            samples.append(TrajectorySample(
                t_s=float(t[i]),
                pos_ned_m=(float(p[0]), float(p[1]), float(p[2])),
                vel_ned_m_s=(float(v[0]), float(v[1]), float(v[2])),
                accel_ned_m_s2=(float(a[0]), float(a[1]), float(a[2])),
                quat_body_from_ned=(float(q[0]), float(q[1]), float(q[2]), float(q[3])),
                omega_body_rad_s=(float(o[0]), float(o[1]), float(o[2])),
                temperature_c=float(tc) if not isinstance(tc, (list, tuple)) else float(tc[0]),
            ))
        airspeed = d.get("airspeed_m_s")
        if airspeed is not None and isinstance(airspeed, (int, float)):
            airspeed = [float(airspeed)] * n
        return cls(
            samples=samples,
            airspeed_m_s=list(airspeed) if airspeed is not None else None,
            origin_lat_deg=float(d.get("origin_lat_deg", 37.0)),
            origin_lon_deg=float(d.get("origin_lon_deg", -122.0)),
            origin_alt_m=float(d.get("origin_alt_m", 0.0)),
        )


# ---------------------------------------------------------------------------
# Atmosphere / ISA
# ---------------------------------------------------------------------------

def isa_pressure_pa(altitude_m: float) -> float:
    """ISA troposphere static pressure (Pa) at geometric altitude *altitude_m*
    (positive up). Valid roughly −500 m … 11 000 m; clamped outside that band
    by evaluating at the nearest endpoint (deterministic, no raise)."""
    h = max(-500.0, min(11_000.0, float(altitude_m)))
    return ISA_P0_PA * (1.0 - ISA_L * h / ISA_T0_K) ** ISA_EXP


def isa_temperature_c(altitude_m: float) -> float:
    h = max(-500.0, min(11_000.0, float(altitude_m)))
    return (ISA_T0_K - ISA_L * h) - 273.15


def isa_altitude_m(pressure_pa: float) -> float:
    """Inverse ISA: pressure → altitude (m, positive up)."""
    p = max(1.0, float(pressure_pa))
    return (ISA_T0_K / ISA_L) * (1.0 - (p / ISA_P0_PA) ** (1.0 / ISA_EXP))


# ---------------------------------------------------------------------------
# IMU — MPU6050
# ---------------------------------------------------------------------------

def imu_specific_force_body_g(sample: TrajectorySample
                              ) -> tuple[float, float, float]:
    """Accelerometer-measured specific force in body frame, units of g.

    ``f_b = R_body←ned · (a_ned − g_ned) / g0`` with ``g_ned = (0, 0, +g0)``
    (down positive). At rest (a=0, level): f_b = (0, 0, −1) in NED-expressed
    body axes when body = NED — wait: a_ned−g_ned = (0,0,−g0), so f = −1 g
    along +Z_down, i.e. the accelerometer reports −1 g on the down axis in
    the *specific-force* convention.

    Flight-computer firmware usually wants "up is +1 g at rest". The MPU6050
    datasheet: "the accelerometer output will be +1 g when the sensing axis is
    pointed straight up". With body +Z down, rest reading is −1 g on Z. We
    keep that physical convention; tests assert free-fall ≈ 0 and static |Z| ≈ 1.
    """
    g_ned = (0.0, 0.0, G0)
    a = sample.accel_ned_m_s2
    proper = (a[0] - g_ned[0], a[1] - g_ned[1], a[2] - g_ned[2])
    fb = ned_to_body(sample.quat_body_from_ned, proper)
    return (fb[0] / G0, fb[1] / G0, fb[2] / G0)


def imu_gyro_body_dps(sample: TrajectorySample) -> tuple[float, float, float]:
    o = sample.omega_body_rad_s
    return (o[0] * RAD2DEG, o[1] * RAD2DEG, o[2] * RAD2DEG)


def mpu6050_temp_raw(temp_c: float) -> int:
    # TEMP_degC = Raw/340 + 36.53  ⇒  Raw = (T − 36.53) * 340
    return _clamp_i16((float(temp_c) - 36.53) * 340.0)


def mpu6050_registers(sample: TrajectorySample) -> dict[int, int]:
    """Full MPU6050 register map snapshot (selected regs) for one sample."""
    ax, ay, az = imu_specific_force_body_g(sample)
    gx, gy, gz = imu_gyro_body_dps(sample)
    regs: dict[int, int] = {
        MPU6050_REG_WHO_AM_I: MPU6050_WHO_AM_I,
    }
    pairs = [
        (MPU6050_REG_ACCEL_XOUT_H, ax * MPU6050_ACCEL_LSB_PER_G),
        (MPU6050_REG_ACCEL_XOUT_H + 2, ay * MPU6050_ACCEL_LSB_PER_G),
        (MPU6050_REG_ACCEL_XOUT_H + 4, az * MPU6050_ACCEL_LSB_PER_G),
        (MPU6050_REG_TEMP_OUT_H, mpu6050_temp_raw(sample.temperature_c)),
        (MPU6050_REG_GYRO_XOUT_H, gx * MPU6050_GYRO_LSB_PER_DPS),
        (MPU6050_REG_GYRO_XOUT_H + 2, gy * MPU6050_GYRO_LSB_PER_DPS),
        (MPU6050_REG_GYRO_XOUT_H + 4, gz * MPU6050_GYRO_LSB_PER_DPS),
    ]
    for reg, raw in pairs:
        raw_i = int(raw) if reg == MPU6050_REG_TEMP_OUT_H else _clamp_i16(raw)
        if raw_i < 0:
            raw_i = (1 << 16) + raw_i
        regs[reg] = (raw_i >> 8) & 0xFF
        regs[reg + 1] = raw_i & 0xFF
    return regs


def mpu6050_burst_response(sample: TrajectorySample) -> list[int]:
    """14-byte burst starting at ACCEL_XOUT_H (0x3B): ax,ay,az,temp,gx,gy,gz."""
    regs = mpu6050_registers(sample)
    return [regs[MPU6050_REG_ACCEL_XOUT_H + i] for i in range(14)]


def mpu6050_series(samples: Sequence[TrajectorySample]
                   ) -> list[dict[str, Any]]:
    """Time-stamped MPU6050 register snapshots."""
    out = []
    for s in samples:
        out.append({
            "t_s": s.t_s,
            "t_ms": s.t_s * 1000.0,
            "device": "mpu6050",
            "registers": mpu6050_registers(s),
            "response": mpu6050_burst_response(s),
            "accel_g": imu_specific_force_body_g(s),
            "gyro_dps": imu_gyro_body_dps(s),
        })
    return out


# ---------------------------------------------------------------------------
# Barometer — BMP280
# ---------------------------------------------------------------------------

def _bmp280_tfine(adc_t: int, cal: dict) -> tuple[int, float]:
    dig_T1, dig_T2, dig_T3 = cal["dig_T1"], cal["dig_T2"], cal["dig_T3"]
    var1 = (((adc_t / 16384.0) - (dig_T1 / 1024.0)) * dig_T2)
    var2 = ((((adc_t / 131072.0) - (dig_T1 / 8192.0)) *
             ((adc_t / 131072.0) - (dig_T1 / 8192.0))) * dig_T3)
    t_fine = int(var1 + var2)
    t_c = t_fine / 5120.0
    return t_fine, t_c


def _bmp280_compensate_p(adc_p: int, t_fine: int, cal: dict) -> float:
    dig = cal
    var1 = t_fine / 2.0 - 64000.0
    var2 = var1 * var1 * dig["dig_P6"] / 32768.0
    var2 = var2 + var1 * dig["dig_P5"] * 2.0
    var2 = var2 / 4.0 + dig["dig_P4"] * 65536.0
    var1 = (dig["dig_P3"] * var1 * var1 / 524288.0 + dig["dig_P2"] * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * dig["dig_P1"]
    if abs(var1) < 1e-12:
        return 0.0
    p = 1048576.0 - adc_p
    p = (p - (var2 / 4096.0)) * 6250.0 / var1
    var1 = dig["dig_P9"] * p * p / 2147483648.0
    var2 = p * dig["dig_P8"] / 32768.0
    return p + (var1 + var2 + dig["dig_P7"]) / 16.0


def _bmp280_find_adc(pressure_pa: float, temp_c: float,
                     cal: dict = BMP280_CALIB) -> tuple[int, int]:
    """Search adc_P (and a matching adc_T) so compensate ≈ target (deterministic)."""
    # Invert temperature roughly: dig_T2 dominates near room temp.
    # adc_T ≈ (T_c * 5120 related) — binary-search adc_T for t_c.
    lo, hi = 0, 1 << 20
    target_t = float(temp_c)
    for _ in range(24):
        mid = (lo + hi) // 2
        _, tc = _bmp280_tfine(mid, cal)
        if tc < target_t:
            lo = mid
        else:
            hi = mid
    adc_t = (lo + hi) // 2
    t_fine, _ = _bmp280_tfine(adc_t, cal)

    lo, hi = 0, 1 << 20
    target_p = float(pressure_pa)
    for _ in range(28):
        mid = (lo + hi) // 2
        p = _bmp280_compensate_p(mid, t_fine, cal)
        # compensate_p decreases as adc_p increases
        if p > target_p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) // 2, adc_t


def bmp280_registers(altitude_m: float, temperature_c: Optional[float] = None,
                     cal: dict = BMP280_CALIB) -> dict[int, int]:
    """BMP280 register snapshot for a geometric altitude (m, positive up)."""
    if temperature_c is None:
        temperature_c = isa_temperature_c(altitude_m)
    p_pa = isa_pressure_pa(altitude_m)
    adc_p, adc_t = _bmp280_find_adc(p_pa, temperature_c, cal)
    regs: dict[int, int] = {BMP280_REG_ID: BMP280_CHIP_ID}
    for i, b in enumerate(_u24_be(adc_p << 4)):  # 20-bit left-aligned in 24
        regs[BMP280_REG_PRESS_MSB + i] = b
    for i, b in enumerate(_u24_be(adc_t << 4)):
        regs[BMP280_REG_TEMP_MSB + i] = b
    # also stash true pressure for tests (not a device reg — callers use physics API)
    return regs


def bmp280_burst_response(altitude_m: float, temperature_c: Optional[float] = None,
                          cal: dict = BMP280_CALIB) -> list[int]:
    """6-byte burst at 0xF7: press_msb,press_lsb,press_xlsb,temp_msb,temp_lsb,temp_xlsb."""
    regs = bmp280_registers(altitude_m, temperature_c, cal)
    return [regs[BMP280_REG_PRESS_MSB + i] for i in range(6)]


def bmp280_series(samples: Sequence[TrajectorySample],
                  cal: dict = BMP280_CALIB) -> list[dict[str, Any]]:
    out = []
    for s in samples:
        alt = -s.pos_ned_m[2]  # NED down → altitude up
        p = isa_pressure_pa(alt)
        out.append({
            "t_s": s.t_s,
            "t_ms": s.t_s * 1000.0,
            "device": "bmp280",
            "altitude_m": alt,
            "pressure_pa": p,
            "registers": bmp280_registers(alt, s.temperature_c, cal),
            "response": bmp280_burst_response(alt, s.temperature_c, cal),
        })
    return out


def bmp280_compensate(regs_or_response: Any, cal: dict = BMP280_CALIB
                      ) -> tuple[float, float]:
    """Return (pressure_pa, temp_c) from a register map or 6-byte burst."""
    if isinstance(regs_or_response, dict):
        b = [regs_or_response[BMP280_REG_PRESS_MSB + i] for i in range(6)]
    else:
        b = list(regs_or_response)
    adc_p = ((b[0] << 16) | (b[1] << 8) | b[2]) >> 4
    adc_t = ((b[3] << 16) | (b[4] << 8) | b[5]) >> 4
    t_fine, t_c = _bmp280_tfine(adc_t, cal)
    p = _bmp280_compensate_p(adc_p, t_fine, cal)
    return p, t_c


# ---------------------------------------------------------------------------
# Magnetometer — LIS3MDL
# ---------------------------------------------------------------------------

def mag_field_body_gauss(heading_rad: float, pitch_rad: float = 0.0,
                         roll_rad: float = 0.0,
                         field_gauss: float = EARTH_FIELD_GAUSS,
                         inclination_rad: float = 60.0 * DEG2RAD
                         ) -> tuple[float, float, float]:
    """Dipole-like local field in body frame from heading (yaw) + attitude.

    NED field: horizontal component points to magnetic north (≈ true north
    here — no declination), vertical component down for positive inclination
    (northern hemisphere).
    """
    h = field_gauss * math.cos(inclination_rad)
    v = field_gauss * math.sin(inclination_rad)
    # magnetic north in NED = (h, 0, v) when heading=0 means nose north;
    # rotate by −heading about down to put north into the horizontal plane.
    # Actually: field_ned = (h * cos(dec), h * sin(dec), v) with dec=0 → (h,0,v).
    # Body from NED via yaw/pitch/roll.
    field_ned = (h, 0.0, v)
    q = quat_from_euler_zyx(heading_rad, pitch_rad, roll_rad)
    return ned_to_body(q, field_ned)


def lis3mdl_registers(heading_rad: float, pitch_rad: float = 0.0,
                      roll_rad: float = 0.0) -> dict[int, int]:
    bx, by, bz = mag_field_body_gauss(heading_rad, pitch_rad, roll_rad)
    regs: dict[int, int] = {LIS3MDL_REG_WHO_AM_I: LIS3MDL_WHO_AM_I}
    for i, raw in enumerate((bx, by, bz)):
        lo, hi = _i16_le(raw * LIS3MDL_LSB_PER_GAUSS)
        base = LIS3MDL_REG_OUT_X_L + 2 * i
        # LIS3MDL has a dummy byte between axes when reading with auto-incr
        # on some modes; the OUT registers are X_L,X_H, Y_L,Y_H, Z_L,Z_H at
        # 0x28..0x2D (no gap in the data regs themselves).
        regs[base] = lo
        regs[base + 1] = hi
    return regs


def lis3mdl_burst_response(heading_rad: float, pitch_rad: float = 0.0,
                           roll_rad: float = 0.0) -> list[int]:
    regs = lis3mdl_registers(heading_rad, pitch_rad, roll_rad)
    return [regs[LIS3MDL_REG_OUT_X_L + i] for i in range(6)]


def lis3mdl_series(samples: Sequence[TrajectorySample]) -> list[dict[str, Any]]:
    out = []
    for s in samples:
        q = s.quat_body_from_ned
        # extract yaw/pitch/roll from quaternion (ZYX)
        w, x, y, z = quat_normalize(q)
        sinp = 2.0 * (w * y - z * x)
        sinp = max(-1.0, min(1.0, sinp))
        pitch = math.asin(sinp)
        roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
        yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
        field = mag_field_body_gauss(yaw, pitch, roll)
        out.append({
            "t_s": s.t_s,
            "t_ms": s.t_s * 1000.0,
            "device": "lis3mdl",
            "heading_rad": yaw,
            "field_gauss": field,
            "registers": lis3mdl_registers(yaw, pitch, roll),
            "response": lis3mdl_burst_response(yaw, pitch, roll),
        })
    return out


# ---------------------------------------------------------------------------
# GNSS — NEO-6M (NMEA RMC+GGA payload)
# ---------------------------------------------------------------------------

def _nmea_checksum(body: str) -> str:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"{c:02X}"


def _nmea_lat(lat_deg: float) -> tuple[str, str]:
    hemi = "N" if lat_deg >= 0 else "S"
    lat = abs(lat_deg)
    deg = int(lat)
    minutes = (lat - deg) * 60.0
    return f"{deg:02d}{minutes:07.4f}", hemi


def _nmea_lon(lon_deg: float) -> tuple[str, str]:
    hemi = "E" if lon_deg >= 0 else "W"
    lon = abs(lon_deg)
    deg = int(lon)
    minutes = (lon - deg) * 60.0
    return f"{deg:03d}{minutes:07.4f}", hemi


def gnss_fix(sample: TrajectorySample, *,
              origin_lat_deg: float = 37.0,
              origin_lon_deg: float = -122.0,
              origin_alt_m: float = 0.0) -> dict[str, Any]:
    """GNSS fix from a NED trajectory sample relative to an origin LLA."""
    dn, de, dd = sample.pos_ned_m
    lat = origin_lat_deg + (dn / R_EARTH) * RAD2DEG
    lon = origin_lon_deg + (de / (R_EARTH * math.cos(origin_lat_deg * DEG2RAD))) * RAD2DEG
    alt = origin_alt_m - dd
    vn, ve, vd = sample.vel_ned_m_s
    sog_m_s = math.hypot(vn, ve)
    sog_knots = sog_m_s * 1.943844
    cog_deg = (math.atan2(ve, vn) * RAD2DEG) % 360.0
    return {
        "t_s": sample.t_s,
        "lat_deg": lat,
        "lon_deg": lon,
        "alt_m": alt,
        "vel_ned_m_s": (vn, ve, vd),
        "sog_m_s": sog_m_s,
        "sog_knots": sog_knots,
        "cog_deg": cog_deg,
    }


def gnss_nmea_sentences(fix: dict[str, Any], *, utc_hhmmss: str = "120000.00"
                        ) -> list[str]:
    """Return GGA + RMC NMEA sentences for a fix dict."""
    lat_s, ns = _nmea_lat(fix["lat_deg"])
    lon_s, ew = _nmea_lon(fix["lon_deg"])
    alt = fix["alt_m"]
    sog = fix["sog_knots"]
    cog = fix["cog_deg"]
    gga_body = (f"GPGGA,{utc_hhmmss},{lat_s},{ns},{lon_s},{ew},"
                f"1,08,1.0,{alt:.1f},M,0.0,M,,")
    rmc_body = (f"GPRMC,{utc_hhmmss},A,{lat_s},{ns},{lon_s},{ew},"
                f"{sog:.2f},{cog:.2f},010126,,,A")
    out = []
    for body in (gga_body, rmc_body):
        out.append(f"${body}*{_nmea_checksum(body)}")
    return out


def gnss_series(samples: Sequence[TrajectorySample], *,
                origin_lat_deg: float = 37.0,
                origin_lon_deg: float = -122.0,
                origin_alt_m: float = 0.0) -> list[dict[str, Any]]:
    out = []
    for s in samples:
        fix = gnss_fix(s, origin_lat_deg=origin_lat_deg,
                        origin_lon_deg=origin_lon_deg, origin_alt_m=origin_alt_m)
        sentences = gnss_nmea_sentences(fix)
        payload = ("\r\n".join(sentences) + "\r\n").encode("ascii")
        out.append({
            "t_s": s.t_s,
            "t_ms": s.t_s * 1000.0,
            "device": "neo6m",
            "fix": fix,
            "nmea": sentences,
            "response": list(payload),  # for i2c_register_stream packing
            "uart_text": "\r\n".join(sentences) + "\r\n",
        })
    return out


# ---------------------------------------------------------------------------
# Pitot / airspeed
# ---------------------------------------------------------------------------

def airspeed_from_ned(sample: TrajectorySample) -> float:
    """True airspeed proxy = |v_ned| (no wind)."""
    v = sample.vel_ned_m_s
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def pitot_dynamic_pressure_pa(airspeed_m_s: float, rho: float = PITOT_RHO0) -> float:
    return 0.5 * rho * float(airspeed_m_s) ** 2


def pitot_raw_counts(airspeed_m_s: float, rho: float = PITOT_RHO0,
                     q_fullscale_pa: float = PITOT_Q_FULLSCALE_PA) -> int:
    """12-bit counts for dynamic pressure (0 at rest → 4095 at q_fullscale)."""
    q = pitot_dynamic_pressure_pa(airspeed_m_s, rho)
    return max(0, min(4095, int(round(4095.0 * q / max(q_fullscale_pa, 1e-9)))))


def pitot_series(samples: Sequence[TrajectorySample],
                 airspeed_m_s: Optional[Sequence[float]] = None
                 ) -> list[dict[str, Any]]:
    out = []
    for i, s in enumerate(samples):
        tas = float(airspeed_m_s[i]) if airspeed_m_s is not None else airspeed_from_ned(s)
        counts = pitot_raw_counts(tas)
        # pack as 2-byte BE "register" response (MSB, LSB of 12-bit left-aligned 16)
        raw16 = counts << 4
        response = [(raw16 >> 8) & 0xFF, raw16 & 0xFF]
        out.append({
            "t_s": s.t_s,
            "t_ms": s.t_s * 1000.0,
            "device": "pitot",
            "airspeed_m_s": tas,
            "q_pa": pitot_dynamic_pressure_pa(tas),
            "counts": counts,
            "response": response,
            "registers": {0x00: response[0], 0x01: response[1]},
        })
    return out


# ---------------------------------------------------------------------------
# Convenience: decode helpers used by tests
# ---------------------------------------------------------------------------

def mpu6050_decode_accel_g(response: Sequence[int]
                           ) -> tuple[float, float, float]:
    """Decode 14-byte MPU6050 burst → accel (g)."""
    def be_i16(hi: int, lo: int) -> int:
        v = ((hi & 0xFF) << 8) | (lo & 0xFF)
        return v - 0x10000 if v & 0x8000 else v

    ax = be_i16(response[0], response[1]) / MPU6050_ACCEL_LSB_PER_G
    ay = be_i16(response[2], response[3]) / MPU6050_ACCEL_LSB_PER_G
    az = be_i16(response[4], response[5]) / MPU6050_ACCEL_LSB_PER_G
    return (ax, ay, az)


def mpu6050_decode_gyro_dps(response: Sequence[int]
                            ) -> tuple[float, float, float]:
    def be_i16(hi: int, lo: int) -> int:
        v = ((hi & 0xFF) << 8) | (lo & 0xFF)
        return v - 0x10000 if v & 0x8000 else v

    gx = be_i16(response[8], response[9]) / MPU6050_GYRO_LSB_PER_DPS
    gy = be_i16(response[10], response[11]) / MPU6050_GYRO_LSB_PER_DPS
    gz = be_i16(response[12], response[13]) / MPU6050_GYRO_LSB_PER_DPS
    return (gx, gy, gz)


def lis3mdl_decode_gauss(response: Sequence[int]
                         ) -> tuple[float, float, float]:
    def le_i16(lo: int, hi: int) -> int:
        v = ((hi & 0xFF) << 8) | (lo & 0xFF)
        return v - 0x10000 if v & 0x8000 else v

    bx = le_i16(response[0], response[1]) / LIS3MDL_LSB_PER_GAUSS
    by = le_i16(response[2], response[3]) / LIS3MDL_LSB_PER_GAUSS
    bz = le_i16(response[4], response[5]) / LIS3MDL_LSB_PER_GAUSS
    return (bx, by, bz)
