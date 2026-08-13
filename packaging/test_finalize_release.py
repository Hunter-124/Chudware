from __future__ import annotations
import importlib.util
import io
import tarfile
from argparse import Namespace
from pathlib import Path
import pytest

_path=Path(__file__).with_name("finalize_release.py")
_spec=importlib.util.spec_from_file_location("finalize_release", _path); assert _spec and _spec.loader
M=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(M)

def test_safe_extract_rejects_traversal(tmp_path: Path):
    archive=tmp_path/"bad.tar"
    with tarfile.open(archive,"w") as bundle:
        info=tarfile.TarInfo("../secret"); info.size=1; bundle.addfile(info,io.BytesIO(b"x"))
    with pytest.raises(M.FinalizeError, match="unsafe path"):
        M.safe_extract(archive,tmp_path/"out")

def test_foreign_platform_script_is_portable_complete_finalize_command(tmp_path: Path):
    sig=tmp_path/"payload.sig"; sig.write_text("sig")
    key=tmp_path/"release.pub"; key.write_text("pub")
    args=Namespace(platform="windows",arch="x86_64",version="1.2.3",channel="beta",out=tmp_path,payload_signature=sig,release_public_key=str(key))
    handoff=tmp_path/"handoff"; handoff.mkdir()
    script=M.host_script(args,handoff)
    text=script.read_text()
    assert "services.installer.build_release" in text
    assert "finalize_release.py complete" in text
    assert "portable-inputs" in text
    assert str(tmp_path.resolve()) not in text
    assert "TODO" not in text and "no-op" not in text

def test_evidence_requests_are_domain_separated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    artifact=tmp_path/"a.AppImage"; artifact.write_bytes(b"artifact")
    locked=tmp_path/"lock"; locked.write_bytes(b"lock")
    args=Namespace(out=tmp_path,version="1.2.3",platform="linux",arch="x86_64",commit="a"*40,locked_input=[locked],command=["smoke"])
    def fake_run(command, **_kwargs):
        out=Path(command[command.index("--out")+1]); out.mkdir(parents=True)
        for name in ("sbom.cdx.json","provenance.slsa.json","platform-test.v1.json"):
            (out/name).write_bytes(b"{}\n")
    monkeypatch.setattr(M.subprocess,"run",fake_run)
    requests=M.evidence_requests(args,artifact,[locked])
    assert [p.read_bytes().split(b"\0",1)[0] for p in requests] == [b"chudware-sbom-v1",b"chudware-provenance-v1",b"chudware-platform-test-v1"]

def _prepared(tmp_path: Path, *, channel="stable"):
    root=tmp_path/"handoff"; root.mkdir()
    lock=root/"requirements.lock"; lock.write_bytes(b"lock")
    payload={"key_id":"release-v1"}
    (root/"payload-manifest.v1.json").write_bytes(M.canonical_json_bytes(payload))
    metadata={"commit":"a"*40,"version":"1.2.3","channel":channel,"platform":"windows","arch":"x86_64","locked_inputs":[{"path":"requirements.lock","sha256":M.digest(lock)}]}
    (root/"handoff.json").write_bytes(M.canonical_json_bytes(metadata))
    args=Namespace(handoff_metadata=None,commit="a"*40,version="1.2.3",channel=channel,platform="windows",arch="x86_64",locked_input=[])
    return root,args

@pytest.mark.parametrize(("field","replacement"),[("commit","b"*40),("version","9.9.9"),("channel","beta")])
def test_prepared_identity_replacements_rejected(tmp_path: Path, field: str, replacement: str):
    root,args=_prepared(tmp_path)
    setattr(args,field,replacement)
    with pytest.raises(M.FinalizeError,match="identity mismatch"):
        M.load_handoff(args,root)

def test_replaced_lock_and_key_id_derivation(tmp_path: Path):
    root,args=_prepared(tmp_path)
    _metadata,_manifest,key_id=M.load_handoff(args,root)
    assert key_id == "release-v1"
    (root/"requirements.lock").write_bytes(b"tampered")
    with pytest.raises(M.FinalizeError,match="locked input was replaced"):
        M.load_handoff(args,root)

def test_unsigned_stable_windows_rejected_but_beta_truthful(tmp_path: Path):
    artifact=tmp_path/"installer.exe"; artifact.write_bytes(b"exe")
    stable=Namespace(channel="stable",platform="windows",platform_signing_evidence=None,artifact=artifact)
    with pytest.raises(M.FinalizeError,match="Authenticode|authenticode"):
        M._stable_platform_evidence(stable)
    stable.channel="beta"
    M._stable_platform_evidence(stable)
