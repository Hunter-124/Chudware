"""In-app Brush / OpenMVS installer: background run, progress, error reporting."""
from __future__ import annotations

import threading

import pytest

from services.vision import reveng_install as ri
from services.vision import reveng_provision as rp


@pytest.fixture(autouse=True)
def _reset_state():
    ri._state.update(running=False, tool="", message="", done=0, total=0, installed=[], errors={})
    yield


def _wait_idle():
    for thread in threading.enumerate():
        if thread.name == "reveng-install":
            thread.join(timeout=10)


def test_installs_only_what_is_missing_and_reports_progress(monkeypatch):
    present = {"brush": True, "openmvs": False}
    monkeypatch.setattr(rp, "_artifact_available", lambda name: present[name])
    ran = []
    started, release = threading.Event(), threading.Event()

    def fake_openmvs(progress=None):
        ran.append("openmvs")
        progress("Downloading OpenMVS", 5, 10)
        started.set()
        release.wait(timeout=10)
        present["openmvs"] = True

    monkeypatch.setitem(ri._INSTALLERS, "openmvs", fake_openmvs)
    monkeypatch.setitem(ri._INSTALLERS, "brush", lambda progress=None: ran.append("brush"))

    ri.start_install()
    assert started.wait(timeout=10)
    mid = ri.install_state()
    assert mid["running"] and mid["tool"] == "openmvs" and (mid["done"], mid["total"]) == (5, 10)
    assert ri.start_install()["running"]   # idempotent while in flight: no second thread
    release.set()
    _wait_idle()
    state = ri.install_state()
    assert ran == ["openmvs"]
    assert state["running"] is False and state["installed"] == ["openmvs"] and state["errors"] == {}


def test_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(rp, "_artifact_available", lambda name: False)

    def boom(progress=None):
        raise ValueError("SHA256 mismatch")

    monkeypatch.setitem(ri._INSTALLERS, "brush", boom)
    monkeypatch.setitem(ri._INSTALLERS, "openmvs", lambda progress=None: None)
    ri.start_install()
    _wait_idle()
    state = ri.install_state()
    assert state["errors"] == {"brush": "SHA256 mismatch"}
    assert state["installed"] == ["openmvs"]
    assert state["running"] is False


def test_noop_when_nothing_missing(monkeypatch):
    monkeypatch.setattr(rp, "_artifact_available", lambda name: True)
    assert ri.start_install()["running"] is False


def test_unknown_tool_rejected(monkeypatch):
    monkeypatch.setattr(rp, "_artifact_available", lambda name: False)
    with pytest.raises(ValueError):
        ri.start_install(["colmap"])


def test_status_carries_install_state(monkeypatch):
    ri._state["message"] = "Downloading OpenMVS"
    assert rp.reveng_status()["install"]["message"] == "Downloading OpenMVS"
