"""A gated tool must REFUSE, not crash — and this suite must not read the developer's config.

Two live defects, observed 2026-07-28 on a box where `agent.tool_families.recon = false`
was simply toggled off in Settings -> AI -> Tools:

  1. PRODUCT.  `tool_registry._guard` handed the refusal *dict* straight back. FastMCP
     builds each tool's output schema from its return annotation and validates the return
     value against it, and 365 of the ~400 registered tools declare ``-> str``, so the
     model got

         ToolError: Error executing tool reverse_engineer: 1 validation error for
         reverse_engineerOutput / result: Input should be a valid string

     instead of "that tool family is switched off". An opaque schema crash teaches a model
     nothing; it retries, and burns the run. Every user who turns a family off hit this.

  2. TESTS.  `services/mcp/test_recon_tools.py` + `test_recon_hints.py` went red on that
     same box, because `tool_registry.deny_reason` -> `is_tool_family_enabled` reads
     `~/.local/share/Chudware/config.json` on every dispatch. A GUI toggle must never be
     able to turn the test suite red; `services/mcp/conftest.py::_hermetic_config` now
     rebinds `services.paths.CONFIG` to a throwaway file for the session.

The refusal-shape tests register their own tools through the REAL `tool_registry.view()`
onto a REAL FastMCP instance and dispatch through `mcp.call_tool`, so FastMCP's output
validation — the thing that actually crashed — runs for every declared return type that
appears in the live catalog (str, dict, list, Any, Image).

Run:  python3 -m pytest services/mcp/test_family_gate_refusal.py -q
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import pytest

from services.mcp import tool_registry as reg

try:
    from mcp.server.fastmcp import FastMCP, Image
    from mcp.server.fastmcp.exceptions import ToolError
except Exception as exc:  # pragma: no cover — `mcp` SDK absent
    FastMCP = None  # type: ignore[assignment]
    _SDK_ERROR: Exception | None = exc
else:
    _SDK_ERROR = None

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
)

pytestmark = pytest.mark.skipif(
    FastMCP is None, reason=f"mcp SDK unavailable: {_SDK_ERROR}"
)


def _dispatch(mcp, name: str, args: dict):
    """Call a registered tool the way a client does.

    Returns ``(content_blocks, structured_or_None)``. FastMCP only produces structured
    output — and therefore only VALIDATES the return value — when the annotation yields an
    output schema: a bare ``dict``/``list``/no annotation gets none (verified against the
    live server: ``Tool.output_schema is None``), while ``-> str`` gets
    ``{"result": {"type": "string"}}``. That asymmetry is exactly why only the
    ``str``-declared tools crashed in the field.
    """
    res = asyncio.run(mcp.call_tool(name, args))
    if isinstance(res, tuple) and len(res) == 2:
        return res[0], res[1]
    return res, None


def _text(content) -> str:
    """The single text block a refusal turns into on the wire."""
    assert content, "tool returned no content"
    return content[0].text


class TestRefusalConformsToDeclaredOutputSchema(unittest.TestCase):
    """A disabled family produces a machine-readable refusal in the tool's own shape."""

    FAMILY = "recon"

    def setUp(self):
        self._catalog = copy.deepcopy(reg.catalog())
        reg.reset_policy()
        self.mcp = FastMCP("gate-test")
        view = reg.view(self.mcp, self.FAMILY)

        @view.tool()
        def gated_str(x: int = 1) -> str:
            return "ran"

        @view.tool()
        def gated_dict(x: int = 1) -> dict:
            return {"ok": True}

        @view.tool()
        def gated_list(x: int = 1) -> list:
            return ["ran"]

        @view.tool()
        def gated_any(x: int = 1):
            return {"ok": True}

        @view.tool()
        def gated_image(x: int = 1) -> Image:
            return Image(data=b"\x89PNG", format="png")

        # Deny the whole family exactly as the Settings toggle does: an empty allow-list
        # would exercise a different branch of deny_reason.
        self._patch = unittest.mock.patch(
            "services.agent.config.is_tool_family_enabled",
            side_effect=lambda fam, cfg=None: fam != self.FAMILY,
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        reg.reset_policy()
        reg._CATALOG.clear()
        reg._CATALOG.update(self._catalog)

    def _assert_teaches(self, payload: dict):
        self.assertFalse(payload["ok"], payload)
        self.assertEqual(payload["family"], self.FAMILY, payload)
        self.assertIn(self.FAMILY, payload["reason"])
        self.assertIn("agent.tool_families", payload["reason"])
        # It must name the offending value AND how to re-enable it.
        self.assertIn("Settings -> AI -> Tools", payload["hint"])
        self.assertIn(self.FAMILY, payload["hint"])

    def test_declared_str_tool_yields_json_refusal_not_a_schema_crash(self):
        """The reported crash: `-> str` + refusal dict = pydantic ValidationError.

        This is the regression guard for the field bug. Neuter the fix (make
        ``_refusal_shaper`` return the dict verbatim) and this raises
        ``ToolError: 1 validation error for gated_strOutput`` — verified 2026-07-28.
        """
        content, structured = _dispatch(self.mcp, "gated_str", {})
        raw = structured["result"]
        self.assertIsInstance(raw, str)  # conforms to the DECLARED output schema
        self._assert_teaches(json.loads(raw))  # ...and is still machine-readable
        self._assert_teaches(json.loads(_text(content)))  # same text the model reads

    def test_declared_dict_tool_yields_dict_refusal(self):
        """A bare ``-> dict`` gets no output schema, so the dict contract is preserved."""
        content, structured = _dispatch(self.mcp, "gated_dict", {})
        self.assertIsNone(structured)
        self._assert_teaches(json.loads(_text(content)))

    def test_declared_list_tool_yields_list_refusal(self):
        content, _structured = _dispatch(self.mcp, "gated_list", {})
        self._assert_teaches(json.loads(_text(content)))

    def test_unannotated_tool_keeps_the_dict_refusal(self):
        content, _structured = _dispatch(self.mcp, "gated_any", {})
        self._assert_teaches(json.loads(_text(content)))

    def test_unrepresentable_return_type_raises_an_actionable_tool_error(self):
        """`-> Image` cannot carry a payload, so refuse as a normal tool error, never as a
        pydantic traceback. The message still names the family and the fix."""
        with self.assertRaises(ToolError) as ctx:
            _dispatch(self.mcp, "gated_image", {})
        msg = str(ctx.exception)
        self.assertIn(self.FAMILY, msg)
        self.assertIn("Settings -> AI -> Tools", msg)
        self.assertNotIn("validation error", msg)

    def test_enabled_family_still_runs(self):
        """The gate is the only thing being tested — an enabled family is untouched."""
        self._patch.stop()
        try:
            _content, structured = _dispatch(self.mcp, "gated_str", {})
            self.assertEqual(structured["result"], "ran")
        finally:
            self._patch.start()


class TestRealReverseEngineerRefusal(unittest.TestCase):
    """The exact tool from the bug report, on the real server, with recon switched off."""

    def setUp(self):
        reg.reset_policy()

    def test_reverse_engineer_refuses_cleanly(self):
        import services.mcp.server as srv  # registers every tool for real

        self.assertEqual(reg.catalog()["reverse_engineer"]["family"], "recon")
        with unittest.mock.patch(
            "services.agent.config.is_tool_family_enabled",
            side_effect=lambda fam, cfg=None: fam != "recon",
        ):
            _content, structured = _dispatch(
                srv.mcp, "reverse_engineer", {"source": "/nonexistent.step"}
            )
        payload = json.loads(structured["result"])
        self.assertFalse(payload["ok"], payload)
        self.assertEqual(payload["family"], "recon")
        self.assertIn("Settings -> AI -> Tools", payload["hint"])


class TestSuiteIsHermetic(unittest.TestCase):
    """The MCP suite must ignore ~/.local/share/Chudware/config.json entirely."""

    def test_config_path_is_not_the_users(self):
        import services.paths as paths
        from services.agent import config as agent_config

        # conftest._hermetic_config redirected it; the user's file is untouched.
        self.assertNotEqual(str(paths.CONFIG), str(paths.active_root() / "config.json"))
        temp_root = os.path.realpath(tempfile.gettempdir())
        config_path = os.path.realpath(paths.CONFIG)
        self.assertEqual(
            os.path.commonpath((config_path, temp_root)),
            temp_root,
            f"config not in the configured temp dir: {paths.CONFIG}",
        )
        self.assertEqual(agent_config.load_config(), {})

    def test_a_recon_test_passes_even_with_recon_disabled_on_disk(self):
        """End-to-end proof: plant `agent.tool_families.recon = false` in an XDG data home
        and run a real recon test against it in a child process. Before the conftest
        fixture this reproduced the developer-config leak exactly (4 failures in
        test_recon_tools.py); now the on-disk toggle is inert."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="chudware-hermetic-") as tmp:
            root = os.path.join(tmp, "Chudware")
            os.makedirs(root, exist_ok=True)
            with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as fh:
                json.dump({"agent": {"tool_families": {"recon": False}}}, fh)
            env = dict(os.environ, XDG_DATA_HOME=tmp)
            env.pop(
                "CHUDWARE_HOSTD_SOCKET", None
            )  # child spawns its own private daemon
            proc = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-m",
                    "pytest",
                    "services/mcp/test_recon_tools.py::test_reverse_engineer_stl_smoke_is_honest",
                    "-q",
                    "-p",
                    "no:randomly",
                    "--no-header",
                ],
                cwd=_REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
        tail = proc.stdout[-4000:] + proc.stderr[-2000:]
        self.assertEqual(proc.returncode, 0, tail)
        # exit 0 also covers "skipped" and "nothing collected" — demand a real pass.
        self.assertIn("1 passed", tail, tail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
