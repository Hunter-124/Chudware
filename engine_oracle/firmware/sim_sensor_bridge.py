"""CFD / kinematic / flight-profile → ``aicad.fwscenario/1`` sensor bridge (Wave-3 S1).

Turns a motion/CFD result **or** an analytic flight profile plus a board's sensor
map into a valid ``aicad.fwscenario/1`` dict that ``fw_sim_run`` can feed into the
emulated MCU. Physics lives in :mod:`engine.firmware.sensor_models`; this module
only samples, packs register bytes into time-varying I2C tracks, and (for GNSS)
UART RX NMEA streams.

Public entry point::

    build_sensor_scenario(sim_result_or_profile, sensor_map,
                          *, duration_s=None, dt_s=0.01) -> dict

``sensor_map`` entries describe which physical device sits on which bus/address::

    [
      {"device": "mpu6050", "bus": "i2c1", "address": 0x68},
      {"device": "bmp280",  "bus": "i2c1", "address": 0x76},
      {"device": "lis3mdl", "bus": "i2c1", "address": 0x1C},
      {"device": "neo6m",   "kind": "uart_rx", "uart": "usart1"},  # or i2c pack
      {"device": "pitot",   "bus": "i2c1", "address": 0x28},
    ]
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence, Union

from engine.firmware.scenario import SCHEMA, validate_scenario
from engine.firmware.sensor_models import (
    FlightProfile,
    TrajectorySample,
    bmp280_series,
    gnss_series,
    lis3mdl_series,
    mpu6050_series,
    pitot_series,
    quat_from_euler_zyx,
)

# Default 7-bit addresses matching common breakout / flight-computer wiring.
_DEFAULT_ADDR = {
    "mpu6050": 0x68,
    "bmp280": 0x76,
    "lis3mdl": 0x1C,
    "pitot": 0x28,
    "neo6m": 0x42,  # only used when packed as i2c_register_stream
}

_SUPPORTED_DEVICES = frozenset(_DEFAULT_ADDR) | {"imu", "baro", "mag", "gnss", "gps", "airspeed"}


def _alias_device(name: str) -> str:
    n = (name or "").strip().lower()
    return {
        "imu": "mpu6050",
        "mpu-6050": "mpu6050",
        "mpu6000": "mpu6050",
        "baro": "bmp280",
        "barometer": "bmp280",
        "bmp-280": "bmp280",
        "mag": "lis3mdl",
        "magnetometer": "lis3mdl",
        "lis3mdl": "lis3mdl",
        "gnss": "neo6m",
        "gps": "neo6m",
        "neo-6m": "neo6m",
        "neo6m": "neo6m",
        "airspeed": "pitot",
        "pitot": "pitot",
        "mpu6050": "mpu6050",
        "bmp280": "bmp280",
    }.get(n, n)


def _as_profile(sim_result_or_profile: Any) -> FlightProfile:
    if isinstance(sim_result_or_profile, FlightProfile):
        return sim_result_or_profile
    if isinstance(sim_result_or_profile, dict):
        # Accept a few CFD/motion result shapes.
        if "profile" in sim_result_or_profile and isinstance(
                sim_result_or_profile["profile"], dict):
            return FlightProfile.from_dict(sim_result_or_profile["profile"])
        if "trajectory" in sim_result_or_profile:
            tr = sim_result_or_profile["trajectory"]
            if isinstance(tr, FlightProfile):
                return tr
            if isinstance(tr, dict):
                return FlightProfile.from_dict(tr)
            if isinstance(tr, list) and tr and isinstance(tr[0], TrajectorySample):
                return FlightProfile(samples=list(tr))
        if "samples" in sim_result_or_profile or "t_s" in sim_result_or_profile \
                or "time_s" in sim_result_or_profile or "altitude_m" in sim_result_or_profile:
            return FlightProfile.from_dict(sim_result_or_profile)
        # CFD velocity field summary: {t_s, velocity_m_s | airspeed_m_s, altitude_m?}
        if "velocity_m_s" in sim_result_or_profile or "airspeed_m_s" in sim_result_or_profile:
            return FlightProfile.from_dict(_cfd_to_profile_dict(sim_result_or_profile))
    if isinstance(sim_result_or_profile, (list, tuple)) and sim_result_or_profile:
        if isinstance(sim_result_or_profile[0], TrajectorySample):
            return FlightProfile(samples=list(sim_result_or_profile))
    raise TypeError(
        "sim_result_or_profile must be a FlightProfile, trajectory dict "
        "(t_s + kinematics), list[TrajectorySample], or CFD summary dict")


def _cfd_to_profile_dict(d: dict) -> dict:
    """Map a thin CFD/flight-speed summary onto a FlightProfile dict."""
    t = list(d.get("t_s") or d.get("time_s") or [])
    if not t:
        # single-point CFD snapshot
        t = [0.0]
    n = len(t)
    speed = d.get("airspeed_m_s", d.get("velocity_m_s", 0.0))
    if isinstance(speed, (int, float)):
        speeds = [float(speed)] * n
    else:
        speeds = [float(s) for s in speed]
        if len(speeds) != n:
            raise ValueError("airspeed/velocity length must match t_s")
    alts = d.get("altitude_m", 0.0)
    if isinstance(alts, (int, float)):
        alts = [float(alts)] * n
    else:
        alts = [float(a) for a in alts]
    # Fly north at the given airspeed; altitude positive-up → NED down = −alt.
    # Optional heading_rad / yaw_rad rotates the ground track.
    headings = d.get("heading_rad", d.get("yaw_rad", 0.0))
    if isinstance(headings, (int, float)):
        headings = [float(headings)] * n
    else:
        headings = [float(h) for h in headings]
    pos, vel, quat, omega = [], [], [], []
    n_pos = 0.0
    e_pos = 0.0
    for i in range(n):
        h = headings[i]
        v = speeds[i]
        vn, ve = v * math.cos(h), v * math.sin(h)
        if i == 0:
            n_pos = 0.0
            e_pos = 0.0
        else:
            dt = float(t[i]) - float(t[i - 1])
            n_pos += vn * dt
            e_pos += ve * dt
        pos.append((n_pos, e_pos, -float(alts[i])))
        vel.append((vn, ve, 0.0))
        quat.append(quat_from_euler_zyx(h, 0.0, 0.0))
        omega.append((0.0, 0.0, 0.0))
    return {
        "t_s": t,
        "pos_ned_m": pos,
        "vel_ned_m_s": vel,
        "quat_body_from_ned": quat,
        "omega_body_rad_s": omega,
        "airspeed_m_s": speeds,
        "origin_lat_deg": float(d.get("origin_lat_deg", 37.0)),
        "origin_lon_deg": float(d.get("origin_lon_deg", -122.0)),
        "origin_alt_m": float(d.get("origin_alt_m", 0.0)),
        "temperature_c": d.get("temperature_c", 25.0),
    }


def _resample(profile: FlightProfile, duration_s: Optional[float],
              dt_s: float) -> list[TrajectorySample]:
    """Nearest-neighbour sample-and-hold onto a uniform grid (deterministic)."""
    if not profile.samples:
        raise ValueError("flight profile has no samples")
    src = sorted(profile.samples, key=lambda s: s.t_s)
    t0 = src[0].t_s
    t1 = src[-1].t_s
    if duration_s is not None:
        t1 = t0 + float(duration_s)
    if t1 < t0:
        t1 = t0
    dt = max(1e-4, float(dt_s))
    # Inclusive end within 1e-9 to keep the final sample.
    n = max(1, int(math.floor((t1 - t0) / dt + 1e-9)) + 1)
    out: list[TrajectorySample] = []
    j = 0
    for i in range(n):
        t = t0 + i * dt
        if t > t1 + 1e-12:
            break
        while j + 1 < len(src) and src[j + 1].t_s <= t:
            j += 1
        # Hold sample j; if t is past the last source sample, keep holding it.
        s = src[j]
        out.append(TrajectorySample(
            t_s=t,
            pos_ned_m=s.pos_ned_m,
            vel_ned_m_s=s.vel_ned_m_s,
            accel_ned_m_s2=s.accel_ned_m_s2,
            quat_body_from_ned=s.quat_body_from_ned,
            omega_body_rad_s=s.omega_body_rad_s,
            temperature_c=s.temperature_c,
        ))
    return out


def _airspeed_series(profile: FlightProfile, samples: list[TrajectorySample]
                     ) -> Optional[list[float]]:
    if profile.airspeed_m_s is None:
        return None
    # Resample airspeed by nearest source index.
    src_t = [s.t_s for s in profile.samples]
    out: list[float] = []
    j = 0
    for s in samples:
        while j + 1 < len(src_t) and src_t[j + 1] <= s.t_s:
            j += 1
        out.append(float(profile.airspeed_m_s[min(j, len(profile.airspeed_m_s) - 1)]))
    return out


def _stream_track(device: str, bus: str, address: int,
                  series: Sequence[dict], *, name: Optional[str] = None) -> dict:
    samples = []
    for row in series:
        samples.append({
            "t_ms": float(row["t_ms"]),
            "response": [int(b) & 0xFF for b in row["response"]],
        })
    tr: dict[str, Any] = {
        "kind": "i2c_register_stream",
        "bus": bus,
        "address": int(address),
        "device": device,
        "samples": samples,
    }
    if name:
        tr["name"] = name
    return tr


def _normalize_sensor_map(sensor_map: Sequence[dict]) -> list[dict]:
    if not sensor_map:
        raise ValueError("sensor_map must list at least one device")
    out = []
    for i, entry in enumerate(sensor_map):
        if not isinstance(entry, dict):
            raise TypeError(f"sensor_map[{i}] must be a dict")
        dev = _alias_device(str(entry.get("device") or entry.get("model") or ""))
        if dev not in _DEFAULT_ADDR:
            raise ValueError(
                f"sensor_map[{i}] unknown device {entry.get('device')!r} "
                f"(supported: mpu6050/bmp280/lis3mdl/neo6m/pitot + aliases)")
        kind = entry.get("kind")
        if kind is None:
            kind = "uart_rx" if dev == "neo6m" and entry.get("uart") else "i2c_register_stream"
        bus = entry.get("bus", "i2c1")
        addr = entry.get("address", _DEFAULT_ADDR[dev])
        if not isinstance(addr, int):
            raise TypeError(f"sensor_map[{i}].address must be int, got {type(addr).__name__}")
        out.append({
            "device": dev,
            "kind": kind,
            "bus": bus,
            "address": addr,
            "uart": entry.get("uart"),
            "name": entry.get("name"),
        })
    return out


def build_sensor_scenario(
    sim_result_or_profile: Any,
    sensor_map: Sequence[dict],
    *,
    duration_s: Optional[float] = None,
    dt_s: float = 0.01,
    name: Optional[str] = None,
) -> dict:
    """Build an ``aicad.fwscenario/1`` dict that plays *sim* into the emulated MCU.

    Parameters
    ----------
    sim_result_or_profile
        :class:`FlightProfile`, trajectory dict, list of :class:`TrajectorySample`,
        or a CFD summary ``{t_s, airspeed_m_s|velocity_m_s, altitude_m?}``.
    sensor_map
        Iterable of ``{device, bus?, address?, kind?, uart?, name?}``.
    duration_s
        Optional run length; defaults to the profile span.
    dt_s
        Sample-and-hold grid step in seconds (default 10 ms).
    name
        Optional scenario display name.

    Returns
    -------
    dict
        Validated ``aicad.fwscenario/1`` document. Raises ``ValueError`` if the
        produced tracks fail structural validation (should not happen for a
        well-formed map — defensive).
    """
    profile = _as_profile(sim_result_or_profile)
    samples = _resample(profile, duration_s, dt_s)
    sensors = _normalize_sensor_map(sensor_map)
    airspeed = _airspeed_series(profile, samples)

    tracks: list[dict] = []
    for ent in sensors:
        dev = ent["device"]
        if dev == "mpu6050":
            series = mpu6050_series(samples)
            tracks.append(_stream_track(dev, ent["bus"], ent["address"], series,
                                        name=ent.get("name")))
        elif dev == "bmp280":
            series = bmp280_series(samples)
            tracks.append(_stream_track(dev, ent["bus"], ent["address"], series,
                                        name=ent.get("name")))
        elif dev == "lis3mdl":
            series = lis3mdl_series(samples)
            tracks.append(_stream_track(dev, ent["bus"], ent["address"], series,
                                        name=ent.get("name")))
        elif dev == "pitot":
            series = pitot_series(samples, airspeed_m_s=airspeed)
            tracks.append(_stream_track(dev, ent["bus"], ent["address"], series,
                                        name=ent.get("name")))
        elif dev == "neo6m":
            series = gnss_series(
                samples,
                origin_lat_deg=profile.origin_lat_deg,
                origin_lon_deg=profile.origin_lon_deg,
                origin_alt_m=profile.origin_alt_m,
            )
            if ent["kind"] == "uart_rx":
                # Collapse to one uart_rx track with per-sample NMEA text events.
                events = [{"t_ms": float(row["t_ms"]), "text": row["uart_text"]}
                          for row in series]
                tr: dict[str, Any] = {
                    "kind": "uart_rx",
                    "events": events,
                    "device": "neo6m",
                }
                if ent.get("uart"):
                    tr["uart"] = ent["uart"]
                tracks.append(tr)
            else:
                tracks.append(_stream_track(dev, ent["bus"], ent["address"], series,
                                            name=ent.get("name")))
        else:  # pragma: no cover - guarded by _normalize_sensor_map
            raise ValueError(f"unsupported device {dev!r}")

    scenario = {
        "schema": SCHEMA,
        "name": name or "sim_sensor_bridge",
        "tracks": tracks,
        "meta": {
            "source": "sim_sensor_bridge",
            "n_samples": len(samples),
            "dt_s": float(dt_s),
            "t0_s": samples[0].t_s if samples else 0.0,
            "t1_s": samples[-1].t_s if samples else 0.0,
            "devices": [e["device"] for e in sensors],
        },
    }
    errs = validate_scenario(scenario)
    if errs:
        raise ValueError("bridge produced invalid fwscenario: " + "; ".join(errs))
    return scenario


def climb_turn_profile(*, duration_s: float = 4.0, dt_s: float = 0.05,
                       climb_rate_m_s: float = 5.0,
                       turn_rate_rad_s: float = 0.25,
                       airspeed_m_s: float = 25.0,
                       start_alt_m: float = 100.0) -> FlightProfile:
    """Canned climb+turn flight profile used by tests and the MCP example.

    Includes a brief pitch-up transient in the first 20 % of the run so the IMU
    accel/gyro streams are non-constant (useful for stimulus coverage); the rest
    is a steady coordinated climb-turn.
    """
    n = max(2, int(round(duration_s / dt_s)) + 1)
    t_s, pos, vel, acc, quat, omega, tas = [], [], [], [], [], [], []
    n_pos = e_pos = 0.0
    for i in range(n):
        t = i * dt_s
        heading = turn_rate_rad_s * t
        alt = start_alt_m + climb_rate_m_s * t
        frac = t / max(duration_s, 1e-9)
        pitch = 0.15 * max(0.0, 1.0 - frac / 0.2) if frac < 0.2 else 0.0
        pitch_rate = (-0.15 / (0.2 * duration_s)) if frac < 0.2 else 0.0
        vn = airspeed_m_s * math.cos(heading) * math.cos(pitch)
        ve = airspeed_m_s * math.sin(heading) * math.cos(pitch)
        vd = -climb_rate_m_s
        if i > 0:
            n_pos += vn * dt_s
            e_pos += ve * dt_s
        t_s.append(t)
        pos.append((n_pos, e_pos, -alt))
        vel.append((vn, ve, vd))
        acc.append((0.0, 0.0, 0.0))
        quat.append(quat_from_euler_zyx(heading, pitch, 0.0))
        omega.append((0.0, pitch_rate, turn_rate_rad_s))
        tas.append(airspeed_m_s)
    return FlightProfile.from_dict({
        "t_s": t_s,
        "pos_ned_m": pos,
        "vel_ned_m_s": vel,
        "accel_ned_m_s2": acc,
        "quat_body_from_ned": quat,
        "omega_body_rad_s": omega,
        "airspeed_m_s": tas,
        "temperature_c": 15.0,
        "origin_lat_deg": 37.4185,
        "origin_lon_deg": -122.0683,
        "origin_alt_m": 0.0,
    })
