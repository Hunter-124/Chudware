"""PCB antenna design + EM-analysis MCP tools.

Thin FastMCP wrappers over :mod:`engine_oracle.ecad.antenna` and
:mod:`engine_oracle.sim.em.antenna_bridge`. Registration is deferred behind
:func:`register_antenna_tools` so ``server.py`` wires us in with a one-line call.

Tools return compact JSON envelopes ``{ok, warnings, hint, next_actions, ...}``
matching :mod:`services.mcp.em_tools`. openEMS-absent full-wave analysis is an
honest skip (never a traceback / never fabricated S11).
"""
from __future__ import annotations

from typing import Any

from services.ecad import antenna as antenna_mod  # STAYS: no C++ counterpart
from services.mcp._envelopes import analysis_envelope
from services.sim.em import antenna_bridge as bridge

# Antenna payloads can carry bound bridge callables; render them as a marker.
_dumps, _err, _ok = analysis_envelope(mark_callables=True)


def _compact_geometry(geom: dict) -> dict:
    """Summarise geometry copper counts; drop full vertex lists unless small."""
    tracks = geom.get("tracks") or []
    zones = geom.get("zones") or []
    pads = geom.get("pads") or []
    vias = geom.get("vias") or []
    return {
        "kind": geom.get("kind"),
        "origin": geom.get("origin"),
        "layer": geom.get("layer"),
        "ground_layer": geom.get("ground_layer"),
        "net": geom.get("net"),
        "gnd_net": geom.get("gnd_net"),
        "bbox_mm": geom.get("bbox_mm"),
        "n_tracks": len(tracks),
        "n_zones": len(zones),
        "n_pads": len(pads),
        "n_vias": len(vias),
        "tracks": tracks,
        "zones": zones,
        "pads": pads,
        "vias": vias,
    }


