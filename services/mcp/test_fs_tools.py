"""General-purpose filesystem + shell MCP tools (fs_tools.py).

Defends the observable contracts a small model relies on: context-safe reading (never dumps
an unbounded file), selector semantics, safe editing, the shell destructive-command guard,
document routing through office_extract, and correct risk classification via the registry.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import subprocess
import tempfile
import time

from pathlib import Path
import pytest

import services.mcp.tool_registry as reg
from services.mcp.fs_tools import register_fs_tools, register_shell_tools


@pytest.fixture(autouse=True)
def _isolated_jail(monkeypatch):
    """Each test may choose a jail; default to the system temp root for legacy fixtures."""
    monkeypatch.setenv("CHUDWARE_FS_ROOT", tempfile.gettempdir())
    for key in ("CHUDWARE_FS_ALLOW_OUTSIDE_ROOT", "CHUDWARE_PROJECT_DIR",
                "CHUDWARE_PROJECT_SAVE_DIR"):
        monkeypatch.delenv(key, raising=False)
    yield
def test_fs_tools_block_escape_symlink_and_recursive_secrets(tmp_path, monkeypatch):
    t = _tools()
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("token")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    assert t["fs_read"](str(outside / "secret.txt"))["reason"] == "outside_workspace_root"
    assert t["fs_read"]("../outside.txt")["reason"] == "not_found"
    assert t["fs_read"]("escape/secret.txt")["reason"] == "outside_workspace_root"

    sensitive = tmp_path / ".ssh"
    sensitive.mkdir()
    (sensitive / "id_rsa").write_text("private")
    assert t["fs_read"](".ssh/id_rsa")["reason"] == "blocked_sensitive_path"
    assert all(".ssh" not in hit for hit in t["fs_glob"]("**/*")["matches"])
    assert t["fs_grep"]("private")["count"] == 0


def test_fs_tools_allow_ordinary_relative_in_root_work(tmp_path, monkeypatch):
    t = _tools()
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    assert t["fs_write"]("nested/file.txt", "hello")["ok"]
    assert t["fs_read"]("nested/file.txt")["ok"]
    assert t["fs_grep"]("hello", path="nested")["count"] == 1


def test_fs_tools_fail_closed_without_root(monkeypatch):
    t = _tools()
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    for name, args in (("fs_read", ("x",)), ("fs_list", (".",)),
                       ("fs_glob", ("*",)), ("fs_grep", ("x",)),
                       ("fs_write", ("x", "y")), ("fs_edit", ("x", "a", "b"))):
        assert t[name](*args)["reason"] == "workspace_root_unavailable"




def test_code_check_jails_doc_path_and_blocks_credentials(tmp_path, monkeypatch):
    from services.mcp.code_compliance_tools import register_code_compliance_tools

    mcp = _FakeMCP()
    register_code_compliance_tools(mcp)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(workspace))
    outside = tmp_path / "outside.json"
    outside.write_text('{"elements": []}')
    assert json.loads(mcp.tools["code_check"](doc_path=str(outside)))["reason"] == (
        "outside_workspace_root")

    credentials = workspace / ".aws"
    credentials.mkdir()
    (credentials / "credentials.json").write_text('{"elements": []}')
    assert json.loads(mcp.tools["code_check"](
        doc_path=str(credentials / "credentials.json")))["reason"] == "blocked_sensitive_path"


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
    assert set(t) == {"fs_read", "fs_list", "fs_glob", "fs_grep", "browse_models",
                      "fs_write", "fs_edit", "run_shell"}


def test_risk_classification():
    _tools()  # populates the catalog via view()
    cat = reg.catalog()
    for name in ("fs_read", "fs_list", "fs_glob", "fs_grep", "browse_models"):
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
        assert "\t" not in r["text"] and r["text"] == "line 5\nline 6\n"


@pytest.mark.parametrize("source", [
    b"int main(void)\n{\n    return 0;\n}\n",          # LF, final newline
    b"alpha\nbeta\n\ngamma",                          # no final newline, blank line
    b"one\r\ntwo\r\n\r\nthree\r\n",                   # CRLF
    b"\n\n",                                          # only terminators
    b"",                                              # empty file
])
def test_fs_read_raw_is_byte_verbatim(tmp_path, monkeypatch, source):
    """raw=True / ':raw' returns the file text verbatim — every line keeps its own
    terminator, so an offset or range window is exactly the matching source slice and a
    file without a final newline stays without one — while the line count is unchanged."""
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    t = _tools()
    p = tmp_path / "src.c"
    p.write_bytes(source)
    physical = source.decode().splitlines(keepends=True)
    numbered = t["fs_read"](str(p))
    assert numbered["total_lines"] == len(physical)

    full = t["fs_read"](str(p), raw=True)
    assert full["ok"] and full["text"] == source.decode()
    assert full["total_lines"] == len(physical) and not full["truncated"]
    assert t["fs_read"](f"{p}:raw")["text"] == source.decode()

    for start in range(1, len(physical) + 1):
        win = t["fs_read"](str(p), offset=start, limit=2, raw=True)
        assert win["text"] == "".join(physical[start - 1:start + 1])
        assert t["fs_read"](f"{p}:{start}-{start}:raw")["text"] == physical[start - 1]


def test_fs_read_raw_pages_reassemble_exact_file(tmp_path, monkeypatch):
    """Paging a raw read with next_offset reassembles the exact file: raw text carries no
    footer, so each page ends on the last shown line's own terminator."""
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    t = _tools()
    p = tmp_path / "big.txt"
    source = "".join(f"line {i}\r\n" for i in range(1, 30)) + "tail without eol"
    p.write_bytes(source.encode())
    got, offset = [], 1
    while True:
        r = t["fs_read"](str(p), offset=offset, limit=7, raw=True)
        assert r["ok"] and r["total_lines"] == 30
        got.append(r["text"])
        if "next_offset" not in r:
            break
        assert r["text"].endswith("\r\n")
        offset = r["next_offset"]
    assert "".join(got) == source


