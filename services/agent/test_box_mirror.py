"""Box mirror (services/plm/box_mirror.py) — offline contract tests.

No network: every test monkeypatches ``box_mirror._http`` with an in-memory
fake of the three Box surfaces the mirror touches (folders, uploads, oauth
token). Config/appdata land in tmp_path via the same ``services.paths``
monkeypatch pattern test_effort.py uses.
"""
from __future__ import annotations

import json
import time

import pytest

from services import paths as engine_paths
from services.plm import box_mirror


# --------------------------------------------------------------------------- fake Box

class FakeBox:
    """In-memory Box: folder tree, file versions, oauth refresh, /users/me."""

    def __init__(self, token: str = "tok-dev") -> None:
        self.token = token
        self.next_id = 100
        self.folders: dict[str, dict[str, str]] = {"0": {}}  # id -> {child name: id}
        self.folder_parent: dict[str, str] = {}
        self.files: dict[str, dict] = {}  # id -> {name, parent, versions: [bytes]}
        self.calls: list[str] = []
        self.refreshed = 0

    def _mint(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def _find_file(self, parent: str, name: str) -> str | None:
        for fid, rec in self.files.items():
            if rec["parent"] == parent and rec["name"] == name:
                return fid
        return None

    @staticmethod
    def _parse_multipart(data: bytes) -> tuple[dict, bytes]:
        parts = data.split(b"\r\n\r\n")
        attrs = json.loads(parts[1].split(b"\r\n--")[0])
        payload = parts[2].rsplit(b"\r\n--", 1)[0]
        return attrs, payload

    def __call__(self, method: str, url: str, *, token: str | None = None,
                 json_body: dict | None = None, form: dict | None = None,
                 data: bytes | None = None, content_type: str | None = None,
                 timeout: float = 0) -> tuple[int, object]:
        self.calls.append(f"{method} {url}")
        if url == box_mirror._TOKEN_URL:
            assert form is not None
            self.refreshed += 1
            return 200, {"access_token": f"tok-oauth-{self.refreshed}",
                         "refresh_token": f"refresh-{self.refreshed}",
                         "expires_in": 3600}
        if token != self.token:
            return 401, {"code": "unauthorized"}
        if url.endswith("/users/me"):
            return 200, {"id": "42", "name": "Test User", "login": "test@example.com"}
        if url == f"{box_mirror._API}/folders":
            assert json_body is not None
            parent = str(json_body["parent"]["id"])
            name = json_body["name"]
            existing = self.folders.get(parent, {}).get(name)
            if existing:
                return 409, {"context_info": {"conflicts": [{"id": existing}]}}
            fid = self._mint()
            self.folders.setdefault(parent, {})[name] = fid
            self.folders[fid] = {}
            self.folder_parent[fid] = parent
            return 201, {"id": fid}
        if url == f"{box_mirror._UPLOAD}/files/content":
            attrs, payload = self._parse_multipart(data or b"")
            parent = str(attrs["parent"]["id"])
            name = attrs["name"]
            existing = self._find_file(parent, name)
            if existing:
                return 409, {"context_info": {"conflicts": {"id": existing}}}
            fid = self._mint()
            self.files[fid] = {"name": name, "parent": parent, "versions": [payload]}
            return 201, {"entries": [{"id": fid}]}
        if url.startswith(f"{box_mirror._UPLOAD}/files/") and url.endswith("/content"):
            fid = url.rsplit("/", 2)[-2]
            if fid not in self.files:
                return 404, {"code": "not_found"}
            _attrs, payload = self._parse_multipart(data or b"")
            self.files[fid]["versions"].append(payload)
            return 200, {"entries": [{"id": fid}]}
        raise AssertionError(f"unexpected request: {method} {url}")


# --------------------------------------------------------------------------- fixtures

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """tmp config + appdata root + fake transport; project with mixed content."""
    monkeypatch.setattr(engine_paths, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(engine_paths, "ROOT", tmp_path / "appdata")
    fake = FakeBox()
    monkeypatch.setattr(box_mirror, "_http", fake)
    project = tmp_path / "RoverProject"
    (project / "sub").mkdir(parents=True)
    (project / ".aicadvault").mkdir()
    (project / "wheel.step+").write_bytes(b"solid wheel")
    (project / "sub" / "axle.step+").write_bytes(b"solid axle")
    (project / ".hidden.txt").write_bytes(b"nope")
    (project / ".aicadvault" / "blob").write_bytes(b"vault-internal")
    return fake, project, tmp_path


def _write_config(tmp_path, box: dict) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"box": box}), encoding="utf-8")


