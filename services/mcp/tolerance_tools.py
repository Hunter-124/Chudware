"""Tolerance stack-up MCP tool.

  stackup_analyze(dims, mode)  -> 1D worst-case + RSS tolerance stack-up report

The pure math lives in the C++ core (``core/tolerance/stackup.cpp``, dispatched via the
libchudware ``stackup_analyze`` method through ``core_bridge``); this module is the thin
MCP wrapper.

Registration note: append-only in ``server.py`` (sibling to structural_tools).
This module is named ``tolerance_tools.py`` (not structural_tools.py) so its
registration line is independent of the other structural/FEA tool appends.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from services.mcp import core_bridge as cb
from services.mcp.cad_tools import _dumps, _err

DISCLAIMER = (
    "Preliminary stack-up — not a certified tolerance analysis. "
    "Verify with a qualified dimensional-metrology engineer before release."
)


def register_tolerance_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def stackup_analyze(
        dims: list,
        mode: str = "both",
    ) -> str:
        """1D tolerance stack-up analysis: worst-case and/or RSS over a dimension chain.

        Computes the assembly gap variation for a 1D chain of feature dimensions using
        the standard equal-bilateral formulas from Fischer, "Mechanical Tolerance Stackup
        and Analysis," 2nd ed., CRC Press 2011, Ch. 3.

        Args:
            dims: Ordered list of dimension dicts.  Each dict requires:
                  - ``name``     (str)   : label for the dimension / feature
                  - ``nominal``  (float) : nominal value in mm (or any consistent unit)
                  - ``tol``      (float) : half-tolerance, the ± value, ≥ 0
                  Optional keys:
                  - ``direction`` (float, default +1) : sign in the 1D chain
                    (+1 = adds to the gap, −1 = subtracts from the gap)
                  - ``ref``      (str)   : source feature ID or document ref
                  - ``assumed``  (bool)  : True if tol was defaulted/assumed because
                    the CAD doc lacked explicit tolerance data — always set this to True
                    and add the dim name to the input when you must invent a tolerance;
                    the tool reports all assumed dims in the result.

            mode: ``"worst_case"`` | ``"rss"`` | ``"both"`` (default).
                  Controls which result block is included (both are always computed;
                  this flag scopes the return to what the caller needs).

        Returns:
            JSON object:
            ```json
            {
              "ok": true,
              "nominal_gap": 3.0,
              "worst_case": {
                "total_tol": 0.115,
                "gap_max": 3.115,
                "gap_min": 2.885
              },
              "rss": {
                "total_tol": 0.0687,
                "gap_max": 3.0687,
                "gap_min": 2.9313
              },
              "contributors": [
                {
                  "name": "A_slot_width",
                  "nominal": 40.0,
                  "tol_half": 0.05,
                  "direction": 1.0,
                  "ref": null,
                  "assumed": false,
                  "contrib_wc": 0.05,
                  "contrib_rss_sq": 0.0025,
                  "pct_wc": 43.48,
                  "pct_rss": 52.91
                },
                ...
              ],
              "assumed_dims": [],
              "disclaimer": "Preliminary stack-up..."
            }
            ```
            ``contributors`` is sorted descending by ``contrib_wc`` (worst-case
            sensitivity first — the dimension that dominates the stack).

            If any dims had ``assumed=True``, ``assumed_dims`` lists their names
            so the caller can flag them in the report.

        Raises:
            Returns ``{ok: false, error, code}`` on invalid input.
        """
        # The C++ core owns the whole envelope: it validates `mode` (invalid_mode),
        # validates the dims (invalid_input), scopes worst_case/rss, and stamps
        # ok/disclaimer — identical shape to the retired Python wrapper.
        try:
            return _dumps(cb.call("stackup_analyze", {"dims": dims, "mode": mode}))
        except cb.CoreError as e:
            return _dumps({
                "ok": False,
                "error": "tolerance stack-up engine unavailable",
                "code": "engine_unavailable",
                "detail": str(e),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)