def register_antenna_tools(mcp) -> None:
    """Register ecad_antenna_design / ecad_antenna_analyze on ``mcp``."""

    @mcp.tool()
    def ecad_antenna_design(
        kind: str = "patch",
        freq_hz: float = 2.45e9,
        substrate_er: float = 4.4,
        substrate_h_mm: float = 1.6,
        copper_t_mm: float = 0.035,
        feed_kind: str = "microstrip",
        feed_z0: float = 50.0,
        meander_turns: int = 3,
        velocity_factor: float = 0.0,
        inset_frac: float = 0.3,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        include_geometry: bool = True,
        detail: bool = False,
    ) -> str:
        """Parametric PCB antenna design (patch / monopole / ifa / pifa).

        Returns closed-form dimensions (mm) from textbook design equations plus
        board copper geometry (tracks/zones/pads) placeable on an aicad.pcb/1
        document. Feed width is synthesised for ``feed_z0`` via the IPC-2141 /
        CPWG calculators in engine.ecad.impedance.

        ``kind``: 'patch' | 'monopole' | 'ifa' | 'pifa'.
        ``velocity_factor``: optional monopole vf (0 = auto from microstrip eeff).
        """
        try:
            opts: dict[str, Any] = {
                "meander_turns": int(meander_turns),
                "inset_frac": float(inset_frac),
            }
            if velocity_factor and velocity_factor > 0:
                opts["velocity_factor"] = float(velocity_factor)
            design = antenna_mod.design_antenna(
                kind,
                float(freq_hz),
                substrate_er=float(substrate_er),
                substrate_h_mm=float(substrate_h_mm),
                copper_t_mm=float(copper_t_mm),
                feed_kind=str(feed_kind),
                feed_z0=float(feed_z0),
                **opts,
            )
        except Exception as exc:  # noqa: BLE001
            return _err(
                f"{type(exc).__name__}: {exc}",
                hint="kind must be patch|monopole|ifa|pifa; freq_hz > 0; er >= 1.",
                next_actions=[
                    "ecad_antenna_design(kind='patch', freq_hz=2.4e9, substrate_er=4.4, substrate_h_mm=1.6)",
                    "ecad_antenna_design(kind='monopole', freq_hz=2.45e9)",
                    "ecad_antenna_design(kind='ifa', freq_hz=2.45e9, meander_turns=3)",
                ],
            )

        dims = design["dimensions"]
        payload: dict[str, Any] = {
            "tool": "ecad_antenna_design",
            "kind": design["kind"],
            "dimensions": dims,
        }
        if include_geometry:
            geom = design["geometry"]((float(origin_x), float(origin_y)))
            if detail:
                payload["geometry"] = geom
            else:
                payload["geometry"] = _compact_geometry(geom)
        return _ok(
            payload,
            hint=(f"{design['kind']} @ {dims.get('freq_ghz')} GHz: "
                  f"see dimensions; feed Z0≈{dims.get('feed', {}).get('z0')} Ω."),
            next_actions=[
                "ecad_antenna_analyze(kind=..., freq_hz=..., ...)",
                "place geometry tracks/zones onto a pcb doc via engine.ecad.antenna.place_antenna_on_board",
            ],
        )

    @mcp.tool()
    def ecad_antenna_analyze(
        kind: str = "patch",
        freq_hz: float = 2.45e9,
        substrate_er: float = 4.4,
        substrate_h_mm: float = 1.6,
        copper_t_mm: float = 0.035,
        feed_kind: str = "microstrip",
        feed_z0: float = 50.0,
        meander_turns: int = 3,
        velocity_factor: float = 0.0,
        want_fullwave: bool = True,
        n_freq: int = 41,
        max_timesteps: int = 40_000,
        detail: bool = False,
    ) -> str:
        """Antenna resonance / return-loss analysis.

        Always returns an analytic closed-form resonance estimate. When
        ``want_fullwave`` is True and openEMS is installed, also runs a real
        FDTD S11 sweep; when openEMS is absent the full-wave block is an honest
        ``{skipped:true, reason}`` (never fabricated).
        """
        try:
            opts: dict[str, Any] = {
                "meander_turns": int(meander_turns),
            }
            if velocity_factor and velocity_factor > 0:
                opts["velocity_factor"] = float(velocity_factor)
            design = antenna_mod.design_antenna(
                kind,
                float(freq_hz),
                substrate_er=float(substrate_er),
                substrate_h_mm=float(substrate_h_mm),
                copper_t_mm=float(copper_t_mm),
                feed_kind=str(feed_kind),
                feed_z0=float(feed_z0),
                **opts,
            )
            result = bridge.analyze_antenna(
                design,
                want_fullwave=bool(want_fullwave),
                n_freq=int(n_freq),
                max_timesteps=int(max_timesteps),
            )
        except Exception as exc:  # noqa: BLE001
            return _err(
                f"{type(exc).__name__}: {exc}",
                hint="Check kind/freq/substrate; try want_fullwave=False for analytic only.",
                next_actions=[
                    "ecad_antenna_design(kind='patch', freq_hz=2.4e9)",
                    "ecad_antenna_analyze(kind='patch', freq_hz=2.4e9, want_fullwave=False)",
                ],
            )

        warnings: list[str] = []
        fw = result.get("fullwave") or {}
        if fw.get("skipped"):
            warnings.append(str(fw.get("reason") or "full-wave skipped"))

        # Compact: drop full S11 arrays unless detail.
        compact = {
            "tool": "ecad_antenna_analyze",
            "kind": result.get("kind"),
            "source": result.get("source"),
            "f_res_hz": result.get("f_res_hz"),
            "f_res_ghz": result.get("f_res_ghz"),
            "s11_min_db": result.get("s11_min_db"),
            "analytic": result.get("analytic"),
        }
        if want_fullwave:
            if detail:
                compact["fullwave"] = fw
            else:
                compact["fullwave"] = {
                    "ok": fw.get("ok"),
                    "skipped": fw.get("skipped"),
                    "reason": fw.get("reason"),
                    "solver": fw.get("solver"),
                    "f_res_ghz": fw.get("f_res_ghz"),
                    "s11_min_db": fw.get("s11_min_db"),
                    "bandwidth_mhz_s11_m10db": fw.get("bandwidth_mhz_s11_m10db"),
                    "n_freq": fw.get("n_freq"),
                }
                if result.get("fullwave_skipped"):
                    compact["fullwave_skipped"] = True

        hint = (f"analytic f_res={result.get('f_res_ghz')} GHz "
                f"(source={result.get('source')})")
        if fw.get("ok") and not fw.get("skipped"):
            hint = (f"full-wave S11 min {fw.get('s11_min_db')} dB @ "
                    f"{fw.get('f_res_ghz')} GHz; analytic "
                    f"{result['analytic'].get('f_res_ghz')} GHz")
        return _ok(
            compact,
            hint=hint,
            warnings=warnings,
            next_actions=[
                "ecad_antenna_design(kind=..., freq_hz=...)",
                "ecad_antenna_analyze(..., detail=True)  # full S11 arrays",
            ],
        )
