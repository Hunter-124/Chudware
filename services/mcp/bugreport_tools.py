"""bugreport_tools.py — submit_bug_report MCP tool (Settings → AI → Tools).

Family: ``workspace`` (user-facing utility; the dedicated ``agent.bug_reports``
toggle is the real gate — checked at call time so external MCP clients honor
the setting too).
Risk: ``write`` — it writes one local markdown file; the ``submit_`` prefix
would auto-classify EXTERNAL (network egress), so tool_registry carries an
explicit override.

All state and the file writer live in
``services.agent.bugreport_context`` — a single import identity, so the ring
buffer agent_turn feeds is the same one this tool reads no matter which
module identity (``services.mcp.*`` vs ``services.mcp.*``) loaded this file.
"""
from __future__ import annotations


def register_bugreport_tools(mcp) -> None:
    """Register the bug-report tool under the ``workspace`` family."""

    @mcp.tool()
    def submit_bug_report(title: str, summary: str, context: str = "",
                          severity: str = "bug") -> dict:
        """File a bug report for the user when something is broken on OUR side.

        USE THIS when a tool errors in a way that looks like a Chudware bug,
        not your mistake — e.g. a call that matches the documented schema
        still fails validation, "unexpected keyword argument" for a documented
        kwarg, an "unknown method" RPC, or a crash. Do NOT use it for ordinary
        retry-and-fix errors whose message already teaches you the correction.

        ``title``: short name for the broken thing (e.g. "add_features rejects
        sketch entity type 'polyline'"). ``summary``: what you tried, what
        happened (exact error), what you expected. ``context``: optional extra
        notes. The markdown report (in the user's
        Documents/Chudware/Bug-Reports/ folder) automatically embeds the
        recent tool failures captured this session — summarize them, do not
        paste raw payloads.
        """
        try:
            from services.agent.config import bug_reports_enabled  # noqa: PLC0415
            if not bug_reports_enabled():
                return {"ok": False, "reason": "disabled",
                        "hint": "Bug reports are turned off (Settings → AI → "
                                "Tools → Send bug reports). Work around the "
                                "issue and tell the user instead."}
        except Exception:  # noqa: BLE001 — unreadable config = default (on)
            pass
        from services.agent.bugreport_context import file_report  # noqa: PLC0415
        return file_report(title, summary, context=context, severity=severity)