@pytest.mark.parametrize("pretty", [False, True])
def test_fs_read_large_json_pages_parse_after_join(tmp_path, monkeypatch, pretty):
    """c250: a raw read of a one-line 100+ KB result.json came back as one string cut
    mid-JSON by the host-tool result cap, and json.loads failed. A page now fits the
    cap as JSON, says it is a page, and byte_offset pages rejoin to the exact file."""
    from services.agent.chudcode_tools import _RESULT_TEXT_BYTES, host_tool_result

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    t = _tools()
    data = {"summary": {"ok": True}, "rows": [
        {"i": i, "name": f"élément \"{i}\"\t", "v": [i * 0.5] * 6} for i in range(2500)]}
    source = json.dumps(data, indent=1 if pretty else None, ensure_ascii=False)
    p = tmp_path / "result.json"
    p.write_text(source, encoding="utf-8")
    total = len(source.encode("utf-8"))
    assert total > 3 * _RESULT_TEXT_BYTES

    got, at, pages = [], 0, 0
    while True:
        r = t["fs_read"](str(p), byte_offset=at)
        envelope = host_tool_result(r, spill=lambda _v: pytest.fail("page was cut"))
        assert len(envelope["content"][0]["text"].encode("utf-8")) <= _RESULT_TEXT_BYTES
        assert "_truncated" not in envelope["details"]
        assert r["ok"] and r["byte_offset"] == at and r["total_bytes"] == total
        assert r["bytes"] == len(r["text"].encode("utf-8"))
        got.append(r["text"])
        pages += 1
        if not r["truncated"]:
            assert "next_byte_offset" not in r
            break
        assert r["next_byte_offset"] == at + r["bytes"]
        assert pretty == ("next_offset" in r)  # one-line JSON can only page by bytes
        at = r["next_byte_offset"]
    assert pages > 1
    assert json.loads("".join(got)) == data

    # The c250 call shape: one raw line larger than a page is split, never dumped whole.
    first = t["fs_read"](str(p), raw=True, limit=1)
    assert first["truncated"] and first["next_byte_offset"] == first["bytes"] < total
    assert t["fs_read"](str(p), byte_offset=total + 1)["reason"] == "byte_offset_out_of_range"


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


