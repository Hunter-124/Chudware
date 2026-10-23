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
        # power_budget: system energy budget kwargs
        power_source: dict = None,
        power_converters: list = None,
        board_path: str = "",
        power_profile_path: str = "",
        power_scenario: str = "",
    ) -> str:
        """Run deterministic checks on one explicit document.

        Returns stable scores/findings plus coverage and limitations. A PASS covers ONLY
        checks named in scores. Names in skipped did not run and are not passes. In particular,
        a battery on a 3D part does not verify a separate drawing, assembly interfaces/fits,
        load capacity, material intent, tolerances, or manufacturability; invoke those domain
        tools/documents separately before claiming the delivered design is verified.

        checks: names from check_metadata. Empty/omitted runs applicable subsecond checks;
                unknown names reject the whole call.
        doc_id: the one document to check (default current part).

        power_budget accepts power_source, power_converters, board_path,
        power_profile_path and power_scenario. See check_metadata for cost/platform.
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
            scores = result.get("scores") or {}
            skipped = result.get("skipped") or []
            result["verdict"] = (
                "fail" if any(float(score) < 1.0 for score in scores.values())
                else "pass" if scores else "no_checks_ran"
            )
            result["coverage"] = {
                "doc_id": doc_id,
                "ran": sorted(scores),
                "skipped": sorted(skipped),
            }
            result["limitations"] = [
                "PASS applies only to coverage.ran; coverage.skipped is unverified.",
                "A part-document battery does not verify separate drawings, assembly fits, "
                "loads, material intent, tolerances, or manufacturability.",
            ]
            return json.dumps(result)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)})

    @mcp.tool()
    def check_metadata() -> str:
        """Return the full verify-battery check registry: names, cost class, platform, reward shape."""
        import json
        from services.verify.battery import check_metadata as _meta

        return json.dumps(_meta())
