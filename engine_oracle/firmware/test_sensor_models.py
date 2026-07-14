"""Physics-accurate sensor models (Wave-3 S1) — pure, deterministic.

Prints wall-times for each case. No Renode / no I/O.
"""
from __future__ import annotations

import math
import time

import pytest

from engine.firmware.sensor_models import (
    EARTH_FIELD_GAUSS,
    G0,
    TrajectorySample,
    bmp280_compensate,
    bmp280_series,
    gnss_fix,
    gnss_series,
    imu_gyro_body_dps,
    imu_specific_force_body_g,
    isa_altitude_m,
    isa_pressure_pa,
    lis3mdl_decode_gauss,
    lis3mdl_series,
    mag_field_body_gauss,
    mpu6050_burst_response,
    mpu6050_decode_accel_g,
    mpu6050_decode_gyro_dps,
    mpu6050_series,
    pitot_raw_counts,
    pitot_series,
    quat_from_euler_zyx,
)


def _wall(label: str, t0: float) -> None:
    print(f"WALL {label}: {(time.perf_counter() - t0) * 1000.0:.3f} ms")


# ---------------------------------------------------------------------------
# IMU
# ---------------------------------------------------------------------------

def test_imu_static_one_g_down():
    """At rest, level body=NED: specific force ≈ (0, 0, −1) g (Z-down convention)."""
    t0 = time.perf_counter()
    s = TrajectorySample(t_s=0.0)  # a=0, identity quat, omega=0
    ax, ay, az = imu_specific_force_body_g(s)
    assert abs(ax) < 1e-9 and abs(ay) < 1e-9
    assert az == pytest.approx(-1.0, abs=1e-9)
    resp = mpu6050_burst_response(s)
    dax, day, daz = mpu6050_decode_accel_g(resp)
    assert dax == pytest.approx(0.0, abs=2 / 16384)
    assert day == pytest.approx(0.0, abs=2 / 16384)
    assert daz == pytest.approx(-1.0, abs=2 / 16384)
    _wall("imu_static_1g", t0)


def test_imu_freefall_zero_g():
    """Free-fall: a_ned = g_ned → specific force ≈ 0 on all axes."""
    t0 = time.perf_counter()
    s = TrajectorySample(t_s=0.0, accel_ned_m_s2=(0.0, 0.0, G0))
    ax, ay, az = imu_specific_force_body_g(s)
    assert abs(ax) < 1e-9 and abs(ay) < 1e-9 and abs(az) < 1e-9
    resp = mpu6050_burst_response(s)
    dax, day, daz = mpu6050_decode_accel_g(resp)
    assert abs(dax) < 2 / 16384 and abs(day) < 2 / 16384 and abs(daz) < 2 / 16384
    _wall("imu_freefall_0g", t0)


def test_imu_pure_spin_constant_gyro():
    """Pure body-Z spin at 90 °/s → gyro Z constant, accel still 1 g down when a=0."""
    t0 = time.perf_counter()
    omega_rad = math.radians(90.0)
    s = TrajectorySample(t_s=0.5, omega_body_rad_s=(0.0, 0.0, omega_rad))
    gx, gy, gz = imu_gyro_body_dps(s)
    assert gx == pytest.approx(0.0, abs=1e-9)
    assert gy == pytest.approx(0.0, abs=1e-9)
    assert gz == pytest.approx(90.0, abs=1e-9)
    resp = mpu6050_burst_response(s)
    dgx, dgy, dgz = mpu6050_decode_gyro_dps(resp)
    # ±250 dps FS, 131 LSB/dps → quantisation ~0.008 °/s
    assert dgx == pytest.approx(0.0, abs=0.02)
    assert dgy == pytest.approx(0.0, abs=0.02)
    assert dgz == pytest.approx(90.0, abs=0.02)
    # still ~1 g down
    _, _, daz = mpu6050_decode_accel_g(resp)
    assert daz == pytest.approx(-1.0, abs=2 / 16384)
    _wall("imu_spin_gyro", t0)


def test_imu_series_deterministic():
    t0 = time.perf_counter()
    samples = [
        TrajectorySample(t_s=0.0),
        TrajectorySample(t_s=0.1, accel_ned_m_s2=(0.0, 0.0, G0)),
        TrajectorySample(t_s=0.2, omega_body_rad_s=(0.1, 0.0, 0.0)),
    ]
    a = mpu6050_series(samples)
    b = mpu6050_series(samples)
    assert a == b
    assert len(a) == 3
    assert a[0]["response"] != a[1]["response"]  # static ≠ freefall
    _wall("imu_series_det", t0)


# ---------------------------------------------------------------------------
# Barometer / ISA
# ---------------------------------------------------------------------------