@pytest.mark.parametrize("suffix", ["odt", "odp", "ods", "ppt", "xls"])
def test_fs_read_routes_converted_office_formats_through_office_extract(
    tmp_path, monkeypatch, suffix
):
    called = []

    def _fake_extract(path):
        called.append(path)
        return {"ok": True, "kind": suffix, "text": f"routed {suffix} text"}

    monkeypatch.setattr("services.mcp.office_tools.extract_office", _fake_extract)
    path = tmp_path / f"fixture.{suffix}"
    path.write_bytes(b"PK\x03\x04binary office fixture")

    result = _tools()["fs_read"](str(path))

    assert result["ok"] and result["is_document"] and result["kind"] == suffix
    assert f"routed {suffix} text" in result["text"]
    assert called == [str(path)]


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


# --------------------------------------------------------------------------- CAD preview

def test_fs_read_corrupt_step_plus_is_a_failed_preview_pointing_at_import_part():
    """A .step+ is never dumped as binary. A container that cannot be read is a
    failure carrying its card's reason, and still points at import_part — even with
    raw=True."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "CD-1102_MudBucket_OilRig.step+")
        with open(p, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 64)
        for kwargs in ({}, {"raw": True}):
            r = t["fs_read"](p, **kwargs)
            assert not r["ok"] and r["reason"] == "use_import_part"
            assert "unreadable" in r["card"]["summary"]
            assert "import_part" in r["hint"]
            assert "describe_part" in r["hint"] and "measure_part" in r["hint"]


def test_fs_read_draw_plus_names_import_part():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.draw+")
        with open(p, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 64)
        r = t["fs_read"](p)
        assert f'import_part(path="{p}")' in r["hint"]


def test_fs_read_text_step_previews_but_raw_true_escapes():
    """Plain-text B-rep exchange formats preview by default (header card + the route to
    the geometry reader), but an explicit raw=True proceeds with the normal text read."""
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "part.step")
        with open(p, "w") as fh:
            fh.write("ISO-10303-21;\nHEADER;\nFILE_NAME('bracket','2026',(''),(''),'',"
                     "'SolidWorks 2024','');\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\n")
        r = t["fs_read"](p)
        assert r["ok"] and r["kind"] == "cad_preview" and r["open_with"] == "import_part"
        assert r["card"]["name"] == "bracket" and r["card"]["schema"] == "AUTOMOTIVE_DESIGN"
        assert "raw=True" in r["hint"]
        r2 = t["fs_read"](p, raw=True)
        assert r2["ok"] and "ISO-10303-21" in r2["text"]


def test_fs_read_dxf_mentions_wires_and_allows_raw():
    t = _tools()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "plate.dxf")
        with open(p, "w") as fh:
            fh.write("0\nSECTION\n2\nENTITIES\n")
        r = t["fs_read"](p)
        assert r["ok"] and r["kind"] == "cad_preview"
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
    bad_commands = (
        "rm -rf /", "rm -rf /*", "rm -r /etc", "rm -f /home/",
        "rm -rf ~", "rm -rf ~/", "rm -rf ~/*",
        "rm -rf $HOME", "rm -rf $HOME/", "rm -rf $HOME/*",
        "rm -rf ${HOME}", "rm -rf ${HOME}/", "rm -rf ${HOME}/*",
        "rm -r -f -- /usr", "rm -rf harmless /var/*",
        "sudo rm x", "dd if=/dev/zero of=/dev/sda", ":(){ :|:& };:",
    )
    for bad in bad_commands:
        r = t["run_shell"](bad)
        assert not r["ok"] and r["reason"] == "blocked", bad


def test_run_shell_keeps_project_relative_cleanup_usable(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    (tmp_path / "scratch").mkdir()
    result = _tools()["run_shell"]("rm -rf scratch")
    assert result["ok"] and result["exit_code"] == 0
    assert not (tmp_path / "scratch").exists()


def test_run_shell_timeout_preserves_json_safe_partial_output(tmp_path):
    script = tmp_path / "partial_output.py"
    script.write_text(
        "import os, time\n"
        "os.write(1, b'out\\xe2\\x98\\x83\\n')\n"
        "os.write(2, b'err\\xff\\n')\n"
        "time.sleep(2)\n"
    )
    r = _tools()["run_shell"](f'"{sys.executable}" "{script}"', timeout_s=1)
    assert not r["ok"] and r["reason"] == "shell_deadline_exceeded"
    assert r["timed_out"] and r["exit_code"] == 124
    assert "out\u2603" in r["output"] and "err\ufffd" in r["output"]
    json.dumps(r)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group lifecycle")
def test_run_shell_timeout_stops_descendant_writes(tmp_path):
    marker = tmp_path / "orphan_write"
    pids = tmp_path / "pids.json"
    child = tmp_path / "child.py"
    child.write_text(
        "import time\nfrom pathlib import Path\n"
        f"time.sleep(2)\nPath({str(marker)!r}).write_text('orphan')\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, {str(child)!r}])\n"
        f"Path({str(pids)!r}).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(10)\n"
    )
    try:
        result = _tools()["run_shell"](
            f'"{sys.executable}" "{parent}"', timeout_s=1)
        assert result["timed_out"] and result["exit_code"] == 124
        assert pids.exists(), "the child must have launched before the deadline"
        time.sleep(2.2)
        assert not marker.exists(), "a timed-out command left a writable descendant"
    finally:
        if pids.exists():
            for pid in json.loads(pids.read_text()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_run_shell_refuses_out_of_range_deadline_before_execution(tmp_path):
    from services.mcp.fs_tools import SHELL_MAX_TIMEOUT_S

    marker = tmp_path / "launched"
    script = tmp_path / "must_not_run.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    result = _tools()["run_shell"](
        f'"{sys.executable}" "{script}"', timeout_s=SHELL_MAX_TIMEOUT_S + 1)
    assert not result["ok"] and result["reason"] == "invalid_timeout"
    assert not marker.exists()


def _observe_file_reads(monkeypatch, *, append_after_first_read=False):
    """Instrument real admitted descriptors, never substitute file contents."""
    from services.mcp import fs_tools

    original = fs_tools._open_read_file
    observed = []
    grew = False

    class ObservedFile:
        def __init__(self, path):
            self.path = path
            self.file = original(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self, count):
            nonlocal grew
            data = self.file.read(count)
            observed.append((count, len(data)))
            if append_after_first_read and not grew:
                grew = True
                with self.path.open("ab") as writer:
                    writer.write(b"growth\n")
            return data

    monkeypatch.setattr(fs_tools, "_open_read_file", ObservedFile)
    return observed


def test_fs_read_large_line_uses_bounded_reads_and_exact_byte_pages(tmp_path, monkeypatch):
    from services.mcp import fs_tools
    from services.agent.chudcode_tools import _RESULT_TEXT_BYTES, host_tool_result

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    path = tmp_path / "wide.txt"
    source = ("é" * (2 * 1024 * 1024) + "\r\ntail").encode()
    path.write_bytes(source)
    observed = _observe_file_reads(monkeypatch)
    tools = _tools()
    numbered = tools["fs_read"]("wide.txt:1-1")
    assert numbered["ok"] and numbered["total_lines"] == 2
    assert numbered["shown_lines"] == 1 and "[+" in numbered["text"]
    first = tools["fs_read"]("wide.txt", byte_offset=0)
    assert first["ok"] and first["truncated"]
    # The host result budgets its actual Unicode-preserving JSON text, not
    # json.dumps' default ASCII expansion (which triples this accented page).
    next_at = first["next_byte_offset"]
    second = tools["fs_read"]("wide.txt", byte_offset=next_at)
    assert second["ok"] and second["byte_offset"] == next_at
    for page in (first, second):
        envelope = host_tool_result(
            page, spill=lambda _value: pytest.fail("read page exceeded host result budget"))
        wire_text = envelope["content"][0]["text"]
        assert len(wire_text.encode("utf-8")) <= _RESULT_TEXT_BYTES
        assert json.loads(wire_text) == page
        assert envelope["details"] == page
        assert len(json.dumps(page["text"], ensure_ascii=False).encode("utf-8")) - 2 <= (
            fs_tools.READ_PAGE_BYTES)
        assert page["bytes"] == len(page["text"].encode("utf-8"))
        assert page["next_byte_offset"] == page["byte_offset"] + page["bytes"]
    joined = (first["text"] + second["text"]).encode()
    assert joined == source[:len(joined)]
    assert max(count for count, _ in observed) <= fs_tools.IO_CHUNK_BYTES
    assert all(count >= size for count, size in observed)


def test_fs_read_refuses_oversized_snapshot_before_reading(tmp_path, monkeypatch):
    from services.mcp import fs_tools

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    path = tmp_path / "huge.txt"
    with path.open("wb") as stream:
        stream.truncate(fs_tools.READ_MAX_INPUT_BYTES + 1)
    observed = _observe_file_reads(monkeypatch)
    result = _tools()["fs_read"]("huge.txt", byte_offset=0)
    assert not result["ok"] and result["reason"] == "read_budget_exceeded"
    assert observed == [] and "text" not in result


@pytest.mark.parametrize("tool", ["fs_read", "fs_grep"])
def test_fs_tools_refuse_growth_without_following_new_bytes(tmp_path, monkeypatch, tool):
    from services.mcp import fs_tools

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    path = tmp_path / "growing.txt"
    source = b"a" * (3 * fs_tools.IO_CHUNK_BYTES) + b"\n"
    path.write_bytes(source)
    observed = _observe_file_reads(monkeypatch, append_after_first_read=True)
    tools = _tools()
    result = tools[tool]("growing.txt") if tool == "fs_read" else tools[tool]("a", path="growing.txt")
    assert not result["ok"] and result["reason"] == "file_changed"
    assert sum(size for _, size in observed) == len(source)
    assert max(count for count, _ in observed) <= fs_tools.IO_CHUNK_BYTES
    assert "matches" not in result and "text" not in result


def test_fs_read_byte_selector_handles_utf8_boundary_and_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    (tmp_path / "unicode.txt").write_bytes("aéz\r\nlast".encode())
    tools = _tools()
    result = tools["fs_read"]("unicode.txt", byte_offset=2)
    assert result["ok"] and result["byte_offset"] == 1 and result["text"] == "éz\r\nlast"
    assert tools["fs_read"]("unicode.txt:1-1:raw")["text"] == "aéz\r\n"
    assert tools["fs_read"]("unicode.txt:1-1", byte_offset=0)["reason"] == "conflicting_offsets"
    assert tools["fs_read"]("unicode.txt", offset=1, byte_offset=0)["reason"] == "conflicting_offsets"


def test_fs_grep_regex_semantics_caps_and_large_file_skip(tmp_path, monkeypatch):
    from services.mcp import fs_tools

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "ordinary.txt").write_text("Alpha\r\nbeta\nALPHA\n", encoding="utf-8")
    with (nested / "oversized.txt").open("wb") as stream:
        stream.write(b"Alpha\n")
        stream.truncate(fs_tools.GREP_MAX_FILE_BYTES + 1)
    tools = _tools()
    result = tools["fs_grep"]("^alpha$", path="nested", glob="*.txt", ignore_case=True)
    assert result["ok"] and result["count"] == 2 and result["files_scanned"] == 2
    assert [match["line"] for match in result["matches"]] == [1, 3]
    assert [match["text"] for match in result["matches"]] == ["Alpha", "ALPHA"]
    capped = tools["fs_grep"]("alpha", path="nested", ignore_case=True, max_matches=1)
    assert capped["ok"] and capped["count"] == 1 and capped["truncated"]
    invalid = tools["fs_grep"]("(", path="nested")
    assert not invalid["ok"] and invalid["reason"] == "bad_pattern"


def test_fs_grep_input_and_output_budget_refusals_are_not_partial_success(tmp_path, monkeypatch):
    from services.mcp import fs_tools

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    (tmp_path / "a.txt").write_text("x" * 200)
    (tmp_path / "b.txt").write_text("x" * 200)
    monkeypatch.setattr(fs_tools, "GREP_MAX_INPUT_BYTES", 256)
    result = _tools()["fs_grep"]("never", path=".")
    assert not result["ok"] and result["reason"] == "search_budget_exceeded"
    assert "matches" not in result
    monkeypatch.setattr(fs_tools, "GREP_MAX_INPUT_BYTES", 64 * 1024 * 1024)
    (tmp_path / "wide.txt").write_text(("match " + '"' * 1500 + "\n") * 100)
    result = _tools()["fs_grep"]("match", path="wide.txt", max_matches=100)
    assert not result["ok"] and result["reason"] == "search_output_budget_exceeded"
    assert "matches" not in result


def test_fs_enumeration_budgets_and_glob_selector_order(tmp_path, monkeypatch):
    from services.mcp import fs_tools

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    nested = tmp_path / "nested"
    nested.mkdir()
    for index in range(6):
        path = nested / f"{index}.txt"
        path.write_text("needle\n")
        os.utime(path, ns=(index + 1, index + 1))
    tools = _tools()
    result = tools["fs_glob"]("nested/**/*.txt", limit=2)
    assert result["ok"] and result["truncated"]
    assert [Path(match).name for match in result["matches"]] == ["5.txt", "4.txt"]
    assert tools["fs_glob"]("*.txt")["matches"] == []
    monkeypatch.setattr(fs_tools, "FS_MAX_SCAN_ENTRIES", 3)
    for name, args in (("fs_list", ("nested",)), ("fs_glob", ("**/*",)),
                       ("fs_grep", ("needle",))):
        result = tools[name](*args)
        assert not result["ok"] and result["reason"] == "scan_budget_exceeded"
        assert "matches" not in result and "entries" not in result


@pytest.mark.skipif(os.name != "posix", reason="POSIX disposable process cleanup probe")
def test_fs_grep_pathological_regex_keeps_control_responsive_and_reaps_child(tmp_path):
    """Outer deadline also makes the old GIL-holding implementation safely testable."""
    (tmp_path / "evil.txt").write_text("a" * 40 + "!\n")
    (tmp_path / "control.txt").write_text("responsive\n")
    pid_file = tmp_path / "grep-pid"
    code = (
        "import json, os, subprocess, threading, time\n"
        "from pathlib import Path\n"
        "from services.mcp import fs_tools\n"
        "class Registry:\n"
        " def __init__(self): self.tools = {}\n"
        " def tool(self):\n"
        "  def add(fn): self.tools[fn.__name__] = fn; return fn\n"
        "  return add\n"
        "registry = Registry(); fs_tools.register_fs_tools(registry)\n"
        "original = fs_tools.subprocess.Popen\n"
        "entered = threading.Event()\n"
        "def launch(*args, **kwargs):\n"
        " child = original(*args, **kwargs)\n"
        f" Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        " entered.set(); return child\n"
        "fs_tools.subprocess.Popen = launch\n"
        "result = {}\n"
        "def search(): result.update(registry.tools['fs_grep']('(a+)+$', path='evil.txt'))\n"
        "started = time.monotonic()\n"
        "thread = threading.Thread(target=search, daemon=True); thread.start()\n"
        "assert entered.wait(2)\n"
        "time.sleep(0.25)\n"
        "control_started = time.monotonic()\n"
        "control = registry.tools['fs_read']('control.txt')\n"
        "control_elapsed = time.monotonic() - control_started\n"
        "assert control['ok'] and 'responsive' in control['text']\n"
        "thread.join(8)\n"
        "assert not thread.is_alive()\n"
        "assert not any(t.name in {'fs-process-output', 'fs-grep-input'} for t in threading.enumerate())\n"
        "print(json.dumps({'control': control['ok'], 'search': result, "
        "'elapsed': time.monotonic() - started, 'control_elapsed': control_elapsed}), flush=True)\n"
    )
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "CHUDWARE_FS_ROOT": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=12)
        assert proc.returncode == 0, stderr.decode(errors="replace")
        result = json.loads(stdout)
        assert result["control"] and result["search"]["reason"] == "search_deadline_exceeded"
        assert not result["search"]["ok"]
        assert result["elapsed"] < 8 and result["control_elapsed"] < 1
        child_pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group output budget")
def test_run_shell_output_budget_stops_real_producer_and_descendant(tmp_path):
    from services.mcp.fs_tools import SHELL_MAX_OUTPUT_BYTES

    marker = tmp_path / "late-write"
    pids = tmp_path / "output-pids.json"
    child = tmp_path / "late.py"
    child.write_text(
        "import time\nfrom pathlib import Path\n"
        f"time.sleep(2)\nPath({str(marker)!r}).write_text('escaped')\n")
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import json, os, subprocess, sys\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, {str(child)!r}])\n"
        f"Path({str(pids)!r}).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "while True:\n"
        " os.write(1, b'x' * 16384)\n"
        " os.write(2, b'y' * 16384)\n")
    try:
        started = time.monotonic()
        result = _tools()["run_shell"](f'"{sys.executable}" "{producer}"', timeout_s=20)
        assert time.monotonic() - started < 8
        assert not result["ok"] and result["reason"] == "output_budget_exceeded"
        assert result["truncated"] and not result["timed_out"]
        assert len(result["output"].encode()) <= SHELL_MAX_OUTPUT_BYTES + 128
        assert pids.exists() and "x" in result["output"] and "y" in result["output"]
        time.sleep(2.2)
        assert not marker.exists()
    finally:
        if pids.exists():
            for pid in json.loads(pids.read_text()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_run_shell_closes_stdin_and_keeps_bounded_success_output(tmp_path):
    from services.mcp.fs_tools import SHELL_MAX_OUTPUT_BYTES

    producer = tmp_path / "finite.py"
    producer.write_text(
        "import os, sys\n"
        "assert sys.stdin.buffer.read() == b''\n"
        f"os.write(1, b'x' * {SHELL_MAX_OUTPUT_BYTES})\n")
    result = _tools()["run_shell"](f'"{sys.executable}" "{producer}"', timeout_s=5)
    assert result["ok"] and not result["truncated"] and not result["timed_out"]
    assert len(result["output"]) == SHELL_MAX_OUTPUT_BYTES


def test_fs_grep_file_count_budget_is_a_refusal(tmp_path, monkeypatch):
    from services.mcp import fs_tools

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(fs_tools, "MAX_GREP_FILES", 2)
    for index in range(3):
        (tmp_path / f"{index}.txt").write_text("needle\n")
    result = _tools()["fs_grep"]("needle")
    assert not result["ok"] and result["reason"] == "search_budget_exceeded"
    assert "matches" not in result and "count" not in result


def test_fs_grep_ignores_ambient_python_import_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    injection = tmp_path / "injection"
    injection.mkdir()
    marker = tmp_path / "imported-ambient"
    (injection / "re.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('injected')\n"
        "raise RuntimeError('ambient regex import')\n")
    monkeypatch.setenv("PYTHONPATH", str(injection))
    monkeypatch.setenv("PYTHONSTARTUP", str(injection / "re.py"))
    (tmp_path / "ordinary.txt").write_text("needle\n")
    result = _tools()["fs_grep"]("needle", path="ordinary.txt")
    assert result["ok"] and result["count"] == 1 and not marker.exists()


def test_fs_grep_preserves_request_local_nested_workspace_jail(tmp_path, monkeypatch):
    from services.agent.request_context import bind_roots

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    selected = tmp_path / "selected"
    selected.mkdir()
    (tmp_path / "outside-selected.txt").write_text("not-admitted\n")
    (selected / "alias.txt").symlink_to(tmp_path / "outside-selected.txt")
    (selected / "inside.txt").write_text("ordinary\n")
    tools = _tools()
    with bind_roots(str(selected)):
        result = tools["fs_grep"]("not-admitted", path=".")
        assert result["ok"] and result["count"] == 0
        assert tools["fs_grep"]("ordinary", path=".")["count"] == 1
