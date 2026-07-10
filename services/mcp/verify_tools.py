"""MCP tool: verify_battery — expose the verify() battery over MCP."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from typing import Optional


def register_verify_tools(mcp: FastMCP) -> None:
    """Register verify_battery as an MCP tool."""

    @mcp.tool()
    def verify_battery(
        checks: list = None,
        doc_id: Optional[str] = None,
        # FEA kwargs (forwarded if check list includes run_fea_*)
        fixtures: list = None,
        loads: list = None,
        material: str = "steel",
        # groundedness kwarg
        model_text: str = "",
        # req_join kwarg
        requirements: list = None,
        # round_trip threshold
        round_trip_threshold: float = 99.0,
        # dim_coverage: the source PART doc backing a drawing doc
        source_doc_id: Optional[str] = None,
        # power_budget (T-FIN23): system energy budget kwargs
        power_source: dict = None,
        power_converters: list = None,
        board_path: str = "",
        power_profile_path: str = "",
        power_scenario: str = "",
    ) -> str:
        """Run a subset of deterministic quality checks on the current part.

        Returns a stable {scores, findings} dict usable as an RL reward vector or CI gate.
        checks: list of check names (see check_metadata for available checks + cost/platform).
               Empty or omitted → run all subsecond/any-platform checks.
        doc_id: target document (default = current part).

        power_budget check (T-FIN23, system energy budget): pass power_source
        ({kind:"battery"|"supply", name, voltage_v, capacity_mah, i_max_a}),
        power_converters (rail tree: [{name, input, output, voltage_out_v,
        topology:"ldo|buck|boost", efficiency: eta|[[load_frac,eta]], i_out_max_a}]),
        board_path (aicad.boardphys/1 sidecar from ecad_to_3d — loads + PDN + sequencing)
        and power_profile_path (aicad.powerprofile/1 from fw_sim_run/fw_power_profile —
        duty-cycle weighting; omit → degraded datasheet-typ mode, labelled).
        Verdict includes battery life for the scenario, per-rail load vs converter
        rating, worst-rail margin, PDN cross-check, sequencing sanity note.

        score per check: 0.0 (fail) .. 1.0 (pass). findings: list of failing details.
        Use check_metadata() for the full check registry with cost/platform annotations.
        """
        import json
        from services.verify.battery import verify

        try:
            kwargs = {}
            if fixtures:
                kwargs["fixtures"] = fixtures
            if loads:
                kwargs["loads"] = loads
            if material:
                kwargs["material"] = material
            if model_text:
                kwargs["model_text"] = model_text
            if requirements:
                kwargs["requirements"] = requirements
            if round_trip_threshold != 99.0:
                kwargs["threshold"] = round_trip_threshold
            if source_doc_id:
                kwargs["source_doc_id"] = source_doc_id
            if power_source:
                kwargs["power_source"] = power_source
            if power_converters:
                kwargs["power_converters"] = power_converters
            if board_path:
                kwargs["board_path"] = board_path
            if power_profile_path:
                kwargs["power_profile_path"] = power_profile_path
            if power_scenario:
                kwargs["power_scenario"] = power_scenario
            result = verify(doc_id=doc_id, checks=checks or [], **kwargs)
            return json.dumps(result)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)})

    @mcp.tool()
    def check_metadata() -> str:
        """Return the full verify-battery check registry: names, cost class, platform, reward shape."""
        import json
        from services.verify.battery import check_metadata as _meta

        return json.dumps(_meta())