def test_baro_isa_known_altitudes():
    t0 = time.perf_counter()
    # Sea level
    assert isa_pressure_pa(0.0) == pytest.approx(101_325.0, rel=1e-6)
    # 1000 m ISA ≈ 89 874.6 Pa
    p1 = isa_pressure_pa(1000.0)
    assert p1 == pytest.approx(89_874.57, rel=1e-4)
    # 5000 m ISA ≈ 54 019.9 Pa? actually ≈ 54048 Pa
    p5 = isa_pressure_pa(5000.0)
    assert 53_000.0 < p5 < 55_000.0
    # round-trip
    assert isa_altitude_m(p1) == pytest.approx(1000.0, abs=0.05)
    _wall("baro_isa", t0)


def test_baro_profile_maps_to_isa_pressures():
    t0 = time.perf_counter()
    alts = [0.0, 500.0, 1000.0, 2000.0]
    samples = [
        TrajectorySample(t_s=float(i), pos_ned_m=(0.0, 0.0, -a), temperature_c=15.0)
        for i, a in enumerate(alts)
    ]
    series = bmp280_series(samples)
    for row, alt in zip(series, alts):
        assert row["pressure_pa"] == pytest.approx(isa_pressure_pa(alt), rel=1e-9)
        p_comp, _t = bmp280_compensate(row["response"])
        # compensation via discrete adc search — within ~30 Pa is fine for stimulus
        assert p_comp == pytest.approx(isa_pressure_pa(alt), abs=50.0)
        assert len(row["response"]) == 6
        assert all(0 <= b <= 255 for b in row["response"])
    # distinct altitudes → distinct register payloads
    payloads = [tuple(r["response"]) for r in series]
    assert len(set(payloads)) == len(payloads)
    _wall("baro_profile", t0)


# ---------------------------------------------------------------------------
# Magnetometer / GNSS / pitot
# ---------------------------------------------------------------------------

def test_mag_heading_north_field_in_body_x():
    t0 = time.perf_counter()
    # heading 0 (nose north), level → field ≈ (H, 0, V) in body
    bx, by, bz = mag_field_body_gauss(0.0)
    h = EARTH_FIELD_GAUSS * math.cos(math.radians(60.0))
    v = EARTH_FIELD_GAUSS * math.sin(math.radians(60.0))
    assert bx == pytest.approx(h, rel=1e-6)
    assert by == pytest.approx(0.0, abs=1e-9)
    assert bz == pytest.approx(v, rel=1e-6)
    # heading 90° (nose east) → north is −Y body
    bx2, by2, bz2 = mag_field_body_gauss(math.radians(90.0))
    assert bx2 == pytest.approx(0.0, abs=1e-9)
    assert by2 == pytest.approx(-h, rel=1e-6)
    assert bz2 == pytest.approx(v, rel=1e-6)
    s = TrajectorySample(t_s=0.0, quat_body_from_ned=quat_from_euler_zyx(0.0, 0.0, 0.0))
    row = lis3mdl_series([s])[0]
    dbx, dby, dbz = lis3mdl_decode_gauss(row["response"])
    assert dbx == pytest.approx(h, abs=2 / 6842)
    assert dby == pytest.approx(0.0, abs=2 / 6842)
    _wall("mag_heading", t0)


def test_gnss_fix_and_nmea():
    t0 = time.perf_counter()
    s = TrajectorySample(
        t_s=1.0,
        pos_ned_m=(1000.0, 500.0, -120.0),
        vel_ned_m_s=(10.0, 0.0, -1.0),
    )
    fix = gnss_fix(s, origin_lat_deg=37.0, origin_lon_deg=-122.0, origin_alt_m=0.0)
    assert fix["alt_m"] == pytest.approx(120.0, abs=1e-9)
    assert fix["lat_deg"] > 37.0
    assert fix["lon_deg"] > -122.0  # east positive offset
    assert fix["sog_m_s"] == pytest.approx(10.0, abs=1e-9)
    series = gnss_series([s], origin_lat_deg=37.0, origin_lon_deg=-122.0)
    assert "GPGGA" in series[0]["nmea"][0]
    assert "GPRMC" in series[0]["nmea"][1]
    assert series[0]["uart_text"].endswith("\r\n")
    _wall("gnss_nmea", t0)


def test_pitot_rest_and_speed():
    t0 = time.perf_counter()
    rest = TrajectorySample(t_s=0.0)
    fly = TrajectorySample(t_s=1.0, vel_ned_m_s=(30.0, 0.0, 0.0))
    assert pitot_raw_counts(0.0) == 0
    assert pitot_raw_counts(30.0) > 100
    series = pitot_series([rest, fly])
    assert series[0]["counts"] == 0
    assert series[1]["counts"] > series[0]["counts"]
    assert series[1]["response"] != series[0]["response"]
    _wall("pitot", t0)