def _enabled_cfg(**auth) -> dict:
    return {"enabled": True, "folder_id": "0",
            "auth": {"method": "developer_token", "developer_token": "tok-dev", **auth}}


# --------------------------------------------------------------------------- config

def test_config_defaults_when_absent(env):
    _fake, _project, tmp = env
    cfg = box_mirror.get_box_config()
    assert cfg["enabled"] is False
    assert cfg["folder_id"] == "0"
    assert cfg["auth"]["method"] == "developer_token"
    assert cfg["max_file_mb"] == 512
    assert box_mirror.is_enabled() is False


def test_config_clamps_and_rejects_garbage(env):
    _fake, _project, tmp = env
    _write_config(tmp, {"enabled": 1, "folder_id": 7, "max_file_mb": "lots",
                        "auth": {"method": "carrier_pigeon"}})
    cfg = box_mirror.get_box_config()
    assert cfg["enabled"] is True
    assert cfg["folder_id"] == "0"          # non-string folder id → default
    assert cfg["max_file_mb"] == 512        # non-int cap → default
    assert cfg["auth"]["method"] == "developer_token"  # unknown method → default


# --------------------------------------------------------------------------- hooks / spool

def test_disabled_hooks_are_noops(env):
    fake, project, _tmp = env
    box_mirror.notify_saved_path(str(project / "wheel.step+"))
    box_mirror.on_vault_saved(str(project))
    assert list(box_mirror._spool_dir().glob("*.json")) == []
    assert fake.calls == []


def test_enqueue_coalesces_per_project(env):
    _fake, project, tmp = env
    _write_config(tmp, _enabled_cfg())
    assert box_mirror.enqueue_project(str(project))
    assert box_mirror.enqueue_project(str(project))
    assert len(list(box_mirror._spool_dir().glob("*.json"))) == 1
    assert box_mirror.enqueue_project(str(project / "does-not-exist")) is None


# --------------------------------------------------------------------------- mirroring

def test_mirror_uploads_visible_tree_and_consumes_spool(env):
    fake, project, tmp = env
    _write_config(tmp, _enabled_cfg())
    out = box_mirror.sync_project(str(project))
    assert out["ok"] is True
    summary = out["projects"][0]
    assert summary["uploaded"] == 2 and summary["updated"] == 0
    # dot files/dirs never leave the machine
    names = {rec["name"] for rec in fake.files.values()}
    assert names == {"wheel.step+", "axle.step+"}
    # folder layout: <root>/RoverProject/sub
    proj_id = fake.folders["0"]["RoverProject"]
    assert "sub" in fake.folders[proj_id]
    # spool consumed
    assert list(box_mirror._spool_dir().glob("*.json")) == []


def test_second_sync_skips_unchanged_and_versions_changed(env):
    fake, project, tmp = env
    _write_config(tmp, _enabled_cfg())
    box_mirror.sync_project(str(project))
    out = box_mirror.sync_project(str(project))
    assert out["projects"][0] == {"project": str(project.resolve()), "uploaded": 0,
                                  "updated": 0, "unchanged": 2, "skipped_large": 0}
    (project / "wheel.step+").write_bytes(b"solid wheel rev-b")
    out = box_mirror.sync_project(str(project))
    assert out["projects"][0]["updated"] == 1 and out["projects"][0]["unchanged"] == 1
    wheel = next(r for r in fake.files.values() if r["name"] == "wheel.step+")
    assert wheel["versions"][-1] == b"solid wheel rev-b"


