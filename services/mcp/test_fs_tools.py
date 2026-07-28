"""General-purpose filesystem + shell MCP tools (fs_tools.py).

Defends the observable contracts a small model relies on: context-safe reading (never dumps
an unbounded file), selector semantics, safe editing, the shell destructive-command guard,
document routing through office_extract, and correct risk classification via the registry.
"""
from __future__ import annotations

import os
import tempfile

import pytest

import services.mcp.tool_registry as reg
from services.mcp.fs_tools import register_fs_tools, register_shell_tools


@pytest.fixture(autouse=True)
def _no_ambient_jail(monkeypatch):
    """fs_tools reads its jail policy from the environment, so these tests MUST pin
    it rather than inherit it.

    Without this, any earlier test that exercised the real
    worker_rpc._agent_admin_turn (which sets CHUDWARE_FS_ROOT via os.environ, not
    monkeypatch) left a jail root behind and the write-safety cases failed only
    when run after it — a pollution that hid until fs and agent tests first shared
    a session.
    """
    for key in ("CHUDWARE_FS_ROOT", "CHUDWARE_FS_ALLOW_OUTSIDE_ROOT",
                "CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR"):
        monkeypatch.delenv(key, raising=False)
    yield


class _FakeMCP:
    """Captures @mcp.tool()-decorated fns by name (what FastMCP does under the hood)."""

    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools():
    m = _FakeMCP()
    register_fs_tools(reg.view(m, "files"))
    register_shell_tools(reg.view(m, "shell"))
    return m.tools


def _big(dirpath, n=2000):
    p = os.path.join(dirpath, "big.txt")
    with open(p, "w") as fh:
        for i in range(1, n + 1):
            fh.write(f"line {i}\n")
    return p


# --------------------------------------------------------------------------- registration

def test_all_tools_register():
    t = _tools()
    assert set(t) == {"fs_read", "fs_list", "fs_glob", "fs_grep",
                      "fs_write", "fs_edit", "run_shell"}


def test_risk_classification():
    _tools()  # populates the catalog via view()
    cat = reg.catalog()
    for name in ("fs_read", "fs_list", "fs_glob", "fs_grep"):
        assert cat[name]["risk"] == "read", name
    for name in ("fs_write", "fs_edit", "run_shell"):
        assert cat[name]["risk"] == "write", name
    assert cat["run_shell"]["family"] == "shell"
    assert cat["fs_read"]["family"] == "files"


# --------------------------------------------------------------------------- context-safe read

def test_fs_read_caps_and_reports_total():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        r = t["fs_read"](_big(d))
        assert r["ok"]
        assert r["shown_lines"] == 400          # default window cap
        assert r["total_lines"] == 2000         # true length always reported
        assert r["truncated"] and r["next_offset"] == 401
        assert r["text"].lstrip().startswith("1\t")  # line-numbered


def test_fs_read_never_exceeds_hard_line_cap():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        r = t["fs_read"](_big(d, n=9000), limit=100000)
        assert r["shown_lines"] <= 4000


def test_fs_read_selectors():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = _big(d)
        assert t["fs_read"](p + ":50-52")["shown_lines"] == 3
        r = t["fs_read"](p + ":100+2")
        assert "line 100" in r["text"] and "line 101" in r["text"] and "line 102" not in r["text"]
        r = t["fs_read"](p + ":1-2,1000-1001")
        assert "line 1\n" in r["text"] and "line 1000" in r["text"]
        r = t["fs_read"](p + ":5-6:raw")
        assert "\t" not in r["text"] and r["text"] == "line 5\nline 6"


def test_fs_read_offset_limit():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        r = t["fs_read"](_big(d), offset=1990, limit=5)
        assert r["shown_lines"] == 5 and "line 1990" in r["text"]


def test_fs_read_refuses_binary():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "b.bin")
        with open(p, "wb") as fh:
            fh.write(b"\x00\x01data")
        r = t["fs_read"](p)
        assert not r["ok"] and r["reason"] == "binary_file"


def test_fs_read_missing_and_dir():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        assert t["fs_read"](os.path.join(d, "nope"))["reason"] == "not_found"
        _big(d)
        r = t["fs_read"](d)
        assert r["ok"] and any(e["name"] == "big.txt" for e in r["entries"])


