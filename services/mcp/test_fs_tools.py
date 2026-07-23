"""General-purpose filesystem + shell MCP tools (fs_tools.py).

Defends the observable contracts a small model relies on: context-safe reading (never dumps
an unbounded file), selector semantics, safe editing, the shell destructive-command guard,
document routing through office_extract, and correct risk classification via the registry.
"""
from __future__ import annotations

import os
import tempfile

import services.mcp.tool_registry as reg
from services.mcp.fs_tools import register_fs_tools, register_shell_tools


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