def test_name_conflict_adopts_existing_box_file(env):
    fake, project, tmp = env
    _write_config(tmp, _enabled_cfg())
    # someone already uploaded wheel.step+ into the (pre-created) project folder
    fake.folders["0"]["RoverProject"] = "900"
    fake.folders["900"] = {}
    fake.files["901"] = {"name": "wheel.step+", "parent": "900", "versions": [b"old"]}
    out = box_mirror.sync_project(str(project))
    assert out["ok"] is True
    assert fake.files["901"]["versions"][-1] == b"solid wheel"  # new version, same id
    st = box_mirror._load_state(str(project))
    assert st["files"]["wheel.step+"]["id"] == "901"


def test_oversize_files_are_skipped_not_fatal(env):
    fake, project, tmp = env
    cfg = _enabled_cfg()
    cfg["max_file_mb"] = 1
    _write_config(tmp, cfg)
    (project / "huge.step+").write_bytes(b"x" * (2 * 1024 * 1024))
    out = box_mirror.sync_project(str(project))
    assert out["ok"] is True
    assert out["projects"][0]["skipped_large"] == 1
    assert all(r["name"] != "huge.step+" for r in fake.files.values())


def test_zero_budget_leaves_spool_intact(env):
    _fake, project, tmp = env
    _write_config(tmp, _enabled_cfg())
    box_mirror.enqueue_project(str(project))
    out = box_mirror.drain(block=True, budget_s=0.0)
    assert out["ok"] is False and any("budget" in e for e in out["errors"])
    assert len(list(box_mirror._spool_dir().glob("*.json"))) == 1


def test_auth_failure_keeps_spool_and_notes_error(env):
    fake, project, tmp = env
    _write_config(tmp, _enabled_cfg(developer_token="wrong-token"))
    out = box_mirror.sync_project(str(project))
    assert out["ok"] is False
    assert any("auth_failed" in e for e in out["errors"])
    assert len(list(box_mirror._spool_dir().glob("*.json"))) == 1
    st = box_mirror.status(str(project))
    assert "auth_failed" in st["project"]["last_error"]


def test_disabled_sync_refuses_with_reason(env):
    _fake, project, tmp = env
    _write_config(tmp, {"enabled": False})
    out = box_mirror.sync_project(str(project))
    assert out == {"ok": False, "reason": "disabled",
                   "hint": out["hint"]} and "enable" in out["hint"]


# --------------------------------------------------------------------------- oauth

def test_oauth_refresh_rotates_and_persists_tokens(env):
    fake, _project, tmp = env
    _write_config(tmp, {"enabled": True, "auth": {
        "method": "oauth", "client_id": "cid", "client_secret": "sec",
        "access_token": "tok-stale", "refresh_token": "refresh-0",
        "access_token_expires_at": time.time() - 10}})
    tok = box_mirror._access_token()
    assert tok == "tok-oauth-1"
    on_disk = json.loads((tmp / "config.json").read_text())["box"]["auth"]
    assert on_disk["access_token"] == "tok-oauth-1"
    assert on_disk["refresh_token"] == "refresh-1"       # Box rotates: MUST persist
    assert on_disk["access_token_expires_at"] > time.time()
    # fresh token short-circuits: no second refresh
    assert box_mirror._access_token() == "tok-oauth-1"
    assert fake.refreshed == 1


def test_oauth_without_grant_reports_not_connected(env):
    _fake, _project, tmp = env
    _write_config(tmp, {"enabled": True, "auth": {
        "method": "oauth", "client_id": "cid", "client_secret": "sec"}})
    with pytest.raises(box_mirror.BoxError) as ei:
        box_mirror._access_token()
    assert ei.value.reason == "not_connected"


# --------------------------------------------------------------------------- status / registry

def test_status_live_reports_account(env):
    fake, project, tmp = env
    _write_config(tmp, _enabled_cfg())
    out = box_mirror.status(str(project), live=True)
    assert out["connected"] is True
    assert out["account"]["login"] == "test@example.com"
    assert out["pending"] == 0


def test_risk_classification():
    from services.mcp.tool_registry import RISK_EXTERNAL, RISK_READ, classify_risk

    assert classify_risk("box_sync") == RISK_EXTERNAL  # network egress, never plain write
    assert classify_risk("box_status") == RISK_READ


def test_box_tools_in_plm_category_pack():
    from services.agent.tool_categories import CATEGORY_PACKS

    assert "box_sync" in CATEGORY_PACKS["plm"]
    assert "box_status" in CATEGORY_PACKS["plm"]
