"""Offline coverage of the public scrape queue status boundary."""
from __future__ import annotations

import hashlib
import json
import threading
import zipfile
from types import SimpleNamespace

import pytest

from services.browser import adapters, queue
from services.agent import request_context


@pytest.fixture
def run_job(monkeypatch):
    monkeypatch.setattr(queue, "_jobs", [])

    def run(adapter, *, preset=None):
        monkeypatch.setattr(adapters, "get_adapter", lambda site: adapter)
        queued = queue.enqueue("example", "bearing", preset=preset)
        assert queued["ok"]
        job = queue._claim_next_job()
        assert job.id == queued["id"]
        queue._run_job(job)
        # Exercise the same JSON boundary used by the MCP queue status tool.
        response = json.loads(json.dumps(queue.queue_status()))
        assert response["ok"]
        return response["jobs"][0]

    return run


def test_enqueue_and_cancel_failures_are_structured(monkeypatch):
    monkeypatch.setattr(queue, "_jobs", [])
    invalid = queue.enqueue("vendor", "")
    assert invalid["reason"] == "parse_failed" and invalid["op"] == "enqueue"
    assert invalid["site"] == "vendor" and invalid["hint"]
    assert all(key in invalid for key in ("url", "status", "retry_after_s", "ask_user"))

    missing = queue.cancel_job("unknown")
    assert missing["reason"] == "not_found" and missing["op"] == "cancel"
    queued = queue.enqueue("vendor", "part")
    assert queue.cancel_job(queued["id"])["ok"]
    cancelled = queue.cancel_job(queued["id"])
    assert cancelled["reason"] == "policy_refused" and cancelled["site"] == "vendor"


def test_search_notice_preserves_c1_failure_and_question(run_job):
    notice = {
        "ok": False, "site": "example", "op": "search", "reason": "rate_limited",
        "hint": "Wait before trying again", "url": "https://example.com/search?q=bearing",
        "status": 429, "retry_after_s": 37.5,
        "ask_user": {"id": "retry", "kind": "approval"}, "detail": "quota reached",
    }
    adapter = SimpleNamespace(search=lambda query: [notice],
                              acquire=lambda hit: pytest.fail("notice is not a hit"))
    job = run_job(adapter)
    assert job["status"] == "error"
    assert job["error"] == notice
    assert job["search_hits"] == [notice]
    assert job["acquire_errors"] == []
    assert job["artifact_path"] == ""


def test_credentials_required_stays_distinct_from_browser_login(run_job):
    notice = {"ok": False, "site": "example", "op": "search",
              "reason": "credentials_required", "hint": "Configure API keys in Settings",
              "url": "https://example.com/search", "status": 401, "retry_after_s": None,
              "ask_user": None}
    job = run_job(SimpleNamespace(search=lambda query: [notice]))
    assert job["status"] == "error"
    assert job["error"] == notice and job["search_hits"] == [notice]


def test_gui_queue_status_keeps_structured_failure(run_job):
    from services.parts import gui_rpc

    notice = {"ok": False, "site": "example", "op": "search", "reason": "captcha",
              "hint": "Manual action required", "url": "https://example.com/search",
              "status": 403, "retry_after_s": 5, "ask_user": {"id": "action"}}
    job = run_job(SimpleNamespace(search=lambda query: [notice]))
    response = gui_rpc._parts_queue_status({}, lambda *_: None)
    assert response["ok"] and response["jobs"] == [job]
    assert response["jobs"][0]["error"] == notice


def test_all_search_notices_are_retained(run_job):
    notices = [
        {"ok": False, "site": "example", "op": "search", "reason": "login_required",
         "hint": "Sign in", "url": "https://example.com/one", "status": 401,
         "retry_after_s": None, "ask_user": {"id": "login"}},
        {"ok": False, "site": "example", "op": "search", "reason": "rate_limited",
         "hint": "Wait", "url": "https://example.com/two", "status": 429,
         "retry_after_s": 42, "ask_user": None},
    ]
    adapter = SimpleNamespace(search=lambda query: notices,
                              acquire=lambda hit: pytest.fail("notice is not a hit"))
    job = run_job(adapter)
    assert job["status"] == "error"
    assert job["error"] == notices[0] and job["search_hits"] == notices


