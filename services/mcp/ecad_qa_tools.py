"""ECAD schematic drawing-quality MCP tools — the geometric self-check ERC cannot do.

``ecad_run_erc`` is electrical: undriven rails, output-output shorts, floating pins. A schematic
can pass it and still be a mess a human would reject — wires routed through symbol bodies,
dangling wire stubs, labels on empty paper, symbols drawn on top of each other. This module
exposes ONE tool, ``ecad_lint_schematic``, which reports all four categories with the offending
refdes / coordinates / label text, so the agent can self-check BEFORE it runs ERC and before it
hands the sheet over.

The geometry lives in services/ecad/sch_qa.py (pure functions over the doc dict, no RPC). This
module is only the MCP surface: resolve the active schematic, run the report, and phrase the
result so the next call is obvious.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature

from services.ecad import sch_qa

from .ecad_common import (
    OpError,
    _dumps,
    _err,
    _get_sch,
)


def register_ecad_qa_tools(mcp: FastMCP) -> None:
    """ECAD schematic drawing-quality MCP tools: ecad_lint_schematic."""

    @mcp.tool()
    def ecad_lint_schematic(doc_id: str = "", max_per_kind: int = 20) -> str:
        """GEOMETRIC self-check of the active schematic — run this BEFORE ecad_run_erc.

        ERC checks electricity; this checks the DRAWING, and reports every offender by name:

          - ``symbol_overlap``      two symbol bodies drawn on top of each other (both refdes
                                    + the overlapping mm box)
          - ``dangling_wire``       a wire end touching no pin / wire / junction / label /
                                    power port / no-connect (wire id, which end, its xy, and
                                    the nearest connection point + how far it missed by)
          - ``wire_crosses_symbol`` a wire routed straight THROUGH a body (wire id, the refdes
                                    it crosses, entry/exit xy, mm inside the body)
          - ``floating_label``      a net label on no wire and no pin, so it names nothing
                                    (label text + xy)

        Deliberately NOT reported (these are legitimate and would make the tool useless):
        a wire terminating on a pin that sits on the body boundary; a wire ended by a label,
        power port, junction or no-connect marker; a bus; anything compared across two sheets.
        A symbol whose body cannot be resolved from its graphics is skipped and named in
        ``checked.symbols_without_body`` rather than guessed at.

        ``clean: true`` means all four checks found nothing. Every finding carries a ``message``
        naming the fix, so act on them one at a time and re-run.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first (this check is schematic-only).",
                        next_actions=["ecad_new_schematic('mydesign')",
                                      "ecad_status()  # see which docs are open"])
        try:
            report = sch_qa.check_schematic_geometry(session.doc,
                                                    max_per_kind=int(max_per_kind or 0))
        except ValueError as e:
            return _err(str(e),
                        hint="ecad_lint_schematic checks a SCHEMATIC's drawing geometry. For a "
                             "board use ecad_run_drc.",
                        next_actions=["ecad_run_drc()"])
        line = sch_qa.verdict(report)
        if report["clean"]:
            next_actions = ["ecad_run_erc()  # drawing is clean — now check it electrically",
                            "ecad_status()"]
        else:
            next_actions = [f"FIX {f['kind']}: {f['message']}" for f in report["findings"][:3]]
            next_actions.append("ecad_lint_schematic()  # re-run after each fix")
        return _dumps({
            "ok": True,
            "doc_id": session.doc_id,
            "revision": session.revision,
            "clean": report["clean"],
            "total": report["total"],
            "counts": report["counts"],
            "findings": report["findings"],
            "truncated": report["truncated"],
            "checked": report["checked"],
            "verdict": line,
            "warnings": ([f"{n} more {k} finding(s) not shown — raise max_per_kind"
                          for k, n in report["truncated"].items()]
                         + ([f"no body geometry for {', '.join(report['checked']['symbols_without_body'])}"
                             f" — those symbols were skipped by the overlap/crossing checks"]
                            if report["checked"]["symbols_without_body"] else [])),
            "hint": line,
            "next_actions": next_actions,
        })