def test_fs_read_routes_documents_through_office_extract():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        csv = os.path.join(d, "parts.csv")
        with open(csv, "w") as fh:
            fh.write("id,name\n1,bolt\n2,nut\n")
        r = t["fs_read"](csv)
        assert r["ok"] and r["is_document"] and r["kind"] == "csv"
        assert "bolt" in r["text"]
        # documents paginate identically
        assert t["fs_read"](csv, offset=2, limit=1)["shown_lines"] == 1


def test_fs_read_document_with_space_and_ampersand_path():
    """Paths containing spaces and '&' must survive selector splitting + document routing
    (the user's project literally has a 'CAD & Design Geometry' folder)."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        nested = os.path.join(d, "CAD & Design Geometry")
        os.makedirs(nested)
        csv = os.path.join(nested, "parts list.csv")
        with open(csv, "w") as fh:
            fh.write("id,name\n1,bolt\n2,nut\n3,washer\n")
        r = t["fs_read"](csv)
        assert r["ok"] and r["kind"] == "csv" and "washer" in r["text"]
        r2 = t["fs_read"](csv + ":2-3")
        assert r2["ok"] and r2["shown_lines"] == 2 and "bolt" in r2["text"]


def test_fs_read_empty_extraction_is_flagged(monkeypatch):
    """An image-only/scanned document extracts to header lines only — fs_read must not
    silently report that as a successful read; it must carry a hint."""
    t = _tools()

    def _fake_extract(_path):
        return {"ok": True, "kind": "pdf", "text": "# Page 1\n# Page 2\n"}

    monkeypatch.setattr("services.mcp.office_tools.extract_office", _fake_extract)
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "scanned.pdf")
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF-fake")
        r = t["fs_read"](pdf)
        assert r["ok"]  # the file read fine — but not silently
        assert "hint" in r and "empty" in r["hint"]

    def _real_extract(_path):
        return {"ok": True, "kind": "pdf", "text": "# Page 1\nactual words\n"}

    monkeypatch.setattr("services.mcp.office_tools.extract_office", _real_extract)
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "real.pdf")
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF-fake")
        assert "hint" not in t["fs_read"](pdf)


# --------------------------------------------------------------------------- search / list

def test_fs_glob_and_grep():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "sub", "x.py"), "w") as fh:
            fh.write("needle here\n")
        g = t["fs_glob"]("**/*.py", path=d)
        assert g["ok"] and any(p.endswith("x.py") for p in g["matches"])
        gr = t["fs_grep"]("needle", path=d)
        assert gr["ok"] and gr["count"] == 1 and gr["matches"][0]["line"] == 1
        assert not t["fs_grep"]("(", path=d)["ok"]  # invalid regex


# --------------------------------------------------------------------------- near-miss recovery

def test_fs_read_not_found_suggests_step_plus_sibling():
    """Our own dual-format trap: CD-1102_MudBucket_OilRig.step does not exist but the
    .step+ native container sits in that exact directory — the miss must suggest it in
    did_you_mean AND in hint (a model that only reads hint still recovers)."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        sibling = os.path.join(d, "CD-1102_MudBucket_OilRig.step+")
        with open(sibling, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 100)
        r = t["fs_read"](os.path.join(d, "CD-1102_MudBucket_OilRig.step"))
        assert not r["ok"] and r["reason"] == "not_found"
        assert r["did_you_mean"] and r["did_you_mean"][0] == sibling
        assert "CD-1102_MudBucket_OilRig.step+" in r["hint"]


def test_fs_read_not_found_suggests_misspelled_directory():
    """A wrong folder NAME is diagnosed too: the parent chain is walked up to the nearest
    existing ancestor and the first missing component is close-matched."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        real_dir = os.path.join(d, "Model Step files")
        os.makedirs(real_dir)
        missing = os.path.join(d, "Model Step filez", "part.step")
        r = t["fs_read"](missing)
        assert not r["ok"] and r["reason"] == "not_found"
        assert any(s.endswith("Model Step files") for s in r["did_you_mean"])


def test_not_found_recovery_shared_by_list_glob_grep_edit():
    """_near_misses is wired into every read-class not_found, not just fs_read."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        sibling = os.path.join(d, "bracket.step+")
        with open(sibling, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 32)
        missing = os.path.join(d, "bracket.step")
        for call in (
            lambda: t["fs_list"](missing),
            lambda: t["fs_glob"]("*", path=missing),
            lambda: t["fs_grep"]("needle", path=missing),
            lambda: t["fs_edit"](missing, "a", "b"),
        ):
            r = call()
            assert not r["ok"] and r["reason"] == "not_found"
            assert sibling in r["did_you_mean"] and sibling in r["hint"]