def test_legacy_notice_gains_context_without_losing_details(run_job):
    notice = {"ok": False, "reason": "login_required", "hint": "",
              "detail": "Sign in first", "ask_user": {"id": "browser_login.example"}}
    adapter = SimpleNamespace(search=lambda query: [notice],
                              acquire=lambda hit: pytest.fail("notice is not a hit"))
    job = run_job(adapter)
    failure = job["error"]
    assert failure["site"] == "example"
    assert failure["op"] == "search"
    assert failure["reason"] == "login_required"
    assert failure["hint"] == "Sign in first"
    assert failure["ask_user"] == notice["ask_user"]
    assert failure["url"] is None and failure["status"] is None
    assert failure["retry_after_s"] is None
    assert job["search_hits"] == [failure]
    assert notice["hint"] == ""  # normalization does not mutate the adapter's object


@pytest.mark.parametrize(("kind", "format_name"), [
    ("cad", "step"), ("document", "pdf"), ("ecad", "zip"), ("archive", "zip"),
])
def test_partial_search_notices_and_failed_attempt_survive_success(
        run_job, tmp_path, kind, format_name):
    artifact = tmp_path / f"artifact.{format_name}"
    if kind == "cad":
        artifact.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n"
                             b"END-ISO-10303-21;\n")
    elif kind == "document":
        artifact.write_bytes(b"%PDF-1.7\n")
    else:
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("part.kicad_sym" if kind == "ecad" else "readme.txt",
                             "downloaded content")
    notice = {"ok": False, "site": "example", "op": "search", "reason": "blocked",
              "hint": "Some results are unavailable", "url": "https://example.com/search",
              "status": 403, "retry_after_s": 10, "ask_user": None}
    first = {"title": "first", "url": "https://example.com/first"}
    second = {"title": "second", "url": "https://example.com/second"}
    failure = {"ok": False, "site": "example", "op": "acquire", "reason": "captcha",
               "hint": "Manual action required", "url": first["url"],
               "status": 403, "retry_after_s": 0, "ask_user": {"id": "question"}}
    acquired = []

    def acquire(hit):
        acquired.append(hit["title"])
        if hit is first:
            return failure
        return {"ok": True, "artifact_path": str(artifact), "artifact_kind": kind,
                "format": format_name, "source_url": second["url"], "title": "second",
                "cad_validated": True}

    adapter = SimpleNamespace(search=lambda query: [notice, first, second], acquire=acquire)
    job = run_job(adapter)
    assert acquired == ["first", "second"]
    assert job["status"] == "done" and job["error"] is None
    assert job["search_hits"] == [notice, first, second]
    assert job["acquire_errors"] == [failure]
    assert job["artifact_path"] == str(artifact)
    assert job["spec_data"]["artifact_kind"] == kind
    assert job["spec_data"]["format"] == format_name
    assert job["spec_data"]["cad_validated"] is (kind == "cad")
    assert job["spec_data"]["source_url"] == second["url"]
    assert job["spec_data"]["site"] == "example"


def test_all_failed_acquisitions_keep_each_failure_and_last_error(run_job):
    hits = [{"url": "https://example.com/one"}, {"url": "https://example.com/two"}]
    failures = [
        {"ok": False, "reason": "blocked", "hint": "Denied", "status": 403},
        {"ok": False, "site": "example", "op": "acquire", "reason": "rate_limited",
         "hint": "Wait", "url": hits[1]["url"], "status": 429,
         "retry_after_s": 12, "ask_user": {"id": "wait"}},
    ]
    adapter = SimpleNamespace(search=lambda query: hits,
                              acquire=lambda hit: failures[hits.index(hit)])
    job = run_job(adapter)
    assert job["status"] == "error"
    assert job["search_hits"] == hits
    assert len(job["acquire_errors"]) == 2
    first, second = job["acquire_errors"]
    assert (first["reason"], first["status"], first["url"]) == ("blocked", 403, hits[0]["url"])
    assert second == failures[1]
    assert job["error"] == second
    assert job["artifact_path"] == "" and job["spec_data"] == {}


