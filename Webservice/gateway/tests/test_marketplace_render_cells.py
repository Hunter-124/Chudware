"""Seller previews over hermetic Unix cell sockets and real PostgreSQL metadata."""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import struct
import tempfile
import threading
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import accounts
import cells
import marketplace_render
from config import settings
from db import tx


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


def png(red: int, green: int, blue: int, *, width: int = 1) -> bytes:
    """Make a complete, CRC-correct PNG without a native renderer or image library."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, 1, 8, 2, 0, 0, 0)
    pixels = b"\x00" + bytes((red, green, blue)) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


PNG_BY_METHOD = {
    "doc_render_png": png(255, 0, 0),
    "doc_render_contact_sheet": png(0, 0, 255),
}


class FakeDaemon:
    """Only fake native execution; cell admission, framing and disk reads stay real."""
    def __init__(self, path: Path, root: Path, behavior):
        self.path, self.root, self.behavior = path, root, behavior
        self.calls = []
        self.outputs = {}
        self.ready = threading.Event()
        self.loop = None
        self.failure = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def start(self):
        self.thread.start()
        assert self.ready.wait(5), "fake cell did not start"
        assert self.failure is None, self.failure

    def serve(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def boot():
            server = await asyncio.start_unix_server(self.client, path=str(self.path))
            self.path.chmod(0o600)
            return server

        try:
            server = self.loop.run_until_complete(boot())
        except BaseException as exc:
            self.failure = exc
            self.ready.set()
            self.loop.close()
            return
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            server.close()
            self.loop.run_until_complete(server.wait_closed())
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()

    async def client(self, reader, writer):
        try:
            while raw := await reader.readline():
                request = json.loads(raw)
                self.calls.append(request)
                method, params = request["method"], request["params"]
                if method == "doc_open":
                    source = Path(params["path"])
                    assert not source.is_absolute() and ".." not in source.parts
                    assert (self.root / source).read_bytes() == STEP
                    result = {"ok": True, "doc_id": "seller-doc"}
                elif method in PNG_BY_METHOD:
                    relative = Path(params["out_path"])
                    assert not relative.is_absolute() and ".." not in relative.parts
                    target = self.root / relative
                    if self.behavior.emit_outputs:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        data = self.behavior.images.get(method, PNG_BY_METHOD[method])
                        target.write_bytes(data)
                        self.outputs[str(relative)] = data
                    result = {"ok": True, "out_path": self.behavior.reply_path,
                              "path": self.behavior.reply_path,
                              "png_path": self.behavior.reply_path}
                else:
                    assert method == "hello", f"unexpected preview RPC: {method}"
                    result = {"ok": True, "proto": 1}
                if method == self.behavior.bad_method:
                    writer.write(self.behavior.bad_reply(request["id"]))
                    await writer.drain()
                    break  # EOF ensures malformed frames cannot leave a hanging test.
                writer.write(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")
                await writer.drain()
        except ConnectionError:
            pass
        except Exception as exc:
            self.failure = exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def stop(self):
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        assert not self.thread.is_alive(), "fake cell thread did not stop"
        assert self.failure is None, self.failure


@pytest.fixture
def marketplace_cells(marketplace_env, activate, monkeypatch, client):
    # The real gateway lifespan owns provider sockets even for CAD-only previews.
    # Preview jobs run on transient worker loops; the broker must stay on ASGI's loop.
    # pytest's deep tmp_path is too long for a cell_id plus AF_UNIX gw.sock.
    with tempfile.TemporaryDirectory(prefix="cw-mk-", dir="/tmp") as directory:
        monkeypatch.setattr(settings, "cells_root", Path(directory))
        monkeypatch.setattr(settings, "university_team_id", "")
        behavior = SimpleNamespace(bad_method=None, bad_reply=None, emit_outputs=True,
                                   reply_path="/work/not-the-requested-preview.png", images={})
        fakes, activations = {}, []

        async def start(cell_id):
            activations.append(cell_id)
            if cell_id not in fakes:
                cell_dir = settings.cells_root / cell_id
                config = json.loads((cell_dir / "cell.json").read_text())
                fake = FakeDaemon(cell_dir / "gw.sock", Path(config["root"]), behavior)
                fakes[cell_id] = fake
                fake.start()

        monkeypatch.setattr(cells, "start_cell", start)

        def listing(label="seller", *, source=True):
            token = uuid.uuid4().hex
            email = activate(f"{label}.{token}@test.invalid")
            listing_id, asset_id = str(uuid.uuid4()), str(uuid.uuid4())
            filename = "bracket.step"
            relative = f"uploads/{asset_id}/{filename}"
            root = settings.listing_asset_root(listing_id)
            root.mkdir(parents=True)
            with tx() as cur:
                cur.execute("INSERT INTO mk_sellers(email,handle,display_name) VALUES(%s,%s,'Cell Seller')",
                            (email, "seller-" + token))
                cur.execute("INSERT INTO mk_listings(id,slug,seller_email,title,license_id,origin)"
                            " VALUES(%s,%s,%s,'Cell Bracket','CC-BY-4.0','ai_assisted')",
                            (listing_id, "bracket-" + token, email))
                if source:
                    target = root / relative
                    target.parent.mkdir(parents=True)
                    target.write_bytes(STEP)
                    cur.execute("INSERT INTO mk_assets(id,listing_id,kind,role,filename,rel_path,format,"
                                "bytes,sha256,gated) VALUES(%s,%s,'source','primary',%s,%s,'step',%s,%s,TRUE)",
                                (asset_id, listing_id, filename, relative, len(STEP), hashlib.sha256(STEP).hexdigest()))
            return SimpleNamespace(email=email, id=listing_id, asset_id=asset_id, filename=filename,
                                   source_relative=relative, root=root)

        try:
            yield SimpleNamespace(listing=listing, fakes=fakes, activations=activations,
                                  behavior=behavior, root=marketplace_env.marketplace_root)
        finally:
            for fake in fakes.values():
                fake.stop()


def assets(listing_id, kind="render"):
    with tx() as cur:
        cur.execute("SELECT * FROM mk_assets WHERE listing_id=%s AND kind=%s ORDER BY role,id",
                    (listing_id, kind))
        return [dict(row) for row in cur.fetchall()]


def thumbnail_id(listing_id):
    with tx() as cur:
        cur.execute("SELECT thumbnail_asset_id FROM mk_listings WHERE id=%s", (listing_id,))
        value = cur.fetchone()["thumbnail_asset_id"]
        return str(value) if value else None


def assert_previews(listing, result, fake):
    assert result["ok"] is True, result
    assert result["listing_id"] == listing.id
    rows = assets(listing.id)
    assert len(rows) == 2
    assert set(result["asset_ids"]) == {str(row["id"]) for row in rows}
    assert {row["role"] for row in rows} == {"thumbnail", "contact_sheet"}
    assert result["thumbnail_asset_id"] == thumbnail_id(listing.id)
    assert result["thumbnail_asset_id"] in result["asset_ids"]
    for row in rows:
        data = (listing.root / row["rel_path"]).read_bytes()
        method = "doc_render_png" if row["role"] == "thumbnail" else "doc_render_contact_sheet"
        assert data == PNG_BY_METHOD[method]
        assert row["bytes"] == len(data)
        assert row["sha256"] == hashlib.sha256(data).hexdigest()
        assert row["content_type"] == "image/png" and row["format"] == "png"
        assert (row["width"], row["height"]) == (1, 1)
        assert row["gated"] is False
    principal = cells.resolve_workspace(listing.email)
    assert (settings.cells_root / principal.cell_id / "provider.sock").is_socket()
    with tx() as cur:
        cur.execute("SELECT count(*) AS n FROM provider_keys WHERE email=%s", (listing.email,))
        assert cur.fetchone()["n"] == 0
    hello = fake.calls[0]
    assert hello["method"] == "hello"
    identity = hello["params"]["principal"]
    assert identity["workspace_key"] == principal.workspace_key
    assert identity["actor_key"] == accounts.get_account(listing.email).uid
    assert identity["right"] == "viewer"
    assert identity["epoch"] == principal.epoch
    assert identity["connection_nonce"]
    assert [call["method"] for call in fake.calls] == [
        "hello", "doc_open", "doc_render_png", "doc_render_contact_sheet"]
    opened = fake.calls[1]["params"]
    assert opened["path"] == f".marketplace/{listing.id}/{listing.asset_id}/{listing.filename}"
    assert opened["detach_path"] is True
    for call in fake.calls[2:]:
        assert call["params"]["doc_id"] == "seller-doc"
        assert call["params"]["out_path"].startswith(f".marketplace/{listing.id}/renders/")
    assert fake.calls[-1]["params"]["views"] == ["iso", "front", "right", "top"]


def test_queue_publishes_real_pngs_from_seller_viewer_cell(marketplace_cells, client):
    env = marketplace_cells
    listing = env.listing()
    job = marketplace_render.queue_render(listing.id)
    assert isinstance(job, concurrent.futures.Future)
    result = job.result(timeout=10)
    seller = cells.resolve_workspace(listing.email)
    assert set(env.fakes) == {seller.cell_id}
    assert_previews(listing, result, env.fakes[seller.cell_id])
    # Stored generated assets are consumer-visible, without activating a visitor cell.
    with tx() as cur:
        cur.execute("UPDATE mk_listings SET status='published',published_at=now() WHERE id=%s", (listing.id,))
    for row in assets(listing.id):
        response = client.get(f"/api/marketplace/assets/{row['id']}")
        assert response.status_code == 200, response.text
        assert response.content == (listing.root / row["rel_path"]).read_bytes()
        assert response.headers["content-type"] == "image/png"
    assert set(env.activations) == {seller.cell_id}


def test_distinct_sellers_have_distinct_preview_cells(marketplace_cells):
    env = marketplace_cells
    a, b = env.listing("a"), env.listing("b")
    for listing in (a, b):
        result = marketplace_render.render_listing_previews(listing.id)
        principal = cells.resolve_workspace(listing.email)
        assert_previews(listing, result, env.fakes[principal.cell_id])
    ap, bp = cells.resolve_workspace(a.email), cells.resolve_workspace(b.email)
    assert ap.cell_id != bp.cell_id and ap.root != bp.root
    assert set(env.fakes) == {ap.cell_id, bp.cell_id}
    assert not (ap.root / ".marketplace" / b.id).exists()
    assert not (bp.root / ".marketplace" / a.id).exists()


def test_admin_requester_never_activates_own_cell(marketplace_cells, client, activate, as_user):
    env = marketplace_cells
    listing = env.listing()
    requester = activate(f"admin.{uuid.uuid4().hex}@test.invalid", role="admin")
    requester_principal = cells.resolve_workspace(requester)
    sentinel = requester_principal.root / "private.step"
    sentinel.write_bytes(b"requester-private")
    response = client.post(f"/api/marketplace/listings/{listing.id}/render", headers=as_user(requester))
    assert response.status_code == 200, response.text
    seller = cells.resolve_workspace(listing.email)
    assert_previews(listing, response.json(), env.fakes[seller.cell_id])
    assert set(env.activations) == {seller.cell_id}
    assert requester_principal.cell_id not in env.fakes
    assert sentinel.read_bytes() == b"requester-private"
    assert not (requester_principal.root / ".marketplace").exists()


@pytest.mark.parametrize("bad_reply", [
    pytest.param(lambda ident: b"not-json\n", id="malformed-json"),
    pytest.param(lambda ident: b"\xff\n", id="invalid-utf8"),
    pytest.param(lambda ident: f'{{"id":{ident},"result":{{}},"result":{{}}}}\n'.encode(), id="duplicate-keys"),
    pytest.param(lambda ident: f'{{"id":{ident},"result":{{"value":NaN}}}}\n'.encode(), id="nonfinite"),
    pytest.param(lambda ident: json.dumps({"id": ident + 99, "result": {"ok": True}}).encode() + b"\n", id="wrong-id"),
    pytest.param(lambda ident: json.dumps({"id": ident, "result": [],}).encode() + b"\n", id="nonobject-result"),
    pytest.param(lambda ident: json.dumps({"id": ident, "result": {"ok": False}}).encode() + b"\n", id="failed-result"),
])
def test_invalid_cell_reply_never_publishes_partial_previews(marketplace_cells, bad_reply):
    env = marketplace_cells
    listing = env.listing()
    env.behavior.bad_method = "doc_render_contact_sheet"
    env.behavior.bad_reply = bad_reply
    result = marketplace_render.render_listing_previews(listing.id)
    assert result["ok"] is False, result
    assert not result.get("asset_ids")
    assert assets(listing.id) == []
    assert thumbnail_id(listing.id) is None
    fake = next(iter(env.fakes.values()))
    assert any(call["method"] == "doc_render_png" for call in fake.calls)
    assert fake.outputs, "failure must occur after actual preview bytes were written"


def test_oversized_cell_reply_never_publishes_assets(marketplace_cells, monkeypatch):
    env = marketplace_cells
    listing = env.listing()
    monkeypatch.setattr(settings, "ws_max_message_bytes", 1024)
    env.behavior.bad_method = "doc_render_contact_sheet"
    env.behavior.bad_reply = lambda ident: json.dumps({"id": ident, "result": {"padding": "x" * 2048}}).encode() + b"\n"
    result = marketplace_render.render_listing_previews(listing.id)
    assert result["ok"] is False, result
    assert assets(listing.id) == []
    assert thumbnail_id(listing.id) is None


@pytest.mark.parametrize("component", ["file", "parent"])
def test_symlinked_source_is_refused_before_cell_activation(marketplace_cells, tmp_path, component):
    env = marketplace_cells
    listing = env.listing()
    source = listing.root / listing.source_relative
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / listing.filename).write_bytes(STEP)
    source.unlink()
    if component == "file":
        source.symlink_to(outside / listing.filename)
    else:
        source.parent.rmdir()
        source.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException) as error:
        marketplace_render.stage_listing_asset(listing.email, listing.id, listing.asset_id,
                                               listing.source_relative, listing.filename)
    assert error.value.status_code == 403
    result = marketplace_render.render_listing_previews(listing.id)
    assert result["ok"] is False, result
    assert env.activations == []
    assert assets(listing.id) == []
    assert thumbnail_id(listing.id) is None
    staged = cells.personal_root(listing.email) / ".marketplace" / listing.id / listing.asset_id / listing.filename
    assert not staged.exists()


@pytest.mark.parametrize("emit_outputs", [True, False], ids=["requested-files-win", "reply-cannot-substitute"])
def test_reply_output_paths_are_non_authoritative(marketplace_cells, tmp_path, emit_outputs):
    env = marketplace_cells
    listing = env.listing()
    outside = tmp_path / "other-tenant-preview.png"
    foreign_png = png(0, 255, 0)
    outside.write_bytes(foreign_png)
    env.behavior.reply_path = str(outside)
    env.behavior.emit_outputs = emit_outputs
    result = marketplace_render.render_listing_previews(listing.id)
    if emit_outputs:
        seller = cells.resolve_workspace(listing.email)
        assert_previews(listing, result, env.fakes[seller.cell_id])
        assert all((listing.root / row["rel_path"]).read_bytes() != foreign_png for row in assets(listing.id))
    else:
        assert result["ok"] is False, result
        assert assets(listing.id) == []
        assert thumbnail_id(listing.id) is None
    assert outside.read_bytes() == foreign_png


@pytest.mark.parametrize("image", [b"not a PNG", PNG_BY_METHOD["doc_render_contact_sheet"][:-12], png(1, 2, 3, width=4097)],
                         ids=["invalid-png", "missing-iend", "oversized-dimensions"])
def test_invalid_render_files_never_publish_partial_assets(marketplace_cells, image):
    env = marketplace_cells
    listing = env.listing()
    env.behavior.images["doc_render_contact_sheet"] = image
    result = marketplace_render.render_listing_previews(listing.id)
    assert result["ok"] is False, result
    assert assets(listing.id) == []
    assert thumbnail_id(listing.id) is None


def test_upload_stages_source_before_preview_render(marketplace_cells, client, as_user):
    env = marketplace_cells
    listing = env.listing(source=False)
    response = client.post(f"/api/studio/listings/{listing.id}/assets", headers=as_user(listing.email),
                           files={"file": (listing.filename, STEP, "application/octet-stream")},
                           data={"kind": "source", "role": "primary", "gated": "true"})
    assert response.status_code == 200, response.text
    uploaded = response.json()["asset"]
    listing.asset_id = uploaded["id"]
    staged_relative = f".marketplace/{listing.id}/{listing.asset_id}/{listing.filename}"
    assert (cells.personal_root(listing.email) / staged_relative).read_bytes() == STEP
    rows = assets(listing.id, "source")
    assert len(rows) == 1 and str(rows[0]["id"]) == listing.asset_id
    assert (listing.root / rows[0]["rel_path"]).read_bytes() == STEP
    assert env.activations == [], "upload staging must not spawn the engine"
    rendered = client.post(f"/api/marketplace/listings/{listing.id}/render", headers=as_user(listing.email))
    assert rendered.status_code == 200, rendered.text
    seller = cells.resolve_workspace(listing.email)
    assert_previews(listing, rendered.json(), env.fakes[seller.cell_id])


def test_seller_thumbnail_is_preserved(marketplace_cells):
    env = marketplace_cells
    listing = env.listing()
    seller_thumbnail = str(uuid.uuid4())
    data = png(0, 255, 0)
    relative = "uploads/seller-thumbnail.png"
    (listing.root / relative).write_bytes(data)
    with tx() as cur:
        cur.execute("INSERT INTO mk_assets(id,listing_id,kind,role,filename,rel_path,content_type,"
                    "format,bytes,sha256,gated) VALUES(%s,%s,'image','thumbnail','seller-thumbnail.png',"
                    "%s,'image/png','png',%s,%s,FALSE)",
                    (seller_thumbnail, listing.id, relative, len(data), hashlib.sha256(data).hexdigest()))
        cur.execute("UPDATE mk_listings SET thumbnail_asset_id=%s WHERE id=%s",
                    (seller_thumbnail, listing.id))
    result = marketplace_render.render_listing_previews(listing.id)
    assert result["ok"] is True, result
    assert result["thumbnail_asset_id"] == seller_thumbnail == thumbnail_id(listing.id)
    rows = assets(listing.id)
    assert len(rows) == 2
    assert {row["role"] for row in rows} == {"gallery", "contact_sheet"}
    assert {str(row["id"]) for row in rows} == set(result["asset_ids"])
    assert all(row["gated"] is False for row in rows)
    assert (listing.root / relative).read_bytes() == data
    assert len(assets(listing.id, "image")) == 1


def test_viewer_method_policy_prevents_write_render_rpc(marketplace_cells, monkeypatch, tmp_path):
    env = marketplace_cells
    listing = env.listing()
    policy = cells.method_policy()
    policy["doc_render_png"] = {"cell": "allow", "right": "write", "scope": "workspace"}
    policy_path = tmp_path / "preview-methods.json"
    policy_path.write_text(json.dumps(policy))
    monkeypatch.setattr(cells, "POLICY_PATH", policy_path)
    result = marketplace_render.render_listing_previews(listing.id)
    assert result["ok"] is False, result
    assert assets(listing.id) == []
    assert thumbnail_id(listing.id) is None
    assert all(call["method"] not in PNG_BY_METHOD for fake in env.fakes.values() for call in fake.calls)


def test_upload_refuses_symlinked_seller_stage_without_committing_source(
        marketplace_cells, client, as_user, tmp_path):
    env = marketplace_cells
    listing = env.listing(source=False)
    seller = cells.resolve_workspace(listing.email)
    outside = tmp_path / "foreign-workspace"
    outside.mkdir()
    sentinel = outside / "private.step"
    sentinel.write_bytes(b"foreign-private")
    (seller.root / ".marketplace").symlink_to(outside, target_is_directory=True)
    response = client.post(f"/api/studio/listings/{listing.id}/assets", headers=as_user(listing.email),
                           files={"file": (listing.filename, STEP, "application/octet-stream")},
                           data={"kind": "source", "role": "primary", "gated": "true"})
    assert response.status_code == 403, response.text
    assert assets(listing.id, "source") == []
    assert assets(listing.id) == []
    assert env.activations == []
    assert sentinel.read_bytes() == b"foreign-private"
    assert not (outside / listing.id).exists()


@pytest.mark.parametrize("delete_listing", [False, True], ids=["asset", "listing"])
def test_deletion_removes_seller_cell_snapshot(marketplace_cells, client, as_user, delete_listing):
    listing = marketplace_cells.listing()
    relative = marketplace_render.stage_listing_asset(
        listing.email, listing.id, listing.asset_id, listing.source_relative, listing.filename)
    staged = cells.personal_root(listing.email) / relative
    assert staged.read_bytes() == STEP
    url = f"/api/studio/listings/{listing.id}"
    if not delete_listing:
        url += f"/assets/{listing.asset_id}"
    response = client.delete(url, headers=as_user(listing.email))
    assert response.status_code == 200, response.text
    assert not staged.exists()
    assert marketplace_cells.activations == []


def test_rerender_replaces_generated_assets_and_removes_obsolete_files(marketplace_cells):
    listing = marketplace_cells.listing()
    first = marketplace_render.render_listing_previews(listing.id)
    assert first["ok"] is True
    old_files = [listing.root / row["rel_path"] for row in assets(listing.id)]
    assert all(path.is_file() for path in old_files)
    second = marketplace_render.render_listing_previews(listing.id)
    assert second["ok"] is True, second
    assert set(first["asset_ids"]).isdisjoint(second["asset_ids"])
    assert len(assets(listing.id)) == 2
    assert all(not path.exists() for path in old_files)
    assert thumbnail_id(listing.id) == second["thumbnail_asset_id"]


def test_generated_assets_respect_seller_asset_quota(marketplace_cells, monkeypatch):
    listing = marketplace_cells.listing()
    preview_bytes = sum(len(image) for image in PNG_BY_METHOD.values())
    monkeypatch.setattr(settings, "marketplace_seller_quota_bytes", len(STEP) + preview_bytes - 1)
    result = marketplace_render.render_listing_previews(listing.id)
    assert result["ok"] is False
    assert result["detail"] == "seller asset quota exceeded"
    assert assets(listing.id) == []
    assert thumbnail_id(listing.id) is None
    assert len(assets(listing.id, "source")) == 1