def test_fs_read_not_found_in_unreadable_parent_still_clean():
    """A huge/unreadable parent degrades to NO suggestions — never an exception."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        locked = os.path.join(d, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o000)
        try:
            r = t["fs_read"](os.path.join(locked, "missing.txt"))
            assert not r["ok"] and r["reason"] == "not_found"
            assert not r.get("did_you_mean")
        finally:
            os.chmod(locked, 0o755)


# --------------------------------------------------------------------------- CAD redirect

def test_fs_read_step_plus_redirects_to_import_part():
    """fs_read on a real .step+ (ZIP container) must not answer a useless binary_file —
    it redirects to import_part + describe/measure/inspect, even with raw=True."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "CD-1102_MudBucket_OilRig.step+")
        with open(p, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 64)
        for kwargs in ({}, {"raw": True}):
            r = t["fs_read"](p, **kwargs)
            assert not r["ok"] and r["reason"] == "use_import_part"
            assert "import_part" in r["hint"]
            assert "describe_part" in r["hint"] and "measure_part" in r["hint"]


def test_fs_read_text_step_redirects_but_raw_true_escapes():
    """Plain-text B-rep exchange formats still redirect by default (the geometry reader
    is the right tool), but an explicit raw=True proceeds with the normal text read."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "part.step")
        with open(p, "w") as fh:
            fh.write("ISO-10303-21;\nHEADER;\nENDSEC;\n")
        r = t["fs_read"](p)
        assert not r["ok"] and r["reason"] == "use_import_part"
        assert "import_part" in r["hint"] and "raw=True" in r["hint"]
        r2 = t["fs_read"](p, raw=True)
        assert r2["ok"] and "ISO-10303-21" in r2["text"]


def test_fs_read_dxf_mentions_wires_and_allows_raw():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "plate.dxf")
        with open(p, "w") as fh:
            fh.write("0\nSECTION\n2\nENTITIES\n")
        r = t["fs_read"](p)
        assert not r["ok"] and r["reason"] == "use_import_part"
        assert "import_part" in r["hint"] and "wires" in r["hint"] and "raw=True" in r["hint"]
        assert t["fs_read"](p, raw=True)["ok"]


# --------------------------------------------------------------------------- write / edit

def test_fs_write_and_edit_safety():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "deep", "f.txt")
        assert t["fs_write"](p, "hello\nhello\n")["ok"]  # makedirs
        # ambiguous edit refused
        r = t["fs_edit"](p, "hello", "HI")
        assert not r["ok"] and r["reason"] == "ambiguous"
        # replace_all works
        r = t["fs_edit"](p, "hello", "HI", replace_all=True)
        assert r["ok"] and r["replacements"] == 2
        assert open(p).read() == "HI\nHI\n"
        # missing substring
        assert t["fs_edit"](p, "zzz", "y")["reason"] == "not_found_in_file"


# --------------------------------------------------------------------------- shell

def test_run_shell_captures_output_and_exit_code():
    t = _tools()
    r = t["run_shell"]("echo out && echo err 1>&2")
    assert r["ok"] and "out" in r["output"] and "err" in r["output"] and r["exit_code"] == 0
    assert t["run_shell"]("exit 3")["exit_code"] == 3


def test_run_shell_blocks_destructive():
    t = _tools()
    for bad in ("rm -rf /", "sudo rm x", "dd if=/dev/zero of=/dev/sda", ":(){ :|:& };:"):
        r = t["run_shell"](bad)
        assert not r["ok"] and r["reason"] == "blocked", bad


def test_run_shell_timeout():
    t = _tools()
    r = t["run_shell"]("sleep 5", timeout_s=1)
    assert r["ok"] and r["timed_out"] and r["exit_code"] == 124