@pytest.mark.parametrize(("case", "reason"), [
    ("absent", "not_found"), ("null", "not_found"), ("missing", "not_found"),
    ("directory", "parse_failed"), ("empty", "parse_failed"), ("invalid", "parse_failed"),
])
def test_success_without_real_nonempty_file_is_an_error(run_job, tmp_path, case, reason):
    missing = tmp_path / "missing.step"
    empty = tmp_path / "empty.step"
    empty.touch()
    result = {"ok": True, "artifact_kind": "cad", "format": "step",
              "status": 200, "retry_after_s": 0}
    values = {"null": None, "missing": str(missing), "directory": str(tmp_path),
              "empty": str(empty), "invalid": 42}
    if case != "absent":
        result["artifact_path"] = values[case]
    hit = {"url": "https://example.com/item"}
    adapter = SimpleNamespace(search=lambda query: [hit], acquire=lambda item: result)
    job = run_job(adapter)
    assert job["status"] == "error" and job["artifact_path"] == ""
    assert job["error"]["reason"] == reason
    assert job["error"]["hint"]
    assert job["error"]["site"] == "example" and job["error"]["op"] == "acquire"
    assert job["error"]["url"] == hit["url"]
    assert job["error"]["status"] == 200 and job["error"]["retry_after_s"] == 0
    assert job["error"]["artifact_kind"] == "cad" and job["error"]["format"] == "step"
    assert job["acquire_errors"] == [job["error"]]
    assert job["search_hits"] == [hit] and job["spec_data"] == {}


@pytest.mark.parametrize(("hits", "reason"), [
    ([], "not_found"), (None, "parse_failed"), ([None], "parse_failed"),
    ([{"ok": False, "hint": ""}], "parse_failed"),
])
def test_empty_or_invalid_search_has_a_nonempty_error(run_job, hits, reason):
    adapter = SimpleNamespace(search=lambda query: hits,
                              acquire=lambda hit: pytest.fail("no usable hit"))
    job = run_job(adapter)
    assert job["status"] == "error"
    assert job["error"]["reason"] == reason
    assert job["error"]["hint"]
    assert job["error"]["site"] == "example" and job["error"]["op"] == "search"


@pytest.mark.parametrize(("exc", "reason"), [
    (TimeoutError("deadline"), "timeout"), (ValueError(), "internal_error"),
])
def test_search_exceptions_are_honest_and_nonempty(run_job, exc, reason):
    def search(query):
        raise exc

    job = run_job(SimpleNamespace(search=search))
    assert job["status"] == "error"
    assert job["error"]["reason"] == reason
    assert type(exc).__name__ in job["error"]["hint"]


def test_acquire_exception_keeps_hit_context(run_job):
    hit = {"url": "https://example.com/item"}

    def acquire(item):
        raise RuntimeError("unexpected adapter failure")

    job = run_job(SimpleNamespace(search=lambda query: [hit], acquire=acquire))
    assert job["status"] == "error"
    assert job["error"]["reason"] == "internal_error"
    assert job["error"]["url"] == hit["url"]
    assert "RuntimeError" in job["error"]["hint"]
    assert job["acquire_errors"] == [job["error"]]


def test_malformed_acquire_reports_failure(run_job):
    hit = {"url": "https://example.com/item"}
    adapter = SimpleNamespace(search=lambda query: [hit], acquire=lambda item: None)
    job = run_job(adapter)
    assert job["status"] == "error" and job["error"]["reason"] == "parse_failed"
    assert job["error"]["url"] == hit["url"] and job["error"]["hint"]


def test_missing_adapter_reports_unsupported(run_job):
    job = run_job(None)
    assert job["status"] == "error"
    assert job["error"]["reason"] == "unsupported" and job["error"]["hint"]


def test_preset_uses_selected_hit_and_preserves_path_metadata(run_job, tmp_path):
    artifact = tmp_path / "manual.pdf"
    artifact.write_bytes(b"%PDF-1.7\n")
    hit = {"ok": True, "title": "Manual", "url": "https://example.com/manual"}
    adapter = SimpleNamespace(
        search=lambda query: pytest.fail("preset must skip search"),
        acquire=lambda item: {"ok": True, "artifact_path": str(artifact),
                              "artifact_kind": "document", "format": "pdf"})
    job = run_job(adapter, preset=hit)
    assert job["status"] == "done" and job["search_hits"] == [hit]
    assert job["spec_data"]["artifact_kind"] == "document"
    assert job["spec_data"]["format"] == "pdf"


@pytest.mark.parametrize(("filename", "content", "kind", "format_name"), [
    ("model.step", b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n"
                   b"END-ISO-10303-21;\n", "cad", "step"),
    ("datasheet.pdf", b"%PDF-1.7\n", "document", "pdf"),
])
def test_legacy_artifact_gets_content_based_classification(
        run_job, tmp_path, filename, content, kind, format_name):
    artifact = tmp_path / filename
    artifact.write_bytes(content)
    hit = {"url": "https://example.com/item"}
    adapter = SimpleNamespace(search=lambda query: [hit],
                              acquire=lambda item: {"ok": True,
                                                    "artifact_path": str(artifact)})
    job = run_job(adapter)
    assert job["status"] == "done"
    assert job["spec_data"]["artifact_kind"] == kind
    assert job["spec_data"]["format"] == format_name


@pytest.mark.parametrize("declared", [False, True])
def test_cad_archive_preserves_validated_member_and_supplied_format(run_job, tmp_path, declared):
    artifact = tmp_path / "model.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("nested/model.step",
                         b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n"
                         b"END-ISO-10303-21;\n")
    result = {"ok": True, "artifact_path": str(artifact)}
    if declared:
        result.update(artifact_kind="cad", format="zip")
    hit = {"url": "https://example.com/item"}
    adapter = SimpleNamespace(search=lambda query: [hit], acquire=lambda item: result)
    job = run_job(adapter)
    assert job["status"] == "done"
    assert job["spec_data"]["artifact_kind"] == "cad"
    assert job["spec_data"]["format"] == ("zip" if declared else "step")
    assert job["spec_data"]["member"] == "nested/model.step"
    assert job["spec_data"]["cad_validated"] is True


@pytest.mark.parametrize(("selected", "succeeds"), [
    ("selected.step", True), ("invalid.step", False), ("missing.step", False),
])
def test_cad_archive_validates_exact_selected_member(run_job, tmp_path, selected, succeeds):
    artifact = tmp_path / "models.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name in ("first.step", "selected.step"):
            archive.writestr(name, b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
        archive.writestr("invalid.step", b"<html>not a model</html>")
    result = {"ok": True, "artifact_path": str(artifact), "artifact_kind": "cad",
              "format": "zip", "member": selected}
    adapter = SimpleNamespace(search=lambda query: [{"url": "https://example.com/item"}],
                              acquire=lambda item: result)
    job = run_job(adapter)
    if succeeds:
        assert job["status"] == "done"
        assert job["spec_data"]["member"] == selected
        assert job["spec_data"]["cad_validated"] is True
    else:
        assert job["status"] == "error" and job["artifact_path"] == ""
        assert job["error"]["reason"] == "parse_failed"
        assert job["error"]["validation_reason"]


def test_cad_archive_selects_declared_format(run_job, tmp_path):
    artifact = tmp_path / "models.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("first.step", b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
        archive.writestr("selected.igs", b" " * 72 + b"S      1\n")
    result = {"ok": True, "artifact_path": str(artifact),
              "artifact_kind": "cad", "format": "iges"}
    adapter = SimpleNamespace(search=lambda query: [{"url": "https://example.com/item"}],
                              acquire=lambda item: result)
    job = run_job(adapter)
    assert job["status"] == "done"
    assert job["spec_data"]["format"] == "iges"
    assert job["spec_data"]["member"] == "selected.igs"
    assert job["spec_data"]["cad_validated"] is True


def test_ambiguous_cad_archive_reports_member_choices(run_job, tmp_path):
    artifact = tmp_path / "models.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name in ("one.step", "two.step"):
            archive.writestr(name, b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
    result = {"ok": True, "artifact_path": str(artifact), "ask_user": None,
              "artifact_kind": "cad", "format": "zip"}
    hit = {"url": "https://example.com/item", "sku": "PART-1"}
    adapter = SimpleNamespace(search=lambda query: [hit],
                              acquire=lambda item: result)
    job = run_job(adapter)
    assert job["status"] == "error" and job["artifact_path"] == ""
    assert job["error"]["reason"] == "unsupported"
    assert set(job["error"]["candidates"]) == {"one.step", "two.step"}
    assert job["error"]["artifact_path"] == str(artifact) and job["error"]["hit"] == hit
    question = job["error"]["ask_user"]
    assert question["kind"] == "choice" and question["suggested"] == "cancel"
    assert {option["value"] for option in question["options"]} == {
        "cancel", "one.step", "two.step"}


def test_untyped_ambiguous_archive_is_not_misrepresented_as_one_model(run_job, tmp_path):
    artifact = tmp_path / "models.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name in ("one.step", "two.step"):
            archive.writestr(name, b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
    adapter = SimpleNamespace(
        search=lambda query: [{"url": "https://example.com/item"}],
        acquire=lambda item: {"ok": True, "artifact_path": str(artifact)})
    job = run_job(adapter)
    assert job["status"] == "error" and job["artifact_path"] == ""
    assert job["error"]["reason"] == "unsupported"
    assert set(job["error"]["candidates"]) == {"one.step", "two.step"}
    assert job["error"]["artifact_path"] == str(artifact)
    assert job["error"]["ask_user"]["kind"] == "choice"


def test_explicit_archive_does_not_require_model_selection(run_job, tmp_path):
    artifact = tmp_path / "models.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name in ("one.step", "two.step"):
            archive.writestr(name, b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
    adapter = SimpleNamespace(
        search=lambda query: [{"url": "https://example.com/item"}],
        acquire=lambda item: {"ok": True, "artifact_path": str(artifact),
                              "artifact_kind": "archive", "format": "zip"})
    job = run_job(adapter)
    assert job["status"] == "done" and job["artifact_path"] == str(artifact)
    assert job["spec_data"]["artifact_kind"] == "archive"
    assert job["spec_data"]["cad_validated"] is False and job["error"] is None


def test_hit_selected_archive_member_is_not_replaced(run_job, tmp_path):
    artifact = tmp_path / "models.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name in ("one.step", "two.step"):
            archive.writestr(name, b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
    hit = {"url": "https://example.com/item", "archive_member": "two.step"}
    result = {"ok": True, "artifact_path": str(artifact),
              "artifact_kind": "cad", "format": "zip"}
    adapter = SimpleNamespace(search=lambda query: [hit], acquire=lambda item: result)
    job = run_job(adapter)
    assert job["status"] == "done"
    assert job["spec_data"]["member"] == "two.step"


def test_extracted_cad_member_remains_provenance(run_job, tmp_path):
    artifact = tmp_path / "extracted.step"
    artifact.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
    result = {"ok": True, "artifact_path": str(artifact), "artifact_kind": "cad",
              "format": "step", "archive_path": str(tmp_path / "models.zip"),
              "member": "nested/selected.step"}
    adapter = SimpleNamespace(search=lambda query: [{"url": "https://example.com/item"}],
                              acquire=lambda item: result)
    job = run_job(adapter)
    assert job["status"] == "done"
    assert job["spec_data"]["member"] == "nested/selected.step"
    assert job["spec_data"]["cad_validated"] is True


def test_extracted_cad_cannot_replace_explicit_member_selection(run_job, tmp_path):
    artifact = tmp_path / "extracted.step"
    artifact.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
    hit = {"url": "https://example.com/item", "archive_member": "wanted.step"}
    result = {"ok": True, "artifact_path": str(artifact), "artifact_kind": "cad",
              "format": "step", "archive_path": str(tmp_path / "models.zip"),
              "member": "different.step"}
    adapter = SimpleNamespace(search=lambda query: [hit], acquire=lambda item: result)
    job = run_job(adapter)
    assert job["status"] == "error" and job["artifact_path"] == ""
    assert job["error"]["reason"] == "parse_failed"
    assert "selected member" in job["error"]["hint"]


def test_declared_native_cad_is_downloadable_but_not_validated(run_job, tmp_path):
    artifact = tmp_path / "part.SLDPRT"
    # An OLE signature identifies a container, not a SolidWorks model or geometry.
    artifact.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"container data")
    hit = {"url": "https://example.com/item"}
    adapter = SimpleNamespace(
        search=lambda query: [hit],
        acquire=lambda item: {"ok": True, "artifact_path": str(artifact),
                              "artifact_kind": "cad", "format": "sldprt",
                              "cad_validated": True})
    job = run_job(adapter)
    assert job["status"] == "done" and job["artifact_path"] == str(artifact)
    assert job["spec_data"]["artifact_kind"] == "cad"
    assert job["spec_data"]["format"] == "sldprt"
    assert job["spec_data"]["cad_validated"] is False


@pytest.mark.parametrize(("content", "declared_format"), [
    (b"<html>Sign in</html>", "sldprt"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1container data", None),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1container data", "step"),
])
def test_unidentified_or_mismatched_native_cad_is_not_done(
        run_job, tmp_path, content, declared_format):
    artifact = tmp_path / "part.sldprt"
    artifact.write_bytes(content)
    result = {"ok": True, "artifact_path": str(artifact), "artifact_kind": "cad"}
    if declared_format is not None:
        result["format"] = declared_format
    adapter = SimpleNamespace(search=lambda query: [{"url": "https://example.com/item"}],
                              acquire=lambda item: result)
    job = run_job(adapter)
    assert job["status"] == "error" and job["artifact_path"] == ""
    assert job["error"]["reason"] in ("parse_failed", "unsupported")
    assert job["error"]["hint"]


@pytest.mark.parametrize(("content", "kind", "format_name"), [
    (b"<html>Sign in</html>", "cad", "step"),
    (b"%PDF-1.7\n", "cad", "step"),
    (b"ISO-10303-21;\nHEADER;\n", "cad", "stl"),
])
def test_invalid_or_misidentified_cad_is_not_done(
        run_job, tmp_path, content, kind, format_name):
    artifact = tmp_path / "model.step"
    artifact.write_bytes(content)
    hit = {"url": "https://example.com/item"}
    adapter = SimpleNamespace(
        search=lambda query: [hit],
        acquire=lambda item: {"ok": True, "artifact_path": str(artifact),
                              "artifact_kind": kind, "format": format_name})
    job = run_job(adapter)
    assert job["status"] == "error"
    assert job["artifact_path"] == ""
    assert job["error"]["reason"] == "parse_failed"
    assert job["error"]["hint"]
    assert job["error"]["url"] == hit["url"]


@pytest.mark.parametrize("member", ["../escape.step", "only.txt"])
def test_unsafe_or_non_cad_archive_is_not_done(run_job, tmp_path, member):
    artifact = tmp_path / "model.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(member, b"ISO-10303-21;\nHEADER;\n")
    hit = {"url": "https://example.com/item"}
    adapter = SimpleNamespace(
        search=lambda query: [hit],
        acquire=lambda item: {"ok": True, "artifact_path": str(artifact),
                              "artifact_kind": "cad", "format": "zip"})
    job = run_job(adapter)
    assert job["status"] == "error" and job["artifact_path"] == ""
    assert job["error"]["reason"] == "parse_failed"
    assert job["error"]["validation_reason"]


def test_unknown_legacy_artifact_is_not_misrepresented(run_job, tmp_path):
    artifact = tmp_path / "unknown.bin"
    artifact.write_bytes(b"unrecognized bytes")
    adapter = SimpleNamespace(
        search=lambda query: [{"url": "https://example.com/item"}],
        acquire=lambda item: {"ok": True, "artifact_path": str(artifact)})
    job = run_job(adapter)
    assert job["status"] == "error"
    assert job["error"]["reason"] == "parse_failed"
    assert job["error"]["validation_reason"]


@pytest.mark.parametrize("raises", [False, True])
def test_worker_failure_paths_publish_structured_errors(monkeypatch, raises):
    class StopWorker(BaseException):
        pass

    class StopWhenIdle(threading.Condition):
        def wait(self, timeout=None):
            raise StopWorker()

    monkeypatch.setattr(queue, "_jobs", [])
    monkeypatch.setattr(queue, "_cond", StopWhenIdle(queue._lock))

    def execute(job):
        if raises:
            raise RuntimeError("unexpected worker failure")

    monkeypatch.setattr(queue, "_run_job", execute)
    queued = queue.enqueue("example", "bearing")
    with pytest.raises(StopWorker):
        queue._worker_loop(0)
    job = queue.queue_status()["jobs"][0]
    assert job["id"] == queued["id"] and job["status"] == "error"
    assert job["error"]["reason"] == "internal_error"
    assert job["error"]["site"] == "example" and job["error"]["op"] == "queue"
    assert job["error"]["hint"] and job["finished_ts"] is not None


def test_required_search_choice_preserves_hits_without_acquiring(run_job):
    url = "https://example.com/search"
    candidates = ["one.step", "two.step"]
    notice = {"ok": False, "reason": "unsupported", "url": url, "candidates": candidates,
              "ask_user": adapters.model_selection_question("example", url, candidates)}
    hit = {"url": "https://example.com/item"}
    adapter = SimpleNamespace(search=lambda query: [notice, hit],
                              acquire=lambda item: pytest.fail("choice must come first"))
    job = run_job(adapter)
    assert job["status"] == "error"
    assert job["error"]["ask_user"] == notice["ask_user"]
    assert job["can_select_member"] is False
    assert job["search_hits"][1] == hit


def test_selected_zip_member_requeues_exact_hit_and_pauses_other_results(run_job, tmp_path):
    artifact = tmp_path / "models.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name in ("one.step", "two.step"):
            archive.writestr(name, b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
    first = {"url": "https://example.com/first", "sku": "PART-1", "format": "zip"}
    second = {"url": "https://example.com/second"}
    calls = []

    def acquire(hit):
        calls.append(hit["url"])
        assert hit["url"] == first["url"], "must not silently choose another search hit"
        result = {"ok": True, "artifact_path": str(artifact),
                  "artifact_kind": "cad", "format": "zip"}
        if hit.get("archive_member"):
            result["member"] = hit["archive_member"]
        return result

    adapter = SimpleNamespace(search=lambda query: [first, second], acquire=acquire)
    job = run_job(adapter)
    assert job["status"] == "error" and calls == [first["url"]]
    assert job["error"]["ask_user"]["kind"] == "choice"
    assert job["can_select_member"] is True
    assert queue.select_member(job["id"], "not-offered.step")["reason"] == "unsupported"
    retry = queue.select_member(job["id"], "two.step", "step")
    assert retry["ok"] and retry["status"] == "pending"
    selected = queue._claim_next_job()
    assert selected.id == retry["id"]
    assert selected.preset["sku"] == "PART-1"
    assert selected.preset["archive_member"] == "two.step" and "member" not in selected.preset
    assert selected.preset["format"] == "step"
    queue._run_job(selected)
    completed = queue.queue_status()["jobs"][0]
    assert completed["status"] == "done"
    assert completed["spec_data"]["member"] == "two.step"
    assert completed["spec_data"]["cad_validated"] is True


def test_adapter_choice_stops_before_trying_another_result(run_job):
    first = {"url": "https://example.com/first", "sku": "PART-1"}
    second = {"url": "https://example.com/second"}
    candidates = [{"id": "file-1"}, {"id": "file-2"}]
    question = adapters.model_selection_question("example", first["url"], ["file-1", "file-2"])
    calls = []

    def acquire(hit):
        calls.append(hit)
        return {"ok": False, "reason": "unsupported", "hint": "Choose a model",
                "selection_field": "file_id", "candidates": candidates, "ask_user": question}

    job = run_job(SimpleNamespace(search=lambda query: [first, second], acquire=acquire))
    assert job["status"] == "error" and calls == [first]
    assert job["error"]["hit"] == first and job["error"]["ask_user"] == question
    assert job["search_hits"] == [first, second]
    assert job["can_select_member"] is True
    assert queue.select_member(job["id"], "cancel")["reason"] == "unsupported"
    retry = queue.select_member(job["id"], "file-2")
    assert retry["ok"] and queue._jobs[-1].preset["file_id"] == "file-2"
    assert "member" not in queue._jobs[-1].preset
    assert "archive_member" not in queue._jobs[-1].preset


@pytest.mark.parametrize("field", [
    "archive_member", "member", "filename", "file_name", "url", "download_url",
    "file_id", "profile_id", "instance_id",
])
def test_selection_uses_question_option_and_only_its_declared_field(run_job, field):
    hit = {"url": "https://example.com/item", "sku": "PART-1",
           "filename": "previous.step", "file_id": "previous-id"}
    chosen = ("https://example.com/selected" if field in ("url", "download_url")
              else "chosen-42")
    question = adapters.model_selection_question("example", hit["url"], [chosen])
    result = {"ok": False, "reason": "unsupported", "hint": "Choose",
              "selection_field": field, "candidates": [{"id": chosen}],
              "ask_user": question}
    job = run_job(SimpleNamespace(search=lambda query: [hit], acquire=lambda item: result))
    assert job["can_select_member"] is True
    retry = queue.select_member(job["id"], chosen)
    assert retry["ok"]
    expected = dict(hit)
    expected[field] = chosen
    assert queue._jobs[-1].preset == expected


def test_printables_name_choice_retries_real_adapter(run_job, monkeypatch, tmp_path):
    from services.browser.adapters import printables

    page = "https://www.printables.com/model/42-servo"
    model = {"name": "Servo", "license": {"name": "CC BY"},
             "stls": [{"id": "7", "name": "arm.step"}],
             "otherFiles": [{"id": "7", "name": "horn.step"}]}
    monkeypatch.setattr(printables, "_query", lambda *_: {"data": {"print": model}})
    monkeypatch.setattr(printables, "_paced", lambda _op, _url, request: (request(), None))
    downloads = []
    monkeypatch.setattr(printables, "_download_link", lambda *args: downloads.append(args) or {
        "data": {"getDownloadLink": {"ok": True,
                 "output": {"link": "https://files.printables.com/download/model"}}}})
    monkeypatch.setattr(printables, "download_path",
                        lambda _site, _url, filename: (tmp_path / filename, None))

    def download(_url, dest, **_kwargs):
        dest.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n"
                         b"END-ISO-10303-21;\n")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(printables, "download_public_cad", download)
    adapter = SimpleNamespace(search=lambda query: [{"url": page}],
                              acquire=printables.ADAPTER.acquire)
    job = run_job(adapter)
    assert job["status"] == "error" and job["can_select_member"] is True
    assert job["error"]["selection_field"] == "file_name"
    retry = queue.select_member(job["id"], "horn.step")
    assert retry["ok"]
    selected = queue._claim_next_job()
    assert selected.preset["file_name"] == "horn.step"
    queue._run_job(selected)
    completed = queue.queue_status()["jobs"][0]
    assert completed["status"] == "done"
    assert completed["spec_data"]["file"]["name"] == "horn.step"
    assert completed["spec_data"]["cad_validated"] is True
    assert downloads == [("42", "7", "other")]


def test_selection_with_unrecognized_field_cannot_be_submitted(run_job):
    hit = {"url": "https://example.com/item"}
    question = adapters.model_selection_question("example", hit["url"], ["option"])
    result = {"ok": False, "reason": "unsupported", "hint": "Choose",
              "selection_field": "site", "candidates": ["option"], "ask_user": question}
    job = run_job(SimpleNamespace(search=lambda query: [hit], acquire=lambda item: result))
    assert job["status"] == "error" and job["can_select_member"] is False
    assert queue.select_member(job["id"], "option")["reason"] == "unsupported"


def test_duplicate_option_values_are_not_retryable(run_job):
    hit = {"url": "https://example.com/item"}
    question = adapters.model_selection_question("example", hit["url"], ["same", "same"])
    result = {"ok": False, "reason": "unsupported", "hint": "Choose",
              "selection_field": "file_name", "ask_user": question}
    job = run_job(SimpleNamespace(search=lambda query: [hit], acquire=lambda item: result))
    assert job["status"] == "error" and job["can_select_member"] is False
    assert queue.select_member(job["id"], "same")["reason"] == "unsupported"


def _principal(actor):
    return {"cell": True, "workspace_key": "team:queue", "actor_key": actor,
            "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
            "right": "editor", "epoch": 1}


def test_queue_status_dedup_and_cancel_are_scoped_to_owner(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CELL", raising=False)
    monkeypatch.delenv("CHUDWARE_ACTOR_SANDBOX", raising=False)
    monkeypatch.setattr(queue, "_jobs", [])
    first_hit = {"url": "https://example.com/item", "member": "one.step"}
    second_hit = {"url": "https://example.com/item", "member": "two.step"}
    with request_context.bind_context(_principal("alice")):
        first = queue.enqueue("example", "bearing", preset=first_hit)
        duplicate = queue.enqueue("example", "bearing", preset=first_hit)
        second = queue.enqueue("example", "bearing", preset=second_hit)
        assert duplicate["id"] == first["id"] and duplicate["deduplicated"]
        assert second["id"] != first["id"]
    with request_context.bind_context(_principal("bob")):
        assert queue.queue_status()["jobs"] == []
        assert queue.cancel_job(first["id"])["reason"] == "not_found"
        bob = queue.enqueue("example", "bearing", preset=first_hit)
        assert bob["id"] != first["id"]
        assert [job["id"] for job in queue.queue_status()["jobs"]] == [bob["id"]]
    with request_context.bind_context(_principal("alice")):
        assert {job["id"] for job in queue.queue_status()["jobs"]} == {
            first["id"], second["id"]}


def test_select_member_requires_own_job_and_offered_candidate(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CELL", raising=False)
    monkeypatch.delenv("CHUDWARE_ACTOR_SANDBOX", raising=False)
    monkeypatch.setattr(queue, "_jobs", [])
    hit = {"url": "https://example.com/item", "sku": "PART-1", "format": "zip"}
    candidates = ["one.step", "two.step"]
    with request_context.bind_context(_principal("alice")):
        queued = queue.enqueue("example", "bearing", preset=hit)
        job = queue._claim_next_job()
        job.error = {"ok": False, "reason": "unsupported", "url": hit["url"],
                     "candidates": candidates, "selection_field": "archive_member",
                     "ask_user": adapters.model_selection_question("example", hit["url"], candidates)}
        job.status = queue.JobStatus.ERROR
        assert queue.queue_status()["jobs"][0]["can_select_member"] is True
    with request_context.bind_context(_principal("bob")):
        assert queue.select_member(queued["id"], "one.step")["reason"] == "not_found"
    with request_context.bind_context(_principal("alice")):
        assert queue.select_member(queued["id"], "other.step")["reason"] == "unsupported"
        retry = queue.select_member(queued["id"], "one.step")
        assert retry["ok"]
        selected = queue._jobs[-1].preset
        assert selected["sku"] == "PART-1" and selected["archive_member"] == "one.step"
        assert "member" not in selected and "format" not in selected
